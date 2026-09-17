from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.enums import AnalystStatus, CoverageStatus, EvidenceStatus
from app.domain.models import Article, ChangeLog, MatrixScore, ResearchView, ScenarioAssessment, ScenarioCard, ShiftSignal, Source
from app.services.methodology import (
    BOILERPLATE,
    FAIL_RESEARCH_VIEW_STATUSES,
    IRRELEVANT_DOCUMENT,
    STRICT_VALIDATED,
    VERIFIED_PASS,
    assess_research_view,
    is_qualified_research_view,
)


ARCHIVED_TEST_DATA = "ARCHIVED_TEST_DATA"
REJECTED_QUALITY = "REJECTED_QUALITY"
PARSE_FAILED = "PARSE_FAILED"

INVALID_ARTICLE_STATUSES = {
    ARCHIVED_TEST_DATA,
    REJECTED_QUALITY,
    AnalystStatus.INVALIDATED.value,
    "INVALID",
    "DUPLICATE",
    "ERROR",
    "EVIDENCE_INVALID",
    PARSE_FAILED,
}
INVALID_REVIEW_STATUSES = {
    ARCHIVED_TEST_DATA,
    AnalystStatus.INVALIDATED.value,
    AnalystStatus.REJECTED.value,
    REJECTED_QUALITY,
    BOILERPLATE,
    IRRELEVANT_DOCUMENT,
    *FAIL_RESEARCH_VIEW_STATUSES,
}
INVALID_SIGNAL_STATUSES = {
    ARCHIVED_TEST_DATA,
    AnalystStatus.INVALIDATED.value,
    AnalystStatus.REJECTED.value,
}
INVALID_COVERAGE_STATUSES = {
    ARCHIVED_TEST_DATA,
    CoverageStatus.NOT_APPLICABLE.value,
}


def has_test_marker(*values: Any) -> bool:
    text = " ".join(str(value or "") for value in values).casefold()
    return any(
        marker in text
        for marker in (
            "demo",
            "manual://demo",
            "openai smoke",
            "smoke note",
            "test fixture",
            ARCHIVED_TEST_DATA.casefold(),
        )
    )


def is_archived_test_record(row: Any) -> bool:
    if bool(getattr(row, "is_demo", False)):
        return True
    if getattr(row, "category", None) == ARCHIVED_TEST_DATA:
        return True
    if getattr(row, "processing_status", None) == ARCHIVED_TEST_DATA:
        return True
    if getattr(row, "review_status", None) == ARCHIVED_TEST_DATA:
        return True
    if getattr(row, "analyst_status", None) == ARCHIVED_TEST_DATA:
        return True
    if getattr(row, "coverage_status", None) == ARCHIVED_TEST_DATA:
        return True
    return has_test_marker(
        getattr(row, "institution_name", ""),
        getattr(row, "title", ""),
        getattr(row, "source_reference", ""),
        getattr(row, "website", ""),
        getattr(row, "url", ""),
    )


def is_production_source(source: Source | None) -> bool:
    return bool(source) and not is_archived_test_record(source) and source.category != ARCHIVED_TEST_DATA


def is_content_valid_article(article: Article | None) -> bool:
    if article is None or is_archived_test_record(article):
        return False
    if article.processing_status in INVALID_ARTICLE_STATUSES:
        return False
    if article.source is not None and not is_production_source(article.source):
        return False
    return True


def is_qualified_production_article(article: Article | None) -> bool:
    return is_content_valid_article(article) and article.processing_status == "ANALYSED"


def is_production_research_view(view: ResearchView | None, article: Article | None = None) -> bool:
    if view is None or is_archived_test_record(view):
        return False
    if view.review_status in INVALID_REVIEW_STATUSES:
        return False
    if view.review_status == VERIFIED_PASS:
        return False
    if view.review_status != STRICT_VALIDATED:
        return False
    article = article or getattr(view, "article", None)
    if not is_content_valid_article(article):
        return False
    if not view.canonical_cell_id:
        return False
    assessment = assess_research_view(view, article)
    if not assessment.passed:
        return False
    return is_qualified_research_view(view, article)


def production_article_ids(session: Session) -> set[str]:
    return {row.id for row in session.scalars(select(Article)).all() if is_content_valid_article(row)}


def production_view_ids(session: Session) -> set[str]:
    ids: set[str] = set()
    for view in session.scalars(select(ResearchView)).all():
        if is_production_research_view(view, session.get(Article, view.article_id)):
            ids.add(view.id)
    return ids


def is_production_change(change: ChangeLog | None, session: Session) -> bool:
    if change is None or is_archived_test_record(change):
        return False
    view = session.get(ResearchView, change.current_view_id)
    return is_production_research_view(view, session.get(Article, view.article_id) if view else None)


def production_change_ids(session: Session) -> set[str]:
    return {row.id for row in session.scalars(select(ChangeLog)).all() if is_production_change(row, session)}


def is_production_scenario(scenario: ScenarioCard | None, session: Session) -> bool:
    if scenario is None or is_archived_test_record(scenario):
        return False
    if scenario.review_status in INVALID_REVIEW_STATUSES:
        return False
    if scenario.linked_change_id:
        return scenario.linked_change_id in production_change_ids(session)
    if scenario.linked_view_id:
        view = session.get(ResearchView, scenario.linked_view_id)
        return is_production_research_view(view, session.get(Article, view.article_id) if view else None)
    return False


def production_scenario_ids(session: Session) -> set[str]:
    change_ids = production_change_ids(session)
    view_ids = production_view_ids(session)
    ids: set[str] = set()
    for scenario in session.scalars(select(ScenarioCard)).all():
        if is_archived_test_record(scenario) or scenario.review_status in INVALID_REVIEW_STATUSES:
            continue
        if scenario.linked_change_id and scenario.linked_change_id in change_ids:
            ids.add(scenario.id)
        elif scenario.linked_view_id and scenario.linked_view_id in view_ids:
            ids.add(scenario.id)
    return ids


def is_production_assessment(assessment: ScenarioAssessment | None, session: Session) -> bool:
    if assessment is None or is_archived_test_record(assessment):
        return False
    if assessment.evidence_status in {ARCHIVED_TEST_DATA, EvidenceStatus.INVALIDATED.value}:
        return False
    return assessment.scenario_id in production_scenario_ids(session)


def is_production_signal(signal: ShiftSignal | None, session: Session) -> bool:
    if signal is None or is_archived_test_record(signal):
        return False
    if signal.analyst_status in INVALID_SIGNAL_STATUSES:
        return False
    if signal.evidence_status in {ARCHIVED_TEST_DATA, EvidenceStatus.INVALIDATED.value}:
        return False
    if signal.change_id and signal.change_id not in production_change_ids(session):
        return False
    if any(has_test_marker(url) for url in signal.source_urls or []):
        return False
    return True


def is_production_matrix_score(row: MatrixScore | None, session: Session | None = None) -> bool:
    if row is None or is_archived_test_record(row):
        return False
    if row.coverage_status in INVALID_COVERAGE_STATUSES:
        return False
    if row.is_demo:
        return False
    return True


def status_note(existing: str | None, message: str) -> str:
    current = (existing or "").strip()
    if not current:
        return message[:2000]
    if message in current:
        return current[:2000]
    return f"{current}\n{message}"[:2000]
