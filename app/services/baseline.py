from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.domain.enums import AnalystStatus, CoverageStatus
from app.domain.models import Article, BaselineScore, BaselineSnapshot, MatrixScore, ResearchView, ReviewRun
from app.services.methodology import is_qualified_research_view
from app.services.normalization import (
    applicable_canonical_cells,
    canonical_cell_registry,
    clip_score,
    template_region,
    valid_score,
)
from app.services.production import is_content_valid_article, is_production_matrix_score, is_production_research_view


REGIONS = ["Global", "US", "Europe", "UK", "Japan", "EM ex China"]


@dataclass(frozen=True)
class CurrentMaeCell:
    canonical_cell_id: str
    template_row_key: str
    region: str
    applicability: str
    baseline_score: int | None
    proposed_adjustment: int | None
    validated_adjustment: int | None
    current_score: int | None
    coverage_status: str
    source_count: int
    publication_ids: list[str]
    publication_dates: list[str]
    supporting_evidence: list[dict[str, Any]]
    contradicting_evidence: list[dict[str, Any]]
    confidence: str
    freshness_score: float
    matrix_score_id: str | None = None


def ensure_baseline_snapshot(session: Session, settings: Settings | None = None) -> BaselineSnapshot | None:
    settings = settings or get_settings()
    path = settings.mae_template_xlsx
    if not path.exists():
        return None
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    existing = session.scalar(
        select(BaselineSnapshot).where(BaselineSnapshot.checksum == checksum).order_by(desc(BaselineSnapshot.created_at))
    )
    if existing:
        if not existing.active:
            _activate_snapshot(session, existing)
        _sync_baseline_scores(session, existing, path)
        return existing
    snapshot = BaselineSnapshot(
        source_file=str(path),
        source_sheet="ex",
        baseline_date=None,
        baseline_date_status="UNKNOWN",
        checksum=checksum,
        note="Imported from filled MAE template. Baseline source date was not present in the workbook.",
    )
    _activate_snapshot(session, snapshot)
    session.add(snapshot)
    session.flush()
    for row in _read_baseline_scores(path):
        session.add(
            BaselineScore(
                snapshot_id=snapshot.id,
                template_row_key=row["template_row_key"],
                canonical_cell_id=row["canonical_cell_id"],
                region=row["region"],
                baseline_score=row["baseline_score"],
                source_row=row["source_row"],
                source_column=row["source_column"],
            )
        )
    session.flush()
    return snapshot


def active_baseline_snapshot(session: Session) -> BaselineSnapshot | None:
    return session.scalar(select(BaselineSnapshot).where(BaselineSnapshot.active.is_(True)).order_by(desc(BaselineSnapshot.imported_at)))


def baseline_scores(session: Session) -> list[BaselineScore]:
    snapshot = active_baseline_snapshot(session)
    if snapshot is None:
        return []
    return list(
        session.scalars(
            select(BaselineScore)
            .where(BaselineScore.snapshot_id == snapshot.id, BaselineScore.deprecated.is_(False))
            .order_by(BaselineScore.template_row_key, BaselineScore.region)
        ).all()
    )


def current_mae_cells(session: Session, mode: str = "REAL") -> list[CurrentMaeCell]:
    mode_value = getattr(mode, "value", mode)
    baseline = {(row.template_row_key, template_region(row.region)): row for row in baseline_scores(session)}
    matrix_rows = [
        row
        for row in session.scalars(select(MatrixScore).order_by(MatrixScore.template_row_key, MatrixScore.region)).all()
        if _mode_matches(row.is_demo, mode_value)
        and (mode_value != "REAL" or is_production_matrix_score(row, session))
    ]
    matrix = {(row.template_row_key, template_region(row.region)): row for row in matrix_rows}
    canonical_cells = canonical_cell_registry(include_not_applicable=True)
    cells: list[CurrentMaeCell] = []
    for canonical in canonical_cells:
        key = (canonical.template_row_key, template_region(canonical.region))
        base = baseline.get(key)
        fresh = matrix.get(key)
        if canonical.applicability == CoverageStatus.NOT_APPLICABLE.value:
            proposed = None
            validated = None
            current = None
            coverage_status = CoverageStatus.NOT_APPLICABLE.value
        else:
            proposed = _proposed_adjustment(fresh)
            validated = _validated_adjustment(fresh)
            current = _current_score(base.baseline_score if base else None, validated)
            coverage_status = fresh.coverage_status if fresh else CoverageStatus.NO_DATA.value
        cells.append(
            CurrentMaeCell(
                canonical_cell_id=canonical.canonical_cell_id,
                template_row_key=key[0],
                region=key[1],
                applicability=canonical.applicability,
                baseline_score=base.baseline_score if base else None,
                proposed_adjustment=proposed,
                validated_adjustment=validated,
                current_score=current,
                coverage_status=coverage_status,
                source_count=fresh.source_count if fresh else 0,
                publication_ids=fresh.publication_ids if fresh else [],
                publication_dates=fresh.publication_dates if fresh else [],
                supporting_evidence=fresh.supporting_evidence if fresh else [],
                contradicting_evidence=fresh.contradicting_evidence if fresh else [],
                confidence=fresh.confidence if fresh else "LOW",
                freshness_score=fresh.freshness_score if fresh else 0.0,
                matrix_score_id=fresh.id if fresh else None,
            )
        )
    return cells


def mae_status_metrics(session: Session, mode: str = "REAL") -> dict[str, Any]:
    mode_value = getattr(mode, "value", mode)
    cells = current_mae_cells(session, mode)
    applicable_cells = [cell for cell in cells if cell.applicability == "APPLICABLE"]
    fresh_rows = [
        cell
        for cell in applicable_cells
        if cell.coverage_status not in {CoverageStatus.NO_DATA.value, CoverageStatus.NOT_APPLICABLE.value}
    ]
    raw_covered = fresh_rows
    qualified_covered = [
        cell
        for cell in applicable_cells
        if cell.coverage_status == CoverageStatus.COVERED.value and cell.source_count >= 2 and bool(cell.supporting_evidence)
    ]
    validated = [cell for cell in cells if cell.validated_adjustment is not None]
    proposed = [cell for cell in cells if cell.proposed_adjustment is not None]
    baseline = active_baseline_snapshot(session)
    latest_review = latest_review_run(session, "financial")
    review_passed = latest_review is not None and latest_review.verdict in {
        "FINANCIAL PASS",
        "FINANCIAL PASS WITH NO APPROVED SHIFTS",
        "PASS WITH COMMENTS",
    }
    financial_reviewed_covered = qualified_covered if review_passed else []
    articles = [
        row
        for row in session.scalars(select(Article)).all()
        if _mode_matches(row.is_demo, mode_value) and (mode_value != "REAL" or is_content_valid_article(row))
    ]
    views = [
        row
        for row in session.scalars(select(ResearchView)).all()
        if _mode_matches(row.is_demo, mode_value)
        and (mode_value != "REAL" or is_production_research_view(row, session.get(Article, row.article_id)))
    ]
    qualified_views = [
        view
        for view in views
        if is_qualified_research_view(view, session.get(Article, view.article_id))
        and (mode_value != "REAL" or is_production_research_view(view, session.get(Article, view.article_id)))
    ]
    cell_count = len(applicable_cells)
    return {
        "baseline_date": baseline.baseline_date.isoformat() if baseline and baseline.baseline_date else None,
        "baseline_date_status": baseline.baseline_date_status if baseline else "MISSING",
        "total_canonical_cells": len(cells),
        "total_applicable_cells": len(applicable_cells),
        "not_applicable_cells": len([cell for cell in cells if cell.applicability == CoverageStatus.NOT_APPLICABLE.value]),
        "baseline_scores": len([cell for cell in applicable_cells if cell.baseline_score is not None]),
        "current_scores": len([cell for cell in applicable_cells if cell.current_score is not None]),
        "raw_publications": len(
            [
                article
                for article in articles
                if article.fetch_status
                in {
                    "FETCHED",
                    "PARSED",
                    "ANALYSED",
                    "MANUAL_TEXT",
                }
            ]
        ),
        "qualified_research_views": len(qualified_views),
        "raw_covered": len(raw_covered),
        "qualified_covered": len(qualified_covered),
        "financial_reviewed_covered": len(financial_reviewed_covered),
        "raw_coverage_pct": round(len(raw_covered) / cell_count * 100, 1) if cell_count else 0.0,
        "qualified_coverage_pct": round(len(qualified_covered) / cell_count * 100, 1) if cell_count else 0.0,
        "financial_reviewed_coverage_pct": round(len(financial_reviewed_covered) / cell_count * 100, 1) if cell_count else 0.0,
        "fresh_covered": len([cell for cell in fresh_rows if cell.coverage_status == CoverageStatus.COVERED.value]),
        "fresh_insufficient": len([cell for cell in fresh_rows if cell.coverage_status == CoverageStatus.INSUFFICIENT_EVIDENCE.value]),
        "fresh_no_data": len([cell for cell in applicable_cells if cell.coverage_status == CoverageStatus.NO_DATA.value]),
        "coverage_pct": round(len([cell for cell in fresh_rows if cell.coverage_status == CoverageStatus.COVERED.value]) / cell_count * 100, 1)
        if cell_count
        else 0.0,
        "proposed_shifts": len(proposed),
        "approved_shifts": len(validated),
        "review_pending": len([cell for cell in proposed if cell.validated_adjustment is None]),
        "financial_verdict": latest_review.verdict if latest_review else "NOT_RUN",
    }


def latest_review_run(session: Session, role: str) -> ReviewRun | None:
    return session.scalar(select(ReviewRun).where(ReviewRun.role == role).order_by(desc(ReviewRun.created_at)))


def write_review_run(
    session: Session,
    role: str,
    verdict: str,
    *,
    overall_quality: int | None = None,
    source_quality: int | None = None,
    evidence_to_score_logic: int | None = None,
    cross_matrix_consistency: int | None = None,
    practical_usefulness: int | None = None,
    critical_findings: list[dict[str, Any]] | None = None,
    cell_findings: list[dict[str, Any]] | None = None,
    run_metadata: dict[str, Any] | None = None,
) -> ReviewRun:
    review = ReviewRun(
        role=role,
        verdict=verdict,
        overall_quality=overall_quality,
        source_quality=source_quality,
        evidence_to_score_logic=evidence_to_score_logic,
        cross_matrix_consistency=cross_matrix_consistency,
        practical_usefulness=practical_usefulness,
        critical_findings=critical_findings or [],
        cell_findings=cell_findings or [],
        run_metadata=run_metadata or {},
    )
    session.add(review)
    session.flush()
    return review


def _read_baseline_scores(path: Path) -> list[dict[str, Any]]:
    return [
        {
            "template_row_key": cell.template_row_key,
            "canonical_cell_id": cell.canonical_cell_id,
            "region": cell.region,
            "baseline_score": valid_score(int(cell.baseline_score)),
            "source_row": cell.row_index,
            "source_column": cell.source_column,
        }
        for cell in applicable_canonical_cells(path)
        if cell.baseline_score is not None
    ]


def _sync_baseline_scores(session: Session, snapshot: BaselineSnapshot, path: Path) -> None:
    desired = {(row["template_row_key"], template_region(row["region"])): row for row in _read_baseline_scores(path)}
    existing = {
        (row.template_row_key, template_region(row.region)): row
        for row in session.scalars(select(BaselineScore).where(BaselineScore.snapshot_id == snapshot.id)).all()
    }
    for key, row in existing.items():
        if key not in desired:
            row.deprecated = True
    for key, payload in desired.items():
        row = existing.get(key)
        if row is None:
            session.add(
                BaselineScore(
                    snapshot_id=snapshot.id,
                    template_row_key=payload["template_row_key"],
                    canonical_cell_id=payload["canonical_cell_id"],
                    region=payload["region"],
                    baseline_score=payload["baseline_score"],
                    source_row=payload["source_row"],
                    source_column=payload["source_column"],
                )
            )
            continue
        row.canonical_cell_id = payload["canonical_cell_id"]
        row.region = payload["region"]
        row.baseline_score = payload["baseline_score"]
        row.source_row = payload["source_row"]
        row.source_column = payload["source_column"]
        row.deprecated = False
    session.flush()


def _activate_snapshot(session: Session, snapshot: BaselineSnapshot) -> None:
    for row in session.scalars(select(BaselineSnapshot).where(BaselineSnapshot.active.is_(True))).all():
        row.active = False
    snapshot.active = True


def _mode_matches(is_demo: bool, mode: str) -> bool:
    if mode == "DEMO":
        return is_demo
    if mode == "REAL":
        return not is_demo
    return True


def _proposed_adjustment(row: MatrixScore | None) -> int | None:
    if row is None:
        return None
    if row.suggested_score is not None:
        return row.suggested_score
    if row.approved_score is not None:
        return row.approved_score
    return None


def _validated_adjustment(row: MatrixScore | None) -> int | None:
    if row is None:
        return None
    if row.coverage_status != CoverageStatus.COVERED.value and not row.override_reason:
        return None
    if row.approved_score is None:
        return None
    return row.approved_score


def _current_score(baseline_score: int | None, validated_adjustment: int | None) -> int | None:
    if baseline_score is None and validated_adjustment is None:
        return None
    base = baseline_score or 0
    adjustment = validated_adjustment or 0
    return valid_score(int(clip_score(base + adjustment)))
