from __future__ import annotations

import json
import uuid
from datetime import date
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.domain.models import AutonomousReleaseRecord
from app.services.atomic_release import (
    AtomicReleaseError,
    AtomicReleaseRequest,
    AtomicReleaseResult,
    publish_atomic_release,
)


AUTONOMOUS_CHANNEL = "autonomous-mae"
DEFAULT_BASELINE_MANIFEST = Path(
    "outputs/audit_baseline/2026-07-13/AUDIT_BASELINE_2026-07-13.json"
)


def publish_autonomous_candidate(
    *,
    engine: Engine,
    settings: Settings,
    root: Path,
    candidate_dir: Path,
    expected_baseline_manifest_hash: str | None = None,
    fault_at: str | None = None,
) -> AtomicReleaseResult:
    candidate_dir = candidate_dir.resolve()
    validation = _read_json(candidate_dir / "stage_07_validation.json")
    manifest = _read_json(candidate_dir / "candidate_manifest.json")
    baseline_path = (root / DEFAULT_BASELINE_MANIFEST).resolve()
    baseline = _read_json(baseline_path)
    actual_baseline_hash = str(baseline.get("aggregate_sha256") or "")
    if expected_baseline_manifest_hash and expected_baseline_manifest_hash != actual_baseline_hash:
        raise AtomicReleaseError(
            "Baseline manifest hash mismatch; automatic baseline refresh is forbidden."
        )
    candidate_id = str(validation["candidate_id"])
    governance_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"mae:autonomous-governance:{candidate_id}")
    )
    model_version = _read_json(candidate_dir / "stage_01_collection.json").get("model", "")
    record_values = {
        "id": governance_id,
        "subject_type": "AUTONOMOUS_MAE_CANDIDATE",
        "subject_id": candidate_id,
        "candidate_id": candidate_id,
        "snapshot_date": date.fromisoformat(validation["snapshot_date"]),
        "technical_status": validation["technical_status"],
        "model_validation_status": validation["model_validation_status"],
        "release_status": validation.get("candidate_release_status") or validation["release_status"],
        "human_review_status": validation["human_review_status"],
        "matrix_rows": 19,
        "matrix_regions": 6,
        "applicable_score_count": validation["applicable_score_count"],
        "not_applicable_count": validation["not_applicable_count"],
        "baseline_manifest_hash": actual_baseline_hash,
        "source_manifest_hash": validation["source_manifest_hash"],
        "candidate_content_hash": validation["candidate_content_hash"],
        "artifact_manifest_hash": manifest["artifact_manifest_hash"],
        "required_artifacts_valid": True,
        "critical_error_count": validation["critical_error_count"],
        "model_version": model_version,
        "validation_summary": validation["checks"],
        "is_demo": False,
    }
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    with factory.begin() as session:
        existing = session.get(AutonomousReleaseRecord, governance_id)
        if existing is None:
            session.add(AutonomousReleaseRecord(**record_values))
        else:
            immutable_fields = (
                "candidate_id",
                "baseline_manifest_hash",
                "source_manifest_hash",
                "candidate_content_hash",
                "artifact_manifest_hash",
            )
            changed = [field for field in immutable_fields if getattr(existing, field) != record_values[field]]
            if changed:
                raise AtomicReleaseError(
                    "Existing autonomous governance content changed: " + ", ".join(changed)
                )
    artifact_names = tuple(item["name"] for item in manifest["artifacts"]) + (
        "candidate_manifest.json",
    )
    release_id = f"mae-{validation['snapshot_date']}-{validation['candidate_content_hash'][:12]}"
    request = AtomicReleaseRequest(
        release_id=release_id,
        channel=AUTONOMOUS_CHANNEL,
        candidate_id=candidate_id,
        snapshot_date=date.fromisoformat(validation["snapshot_date"]),
        governance_record_id=governance_id,
        source_dir=candidate_dir,
        artifact_names=artifact_names,
        expected_baseline_manifest_hash=actual_baseline_hash,
        gate_type="AUTONOMOUS",
    )
    return publish_atomic_release(
        engine=engine,
        settings=settings,
        root=root,
        request=request,
        fault_at=fault_at,
    )


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AtomicReleaseError(f"Required JSON artifact cannot be read: {path.name}") from exc
    if not isinstance(value, dict):
        raise AtomicReleaseError(f"Required JSON artifact is not an object: {path.name}")
    return value
