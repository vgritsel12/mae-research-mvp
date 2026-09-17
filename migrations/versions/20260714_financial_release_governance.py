from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from sqlalchemy import Connection, inspect, select

from app.domain.models import FinancialReleaseRecord, FinancialReviewDecision


REVISION = "20260714_financial_release_governance"


def upgrade(connection: Connection) -> dict[str, int]:
    """Create governance tables and conservatively mirror legacy snapshot status.

    The source `mae_snapshots` rows are never updated. Backfill creates a separate
    quarantined record, preserving technical status without inferring financial or
    analyst approval.
    """

    FinancialReleaseRecord.__table__.create(bind=connection, checkfirst=True)
    FinancialReviewDecision.__table__.create(bind=connection, checkfirst=True)
    if not inspect(connection).has_table("mae_snapshots"):
        return {"legacy_rows": 0, "inserted": 0, "existing": 0}

    legacy_rows = connection.exec_driver_sql(
        "SELECT id, snapshot_date, status, is_demo FROM mae_snapshots ORDER BY snapshot_date, id"
    ).mappings().all()
    existing = set(
        connection.execute(
            select(FinancialReleaseRecord.__table__.c.subject_id).where(
                FinancialReleaseRecord.__table__.c.subject_type == "LEGACY_MAE_SNAPSHOT"
            )
        ).scalars()
    )
    now = datetime.now(UTC)
    inserted = 0
    for row in legacy_rows:
        subject_id = str(row["id"])
        if subject_id in existing:
            continue
        snapshot_date = row["snapshot_date"]
        if isinstance(snapshot_date, str):
            snapshot_date = date.fromisoformat(snapshot_date[:10])
        legacy_status = str(row["status"] or "")
        technical_status = "FAILED" if legacy_status.startswith("INVALID") else "PASSED"
        stable_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"mae:financial-release:legacy:{subject_id}"))
        connection.execute(
            FinancialReleaseRecord.__table__.insert().values(
                id=stable_id,
                subject_type="LEGACY_MAE_SNAPSHOT",
                subject_id=subject_id,
                snapshot_date=snapshot_date,
                technical_status=technical_status,
                financial_status="NOT_REVIEWED",
                analyst_review_status="NOT_REVIEWED",
                release_status="QUARANTINED",
                reviewer_actor_type="NONE",
                reviewer_identity="",
                reviewed_at=None,
                decision_reason=(
                    f"Legacy technical status {legacy_status or 'UNKNOWN'} preserved; "
                    "financial and analyst approval were not inferred."
                ),
                rulebook_version="",
                review_version="legacy_backfill_v1",
                baseline_manifest_hash="",
                source_content_hash="",
                candidate_content_hash="",
                artifact_manifest_hash="",
                required_artifacts_valid=False,
                unresolved_manual_review_count=0,
                rejection_count=0,
                is_demo=bool(row["is_demo"]),
                created_at=now,
                updated_at=now,
            )
        )
        inserted += 1
    return {"legacy_rows": len(legacy_rows), "inserted": inserted, "existing": len(existing)}


def downgrade(connection: Connection) -> None:
    del connection
    raise RuntimeError("Destructive downgrade is intentionally unsupported for financial audit governance tables.")


def postgres_ddl() -> dict[str, str]:
    """Return PostgreSQL DDL for review/tests without claiming a live integration run."""

    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    dialect = postgresql.dialect()
    return {
        FinancialReleaseRecord.__tablename__: str(
            CreateTable(FinancialReleaseRecord.__table__).compile(dialect=dialect)
        ),
        FinancialReviewDecision.__tablename__: str(
            CreateTable(FinancialReviewDecision.__table__).compile(dialect=dialect)
        ),
    }
