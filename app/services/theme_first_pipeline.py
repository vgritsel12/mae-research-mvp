from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import ROOT_DIR
from app.domain.enums import Confidence
from app.domain.models import Article, AtomicReleasePointer, Source
from app.domain.schemas import MarketSynthesisResponse, ThemeMatrixSynthesisResponse
from app.llm.provider import LLMProvider
from app.services.autonomous_matrix import load_previous_official_scores
from app.services.autonomous_release import AUTONOMOUS_CHANNEL
from app.services.normalization import CanonicalCell, canonical_cell_registry, normalize_space


THEME_SYNTHESIS_MODE = "THEME_SYNTHESIS"
PIPELINE_VERSION = "THEME_FIRST_MVP_V1"

LEGACY_CURATED_SOURCE_IDS = (
    "1fcdc947-738e-40c5-a3e4-46741d1e40be",  # Amundi geopolitics / global macro
    "30ea3d4d-e02c-45a4-b204-6f64b404649d",  # Deutsche economic and market outlook
    "7e4589ec-5e21-45b6-a6d9-dd7393877cb9",  # ECB June meeting
    "734aea60-12db-47e9-a725-7f46c3224762",  # JPM mid-year outlooks
    "9e5a8058-53a5-4436-a672-9d34b7cb4b50",  # BoE financial stability report
    "78a87082-6883-433d-b59f-b4c321d358af",  # BoE credit conditions
    "828ab633-9123-4ea0-bf28-59bb0e41cb33",  # HSBC global economics
    "79c48fd4-b9dd-4afc-8194-e703e33c66e8",  # HSBC EM sentiment
    "fc4e95a7-4b6e-4908-bb59-b5edd827e4a7",  # Amundi mid-year outlook
    "5087547d-2c0d-4f01-b99b-abe2947abcf6",  # Invesco emerging markets
    "a6f49be0-d9c8-47ba-86c6-d105ab570a48",  # BlackRock equity outlook
    "e498bd5c-57bb-4209-ae47-d56ecf0a7a3e",  # BlackRock fixed income outlook
    "10787051-0305-407b-a550-a517d55df41d",  # Morgan Stanley market/economic forecasts
    "e4e14349-33d6-4d2c-ba7a-3b43dec7d97f",  # Global X commodities tracker
    "84dc7053-3ae8-4075-a6fb-ad1906f3ed61",  # State Street gold outlook
)

EXCLUDED_TITLE_PATTERNS = (
    "pet industry",
    "world cup",
    "payments outlook",
    "tokenisation",
    "multifamily",
    "homepage",
    "podcast",
)


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=lambda item: item.isoformat() if isinstance(item, date) else str(item),
        ).encode("utf-8")
    ).hexdigest()


def run_theme_first_synthesis(
    session: Session,
    *,
    provider: LLMProvider,
    snapshot_date: date,
    previous_scores_path: Path,
    source_pack: list[dict[str, Any]],
    source_selection_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    previous_date, previous_scores = load_previous_theme_first_or_official_scores(session, previous_scores_path)
    stage1 = provider.synthesize_market_themes(
        [_llm_source_payload(item) for item in source_pack],
        {
            "snapshot_date": snapshot_date.isoformat(),
            "coverage_requirements": [
                "global macro",
                "US",
                "Europe",
                "UK",
                "Japan",
                "EM ex China",
                "equities",
                "government bonds",
                "credit",
                "commodities",
            ],
        },
    )
    _repair_stage1_source_lineage(stage1, source_pack)
    stage1_payload = build_stage1_payload(source_pack, stage1)
    if source_selection_manifest:
        stage1_payload["source_selection"] = {
            "selection_algorithm_version": source_selection_manifest.get("selection_algorithm_version"),
            "source_pack_hash": source_selection_manifest.get("source_pack_hash"),
            "selected_count": source_selection_manifest.get("selected_count"),
            "independent_provider_count": source_selection_manifest.get("independent_provider_count"),
        }
    stage2 = provider.synthesize_theme_matrix(
        {
            "snapshot_date": snapshot_date.isoformat(),
            "scoring_rules": {
                "-3": "strong negative conviction",
                "-2": "negative",
                "-1": "moderate negative",
                "0": "balanced / neutral",
                "1": "moderate positive",
                "2": "positive",
                "3": "strong positive conviction",
            },
            "previous_matrix_context_only": previous_scores,
            "canonical_template": [_template_payload(cell) for cell in canonical_cell_registry()],
            "admitted_sources": [_source_lineage_payload(item) for item in source_pack],
            "themes": [theme.model_dump(mode="json") for theme in stage1.themes],
        }
    )
    _repair_stage2_source_lineage(stage2, source_pack)
    confidence_adjustments = calibrate_stage2_confidence(stage2, source_pack)
    matrix_payload = build_theme_matrix_payload(
        stage2,
        stage1,
        source_pack=source_pack,
        snapshot_date=snapshot_date,
        previous_snapshot_date=previous_date,
        previous_scores=previous_scores,
    )
    scenario_payload = build_theme_scenario_payload(stage2, snapshot_date=snapshot_date)
    validation_errors = validate_theme_first_payloads(
        source_pack=source_pack,
        stage1=stage1,
        stage2=stage2,
        matrix_payload=matrix_payload,
        snapshot_date=snapshot_date,
    )
    return {
        "source_pack": source_pack,
        "source_selection_manifest": source_selection_manifest or {},
        "stage1": stage1_payload,
        "stage2_lineage": build_lineage_payload(stage1, stage2),
        "matrix": matrix_payload,
        "scenarios": scenario_payload,
        "validation_errors": validation_errors,
        "previous_snapshot_date": previous_date,
        "confidence_adjustments": confidence_adjustments,
    }


def select_legacy_curated_source_pack(session: Session, *, snapshot_date: date) -> list[dict[str, Any]]:
    """Legacy fixture helper retained for tests; production selection uses source_selection.py."""

    articles = {
        article.id: article
        for article in session.scalars(
            select(Article)
            .where(Article.id.in_(LEGACY_CURATED_SOURCE_IDS), Article.is_demo.is_(False), Article.publication_date <= snapshot_date)
            .order_by(Article.publication_date.desc(), Article.id)
        ).all()
    }
    selected: list[dict[str, Any]] = []
    for source_id in LEGACY_CURATED_SOURCE_IDS:
        article = articles.get(source_id)
        if article is None or _is_excluded(article.title):
            continue
        source = session.get(Source, article.source_id)
        if source is None or source.is_demo:
            continue
        selected.append(_source_pack_item(article, source))
    if not 10 <= len(selected) <= 20:
        raise ValueError(f"curated source pack must contain 10-20 documents; selected={len(selected)}")
    if len({item["provider"].casefold() for item in selected}) < 6:
        raise ValueError("curated source pack must contain at least 6 independent providers")
    return selected


def load_previous_theme_first_or_official_scores(
    session: Session,
    previous_scores_path: Path,
) -> tuple[date, dict[str, int | None]]:
    pointer = session.get(AtomicReleasePointer, AUTONOMOUS_CHANNEL)
    if pointer is not None:
        release_dir = Path(pointer.release_directory)
        if not release_dir.is_absolute():
            release_dir = ROOT_DIR / release_dir
        matrix_path = release_dir / "stage_04_matrix.json"
        try:
            payload = json.loads(matrix_path.read_text(encoding="utf-8"))
            cells = payload.get("cells") or []
            if len(cells) == len(canonical_cell_registry()):
                scores: dict[str, int | None] = {}
                for canonical, cell in zip(canonical_cell_registry(), cells, strict=True):
                    if cell.get("canonical_cell_id") != canonical.canonical_cell_id:
                        raise ValueError("theme-first previous matrix order mismatch")
                    scores[canonical.canonical_cell_id] = cell.get("current")
                return date.fromisoformat(str(payload.get("snapshot_date"))[:10]), scores
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    return load_previous_official_scores(previous_scores_path)


def build_stage1_payload(source_pack: list[dict[str, Any]], stage1: MarketSynthesisResponse) -> dict[str, Any]:
    source_ids = {item["source_id"] for item in source_pack}
    return {
        "schema_version": "THEME_FIRST_STAGE_1",
        "pipeline_version": PIPELINE_VERSION,
        "selection_method": "automatic_coverage_constrained_source_pack",
        "legacy_research_views_used": 0,
        "admitted_source_count": len(source_pack),
        "independent_provider_count": len(
            {str(item.get("canonical_provider_name") or item.get("provider") or "").casefold() for item in source_pack}
        ),
        "admitted_sources": [{k: v for k, v in item.items() if k != "content_excerpt"} for item in source_pack],
        "documents": [doc.model_dump(mode="json") for doc in stage1.documents],
        "theme_count": len(stage1.themes),
        "themes": [theme.model_dump(mode="json") for theme in stage1.themes],
        "source_manifest_hash": canonical_hash(sorted(source_ids)),
    }


def build_lineage_payload(stage1: MarketSynthesisResponse, stage2: ThemeMatrixSynthesisResponse) -> dict[str, Any]:
    theme_ids = {theme.theme_id for theme in stage1.themes}
    source_ids = {doc.source_id for doc in stage1.documents}
    return {
        "schema_version": "THEME_FIRST_LINEAGE_V1",
        "legacy_research_views_used": 0,
        "theme_ids": sorted(theme_ids),
        "source_ids": sorted(source_ids),
        "cell_count": len(stage2.cells),
        "scenario_types": sorted(str(scenario.scenario_id) for scenario in stage2.scenarios),
        "lineage_errors": _lineage_errors(stage1, stage2),
    }


def build_theme_matrix_payload(
    stage2: ThemeMatrixSynthesisResponse,
    stage1: MarketSynthesisResponse,
    *,
    source_pack: list[dict[str, Any]],
    snapshot_date: date,
    previous_snapshot_date: date,
    previous_scores: dict[str, int | None],
) -> dict[str, Any]:
    draft_by_id = {cell.canonical_cell_id: cell for cell in stage2.cells}
    source_by_id = {item["source_id"]: item for item in source_pack}
    theme_by_id = {theme.theme_id: theme for theme in stage1.themes}
    records: list[dict[str, Any]] = []
    for canonical in canonical_cell_registry():
        previous = previous_scores.get(canonical.canonical_cell_id)
        if canonical.applicability == "NOT_APPLICABLE":
            values = {
                "canonical_cell_id": canonical.canonical_cell_id,
                "row_index": canonical.row_index,
                "asset_class": canonical.asset_class,
                "asset_group": canonical.asset_group,
                "asset_segment": canonical.asset_segment,
                "region": canonical.region,
                "applicability": "NOT_APPLICABLE",
                "current": None,
                "previous": None,
                "delta": None,
                "mode": None,
                "confidence": None,
                "sources": [],
                "reasoning": "Not applicable under the signed canonical template.",
                "invalidation": "Applicability changes require a versioned template decision.",
                "transmission_chain": [],
                "registered_inputs": [],
                "supporting_theme_ids": [],
                "supporting_source_ids": [],
                "previous_snapshot_date": previous_snapshot_date.isoformat(),
                "assessment_date": snapshot_date.isoformat(),
            }
        else:
            draft = draft_by_id.get(canonical.canonical_cell_id)
            if draft is None:
                raise ValueError(f"missing synthesized cell: {canonical.canonical_cell_id}")
            sources = [_matrix_source(source_by_id[source_id]) for source_id in draft.supporting_source_ids if source_id in source_by_id]
            values = {
                "canonical_cell_id": canonical.canonical_cell_id,
                "row_index": canonical.row_index,
                "asset_class": canonical.asset_class,
                "asset_group": canonical.asset_group,
                "asset_segment": canonical.asset_segment,
                "region": canonical.region,
                "applicability": "APPLICABLE",
                "current": int(draft.score),
                "previous": previous,
                "delta": int(draft.score) - int(previous or 0),
                "mode": THEME_SYNTHESIS_MODE,
                "confidence": str(draft.confidence),
                "sources": sources,
                "reasoning": draft.thesis.strip(),
                "invalidation": draft.invalidation_condition.strip(),
                "transmission_chain": [
                    f"{theme_id}: {theme_by_id[theme_id].title}"
                    for theme_id in draft.supporting_theme_ids
                    if theme_id in theme_by_id
                ],
                "registered_inputs": [],
                "supporting_theme_ids": list(draft.supporting_theme_ids),
                "supporting_source_ids": list(draft.supporting_source_ids),
                "main_risk": draft.main_risk.strip(),
                "previous_snapshot_date": previous_snapshot_date.isoformat(),
                "assessment_date": snapshot_date.isoformat(),
            }
        values["business_hash"] = canonical_hash(values)
        records.append(values)
    result = {
        "schema_version": "THEME_FIRST_MATRIX_V1",
        "snapshot_date": snapshot_date.isoformat(),
        "previous_snapshot_date": previous_snapshot_date.isoformat(),
        "cells": records,
        "applicable_score_count": sum(cell["applicability"] == "APPLICABLE" for cell in records),
        "not_applicable_count": sum(cell["applicability"] == "NOT_APPLICABLE" for cell in records),
        "mode_counts": {
            THEME_SYNTHESIS_MODE: sum(cell["mode"] == THEME_SYNTHESIS_MODE for cell in records),
            "DIRECT": 0,
            "COMPOSITE": 0,
            "MODEL_INFERRED": 0,
            "CARRY_FORWARD": 0,
        },
    }
    result["business_hash"] = canonical_hash(result)
    return result


def build_theme_scenario_payload(stage2: ThemeMatrixSynthesisResponse, *, snapshot_date: date) -> dict[str, Any]:
    scenarios = []
    for scenario in stage2.scenarios:
        scenarios.append(
            {
                "assessment_date": snapshot_date.isoformat(),
                "scenario_id": str(scenario.scenario_id),
                "scenario_name": scenario.scenario_name,
                "narrative": scenario.narrative,
                "probability_band": scenario.probability_band,
                "causal_chain": scenario.causal_chain,
                "trigger": scenario.trigger,
                "veto": scenario.veto,
                "material_affected_cells": "; ".join(scenario.material_affected_cells),
                "provenance_mode": "THEME_FIRST_MVP",
            }
        )
    return {
        "schema_version": "THEME_FIRST_SCENARIOS_V1",
        "scenario_types": sorted(row["scenario_id"] for row in scenarios),
        "provenance_mode": "THEME_FIRST_MVP",
        "scenarios": scenarios,
    }


def validate_theme_first_payloads(
    *,
    source_pack: list[dict[str, Any]],
    stage1: MarketSynthesisResponse,
    stage2: ThemeMatrixSynthesisResponse,
    matrix_payload: dict[str, Any],
    snapshot_date: date,
) -> list[str]:
    errors: list[str] = []
    registry = canonical_cell_registry()
    applicable_ids = {cell.canonical_cell_id for cell in registry if cell.applicability == "APPLICABLE"}
    not_applicable_ids = {cell.canonical_cell_id for cell in registry if cell.applicability == "NOT_APPLICABLE"}
    source_ids = {item["source_id"] for item in source_pack}
    theme_ids = {theme.theme_id for theme in stage1.themes}
    cells = matrix_payload.get("cells") or []
    applicable = [cell for cell in cells if cell.get("applicability") == "APPLICABLE"]
    not_applicable = [cell for cell in cells if cell.get("applicability") == "NOT_APPLICABLE"]
    cell_ids = [cell.get("canonical_cell_id") for cell in applicable]
    if len(applicable) != 104:
        errors.append(f"APPLICABLE_COUNT_INVALID:{len(applicable)}")
    if len(not_applicable) != 10:
        errors.append(f"NOT_APPLICABLE_COUNT_INVALID:{len(not_applicable)}")
    if set(cell_ids) != applicable_ids:
        errors.append("APPLICABLE_CELL_SET_MISMATCH")
    if {cell.get("canonical_cell_id") for cell in not_applicable} != not_applicable_ids:
        errors.append("NOT_APPLICABLE_CELL_SET_MISMATCH")
    duplicates = [cell_id for cell_id, count in Counter(cell_ids).items() if count > 1]
    if duplicates:
        errors.append("DUPLICATE_CELLS:" + ",".join(sorted(duplicates)[:5]))
    for item in source_pack:
        if item["date"] > snapshot_date.isoformat():
            errors.append(f"FUTURE_SOURCE:{item['source_id']}")
    if not 8 <= len(stage1.themes) <= 12:
        errors.append(f"THEME_COUNT_INVALID:{len(stage1.themes)}")
    for theme in stage1.themes:
        if not set(theme.supporting_source_ids).issubset(source_ids):
            errors.append(f"THEME_UNKNOWN_SOURCE:{theme.theme_id}")
    for cell in applicable:
        cid = str(cell.get("canonical_cell_id"))
        score = cell.get("current")
        if not isinstance(score, int) or not -3 <= score <= 3:
            errors.append(f"SCORE_RANGE:{cid}")
        if not str(cell.get("reasoning") or "").strip():
            errors.append(f"MISSING_THESIS:{cid}")
        if str(cell.get("confidence") or "") not in {"HIGH", "MEDIUM", "LOW"}:
            errors.append(f"INVALID_CONFIDENCE:{cid}")
        cell_theme_ids = set(cell.get("supporting_theme_ids") or [])
        cell_source_ids = set(cell.get("supporting_source_ids") or [])
        if not cell_theme_ids or not cell_theme_ids.issubset(theme_ids):
            errors.append(f"INVALID_THEME_LINEAGE:{cid}")
        if not cell_source_ids or not cell_source_ids.issubset(source_ids):
            errors.append(f"INVALID_SOURCE_LINEAGE:{cid}")
    errors.extend(_lineage_errors(stage1, stage2))
    return sorted(set(errors))


def _lineage_errors(stage1: MarketSynthesisResponse, stage2: ThemeMatrixSynthesisResponse) -> list[str]:
    source_ids = {doc.source_id for doc in stage1.documents}
    theme_ids = {theme.theme_id for theme in stage1.themes}
    errors: list[str] = []
    for cell in stage2.cells:
        if not set(cell.supporting_theme_ids).issubset(theme_ids):
            errors.append(f"{cell.canonical_cell_id}:unknown_theme")
        if not set(cell.supporting_source_ids).issubset(source_ids):
            errors.append(f"{cell.canonical_cell_id}:unknown_source")
    scenario_types = {str(scenario.scenario_id) for scenario in stage2.scenarios}
    if scenario_types != {"BASE", "UPSIDE", "DOWNSIDE"}:
        errors.append("scenario_set_invalid")
    return errors


def _source_pack_item(article: Article, source: Source) -> dict[str, Any]:
    return {
        "source_id": article.id,
        "provider": source.institution_name,
        "raw_provider_name": source.institution_name,
        "canonical_provider_name": source.institution_name,
        "title": article.title,
        "date": article.publication_date.isoformat(),
        "url": article.canonical_url or article.url or article.source_reference,
        "document_type": _document_type(article.title, source.institution_name),
        "selection_reason": _selection_reason(article.title, source.institution_name),
        "coverage_regions": _coverage_regions(article.title, article.content_text),
        "coverage_asset_classes": _coverage_assets(article.title, article.content_text),
        "content_hash": article.content_hash,
        "content_chars": len(article.content_text or ""),
        "content_excerpt": _content_excerpt(article.content_text),
    }


def _llm_source_payload(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_id": item["source_id"],
        "provider": item["provider"],
        "raw_provider_name": item.get("raw_provider_name", item["provider"]),
        "canonical_provider_name": item.get("canonical_provider_name", item["provider"]),
        "title": item["title"],
        "date": item["date"],
        "url": item["url"],
        "document_type": item["document_type"],
        "selection_reason": item["selection_reason"],
        "coverage_regions": item["coverage_regions"],
        "coverage_asset_classes": item["coverage_asset_classes"],
        "content_excerpt": item["content_excerpt"],
    }


def _source_lineage_payload(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item.get(key, "")
        for key in [
            "source_id",
            "provider",
            "raw_provider_name",
            "canonical_provider_name",
            "title",
            "date",
            "url",
            "document_type",
            "source_tier",
        ]
    }


def _template_payload(cell: CanonicalCell) -> dict[str, Any]:
    return {
        "canonical_cell_id": cell.canonical_cell_id,
        "row_index": cell.row_index,
        "asset_class": cell.asset_class,
        "asset_group": cell.asset_group,
        "asset_segment": cell.asset_segment,
        "region": cell.region,
        "applicability": cell.applicability,
    }


def _matrix_source(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": item["provider"],
        "raw_provider_name": item.get("raw_provider_name", item["provider"]),
        "canonical_provider_name": item.get("canonical_provider_name", item["provider"]),
        "title": item["title"],
        "publication_date": item["date"],
        "url": item["url"],
        "content_hash": item.get("content_hash", ""),
        "source_id": item["source_id"],
    }


def calibrate_stage2_confidence(
    stage2: ThemeMatrixSynthesisResponse,
    source_pack: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    source_by_id = {item["source_id"]: item for item in source_pack}
    canonical_by_id = {cell.canonical_cell_id: cell for cell in canonical_cell_registry()}
    adjustments: list[dict[str, Any]] = []
    for cell in stage2.cells:
        original = Confidence(str(cell.confidence))
        capped = original
        reasons: list[str] = []
        sources = [source_by_id[source_id] for source_id in cell.supporting_source_ids if source_id in source_by_id]
        providers = {
            str(source.get("canonical_provider_name") or source.get("provider") or "").casefold()
            for source in sources
            if str(source.get("canonical_provider_name") or source.get("provider") or "").strip()
        }
        if original == Confidence.HIGH and len(providers) < 2:
            capped = Confidence.MEDIUM
            reasons.append("HIGH_REQUIRES_TWO_INDEPENDENT_PROVIDERS")
        if len(providers) <= 1 and capped == Confidence.HIGH:
            capped = Confidence.MEDIUM
            reasons.append("SINGLE_PROVIDER_DEPENDENCE")
        canonical = canonical_by_id.get(cell.canonical_cell_id)
        region = canonical.region if canonical else ""
        if canonical and region not in {"Global", "US", "Europe"}:
            source_regions = {
                region_name
                for source in sources
                for region_name in (source.get("coverage_regions") or [])
            }
            if region not in source_regions and "Global" not in source_regions and capped != Confidence.LOW:
                capped = Confidence.LOW
                reasons.append("THIN_REGION_SPECIFIC_EVIDENCE")
        if any("LIMITED_DETAIL" in (source.get("quality_flags") or []) for source in sources) and capped == Confidence.HIGH:
            capped = Confidence.MEDIUM
            reasons.append("BROAD_OR_LIMITED_SOURCE_DETAIL")
        if capped != original:
            cell.confidence = capped
            adjustments.append(
                {
                    "canonical_cell_id": cell.canonical_cell_id,
                    "from": original.value,
                    "to": capped.value,
                    "reasons": reasons,
                }
            )
    return adjustments


def _document_type(title: str, provider: str) -> str:
    text = f"{provider} {title}".casefold()
    if "central bank" in text or "ecb" in text or "bank of england" in text:
        return "official policy / financial stability publication"
    if "outlook" in text or "forecast" in text:
        return "market outlook"
    if "sentiment" in text:
        return "institutional sentiment survey"
    if "commodit" in text or "gold" in text:
        return "commodity outlook"
    return "institutional research"


def _selection_reason(title: str, provider: str) -> str:
    return f"Curated MVP source for broad MAE coverage: {provider} / {title}"


def _coverage_regions(title: str, text: str) -> list[str]:
    combined = f"{title}\n{text[:6000]}".casefold()
    regions = []
    checks = [
        ("Global", ("global", "world", "crosscurrents")),
        ("US", (" us ", "u.s.", "united states", "fed ", "fomc")),
        ("Europe", ("europe", "euro area", "ecb")),
        ("UK", (" uk ", "united kingdom", "bank of england")),
        ("Japan", ("japan", "japanese", "boj")),
        ("EM ex China", ("emerging market", "em ", "china", "em ex")),
    ]
    padded = f" {combined} "
    for region, tokens in checks:
        if any(token in padded for token in tokens):
            regions.append(region)
    return regions or ["Global"]


def _coverage_assets(title: str, text: str) -> list[str]:
    combined = f"{title}\n{text[:6000]}".casefold()
    assets = []
    checks = [
        ("Equities", ("equity", "equities", "stocks", "earnings")),
        ("Government Bonds", ("government bond", "sovereign", "duration", "rates", "yield")),
        ("Credit", ("credit", "spreads", "corporate bond", "financial stability")),
        ("Commodities", ("commodity", "commodities", "gold", "energy", "oil")),
    ]
    for asset, tokens in checks:
        if any(token in combined for token in tokens):
            assets.append(asset)
    return assets or ["Multi-asset"]


def _content_excerpt(text: str, *, max_chars: int = 900) -> str:
    cleaned = normalize_space(text)
    if len(cleaned) <= max_chars:
        return cleaned
    head = cleaned[: max_chars // 2]
    tail = cleaned[-max_chars // 3 :]
    return f"{head} ... {tail}"


def _is_excluded(title: str) -> bool:
    lowered = title.casefold()
    return any(pattern in lowered for pattern in EXCLUDED_TITLE_PATTERNS)


def _repair_stage1_source_lineage(stage1: MarketSynthesisResponse, source_pack: list[dict[str, Any]]) -> None:
    valid_ids = {item["source_id"] for item in source_pack}
    for theme in stage1.themes:
        theme.supporting_source_ids = _repair_source_id_list(theme.supporting_source_ids, valid_ids)
        theme.contradicting_source_ids = _repair_source_id_list(theme.contradicting_source_ids, valid_ids)


def _repair_stage2_source_lineage(stage2: ThemeMatrixSynthesisResponse, source_pack: list[dict[str, Any]]) -> None:
    valid_ids = {item["source_id"] for item in source_pack}
    for cell in stage2.cells:
        cell.supporting_source_ids = _repair_source_id_list(cell.supporting_source_ids, valid_ids)


def _repair_source_id_list(source_ids: list[str], valid_ids: set[str]) -> list[str]:
    repaired: list[str] = []
    for source_id in source_ids:
        candidate = _repair_source_id(source_id, valid_ids)
        if candidate not in repaired:
            repaired.append(candidate)
    return repaired


def _repair_source_id(source_id: str, valid_ids: set[str]) -> str:
    if source_id in valid_ids:
        return source_id
    parts = source_id.split("-")
    if len(parts) >= 2:
        prefix = "-".join(parts[:2]) + "-"
        matches = [valid_id for valid_id in valid_ids if valid_id.startswith(prefix)]
        if len(matches) == 1:
            return matches[0]
    return source_id
