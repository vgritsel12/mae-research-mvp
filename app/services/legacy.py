from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.enums import AnalystStatus, EvidenceStatus
from app.domain.models import AppLog, Article, ChangeLog, ResearchView, ScenarioAssessment, ScenarioCard, ShiftSignal
from app.services.methodology import is_qualified_research_view
from app.services.production import is_content_valid_article, is_production_research_view


LEGACY_ECB_MARKERS = [
    "ecb.mg260709",
    "higher-for-longer",
    "rate hike",
]


def invalidate_legacy_ecb_misinterpretation(session: Session) -> int:
    """Exclude the known legacy ECB rate/cuts mismatch from validated MAE shifts."""
    invalidated = 0
    signals = list(session.scalars(select(ShiftSignal).where(ShiftSignal.is_demo.is_(False))).all())
    for signal in signals:
        haystack = " ".join(
            [
                signal.what_changed or "",
                signal.why_now or "",
                " ".join(signal.source_urls or []),
                signal.analyst_comment or "",
            ]
        ).casefold()
        if not any(marker in haystack for marker in LEGACY_ECB_MARKERS):
            continue
        if signal.analyst_status == AnalystStatus.INVALIDATED.value:
            continue
        signal.analyst_status = AnalystStatus.INVALIDATED.value
        signal.evidence_status = EvidenceStatus.INVALIDATED.value
        signal.analyst_comment = (
            "INVALIDATED: legacy ECB interpretation is excluded until semantic validation recreates the case."
        )
        invalidated += 1
        if signal.change_id:
            _invalidate_change_children(session, signal.change_id)
    if invalidated:
        session.add(
            AppLog(
                level="WARNING",
                event="legacy_ecb_invalidated",
                message="Legacy ECB signal/scenarios were excluded from Current MAE.",
                context={"signals": invalidated},
            )
        )
    session.flush()
    return invalidated


def normalize_legacy_article_statuses(session: Session) -> int:
    updated = 0
    article_ids_with_views: set[str] = set()
    for view in session.scalars(select(ResearchView)).all():
        article = session.get(Article, view.article_id)
        if article is not None and is_content_valid_article(article) and is_production_research_view(view, article):
            article_ids_with_views.add(view.article_id)
    for article in session.scalars(select(Article)).all():
        if article.id in article_ids_with_views and article.processing_status in {"VIEWS_EXTRACTED", "NEW"}:
            article.processing_status = "ANALYSED"
            updated += 1
    if updated:
        session.add(
            AppLog(
                level="INFO",
                event="legacy_article_statuses_normalized",
                message="Legacy articles with research views were marked ANALYSED for ingestion metrics.",
                context={"articles": updated},
            )
        )
    session.flush()
    return updated


def _invalidate_change_children(session: Session, change_id: str) -> None:
    change = session.get(ChangeLog, change_id)
    if change:
        change.explanation = f"{change.explanation}\n\n[INVALIDATED] Legacy ECB interpretation excluded from Current MAE."
    scenarios = list(session.scalars(select(ScenarioCard).where(ScenarioCard.linked_change_id == change_id)).all())
    for scenario in scenarios:
        scenario.review_status = AnalystStatus.INVALIDATED.value
        refs = list(scenario.source_references or [])
        refs.append(
            {
                "type": "legacy_invalidation",
                "note": "Scenario excluded from validated shifts until regenerated after semantic validation.",
            }
        )
        scenario.source_references = refs
        assessment = session.scalar(select(ScenarioAssessment).where(ScenarioAssessment.scenario_id == scenario.id))
        if assessment:
            assessment.evidence_status = EvidenceStatus.INVALIDATED.value
            assessment.explanation = (
                f"{assessment.explanation}\n\nINVALIDATED: legacy scenario excluded from validated MAE adjustment."
            )
