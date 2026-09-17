from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from typing import TypeVar

from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.domain.enums import (
    AnalystReviewStatus,
    FinancialReleaseStatus,
    FinancialValidationStatus,
    ReviewActorType,
    TechnicalValidationStatus,
)
from app.domain.models import FinancialReleaseRecord, FinancialReviewDecision
from app.domain.schemas import FinancialReleaseState, ReleaseGateResult
from app.services.audit_baseline import canonical_hash


HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
AUTOMATION_IDENTITIES = {"ai", "automation", "bot", "codex", "service", "system"}
T = TypeVar("T")


class GovernanceError(RuntimeError):
    """Base error for release-governance operations."""


class ReadOnlyGovernanceError(GovernanceError):
    """Raised before a write session is opened in public/read-only mode."""


class InvalidLifecycleTransition(GovernanceError):
    """Raised when an axis attempts an unsupported or backward transition."""


class InvalidHumanApproval(GovernanceError):
    """Raised when an analyst decision is missing verifiable human metadata."""


class ReleaseGateClosed(GovernanceError):
    """Raised when eligibility is requested with unresolved blockers."""


def state_from_record(record: FinancialReleaseRecord) -> FinancialReleaseState:
    return FinancialReleaseState.model_validate(record)


def evaluate_release_gate(
    state: FinancialReleaseState | FinancialReleaseRecord,
    *,
    expected_baseline_manifest_hash: str | None = None,
) -> ReleaseGateResult:
    candidate = state_from_record(state) if isinstance(state, FinancialReleaseRecord) else state
    blockers: list[str] = []
    if candidate.technical_status != TechnicalValidationStatus.PASSED:
        blockers.append("TECHNICAL_NOT_PASSED")
    if candidate.financial_status != FinancialValidationStatus.PASSED:
        blockers.append("FINANCIAL_NOT_PASSED")
    if candidate.analyst_review_status != AnalystReviewStatus.APPROVED:
        blockers.append("ANALYST_NOT_APPROVED")
    if candidate.reviewer_actor_type != ReviewActorType.HUMAN:
        blockers.append("ANALYST_ACTOR_NOT_HUMAN")
    if not candidate.reviewer_identity.strip():
        blockers.append("ANALYST_IDENTITY_MISSING")
    if candidate.reviewed_at is None:
        blockers.append("ANALYST_TIMESTAMP_MISSING")
    if not candidate.decision_reason.strip():
        blockers.append("ANALYST_REASON_MISSING")
    if not candidate.rulebook_version.strip():
        blockers.append("RULEBOOK_VERSION_MISSING")
    if not candidate.review_version.strip():
        blockers.append("REVIEW_VERSION_MISSING")
    if not _valid_hash(candidate.baseline_manifest_hash):
        blockers.append("BASELINE_HASH_MISSING")
    elif expected_baseline_manifest_hash and candidate.baseline_manifest_hash != expected_baseline_manifest_hash:
        blockers.append("BASELINE_HASH_MISMATCH")
    if not _valid_hash(candidate.source_content_hash):
        blockers.append("SOURCE_HASH_MISSING")
    if not _valid_hash(candidate.candidate_content_hash):
        blockers.append("CANDIDATE_HASH_MISSING")
    if not _valid_hash(candidate.artifact_manifest_hash):
        blockers.append("ARTIFACT_HASH_MISSING")
    if not candidate.required_artifacts_valid:
        blockers.append("REQUIRED_ARTIFACTS_INVALID")
    if candidate.unresolved_manual_review_count:
        blockers.append("UNRESOLVED_MANUAL_REVIEW")
    if candidate.rejection_count:
        blockers.append("REJECTIONS_PRESENT")
    return ReleaseGateResult(eligible=not blockers, blockers=blockers)


def require_governance_write_access(settings: Settings) -> None:
    if settings.public_demo or settings.database_mode == "READ_ONLY_EPHEMERAL":
        raise ReadOnlyGovernanceError("Financial governance is read-only in public demo mode.")


def execute_governance_mutation(
    *,
    settings: Settings,
    session_factory: sessionmaker[Session],
    mutation: Callable[[Session], T],
) -> T:
    """Reject read-only mode before the session factory can open a write session."""

    require_governance_write_access(settings)
    with session_factory.begin() as session:
        return mutation(session)


def transition_status(
    record: FinancialReleaseRecord,
    *,
    axis: str,
    new_status: str,
    settings: Settings,
) -> None:
    require_governance_write_access(settings)
    attribute, allowed = _transition_policy(axis)
    previous = str(getattr(record, attribute))
    if previous == new_status:
        return
    if new_status not in allowed.get(previous, set()):
        raise InvalidLifecycleTransition(f"Invalid {axis} transition: {previous} -> {new_status}")
    if axis == "analyst" and new_status == AnalystReviewStatus.APPROVED.value:
        raise InvalidHumanApproval("Analyst approval requires record_human_analyst_decision().")
    setattr(record, attribute, new_status)


def bind_baseline_manifest(
    record: FinancialReleaseRecord,
    *,
    baseline_manifest_hash: str,
    settings: Settings,
) -> None:
    require_governance_write_access(settings)
    if record.release_status != FinancialReleaseStatus.QUARANTINED.value:
        raise InvalidLifecycleTransition("Baseline can only be bound while the subject is quarantined.")
    if not _valid_hash(baseline_manifest_hash):
        raise GovernanceError("baseline_manifest_hash must be a lowercase SHA-256 value")
    if record.baseline_manifest_hash and record.baseline_manifest_hash != baseline_manifest_hash:
        raise GovernanceError("A different baseline hash is already bound; automatic refresh is forbidden.")
    record.baseline_manifest_hash = baseline_manifest_hash


def record_human_analyst_decision(
    session: Session,
    record: FinancialReleaseRecord,
    *,
    new_status: AnalystReviewStatus,
    actor_type: ReviewActorType,
    reviewer_identity: str,
    reviewed_at: datetime,
    decision_reason: str,
    rulebook_version: str,
    review_version: str,
    source_content_hash: str,
    settings: Settings,
) -> FinancialReviewDecision:
    require_governance_write_access(settings)
    identity = reviewer_identity.strip()
    reason = decision_reason.strip()
    if actor_type != ReviewActorType.HUMAN:
        raise InvalidHumanApproval("Only an explicit HUMAN actor may sign analyst acceptance.")
    if not identity or identity.casefold() in AUTOMATION_IDENTITIES:
        raise InvalidHumanApproval("A non-automation reviewer identity is required.")
    if reviewed_at.tzinfo is None or reviewed_at.utcoffset() is None:
        raise InvalidHumanApproval("reviewed_at must be timezone-aware.")
    if not reason:
        raise InvalidHumanApproval("A non-empty analyst decision rationale is required.")
    if new_status not in {AnalystReviewStatus.APPROVED, AnalystReviewStatus.REJECTED}:
        raise InvalidHumanApproval("Human decision must be APPROVED or REJECTED.")
    if record.analyst_review_status != AnalystReviewStatus.NOT_REVIEWED.value:
        raise InvalidLifecycleTransition(
            f"Analyst decision is terminal: {record.analyst_review_status} -> {new_status.value}"
        )
    if not rulebook_version.strip() or not review_version.strip():
        raise InvalidHumanApproval("Rulebook and review versions are required.")
    if not _valid_hash(source_content_hash):
        raise InvalidHumanApproval("A source-content SHA-256 is required.")

    payload = {
        "release_record_id": record.id,
        "previous_status": record.analyst_review_status,
        "new_status": new_status.value,
        "actor_type": actor_type.value,
        "reviewer_identity": identity,
        "reviewed_at": reviewed_at.isoformat(),
        "decision_reason": reason,
        "rulebook_version": rulebook_version.strip(),
        "review_version": review_version.strip(),
        "source_content_hash": source_content_hash,
    }
    decision = FinancialReviewDecision(
        release_record_id=record.id,
        previous_status=record.analyst_review_status,
        new_status=new_status.value,
        reviewer_actor_type=actor_type.value,
        reviewer_identity=identity,
        reviewed_at=reviewed_at,
        decision_reason=reason,
        rulebook_version=rulebook_version.strip(),
        review_version=review_version.strip(),
        source_content_hash=source_content_hash,
        decision_content_hash=canonical_hash(payload),
        is_demo=record.is_demo,
    )
    record.analyst_review_status = new_status.value
    record.reviewer_actor_type = actor_type.value
    record.reviewer_identity = identity
    record.reviewed_at = reviewed_at
    record.decision_reason = reason
    record.rulebook_version = rulebook_version.strip()
    record.review_version = review_version.strip()
    record.source_content_hash = source_content_hash
    session.add(decision)
    return decision


def mark_release_eligible(
    record: FinancialReleaseRecord,
    *,
    expected_baseline_manifest_hash: str,
    settings: Settings,
) -> ReleaseGateResult:
    result = evaluate_release_gate(record, expected_baseline_manifest_hash=expected_baseline_manifest_hash)
    if not result.eligible:
        raise ReleaseGateClosed(", ".join(result.blockers))
    transition_status(
        record,
        axis="release",
        new_status=FinancialReleaseStatus.ELIGIBLE.value,
        settings=settings,
    )
    return result


def _transition_policy(axis: str) -> tuple[str, dict[str, set[str]]]:
    policies: dict[str, tuple[str, dict[str, set[str]]]] = {
        "technical": (
            "technical_status",
            {
                TechnicalValidationStatus.NOT_VALIDATED.value: {
                    TechnicalValidationStatus.PASSED.value,
                    TechnicalValidationStatus.FAILED.value,
                }
            },
        ),
        "financial": (
            "financial_status",
            {
                FinancialValidationStatus.NOT_REVIEWED.value: {
                    FinancialValidationStatus.MANUAL_REVIEW_REQUIRED.value,
                    FinancialValidationStatus.PASSED.value,
                    FinancialValidationStatus.FAILED.value,
                },
                FinancialValidationStatus.MANUAL_REVIEW_REQUIRED.value: {
                    FinancialValidationStatus.PASSED.value,
                    FinancialValidationStatus.FAILED.value,
                },
            },
        ),
        "analyst": (
            "analyst_review_status",
            {
                AnalystReviewStatus.NOT_REVIEWED.value: {
                    AnalystReviewStatus.APPROVED.value,
                    AnalystReviewStatus.REJECTED.value,
                }
            },
        ),
        "release": (
            "release_status",
            {
                FinancialReleaseStatus.QUARANTINED.value: {
                    FinancialReleaseStatus.ELIGIBLE.value,
                    FinancialReleaseStatus.REJECTED.value,
                },
                FinancialReleaseStatus.ELIGIBLE.value: {FinancialReleaseStatus.RELEASED.value},
            },
        ),
    }
    try:
        return policies[axis]
    except KeyError as exc:
        raise GovernanceError(f"Unknown governance axis: {axis}") from exc


def _valid_hash(value: str) -> bool:
    return bool(HASH_PATTERN.fullmatch(value))
