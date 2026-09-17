from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from sqlalchemy import func, select

from app.domain.enums import RunMode
from app.domain.models import Article, LLMCallLog
from app.llm.provider import LLMProviderError, LLMUsageLimitError, RuleBasedProvider, get_provider
from app.services.changes import ChangeTrackerService
from app.services.evidence import EvidenceEngine
from app.services.extraction import ResearchExtractionService
from app.services.matrix import MatrixEngine
from app.services.scenarios import ScenarioEngine
from app.services.signals import ShiftSignalEngine


class PipelineService:
    def __init__(self, session_key: str | None = None) -> None:
        self.run_id = str(uuid.uuid4())
        self.provider_error: LLMProviderError | None = None
        try:
            provider = get_provider(session_key=session_key, run_id=self.run_id)
        except LLMProviderError as exc:
            self.provider_error = exc
            provider = RuleBasedProvider()
        self.primary_provider = provider
        self.fallback_provider = RuleBasedProvider()
        self.extractor = ResearchExtractionService(provider)
        self.changes = ChangeTrackerService(provider)
        self.scenarios = ScenarioEngine(provider)
        self.evidence = EvidenceEngine()
        self.signals = ShiftSignalEngine(provider)
        self.matrix = MatrixEngine()

    def process_article(self, session: Session, article_id: str) -> dict:
        if self.provider_error is not None:
            article = session.get(Article, article_id)
            if article is not None:
                article.processing_status = "LLM_REQUIRED"
                article.error_message = (
                    "OpenAI не настроен или недоступен; анализ публикации не запущен: "
                    f"{str(self.provider_error)[:300]}"
                )
                session.flush()
            return {
                "views": 0,
                "changes": 0,
                "scenarios": 0,
                "assessments": 0,
                "signals": 0,
                "matrix_scores": 0,
                "llm_calls": 0,
                "run_id": self.run_id,
                "fallback": False,
                "blocked_reason": "LLM_REQUIRED",
            }
        try:
            return self._process_article(session, article_id, used_fallback=False)
        except (LLMUsageLimitError, LLMProviderError) as exc:
            article = session.get(Article, article_id)
            if article is not None:
                article.processing_status = "LLM_REQUIRED"
                article.error_message = (
                    "LLM недоступен; RuleBasedProvider не создаёт финальный Research View "
                    f"для рабочих публикаций: {str(exc)[:300]}"
                )
                session.flush()
            return {
                "views": 0,
                "changes": 0,
                "scenarios": 0,
                "assessments": 0,
                "signals": 0,
                "matrix_scores": 0,
                "llm_calls": int(
                    session.scalar(
                        select(func.count(LLMCallLog.id)).where(
                            LLMCallLog.run_id == self.run_id,
                            LLMCallLog.status != "BLOCKED",
                        )
                    )
                    or 0
                ),
                "run_id": self.run_id,
                "fallback": False,
                "blocked_reason": "LLM_REQUIRED",
            }

    def _process_article(self, session: Session, article_id: str, used_fallback: bool) -> dict:
        article = session.get(Article, article_id)
        if article is None:
            raise ValueError("Article not found")
        for service in [self.extractor, self.changes, self.scenarios, self.signals]:
            provider = getattr(service, "provider", None)
            attach = getattr(provider, "attach_session", None)
            if callable(attach):
                attach(session)
        views = self.extractor.extract_views(session, article_id)
        changes = []
        scenarios = []
        assessments = []
        signals = []
        for view in views:
            change = self.changes.track_changes(session, view.id)
            changes.append(change)
            cards = self.scenarios.generate_scenarios(session, change.id)
            scenarios.extend(cards)
            for card in cards:
                assessment = self.evidence.assess_scenario(session, card.id)
                assessments.append(assessment)
            signal = self.signals.generate_signal(session, change.id)
            if signal:
                signals.append(signal)
        matrix_mode = RunMode.DEMO.value if article.is_demo else RunMode.REAL.value
        matrix_scores = self.matrix.rebuild_matrix(session, mode=matrix_mode)
        article.processing_status = "ANALYSED" if views else "MANUAL_REQUIRED"
        return {
            "views": len(views),
            "changes": len(changes),
            "scenarios": len(scenarios),
            "assessments": len(assessments),
            "signals": len(signals),
            "matrix_scores": len(matrix_scores),
            "llm_calls": int(
                session.scalar(
                    select(func.count(LLMCallLog.id)).where(
                        LLMCallLog.run_id == self.run_id,
                        LLMCallLog.status != "BLOCKED",
                    )
                )
                or 0
            ),
            "run_id": self.run_id,
            "fallback": used_fallback,
        }

    def _use_fallback_provider(self) -> None:
        provider = self.fallback_provider
        self.extractor = ResearchExtractionService(provider)
        self.changes = ChangeTrackerService(provider)
        self.scenarios = ScenarioEngine(provider)
        self.signals = ShiftSignalEngine(provider)
