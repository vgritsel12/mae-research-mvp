from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import OUTPUT_DIR, ROOT_DIR, Settings, get_settings
from app.domain.enums import (
    AutonomousReleaseStatus,
    AutonomousTechnicalStatus,
    Confidence,
    ModelValidationStatus,
)
from app.domain.models import Article, AtomicReleasePointer, ResearchView
from app.domain.models import LLMCallLog
from app.llm.provider import get_provider
from app.services.autonomous_matrix import (
    AutonomousSignal,
    AutonomousSourceRef,
    build_autonomous_matrix,
)
from app.services.collectors import run_update
from app.services.normalization import canonical_cell_id_for
from app.services.autonomous_release import AUTONOMOUS_CHANNEL
from app.services.historical_analogs import build_release_historical_analog_artifact
from app.services.source_selection import SourceSelectionError, select_autonomous_source_pack
from app.services.theme_first_pipeline import (
    PIPELINE_VERSION,
    load_previous_theme_first_or_official_scores,
    run_theme_first_synthesis,
)


AUTONOMOUS_DISCLOSURE = (
    "AI-generated market assessment. Not individually reviewed by a human analyst "
    "and not personal investment advice."
)
REQUIRED_SCENARIOS = {"BASE", "UPSIDE", "DOWNSIDE"}
STAGE_NAMES = (
    "stage_01_collection.json",
    "stage_02_research_views.json",
    "stage_03_signals.json",
    "stage_04_matrix.json",
    "stage_05_scenarios.json",
    "stage_06_explanations_analogs.json",
    "stage_07_validation.json",
)
SOURCE_SELECTION_MANIFEST_NAME = "source_selection_manifest.json"


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


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run_autonomous_candidate(
    session: Session,
    *,
    snapshot_date: date,
    settings: Settings | None = None,
    output_root: Path | None = None,
    collect: bool = True,
    collector: Callable[..., Any] = run_update,
    previous_scores_path: Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Create a fully persisted candidate; publication is a separate transaction."""

    settings = settings or get_settings()
    output_root = output_root or OUTPUT_DIR / "autonomous" / "candidates"
    previous_date, previous_scores = load_previous_theme_first_or_official_scores(
        session,
        previous_scores_path or Path("outputs/mae_full_latest_scores.csv"),
    )
    prior_llm_log_ids = set(session.scalars(select(LLMCallLog.id)).all())
    provider_configured = bool(
        settings.llm_provider == "openai" and settings.openai_api_key and settings.openai_model
    )

    technical_status = AutonomousTechnicalStatus.PASSED
    critical_errors: list[str] = []
    model_failures: list[dict[str, Any]] = []
    theme_result: dict[str, Any] | None = None
    source_selection_manifest: dict[str, Any] = {
        "schema_version": "SOURCE_SELECTION_MANIFEST_V1",
        "selection_algorithm_version": "NOT_RUN",
        "snapshot_date": snapshot_date.isoformat(),
        "candidate_document_count": 0,
        "eligible_count": 0,
        "rejected_count": 0,
        "not_selected_count": 0,
        "selected_count": 0,
        "independent_provider_count": 0,
        "candidate_documents": [],
        "selected_documents": [],
        "eligible_not_selected_documents": [],
        "rejected_documents": [],
        "rejection_reason_codes": {},
        "provider_concentration": {},
        "region_coverage": {},
        "asset_class_coverage": {},
        "source_tier_distribution": {},
        "source_pack_hash": canonical_hash([]),
        "blockers": [],
    }
    collection_reason = "THEME_FIRST_AUTONOMOUS_SOURCE_SELECTION"
    collector_status = "NOT_RUN"
    collection_summary: dict[str, Any] = {}
    if provider_configured:
        try:
            if collect:
                _progress(progress, "Получаю свежие публикации")
                try:
                    collection_job = collector(session, settings=settings, process_articles=False)
                except TypeError:
                    collection_job = collector(session, settings=settings)
                collector_status = str(getattr(collection_job, "status", "SUCCESS") or "SUCCESS")
                collection_summary = _collection_summary(collection_job)
            else:
                collector_status = "SKIPPED"
            _progress(progress, "Проверяю доступные материалы")
            _progress(progress, "Отбираю рыночные материалы")
            previous_source_hashes = _previous_committed_source_hashes(session)
            source_pack, source_selection_manifest = select_autonomous_source_pack(
                session,
                snapshot_date=snapshot_date,
                lookback_days=settings.lookback_days,
                previous_source_hashes=previous_source_hashes,
                require_new_source=bool(previous_source_hashes),
            )
            source_pack_hash = str(source_selection_manifest.get("source_pack_hash") or canonical_hash(source_pack))[:16]
            run_session_key = f"theme-first-{snapshot_date.isoformat()}-{source_pack_hash}"
            provider = get_provider(settings, session_key=run_session_key, run_id=run_session_key)
            if hasattr(provider, "max_retries"):
                provider.max_retries = 0
            if hasattr(provider, "attach_session"):
                provider.attach_session(session)
            _progress(progress, "Формирую основные темы")
            theme_result = run_theme_first_synthesis(
                session,
                provider=provider,
                snapshot_date=snapshot_date,
                previous_scores_path=previous_scores_path or Path("outputs/mae_full_latest_scores.csv"),
                source_pack=source_pack,
                source_selection_manifest=source_selection_manifest,
            )
            _progress(progress, "Пересчитываю матрицу")
            collector_status = "SUCCESS"
        except SourceSelectionError as exc:
            technical_status = AutonomousTechnicalStatus.FAILED
            collector_status = "BLOCKED"
            source_selection_manifest = exc.manifest
            blockers = tuple(source_selection_manifest.get("blockers") or [])
            critical_errors.extend(f"SOURCE_SELECTION_BLOCKED:{blocker}" for blocker in blockers)
        except Exception as exc:  # noqa: BLE001 - candidate must persist failure evidence
            technical_status = AutonomousTechnicalStatus.FAILED
            collector_status = "FAILED"
            critical_errors.append(f"THEME_FIRST_PIPELINE_FAILED:{type(exc).__name__}: {exc}")
            model_failures.append({"theme_first_error": f"{type(exc).__name__}: {exc}"})
    else:
        collection_reason = "OPENAI_NOT_CONFIGURED"
        collector_status = "BLOCKED"
        critical_errors.append("OPENAI_NOT_CONFIGURED")

    llm_logs = _new_llm_logs(session, prior_llm_log_ids)
    llm_call_count = sum(1 for row in llm_logs if row.status != "BLOCKED")
    llm_success_count = sum(1 for row in llm_logs if row.status == "SUCCESS")
    llm_error_count = sum(1 for row in llm_logs if row.status == "ERROR")

    source_pack = list((theme_result or {}).get("source_pack") or [])
    if not source_pack and source_selection_manifest.get("selected_documents"):
        source_pack = [
            {
                "source_id": item.get("source_id"),
                "provider": item.get("canonical_provider_name") or item.get("provider") or "",
                "raw_provider_name": item.get("raw_provider_name") or "",
                "canonical_provider_name": item.get("canonical_provider_name") or item.get("provider") or "",
                "title": item.get("title") or "",
                "date": item.get("date") or "",
                "url": item.get("url") or "",
                "document_type": item.get("document_type") or "",
                "selection_reason": item.get("selection_reason") or "",
                "coverage_regions": item.get("coverage_regions") or [],
                "coverage_asset_classes": item.get("coverage_asset_classes") or [],
                "source_tier": item.get("source_tier") or "",
                "content_hash": item.get("content_hash") or "",
            }
            for item in source_selection_manifest.get("selected_documents") or []
        ]
    source_manifest = sorted(
        (
            source["url"],
            source["date"],
            source.get("content_hash", ""),
            source["source_id"],
        )
        for source in source_pack
    )
    source_manifest_hash = canonical_hash(source_manifest)
    market_series_path = Path("data/historical_market_series.csv")
    registered_input_manifest_hash = file_sha256(market_series_path) if market_series_path.is_file() else ""
    run_fingerprint = canonical_hash(
        {
            "pipeline_contract_version": PIPELINE_VERSION,
            "snapshot_date": snapshot_date.isoformat(),
            "previous_snapshot_date": previous_date.isoformat(),
            "previous_scores": previous_scores,
            "registered_input_manifest_hash": registered_input_manifest_hash,
            "sources": source_manifest,
            "themes": (theme_result or {}).get("stage1", {}).get("themes", []),
            "matrix": (theme_result or {}).get("matrix", {}),
        }
    )
    candidate_id = f"{snapshot_date.isoformat()}-{run_fingerprint[:16]}"
    candidate_dir = output_root / candidate_id
    candidate_dir.mkdir(parents=True, exist_ok=True)

    collection_stage = {
        "status": collector_status,
        "reason": collection_reason,
        "provider": settings.llm_provider,
        "model": settings.openai_model or "NOT_CONFIGURED",
        "responses_api_controls_reused": True,
        "max_llm_calls_per_day": settings.max_llm_calls_per_day,
        "max_llm_calls_per_session": settings.max_llm_calls_per_session,
        "new_article_count": 0,
        "new_research_view_count": 0,
        "candidate_research_view_count": 0,
        "legacy_research_views_used": 0,
        "new_source_count": len(source_manifest),
        "candidate_source_count": len(source_manifest),
        "admitted_source_count": len(source_pack),
        "admitted_sources": [{k: v for k, v in source.items() if k != "content_excerpt"} for source in source_pack],
        "llm_call_count": llm_call_count,
        "llm_success_count": llm_success_count,
        "llm_error_count": llm_error_count,
        "details": [
            {
                "source_id": source["source_id"],
                "provider": source["provider"],
                "title": source["title"],
                "date": source["date"],
                "url": source["url"],
                "status": "admitted",
            }
            for source in source_pack
        ],
        "source_manifest_hash": source_manifest_hash,
        "registered_input_manifest_hash": registered_input_manifest_hash,
        "source_selection_manifest": SOURCE_SELECTION_MANIFEST_NAME,
        "collection_summary": collection_summary,
        "source_selection_summary": {
            "candidate_document_count": source_selection_manifest.get("candidate_document_count", 0),
            "eligible_count": source_selection_manifest.get("eligible_count", 0),
            "rejected_count": source_selection_manifest.get("rejected_count", 0),
            "selected_count": source_selection_manifest.get("selected_count", 0),
            "independent_provider_count": source_selection_manifest.get("independent_provider_count", 0),
            "blockers": source_selection_manifest.get("blockers") or [],
        },
    }
    _write_json(candidate_dir / STAGE_NAMES[0], collection_stage)
    _write_json(candidate_dir / SOURCE_SELECTION_MANIFEST_NAME, source_selection_manifest)
    stage1_payload = (theme_result or {}).get(
        "stage1",
        {"schema_version": "THEME_FIRST_STAGE_1", "themes": [], "documents": [], "admitted_sources": []},
    )
    lineage_payload = (theme_result or {}).get(
        "stage2_lineage",
        {"schema_version": "THEME_FIRST_LINEAGE_V1", "lineage_errors": []},
    )
    matrix_payload = (theme_result or {}).get("matrix", {"error": "theme-first matrix unavailable", "cells": []})
    scenario_payload = (theme_result or {}).get(
        "scenarios",
        {"schema_version": "THEME_FIRST_SCENARIOS_V1", "scenario_types": [], "scenarios": []},
    )
    validation_errors = list((theme_result or {}).get("validation_errors") or [])
    confidence_adjustments = list((theme_result or {}).get("confidence_adjustments") or [])
    if validation_errors:
        technical_status = AutonomousTechnicalStatus.FAILED
        critical_errors.extend(validation_errors)
    _write_json(candidate_dir / STAGE_NAMES[1], stage1_payload)
    _write_json(candidate_dir / STAGE_NAMES[2], lineage_payload)
    _write_json(candidate_dir / STAGE_NAMES[3], matrix_payload)

    scenarios = list(scenario_payload.get("scenarios") or [])
    scenario_types = {str(row.get("scenario_id", "")).upper() for row in scenarios}
    if scenario_types != REQUIRED_SCENARIOS:
        technical_status = AutonomousTechnicalStatus.FAILED
        critical_errors.append(f"SCENARIO_SET_INVALID: {sorted(scenario_types)}")
    _write_json(candidate_dir / STAGE_NAMES[4], scenario_payload)

    explanations = _build_explanations(matrix_payload)
    analog_reference = _historical_analog_reference(snapshot_date)
    _write_json(
        candidate_dir / STAGE_NAMES[5],
        {"explanations": explanations, "historical_analogs": analog_reference},
    )

    mode_counts = matrix_payload.get("mode_counts", {}) if isinstance(matrix_payload, dict) else {}
    independent_provider_count = len(
        {
            str(source.get("canonical_provider_name") or source.get("provider") or "").strip().casefold()
            for source in source_pack
            if str(source.get("canonical_provider_name") or source.get("provider") or "").strip()
        }
    )
    _append_initial_baseline_blockers(
        critical_errors,
        collect=collect,
        provider_configured=provider_configured,
        collector_status=collector_status,
        source_manifest=source_manifest,
        llm_success_count=llm_success_count,
        independent_provider_count=independent_provider_count,
        matrix_payload=matrix_payload,
        scenario_types=scenario_types,
    )
    if critical_errors:
        technical_status = AutonomousTechnicalStatus.FAILED

    if not provider_configured:
        model_status = ModelValidationStatus.FAILED
    elif model_failures and not matrix_payload.get("cells"):
        model_status = ModelValidationStatus.FAILED
    elif collect and provider_configured and llm_success_count == 0:
        model_status = ModelValidationStatus.FAILED
    elif model_failures or validation_errors:
        model_status = ModelValidationStatus.PARTIAL
    else:
        model_status = ModelValidationStatus.PASSED
    candidate_release_status = (
        AutonomousReleaseStatus.DRAFT
        if technical_status == AutonomousTechnicalStatus.PASSED
        and model_status != ModelValidationStatus.FAILED
        else AutonomousReleaseStatus.BLOCKED
    )
    release_status = (
        AutonomousReleaseStatus.AUTO_PUBLISHED
        if candidate_release_status == AutonomousReleaseStatus.DRAFT
        else AutonomousReleaseStatus.BLOCKED
    )
    candidate_content_hash = canonical_hash(
        _stable_content_hash_payload(
            {
                "matrix": matrix_payload,
                "scenarios": scenarios,
                "explanations": explanations,
                "historical_analogs": analog_reference,
            }
        )
    )
    validation = {
        "candidate_id": candidate_id,
        "snapshot_date": snapshot_date.isoformat(),
        "previous_snapshot_date": previous_date.isoformat(),
        "technical_status": technical_status.value,
        "model_validation_status": model_status.value,
        "release_status": release_status.value,
        "candidate_release_status": candidate_release_status.value,
        "human_review_status": "NOT_REQUESTED",
        "applicable_score_count": int(matrix_payload.get("applicable_score_count") or 0),
        "not_applicable_count": int(matrix_payload.get("not_applicable_count") or 0),
        "source_manifest_hash": source_manifest_hash,
        "candidate_content_hash": candidate_content_hash,
        "critical_error_count": len(critical_errors),
        "critical_errors": critical_errors,
        "model_failures": model_failures,
        "release_kind": "INITIAL_AUTONOMOUS_BASELINE",
        "pipeline_version": PIPELINE_VERSION,
        "legacy_research_views_used": 0,
        "theme_count": len(stage1_payload.get("themes") or []),
        "admitted_source_count": len(source_pack),
        "source_selection": {
            "selected_count": int(source_selection_manifest.get("selected_count") or 0),
            "candidate_document_count": int(source_selection_manifest.get("candidate_document_count") or 0),
            "eligible_count": int(source_selection_manifest.get("eligible_count") or 0),
            "rejected_count": int(source_selection_manifest.get("rejected_count") or 0),
            "source_pack_hash": source_selection_manifest.get("source_pack_hash", ""),
            "blockers": source_selection_manifest.get("blockers") or [],
        },
        "confidence_adjustment_count": len(confidence_adjustments),
        "confidence_adjustments": confidence_adjustments,
        "mode_counts": mode_counts,
        "llm_call_count": llm_call_count,
        "llm_success_count": llm_success_count,
        "llm_error_count": llm_error_count,
        "independent_provider_count": independent_provider_count,
        "checks": {
            "matrix_geometry": bool(
                matrix_payload.get("applicable_score_count") == 104 and matrix_payload.get("not_applicable_count") == 10
            ),
            "scenario_set": scenario_types == REQUIRED_SCENARIOS,
            "no_carry_forward": mode_counts.get("CARRY_FORWARD", 0) == 0,
            "collection_non_empty": len(source_manifest) > 0,
            "successful_llm_call_present": llm_success_count > 0,
            "independent_sources": independent_provider_count >= 6,
            "source_selection_coverage": not (source_selection_manifest.get("blockers") or []),
            "theme_lineage": not validation_errors,
            "legacy_research_views_used": False,
            "future_evidence_rejected": True,
            "human_record_required": False,
            "intermediate_stages_persisted": True,
        },
        "disclosure": AUTONOMOUS_DISCLOSURE,
    }
    _write_json(candidate_dir / STAGE_NAMES[6], validation)
    _progress(progress, "Проверяю результат")

    artifact_names = (*STAGE_NAMES, SOURCE_SELECTION_MANIFEST_NAME)
    artifacts = [
        {"name": name, "sha256": file_sha256(candidate_dir / name), "bytes": (candidate_dir / name).stat().st_size}
        for name in artifact_names
    ]
    candidate_manifest = {
        "candidate_id": candidate_id,
        "run_fingerprint": run_fingerprint,
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
    }
    candidate_manifest["artifact_manifest_hash"] = canonical_hash(artifacts)
    _write_json(candidate_dir / "candidate_manifest.json", candidate_manifest)
    return {
        **validation,
        "candidate_dir": str(candidate_dir),
        "artifact_manifest_hash": candidate_manifest["artifact_manifest_hash"],
        "stage_manifest": candidate_manifest,
    }


def _new_llm_logs(session: Session, prior_ids: set[str]) -> list[LLMCallLog]:
    query = select(LLMCallLog).order_by(LLMCallLog.created_at, LLMCallLog.id)
    if prior_ids:
        query = query.where(LLMCallLog.id.not_in(prior_ids))
    return list(session.scalars(query).all())


def _collection_summary(collection_job: Any) -> dict[str, Any]:
    details = getattr(collection_job, "details", None) or []
    for item in details:
        if isinstance(item, dict) and item.get("source") == "__collection_summary__":
            return {k: v for k, v in item.items() if k != "source"}
    return {
        "status": str(getattr(collection_job, "status", "") or ""),
        "sources_attempted": int(getattr(collection_job, "total_sources", 0) or 0),
        "sources_succeeded": int(getattr(collection_job, "succeeded", 0) or 0),
        "sources_failed": int(getattr(collection_job, "failed", 0) or 0),
    }


def _progress(callback: Callable[[str], None] | None, message: str) -> None:
    if callback is not None:
        callback(message)


def _previous_committed_source_hashes(session: Session) -> set[str]:
    pointer = session.get(AtomicReleasePointer, AUTONOMOUS_CHANNEL)
    if pointer is None:
        return set()
    release_dir = Path(pointer.release_directory)
    if not release_dir.is_absolute():
        release_dir = ROOT_DIR / release_dir
    candidates = [
        release_dir / SOURCE_SELECTION_MANIFEST_NAME,
        release_dir / "stage_02_research_views.json",
    ]
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows = payload.get("selected_documents") or payload.get("admitted_sources") or []
        hashes = {str(row.get("content_hash") or "") for row in rows if row.get("content_hash")}
        if hashes:
            return hashes
    return set()


def _append_initial_baseline_blockers(
    critical_errors: list[str],
    *,
    collect: bool,
    provider_configured: bool,
    collector_status: str,
    source_manifest: list[tuple[str, str, str, str]],
    llm_success_count: int,
    independent_provider_count: int,
    matrix_payload: dict[str, Any],
    scenario_types: set[str],
) -> None:
    if not provider_configured:
        critical_errors.append("OPENAI_NOT_CONFIGURED")
    if provider_configured and llm_success_count <= 0:
        critical_errors.append("NO_SUCCESSFUL_LLM_CALLS")
    if not source_manifest:
        critical_errors.append("COLLECTION_EMPTY")
    if collect and provider_configured and collector_status in {"FAILED", "ERROR"}:
        critical_errors.append(f"COLLECTION_FAILED:{collector_status}")
    if independent_provider_count < 6:
        critical_errors.append("INSUFFICIENT_INDEPENDENT_SOURCES")
    cells = matrix_payload.get("cells", []) if isinstance(matrix_payload, dict) else []
    mode_counts = matrix_payload.get("mode_counts", {}) if isinstance(matrix_payload, dict) else {}
    if mode_counts.get("CARRY_FORWARD", 0):
        critical_errors.append("CARRY_FORWARD_PROHIBITED_FOR_INITIAL_BASELINE")
    if len(cells) != 114:
        critical_errors.append("MATRIX_GEOMETRY_INVALID")
    if matrix_payload.get("applicable_score_count") != 104 or matrix_payload.get("not_applicable_count") != 10:
        critical_errors.append("MATRIX_RECONCILIATION_INVALID")
    legacy_only = [
        cell.get("canonical_cell_id")
        for cell in cells
        if cell.get("applicability") == "APPLICABLE"
        and (
            cell.get("mode") in {"", None}
            or "previous committed matrix is retained" in str(cell.get("reasoning", ""))
        )
    ]
    if legacy_only:
        critical_errors.append("LEGACY_ONLY_CELL:" + ",".join(str(item) for item in legacy_only[:5]))
    if scenario_types != REQUIRED_SCENARIOS:
        critical_errors.append("SCENARIO_SET_INVALID")


def _signals_from_views(
    session: Session,
    views: list[ResearchView],
    *,
    snapshot_date: date,
) -> tuple[list[dict[str, Any]], list[AutonomousSignal], list[str]]:
    normalized: list[dict[str, Any]] = []
    errors: list[str] = []
    grouped: dict[str, list[tuple[ResearchView, Article, AutonomousSourceRef]]] = defaultdict(list)
    for view in views:
        if view.is_demo:
            errors.append(f"{view.id}: DEMO view excluded from production candidate")
            continue
        article = session.get(Article, view.article_id)
        if article is None or article.is_demo:
            errors.append(f"{view.id}: real article provenance missing")
            continue
        cell_id = view.canonical_cell_id or canonical_cell_id_for(view.template_row_key, view.region)
        url = article.canonical_url or article.url or ""
        if not cell_id or not url.startswith(("https://", "http://")):
            errors.append(f"{view.id}: canonical mapping or public URL missing")
            continue
        if article.publication_date > snapshot_date:
            errors.append(f"{view.id}: future publication date {article.publication_date}")
            continue
        if isinstance(view.position_score, bool) or not isinstance(view.position_score, int):
            errors.append(f"{view.id}: integer position score missing")
            continue
        source = AutonomousSourceRef(
            provider=view.institution,
            title=article.title,
            publication_date=article.publication_date,
            url=url,
            content_hash=article.content_hash,
            source_id=article.id,
        )
        item = {
            "view_id": view.id,
            "article_id": article.id,
            "canonical_cell_id": cell_id,
            "institution": view.institution,
            "position_score": view.position_score,
            "confidence": view.confidence,
            "drivers": list(view.drivers or []),
            "risks": list(view.risks or []),
            "extraction_method": view.extraction_method,
            "source": source.model_dump(mode="json"),
        }
        normalized.append(item)
        grouped[cell_id].append((view, article, source))

    signals: list[AutonomousSignal] = []
    for cell_id in sorted(grouped):
        entries = grouped[cell_id]
        unique_sources = {entry[2].url: entry[2] for entry in entries}
        scores = [entry[0].position_score for entry in entries]
        proposed_score = max(-3, min(3, _round_score(sum(scores) / len(scores))))
        mode = "COMPOSITE" if len(unique_sources) >= 2 else "DIRECT"
        confidences = {entry[0].confidence for entry in entries}
        confidence = "HIGH" if confidences == {"HIGH"} else "LOW" if "LOW" in confidences else "MEDIUM"
        institutions = ", ".join(sorted({entry[0].institution for entry in entries}))
        drivers = sorted({driver for entry in entries for driver in (entry[0].drivers or [])})
        risks = sorted({risk for entry in entries for risk in (entry[0].risks or [])})
        signals.append(
            AutonomousSignal(
                canonical_cell_id=cell_id,
                mode=mode,
                proposed_score=proposed_score,
                confidence=confidence,
                sources=list(unique_sources.values()),
                reasoning=(
                    f"Validated Responses API research view from {institutions}. "
                    f"Drivers: {', '.join(drivers) or 'none stated'}."
                ),
                invalidation=(
                    f"Invalidate if these stated risks materialize: {', '.join(risks) or 'source thesis reverses'}."
                ),
                explicit_balanced_conclusion=proposed_score == 0,
            )
        )
    return normalized, signals, errors


def _round_score(value: float) -> int:
    return int(value + 0.5) if value >= 0 else int(value - 0.5)


def _build_autonomous_scenarios(matrix_payload: dict[str, Any], *, snapshot_date: date) -> list[dict[str, Any]]:
    cells = [
        cell
        for cell in matrix_payload.get("cells", [])
        if cell.get("applicability") == "APPLICABLE" and isinstance(cell.get("current"), int)
    ]
    positive = _rank_material_cells(cells, positive=True)
    negative = _rank_material_cells(cells, positive=False)
    changed = [
        cell
        for cell in sorted(cells, key=lambda item: abs(int(item.get("delta") or 0)), reverse=True)
        if int(cell.get("delta") or 0) != 0
    ][:12]
    base_cells = changed or positive[:6] + negative[:6]
    upside_cells = positive[:12]
    downside_cells = negative[:12]
    aggregate_score = sum(int(cell.get("current") or 0) for cell in cells)
    stance = "constructive" if aggregate_score > 8 else "defensive" if aggregate_score < -8 else "balanced"
    return [
        {
            "scenario_id": "BASE",
            "scenario_name": "Current autonomous baseline",
            "probability_band": "45-55%",
            "assessment_date": snapshot_date.isoformat(),
            "provenance_mode": "AUTONOMOUS_INITIAL_BASELINE",
            "narrative": (
                f"Automatically formed {stance} 6-12 month baseline from current MAE cell evidence. "
                "Direct and composite publication signals are used where available; remaining cells use "
                "registered official or market inputs with explicit model-inferred provenance."
            ),
            "causal_chain": (
                "publication signals and registered inputs -> region/asset transmission -> current MAE "
                "score distribution -> portfolio reaction map"
            ),
            "trigger": "Incoming publications and registered inputs remain broadly consistent with current cell scores.",
            "veto": "Invalidate if new dated publications reverse the main drivers or market inputs move against the transmission chain.",
            "material_affected_cells": _cell_list(base_cells),
        },
        {
            "scenario_id": "UPSIDE",
            "scenario_name": "Easing and risk appetite extension",
            "probability_band": "20-30%",
            "assessment_date": snapshot_date.isoformat(),
            "provenance_mode": "AUTONOMOUS_INITIAL_BASELINE",
            "narrative": (
                "Upside scenario emphasizes cells with the strongest positive current scores or positive revisions. "
                "It does not claim direct institutional endorsement unless the underlying cell mode is DIRECT or COMPOSITE."
            ),
            "causal_chain": (
                "softer inflation or growth resilience -> lower discount-rate pressure and broader risk appetite -> "
                "positive-score cells outperform"
            ),
            "trigger": "Disinflation, easier policy expectations, or earnings revisions improve together.",
            "veto": "Invalidate if policy rates or risk premia rise enough to weaken the positive cell group.",
            "material_affected_cells": _cell_list(upside_cells),
        },
        {
            "scenario_id": "DOWNSIDE",
            "scenario_name": "Sticky inflation or risk-off reversal",
            "probability_band": "20-30%",
            "assessment_date": snapshot_date.isoformat(),
            "provenance_mode": "AUTONOMOUS_INITIAL_BASELINE",
            "narrative": (
                "Downside scenario emphasizes cells with negative current scores or negative revisions. "
                "The scenario is generated automatically from available inputs and should be read as a risk map."
            ),
            "causal_chain": (
                "sticky inflation, tighter liquidity, or spread widening -> weaker multiples and funding conditions -> "
                "negative-score cells underperform"
            ),
            "trigger": "Inflation surprises, policy repricing, credit-spread widening, or earnings downgrades intensify.",
            "veto": "Invalidate if newer publications and market inputs confirm easing pressure or improving breadth.",
            "material_affected_cells": _cell_list(downside_cells),
        },
    ]


def _rank_material_cells(cells: list[dict[str, Any]], *, positive: bool) -> list[dict[str, Any]]:
    def key(cell: dict[str, Any]) -> tuple[int, int, str]:
        current = int(cell.get("current") or 0)
        delta = int(cell.get("delta") or 0)
        direction_score = current if positive else -current
        direction_delta = delta if positive else -delta
        return (direction_score, direction_delta, str(cell.get("canonical_cell_id", "")))

    return [
        cell
        for cell in sorted(cells, key=key, reverse=True)
        if (int(cell.get("current") or 0) > 0 if positive else int(cell.get("current") or 0) < 0)
    ][:12]


def _cell_list(cells: list[dict[str, Any]]) -> str:
    if not cells:
        return "No material cell group identified"
    return "; ".join(
        f"{cell.get('canonical_cell_id')} ({cell.get('region')} / {cell.get('asset_segment')}: {cell.get('current')})"
        for cell in cells[:12]
    )


def _historical_analog_reference(snapshot_date: date) -> dict[str, Any]:
    series_path = Path("data/historical_market_series.csv")
    if series_path.is_file():
        payload = build_release_historical_analog_artifact(snapshot_date, root=ROOT_DIR)
        artifact_hash = canonical_hash(_stable_content_hash_payload(payload))
        return {
            "source_artifact": str(series_path),
            "source_sha256": file_sha256(series_path),
            "source_snapshot_date": payload.get("snapshot_date"),
            "release_snapshot_date": payload.get("release_snapshot_date"),
            "regime_observation_cutoff": payload.get("regime_observation_cutoff"),
            "data_last_available_date": payload.get("data_last_available_date"),
            "generated_at": payload.get("generated_at"),
            "status": payload.get("status"),
            "mode": payload.get("mode"),
            "no_look_ahead": payload.get("no_look_ahead"),
            "limitations": payload.get("limitations", []),
            "analog_count": len(payload.get("analogs", [])),
            "recent_regime_match_count": len(payload.get("recent_regime_matches", [])),
            "historical_analog_periods": [str(item.get("period")) for item in payload.get("historical_analogs", [])],
            "recent_regime_match_periods": [str(item.get("period")) for item in payload.get("recent_regime_matches", [])],
            "artifact_sha256": artifact_hash,
            "provenance_mode": "DESCRIPTIVE_RECALCULATED_FOR_RELEASE",
            "artifact_location": "top_level_historical_analogs_export",
        }
    path = Path("outputs/mae_historical_analogs_latest.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        "source_artifact": str(path),
        "source_sha256": file_sha256(path),
        "source_snapshot_date": payload.get("snapshot_date"),
        "release_snapshot_date": snapshot_date.isoformat(),
        "status": payload.get("status"),
        "mode": payload.get("mode"),
        "no_look_ahead": payload.get("no_look_ahead"),
        "limitations": payload.get("limitations", []),
        "analog_count": len(payload.get("analogs", [])),
        "provenance_mode": "DESCRIPTIVE_REFERENCE",
        "artifact": payload,
    }


def _stable_content_hash_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _stable_content_hash_payload(item)
            for key, item in value.items()
            if key != "generated_at"
        }
    if isinstance(value, list):
        return [_stable_content_hash_payload(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_stable_content_hash_payload(item) for item in value)
    return value


def _build_explanations(matrix_payload: dict[str, Any]) -> dict[str, Any]:
    cells = matrix_payload.get("cells", [])
    changed = [
        {
            "canonical_cell_id": cell["canonical_cell_id"],
            "previous": cell["previous"],
            "current": cell["current"],
            "delta": cell["delta"],
            "mode": cell["mode"],
            "reasoning": cell["reasoning"],
        }
        for cell in cells
        if cell.get("delta") not in (None, 0)
    ]
    return {
        "changed_cell_count": 0,
        "changed_cells": [],
        "market_changed_cell_count": 0,
        "market_changed_cells": [],
        "technical_revision_count": len(changed),
        "technical_revision_cells": changed,
        "carry_forward_cell_count": sum(cell.get("mode") == "CARRY_FORWARD" for cell in cells),
        "model_inferred_cell_count": sum(cell.get("mode") == "MODEL_INFERRED" for cell in cells),
        "summary": (
            "Текущая матрица является исходной точкой для будущих месячных сравнений."
        ),
    }


def _write_json(path: Path, value: Any) -> None:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(encoded, encoding="utf-8")
    os.replace(temporary, path)
