from __future__ import annotations

import csv
import statistics
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.domain.enums import EvidenceStatus, PricingStatus
from app.domain.models import EvidenceObservation, ResearchView, ScenarioAssessment, ScenarioCard
from app.services.security import validate_public_http_url


IMPORTANCE_WEIGHT = {"STANDARD": 1.0, "CRITICAL": 2.0}

ASSET_EVIDENCE_KEYWORDS = {
    "EQUITY_GROWTH_TECH": {
        "eps",
        "earnings",
        "guidance",
        "market breadth",
        "valuation",
        "software",
        "semiconductor",
        "demand",
        "real yield",
        "real yields",
    },
    "EUROPE_GOV_SHORT": {
        "ecb",
        "policy-rate",
        "policy rate",
        "short-end",
        "short end",
        "sovereign yield",
        "sovereign yields",
        "inflation",
        "consensus",
        "wage",
        "pmi",
        "market-implied",
        "market implied",
        "cuts",
    },
    "GLOBAL_GOLD": {
        "real yield",
        "real yields",
        "dxy",
        "etf",
        "central-bank",
        "central bank",
        "inflation expectation",
        "inflation expectations",
        "geopolitical",
        "volatility",
    },
}


class EvidenceEngine:
    def add_observation(
        self,
        session: Session,
        scenario_id: str,
        indicator: str,
        observation_date: date,
        expected: str,
        actual: str,
        source_url: str,
        support_value: int,
        importance: str = "STANDARD",
        surprise: str = "",
        market_implied: str = "",
        is_demo: bool = False,
    ) -> EvidenceObservation:
        if support_value not in {-1, 0, 1}:
            raise ValueError("support_value must be -1, 0 or +1")
        self.validate_observation_for_scenario(session, scenario_id, indicator, source_url, is_demo)
        row = EvidenceObservation(
            scenario_id=scenario_id,
            indicator=indicator,
            observation_date=observation_date,
            expected=expected,
            actual=actual,
            surprise=surprise,
            market_implied=market_implied,
            direction_for_scenario="supports" if support_value > 0 else "contradicts" if support_value < 0 else "neutral",
            importance=importance,
            source_url=source_url,
            support_value=support_value,
            is_demo=is_demo,
        )
        session.add(row)
        session.flush()
        return row

    def assess_scenario(self, session: Session, scenario_id: str) -> ScenarioAssessment:
        scenario = session.get(ScenarioCard, scenario_id)
        if scenario is None:
            raise ValueError("Scenario not found")
        observations = session.scalars(select(EvidenceObservation).where(EvidenceObservation.scenario_id == scenario_id)).all()
        for row in observations:
            self.validate_observation_for_scenario(session, scenario_id, row.indicator, row.source_url, row.is_demo)
        status, weighted = self.calculate_evidence_status(observations)
        pricing_status, pricing_metrics = self.calculate_pricing_status(get_settings().demo_market_series_path)
        explanation = (
            f"{len(observations)} observations; weighted_score={weighted:.2f}; "
            f"pricing_status={pricing_status.value}."
        )
        existing = session.scalar(select(ScenarioAssessment).where(ScenarioAssessment.scenario_id == scenario_id))
        if existing:
            existing.evidence_status = status.value
            existing.weighted_score = weighted
            existing.pricing_status = pricing_status.value
            existing.pricing_metrics = pricing_metrics
            existing.explanation = explanation
            existing.assessed_at = datetime.now().astimezone()
            session.flush()
            return existing
        assessment = ScenarioAssessment(
            scenario_id=scenario_id,
            evidence_status=status.value,
            weighted_score=weighted,
            pricing_status=pricing_status.value,
            pricing_metrics=pricing_metrics,
            explanation=explanation,
            is_demo=scenario.is_demo,
        )
        session.add(assessment)
        session.flush()
        return assessment

    def validate_observation_for_scenario(
        self,
        session: Session,
        scenario_id: str,
        indicator: str,
        source_url: str,
        is_demo: bool,
    ) -> None:
        scenario = session.get(ScenarioCard, scenario_id)
        if scenario is None:
            raise ValueError("scenario_id does not exist")
        if not scenario.is_demo and is_demo:
            raise ValueError("DEMO evidence cannot be attached to a REAL scenario")
        if not scenario.is_demo and not _is_real_evidence_source(source_url):
            raise ValueError("REAL evidence requires a public URL or official:// source reference")
        family = self.scenario_asset_family(session, scenario)
        if not self.indicator_is_relevant(family, indicator):
            raise ValueError(f"Evidence indicator '{indicator}' is not relevant for scenario asset family {family}")

    @staticmethod
    def scenario_asset_family(session: Session, scenario: ScenarioCard) -> str:
        view = session.get(ResearchView, scenario.linked_view_id) if scenario.linked_view_id else None
        if view is None:
            raise ValueError("Scenario has no linked Research View")
        row_key = view.template_row_key.upper()
        if row_key.startswith("FIXED INCOME|GOV|SHORT TERM") and view.region in {"Europe", "Eurozone"}:
            return "EUROPE_GOV_SHORT"
        if row_key in {"GOLD|GOLD|GOLD", "COMMODITIES|COMMODITIES|GOLD", "COMMODITIES|COMMODITIES|OTHER PRECIOUS METALS"}:
            return "GLOBAL_GOLD"
        if row_key.startswith("EQUITY|OTHER CATEGORIES|GROWTH") or row_key.startswith("EQUITY|SECTOR|TECHNOLOGY"):
            return "EQUITY_GROWTH_TECH"
        if row_key.startswith("EQUITY|"):
            return "EQUITY_GROWTH_TECH"
        raise ValueError(f"No evidence relevance rule for scenario asset {view.template_row_key} / {view.region}")

    @staticmethod
    def indicator_is_relevant(asset_family: str, indicator: str) -> bool:
        lowered = indicator.casefold()
        return any(keyword in lowered for keyword in ASSET_EVIDENCE_KEYWORDS.get(asset_family, set()))

    @staticmethod
    def calculate_evidence_status(observations: Iterable[EvidenceObservation]) -> tuple[EvidenceStatus, float]:
        rows = list(observations)
        valid = [row for row in rows if row.source_url and row.actual and row.expected]
        weighted_sum = 0.0
        weight_total = 0.0
        critical_negative = False
        for row in valid:
            weight = IMPORTANCE_WEIGHT.get(row.importance, 1.0)
            weighted_sum += row.support_value * weight
            weight_total += weight
            if row.importance == "CRITICAL" and row.support_value == -1:
                critical_negative = True
        weighted_score = weighted_sum / weight_total if weight_total else 0.0
        if critical_negative or weighted_score <= -0.5:
            return EvidenceStatus.CONTRADICTED, weighted_score
        if len(valid) < 3:
            return EvidenceStatus.INSUFFICIENT_DATA, weighted_score
        if weighted_score >= 0.5:
            return EvidenceStatus.CONFIRMED, weighted_score
        return EvidenceStatus.MIXED, weighted_score

    @staticmethod
    def calculate_pricing_status(path: Path) -> tuple[PricingStatus, dict]:
        if not path.exists():
            return PricingStatus.UNKNOWN, {"reason": "market_series_missing"}
        with path.open("r", encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        if len(rows) < 3:
            return PricingStatus.UNKNOWN, {"reason": "insufficient_rows"}
        first = rows[0]
        last = rows[-1]
        try:
            asset_return = float(last["asset_price"]) / float(first["asset_price"]) - 1
            benchmark_return = float(last["benchmark_price"]) / float(first["benchmark_price"]) - 1
            relative_move = asset_return - benchmark_return
            daily_rel = []
            for prev, curr in zip(rows, rows[1:]):
                prev_asset = float(prev["asset_price"])
                prev_bench = float(prev["benchmark_price"])
                curr_asset = float(curr["asset_price"])
                curr_bench = float(curr["benchmark_price"])
                daily_rel.append((curr_asset / prev_asset - 1) - (curr_bench / prev_bench - 1))
            volatility = statistics.pstdev(daily_rel) * (len(daily_rel) ** 0.5) if len(daily_rel) >= 2 else 0.0
        except (KeyError, ValueError, ZeroDivisionError):
            return PricingStatus.UNKNOWN, {"reason": "bad_market_series"}
        floor_volatility = 0.05
        effective_vol = max(abs(volatility), floor_volatility)
        normalized_move = abs(relative_move) / effective_vol
        metrics = {
            "asset_return": asset_return,
            "benchmark_return": benchmark_return,
            "relative_move": relative_move,
            "rolling_volatility_proxy": volatility,
            "effective_volatility": effective_vol,
            "normalized_move": normalized_move,
        }
        if normalized_move < 0.5:
            return PricingStatus.NOT_PRICED, metrics
        if normalized_move < 1.0:
            return PricingStatus.PARTLY_PRICED, metrics
        return PricingStatus.PRICED_IN, metrics


def _is_real_evidence_source(source_url: str) -> bool:
    if source_url.startswith("official://"):
        return True
    ok, _ = validate_public_http_url(source_url)
    return ok
