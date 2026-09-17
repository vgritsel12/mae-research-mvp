from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


MANIFEST_VERSION = "mae_audit_baseline_v1"
FULL_PRODUCT_VERSION = "excel_first_full_quality_v3"
RUNTIME_DERIVED_TABLES = {
    "change_logs",
    "llm_call_logs",
    "matrix_scores",
    "scenario_assessments",
    "scenario_cards",
}


class BaselineError(RuntimeError):
    """Base class for audit-baseline failures."""


class BaselineMismatchError(BaselineError):
    """Raised when frozen evidence no longer matches its production source."""


@dataclass(frozen=True)
class ProductionFileSpec:
    relative_path: str
    role: str
    required: bool = True


def default_production_files(snapshot_date: date) -> tuple[ProductionFileSpec, ...]:
    stamp = snapshot_date.isoformat()
    full = f"outputs/snapshots/{stamp}/full"
    analogs = f"outputs/snapshots/{stamp}/historical_analogs"
    specs = [
        ProductionFileSpec(f"{full}/mae_full_scores_{stamp}.csv", "dated_full_scores"),
        ProductionFileSpec(f"{full}/mae_full_evidence_{stamp}.csv", "dated_full_evidence"),
        ProductionFileSpec(f"{full}/mae_full_scenarios_{stamp}.csv", "dated_full_scenarios"),
        ProductionFileSpec(f"{full}/mae_full_transmission_{stamp}.csv", "dated_full_transmission"),
        ProductionFileSpec(f"{full}/mae_full_report_{stamp}.md", "dated_full_report"),
        ProductionFileSpec(f"{full}/mae_full_quality_report_{stamp}.json", "dated_full_quality"),
        ProductionFileSpec(f"{full}/mae_full_quality_report_{stamp}.md", "dated_full_quality"),
        ProductionFileSpec(f"{full}/mae_full_validation_{stamp}.json", "dated_full_validation"),
        ProductionFileSpec(f"{full}/mae_full_snapshot_{stamp}.xlsx", "dated_full_workbook"),
        ProductionFileSpec(f"{analogs}/mae_historical_analogs_{stamp}.csv", "dated_historical_analogs"),
        ProductionFileSpec(f"{analogs}/mae_historical_analogs_{stamp}.json", "dated_historical_analogs"),
        ProductionFileSpec("outputs/mae_full_latest_scores.csv", "current_full_alias"),
        ProductionFileSpec("outputs/mae_full_latest_evidence.csv", "current_full_alias"),
        ProductionFileSpec("outputs/mae_full_latest_scenarios.csv", "current_full_alias"),
        ProductionFileSpec("outputs/mae_full_latest_transmission.csv", "current_full_alias"),
        ProductionFileSpec("outputs/mae_full_latest_report.md", "current_full_alias"),
        ProductionFileSpec("outputs/mae_full_latest_quality_report.json", "current_full_alias"),
        ProductionFileSpec("outputs/mae_full_latest_quality_report.md", "current_full_alias"),
        ProductionFileSpec("outputs/mae_full_latest.xlsx", "current_full_alias"),
        ProductionFileSpec("outputs/mae_historical_analogs_latest.csv", "current_analogs_alias"),
        ProductionFileSpec("outputs/mae_historical_analogs_latest.json", "current_analogs_alias"),
        ProductionFileSpec("outputs/mae_latest_scores.csv", "accepted_pilot_input"),
        ProductionFileSpec("outputs/mae_latest_evidence.csv", "accepted_pilot_input"),
        ProductionFileSpec("outputs/mae_latest_scenarios.csv", "accepted_pilot_input"),
        ProductionFileSpec("outputs/mae_latest_report.md", "accepted_pilot_input"),
        ProductionFileSpec("outputs/mae_latest.xlsx", "accepted_pilot_input"),
        ProductionFileSpec("data/MAE_Shift_Signal_Framework_FINAL_SRS_v1.0.docx", "authoritative_srs"),
        ProductionFileSpec("data/MAE_template.csv", "matrix_template"),
        ProductionFileSpec("data/MAE_template_filled.xlsx", "matrix_template"),
        ProductionFileSpec("data/applicability_v1.csv", "methodology_input"),
        ProductionFileSpec("data/exposure_v1.csv", "methodology_input"),
        ProductionFileSpec("data/approved_sources_v1.csv", "source_governance_input"),
        ProductionFileSpec("data/research_lifecycle_config_v1.json", "source_governance_input"),
        ProductionFileSpec("data/historical_market_series.csv", "historical_analogs_input"),
        ProductionFileSpec("data/historical_market_series_sources.json", "historical_analogs_input"),
        ProductionFileSpec("outputs/production_cleanup/financial_mapping_second_review_report.json", "financial_review_input"),
        ProductionFileSpec("outputs/production_cleanup/invalid_mapping_second_review_actions.csv", "financial_review_input"),
        ProductionFileSpec("outputs/production_cleanup/verified_pass_second_review.csv", "financial_review_input"),
    ]
    return tuple(specs)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _normalize_sql_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"hex": value.hex()}
    if isinstance(value, float):
        return format(value, ".17g")
    return value


def _table_fingerprint(
    connection: sqlite3.Connection,
    table: str,
    *,
    columns: Sequence[str] | None = None,
    primary_key: Sequence[str] | None = None,
) -> dict[str, Any]:
    info = connection.execute(f"PRAGMA table_info({_quote_identifier(table)})").fetchall()
    available = [str(row[1]) for row in info]
    if not available:
        raise BaselineMismatchError(f"Database table is missing: {table}")
    selected = list(columns or available)
    missing = sorted(set(selected) - set(available))
    if missing:
        raise BaselineMismatchError(f"Database columns are missing from {table}: {', '.join(missing)}")
    keys = list(primary_key or [str(row[1]) for row in sorted(info, key=lambda item: int(item[5])) if int(row[5])])
    order = keys or selected
    select_sql = ", ".join(_quote_identifier(name) for name in selected)
    order_sql = ", ".join(_quote_identifier(name) for name in order)
    rows = connection.execute(
        f"SELECT {select_sql} FROM {_quote_identifier(table)} ORDER BY {order_sql}"
    ).fetchall()
    digest = hashlib.sha256()
    for row in rows:
        normalized = [_normalize_sql_value(value) for value in row]
        digest.update(
            json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
        )
        digest.update(b"\n")
    return {
        "table": table,
        "columns": selected,
        "primary_key": keys,
        "row_count": len(rows),
        "content_sha256": digest.hexdigest(),
    }


def database_fingerprint(
    database_path: Path,
    *,
    baseline_tables: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not database_path.is_file():
        raise BaselineError(f"SQLite database does not exist: {database_path}")
    uri = f"file:{database_path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.execute("PRAGMA query_only = ON")
        if baseline_tables is None:
            table_names = [
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                ).fetchall()
            ]
            tables = [_table_fingerprint(connection, table) for table in table_names]
        else:
            tables = [
                _table_fingerprint(
                    connection,
                    str(item["table"]),
                    columns=[str(value) for value in item["columns"]],
                    primary_key=[str(value) for value in item.get("primary_key", [])],
                )
                for item in baseline_tables
            ]
        pointer = _current_full_pointer(connection)
    business_payload = {
        "tables": tables,
        "current_pointer": pointer,
    }
    return {
        "relative_path": "data/mae.db",
        "byte_size": database_path.stat().st_size,
        "file_sha256_at_freeze": sha256_file(database_path),
        "tables": tables,
        "current_pointer": pointer,
        "business_sha256": canonical_hash(business_payload),
    }


def _current_full_pointer(connection: sqlite3.Connection) -> dict[str, Any]:
    try:
        rows = connection.execute(
            """
            SELECT id, snapshot_date, status, run_metadata, immutable, is_demo
            FROM mae_snapshots
            WHERE is_demo = 0
            ORDER BY snapshot_date DESC, created_at DESC, id DESC
            """
        ).fetchall()
    except sqlite3.OperationalError as exc:
        raise BaselineError("mae_snapshots is required to identify the current production snapshot") from exc
    for row in rows:
        raw_metadata = row[3]
        try:
            metadata = json.loads(raw_metadata) if isinstance(raw_metadata, str) else dict(raw_metadata or {})
        except (TypeError, json.JSONDecodeError):
            continue
        if metadata.get("full_product_version") != FULL_PRODUCT_VERSION:
            continue
        pointer = {
            "id": str(row[0]),
            "snapshot_date": str(row[1]),
            "status": str(row[2]),
            "run_metadata_sha256": canonical_hash(metadata),
            "immutable": bool(row[4]),
            "is_demo": bool(row[5]),
        }
        pointer["pointer_sha256"] = canonical_hash(pointer)
        return pointer
    raise BaselineError("No non-DEMO full production snapshot pointer was found")


def _file_entries(root: Path, specs: Iterable[ProductionFileSpec]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for spec in specs:
        source = root / spec.relative_path
        if not source.is_file():
            if spec.required:
                raise BaselineError(f"Required production dependency is missing: {spec.relative_path}")
            continue
        entries.append(
            {
                "relative_path": spec.relative_path,
                "role": spec.role,
                "byte_size": source.stat().st_size,
                "sha256": sha256_file(source),
                "immutable_path": f"immutable/{spec.relative_path}",
            }
        )
    return sorted(entries, key=lambda item: item["relative_path"])


def _integrity_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "manifest_version": manifest["manifest_version"],
        "snapshot_date": manifest["snapshot_date"],
        "governance": manifest["governance"],
        "files": manifest["files"],
        "database": manifest["database"],
    }


def freeze_audit_baseline(
    *,
    root: Path,
    snapshot_date: date,
    output_root: Path | None = None,
    file_specs: Sequence[ProductionFileSpec] | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    destination = (output_root or root / "outputs" / "audit_baseline" / snapshot_date.isoformat()).resolve()
    manifest_path = destination / f"AUDIT_BASELINE_{snapshot_date.isoformat()}.json"
    specs = tuple(file_specs or default_production_files(snapshot_date))
    if manifest_path.exists():
        result = verify_audit_baseline(
            root=root,
            manifest_path=manifest_path,
            strict_database_binary=True,
        )
        if not result["ok"]:
            raise BaselineMismatchError("; ".join(result["mismatches"]))
        return json.loads(manifest_path.read_text(encoding="utf-8"))

    files = _file_entries(root, specs)
    database_path = root / "data" / "mae.db"
    database = database_fingerprint(database_path)
    if database["current_pointer"]["snapshot_date"] != snapshot_date.isoformat():
        raise BaselineError(
            "Current full production pointer date does not match requested baseline: "
            f"{database['current_pointer']['snapshot_date']} != {snapshot_date.isoformat()}"
        )
    manifest: dict[str, Any] = {
        "manifest_version": MANIFEST_VERSION,
        "snapshot_date": snapshot_date.isoformat(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "governance": {
            "technical_status": "TECHNICALLY_VALIDATED",
            "financial_status": "NOT_FINANCIALLY_APPROVED",
            "analyst_review_status": "NOT_REVIEWED",
            "release_status": "QUARANTINED",
        },
        "files": files,
        "database": database,
    }
    manifest["aggregate_sha256"] = canonical_hash(_integrity_payload(manifest))

    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=str(destination.parent)))
    try:
        for entry in files:
            source = root / entry["relative_path"]
            target = stage / entry["immutable_path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if sha256_file(target) != entry["sha256"]:
                raise BaselineMismatchError(f"Immutable copy verification failed: {entry['relative_path']}")
            target.chmod(0o444)
        database_copy = stage / "immutable" / "data" / "mae.db"
        database_copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(database_path, database_copy)
        if sha256_file(database_copy) != database["file_sha256_at_freeze"]:
            raise BaselineMismatchError("Immutable SQLite copy verification failed")
        database_copy.chmod(0o444)

        notice = _quarantine_notice(manifest)
        (stage / "QUARANTINE_NOTICE.md").write_text(notice, encoding="utf-8")
        (stage / manifest_path.name).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        try:
            os.rename(stage, destination)
        except FileExistsError as exc:
            raise BaselineError(f"Baseline destination was claimed concurrently: {destination}") from exc
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return manifest


def _quarantine_notice(manifest: dict[str, Any]) -> str:
    status = manifest["governance"]
    return "\n".join(
        [
            "# MAE 2026-07-13 — Quarantine Notice",
            "",
            "This is an immutable audit baseline, not a financially approved release.",
            "",
            f"- Technical status: `{status['technical_status']}`",
            f"- Financial status: `{status['financial_status']}`",
            f"- Analyst review status: `{status['analyst_review_status']}`",
            f"- Release status: `{status['release_status']}`",
            f"- Baseline aggregate SHA-256: `{manifest['aggregate_sha256']}`",
            "",
            "Automation must not change these statuses or create analyst approval.",
            "",
        ]
    )


def verify_audit_baseline(
    *,
    root: Path,
    manifest_path: Path,
    strict_database_binary: bool = False,
) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_aggregate = str(manifest.get("aggregate_sha256") or "")
    actual_aggregate = canonical_hash(_integrity_payload(manifest))
    mismatches: list[str] = []
    if actual_aggregate != expected_aggregate:
        mismatches.append("baseline manifest aggregate SHA-256 mismatch")

    baseline_root = manifest_path.parent
    for entry in manifest["files"]:
        relative = str(entry["relative_path"])
        source = root / relative
        immutable = baseline_root / str(entry["immutable_path"])
        if not source.is_file():
            mismatches.append(f"production file missing: {relative}")
            continue
        source_hash = sha256_file(source)
        if source_hash != entry["sha256"] or source.stat().st_size != entry["byte_size"]:
            mismatches.append(f"production file mismatch: {relative}")
        if not immutable.is_file() or sha256_file(immutable) != entry["sha256"]:
            mismatches.append(f"immutable file mismatch: {relative}")

    expected_database = manifest["database"]
    current_database = database_fingerprint(
        root / str(expected_database["relative_path"]),
        baseline_tables=expected_database["tables"],
    )
    database_business_matches = current_database["business_sha256"] == expected_database["business_sha256"]
    if not database_business_matches and not strict_database_binary:
        expected_stable = _database_business_hash_excluding(
            expected_database,
            excluded_tables=RUNTIME_DERIVED_TABLES,
        )
        current_stable = _database_business_hash_excluding(
            current_database,
            excluded_tables=RUNTIME_DERIVED_TABLES,
        )
        database_business_matches = current_stable == expected_stable
    if not database_business_matches:
        mismatches.append("database business fingerprint mismatch")
    if current_database["current_pointer"] != expected_database["current_pointer"]:
        mismatches.append("current production pointer mismatch")
    binary_matches = current_database["file_sha256_at_freeze"] == expected_database["file_sha256_at_freeze"]
    if strict_database_binary and not binary_matches:
        mismatches.append("database binary SHA-256 mismatch")
    database_copy = baseline_root / "immutable" / str(expected_database["relative_path"])
    if not database_copy.is_file() or sha256_file(database_copy) != expected_database["file_sha256_at_freeze"]:
        mismatches.append("immutable database copy mismatch")

    return {
        "ok": not mismatches,
        "mismatches": mismatches,
        "file_count": len(manifest["files"]),
        "table_count": len(expected_database["tables"]),
        "aggregate_sha256": expected_aggregate,
        "database_business_sha256": current_database["business_sha256"],
        "current_pointer_sha256": current_database["current_pointer"]["pointer_sha256"],
        "database_binary_matches": binary_matches,
    }


def _database_business_hash_excluding(
    database: dict[str, Any],
    *,
    excluded_tables: set[str],
) -> str:
    tables = [
        table
        for table in database.get("tables", [])
        if str(table.get("table")) not in excluded_tables
    ]
    return canonical_hash({"tables": tables, "current_pointer": database.get("current_pointer")})
