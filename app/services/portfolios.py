from __future__ import annotations

"""Deprecated legacy allocation helper.

Portfolio construction is outside the current MAE scope. This module remains
only so older local exports/imports can be inspected without destructive data
removal. It is not imported by the active Streamlit navigation or new exports.
"""

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.enums import CoverageStatus, RunMode
from app.domain.models import MatrixScore
from app.services.normalization import mae_result_template_row_keys, parse_template_rows, template_region


REGIONS = ["Global", "US", "Europe", "UK", "Japan", "EM ex China"]

PROFILE_TARGETS: dict[str, dict[str, Decimal]] = {
    "Conservative": {
        "EQUITY": Decimal("25"),
        "FIXED INCOME": Decimal("63"),
        "GOLD": Decimal("7"),
        "LIQUIDITY": Decimal("5"),
    },
    "Moderate": {
        "EQUITY": Decimal("55"),
        "FIXED INCOME": Decimal("33"),
        "GOLD": Decimal("7"),
        "LIQUIDITY": Decimal("5"),
    },
    "Aggressive": {
        "EQUITY": Decimal("73"),
        "FIXED INCOME": Decimal("15"),
        "GOLD": Decimal("7"),
        "LIQUIDITY": Decimal("5"),
    },
}

PROFILE_LABELS = {
    "Conservative": "Консервативный",
    "Moderate": "Умеренный",
    "Aggressive": "Агрессивный",
}


@dataclass(frozen=True)
class PortfolioRow:
    profile: str
    asset_class: str
    asset_group: str
    asset_segment: str
    template_row_key: str
    region: str
    weight: Decimal
    mae_score: int | None
    coverage_status: str


class PortfolioEngine:
    def build_profiles(self, session: Session, mode: str | RunMode = RunMode.REAL.value) -> dict[str, list[PortfolioRow]]:
        mode_value = getattr(mode, "value", mode)
        matrix = self._matrix_by_cell(session, mode_value)
        template_rows = [row for row in parse_template_rows() if row.template_row_key in mae_result_template_row_keys()]
        profiles: dict[str, list[PortfolioRow]] = {}
        for profile, targets in PROFILE_TARGETS.items():
            rows: list[PortfolioRow] = []
            for asset_class, target in targets.items():
                if asset_class == "LIQUIDITY":
                    rows.append(
                        PortfolioRow(
                            profile=profile,
                            asset_class="LIQUIDITY",
                            asset_group="Cash",
                            asset_segment="Cash",
                            template_row_key="LIQUIDITY|Cash|Cash",
                            region="Global",
                            weight=target,
                            mae_score=0,
                            coverage_status=CoverageStatus.COVERED.value,
                        )
                    )
                    continue
                bucket_rows = [row for row in template_rows if _portfolio_asset_class(row.asset_class) == asset_class]
                cells: list[dict[str, Any]] = []
                for template_row in bucket_rows:
                    cell_regions = ["Global"] if asset_class == "GOLD" else REGIONS
                    for region in cell_regions:
                        score = matrix.get((template_row.template_row_key, region))
                        score_value = score.approved_score if score and score.approved_score is not None else 0
                        cells.append(
                            {
                                "row": template_row,
                                "region": region,
                                "score": score.approved_score if score else None,
                                "coverage": score.coverage_status if score else CoverageStatus.NO_DATA.value,
                                "raw": Decimal("1") + Decimal(str(score_value)) * Decimal("0.06"),
                            }
                        )
                total_raw = sum((cell["raw"] for cell in cells), Decimal("0")) or Decimal("1")
                for cell in cells:
                    weight = target * cell["raw"] / total_raw
                    template_row = cell["row"]
                    rows.append(
                        PortfolioRow(
                            profile=profile,
                            asset_class=asset_class,
                            asset_group=template_row.asset_group,
                            asset_segment=template_row.asset_segment,
                            template_row_key=template_row.template_row_key,
                            region=cell["region"],
                            weight=weight,
                            mae_score=cell["score"],
                            coverage_status=cell["coverage"],
                        )
                    )
            profiles[profile] = self._rounded_to_targets(rows, targets)
        return profiles

    def profile_summary(self, rows: list[PortfolioRow]) -> dict[str, Decimal]:
        totals: dict[str, Decimal] = {}
        for row in rows:
            totals[row.asset_class] = totals.get(row.asset_class, Decimal("0")) + row.weight
        total = sum(totals.values(), Decimal("0"))
        if total != Decimal("100.00"):
            raise ValueError(f"Portfolio allocation must sum to 100.00, got {total}")
        return totals

    @staticmethod
    def _matrix_by_cell(session: Session, mode: str) -> dict[tuple[str, str], MatrixScore]:
        rows = list(session.scalars(select(MatrixScore)).all())
        if mode == RunMode.DEMO.value:
            rows = [row for row in rows if row.is_demo]
        elif mode == RunMode.REAL.value:
            rows = [row for row in rows if not row.is_demo]
        return {
            (row.template_row_key, template_region(row.region)): row
            for row in rows
            if row.approved_score is not None
            and row.coverage_status not in {CoverageStatus.NO_DATA.value, CoverageStatus.INSUFFICIENT_EVIDENCE.value}
        }

    @staticmethod
    def _rounded_to_targets(rows: list[PortfolioRow], targets: dict[str, Decimal]) -> list[PortfolioRow]:
        rounded = [
            PortfolioRow(
                profile=row.profile,
                asset_class=row.asset_class,
                asset_group=row.asset_group,
                asset_segment=row.asset_segment,
                template_row_key=row.template_row_key,
                region=row.region,
                weight=row.weight.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                mae_score=row.mae_score,
                coverage_status=row.coverage_status,
            )
            for row in rows
        ]
        for asset_class, target in targets.items():
            indices = [idx for idx, row in enumerate(rounded) if row.asset_class == asset_class]
            if not indices:
                continue
            class_sum = sum((rounded[idx].weight for idx in indices), Decimal("0.00"))
            delta = target.quantize(Decimal("0.01")) - class_sum
            if not delta:
                continue
            target_index = max(indices, key=lambda idx: rounded[idx].weight)
            row = rounded[target_index]
            rounded[target_index] = _replace_weight(row, row.weight + delta)
        return rounded


def _portfolio_asset_class(asset_class: str) -> str:
    upper = asset_class.upper()
    if upper == "GOLD":
        return "GOLD"
    if upper == "EQUITY":
        return "EQUITY"
    if upper == "FIXED INCOME":
        return "FIXED INCOME"
    return upper


def _replace_weight(row: PortfolioRow, weight: Decimal) -> PortfolioRow:
    return PortfolioRow(
        profile=row.profile,
        asset_class=row.asset_class,
        asset_group=row.asset_group,
        asset_segment=row.asset_segment,
        template_row_key=row.template_row_key,
        region=row.region,
        weight=weight,
        mae_score=row.mae_score,
        coverage_status=row.coverage_status,
    )
