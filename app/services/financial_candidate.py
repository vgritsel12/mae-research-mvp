from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Sequence

from app.config import ROOT_DIR
from app.domain.enums import Direction, ScoreStatus
from app.domain.schemas import FinancialCandidateCell
from app.services.audit_baseline import canonical_hash, sha256_file, verify_audit_baseline
from app.services.normalization import CanonicalCell, canonical_cell_registry
from app.services.strict_financial_validator import (
    StrictContractPaths,
    assert_strict_contract,
    evaluate_mapping,
)
from app.services.strict_review_application import CANDIDATE_ID_PATTERN, RESERVED_CANDIDATE_IDS


CANDIDATE_SCHEMA_VERSION = "2.0.0"
CANDIDATE_BUILDER_VERSION = "MAE-FINANCIAL-CANDIDATE-2.0.0"
MAX_CARRY_FORWARD_AGE_DAYS = 90
PHASE3_REQUIRED_FILES = (
    "financial_review_before_after.json",
    "active_true_pass.csv",
    "downgraded_mappings.csv",
    "manual_review_queue.csv",
    "rejected_mappings.csv",
)
PHASE5_JSON_FILES = (
    "financial_validation_report.json",
    "eligible_evidence.json",
    "mae_matrix.json",
    "shift_signals.json",
    "what_changed.json",
    "scenarios.json",
    "transmission_paths.json",
)
PHASE5_CSV_FILES = (
    "cell_financial_audit.csv",
    "unscored_cells.csv",
    "source_role_counts.csv",
)


class FinancialCandidateError(RuntimeError):
    """Hard-stop before publication or production mutation."""


@dataclass(frozen=True)
class FinancialCandidateBuildResult:
    candidate_id: str
    candidate_root: Path
    run_id: str
    business_data_hash: str
    reconciliation: dict[str, Any]
    cells: tuple[dict[str, Any], ...]
    export_bytes: dict[str, bytes]
    artifact_hashes: dict[str, str]
    workbook_qa: dict[str, Any]
    wrote_outputs: bool
    published: bool = False


def build_financial_candidate(
    *,
    root: Path,
    snapshot_date: date,
    candidate_id: str,
    no_publish: bool,
    build_workbook: bool = True,
    write_outputs: bool = True,
    node_executable: str | None = None,
) -> FinancialCandidateBuildResult:
    """Build a sparse candidate from pinned review data without touching production."""
    root = root.resolve()
    _validate_candidate_id(candidate_id)
    if not no_publish:
        raise FinancialCandidateError("Real candidate builds require --no-publish; release is a separate human-gated workflow.")
    candidate_root = _candidate_root(root, candidate_id)
    baseline_path = (
        root
        / "outputs"
        / "audit_baseline"
        / snapshot_date.isoformat()
        / f"AUDIT_BASELINE_{snapshot_date.isoformat()}.json"
    )
    baseline = _load_json(baseline_path)
    baseline_verification = verify_audit_baseline(
        root=root,
        manifest_path=baseline_path,
        strict_database_binary=False,
    )
    if not baseline_verification["ok"]:
        raise FinancialCandidateError(
            "BASELINE_MISMATCH_HARD_STOP: " + "; ".join(baseline_verification["mismatches"])
        )
    if baseline.get("aggregate_sha256") != baseline_verification["aggregate_sha256"]:
        raise FinancialCandidateError("BASELINE_MISMATCH_HARD_STOP: aggregate hash mismatch")
    if baseline.get("snapshot_date") != snapshot_date.isoformat():
        raise FinancialCandidateError("BASELINE_MISMATCH_HARD_STOP: snapshot date mismatch")

    review_payload, review_manifest = _load_review_bundle(candidate_root, candidate_id, snapshot_date, baseline)
    strict_case = _strict_true_pass_case(root, review_payload)
    strict_result = evaluate_mapping(strict_case, StrictContractPaths(
        playbook=root / "FINANCIAL_MAPPING_PLAYBOOK_v2_APPROVED.md",
        source_policy=root / "data" / "source_governance_v2.json",
        fixture=root / "tests" / "fixtures" / "financial_mapping_v2.json",
    ))
    assert_strict_contract(strict_result)
    target = strict_result["target_results"][0]
    if not target["admitted"] or target["financial_status"] != "TRUE_PASS":
        raise FinancialCandidateError("Signed TRUE_PASS failed strict v2 replay; candidate build stopped.")

    run_id = canonical_hash(
        {
            "builder_version": CANDIDATE_BUILDER_VERSION,
            "candidate_id": candidate_id,
            "snapshot_date": snapshot_date.isoformat(),
            "baseline_aggregate_sha256": baseline["aggregate_sha256"],
            "review_business_hash": review_payload["business_content_hash"],
            "contract_hashes": strict_result["contract"]["hashes"],
        }
    )
    built_at = datetime.now(UTC).isoformat()
    decisions = list(review_payload["decisions"])
    decisions_by_cell: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for decision in decisions:
        decisions_by_cell[str(decision["canonical_cell_id"])].append(decision)

    eligible_evidence = [_eligible_evidence_record(strict_case, strict_result, decisions)]
    cells = _build_cells(
        canonical_cell_registry(include_not_applicable=True),
        decisions_by_cell=decisions_by_cell,
        candidate_id=candidate_id,
        run_id=run_id,
        snapshot_date=snapshot_date,
        eligible_evidence=eligible_evidence,
    )
    cell_dicts = [cell.model_dump(mode="json") for cell in cells]
    derived = _derived_outputs(cells, eligible_evidence)
    source_role_counts = _source_role_counts(eligible_evidence)
    reconciliation = _reconciliation(cells, eligible_evidence, source_role_counts)
    _validate_reconciliation(reconciliation)
    governance = _read_governance_state(root / "data" / "mae.db", baseline)
    if governance["analyst_review_status"] != "NOT_REVIEWED" or governance["release_status"] != "QUARANTINED":
        raise FinancialCandidateError("Governance hard-stop: real production record is no longer NOT_REVIEWED/QUARANTINED.")
    if governance["human_review_decision_count"] != 0:
        raise FinancialCandidateError("Governance hard-stop: unexpected human-review decision exists.")

    stages = _stage_payloads(
        run_id=run_id,
        built_at=built_at,
        eligible_evidence=eligible_evidence,
        cells=cell_dicts,
        derived=derived,
    )
    business_payload = {
        "schema_version": CANDIDATE_SCHEMA_VERSION,
        "builder_version": CANDIDATE_BUILDER_VERSION,
        "candidate_id": candidate_id,
        "snapshot_date": snapshot_date.isoformat(),
        "run_id": run_id,
        "baseline_aggregate_sha256": baseline["aggregate_sha256"],
        "review_business_hash": review_payload["business_content_hash"],
        "strict_contract": strict_result["contract"],
        "eligible_evidence": eligible_evidence,
        "cells": cell_dicts,
        "derived_outputs": derived,
        "source_role_counts": source_role_counts,
        "reconciliation": reconciliation,
        "governance": {
            "technical_status": "PASSED",
            "financial_status": "MANUAL_REVIEW_REQUIRED",
            "analyst_review_status": "NOT_REVIEWED",
            "release_status": "QUARANTINED",
            "human_approval_created": False,
        },
    }
    business_data_hash = canonical_hash(business_payload)
    report = {
        **business_payload,
        "business_data_hash": business_data_hash,
        "metadata": {"built_at": built_at, "volatile_fields_excluded_from_business_hash": ["metadata.built_at"]},
        "production_governance": governance,
        "baseline_verification": baseline_verification,
        "review_manifest_hash": sha256_file(candidate_root / "strict_review_application_manifest.json"),
        "intermediate_stages": [
            {"file": name, "content_hash": payload["content_hash"]} for name, payload in stages.items()
        ],
        "workbook_verification": {"status": "PENDING" if build_workbook and write_outputs else "SKIPPED"},
        "conclusion": "CANDIDATE READY FOR HUMAN REVIEW — NOT FINANCIALLY APPROVED",
    }
    file_bytes = _output_files(report, cell_dicts, eligible_evidence, source_role_counts, derived, stages)
    workbook_qa: dict[str, Any] = {"status": "SKIPPED"}
    artifact_hashes = {name: _sha256_bytes(content) for name, content in file_bytes.items()}

    if write_outputs:
        candidate_root.mkdir(parents=True, exist_ok=True)
        for name, content in file_bytes.items():
            _atomic_write(candidate_root / name, content)
        if build_workbook:
            _build_workbook(
                root=root,
                candidate_root=candidate_root,
                node_executable=node_executable,
            )
            workbook_qa = _load_json(candidate_root / "workbook_qa.json")
            if workbook_qa.get("status") != "PASSED" or workbook_qa.get("formula_error_count") != 0:
                raise FinancialCandidateError(f"Workbook verification failed: {workbook_qa}")
            report["workbook_verification"] = workbook_qa
            final_report = _json_bytes(report)
            _atomic_write(candidate_root / "financial_validation_report.json", final_report)
            artifact_hashes["financial_validation_report.json"] = _sha256_bytes(final_report)
            for path in sorted(candidate_root.glob("workbook_previews/*.png")):
                artifact_hashes[str(path.relative_to(candidate_root))] = sha256_file(path)
            for name in ("MAE_financial_candidate.xlsx", "workbook_qa.json"):
                artifact_hashes[name] = sha256_file(candidate_root / name)
        artifact_manifest = {
            "schema_version": CANDIDATE_SCHEMA_VERSION,
            "candidate_id": candidate_id,
            "snapshot_date": snapshot_date.isoformat(),
            "run_id": run_id,
            "business_data_hash": business_data_hash,
            "baseline_aggregate_sha256": baseline["aggregate_sha256"],
            "review_business_hash": review_payload["business_content_hash"],
            "artifact_hashes": dict(sorted(artifact_hashes.items())),
            "governance": report["governance"],
            "publication_performed": False,
        }
        manifest_bytes = _json_bytes(artifact_manifest)
        _atomic_write(candidate_root / "financial_candidate_artifact_manifest.json", manifest_bytes)
        artifact_hashes["financial_candidate_artifact_manifest.json"] = _sha256_bytes(manifest_bytes)

    final_baseline = verify_audit_baseline(root=root, manifest_path=baseline_path, strict_database_binary=False)
    if not final_baseline["ok"]:
        raise FinancialCandidateError(
            "POST_BUILD_BASELINE_MISMATCH_HARD_STOP: " + "; ".join(final_baseline["mismatches"])
        )
    return FinancialCandidateBuildResult(
        candidate_id=candidate_id,
        candidate_root=candidate_root,
        run_id=run_id,
        business_data_hash=business_data_hash,
        reconciliation=reconciliation,
        cells=tuple(cell_dicts),
        export_bytes=dict(file_bytes),
        artifact_hashes=dict(sorted(artifact_hashes.items())),
        workbook_qa=workbook_qa,
        wrote_outputs=write_outputs,
    )


def carry_forward_or_insufficient(
    *,
    source: FinancialCandidateCell,
    candidate_id: str,
    run_id: str,
    snapshot_date: date,
    age_days: int,
    max_age_days: int = MAX_CARRY_FORWARD_AGE_DAYS,
) -> FinancialCandidateCell:
    """Create an explicitly traced carry-forward, or expire it to null/insufficient."""
    if source.score_status not in {ScoreStatus.ACTIVE, ScoreStatus.CARRY_FORWARD} or source.score is None:
        raise FinancialCandidateError("Carry-forward source must be a scored ACTIVE/CARRY_FORWARD cell.")
    if not source.cell_business_hash or not bool(source.trace.get("strict_admission")):
        raise FinancialCandidateError("Carry-forward source lacks admitted immutable lineage.")
    base = {
        "candidate_id": candidate_id,
        "run_id": run_id,
        "snapshot_date": snapshot_date,
        "canonical_cell_id": source.canonical_cell_id,
        "template_row_key": source.template_row_key,
        "region": source.region,
        "applicability": source.applicability,
    }
    if age_days > max_age_days:
        return _hashed_cell(
            {
                **base,
                "score_status": ScoreStatus.INSUFFICIENT_DATA,
                "score": None,
                "direction": None,
                "limitation_reason": f"Admitted prior score expired at {age_days} days; maximum is {max_age_days}.",
                "rejection_reason": "",
                "carry_forward_source_candidate_id": "",
                "carry_forward_source_cell_hash": "",
                "carry_forward_age_days": None,
                "trace": {"strict_admission": False, "explicit_neutral": False, "expired_source_hash": source.cell_business_hash},
            }
        )
    return _hashed_cell(
        {
            **base,
            "score_status": ScoreStatus.CARRY_FORWARD,
            "score": source.score,
            "direction": source.direction,
            "limitation_reason": f"Carried forward {age_days} days from an admitted prior candidate; no new current evidence.",
            "rejection_reason": "",
            "carry_forward_source_candidate_id": source.candidate_id,
            "carry_forward_source_cell_hash": source.cell_business_hash,
            "carry_forward_age_days": age_days,
            "trace": {
                **source.trace,
                "strict_admission": True,
                "explicit_neutral": bool(source.trace.get("explicit_neutral")),
                "carry_forward_disclosure": True,
            },
        }
    )


def is_material_shift(
    *,
    score_status: ScoreStatus,
    score: int | None,
    prior_score: int | None,
) -> bool:
    return (
        score_status in {ScoreStatus.ACTIVE, ScoreStatus.CARRY_FORWARD}
        and isinstance(score, int)
        and not isinstance(score, bool)
        and isinstance(prior_score, int)
        and not isinstance(prior_score, bool)
        and abs(score - prior_score) >= 1
    )


def _validate_candidate_id(candidate_id: str) -> None:
    if not CANDIDATE_ID_PATTERN.fullmatch(candidate_id) or candidate_id.casefold() in RESERVED_CANDIDATE_IDS:
        raise FinancialCandidateError("Candidate ID is invalid or reserved.")


def _candidate_root(root: Path, candidate_id: str) -> Path:
    base = (root / "outputs" / "candidates").resolve()
    destination = (base / candidate_id).resolve()
    try:
        destination.relative_to(base)
    except ValueError as exc:
        raise FinancialCandidateError("Candidate output escaped outputs/candidates.") from exc
    return destination


def _load_review_bundle(
    candidate_root: Path,
    candidate_id: str,
    snapshot_date: date,
    baseline: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest_path = candidate_root / "strict_review_application_manifest.json"
    manifest = _load_json(manifest_path)
    if manifest.get("candidate_id") != candidate_id or manifest.get("snapshot_date") != snapshot_date.isoformat():
        raise FinancialCandidateError("Strict-review candidate identity/date mismatch.")
    if manifest.get("baseline_aggregate_sha256") != baseline.get("aggregate_sha256"):
        raise FinancialCandidateError("BASELINE_MISMATCH_HARD_STOP: strict-review baseline binding mismatch")
    for name in PHASE3_REQUIRED_FILES:
        path = candidate_root / name
        expected = str(manifest.get("output_hashes", {}).get(name) or "")
        if not path.is_file() or sha256_file(path) != expected:
            raise FinancialCandidateError(f"Strict-review artifact hash mismatch: {name}")
    payload = _load_json(candidate_root / "financial_review_before_after.json")
    supplied_hash = str(payload.get("business_content_hash") or "")
    unhashed = dict(payload)
    unhashed.pop("business_content_hash", None)
    if canonical_hash(unhashed) != supplied_hash or supplied_hash != manifest.get("business_content_hash"):
        raise FinancialCandidateError("Strict-review business content hash mismatch.")
    if len(payload.get("decisions", [])) != 322 or payload.get("summary", {}).get("active_financial_pass_count") != 1:
        raise FinancialCandidateError("Strict-review decision reconciliation mismatch.")
    return payload, manifest


def _strict_true_pass_case(root: Path, review_payload: dict[str, Any]) -> dict[str, Any]:
    eligible = [decision for decision in review_payload["decisions"] if decision.get("financially_eligible") is True]
    if len(eligible) != 1:
        raise FinancialCandidateError("Exactly one signed eligible decision is required.")
    fixture = _load_json(root / "tests" / "fixtures" / "financial_mapping_v2.json")
    cases = [case for case in fixture["cases"] if case["case_id"] == eligible[0]["research_view_id"]]
    if len(cases) != 1 or bool(cases[0].get("is_demo")):
        raise FinancialCandidateError("Signed non-DEMO true-pass fixture is missing or ambiguous.")
    if cases[0]["targets"][0]["target_id"] != eligible[0]["canonical_cell_id"]:
        raise FinancialCandidateError("Signed fixture target differs from strict-review decision.")
    return cases[0]


def _eligible_evidence_record(
    strict_case: dict[str, Any],
    strict_result: dict[str, Any],
    decisions: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    target = strict_result["target_results"][0]
    decision = next(row for row in decisions if row.get("financially_eligible") is True)
    source_policy = _load_json(ROOT_DIR / "data" / "source_governance_v2.json")
    mode_rule = source_policy["evidence_modes"][strict_case["evidence_mode"]]
    return {
        "research_view_id": strict_case["case_id"],
        "canonical_cell_id": target["target_id"],
        "publication_title": strict_case["publication_title"],
        "exact_quote": strict_case["exact_quote"],
        "direction": target["requested_direction"],
        "score": target["score"],
        "confidence": target["confidence"],
        "evidence_mode": target["evidence_mode"],
        "score_cap": mode_rule["absolute_score_cap"],
        "sources": strict_case["sources"],
        "approved_source_count": len(strict_case["sources"]),
        "review_decision": {
            "decision_id": decision["decision_id"],
            "review_version": decision["review_version"],
            "review_timestamp": decision["review_timestamp"],
            "input_content_hash": decision["input_content_hash"],
            "baseline_record_hash": decision["baseline_record_hash"],
            "financial_status": decision["new_status"],
        },
        "contract": strict_result["contract"],
        "gates": target["gates"],
        "validator_result_hash": target["result_hash"],
        "validator_output_hash": strict_result["output_hash"],
        "limitations": [
            "Methodology admission is not analyst approval.",
            "Candidate remains NOT_REVIEWED and QUARANTINED.",
        ],
    }


def _build_cells(
    registry: Sequence[CanonicalCell],
    *,
    decisions_by_cell: dict[str, list[dict[str, Any]]],
    candidate_id: str,
    run_id: str,
    snapshot_date: date,
    eligible_evidence: list[dict[str, Any]],
) -> list[FinancialCandidateCell]:
    evidence_by_cell = {item["canonical_cell_id"]: item for item in eligible_evidence}
    cells: list[FinancialCandidateCell] = []
    for canonical in registry:
        decisions = decisions_by_cell.get(canonical.canonical_cell_id, [])
        active = [row for row in decisions if row.get("financially_eligible") is True]
        common = {
            "candidate_id": candidate_id,
            "run_id": run_id,
            "snapshot_date": snapshot_date,
            "canonical_cell_id": canonical.canonical_cell_id,
            "template_row_key": canonical.template_row_key,
            "region": canonical.region,
            "applicability": canonical.applicability,
            "carry_forward_source_candidate_id": "",
            "carry_forward_source_cell_hash": "",
            "carry_forward_age_days": None,
        }
        decision_summary = dict(sorted(Counter(str(row["new_status"]) for row in decisions).items()))
        if canonical.applicability == "NOT_APPLICABLE":
            payload = {
                **common,
                "score_status": ScoreStatus.NOT_APPLICABLE,
                "score": None,
                "direction": None,
                "limitation_reason": "Canonical template marks this region/row position NOT_APPLICABLE.",
                "rejection_reason": "",
                "trace": {"strict_admission": False, "explicit_neutral": False, "decision_counts": decision_summary},
            }
        elif active:
            if len(active) != 1 or canonical.canonical_cell_id not in evidence_by_cell:
                raise FinancialCandidateError(f"Active evidence does not reconcile for {canonical.canonical_cell_id}")
            evidence = evidence_by_cell[canonical.canonical_cell_id]
            gates = evidence["gates"]
            explicit_neutral = (
                evidence["direction"] == "NEUTRAL"
                and evidence["score"] == 0
                and gates["direction"]["passed"] is True
                and bool(gates["direction"]["observed"].get("neutral"))
            )
            payload = {
                **common,
                "score_status": ScoreStatus.ACTIVE,
                "score": int(evidence["score"]),
                "direction": Direction(evidence["direction"]),
                "limitation_reason": "Methodology-admitted candidate score; analyst review is still NOT_REVIEWED.",
                "rejection_reason": "",
                "trace": {
                    "strict_admission": True,
                    "explicit_neutral": explicit_neutral,
                    "research_view_id": evidence["research_view_id"],
                    "review_decision": evidence["review_decision"],
                    "gates": evidence["gates"],
                    "sources": evidence["sources"],
                    "approved_source_count": evidence["approved_source_count"],
                    "evidence_mode": evidence["evidence_mode"],
                    "score_cap": evidence["score_cap"],
                    "confidence": evidence["confidence"],
                    "contract": evidence["contract"],
                    "validator_result_hash": evidence["validator_result_hash"],
                    "decision_counts": decision_summary,
                },
            }
        elif any(row.get("new_status") == "MANUAL_REVIEW_REQUIRED" for row in decisions):
            manual_count = sum(row.get("new_status") == "MANUAL_REVIEW_REQUIRED" for row in decisions)
            payload = {
                **common,
                "score_status": ScoreStatus.INSUFFICIENT_DATA,
                "score": None,
                "direction": None,
                "limitation_reason": f"{manual_count} unresolved mapping(s) require human review; no score inferred.",
                "rejection_reason": "",
                "trace": {
                    "strict_admission": False,
                    "explicit_neutral": False,
                    "decision_counts": decision_summary,
                    "decision_hashes": sorted(row["decision_id"] for row in decisions),
                },
            }
        elif decisions:
            payload = {
                **common,
                "score_status": ScoreStatus.REJECTED,
                "score": None,
                "direction": None,
                "limitation_reason": "",
                "rejection_reason": "All current mappings were downgraded or rejected by strict financial review.",
                "trace": {
                    "strict_admission": False,
                    "explicit_neutral": False,
                    "decision_counts": decision_summary,
                    "decision_hashes": sorted(row["decision_id"] for row in decisions),
                },
            }
        else:
            payload = {
                **common,
                "score_status": ScoreStatus.INSUFFICIENT_DATA,
                "score": None,
                "direction": None,
                "limitation_reason": "No financially admissible current evidence exists for this applicable cell.",
                "rejection_reason": "",
                "trace": {"strict_admission": False, "explicit_neutral": False, "decision_counts": {}},
            }
        cells.append(_hashed_cell(payload))
    return cells


def _hashed_cell(payload: dict[str, Any]) -> FinancialCandidateCell:
    unhashed = dict(payload)
    unhashed.pop("cell_business_hash", None)
    normalized = FinancialCandidateCell(**{**unhashed, "cell_business_hash": ""})
    business_hash = canonical_hash(normalized.model_dump(mode="json", exclude={"cell_business_hash"}))
    return normalized.model_copy(update={"cell_business_hash": business_hash})


def _derived_outputs(cells: Sequence[FinancialCandidateCell], eligible_evidence: list[dict[str, Any]]) -> dict[str, Any]:
    active_by_cell = {cell.canonical_cell_id: cell for cell in cells if cell.score_status in {ScoreStatus.ACTIVE, ScoreStatus.CARRY_FORWARD}}
    changes = []
    for cell in active_by_cell.values():
        canonical = next(row for row in canonical_cell_registry(include_not_applicable=True) if row.canonical_cell_id == cell.canonical_cell_id)
        material = is_material_shift(score_status=cell.score_status, score=cell.score, prior_score=canonical.baseline_score)
        changes.append(
            {
                "canonical_cell_id": cell.canonical_cell_id,
                "prior_score": canonical.baseline_score,
                "candidate_score": cell.score,
                "delta": cell.score - canonical.baseline_score if cell.score is not None and canonical.baseline_score is not None else None,
                "material_shift": material,
                "status": "MATERIAL_SHIFT" if material else "NO_MATERIAL_CHANGE",
            }
        )
    material = [row for row in changes if row["material_shift"]]
    if material:
        scenarios = [
            {
                "scenario_type": "BASE",
                "status": "CANDIDATE_ONLY",
                "affected_cells": [row["canonical_cell_id"] for row in material],
                "reason": "Explicit material shifts in admitted ACTIVE/CARRY_FORWARD cells.",
            }
        ]
        transmissions = [
            {
                "canonical_cell_id": row["canonical_cell_id"],
                "status": "CANDIDATE_ONLY",
                "reason": "Material score delta supported by admitted evidence lineage.",
            }
            for row in material
        ]
        signals = [
            {
                "canonical_cell_id": row["canonical_cell_id"],
                "signal": "SHIFT",
                "delta": row["delta"],
            }
            for row in material
        ]
        state = "AVAILABLE"
        reason = "Material admitted shifts exist."
    else:
        scenarios = []
        transmissions = []
        signals = []
        state = "INSUFFICIENT_DATA"
        reason = "No materially shifted ACTIVE/CARRY_FORWARD cell exists; no scenario, transmission path, or Shift Signal was generated."
    return {
        "mae_matrix": [cell.model_dump(mode="json") for cell in cells],
        "evidence": eligible_evidence,
        "what_changed": {"status": "NO_MATERIAL_CHANGE" if not material else "MATERIAL_SHIFT", "records": changes},
        "shift_signals": {"status": state, "reason": reason, "records": signals},
        "scenarios": {"status": state, "reason": reason, "records": scenarios},
        "transmission_paths": {"status": state, "reason": reason, "records": transmissions},
    }


def _source_role_counts(eligible_evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: Counter[tuple[str, str, str]] = Counter()
    for evidence in eligible_evidence:
        for source in evidence["sources"]:
            counts[(source["role"], source["tier"], evidence["evidence_mode"])] += 1
    return [
        {"source_role": role, "source_tier": tier, "evidence_mode": mode, "approved_source_count": count}
        for (role, tier, mode), count in sorted(counts.items())
    ]


def _reconciliation(
    cells: Sequence[FinancialCandidateCell],
    evidence: list[dict[str, Any]],
    source_counts: list[dict[str, Any]],
) -> dict[str, Any]:
    statuses = Counter(cell.score_status.value for cell in cells)
    numeric = sum(isinstance(cell.score, int) and not isinstance(cell.score, bool) for cell in cells)
    zeros = sum(cell.score == 0 for cell in cells)
    explicit_neutral_zeros = sum(cell.score == 0 and bool(cell.trace.get("explicit_neutral")) for cell in cells)
    return {
        "geometry_cells": len(cells),
        "geometry_rows": len({cell.template_row_key for cell in cells}),
        "geometry_regions": len({cell.region for cell in cells}),
        "applicable_cells": sum(cell.applicability == "APPLICABLE" for cell in cells),
        "not_applicable_cells": sum(cell.applicability == "NOT_APPLICABLE" for cell in cells),
        "status_counts": {status.value: statuses.get(status.value, 0) for status in ScoreStatus},
        "numeric_score_count": numeric,
        "null_score_count": len(cells) - numeric,
        "zero_score_count": zeros,
        "explicit_neutral_zero_count": explicit_neutral_zeros,
        "evidence_mode_counts": dict(sorted(Counter(item["evidence_mode"] for item in evidence).items())),
        "eligible_evidence_count": len(evidence),
        "approved_source_count": sum(row["approved_source_count"] for row in source_counts),
    }


def _validate_reconciliation(summary: dict[str, Any]) -> None:
    if (
        summary["geometry_cells"] != 114
        or summary["geometry_rows"] != 19
        or summary["geometry_regions"] != 6
        or summary["applicable_cells"] != 104
        or summary["not_applicable_cells"] != 10
    ):
        raise FinancialCandidateError(f"Canonical geometry reconciliation failed: {summary}")
    if sum(summary["status_counts"].values()) != 114:
        raise FinancialCandidateError("Score-status reconciliation failed.")
    if summary["numeric_score_count"] + summary["null_score_count"] != 114:
        raise FinancialCandidateError("Numeric/null reconciliation failed.")
    if summary["zero_score_count"] != summary["explicit_neutral_zero_count"]:
        raise FinancialCandidateError("Zero score exists without explicit admitted neutral evidence.")


def _stage_payloads(
    *,
    run_id: str,
    built_at: str,
    eligible_evidence: list[dict[str, Any]],
    cells: list[dict[str, Any]],
    derived: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    stage_data = {
        "stage_01_eligible_evidence.json": eligible_evidence,
        "stage_02_per_cell_gates.json": [
            {
                "canonical_cell_id": cell["canonical_cell_id"],
                "score_status": cell["score_status"],
                "gates": cell["trace"].get("gates", {}),
                "decision_counts": cell["trace"].get("decision_counts", {}),
            }
            for cell in cells
        ],
        "stage_03_score_decisions.json": cells,
        "stage_04_derived_inputs.json": {
            "what_changed": derived["what_changed"],
            "shift_signals": derived["shift_signals"],
            "scenarios": derived["scenarios"],
            "transmission_paths": derived["transmission_paths"],
        },
    }
    return {
        name: {
            "schema_version": CANDIDATE_SCHEMA_VERSION,
            "stage": name.removesuffix(".json"),
            "run_id": run_id,
            "metadata": {"built_at": built_at},
            "content_hash": canonical_hash(data),
            "data": data,
        }
        for name, data in stage_data.items()
    }


def _output_files(
    report: dict[str, Any],
    cells: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    source_counts: list[dict[str, Any]],
    derived: dict[str, Any],
    stages: dict[str, dict[str, Any]],
) -> dict[str, bytes]:
    cell_fields = [
        "canonical_cell_id",
        "template_row_key",
        "region",
        "applicability",
        "score_status",
        "score",
        "direction",
        "limitation_reason",
        "rejection_reason",
        "evidence_mode",
        "confidence",
        "approved_source_count",
        "review_status",
        "cell_business_hash",
    ]
    flattened = []
    for cell in cells:
        trace = cell["trace"]
        flattened.append(
            {
                **{name: cell.get(name) for name in cell_fields},
                "evidence_mode": trace.get("evidence_mode", ""),
                "confidence": trace.get("confidence", ""),
                "approved_source_count": trace.get("approved_source_count", 0),
                "review_status": "NOT_REVIEWED",
            }
        )
    unscored = [row for row in flattened if row["score"] is None]
    files = {
        "financial_validation_report.json": _json_bytes(report),
        "eligible_evidence.json": _json_bytes({"status": "AVAILABLE", "records": evidence}),
        "mae_matrix.json": _json_bytes({"status": "CANDIDATE_ONLY", "records": cells}),
        "shift_signals.json": _json_bytes(derived["shift_signals"]),
        "what_changed.json": _json_bytes(derived["what_changed"]),
        "scenarios.json": _json_bytes(derived["scenarios"]),
        "transmission_paths.json": _json_bytes(derived["transmission_paths"]),
        "cell_financial_audit.csv": _csv_bytes(flattened, cell_fields),
        "unscored_cells.csv": _csv_bytes(unscored, cell_fields),
        "source_role_counts.csv": _csv_bytes(
            source_counts,
            ("source_role", "source_tier", "evidence_mode", "approved_source_count"),
        ),
    }
    files.update({name: _json_bytes(payload) for name, payload in stages.items()})
    return files


def _read_governance_state(database_path: Path, baseline: dict[str, Any]) -> dict[str, Any]:
    pointer_id = baseline["database"]["current_pointer"]["id"]
    uri = f"file:{database_path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        row = connection.execute(
            """
            SELECT technical_status, financial_status, analyst_review_status, release_status,
                   reviewer_actor_type, reviewer_identity, baseline_manifest_hash
            FROM financial_release_records
            WHERE subject_id = ?
            """,
            (pointer_id,),
        ).fetchone()
        decisions = connection.execute("SELECT COUNT(*) FROM financial_review_decisions").fetchone()[0]
    if row is None:
        raise FinancialCandidateError("Financial governance record for production pointer is missing.")
    return {**dict(row), "human_review_decision_count": int(decisions)}


def _build_workbook(*, root: Path, candidate_root: Path, node_executable: str | None) -> None:
    node = node_executable or shutil.which("node")
    if not node:
        raise FinancialCandidateError("Bundled Node runtime is unavailable; workbook build cannot proceed.")
    builder = root / "scripts" / "build_financial_candidate_workbook.mjs"
    if not builder.is_file():
        raise FinancialCandidateError(f"Workbook builder is missing: {builder}")
    command = [
        node,
        str(builder),
        "--input-dir",
        str(candidate_root),
        "--output",
        str(candidate_root / "MAE_financial_candidate.xlsx"),
        "--preview-dir",
        str(candidate_root / "workbook_previews"),
        "--qa-output",
        str(candidate_root / "workbook_qa.json"),
    ]
    completed = subprocess.run(command, cwd=root, text=True, capture_output=True, check=False)
    if completed.stdout:
        print(completed.stdout.rstrip())
    if completed.returncode != 0:
        raise FinancialCandidateError(
            f"Workbook builder exited {completed.returncode}: {completed.stderr.strip()}"
        )


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FinancialCandidateError(f"Cannot load JSON artifact: {path}") from exc
    if not isinstance(value, dict):
        raise FinancialCandidateError(f"Expected JSON object: {path}")
    return value


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _csv_bytes(rows: Sequence[dict[str, Any]], fields: Sequence[str]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(fields), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: "" if row.get(field) is None else row.get(field, "") for field in fields})
    return buffer.getvalue().encode("utf-8")


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
