from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Sequence

from app.services.audit_baseline import BaselineError, canonical_hash, sha256_file, verify_audit_baseline


REVIEW_VERSION = "financial_mapping_second_review_v2.0"
EXPECTED_REVIEWED_DISTRIBUTION = {
    "INSUFFICIENT_EVIDENCE": 4,
    "IRRELEVANT_DOCUMENT": 6,
    "TRUE_PASS": 1,
    "UNSUPPORTED_DIRECTION": 8,
    "WRONG_ASSET_CLASS": 6,
    "WRONG_REGION": 11,
    "WRONG_SEGMENT": 10,
}
EXPECTED_INVALID_DISTRIBUTION = {"MANUAL_REVIEW_REQUIRED": 220, "REJECT": 56}
CANDIDATE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,119}$")
RESERVED_CANDIDATE_IDS = {"current", "latest", "production"}


class StrictReviewApplicationError(RuntimeError):
    """Raised before candidate writes when signed review inputs fail validation."""


@dataclass(frozen=True)
class StrictReviewInputs:
    report_json: Path
    reviewed_csv: Path
    invalid_actions_csv: Path
    baseline_manifest: Path


@dataclass(frozen=True)
class StrictReviewApplicationResult:
    candidate_id: str
    snapshot_date: str
    candidate_root: Path
    dry_run: bool
    summary: dict[str, Any]
    input_hashes: dict[str, str]
    business_content_hash: str
    output_hashes: dict[str, str]


def default_inputs(root: Path, snapshot_date: date) -> StrictReviewInputs:
    cleanup = root / "outputs" / "production_cleanup"
    return StrictReviewInputs(
        report_json=cleanup / "financial_mapping_second_review_report.json",
        reviewed_csv=cleanup / "verified_pass_second_review.csv",
        invalid_actions_csv=cleanup / "invalid_mapping_second_review_actions.csv",
        baseline_manifest=(
            root
            / "outputs"
            / "audit_baseline"
            / snapshot_date.isoformat()
            / f"AUDIT_BASELINE_{snapshot_date.isoformat()}.json"
        ),
    )


def apply_financial_second_review(
    *,
    root: Path,
    snapshot_date: date,
    candidate_id: str,
    inputs: StrictReviewInputs | None = None,
    output_root: Path | None = None,
    dry_run: bool = False,
    enforce_production_counts: bool = True,
    allow_demo_records: bool = False,
) -> StrictReviewApplicationResult:
    root = root.resolve()
    _validate_candidate_id(candidate_id)
    inputs = inputs or default_inputs(root, snapshot_date)
    destination = _candidate_destination(root, candidate_id, output_root)
    manifest = _load_json(inputs.baseline_manifest)
    baseline_verification = verify_audit_baseline(
        root=root,
        manifest_path=inputs.baseline_manifest,
        strict_database_binary=False,
    )
    if not baseline_verification["ok"]:
        raise StrictReviewApplicationError(
            "Immutable baseline verification failed: " + "; ".join(baseline_verification["mismatches"])
        )
    if str(manifest.get("snapshot_date")) != snapshot_date.isoformat():
        raise StrictReviewApplicationError("Baseline snapshot date does not match the requested candidate date.")
    input_hashes = _validate_input_hashes(root, inputs, manifest)
    report = _load_json(inputs.report_json)
    reviewed_csv = _read_csv(inputs.reviewed_csv)
    invalid_csv = _read_csv(inputs.invalid_actions_csv)
    _validate_report_schema(report)
    reviewed_rows, invalid_rows = _validate_cross_file_rows(report, reviewed_csv, invalid_csv)

    reviewed_distribution = dict(sorted(Counter(row["second_review_status"] for row in reviewed_rows).items()))
    invalid_distribution = dict(sorted(Counter(row["revised_action_group"] for row in invalid_rows).items()))
    if enforce_production_counts:
        if reviewed_distribution != EXPECTED_REVIEWED_DISTRIBUTION:
            raise StrictReviewApplicationError(
                f"Reviewed decision distribution mismatch: {reviewed_distribution}"
            )
        if invalid_distribution != EXPECTED_INVALID_DISTRIBUTION:
            raise StrictReviewApplicationError(
                f"Invalid-action distribution mismatch: {invalid_distribution}"
            )

    baseline_database = inputs.baseline_manifest.parent / "immutable" / str(manifest["database"]["relative_path"])
    baseline_rows = _baseline_research_views(baseline_database)
    decisions = _build_decisions(
        reviewed_rows,
        invalid_rows,
        baseline_rows,
        input_hashes=input_hashes,
        review_timestamp=str(report.get("generated_at") or ""),
        allow_demo_records=allow_demo_records,
    )
    if len({row["decision_id"] for row in decisions}) != len(decisions):
        raise StrictReviewApplicationError("Decision identifiers are not unique.")
    summary = {
        "reviewed_count": len(reviewed_rows),
        "true_pass_count": reviewed_distribution.get("TRUE_PASS", 0),
        "downgraded_count": len(reviewed_rows) - reviewed_distribution.get("TRUE_PASS", 0),
        "reviewed_distribution": reviewed_distribution,
        "invalid_action_count": len(invalid_rows),
        "invalid_action_distribution": invalid_distribution,
        "manual_review_count": invalid_distribution.get("MANUAL_REVIEW_REQUIRED", 0),
        "reject_count": invalid_distribution.get("REJECT", 0),
        "active_financial_pass_count": sum(row["financially_eligible"] for row in decisions),
        "auto_remap_count": sum(row["auto_remap_applied"] for row in decisions),
        "decision_count": len(decisions),
    }
    payload = {
        "schema_version": "1.0",
        "candidate_id": candidate_id,
        "snapshot_date": snapshot_date.isoformat(),
        "review_version": REVIEW_VERSION,
        "review_timestamp": str(report.get("generated_at") or ""),
        "baseline_aggregate_sha256": manifest["aggregate_sha256"],
        "input_hashes": input_hashes,
        "candidate_governance": {
            "technical_status": "PASSED",
            "financial_status": "MANUAL_REVIEW_REQUIRED",
            "analyst_review_status": "NOT_REVIEWED",
            "release_status": "QUARANTINED",
        },
        "summary": summary,
        "decisions": decisions,
    }
    business_content_hash = canonical_hash(payload)
    payload["business_content_hash"] = business_content_hash
    files = _candidate_files(payload)
    output_hashes = {name: _sha256_bytes(content) for name, content in files.items()}
    manifest_payload = {
        "schema_version": "1.0",
        "candidate_id": candidate_id,
        "snapshot_date": snapshot_date.isoformat(),
        "business_content_hash": business_content_hash,
        "baseline_aggregate_sha256": manifest["aggregate_sha256"],
        "input_hashes": input_hashes,
        "output_hashes": output_hashes,
        "governance": payload["candidate_governance"],
    }
    manifest_bytes = _json_bytes(manifest_payload)
    files["strict_review_application_manifest.json"] = manifest_bytes
    output_hashes["strict_review_application_manifest.json"] = _sha256_bytes(manifest_bytes)
    if not dry_run:
        _write_candidate_files(destination, files)
    return StrictReviewApplicationResult(
        candidate_id=candidate_id,
        snapshot_date=snapshot_date.isoformat(),
        candidate_root=destination,
        dry_run=dry_run,
        summary=summary,
        input_hashes=input_hashes,
        business_content_hash=business_content_hash,
        output_hashes=dict(sorted(output_hashes.items())),
    )


def is_decision_financially_eligible(decision: dict[str, Any]) -> bool:
    return bool(
        decision.get("decision_source") == "PREVIOUSLY_VERIFIED_SECOND_REVIEW"
        and decision.get("new_status") == "TRUE_PASS"
        and decision.get("financially_eligible") is True
        and decision.get("derived_output_eligible") is True
        and decision.get("auto_remap_applied") is False
    )


def _validate_candidate_id(candidate_id: str) -> None:
    if not CANDIDATE_ID_PATTERN.fullmatch(candidate_id):
        raise StrictReviewApplicationError("Candidate ID contains a path or unsupported characters.")
    if candidate_id.casefold() in RESERVED_CANDIDATE_IDS:
        raise StrictReviewApplicationError("Reserved production/current candidate ID is forbidden.")


def _candidate_destination(root: Path, candidate_id: str, output_root: Path | None) -> Path:
    base = (root / "outputs" / "candidates").resolve()
    destination = (output_root or base / candidate_id).resolve()
    try:
        destination.relative_to(base)
    except ValueError as exc:
        raise StrictReviewApplicationError("Candidate outputs must remain under outputs/candidates.") from exc
    if destination == base:
        raise StrictReviewApplicationError("Candidate output must use an isolated candidate directory.")
    return destination


def _validate_input_hashes(
    root: Path,
    inputs: StrictReviewInputs,
    manifest: dict[str, Any],
) -> dict[str, str]:
    expected = {str(row["relative_path"]): str(row["sha256"]) for row in manifest["files"]}
    canonical = {
        "report_json": "outputs/production_cleanup/financial_mapping_second_review_report.json",
        "reviewed_csv": "outputs/production_cleanup/verified_pass_second_review.csv",
        "invalid_actions_csv": "outputs/production_cleanup/invalid_mapping_second_review_actions.csv",
    }
    paths = {
        "report_json": inputs.report_json,
        "reviewed_csv": inputs.reviewed_csv,
        "invalid_actions_csv": inputs.invalid_actions_csv,
    }
    hashes: dict[str, str] = {}
    for key, path in paths.items():
        if not path.is_file():
            raise StrictReviewApplicationError(f"Strict review input is missing: {path}")
        actual = sha256_file(path)
        expected_hash = expected.get(canonical[key])
        if not expected_hash:
            raise StrictReviewApplicationError(f"Baseline manifest does not pin {canonical[key]}")
        if actual != expected_hash:
            raise StrictReviewApplicationError(f"Strict review input hash mismatch: {key}")
        hashes[key] = actual
    try:
        inputs.baseline_manifest.resolve().relative_to(root)
    except ValueError:
        pass
    hashes["baseline_manifest"] = sha256_file(inputs.baseline_manifest)
    return dict(sorted(hashes.items()))


def _validate_report_schema(report: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "generated_at",
        "previous_verified_pass_count",
        "true_pass_count",
        "downgraded_count",
        "previously_verified_second_review",
        "invalid_mapping_second_review_count",
        "invalid_mapping_second_review_actions",
    }
    missing = sorted(required - set(report))
    if missing:
        raise StrictReviewApplicationError(f"Second-review JSON schema is missing: {', '.join(missing)}")
    if str(report["schema_version"]) != "2.0":
        raise StrictReviewApplicationError("Unsupported second-review schema version.")


def _validate_cross_file_rows(
    report: dict[str, Any],
    reviewed_csv: list[dict[str, str]],
    invalid_csv: list[dict[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    reviewed_json = list(report["previously_verified_second_review"])
    invalid_json = list(report["invalid_mapping_second_review_actions"])
    _require_fields(reviewed_csv, {"research_view_id", "previous_status", "second_review_status", "primary_failure_reason"})
    _require_fields(invalid_csv, {"research_view_id", "legacy_action_group", "revised_action_group", "strict_action_reason"})
    _unique_ids(reviewed_csv, "reviewed CSV")
    _unique_ids(invalid_csv, "invalid-actions CSV")
    reviewed_by_id = _index_rows(reviewed_json, "reviewed JSON")
    invalid_by_id = _index_rows(invalid_json, "invalid-actions JSON")
    if set(reviewed_by_id) != {row["research_view_id"] for row in reviewed_csv}:
        raise StrictReviewApplicationError("Reviewed JSON/CSV identifiers do not match.")
    if set(invalid_by_id) != {row["research_view_id"] for row in invalid_csv}:
        raise StrictReviewApplicationError("Invalid-action JSON/CSV identifiers do not match.")
    if set(reviewed_by_id) & set(invalid_by_id):
        raise StrictReviewApplicationError("Reviewed and invalid-action identifiers overlap.")
    for row in reviewed_csv:
        source = reviewed_by_id[row["research_view_id"]]
        for field in ("previous_status", "second_review_status", "canonical_cell_id"):
            if str(source.get(field) or "") != str(row.get(field) or ""):
                raise StrictReviewApplicationError(f"Reviewed JSON/CSV mismatch for {row['research_view_id']}: {field}")
    for row in invalid_csv:
        source = invalid_by_id[row["research_view_id"]]
        for field in ("legacy_action_group", "revised_action_group"):
            if str(source.get(field) or "") != str(row.get(field) or ""):
                raise StrictReviewApplicationError(f"Invalid JSON/CSV mismatch for {row['research_view_id']}: {field}")
    if int(report["previous_verified_pass_count"]) != len(reviewed_csv):
        raise StrictReviewApplicationError("Reviewed count does not match JSON summary.")
    if int(report["invalid_mapping_second_review_count"]) != len(invalid_csv):
        raise StrictReviewApplicationError("Invalid-action count does not match JSON summary.")
    return [reviewed_by_id[row["research_view_id"]] for row in reviewed_csv], [
        invalid_by_id[row["research_view_id"]] for row in invalid_csv
    ]


def _baseline_research_views(database: Path) -> dict[str, dict[str, Any]]:
    if not database.is_file():
        raise BaselineError(f"Immutable baseline database is missing: {database}")
    uri = f"file:{database.resolve()}?mode=ro"
    query = """
        SELECT rv.id, rv.article_id, rv.canonical_cell_id, rv.template_row_key, rv.region,
               rv.asset_class, rv.asset_segment, rv.direction, rv.position_score,
               rv.review_status, rv.strict_review_status, rv.is_demo,
               COALESCE(a.content_hash, '') AS article_content_hash,
               COALESCE(a.source_reference, '') AS source_reference
        FROM research_views rv
        LEFT JOIN articles a ON a.id = rv.article_id
        ORDER BY rv.id
    """
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        rows = [dict(row) for row in connection.execute(query).fetchall()]
    return {str(row["id"]): row for row in rows}


def _build_decisions(
    reviewed_rows: Sequence[dict[str, Any]],
    invalid_rows: Sequence[dict[str, Any]],
    baseline_rows: dict[str, dict[str, Any]],
    *,
    input_hashes: dict[str, str],
    review_timestamp: str,
    allow_demo_records: bool,
) -> list[dict[str, Any]]:
    decisions: list[dict[str, Any]] = []
    for row in reviewed_rows:
        baseline = _match_baseline(row, baseline_rows, allow_demo_records=allow_demo_records)
        status = str(row["second_review_status"])
        decision = {
            "research_view_id": str(row["research_view_id"]),
            "decision_source": "PREVIOUSLY_VERIFIED_SECOND_REVIEW",
            "old_status": str(row.get("previous_status") or ""),
            "new_status": status,
            "reason": str(row.get("primary_failure_reason") or ""),
            "canonical_cell_id": str(row.get("canonical_cell_id") or ""),
            "financially_eligible": status == "TRUE_PASS",
            "derived_output_eligible": status == "TRUE_PASS",
            "auto_remap_applied": False,
            "review_version": REVIEW_VERSION,
            "review_timestamp": review_timestamp,
            "input_content_hash": input_hashes["reviewed_csv"],
            "baseline_record_hash": canonical_hash(baseline),
        }
        decision["decision_id"] = canonical_hash(decision)
        decisions.append(decision)
    for row in invalid_rows:
        baseline = _match_baseline(row, baseline_rows, allow_demo_records=allow_demo_records)
        status = str(row["revised_action_group"])
        decision = {
            "research_view_id": str(row["research_view_id"]),
            "decision_source": "INVALID_MAPPING_SECOND_REVIEW",
            "old_status": str(row.get("legacy_action_group") or row.get("action_group") or "INVALID_MAPPING"),
            "new_status": status,
            "reason": str(row.get("strict_action_reason") or row.get("financial_explanation") or ""),
            "canonical_cell_id": str(baseline.get("canonical_cell_id") or ""),
            "financially_eligible": False,
            "derived_output_eligible": False,
            "auto_remap_applied": False,
            "review_version": REVIEW_VERSION,
            "review_timestamp": review_timestamp,
            "input_content_hash": input_hashes["invalid_actions_csv"],
            "baseline_record_hash": canonical_hash(baseline),
        }
        decision["decision_id"] = canonical_hash(decision)
        decisions.append(decision)
    return sorted(decisions, key=lambda item: (item["decision_source"], item["research_view_id"]))


def _match_baseline(
    row: dict[str, Any],
    baseline_rows: dict[str, dict[str, Any]],
    *,
    allow_demo_records: bool,
) -> dict[str, Any]:
    identifier = str(row.get("research_view_id") or "")
    baseline = baseline_rows.get(identifier)
    if baseline is None:
        raise StrictReviewApplicationError(f"Review identifier is absent from immutable baseline: {identifier}")
    if bool(baseline.get("is_demo")) and not allow_demo_records:
        raise StrictReviewApplicationError(f"DEMO research view is forbidden in the real candidate: {identifier}")
    report_cell = str(row.get("canonical_cell_id") or "")
    if report_cell and report_cell != str(baseline.get("canonical_cell_id") or ""):
        raise StrictReviewApplicationError(f"Baseline cell mismatch for research view {identifier}")
    return baseline


def _candidate_files(payload: dict[str, Any]) -> dict[str, bytes]:
    decisions = payload["decisions"]
    fields = [
        "decision_id",
        "research_view_id",
        "decision_source",
        "old_status",
        "new_status",
        "reason",
        "canonical_cell_id",
        "financially_eligible",
        "derived_output_eligible",
        "auto_remap_applied",
        "review_version",
        "review_timestamp",
        "input_content_hash",
        "baseline_record_hash",
    ]
    reviewed = [row for row in decisions if row["decision_source"] == "PREVIOUSLY_VERIFIED_SECOND_REVIEW"]
    invalid = [row for row in decisions if row["decision_source"] == "INVALID_MAPPING_SECOND_REVIEW"]
    return {
        "financial_review_before_after.json": _json_bytes(payload),
        "manual_review_queue.csv": _csv_bytes(
            [row for row in invalid if row["new_status"] == "MANUAL_REVIEW_REQUIRED"], fields
        ),
        "rejected_mappings.csv": _csv_bytes([row for row in invalid if row["new_status"] == "REJECT"], fields),
        "downgraded_mappings.csv": _csv_bytes(
            [row for row in reviewed if row["new_status"] != "TRUE_PASS"], fields
        ),
        "active_true_pass.csv": _csv_bytes(
            [row for row in reviewed if row["new_status"] == "TRUE_PASS"], fields
        ),
    }


def _write_candidate_files(destination: Path, files: dict[str, bytes]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        for name, content in files.items():
            path = destination / name
            if not path.is_file() or path.read_bytes() != content:
                raise StrictReviewApplicationError(
                    f"Candidate output already exists with different content: {path}"
                )
        return
    stage = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=str(destination.parent)))
    try:
        for name, content in files.items():
            (stage / name).write_bytes(content)
        os.rename(stage, destination)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StrictReviewApplicationError(f"Cannot read strict-review JSON: {path}") from exc
    if not isinstance(value, dict):
        raise StrictReviewApplicationError(f"Expected a JSON object: {path}")
    return value


def _require_fields(rows: Sequence[dict[str, str]], fields: set[str]) -> None:
    if not rows:
        raise StrictReviewApplicationError("Strict-review CSV has no rows.")
    missing = sorted(fields - set(rows[0]))
    if missing:
        raise StrictReviewApplicationError(f"Strict-review CSV is missing: {', '.join(missing)}")


def _unique_ids(rows: Sequence[dict[str, Any]], label: str) -> None:
    identifiers = [str(row.get("research_view_id") or "") for row in rows]
    if not all(identifiers) or len(set(identifiers)) != len(identifiers):
        raise StrictReviewApplicationError(f"Missing or duplicate research_view_id in {label}.")


def _index_rows(rows: Sequence[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
    _unique_ids(rows, label)
    return {str(row["research_view_id"]): dict(row) for row in rows}


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _csv_bytes(rows: Sequence[dict[str, Any]], fields: Sequence[str]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(fields), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: row.get(field, "") for field in fields})
    return buffer.getvalue().encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
