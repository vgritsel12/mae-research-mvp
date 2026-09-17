from __future__ import annotations

from datetime import timedelta

from sqlalchemy import and_, desc, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.domain.enums import IdeaState, Materiality
from app.domain.models import Article, ChangeLog, ResearchView
from app.llm.provider import LLMProvider


class ChangeTrackerService:
    def __init__(self, provider: LLMProvider) -> None:
        self.provider = provider

    def track_changes(self, session: Session, view_id: str) -> ChangeLog:
        current = session.get(ResearchView, view_id)
        if current is None:
            raise ValueError("ResearchView not found")
        existing = session.scalar(select(ChangeLog).where(ChangeLog.current_view_id == view_id))
        if existing:
            return existing
        current_article = session.get(Article, current.article_id)
        if current_article is None:
            raise ValueError("Article not found for view")
        min_date = current_article.publication_date - timedelta(days=get_settings().previous_view_max_days)
        previous = session.scalar(
            select(ResearchView)
            .join(Article, Article.id == ResearchView.article_id)
            .where(
                and_(
                    ResearchView.id != current.id,
                    ResearchView.institution == current.institution,
                    ResearchView.region == current.region,
                    ResearchView.asset_class == current.asset_class,
                    ResearchView.asset_group == current.asset_group,
                    ResearchView.asset_segment == current.asset_segment,
                    Article.publication_date < current_article.publication_date,
                    Article.publication_date >= min_date,
                )
            )
            .order_by(desc(Article.publication_date))
        )
        if previous is None:
            log = ChangeLog(
                previous_view_id=None,
                current_view_id=current.id,
                change_types=["NEW_IDEA"],
                old_values={},
                new_values=self._view_snapshot(current),
                explanation="Первый сопоставимый взгляд в базе: создана NEW_IDEA.",
                materiality=Materiality.MEDIUM.value,
                machine_confidence=0.65,
                idea_state=IdeaState.NEW_IDEA.value,
                is_demo=current.is_demo,
            )
            session.add(log)
            session.flush()
            return log
        explanation = self.provider.summarize_change(previous, current)
        materiality = self.calculate_materiality(previous, current, explanation.change_types)
        idea_state = IdeaState.NO_MATERIAL_CHANGE if materiality == Materiality.NO_MATERIAL_CHANGE else IdeaState.MATERIAL_SHIFT
        log = ChangeLog(
            previous_view_id=previous.id,
            current_view_id=current.id,
            change_types=explanation.change_types,
            old_values=self._view_snapshot(previous),
            new_values=self._view_snapshot(current),
            explanation=explanation.explanation,
            materiality=materiality.value,
            machine_confidence=self.machine_confidence(previous, current),
            idea_state=idea_state.value,
            is_demo=current.is_demo,
        )
        session.add(log)
        session.flush()
        return log

    @staticmethod
    def calculate_materiality(previous: ResearchView, current: ResearchView, change_types: list[str]) -> Materiality:
        old_score = previous.position_score if previous.position_score is not None else 0
        new_score = current.position_score if current.position_score is not None else 0
        score_delta = abs(new_score - old_score)
        if previous.direction != current.direction or score_delta >= 2:
            return Materiality.HIGH
        if score_delta == 1 or {"NEW_DRIVER", "REMOVED_DRIVER", "RISK_CHANGE", "CATALYST_CHANGE", "CONFIDENCE_CHANGE"} & set(change_types):
            return Materiality.MEDIUM
        if change_types == ["NO_MATERIAL_CHANGE"] or not change_types:
            return Materiality.NO_MATERIAL_CHANGE
        return Materiality.LOW

    @staticmethod
    def machine_confidence(previous: ResearchView, current: ResearchView) -> float:
        score = 0.45
        if previous.evidence_quotes and current.evidence_quotes:
            score += 0.25
        if previous.horizon == current.horizon:
            score += 0.15
        if previous.template_row_key == current.template_row_key:
            score += 0.15
        return min(1.0, score)

    @staticmethod
    def _view_snapshot(view: ResearchView) -> dict:
        return {
            "direction": view.direction,
            "position_score": view.position_score,
            "confidence": view.confidence,
            "drivers": view.drivers,
            "risks": view.risks,
            "catalysts": view.catalysts,
        }

