from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import ROOT_DIR
from app.domain.models import AnalystReview, AppLog, ResearchView
from app.services.methodology import (
    ARCHIVED_LEGACY,
    BOILERPLATE,
    INVALID_EVIDENCE,
    INVALID_MAPPING,
    IRRELEVANT_DOCUMENT,
    MANUAL_REVIEW_REQUIRED,
    REJECTED_EVIDENCE,
    REJECTED_MAPPING,
    STRICT_VALIDATED,
    VERIFIED_PASS,
)


SECOND_REVIEW_TO_PRODUCTION_STATUS = {
    "TRUE_PASS": STRICT_VALIDATED,
    "WRONG_REGION": REJECTED_MAPPING,
    "WRONG_ASSET_CLASS": REJECTED_MAPPING,
    "WRONG_SEGMENT": REJECTED_MAPPING,
    "UNSUPPORTED_DIRECTION": REJECTED_MAPPING,
    "INSUFFICIENT_EVIDENCE": REJECTED_EVIDENCE,
    "TOO_BROAD_FOR_CELL": REJECTED_MAPPING,
    "DUPLICATE_MAPPING": REJECTED_MAPPING,
    "IRRELEVANT_DOCUMENT": IRRELEVANT_DOCUMENT,
    "MANUAL_REVIEW_REQUIRED": MANUAL_REVIEW_REQUIRED,
}


def apply_strict_second_review_status_migration(
    session: Session,
    report_path: Path | None = None,
    reviewer_label: str = "strict_second_review_migration",
) -> dict[str, Any]:
    """Apply accepted second-review statuses to production ResearchView rows.

    This is intentionally status-only: it does not create views, recalculate coverage,
    rebuild Current MAE, or run Final Controller.
    """
    report_path = report_path or ROOT_DIR / "outputs" / "production_cleanup" / "financial_mapping_second_review_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    timestamp = datetime.now(UTC).isoformat()
    changes: list[dict[str, Any]] = []

    for row in report.get("previously_verified_second_review", []):
        view = session.get(ResearchView, row["research_view_id"])
        if view is None or view.is_demo:
            continue
        target = SECOND_REVIEW_TO_PRODUCTION_STATUS.get(row["second_review_status"], MANUAL_REVIEW_REQUIRED)
        _set_status(
            session,
            view,
            target,
            strict_status=row["second_review_status"],
            reviewer_label=reviewer_label,
            comment=f"[{timestamp}] Strict second review migrated previous VERIFIED_PASS to {target}. Reason: {row.get('primary_failure_reason', '')[:500]}",
            changes=changes,
        )

    invalid_rows = report.get("invalid_mapping_second_review_actions", [])
    for row in invalid_rows:
        view = session.get(ResearchView, row["research_view_id"])
        if view is None or view.is_demo:
            continue
        target = MANUAL_REVIEW_REQUIRED if row.get("revised_action_group") == "MANUAL_REVIEW_REQUIRED" else REJECTED_MAPPING
        _set_status(
            session,
            view,
            target,
            strict_status=row.get("revised_action_group", ""),
            reviewer_label=reviewer_label,
            comment=f"[{timestamp}] Strict second review migrated previous INVALID_MAPPING to {target}. Reason: {row.get('strict_action_reason', '')[:500]}",
            changes=changes,
        )

    for view in session.scalars(select(ResearchView).where(ResearchView.is_demo.is_(False))).all():
        if view.review_status == INVALID_EVIDENCE:
            _set_status(
                session,
                view,
                REJECTED_EVIDENCE,
                strict_status=REJECTED_EVIDENCE,
                reviewer_label=reviewer_label,
                comment=f"[{timestamp}] Strict status migration excludes INVALID_EVIDENCE from production calculations.",
                changes=changes,
            )
        elif view.review_status in {IRRELEVANT_DOCUMENT, BOILERPLATE}:
            if not view.legacy_review_status:
                view.legacy_review_status = view.review_status
            if not view.strict_review_status:
                view.strict_review_status = view.review_status
        elif view.review_status == VERIFIED_PASS:
            _set_status(
                session,
                view,
                ARCHIVED_LEGACY,
                strict_status=ARCHIVED_LEGACY,
                reviewer_label=reviewer_label,
                comment=f"[{timestamp}] Previous VERIFIED_PASS lacked second-review row and was archived as legacy candidate.",
                changes=changes,
            )

    counts = Counter(row.review_status for row in session.scalars(select(ResearchView).where(ResearchView.is_demo.is_(False))).all())
    result = {
        "updated": len(changes),
        "status_counts": dict(sorted(counts.items())),
        "changes_sample": changes[:25],
        "mutates_database": True,
        "recalculates_coverage": False,
        "rebuilds_current_mae": False,
        "runs_final_controller": False,
    }
    session.add(
        AppLog(
            level="WARNING",
            event="strict_second_review_status_migration",
            message="Production ResearchView statuses now reflect accepted strict second review; old VERIFIED_PASS is no longer financial confirmation.",
            context=result,
        )
    )
    session.flush()
    return result


def _set_status(
    session: Session,
    view: ResearchView,
    target: str,
    strict_status: str,
    reviewer_label: str,
    comment: str,
    changes: list[dict[str, Any]],
) -> None:
    previous = view.review_status
    if not view.legacy_review_status:
        view.legacy_review_status = previous
    view.strict_review_status = strict_status
    if previous == target:
        return
    view.review_status = target
    session.add(
        AnalystReview(
            entity_type="research_view",
            entity_id=view.id,
            previous_status=previous,
            new_status=target,
            comment=comment,
            reviewer_label=reviewer_label,
        )
    )
    changes.append(
        {
            "research_view_id": view.id,
            "previous_status": previous,
            "new_status": target,
            "strict_review_status": strict_status,
        }
    )
