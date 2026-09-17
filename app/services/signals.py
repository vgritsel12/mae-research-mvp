from __future__ import annotations

from datetime import timedelta

from sqlalchemy import and_, desc, select
from sqlalchemy.orm import Session

from app.domain.enums import AnalystStatus, Confidence, Direction, EvidenceStatus, IdeaState, SignalDirection
from app.domain.models import Article, ChangeLog, EvidenceObservation, ResearchView, ScenarioAssessment, ScenarioCard, ShiftSignal
from app.domain.models import utcnow
from app.llm.provider import LLMProvider
from app.services.security import validate_public_http_url
from app.services.semantic import signal_consistency_warnings
from app.services.methodology import STRICT_VALIDATED


class ShiftSignalEngine:
    def __init__(self, provider: LLMProvider) -> None:
        self.provider = provider

    def generate_signal(self, session: Session, change_id: str) -> ShiftSignal | None:
        change = session.get(ChangeLog, change_id)
        if change is None:
            raise ValueError("ChangeLog not found")
        if change.idea_state in {IdeaState.NO_MATERIAL_CHANGE.value, IdeaState.NEW_IDEA.value}:
            return None
        current = session.get(ResearchView, change.current_view_id)
        if current is None:
            raise ValueError("Current view not found")
        if current.review_status not in {STRICT_VALIDATED, AnalystStatus.REVIEWED.value, AnalystStatus.APPROVED.value}:
            return None
        scenarios = session.scalars(select(ScenarioCard).where(ScenarioCard.linked_change_id == change.id)).all()
        if len(scenarios) != 3:
            return None
        assessments = session.scalars(
            select(ScenarioAssessment).where(ScenarioAssessment.scenario_id.in_([s.id for s in scenarios]))
        ).all()
        if len(assessments) != 3:
            return None
        assessment_by_scenario = {a.scenario_id: a for a in assessments}
        dominant = max(scenarios, key=lambda s: assessment_by_scenario[s.id].weighted_score)
        dominant_assessment = assessment_by_scenario[dominant.id]
        if dominant_assessment.evidence_status not in {EvidenceStatus.CONFIRMED.value, EvidenceStatus.MIXED.value}:
            return None
        if not self._evidence_integrity_ok(session, scenarios, dominant, dominant_assessment, change.is_demo):
            return None
        if all(a.weighted_score == assessments[0].weighted_score for a in assessments):
            dominant_type = "UNRESOLVED"
        else:
            dominant_type = dominant.scenario_type
        direction = self._signal_direction(current.direction)
        strength = self._strength(current.direction, current.position_score)
        corroborating = self._corroborating_views(session, current)
        source_ids, source_urls = self._source_refs(session, [current, *corroborating])
        if not source_urls:
            return None
        if not change.is_demo and not all(_is_public_or_official(url) for url in source_urls):
            return None
        confidence = self._confidence(current.confidence, len(set(source_ids)))
        existing = session.scalar(select(ShiftSignal).where(ShiftSignal.change_id == change.id))
        what_changed = self._what_changed(change)
        context = {
            "asset": current.asset_segment,
            "direction": direction.value,
            "what_changed": what_changed,
            "dominant_scenario": dominant_type,
            "evidence_status": dominant_assessment.evidence_status,
            "pricing_status": dominant_assessment.pricing_status,
        }
        payload = dict(
            asset=current.asset_segment,
            template_row_key=current.template_row_key,
            canonical_cell_id=current.canonical_cell_id,
            region=current.region,
            direction=direction.value,
            suggested_strength=strength,
            what_changed=what_changed,
            dominant_scenario=dominant_type,
            why_now=self.provider.explain_signal(context),
            evidence_status=dominant_assessment.evidence_status,
            pricing_status=dominant_assessment.pricing_status,
            transmission_chain="Драйверы публикации -> ожидания прибыли/ставок -> относительная привлекательность актива",
            trigger=(dominant.triggers or ["обновление evidence"])[0],
            veto=(dominant.reversal_conditions or ["нет условий отмены"])[0],
            key_risk=(current.risks or ["недостаток evidence"])[0],
            next_review_date=(utcnow() + timedelta(days=30)).date(),
            confidence=confidence.value,
            source_ids=source_ids,
            source_urls=source_urls,
            is_demo=current.is_demo,
        )
        semantic_warnings = signal_consistency_warnings(current, payload)
        semantic_warnings.extend(
            f"Сценарий {scenario.scenario_type} требует проверки."
            for scenario in scenarios
            if scenario.review_status == AnalystStatus.NEEDS_REVIEW.value
        )
        review_status = AnalystStatus.NEEDS_REVIEW.value if semantic_warnings else AnalystStatus.NEW.value
        review_comment = " ".join(semantic_warnings)
        if existing:
            for key, value in payload.items():
                setattr(existing, key, value)
            if semantic_warnings:
                existing.analyst_status = AnalystStatus.NEEDS_REVIEW.value
                existing.analyst_comment = review_comment[:1000]
            session.flush()
            return existing
        signal = ShiftSignal(change_id=change.id, analyst_status=review_status, analyst_comment=review_comment[:1000], **payload)
        session.add(signal)
        session.flush()
        return signal

    @staticmethod
    def _signal_direction(direction: str) -> SignalDirection:
        if direction == Direction.BULLISH.value:
            return SignalDirection.POSITIVE
        if direction == Direction.BEARISH.value:
            return SignalDirection.NEGATIVE
        return SignalDirection.NEUTRAL

    @staticmethod
    def _strength(direction: str, score: int | None) -> int:
        if score is None:
            return 0
        if direction == Direction.BULLISH.value:
            return max(0, min(3, score))
        if direction == Direction.BEARISH.value:
            return min(0, max(-3, score))
        return 0

    @staticmethod
    def _confidence(base_confidence: str, unique_source_count: int) -> Confidence:
        if unique_source_count < 2:
            return Confidence.LOW
        if base_confidence == Confidence.HIGH.value:
            return Confidence.HIGH
        return Confidence.MEDIUM

    @staticmethod
    def _what_changed(change: ChangeLog) -> str:
        pieces = []
        if "DIRECTION_CHANGE" in change.change_types:
            pieces.append(f"направление {change.old_values.get('direction')} -> {change.new_values.get('direction')}")
        if "SCORE_CHANGE" in change.change_types:
            pieces.append(f"score {change.old_values.get('position_score')} -> {change.new_values.get('position_score')}")
        new_drivers = set(change.new_values.get("drivers") or []) - set(change.old_values.get("drivers") or [])
        if new_drivers:
            pieces.append("новый драйвер: " + ", ".join(sorted(new_drivers)))
        return "; ".join(pieces) or "новая аналитическая идея"

    @staticmethod
    def _corroborating_views(session: Session, current: ResearchView) -> list[ResearchView]:
        current_article = session.get(Article, current.article_id)
        current_date = current_article.publication_date if current_article else None
        query = (
            select(ResearchView)
            .join(Article, Article.id == ResearchView.article_id)
            .where(
                and_(
                    ResearchView.id != current.id,
                    ResearchView.region == current.region,
                    ResearchView.template_row_key == current.template_row_key,
                    ResearchView.direction == current.direction,
                    ResearchView.is_demo.is_(current.is_demo),
                    ResearchView.position_score.is_not(None),
                    ResearchView.review_status.in_([STRICT_VALIDATED, AnalystStatus.REVIEWED.value, AnalystStatus.APPROVED.value]),
                )
            )
            .order_by(desc(Article.publication_date))
        )
        if current_date:
            query = query.where(Article.publication_date <= current_date + timedelta(days=45))
        return list(session.scalars(query).all())

    @staticmethod
    def _source_refs(session: Session, views: list[ResearchView]) -> tuple[list[str], list[str]]:
        source_ids: list[str] = []
        source_urls: list[str] = []
        for view in views:
            article = session.get(Article, view.article_id)
            if article:
                if article.source_id not in source_ids:
                    source_ids.append(article.source_id)
                if article.source_reference and article.source_reference not in source_urls:
                    source_urls.append(article.source_reference)
        return source_ids, source_urls

    @staticmethod
    def _evidence_integrity_ok(
        session: Session,
        scenarios: list[ScenarioCard],
        dominant: ScenarioCard,
        dominant_assessment: ScenarioAssessment,
        is_demo_signal: bool,
    ) -> bool:
        for scenario in scenarios:
            rows = session.scalars(select(EvidenceObservation).where(EvidenceObservation.scenario_id == scenario.id)).all()
            if not is_demo_signal:
                if any(row.is_demo for row in rows):
                    return False
                if any(not _is_public_or_official(row.source_url) for row in rows):
                    return False
            if scenario.id == dominant.id and dominant_assessment.evidence_status == EvidenceStatus.CONFIRMED.value:
                valid_rows = [row for row in rows if row.source_url and row.actual and row.expected]
                if not 3 <= len(valid_rows) <= 5:
                    return False
        return True


def _is_public_or_official(url: str) -> bool:
    if url.startswith("official://"):
        return True
    ok, _ = validate_public_http_url(url)
    return ok
