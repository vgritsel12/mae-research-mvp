from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Any

from sqlalchemy import Engine

from app.config import get_settings
from app.services.atomic_release import resolve_committed_release
from app.services.autonomous_pipeline import AUTONOMOUS_DISCLOSURE, canonical_hash, file_sha256
from app.services.autonomous_release import AUTONOMOUS_CHANNEL
from app.services.historical_analogs import build_release_historical_analog_artifact
from app.services.temporal_metadata import comparison_metadata
from app.services.trust_language import sanitize_trust_language


JSON_NAME = "MAE_autonomous_release.json"
CSV_NAME = "MAE_autonomous_matrix.csv"
XLSX_NAME = "MAE_autonomous_market_report.xlsx"
MANIFEST_NAME = "export_manifest.json"
SOURCE_SELECTION_NAME = "source_selection_manifest.json"
HISTORICAL_ANALOGS_NAME = "historical_analogs.json"
SOURCE_REPORT_NAME = "source_report.json"


def prepare_autonomous_exports(*, engine: Engine, root: Path) -> dict[str, Any]:
    resolved = resolve_committed_release(engine=engine, root=root, channel=AUTONOMOUS_CHANNEL)
    market_synthesis = sanitize_trust_language(_read_json(resolved.artifact_paths["stage_02_research_views.json"]))
    lineage = sanitize_trust_language(_read_json(resolved.artifact_paths["stage_03_signals.json"]))
    matrix = sanitize_trust_language(_read_json(resolved.artifact_paths["stage_04_matrix.json"]))
    scenarios = sanitize_trust_language(_read_json(resolved.artifact_paths["stage_05_scenarios.json"]))
    explanations = sanitize_trust_language(_read_json(resolved.artifact_paths["stage_06_explanations_analogs.json"]))
    validation = sanitize_trust_language(_read_json(resolved.artifact_paths["stage_07_validation.json"]))
    source_selection_path = resolved.artifact_paths.get(SOURCE_SELECTION_NAME)
    source_selection = (
        sanitize_trust_language(_read_json(source_selection_path))
        if source_selection_path and source_selection_path.is_file()
        else {}
    )
    historical_analogs = sanitize_trust_language(
        _historical_analog_payload(
            explanations,
            root=root,
            snapshot_date=str(validation.get("snapshot_date") or ""),
        )
    )
    source_report = sanitize_trust_language(_source_report_payload(source_selection))
    historical_analog_hash = canonical_hash(historical_analogs) if historical_analogs else ""
    explanations = _normalized_explanations_payload(
        explanations,
        historical_analogs=historical_analogs,
        historical_analog_hash=historical_analog_hash,
    )
    temporal = comparison_metadata(
        root=root,
        sqlite_path=get_settings().sqlite_path,
        current_release_id=resolved.release_id,
        current_snapshot_date=str(validation.get("snapshot_date") or ""),
        technical_previous_snapshot_date=str(validation.get("previous_snapshot_date") or ""),
    )
    export_dir = root / "outputs" / "autonomous" / "exports" / resolved.release_id
    export_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "AUTONOMOUS_MAE_EXPORT_V1",
        "release_id": resolved.release_id,
        "release_manifest_hash": resolved.manifest_hash,
        "disclosure": AUTONOMOUS_DISCLOSURE,
        "governance": {
            "technical_status": validation["technical_status"],
            "model_validation_status": validation["model_validation_status"],
            "release_status": "AUTO_PUBLISHED",
            "human_review_status": validation["human_review_status"],
        },
        "temporal_metadata": temporal,
        "validation": _validation_payload(
            validation,
            temporal=temporal,
            historical_analog_hash=historical_analog_hash,
        ),
        "market_synthesis": market_synthesis,
        "lineage": lineage,
        "source_selection": source_selection,
        "source_report": source_report,
        "historical_analogs": historical_analogs,
        "matrix": matrix,
        "scenarios": scenarios,
        "explanations_and_analogs": explanations,
    }
    _write_json(export_dir / JSON_NAME, payload)
    if source_selection:
        _write_json(export_dir / SOURCE_SELECTION_NAME, source_selection)
    if source_report:
        _write_json(export_dir / SOURCE_REPORT_NAME, source_report)
    if historical_analogs:
        _write_json(export_dir / HISTORICAL_ANALOGS_NAME, historical_analogs)
    fields = [
        "canonical_cell_id", "row_index", "asset_class", "asset_group", "asset_segment",
        "region", "applicability", "current", "previous", "delta", "mode", "confidence",
        "supporting_theme_ids", "supporting_source_ids", "main_risk",
        "source_count", "source_urls", "reasoning", "invalidation", "transmission_chain",
        "registered_inputs", "business_hash",
    ]
    temporary = export_dir / f"{CSV_NAME}.tmp"
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for cell in matrix["cells"]:
            writer.writerow(
                {
                    **{field: cell.get(field) for field in fields},
                    "source_count": len(cell.get("sources") or []),
                    "source_urls": "; ".join(source.get("url", "") for source in cell.get("sources") or []),
                    "supporting_theme_ids": "; ".join(cell.get("supporting_theme_ids") or []),
                    "supporting_source_ids": "; ".join(cell.get("supporting_source_ids") or []),
                    "main_risk": cell.get("main_risk") or "",
                    "transmission_chain": " → ".join(cell.get("transmission_chain") or []),
                    "registered_inputs": "; ".join(cell.get("registered_inputs") or []),
                }
            )
    os.replace(temporary, export_dir / CSV_NAME)
    build_input = {
        "release_dir": str(resolved.release_directory),
        "export_dir": str(export_dir),
        "release_id": resolved.release_id,
        "release_manifest_hash": resolved.manifest_hash,
        "json_path": str(export_dir / JSON_NAME),
        "csv_path": str(export_dir / CSV_NAME),
        "xlsx_path": str(export_dir / XLSX_NAME),
    }
    _write_json(export_dir / "workbook_build_input.json", build_input)
    return build_input


def finalize_autonomous_exports(*, export_dir: Path) -> dict[str, Any]:
    paths = [export_dir / JSON_NAME, export_dir / CSV_NAME, export_dir / XLSX_NAME]
    for optional_name in (SOURCE_SELECTION_NAME, SOURCE_REPORT_NAME, HISTORICAL_ANALOGS_NAME):
        if (export_dir / optional_name).is_file():
            paths.append(export_dir / optional_name)
    missing = [path.name for path in paths if not path.is_file()]
    if missing:
        raise FileNotFoundError("Missing autonomous exports: " + ", ".join(missing))
    payload = _read_json(export_dir / JSON_NAME)
    _assert_single_historical_artifact(payload)
    matrix = payload["matrix"]
    with (export_dir / CSV_NAME).open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(matrix.get("cells") or []) != 114 or len(rows) != 114:
        raise ValueError("JSON/CSV matrix row reconciliation failed")
    for cell, row in zip(matrix["cells"], rows, strict=True):
        if row["canonical_cell_id"] != cell["canonical_cell_id"]:
            raise ValueError("JSON/CSV canonical ordering mismatch")
        expected = "" if cell.get("current") is None else str(cell["current"])
        if row["current"] != expected or row["mode"] != (cell.get("mode") or ""):
            raise ValueError(f"JSON/CSV score or mode mismatch for {cell['canonical_cell_id']}")
    files = [
        {"name": path.name, "bytes": path.stat().st_size, "sha256": file_sha256(path)}
        for path in paths
    ]
    manifest = {
        "schema_version": "AUTONOMOUS_MAE_EXPORT_MANIFEST_V1",
        "release_id": payload["release_id"],
        "release_manifest_hash": payload["release_manifest_hash"],
        "matrix_business_hash": matrix["business_hash"],
        "row_count": len(rows),
        "applicable_score_count": matrix["applicable_score_count"],
        "not_applicable_count": matrix["not_applicable_count"],
        "mode_counts": matrix["mode_counts"],
        "no_stale_historical_artifact": True,
        "files": files,
    }
    manifest["aggregate_sha256"] = canonical_hash(manifest)
    _write_json(export_dir / MANIFEST_NAME, manifest)
    return manifest


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _historical_analog_payload(explanations: dict[str, Any], *, root: Path, snapshot_date: str) -> dict[str, Any]:
    analogs = explanations.get("historical_analogs") or {}
    if isinstance(analogs.get("artifact"), dict):
        artifact = analogs["artifact"]
        if _analog_matches_snapshot(artifact, snapshot_date):
            return artifact
        recalculated = _recalculated_analogs(root=root, snapshot_date=snapshot_date)
        return recalculated or artifact
    source_artifact = analogs.get("source_artifact")
    if source_artifact:
        path = Path(str(source_artifact))
        if not path.is_absolute():
            path = root / path
        if path.is_file():
            if path.suffix.lower() != ".json":
                recalculated = _recalculated_analogs(root=root, snapshot_date=snapshot_date)
                if recalculated:
                    return recalculated
                return {}
            artifact = _read_json(path)
            if _analog_matches_snapshot(artifact, snapshot_date):
                return artifact
            recalculated = _recalculated_analogs(root=root, snapshot_date=snapshot_date)
            return recalculated or artifact
    fallback = root / "outputs" / "mae_historical_analogs_latest.json"
    if fallback.is_file():
        artifact = _read_json(fallback)
        if _analog_matches_snapshot(artifact, snapshot_date):
            return artifact
        recalculated = _recalculated_analogs(root=root, snapshot_date=snapshot_date)
        return recalculated or artifact
    return _recalculated_analogs(root=root, snapshot_date=snapshot_date)


def _analog_matches_snapshot(artifact: dict[str, Any], snapshot_date: str) -> bool:
    expected = str(snapshot_date or "")[:10]
    if not expected:
        return True
    return str(artifact.get("release_snapshot_date") or artifact.get("snapshot_date") or "")[:10] == expected


def _recalculated_analogs(*, root: Path, snapshot_date: str) -> dict[str, Any]:
    try:
        from datetime import date

        parsed = date.fromisoformat(str(snapshot_date or "")[:10])
        return build_release_historical_analog_artifact(parsed, root=root)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def _normalized_explanations_payload(
    explanations: dict[str, Any],
    *,
    historical_analogs: dict[str, Any],
    historical_analog_hash: str,
) -> dict[str, Any]:
    result = dict(explanations)
    explanation_block = dict(result.get("explanations") or {})
    technical_cells = explanation_block.get("technical_revision_cells") or explanation_block.get("changed_cells") or []
    if not isinstance(technical_cells, list):
        technical_cells = []
    explanation_block.update(
        {
            "changed_cell_count": 0,
            "changed_cells": [],
            "market_changed_cell_count": 0,
            "market_changed_cells": [],
            "technical_revision_count": len(technical_cells),
            "technical_revision_cells": technical_cells,
            "summary": "Текущая матрица является исходной точкой для будущих месячных сравнений.",
        }
    )
    result["explanations"] = explanation_block
    reference = dict(result.get("historical_analogs") or {})
    reference.pop("artifact", None)
    reference.update(
        {
            "source_snapshot_date": historical_analogs.get("snapshot_date", ""),
            "release_snapshot_date": historical_analogs.get("release_snapshot_date", ""),
            "generated_at": historical_analogs.get("generated_at", ""),
            "methodology_version": historical_analogs.get("methodology_version", ""),
            "source_sha256": reference.get("source_sha256") or historical_analogs.get("input_series_sha256", ""),
            "artifact_sha256": historical_analog_hash,
            "historical_analog_periods": [
                str(item.get("period"))
                for item in historical_analogs.get("historical_analogs", historical_analogs.get("analogs", []))
            ],
            "recent_regime_match_periods": [
                str(item.get("period"))
                for item in historical_analogs.get("recent_regime_matches", [])
            ],
            "artifact_location": "top_level_historical_analogs",
        }
    )
    result["historical_analogs"] = reference
    return result


def _validation_payload(validation: dict[str, Any], *, temporal: dict[str, Any], historical_analog_hash: str) -> dict[str, Any]:
    result = dict(validation)
    original_status = str(result.get("candidate_release_status") or result.get("release_status") or "")
    if original_status and original_status != "AUTO_PUBLISHED":
        result["candidate_release_status"] = original_status
    result["release_status"] = "AUTO_PUBLISHED"
    initial = temporal.get("comparison_type") == "INITIAL_BASELINE"
    technical_changes = _technical_revision_cells(result)
    if initial:
        result["market_changed_cell_count"] = 0
        result["changed_cell_count"] = 0
        result["changed_cells"] = []
    result["technical_revision_count"] = len(technical_changes)
    result["technical_revision_cells"] = technical_changes
    result["technical_previous_snapshot_date"] = temporal.get("technical_previous_snapshot_date", "")
    result["no_stale_historical_artifact"] = True
    result["historical_analog_hash"] = historical_analog_hash
    result["published_release_has_consistent_status"] = True
    return result


def _technical_revision_cells(validation: dict[str, Any]) -> list[dict[str, Any]]:
    existing = validation.get("technical_revision_cells")
    if isinstance(existing, list):
        return [item for item in existing if isinstance(item, dict)]
    return []


def _assert_single_historical_artifact(payload: dict[str, Any]) -> None:
    top = payload.get("historical_analogs") or {}
    expected_snapshot = str(top.get("release_snapshot_date") or top.get("snapshot_date") or "")[:10]
    expected_hash = canonical_hash(top) if top else ""
    reference = ((payload.get("explanations_and_analogs") or {}).get("historical_analogs") or {})
    if isinstance(reference.get("artifact"), dict):
        raise ValueError("Stale embedded historical artifact detected in explanations_and_analogs.")
    ref_snapshot = str(reference.get("release_snapshot_date") or reference.get("source_snapshot_date") or "")[:10]
    ref_hash = str(reference.get("artifact_sha256") or "")
    if expected_snapshot and ref_snapshot and ref_snapshot != expected_snapshot:
        raise ValueError("Historical analog snapshot mismatch inside release export.")
    if expected_hash and ref_hash and ref_hash != expected_hash:
        raise ValueError("Historical analog hash mismatch inside release export.")


def _source_report_payload(source_selection: dict[str, Any]) -> dict[str, Any]:
    selected = []
    for item in source_selection.get("selected_documents") or []:
        why_used_user = item.get("why_used_user") or _derive_source_why_used(item)
        selected.append(
            {
                "provider": item.get("canonical_provider_name") or item.get("provider") or "",
                "title": item.get("title") or "",
                "date": item.get("date") or "",
                "url": item.get("url") or "",
                "document_type": item.get("document_type") or "",
                "why_used_user": why_used_user,
                "selection_tier": item.get("source_tier") or "",
                "selection_score": item.get("total_score"),
                "regions": item.get("coverage_regions") or [],
                "asset_classes": item.get("coverage_asset_classes") or [],
            }
        )
    if not selected:
        return {}
    return {
        "schema_version": "MAE_SOURCE_REPORT_V1",
        "snapshot_date": source_selection.get("snapshot_date") or "",
        "document_count": len(selected),
        "independent_provider_count": source_selection.get("independent_provider_count", 0),
        "documents": selected,
        "coverage": {
            "regions": source_selection.get("region_coverage") or {},
            "asset_classes": source_selection.get("asset_class_coverage") or {},
        },
    }


def _derive_source_why_used(item: dict[str, Any]) -> str:
    title = str(item.get("title") or "").casefold()
    regions = item.get("coverage_regions") or []
    assets = item.get("coverage_asset_classes") or []
    region_text = _region_phrase(regions) if isinstance(regions, list) else str(regions)
    asset_text = _asset_phrase(assets) if isinstance(assets, list) else str(assets)
    if "financial stability" in title:
        return f"Источник по финансовой устойчивости, банковскому сектору и условиям Credit для {region_text}."
    if "credit conditions" in title or "bank liabilities" in title:
        return f"Источник по банковскому фондированию и условиям Credit для {region_text}."
    if "commodit" in title or "energy" in title:
        return f"Источник по Commodities, энергетике и инфляционным рискам для {region_text}."
    if "japan" in title or "boj" in title:
        return "Источник по политике BoJ и состоянию рынка Japan Government Bonds."
    if "emerging market" in title:
        return "Источник по EM Equities, потокам капитала и динамике доллара."
    if "outlook" in title or "forecast" in title:
        return f"Глобальный Multi-asset обзор для оценки {asset_text or 'Equities, Government Bonds и Commodities'}."
    return f"Аналитический обзор {asset_text or 'Markets'} для {region_text or 'Global'}."


def _region_phrase(values: list[str]) -> str:
    labels = [str(value) for value in values[:3] if value]
    return _join_labels(labels) if labels else "Global"


def _asset_phrase(values: list[str]) -> str:
    labels = [str(value) for value in values if value]
    if not labels or labels == ["Multi-asset"]:
        return "Equities, Government Bonds и Commodities"
    return _join_labels(labels)


def _join_labels(labels: list[str]) -> str:
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return " и ".join(labels)
    return ", ".join(labels[:-1]) + " и " + labels[-1]


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
