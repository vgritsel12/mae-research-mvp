from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.enums import AnalystStatus
from app.domain.models import AppLog, Article, ResearchView
from app.llm.provider import LLMProvider
from app.services.methodology import append_article_error, research_view_draft_errors
from app.services.normalization import canonical_cell_id_for, is_applicable_canonical_cell, make_template_row_key, map_to_canonical_row_key, normalize_region
from app.services.semantic import evidence_quote_warnings
from app.services.staged_extraction import ConservativeStagedExtractionPipeline


class ResearchExtractionService:
    def __init__(self, provider: LLMProvider) -> None:
        self.provider = provider

    def extract_views(self, session: Session, article_id: str) -> list[ResearchView]:
        article = session.get(Article, article_id)
        if article is None:
            raise ValueError("Article not found")
        existing = session.scalars(select(ResearchView).where(ResearchView.article_id == article_id)).all()
        if existing:
            return list(existing)
        if not article.is_demo and not getattr(self.provider, "final_research_view_allowed", True):
            article.processing_status = "LLM_REQUIRED"
            append_article_error(
                article,
                "RuleBasedProvider используется только для discovery/preliminary tagging; финальный Research View не создан.",
            )
            session.flush()
            return []
        if not article.is_demo:
            pipeline = ConservativeStagedExtractionPipeline()
            views = pipeline.extract_research_views(session, article)
            if not views:
                append_article_error(
                    article,
                    "STRICT_PIPELINE: Research View не создан; требуется явный инвестиционный тезис, регион, горизонт, направление и transmission logic.",
                )
            return views
        drafts = self.provider.extract_research_views(article)
        views: list[ResearchView] = []
        rejected: list[str] = []
        unmapped: list[str] = []
        for draft in drafts:
            errors = research_view_draft_errors(article, draft)
            if errors:
                rejected.append(f"{draft.asset_class}/{draft.asset_group}/{draft.asset_segment}: {'; '.join(errors[:5])}")
                continue
            raw_row_key = make_template_row_key(draft.asset_class, draft.asset_group, draft.asset_segment)
            row_key = map_to_canonical_row_key(draft.asset_class, draft.asset_group, draft.asset_segment)
            region = normalize_region(draft.region)
            if row_key is None or not is_applicable_canonical_cell(row_key, region):
                candidate = f"{raw_row_key} / {region}"
                unmapped.append(candidate)
                continue
            cell_id = canonical_cell_id_for(row_key, region) or ""
            quote_payload = [q.model_dump() for q in draft.evidence_quotes]
            review_status = (
                AnalystStatus.NEEDS_REVIEW.value
                if evidence_quote_warnings(quote_payload, article.is_demo)
                else AnalystStatus.NEW.value
            )
            view = ResearchView(
                article_id=article.id,
                institution=draft.institution,
                horizon=draft.horizon,
                region=region,
                asset_class=row_key.split("|", 2)[0],
                asset_group=row_key.split("|", 2)[1],
                asset_segment=row_key.split("|", 2)[2],
                template_row_key=row_key,
                canonical_cell_id=cell_id,
                direction=draft.direction.value,
                position_score=draft.position_score,
                confidence=draft.confidence.value,
                drivers=draft.drivers,
                risks=draft.risks,
                catalysts=draft.catalysts,
                evidence_quotes=quote_payload,
                extraction_method=draft.extraction_method.value,
                review_status=review_status,
                is_demo=article.is_demo,
            )
            session.add(view)
            views.append(view)
        if views:
            article.processing_status = "VIEWS_EXTRACTED"
        else:
            article.processing_status = "EVIDENCE_INVALID" if drafts else "MANUAL_REQUIRED"
        if rejected:
            append_article_error(article, "Research View rejected by evidence gate: " + " | ".join(rejected)[:1500])
        if unmapped:
            append_article_error(article, "UNMAPPED_CANDIDATE: " + " | ".join(unmapped)[:1500])
            session.add(
                AppLog(
                    level="WARNING",
                    event="unmapped_candidate",
                    message="Extraction proposed categories outside the canonical MAE universe.",
                    context={"article_id": article.id, "candidates": unmapped[:20]},
                )
            )
        session.flush()
        return views
