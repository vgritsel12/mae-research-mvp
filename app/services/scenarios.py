from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.domain.enums import AnalystStatus, IdeaState, ScenarioType
from app.domain.models import Article, ChangeLog, ResearchView, ScenarioCard
from app.llm.provider import LLMProvider
from app.services.semantic import scenario_consistency_warnings


class ScenarioEngine:
    def __init__(self, provider: LLMProvider) -> None:
        self.provider = provider

    def generate_scenarios(self, session: Session, change_id: str) -> list[ScenarioCard]:
        change = session.get(ChangeLog, change_id)
        if change is None:
            raise ValueError("ChangeLog not found")
        if change.idea_state == IdeaState.NO_MATERIAL_CHANGE.value:
            session.execute(delete(ScenarioCard).where(ScenarioCard.linked_change_id == change_id))
            session.flush()
            return []
        current = session.get(ResearchView, change.current_view_id)
        if current is None:
            raise ValueError("Current view not found")
        article = session.get(Article, current.article_id)
        context = {
            "change_id": change.id,
            "view_id": current.id,
            "asset": current.asset_segment,
            "region": current.region,
            "direction": current.direction,
            "position_score": current.position_score,
            "drivers": current.drivers,
            "risks": current.risks,
            "evidence_quotes": current.evidence_quotes,
            "source_reference": article.source_reference if article else "manual/demo",
            "article_excerpt": (article.content_text[:1600] if article else ""),
        }
        drafts = self.provider.generate_scenarios(context)
        seen_types = {d.scenario_type for d in drafts}
        if seen_types != {ScenarioType.BASE, ScenarioType.UPSIDE, ScenarioType.DOWNSIDE}:
            raise ValueError("Scenario Engine must create exactly BASE, UPSIDE and DOWNSIDE")
        self._assert_distinct(drafts)
        semantic_warnings = scenario_consistency_warnings(current, drafts)
        review_status = AnalystStatus.NEEDS_REVIEW.value if semantic_warnings else AnalystStatus.NEW.value
        existing_by_type: dict[str, ScenarioCard] = {}
        for card in session.scalars(select(ScenarioCard).where(ScenarioCard.linked_change_id == change_id)).all():
            if card.scenario_type in existing_by_type:
                session.delete(card)
            else:
                existing_by_type[card.scenario_type] = card
        scenarios: list[ScenarioCard] = []
        for draft in drafts:
            card = existing_by_type.get(draft.scenario_type.value)
            source_references = [ref.model_dump() for ref in draft.source_references]
            if semantic_warnings:
                source_references.append({"source_reference": article.source_reference if article else "manual/demo", "note": "semantic_review: " + " ".join(semantic_warnings)})
            payload = {
                "linked_change_id": change.id,
                "linked_view_id": current.id,
                "scenario_type": draft.scenario_type.value,
                "title": draft.title,
                "description": draft.description,
                "assumptions": draft.assumptions,
                "triggers": draft.triggers,
                "early_indicators": draft.early_indicators,
                "beneficiaries": draft.beneficiaries,
                "vulnerable_assets": draft.vulnerable_assets,
                "expected_reaction": draft.expected_reaction,
                "reversal_conditions": draft.reversal_conditions,
                "probability_band": draft.probability_band,
                "review_status": review_status,
                "source_references": source_references,
                "is_demo": change.is_demo,
            }
            if card is None:
                card = ScenarioCard(**payload)
                session.add(card)
            else:
                for key, value in payload.items():
                    setattr(card, key, value)
            scenarios.append(card)
        session.flush()
        return scenarios

    @staticmethod
    def _assert_distinct(drafts: list) -> None:
        titles = {d.title.strip().casefold() for d in drafts}
        reactions = {d.expected_reaction.strip().casefold() for d in drafts}
        if len(titles) != 3 or len(reactions) != 3:
            raise ValueError("Scenario cards are not distinct enough")
