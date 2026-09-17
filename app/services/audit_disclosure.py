from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from app.config import ROOT_DIR


DEFAULT_CANDIDATE_ID = "2026-07-13-financial-remediation"
QUARANTINE_BANNER = "Технически проверено; финансовая проверка не завершена"
SOURCE_TAXONOMY_LABELS = {
    "research_documents": "Research documents",
    "data_providers": "Data providers",
    "benchmarks": "Benchmarks",
    "evidence_items": "Evidence items",
    "approved_direct_evidence": "Approved direct evidence",
    "composite_inputs": "Composite inputs",
    "carry_forward_lineage": "Carry-forward lineage",
}
CANDIDATE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass
class AuditDisclosure:
    candidate_id: str = ""
    snapshot_date: str = ""
    technical_status: str = "UNAVAILABLE"
    financial_status: str = "UNAVAILABLE"
    analyst_review_status: str = "UNAVAILABLE"
    release_status: str = "UNAVAILABLE"
    reviewer_actor_type: str = "NONE"
    human_approval_created: bool = False
    cells: pd.DataFrame = field(default_factory=pd.DataFrame)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    source_taxonomy: dict[str, int] = field(default_factory=dict)
    what_changed: dict[str, Any] = field(default_factory=dict)
    shift_signals: dict[str, Any] = field(default_factory=dict)
    scenarios: dict[str, Any] = field(default_factory=dict)
    transmission_paths: dict[str, Any] = field(default_factory=dict)
    analogs: dict[str, Any] = field(default_factory=dict)
    reconciliation: dict[str, Any] = field(default_factory=dict)
    paths: dict[str, Path] = field(default_factory=dict)
    errors: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return len(self.cells) == 114 and bool(self.candidate_id)

    @property
    def is_quarantined(self) -> bool:
        return self.release_status == "QUARANTINED"

    @property
    def banner(self) -> str:
        return QUARANTINE_BANNER if self.is_quarantined else ""

    @property
    def status_counts(self) -> dict[str, int]:
        if self.cells.empty:
            return {}
        counts = self.cells["score_status"].value_counts().to_dict()
        return {str(key): int(value) for key, value in counts.items()}


def candidate_root(root: Path = ROOT_DIR, candidate_id: str = DEFAULT_CANDIDATE_ID) -> Path:
    validate_candidate_id(candidate_id)
    return root / "outputs" / "candidates" / candidate_id


def validate_candidate_id(candidate_id: str) -> str:
    value = str(candidate_id or "")
    if value in {".", ".."} or not CANDIDATE_ID_PATTERN.fullmatch(value):
        raise ValueError("candidate_id must be 1-128 safe ASCII filename characters and cannot contain traversal")
    return value


def audit_disclosure_paths(root: Path = ROOT_DIR, candidate_id: str = DEFAULT_CANDIDATE_ID) -> dict[str, Path]:
    directory = candidate_root(root, candidate_id)
    analog_directory = directory / "historical_analogs"
    return {
        "root": directory,
        "report": directory / "financial_validation_report.json",
        "cells": directory / "cell_financial_audit.csv",
        "evidence": directory / "eligible_evidence.json",
        "what_changed": directory / "what_changed.json",
        "shift_signals": directory / "shift_signals.json",
        "scenarios": directory / "scenarios.json",
        "transmission_paths": directory / "transmission_paths.json",
        "analogs": analog_directory / "historical_analogs_v2.json",
        "json_export": directory / "MAE_audit_disclosure.json",
        "csv_export": directory / "MAE_audit_disclosure.csv",
        "xlsx_export": directory / "MAE_financial_candidate.xlsx",
    }


def load_audit_disclosure(
    *, root: Path = ROOT_DIR, candidate_id: str = DEFAULT_CANDIDATE_ID
) -> AuditDisclosure:
    paths = audit_disclosure_paths(root, candidate_id)
    errors: list[str] = []
    report = _read_json(paths["report"], errors, "financial validation report")
    eligible = _read_json(paths["evidence"], errors, "eligible evidence")
    cells = _read_cells(paths["cells"], errors)
    governance = dict(report.get("governance") or {})
    production_governance = dict(report.get("production_governance") or {})
    evidence = list(eligible.get("records") or [])
    taxonomy = _source_taxonomy(cells, evidence)

    return AuditDisclosure(
        candidate_id=str(report.get("candidate_id") or candidate_id),
        snapshot_date=str(report.get("snapshot_date") or ""),
        technical_status=str(governance.get("technical_status") or "UNAVAILABLE"),
        financial_status=str(governance.get("financial_status") or "UNAVAILABLE"),
        analyst_review_status=str(governance.get("analyst_review_status") or "UNAVAILABLE"),
        release_status=str(governance.get("release_status") or "UNAVAILABLE"),
        reviewer_actor_type=str(production_governance.get("reviewer_actor_type") or "NONE"),
        human_approval_created=bool(governance.get("human_approval_created", False)),
        cells=cells,
        evidence=evidence,
        source_taxonomy=taxonomy,
        what_changed=_read_json(paths["what_changed"], errors, "What Changed"),
        shift_signals=_read_json(paths["shift_signals"], errors, "Shift Signal"),
        scenarios=_read_json(paths["scenarios"], errors, "scenarios"),
        transmission_paths=_read_json(paths["transmission_paths"], errors, "transmission paths"),
        analogs=_read_json(paths["analogs"], errors, "historical analogs v2"),
        reconciliation=dict(report.get("reconciliation") or {}),
        paths=paths,
        errors=tuple(dict.fromkeys(errors)),
    )


def export_audit_disclosure(
    *, root: Path = ROOT_DIR, candidate_id: str = DEFAULT_CANDIDATE_ID
) -> dict[str, Path]:
    """Write deterministic JSON/CSV peers for the already-built candidate workbook."""
    disclosure = load_audit_disclosure(root=root, candidate_id=candidate_id)
    if not disclosure.available or disclosure.errors:
        raise ValueError(f"Audit disclosure is incomplete: {', '.join(disclosure.errors) or 'invalid geometry'}")
    paths = disclosure.paths
    payload = disclosure_payload(disclosure)
    paths["json_export"].write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    taxonomy_columns = list(SOURCE_TAXONOMY_LABELS)
    cell_columns = [
        "canonical_cell_id",
        "template_row_key",
        "asset_segment",
        "geography",
        "applicability",
        "score_status",
        "score",
        "direction",
        "evidence_mode",
        "confidence",
        "approved_source_count",
        "review_status",
        "limitation_reason",
        "rejection_reason",
    ]
    with paths["csv_export"].open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=cell_columns + taxonomy_columns)
        writer.writeheader()
        for cell in disclosure.cells.to_dict("records"):
            row = {column: cell.get(column, "") for column in cell_columns}
            row.update(disclosure.source_taxonomy)
            writer.writerow(row)
    return {key: paths[key] for key in ("json_export", "csv_export", "xlsx_export")}


def disclosure_payload(disclosure: AuditDisclosure) -> dict[str, Any]:
    cells = [
        {key: (None if pd.isna(value) else value) for key, value in record.items()}
        for record in disclosure.cells.to_dict("records")
    ]
    return {
        "schema_version": "1.0",
        "candidate_id": disclosure.candidate_id,
        "snapshot_date": disclosure.snapshot_date,
        "banner": disclosure.banner,
        "governance": {
            "technical_status": disclosure.technical_status,
            "financial_status": disclosure.financial_status,
            "analyst_review_status": disclosure.analyst_review_status,
            "release_status": disclosure.release_status,
            "reviewer_actor_type": disclosure.reviewer_actor_type,
            "human_approval_created": disclosure.human_approval_created,
        },
        "source_taxonomy": disclosure.source_taxonomy,
        "reconciliation": disclosure.reconciliation,
        "cells": cells,
        "eligible_evidence": disclosure.evidence,
        "what_changed": disclosure.what_changed,
        "shift_signals": disclosure.shift_signals,
        "scenarios": disclosure.scenarios,
        "transmission_paths": disclosure.transmission_paths,
        "historical_analogs": disclosure.analogs,
    }


def _read_cells(path: Path, errors: list[str]) -> pd.DataFrame:
    if not path.is_file():
        errors.append("cell financial audit is missing")
        return pd.DataFrame()
    try:
        cells = pd.read_csv(path, keep_default_na=False, encoding="utf-8-sig")
    except (OSError, UnicodeError, pd.errors.ParserError) as exc:
        errors.append(f"cell financial audit failed to load ({type(exc).__name__})")
        return pd.DataFrame()
    cells["score"] = pd.to_numeric(cells["score"], errors="coerce")
    cells["approved_source_count"] = pd.to_numeric(cells["approved_source_count"], errors="coerce").fillna(0).astype(int)
    cells["asset_segment"] = cells["template_row_key"].map(_asset_segment)
    cells["geography"] = cells["region"]
    cells["applicable"] = cells["applicability"].eq("APPLICABLE")
    cells["cell_id"] = cells["canonical_cell_id"]
    return cells


def _source_taxonomy(cells: pd.DataFrame, evidence: list[dict[str, Any]]) -> dict[str, int]:
    sources = [source for item in evidence for source in item.get("sources") or []]
    modes = cells.get("evidence_mode", pd.Series(dtype=str)).astype(str)
    statuses = cells.get("score_status", pd.Series(dtype=str)).astype(str)
    return {
        "research_documents": len({str(item.get("research_view_id") or "") for item in evidence if item.get("research_view_id")}),
        "data_providers": len({str(source.get("institution") or "") for source in sources if source.get("role") == "DATA_PROVIDER"}),
        "benchmarks": len({str(source.get("source_id") or "") for source in sources if source.get("role") == "BENCHMARK"}),
        "evidence_items": len(evidence),
        "approved_direct_evidence": sum(1 for item in evidence if item.get("evidence_mode") == "DIRECT"),
        "composite_inputs": int(modes.eq("COMPOSITE").sum()),
        "carry_forward_lineage": int(statuses.eq("CARRY_FORWARD").sum()),
    }


def _asset_segment(row_key: str) -> str:
    parts = str(row_key).split("|")
    if len(parts) != 3:
        return str(row_key)
    asset_class, group, segment = parts
    if segment == "Market Breadth (Equal Weight)":
        return "Market Breadth"
    if asset_class == "FIXED INCOME" and group == "GOV":
        return f"Government Bonds {segment}"
    if asset_class == "FIXED INCOME" and group == "CORP HY":
        return f"Corporate High Yield {segment}"
    if asset_class == "FIXED INCOME" and group == "CORP IG":
        return f"Corporate Investment Grade {segment}"
    if segment == "Other precious metals":
        return "Other Precious Metals"
    return segment


def _read_json(path: Path, errors: list[str], label: str) -> dict[str, Any]:
    if not path.is_file():
        errors.append(f"{label} is missing")
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        errors.append(f"{label} failed to load ({type(exc).__name__})")
        return {}
