from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine

from app.config import ROOT_DIR, get_settings
from app.services.audit_disclosure import AuditDisclosure, audit_disclosure_paths, load_audit_disclosure
from app.services.mae_snapshot_product import FULL_ASSETS, FULL_GEOGRAPHIES, FULL_PRODUCT_VERSION
from app.services.atomic_release import ReleaseIntegrityError, ResolvedRelease, resolve_committed_release
from app.services.autonomous_pipeline import AUTONOMOUS_DISCLOSURE, canonical_hash, file_sha256
from app.services.autonomous_release import AUTONOMOUS_CHANNEL
from app.services.historical_analogs import build_release_historical_analog_artifact
from app.services.temporal_metadata import comparison_metadata, parse_iso_date
from app.services.trust_language import sanitize_trust_language


ASSET_ORDER = [asset_segment for _, _, asset_segment in FULL_ASSETS]
GEOGRAPHY_ORDER = list(FULL_GEOGRAPHIES)


@dataclass
class FullSnapshotData:
    scores: pd.DataFrame = field(default_factory=pd.DataFrame)
    evidence: pd.DataFrame = field(default_factory=pd.DataFrame)
    scenarios: pd.DataFrame = field(default_factory=pd.DataFrame)
    transmission: pd.DataFrame = field(default_factory=pd.DataFrame)
    quality: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    market_summary: str = ""
    snapshot_date: str = ""
    source_mode: str = "UNAVAILABLE"
    snapshot_id: str = ""
    paths: dict[str, Path] = field(default_factory=dict)
    audit: AuditDisclosure = field(default_factory=AuditDisclosure)
    release: dict[str, Any] = field(default_factory=dict)
    analogs: dict[str, Any] = field(default_factory=dict)
    disclosure: str = AUTONOMOUS_DISCLOSURE
    errors: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return not self.scores.empty and len(self.scores) == 114

    @property
    def applicable_scores(self) -> pd.DataFrame:
        if self.scores.empty or "applicable" not in self.scores:
            return pd.DataFrame(columns=self.scores.columns)
        return self.scores[self.scores["applicable"].astype(str).str.lower().eq("true")].copy()

    @property
    def not_applicable_scores(self) -> pd.DataFrame:
        if self.scores.empty or "applicable" not in self.scores:
            return pd.DataFrame(columns=self.scores.columns)
        return self.scores[~self.scores["applicable"].astype(str).str.lower().eq("true")].copy()

    @property
    def evidence_coverage(self) -> int:
        if self.evidence.empty or self.applicable_scores.empty:
            return 0
        if "review_status" not in self.evidence or "cell_id" not in self.evidence:
            return 0
        valid = self.evidence[self.evidence["review_status"].astype(str).eq("PASS")]
        return len(set(valid.get("cell_id", pd.Series(dtype=str))) & set(self.applicable_scores["cell_id"]))

    @property
    def benchmark_coverage(self) -> int:
        value = self.quality.get("benchmark_coverage")
        if isinstance(value, int):
            return value
        applicable = self.applicable_scores
        if applicable.empty or "benchmark_id" not in applicable:
            return 0
        return int(applicable["benchmark_id"].astype(str).str.strip().ne("").sum())

    @property
    def is_autonomous(self) -> bool:
        return self.source_mode in {"AUTONOMOUS_COMMITTED_RELEASE", "AUTONOMOUS_BUNDLED_RELEASE"}


def snapshot_cache_token(root: Path = ROOT_DIR, sqlite_path: Path | None = None) -> str:
    """Return a cheap cache-buster without opening or mutating canonical data."""
    database = sqlite_path or get_settings().sqlite_path
    paths = [
        database,
        root / "outputs" / "mae_full_latest_scores.csv",
        root / "outputs" / "mae_full_latest_evidence.csv",
        root / "outputs" / "mae_full_latest_scenarios.csv",
        root / "outputs" / "mae_full_latest_transmission.csv",
        root / "outputs" / "mae_full_latest_quality_report.json",
        root / "outputs" / "mae_full_latest_report.md",
    ]
    audit_paths = audit_disclosure_paths(root)
    paths.extend(
        audit_paths[key]
        for key in ("report", "cells", "evidence", "what_changed", "scenarios", "analogs", "json_export", "csv_export", "xlsx_export")
    )
    parts = []
    for path in paths:
        try:
            stat = path.stat()
            parts.append(f"{path.name}:{stat.st_mtime_ns}:{stat.st_size}")
        except OSError:
            parts.append(f"{path.name}:missing")
    return "|".join(parts)


@st.cache_resource(show_spinner=False)
def _readonly_connection(database_path: str, database_mtime_ns: int) -> sqlite3.Connection:
    del database_mtime_ns  # cache key only
    uri = f"file:{Path(database_path).resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


@st.cache_data(show_spinner=False)
def load_full_snapshot_cached(
    root_path: str = str(ROOT_DIR),
    sqlite_path: str = "",
    cache_token: str = "",
) -> FullSnapshotData:
    del cache_token  # cache key only
    root = Path(root_path)
    database = Path(sqlite_path) if sqlite_path else get_settings().sqlite_path
    return load_full_snapshot(root=root, sqlite_path=database)


def load_full_snapshot(*, root: Path = ROOT_DIR, sqlite_path: Path | None = None) -> FullSnapshotData:
    """Load the active full snapshot without any schema, database, or artifact writes."""
    database = sqlite_path or get_settings().sqlite_path
    errors: list[str] = []
    autonomous = _load_autonomous_release(root=root, database=database, errors=errors)
    if autonomous is not None:
        return autonomous
    database_snapshot = _latest_full_snapshot_from_sqlite(database, errors)

    snapshot_date = str(database_snapshot.get("snapshot_date") or "")
    snapshot_id = str(database_snapshot.get("id") or "")
    metadata = dict(database_snapshot.get("metadata") or {})
    paths, source_mode = _canonical_paths(root, snapshot_date)
    if not database_snapshot:
        source_mode = "UNAVAILABLE"

    scores = _read_csv(paths.get("scores"), errors, "Score Matrix CSV")
    evidence = _read_csv(paths.get("evidence"), errors, "Evidence CSV")
    scenarios = _read_csv(paths.get("scenarios"), errors, "Scenarios CSV")
    transmission = _read_csv(paths.get("transmission"), errors, "Transmission CSV")
    quality = _read_json(paths.get("quality_json"), errors, "Quality Report")
    validation = _read_json(paths.get("validation"), errors, "Validation Report", required=False)
    report_text = _read_text(paths.get("report"), errors, "Markdown Report")

    if not scores.empty:
        scores = _normalize_scores(scores)
        csv_date = str(scores.iloc[0].get("snapshot_date") or "")
        if snapshot_date and csv_date and csv_date != snapshot_date:
            errors.append(
                f"SQLite snapshot {snapshot_date} не совпадает с canonical dataset {csv_date}; показан подтверждённый dataset."
            )
        snapshot_date = csv_date or snapshot_date
        metadata = {
            **metadata,
            **dict(validation.get("metadata") or {}),
            "snapshot_date": snapshot_date,
            "snapshot_type": str(scores.iloc[0].get("snapshot_type") or metadata.get("snapshot_type") or ""),
            "previous_snapshot_date": str(
                scores.iloc[0].get("previous_snapshot_date") or metadata.get("previous_snapshot_date") or ""
            ),
        }
    market_summary = _market_summary(report_text) or str(metadata.get("market_summary") or "")
    if not market_summary:
        errors.append("Canonical Market Summary отсутствует.")
    audit = load_audit_disclosure(root=root)

    return FullSnapshotData(
        scores=scores,
        evidence=evidence,
        scenarios=scenarios,
        transmission=transmission,
        quality=quality,
        validation=validation,
        metadata=metadata,
        market_summary=market_summary,
        snapshot_date=snapshot_date,
        source_mode=("SQLITE + CANONICAL_DATASETS" if database_snapshot and source_mode == "DATED_CANONICAL" else source_mode),
        snapshot_id=snapshot_id,
        paths=paths,
        audit=audit,
        errors=tuple(dict.fromkeys(errors)),
    )


def _load_autonomous_release(
    *,
    root: Path,
    database: Path,
    errors: list[str],
) -> FullSnapshotData | None:
    engine = None
    resolved: ResolvedRelease | None = None
    if database.is_file():
        engine = create_engine(
            f"sqlite:///{database}",
            future=True,
            connect_args={"check_same_thread": False},
        )
        try:
            resolved = resolve_committed_release(
                engine=engine,
                root=root,
                channel=AUTONOMOUS_CHANNEL,
            )
        except (ReleaseIntegrityError, OSError, ValueError) as exc:
            errors.append(f"SQLite release pointer недоступен; используется bundled release artifact: {type(exc).__name__}.")
    resolved_from_database = resolved is not None
    if resolved is None:
        resolved = _resolve_bundled_autonomous_release(root=root, errors=errors)
    if resolved is None:
        if engine is not None:
            engine.dispose()
        return None
    try:
        matrix = sanitize_trust_language(
            json.loads(resolved.artifact_paths["stage_04_matrix.json"].read_text(encoding="utf-8"))
        )
        market_synthesis = sanitize_trust_language(
            json.loads(resolved.artifact_paths["stage_02_research_views.json"].read_text(encoding="utf-8"))
        )
        lineage = sanitize_trust_language(
            json.loads(resolved.artifact_paths["stage_03_signals.json"].read_text(encoding="utf-8"))
        )
        scenarios_payload = sanitize_trust_language(
            json.loads(resolved.artifact_paths["stage_05_scenarios.json"].read_text(encoding="utf-8"))
        )
        explanation_payload = sanitize_trust_language(
            json.loads(resolved.artifact_paths["stage_06_explanations_analogs.json"].read_text(encoding="utf-8"))
        )
        validation = sanitize_trust_language(
            json.loads(resolved.artifact_paths["stage_07_validation.json"].read_text(encoding="utf-8"))
        )
        source_selection_path = resolved.artifact_paths.get("source_selection_manifest.json")
        source_selection = (
            sanitize_trust_language(json.loads(source_selection_path.read_text(encoding="utf-8")))
            if source_selection_path and source_selection_path.is_file()
            else {}
        )
        official = pd.read_csv(
            root / "outputs" / "mae_full_latest_scores.csv",
            dtype=str,
            keep_default_na=False,
            encoding="utf-8-sig",
        )
        cells = list(matrix.get("cells") or [])
        if len(cells) != 114 or len(official) != 114:
            raise ValueError("autonomous matrix/official metadata geometry mismatch")
        rows: list[dict[str, Any]] = []
        evidence_rows: list[dict[str, Any]] = []
        transmission_rows: list[dict[str, Any]] = []
        for cell, (_, previous_metadata) in zip(cells, official.iterrows(), strict=True):
            if str(cell.get("region")) != str(previous_metadata.get("geography")):
                raise ValueError("autonomous matrix order does not match official metadata")
            applicable = cell.get("applicability") == "APPLICABLE"
            row = {
                **previous_metadata.to_dict(),
                "cell_id": cell["canonical_cell_id"],
                "canonical_cell_id": cell["canonical_cell_id"],
                "snapshot_date": validation["snapshot_date"],
                "previous_snapshot_date": validation["previous_snapshot_date"],
                "applicable": "true" if applicable else "false",
                "score": "" if cell.get("current") is None else str(cell["current"]),
                "previous_score": "" if cell.get("previous") is None else str(cell["previous"]),
                "score_delta": "" if cell.get("delta") is None else str(cell["delta"]),
                "evidence_mode": cell.get("mode") or "NOT_APPLICABLE",
                "conviction": cell.get("confidence") or "NOT_APPLICABLE",
                "thesis": cell.get("reasoning") or "",
                "main_driver": " → ".join(cell.get("transmission_chain") or []) or cell.get("reasoning") or "",
                "trigger": cell.get("invalidation") or "",
                "veto": cell.get("invalidation") or "",
                "change_type": (
                    "NOT_APPLICABLE"
                    if not applicable
                    else "CARRY_FORWARD"
                    if cell.get("mode") == "CARRY_FORWARD"
                    else "MODEL_INFERRED"
                    if cell.get("mode") == "MODEL_INFERRED"
                    else "MARKET_CHANGE"
                ),
                "what_changed": cell.get("reasoning") or "",
                "why_changed": cell.get("reasoning") or "",
                "business_hash": cell.get("business_hash") or "",
                "invalidation": cell.get("invalidation") or "",
                "registered_inputs": "; ".join(cell.get("registered_inputs") or []),
                "supporting_theme_ids": "; ".join(cell.get("supporting_theme_ids") or []),
                "supporting_source_ids": "; ".join(cell.get("supporting_source_ids") or []),
                "main_risk": cell.get("main_risk") or "",
            }
            rows.append(row)
            transmission_rows.append(
                {
                    "cell_id": cell["canonical_cell_id"],
                    "asset_segment": previous_metadata.get("asset_segment"),
                    "geography": cell.get("region"),
                    "current_score": cell.get("current"),
                    "previous_score": cell.get("previous"),
                    "delta": cell.get("delta"),
                    "mode": cell.get("mode"),
                    "confidence": cell.get("confidence"),
                    "transmission_mechanism": " → ".join(cell.get("transmission_chain") or []),
                    "invalidation": cell.get("invalidation"),
                }
            )
            for source in cell.get("sources") or []:
                evidence_rows.append(
                    {
                        "cell_id": cell["canonical_cell_id"],
                        "provider": source.get("provider", ""),
                        "title": source.get("title", ""),
                        "publication_date": source.get("publication_date", ""),
                        "URL": source.get("url", ""),
                        "excerpt": cell.get("reasoning", ""),
                        "geography": cell.get("region", ""),
                        "asset_segment": previous_metadata.get("asset_segment", ""),
                        "review_status": "PASS",
                        "evidence_mode": cell.get("mode", ""),
                        "source_id": source.get("source_id", ""),
                    }
                )
        scores = _normalize_scores(pd.DataFrame(rows))
        scenarios = pd.DataFrame(scenarios_payload.get("scenarios") or [])
        analogs = _historical_analogs_from_release(
            explanation_payload,
            root=root,
            snapshot_date=str(validation.get("snapshot_date") or ""),
        )
        exports_dir = root / "outputs" / "autonomous" / "exports" / resolved.release_id
        paths = {
            **resolved.artifact_paths,
            "json_export": exports_dir / "MAE_autonomous_release.json",
            "csv_export": exports_dir / "MAE_autonomous_matrix.csv",
            "xlsx_export": exports_dir / "MAE_autonomous_market_report.xlsx",
            "export_manifest": exports_dir / "export_manifest.json",
            "source_selection_manifest": exports_dir / "source_selection_manifest.json",
            "source_report": exports_dir / "source_report.json",
            "historical_analog_export": exports_dir / "historical_analogs.json",
        }
        release = {
            "release_id": resolved.release_id,
            "manifest_hash": resolved.manifest_hash,
            "release_status": "AUTO_PUBLISHED",
            "technical_status": validation.get("technical_status"),
            "model_validation_status": validation.get("model_validation_status"),
            "human_review_status": validation.get("human_review_status"),
            "candidate_id": validation.get("candidate_id"),
        }
        market_summary = str(
            (explanation_payload.get("explanations") or {}).get("summary")
            or "The committed autonomous release is available with current cell-level provenance."
        )
        return FullSnapshotData(
            scores=scores,
            evidence=pd.DataFrame(evidence_rows),
            scenarios=scenarios,
            transmission=pd.DataFrame(transmission_rows),
            quality={
                "status": "PASS",
                "benchmark_coverage": int(scores["benchmark_id"].astype(str).str.strip().ne("").sum()),
                "matrix_business_hash": matrix.get("business_hash"),
            },
            validation=validation,
            metadata={
                **release,
                "snapshot_date": validation.get("snapshot_date"),
                "previous_snapshot_date": validation.get("previous_snapshot_date"),
                "mode_counts": matrix.get("mode_counts") or {},
                "market_themes": market_synthesis.get("themes") or [],
                "admitted_sources": market_synthesis.get("admitted_sources") or [],
                "theme_documents": market_synthesis.get("documents") or [],
                "theme_lineage": lineage,
                "source_selection": source_selection,
                "historical_analog_reference": explanation_payload.get("historical_analogs") or {},
                **comparison_metadata(
                    root=root,
                    sqlite_path=database,
                    current_release_id=resolved.release_id,
                    current_snapshot_date=str(validation.get("snapshot_date") or ""),
                    technical_previous_snapshot_date=str(validation.get("previous_snapshot_date") or ""),
                ),
            },
            market_summary=market_summary,
            snapshot_date=str(validation.get("snapshot_date") or ""),
            source_mode="AUTONOMOUS_COMMITTED_RELEASE" if resolved_from_database else "AUTONOMOUS_BUNDLED_RELEASE",
            snapshot_id=resolved.release_id,
            paths=paths,
            release=release,
            analogs=analogs,
            disclosure=AUTONOMOUS_DISCLOSURE,
            errors=tuple(dict.fromkeys(errors)),
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError, pd.errors.ParserError) as exc:
        errors.append(f"Autonomous release could not be loaded ({type(exc).__name__}: {exc}).")
        return None
    finally:
        if engine is not None:
            engine.dispose()


def _resolve_bundled_autonomous_release(*, root: Path, errors: list[str]) -> ResolvedRelease | None:
    versions = root / "outputs" / "releases" / "versions" / AUTONOMOUS_CHANNEL
    manifests = sorted(
        (path for path in versions.glob("mae-*/release_manifest.json") if path.is_file()),
        key=lambda path: (path.stat().st_mtime_ns, path.parent.name),
        reverse=True,
    )
    for manifest_path in manifests:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            aggregate_input = dict(manifest)
            aggregate = aggregate_input.pop("aggregate_sha256", "")
            if canonical_hash(aggregate_input) != aggregate:
                raise ValueError("release manifest aggregate mismatch")
            artifact_paths: dict[str, Path] = {}
            for item in manifest.get("files") or []:
                relative_path = str(item.get("relative_path") or "")
                artifact = manifest_path.parent / relative_path
                if not artifact.is_file() or file_sha256(artifact) != item.get("sha256"):
                    raise ValueError(f"artifact mismatch: {relative_path}")
                artifact_paths[relative_path] = artifact
            release_id = str(manifest.get("release_id") or manifest_path.parent.name)
            export_manifest_path = root / "outputs" / "autonomous" / "exports" / release_id / "export_manifest.json"
            if not export_manifest_path.is_file():
                raise ValueError("bundled export manifest missing")
            export_manifest = json.loads(export_manifest_path.read_text(encoding="utf-8"))
            if str(export_manifest.get("release_id") or "") != release_id:
                raise ValueError("bundled export manifest release mismatch")
            return ResolvedRelease(
                release_id=release_id,
                channel=str(manifest.get("channel") or AUTONOMOUS_CHANNEL),
                release_directory=manifest_path.parent,
                manifest_path=manifest_path,
                manifest_hash=file_sha256(manifest_path),
                artifact_paths=artifact_paths,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"Bundled release skipped: {manifest_path.parent.name}: {exc}")
    return None


def get_cell(bundle: FullSnapshotData, asset_segment: str, geography: str) -> dict[str, Any] | None:
    if bundle.scores.empty:
        return None
    matches = bundle.scores[
        bundle.scores["asset_segment"].eq(asset_segment) & bundle.scores["geography"].eq(geography)
    ]
    return matches.iloc[0].to_dict() if not matches.empty else None


def matrix_dimensions(bundle: FullSnapshotData) -> tuple[int, int, int, int]:
    if bundle.scores.empty:
        return 0, 0, 0, 0
    return (
        int(bundle.scores["asset_segment"].nunique()),
        int(bundle.scores["geography"].nunique()),
        len(bundle.applicable_scores),
        len(bundle.not_applicable_scores),
    )


def _historical_analogs_from_release(explanation_payload: dict[str, Any], *, root: Path, snapshot_date: str = "") -> dict[str, Any]:
    analogs = explanation_payload.get("historical_analogs") or {}
    if isinstance(analogs.get("artifact"), dict):
        artifact = sanitize_trust_language(analogs["artifact"])
        if _analog_matches_snapshot(artifact, snapshot_date):
            return artifact
        recalculated = _recalculated_analogs(root=root, snapshot_date=snapshot_date)
        if recalculated:
            return recalculated
        return artifact
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
            artifact = sanitize_trust_language(json.loads(path.read_text(encoding="utf-8")))
            if _analog_matches_snapshot(artifact, snapshot_date):
                return artifact
            recalculated = _recalculated_analogs(root=root, snapshot_date=snapshot_date)
            if recalculated:
                return recalculated
            return artifact
    fallback = root / "outputs" / "mae_historical_analogs_latest.json"
    if fallback.is_file():
        artifact = sanitize_trust_language(json.loads(fallback.read_text(encoding="utf-8")))
        if _analog_matches_snapshot(artifact, snapshot_date):
            return artifact
        recalculated = _recalculated_analogs(root=root, snapshot_date=snapshot_date)
        if recalculated:
            return recalculated
        return artifact
    return {}


def _analog_matches_snapshot(artifact: dict[str, Any], snapshot_date: str) -> bool:
    expected = str(snapshot_date or "")[:10]
    if not expected:
        return True
    return str(artifact.get("release_snapshot_date") or artifact.get("snapshot_date") or "")[:10] == expected


def _recalculated_analogs(*, root: Path, snapshot_date: str) -> dict[str, Any]:
    parsed = parse_iso_date(snapshot_date)
    if parsed is None:
        return {}
    try:
        return sanitize_trust_language(build_release_historical_analog_artifact(parsed, root=root))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def _latest_full_snapshot_from_sqlite(database: Path, errors: list[str]) -> dict[str, Any]:
    if not database.exists():
        errors.append("SQLite недоступна; authoritative release не определён.")
        return {}
    try:
        connection = _readonly_connection(str(database), database.stat().st_mtime_ns)
        rows = connection.execute(
            """
            SELECT id, snapshot_date, status, run_metadata
            FROM mae_snapshots
            WHERE is_demo = 0 AND snapshot_date <= ?
            ORDER BY snapshot_date DESC, created_at DESC
            """,
            (date.today().isoformat(),),
        ).fetchall()
        for row in rows:
            if str(row["status"] or "").startswith("INVALID"):
                continue
            try:
                metadata = json.loads(row["run_metadata"]) if isinstance(row["run_metadata"], str) else dict(row["run_metadata"] or {})
            except (TypeError, json.JSONDecodeError):
                continue
            if metadata.get("full_product_version") == FULL_PRODUCT_VERSION:
                return {"id": row["id"], "snapshot_date": row["snapshot_date"], "metadata": metadata}
        errors.append("В SQLite не найден подтверждённый full snapshot; latest-файлы не являются authoritative.")
    except (OSError, sqlite3.Error) as exc:
        errors.append(f"SQLite временно недоступна ({type(exc).__name__}); authoritative release не определён.")
    return {}


def _canonical_paths(root: Path, snapshot_date: str) -> tuple[dict[str, Path], str]:
    if snapshot_date:
        directory = root / "outputs" / "snapshots" / snapshot_date / "full"
        dated = {
            "scores": directory / f"mae_full_scores_{snapshot_date}.csv",
            "evidence": directory / f"mae_full_evidence_{snapshot_date}.csv",
            "scenarios": directory / f"mae_full_scenarios_{snapshot_date}.csv",
            "transmission": directory / f"mae_full_transmission_{snapshot_date}.csv",
            "report": directory / f"mae_full_report_{snapshot_date}.md",
            "quality_json": directory / f"mae_full_quality_report_{snapshot_date}.json",
            "quality_md": directory / f"mae_full_quality_report_{snapshot_date}.md",
            "validation": directory / f"mae_full_validation_{snapshot_date}.json",
            "workbook": directory / f"mae_full_snapshot_{snapshot_date}.xlsx",
        }
        required = ("scores", "evidence", "scenarios", "transmission", "report", "quality_json")
        if all(dated[name].exists() for name in required):
            return dated, "DATED_CANONICAL"

    return {}, "UNAVAILABLE"


def _read_csv(path: Path | None, errors: list[str], label: str) -> pd.DataFrame:
    if not path or not path.is_file():
        errors.append(f"{label} отсутствует.")
        return pd.DataFrame()
    try:
        return pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    except (OSError, pd.errors.ParserError, UnicodeError) as exc:
        errors.append(f"{label} не удалось прочитать ({type(exc).__name__}).")
        return pd.DataFrame()


def _read_json(path: Path | None, errors: list[str], label: str, *, required: bool = True) -> dict[str, Any]:
    if not path or not path.is_file():
        if required:
            errors.append(f"{label} отсутствует.")
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"{label} не удалось прочитать ({type(exc).__name__}).")
        return {}


def _read_text(path: Path | None, errors: list[str], label: str) -> str:
    if not path or not path.is_file():
        errors.append(f"{label} отсутствует.")
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        errors.append(f"{label} не удалось прочитать ({type(exc).__name__}).")
        return ""


def _market_summary(report_text: str) -> str:
    match = re.search(r"^## Market Summary\s*(.+?)(?=^## |\Z)", report_text, flags=re.MULTILINE | re.DOTALL)
    if not match:
        return ""
    return " ".join(line.strip() for line in match.group(1).strip().splitlines() if line.strip())


def _normalize_scores(scores: pd.DataFrame) -> pd.DataFrame:
    result = scores.copy()
    result["asset_segment"] = pd.Categorical(result["asset_segment"], categories=ASSET_ORDER, ordered=True)
    result["geography"] = pd.Categorical(result["geography"], categories=GEOGRAPHY_ORDER, ordered=True)
    result = result.sort_values(["asset_segment", "geography"], kind="stable").reset_index(drop=True)
    result["asset_segment"] = result["asset_segment"].astype("string")
    result["geography"] = result["geography"].astype("string")
    return result
