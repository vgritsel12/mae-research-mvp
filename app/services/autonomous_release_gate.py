from __future__ import annotations

import re

from app.config import Settings
from app.domain.enums import (
    AutonomousReleaseStatus,
    AutonomousTechnicalStatus,
    ModelValidationStatus,
)
from app.domain.models import AutonomousReleaseRecord
from app.domain.schemas import AutonomousGateResult, AutonomousReleaseState
from app.services.financial_release_gate import (
    GovernanceError,
    ReadOnlyGovernanceError,
    require_governance_write_access,
)


HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
EXPECTED_ROWS = 19
EXPECTED_REGIONS = 6
EXPECTED_APPLICABLE = 104
EXPECTED_NOT_APPLICABLE = 10


class AutonomousReleaseGateClosed(GovernanceError):
    pass


def state_from_record(record: AutonomousReleaseRecord) -> AutonomousReleaseState:
    return AutonomousReleaseState.model_validate(record)


def evaluate_autonomous_release_gate(
    state: AutonomousReleaseState | AutonomousReleaseRecord,
    *,
    expected_baseline_manifest_hash: str | None = None,
) -> AutonomousGateResult:
    """Evaluate machine release integrity; human review is intentionally absent."""

    candidate = state_from_record(state) if isinstance(state, AutonomousReleaseRecord) else state
    blockers: list[str] = []
    if candidate.technical_status != AutonomousTechnicalStatus.PASSED:
        blockers.append("TECHNICAL_FAILED")
    if candidate.model_validation_status == ModelValidationStatus.FAILED:
        blockers.append("MODEL_VALIDATION_FAILED")
    if candidate.matrix_rows != EXPECTED_ROWS:
        blockers.append("MATRIX_ROWS_INVALID")
    if candidate.matrix_regions != EXPECTED_REGIONS:
        blockers.append("MATRIX_REGIONS_INVALID")
    if candidate.applicable_score_count != EXPECTED_APPLICABLE:
        blockers.append("APPLICABLE_SCORE_COUNT_INVALID")
    if candidate.not_applicable_count != EXPECTED_NOT_APPLICABLE:
        blockers.append("NOT_APPLICABLE_COUNT_INVALID")
    if not HASH_PATTERN.fullmatch(candidate.baseline_manifest_hash):
        blockers.append("BASELINE_MANIFEST_HASH_INVALID")
    elif (
        expected_baseline_manifest_hash
        and candidate.baseline_manifest_hash != expected_baseline_manifest_hash
    ):
        blockers.append("BASELINE_MANIFEST_HASH_MISMATCH")
    if not HASH_PATTERN.fullmatch(candidate.source_manifest_hash):
        blockers.append("SOURCE_MANIFEST_HASH_INVALID")
    if not HASH_PATTERN.fullmatch(candidate.candidate_content_hash):
        blockers.append("CANDIDATE_CONTENT_HASH_INVALID")
    if not HASH_PATTERN.fullmatch(candidate.artifact_manifest_hash):
        blockers.append("ARTIFACT_MANIFEST_HASH_INVALID")
    if not candidate.required_artifacts_valid:
        blockers.append("REQUIRED_ARTIFACTS_INVALID")
    if candidate.critical_error_count:
        blockers.append("CRITICAL_ERRORS_PRESENT")
    return AutonomousGateResult(eligible=not blockers, blockers=blockers)


def mark_auto_published(
    record: AutonomousReleaseRecord,
    *,
    settings: Settings,
) -> AutonomousGateResult:
    require_autonomous_write_access(settings)
    result = evaluate_autonomous_release_gate(record)
    if not result.eligible:
        record.release_status = AutonomousReleaseStatus.BLOCKED.value
        raise AutonomousReleaseGateClosed(", ".join(result.blockers))
    record.release_status = AutonomousReleaseStatus.AUTO_PUBLISHED.value
    return result


def require_autonomous_write_access(settings: Settings) -> None:
    try:
        require_governance_write_access(settings)
    except ReadOnlyGovernanceError as exc:
        raise ReadOnlyGovernanceError(
            "Autonomous release governance is read-only in public demo mode."
        ) from exc
