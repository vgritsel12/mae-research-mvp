from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import ROOT_DIR
from app.domain.models import Article, MaeComponentCell, MaeComponentSnapshot, ResearchView
from app.services.component_engine import (
    CONTEXT_ONLY_RESEARCH,
    DIRECT_RESEARCH,
    INSUFFICIENT,
    REJECTED_RESEARCH,
    SEMI_DIRECT_RESEARCH,
    canonical_provider_info,
    core_component_cells,
    exposure_weight,
    load_provider_registry,
    research_acceptance,
)


VALIDATION_PASSED = "PASSED"
VALIDATION_INSUFFICIENT = "INSUFFICIENT"
SCORE_TYPE_MAE_CONSENSUS = "MAE_CONSENSUS"
SCORE_TYPE_MODEL_BASED = "MODEL_BASED"
SCORE_TYPE_INSUFFICIENT = "INSUFFICIENT"

CSV_COLUMNS = [
    "snapshot_date",
    "cell_id",
    "research_score",
    "data_score",
    "market_score",
    "final_score",
    "benchmark",
    "independent_layers",
    "duplicate_signal_warning",
    "lookahead_warning",
    "validation_status",
    "score_type",
    "validation_comment",
]

LATEST_CSV_COLUMNS = [
    "snapshot_date",
    "cell_id",
    "cell_name",
    "component",
    "factor_name",
    "raw_value",
    "normalized_value",
    "contribution",
    "provider",
    "source_url",
    "observation_date",
    "publication_date",
    "benchmark",
    "direct_or_inherited",
    "evidence_family",
    "provider_family",
    "score_type",
    "validation_warning",
    "final_component_score",
    "final_mae_score",
    "validation_status",
]

RESEARCH_ITEMS_COLUMNS = [
    "cell_id",
    "provider",
    "provider_family",
    "title",
    "author",
    "publication_date",
    "canonical_url",
    "horizon",
    "geography",
    "asset_class",
    "segment",
    "stance",
    "excerpt",
    "drivers",
    "risks",
    "classification",
    "accepted_for_score",
    "score_contribution",
]

REJECTED_RESEARCH_COLUMNS = [
    "provider",
    "title",
    "URL",
    "rejection_reason",
    "attempted_cell",
    "classification",
]


def validate_core_history(session: Session, output_path: Path | None = None) -> dict[str, Any]:
    output_path = output_path or ROOT_DIR / "outputs" / "mae_core_validation.csv"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    passed = 0
    insufficient = 0
    snapshots = list(
        session.scalars(
            select(MaeComponentSnapshot)
            .where(MaeComponentSnapshot.is_demo.is_(False))
            .order_by(MaeComponentSnapshot.snapshot_date)
        ).all()
    )
    for snapshot in snapshots:
        cells = list(
            session.scalars(
                select(MaeComponentCell)
                .where(MaeComponentCell.snapshot_id == snapshot.id)
                .order_by(MaeComponentCell.asset_bucket, MaeComponentCell.template_row_key, MaeComponentCell.region)
            ).all()
        )
        for cell in cells:
            row = validate_cell(cell)
            rows.append(row)
            if row["validation_status"] == VALIDATION_PASSED:
                passed += 1
                cell.validation_status = VALIDATION_PASSED
                cell.validation_comment = "Проверка пройдена."
            else:
                insufficient += 1
                _downgrade_cell(cell, row["validation_comment"])
    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    session.flush()
    return {
        "path": str(output_path),
        "total": len(rows),
        "passed": passed,
        "insufficient": insufficient,
    }


def validate_cell(cell: MaeComponentCell) -> dict[str, Any]:
    layers = [layer for layer in (cell.component_coverage or []) if _layer_score(cell, layer) is not None]
    duplicate_warning = duplicate_signal_warning(cell.factor_details or {})
    lookahead_warning = lookahead_warning_for_cell(cell)
    comments: list[str] = []
    if len(set(layers)) < 2:
        comments.append("Доступно меньше двух независимых слоёв.")
    if duplicate_warning:
        comments.append("Один underlying signal используется в нескольких слоях.")
    if lookahead_warning:
        comments.append("Есть показатель или источник с датой позже snapshot.")
    if not cell.benchmark:
        comments.append("Бенчмарк не задан.")
    if cell.data_score is not None and not has_cell_specific_data(cell.factor_details or {}):
        comments.append("Фундаментальный компонент не содержит cell-specific показатель.")
    if cell.market_score is not None and not has_benchmark_relative_market(cell.factor_details or {}):
        comments.append("Рыночный компонент не содержит benchmark-relative расчёт.")
    if cell.research_score is not None:
        families = research_provider_families(cell.factor_details or {}, scored_only=True)
        if len(families) < 2:
            comments.append("Research Score требует минимум две независимые provider families.")
        if not has_direct_research(cell.factor_details or {}):
            comments.append("Research Score требует минимум один DIRECT материал.")
    if cell.research_score is None and cell.data_score is not None and cell.market_score is not None and cell.composite_score is not None:
        if abs(cell.composite_score) > 1:
            comments.append("Data+Market без полноценного Research Score должны быть ограничены диапазоном -1..+1.")
    if cell.research_score is not None and len(set(layers)) == 2 and cell.composite_score is not None and abs(cell.composite_score) > 2:
        comments.append("Research плюс один дополнительный слой должны быть ограничены диапазоном -2..+2.")
    if cell.composite_score is not None and abs(cell.composite_score) == 3 and len(set(layers)) < 3:
        comments.append("Оценка ±3 разрешена только при наличии Research, Data и Market.")
    if cell.composite_score is not None and abs(cell.composite_score) == 3 and _has_aging_or_carried_research(cell.factor_details or {}):
        if not _has_fresh_data_or_market_confirmation(cell):
            comments.append("Оценка ±3 не допускается для перенесённого старого Research без свежего подтверждения Data или Market.")
    comments.extend(_cell_specific_gate_comments(cell))
    if cell.composite_score is None:
        comments.append("Итоговая оценка отсутствует до validation.")
    status = VALIDATION_PASSED if not comments else VALIDATION_INSUFFICIENT
    comment = "Проверка пройдена." if status == VALIDATION_PASSED else " ".join(comments)
    score_type = score_type_for_cell(cell, status == VALIDATION_PASSED)
    return {
        "snapshot_date": cell.snapshot_date.isoformat(),
        "cell_id": cell.canonical_cell_id,
        "research_score": _blank_none(cell.research_score),
        "data_score": _blank_none(cell.data_score),
        "market_score": _blank_none(cell.market_score),
        "final_score": _blank_none(cell.composite_score) if status == VALIDATION_PASSED else "",
        "benchmark": cell.benchmark,
        "independent_layers": ",".join(sorted(set(layers))),
        "duplicate_signal_warning": duplicate_warning,
        "lookahead_warning": lookahead_warning,
        "validation_status": status,
        "validation_comment": comment,
        "score_type": score_type,
    }


def score_type_for_cell(cell: MaeComponentCell, passed: bool = True) -> str:
    if not passed or cell.composite_score is None:
        return SCORE_TYPE_INSUFFICIENT
    if cell.research_score is not None and (cell.data_score is not None or cell.market_score is not None):
        return SCORE_TYPE_MAE_CONSENSUS
    if cell.research_score is None and cell.data_score is not None and cell.market_score is not None:
        return SCORE_TYPE_MODEL_BASED
    return SCORE_TYPE_INSUFFICIENT


def validate_latest_component_snapshot(
    session: Session,
    *,
    snapshot_date: date | None = None,
    factor_output_path: Path | None = None,
    summary_output_path: Path | None = None,
) -> dict[str, Any]:
    snapshot = latest_snapshot_for_validation(session, snapshot_date=snapshot_date)
    if snapshot is None:
        raise RuntimeError("Нет component snapshot для latest validation.")
    factor_output_path = factor_output_path or ROOT_DIR / "outputs" / "mae_latest_validation.csv"
    summary_output_path = summary_output_path or ROOT_DIR / "outputs" / "mae_latest_validation_summary.json"
    factor_output_path.parent.mkdir(parents=True, exist_ok=True)
    summary_output_path.parent.mkdir(parents=True, exist_ok=True)

    cells = list(
        session.scalars(
            select(MaeComponentCell)
            .where(MaeComponentCell.snapshot_id == snapshot.id)
            .order_by(MaeComponentCell.asset_bucket, MaeComponentCell.template_row_key, MaeComponentCell.region)
        ).all()
    )
    factor_rows: list[dict[str, Any]] = []
    passed = 0
    insufficient = 0
    for cell in cells:
        row = validate_cell(cell)
        if row["validation_status"] == VALIDATION_PASSED:
            passed += 1
            cell.validation_status = VALIDATION_PASSED
            cell.validation_comment = "Проверка пройдена."
        else:
            insufficient += 1
            _downgrade_cell(cell, row["validation_comment"])
        factor_rows.extend(factor_level_rows(cell, row))

    with factor_output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=LATEST_CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(factor_rows)

    summary = latest_validation_summary(cells, snapshot, passed=passed, insufficient=insufficient)
    research_output_paths = write_latest_research_item_outputs(session, snapshot, cells)
    summary.update(research_output_paths["summary_counts"])
    summary_output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    session.flush()
    return {
        "path": str(factor_output_path),
        "summary_path": str(summary_output_path),
        "research_items_path": research_output_paths["research_items_path"],
        "rejected_research_items_path": research_output_paths["rejected_research_items_path"],
        "total": len(cells),
        "passed": passed,
        "insufficient": insufficient,
    }


def latest_snapshot_for_validation(session: Session, *, snapshot_date: date | None = None) -> MaeComponentSnapshot | None:
    query = select(MaeComponentSnapshot).where(MaeComponentSnapshot.is_demo.is_(False))
    if snapshot_date is not None:
        query = query.where(MaeComponentSnapshot.snapshot_date == snapshot_date)
    return session.scalar(query.order_by(desc(MaeComponentSnapshot.snapshot_date), desc(MaeComponentSnapshot.created_at)))


def duplicate_signal_warning(factor_details: dict[str, Any]) -> str:
    layers_by_signal: dict[str, set[str]] = defaultdict(set)
    for layer in ("research", "data", "market"):
        for factor in factor_details.get(layer, []) or []:
            signal = str(factor.get("underlying_signal") or factor.get("name") or "").strip()
            if not signal:
                continue
            layers_by_signal[signal].add(layer)
    duplicates = sorted(signal for signal, layers in layers_by_signal.items() if len(layers) > 1)
    return "; ".join(duplicates)


def lookahead_warning_for_cell(cell: MaeComponentCell) -> str:
    warnings: list[str] = []
    for layer in ("research", "data", "market"):
        for factor in (cell.factor_details or {}).get(layer, []) or []:
            end_date = _parse_date(factor.get("end_date") or factor.get("metadata", {}).get("current_date"))
            if end_date and end_date > cell.snapshot_date:
                warnings.append(f"{factor.get('name')}: {end_date.isoformat()}")
    for source in cell.source_details or []:
        published = _parse_date(source.get("publication_date"))
        if published and published > cell.snapshot_date:
            warnings.append(f"{source.get('provider')}: {published.isoformat()}")
    return "; ".join(warnings)


def has_benchmark_relative_market(factor_details: dict[str, Any]) -> bool:
    market_factors = factor_details.get("market", []) or []
    return any(bool(factor.get("benchmark_relative")) for factor in market_factors)


def has_cell_specific_data(factor_details: dict[str, Any]) -> bool:
    data_factors = factor_details.get("data", []) or []
    return any(bool(factor.get("cell_specific")) for factor in data_factors)


def has_direct_research(factor_details: dict[str, Any]) -> bool:
    research_factors = factor_details.get("research", []) or []
    return any(
        (factor.get("direct_or_semi_direct") == DIRECT_RESEARCH or factor.get("direct_or_inherited") == "direct")
        for factor in research_factors
    )


def has_inherited_research(factor_details: dict[str, Any]) -> bool:
    research_factors = factor_details.get("research", []) or []
    return any(
        (factor.get("direct_or_semi_direct") == SEMI_DIRECT_RESEARCH or factor.get("direct_or_inherited") == "semi_direct")
        for factor in research_factors
    )


def research_provider_families(factor_details: dict[str, Any], *, scored_only: bool = False) -> set[str]:
    families: set[str] = set()
    for factor in factor_details.get("research", []) or []:
        if scored_only and not factor.get("accepted_for_score"):
            continue
        family = factor.get("provider_family") or factor.get("provider_group")
        if family:
            families.add(str(family))
    return families


def _has_aging_or_carried_research(factor_details: dict[str, Any]) -> bool:
    for factor in factor_details.get("research", []) or []:
        if factor.get("carried_forward") or factor.get("lifecycle_status") == "AGING":
            return True
    return False


def _has_fresh_data_or_market_confirmation(cell: MaeComponentCell) -> bool:
    if cell.composite_score is None:
        return False
    direction = 1 if cell.composite_score > 0 else -1 if cell.composite_score < 0 else 0
    if direction == 0:
        return True
    return any(
        score is not None and (1 if score > 0 else -1 if score < 0 else 0) == direction
        for score in (cell.data_score, cell.market_score)
    )


def _cell_specific_gate_comments(cell: MaeComponentCell) -> list[str]:
    comments: list[str] = []
    row = cell.template_row_key
    factors = cell.factor_details or {}
    market_factors = factors.get("market", []) or []
    market_names = {str(factor.get("name") or "") for factor in market_factors}
    if row == "EQUITY|Wide Market|Wide Market" and cell.market_score is not None:
        if not any(factor.get("benchmark_relative") and "global" in str(factor.get("series", "")).casefold() for factor in market_factors):
            comments.append("US Wide Market не содержит сравнение с approved Global Equity benchmark.")
    if row == "EQUITY|Other categories|Market Breadth (Equal Weight)":
        if cell.market_score is None or not has_benchmark_relative_market(factors):
            comments.append("Market Breadth не содержит equal-weight versus cap-weight market indicator.")
    if row == "EQUITY|Other categories|Small Cap":
        if (cell.market_score is None or not has_benchmark_relative_market(factors)) and not has_direct_research(factors):
            comments.append("Small Cap не содержит small-cap versus large/wide market indicator.")
    if row == "COMMODITIES|Commodities|Gold":
        has_approved_gold_price = any(
            factor.get("name") == "gold_price" and factor.get("approved_gold_price_indicator") is True
            for factor in market_factors
        )
        if not has_approved_gold_price:
            comments.append("Gold Market Score не содержит approved actual gold price или benchmark indicator.")
    if cell.research_score is not None and not has_direct_research(factors):
        comments.append("Research Score сформирован только из inherited/broad research.")
    return comments


def factor_level_rows(cell: MaeComponentCell, validation_row: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    factor_details = cell.factor_details or {}
    for component in ("research", "data", "market"):
        factors = factor_details.get(component, []) or []
        for factor in factors:
            rows.append(_factor_row(cell, validation_row, component, factor))
    return rows


def latest_validation_summary(
    cells: list[MaeComponentCell],
    snapshot: MaeComponentSnapshot,
    *,
    passed: int,
    insufficient: int,
) -> dict[str, Any]:
    providers: dict[str, int] = defaultdict(int)
    missing: dict[str, list[str]] = {}
    score_types = {SCORE_TYPE_MAE_CONSENSUS: 0, SCORE_TYPE_MODEL_BASED: 0, SCORE_TYPE_INSUFFICIENT: 0}
    for cell in cells:
        validation = validate_cell(cell)
        score_types[validation["score_type"]] = score_types.get(validation["score_type"], 0) + 1
        details = cell.factor_details or {}
        for layer in ("research", "data", "market"):
            for factor in details.get(layer, []) or []:
                provider = factor.get("provider") or factor.get("source") or _provider_from_url(factor.get("source_url") or factor.get("source"))
                if provider:
                    providers[str(provider)] += 1
        warnings = validation["validation_comment"]
        if warnings != "Проверка пройдена.":
            missing[cell.template_row_key] = [part.strip() for part in warnings.split(".") if part.strip()]
    missing_benchmarks = {
        cell: messages
        for cell, messages in missing.items()
        if any("benchmark" in message.casefold() or "бенчмарк" in message.casefold() for message in messages)
    }
    missing_fundamental = {
        cell: messages
        for cell, messages in missing.items()
        if any("фундамент" in message.casefold() or "cell-specific" in message.casefold() or "данн" in message.casefold() for message in messages)
    }
    for cell in cells:
        if cell.data_score is None:
            missing_fundamental.setdefault(cell.template_row_key, []).append("Нет полноценного Fundamental Data component.")
    return {
        "snapshot_date": snapshot.snapshot_date.isoformat(),
        "cells_total": len(cells),
        "passed": passed,
        "insufficient": insufficient,
        "mae_consensus_cells": score_types.get(SCORE_TYPE_MAE_CONSENSUS, 0),
        "model_based_cells": score_types.get(SCORE_TYPE_MODEL_BASED, 0),
        "insufficient_cells": score_types.get(SCORE_TYPE_INSUFFICIENT, 0),
        "scored_research_cells": len([cell for cell in cells if cell.research_score is not None]),
        "direct_research_cells": len([cell for cell in cells if has_direct_research(cell.factor_details or {})]),
        "semi_direct_research_cells": len(
            [
                cell
                for cell in cells
                if has_inherited_research(cell.factor_details or {}) and not has_direct_research(cell.factor_details or {})
            ]
        ),
        "cell_specific_data_cells": len([cell for cell in cells if has_cell_specific_data(cell.factor_details or {})]),
        "benchmark_relative_market_cells": len([cell for cell in cells if has_benchmark_relative_market(cell.factor_details or {})]),
        "provider_families_used": sorted(
            {
                family
                for cell in cells
                for family in research_provider_families(cell.factor_details or {}, scored_only=False)
            }
        ),
        "missing_indicators": missing,
        "missing_benchmarks": missing_benchmarks,
        "missing_fundamental_factors": missing_fundamental,
        "providers_used": dict(sorted(providers.items())),
    }


def write_latest_research_item_outputs(
    session: Session,
    snapshot: MaeComponentSnapshot,
    cells: list[MaeComponentCell],
    *,
    research_items_path: Path | None = None,
    rejected_items_path: Path | None = None,
) -> dict[str, Any]:
    research_items_path = research_items_path or ROOT_DIR / "outputs" / "research_items_latest.csv"
    rejected_items_path = rejected_items_path or ROOT_DIR / "outputs" / "rejected_research_items.csv"
    research_items_path.parent.mkdir(parents=True, exist_ok=True)
    rejected_items_path.parent.mkdir(parents=True, exist_ok=True)

    accepted_rows = _accepted_research_item_rows(cells)
    rejected_rows = _rejected_research_item_rows(session, snapshot)
    with research_items_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=RESEARCH_ITEMS_COLUMNS)
        writer.writeheader()
        writer.writerows(accepted_rows)
    with rejected_items_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=REJECTED_RESEARCH_COLUMNS)
        writer.writeheader()
        writer.writerows(rejected_rows)
    irrelevant_mapping_count = len([row for row in rejected_rows if str(row.get("rejection_reason", "")).startswith("disallowed_mapping")])
    return {
        "research_items_path": str(research_items_path),
        "rejected_research_items_path": str(rejected_items_path),
        "summary_counts": {
            "research_items_count": len(accepted_rows),
            "rejected_research_count": len(rejected_rows),
            "irrelevant_mapping_count": irrelevant_mapping_count,
        },
    }


def _accepted_research_item_rows(cells: list[MaeComponentCell]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for cell in cells:
        for factor in (cell.factor_details or {}).get("research", []) or []:
            classification = factor.get("direct_or_semi_direct")
            if classification not in {DIRECT_RESEARCH, SEMI_DIRECT_RESEARCH}:
                continue
            key = (
                str(cell.canonical_cell_id),
                str(factor.get("provider_family") or factor.get("provider_group") or ""),
                str(factor.get("source_url") or factor.get("title") or ""),
                str(classification or ""),
            )
            grouped.setdefault(
                key,
                {
                    "cell_id": cell.canonical_cell_id,
                    "provider": factor.get("provider") or factor.get("source") or "",
                    "provider_family": factor.get("provider_family") or factor.get("provider_group") or "",
                    "title": factor.get("title") or "",
                    "author": factor.get("author") or "",
                    "publication_date": factor.get("publication_date") or "",
                    "canonical_url": factor.get("source_url") or "",
                    "horizon": factor.get("horizon") or "",
                    "geography": factor.get("geography") or "",
                    "asset_class": factor.get("asset_class") or "",
                    "segment": factor.get("segment") or "",
                    "stance": factor.get("extracted_stance") or "",
                    "excerpt": factor.get("source_excerpt") or "",
                    "drivers": "; ".join(factor.get("drivers") or []),
                    "risks": "; ".join(factor.get("risks") or []),
                    "classification": classification,
                    "accepted_for_score": str(bool(factor.get("accepted_for_score"))).lower(),
                    "score_contribution": "" if factor.get("score") is None else factor.get("score"),
                },
            )
    return sorted(grouped.values(), key=lambda row: (row["cell_id"], row["provider_family"], row["title"]))


def _rejected_research_item_rows(session: Session, snapshot: MaeComponentSnapshot) -> list[dict[str, Any]]:
    registry = load_provider_registry()
    cells = core_component_cells()
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for view in session.scalars(select(ResearchView).order_by(ResearchView.template_row_key, ResearchView.region)).all():
        article = session.get(Article, view.article_id)
        if article is not None and (article.is_demo or view.is_demo):
            continue
        for cell in cells:
            weight, mapping_rule = exposure_weight(view, cell)
            if weight <= 0:
                continue
            assessment = research_acceptance(view, article, cell, snapshot.snapshot_date, registry=registry)
            if assessment["accepted"]:
                continue
            provider = canonical_provider_info(article.source if article else None, registry)
            title = article.title if article else ""
            url = (article.canonical_url or article.url or article.source_reference) if article else ""
            reason = assessment.get("rejection_reason") or f"not_accepted:{mapping_rule}"
            classification = CONTEXT_ONLY_RESEARCH if str(reason).startswith("disallowed_mapping") else REJECTED_RESEARCH
            key = (provider["provider"], title, url, cell.canonical_cell_id, reason)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "provider": provider["provider"],
                    "title": title,
                    "URL": url,
                    "rejection_reason": reason,
                    "attempted_cell": f"{cell.template_row_key} | {cell.region}",
                    "classification": classification,
                }
            )
    return sorted(rows, key=lambda row: (row["provider"], row["attempted_cell"], row["title"], row["rejection_reason"]))


def _downgrade_cell(cell: MaeComponentCell, comment: str) -> None:
    details = dict(cell.factor_details or {})
    details["pre_validation_composite_score"] = cell.composite_score
    cell.factor_details = details
    cell.composite_score = None
    cell.score_change = None
    cell.divergence_status = INSUFFICIENT
    cell.validation_status = VALIDATION_INSUFFICIENT
    cell.validation_comment = comment


def _factor_row(cell: MaeComponentCell, validation_row: dict[str, Any], component: str, factor: dict[str, Any]) -> dict[str, Any]:
    score = factor.get("score") if factor else None
    source_url = factor.get("source_url") or factor.get("url") or factor.get("source") or ""
    warnings = []
    if factor.get("not_scored_reason"):
        warnings.append(str(factor["not_scored_reason"]))
    if validation_row["validation_status"] != VALIDATION_PASSED:
        warnings.append(validation_row["validation_comment"])
    return {
        "snapshot_date": cell.snapshot_date.isoformat(),
        "cell_id": cell.canonical_cell_id,
        "cell_name": f"{cell.template_row_key} | {cell.region}",
        "component": component,
        "factor_name": factor.get("name") or factor.get("title") or factor.get("source") or "",
        "raw_value": _blank_none(factor.get("raw_value")),
        "normalized_value": _blank_none(score),
        "contribution": _blank_none(score),
        "provider": factor.get("provider") or factor.get("source") or _provider_from_url(source_url),
        "source_url": source_url,
        "observation_date": factor.get("end_date") or (factor.get("metadata") or {}).get("current_date") or "",
        "publication_date": factor.get("publication_date") or "",
        "benchmark": factor.get("benchmark") or cell.benchmark,
        "direct_or_inherited": factor.get("direct_or_inherited") or ("direct" if component in {"data", "market"} and factor else ""),
        "evidence_family": factor.get("provider_group") or factor.get("underlying_signal") or "",
        "provider_family": factor.get("provider_family") or factor.get("provider_group") or "",
        "score_type": validation_row.get("score_type", SCORE_TYPE_INSUFFICIENT),
        "validation_warning": " ".join(warnings),
        "final_component_score": _blank_none(_layer_score(cell, component)),
        "final_mae_score": _blank_none(cell.composite_score) if validation_row["validation_status"] == VALIDATION_PASSED else "",
        "validation_status": validation_row["validation_status"],
    }


def _layer_score(cell: MaeComponentCell, layer: str) -> int | None:
    return {
        "research": cell.research_score,
        "data": cell.data_score,
        "market": cell.market_score,
    }.get(layer)


def _blank_none(value: Any) -> Any:
    return "" if value is None else value


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _provider_from_url(value: Any) -> str:
    text = str(value or "")
    if "fred.stlouisfed.org" in text:
        return "FRED"
    if not text:
        return ""
    return text.split("/")[2] if "://" in text and len(text.split("/")) > 2 else text[:80]
