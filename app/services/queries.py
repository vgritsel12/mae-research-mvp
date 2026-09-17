from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.domain.models import (
    AppLog,
    Article,
    ChangeLog,
    EvidenceObservation,
    MatrixScore,
    ResearchView,
    ScenarioAssessment,
    ScenarioCard,
    ShiftSignal,
    Source,
    LLMCallLog,
    UpdateJob,
)
from app.domain.enums import ru
from app.services.production import (
    is_content_valid_article,
    is_production_change,
    is_production_matrix_score,
    is_production_research_view,
    is_production_scenario,
    is_production_signal,
    is_production_source,
    production_scenario_ids,
)


class QueryService:
    def dashboard(self, session: Session) -> dict[str, Any]:
        sources = [row for row in session.scalars(select(Source)).all() if is_production_source(row)]
        articles = [row for row in session.scalars(select(Article)).all() if is_content_valid_article(row)]
        views = [row for row in session.scalars(select(ResearchView)).all() if is_production_research_view(row, session.get(Article, row.article_id))]
        changes = [row for row in session.scalars(select(ChangeLog)).all() if is_production_change(row, session)]
        scenarios = [row for row in session.scalars(select(ScenarioCard)).all() if is_production_scenario(row, session)]
        signals = [row for row in session.scalars(select(ShiftSignal)).all() if is_production_signal(row, session)]
        return {
            "sources": len(sources),
            "articles": len(articles),
            "views": len(views),
            "changes": len(changes),
            "scenarios": len(scenarios),
            "signals": len(signals),
            "approved_signals": len([s for s in signals if s.analyst_status == "APPROVED"]),
            "errors": len([j for j in session.scalars(select(UpdateJob)).all() if j.failed]),
        }

    def sources(self, session: Session) -> list[dict[str, Any]]:
        return [
            {
                "id": s.id,
                "Институт": s.institution_name,
                "URL": s.website,
                "Категория": s.category,
                "Адаптер": s.adapter_type,
                "Активен": s.active,
                "Проверен": s.last_checked_at,
            }
            for s in session.scalars(select(Source).order_by(Source.institution_name)).all()
            if is_production_source(s)
        ]

    def articles(self, session: Session) -> list[dict[str, Any]]:
        rows = []
        for a in session.scalars(select(Article).order_by(desc(Article.publication_date))).all():
            if not is_content_valid_article(a):
                continue
            rows.append(
                {
                    "id": a.id,
                    "Дата": a.publication_date,
                    "Заголовок": a.title,
                    "Источник": a.source.institution_name,
                    "Статус": a.processing_status,
                    "Fetch": a.fetch_status,
                    "URL": a.source_reference,
                }
            )
        return rows

    def views(self, session: Session) -> list[dict[str, Any]]:
        return [
            {
                "id": v.id,
                "Институт": v.institution,
                "Регион": v.region,
                "Класс": v.asset_class,
                "Группа": v.asset_group,
                "Сегмент": v.asset_segment,
                "Направление": ru(v.direction),
                "Score": v.position_score,
                "Уверенность": ru(v.confidence),
                "Статус": ru(v.review_status),
                "Драйверы": "; ".join(v.drivers),
                "Источник": v.article.source_reference,
            }
            for v in session.scalars(select(ResearchView).order_by(desc(ResearchView.created_at))).all()
            if is_production_research_view(v, session.get(Article, v.article_id))
        ]

    def changes(self, session: Session) -> list[dict[str, Any]]:
        return [
            {
                "id": c.id,
                "Тип идеи": ru(c.idea_state),
                "Материальность": ru(c.materiality),
                "Типы изменений": ", ".join(c.change_types),
                "Machine confidence": round(c.machine_confidence, 2),
                "Объяснение": c.explanation,
            }
            for c in session.scalars(select(ChangeLog).order_by(desc(ChangeLog.created_at))).all()
            if is_production_change(c, session)
        ]

    def scenarios(self, session: Session) -> list[dict[str, Any]]:
        return [
            {
                "id": s.id,
                "change_id": s.linked_change_id,
                "Тип": s.scenario_type,
                "Заголовок": s.title,
                "Предпосылки": "; ".join(s.assumptions),
                "Триггеры": "; ".join(s.triggers),
                "Реакция": s.expected_reaction,
                "Reversal": "; ".join(s.reversal_conditions),
                "Статус": ru(s.review_status),
            }
            for s in session.scalars(select(ScenarioCard).order_by(ScenarioCard.linked_change_id, ScenarioCard.scenario_type)).all()
            if is_production_scenario(s, session)
        ]

    def evidence(self, session: Session) -> list[dict[str, Any]]:
        rows = []
        assessments = {a.scenario_id: a for a in session.scalars(select(ScenarioAssessment)).all()}
        scenario_ids = production_scenario_ids(session)
        for e in session.scalars(select(EvidenceObservation).order_by(desc(EvidenceObservation.observation_date))).all():
            if e.scenario_id not in scenario_ids:
                continue
            a = assessments.get(e.scenario_id)
            rows.append(
                {
                    "id": e.id,
                    "scenario_id": e.scenario_id,
                    "Индикатор": e.indicator,
                    "Дата": e.observation_date,
                    "Факт": e.actual,
                    "Вес": e.importance,
                    "Support": e.support_value,
                    "Evidence status": ru(a.evidence_status) if a else "",
                    "Pricing": ru(a.pricing_status) if a else "",
                    "Источник": e.source_url,
                }
            )
        return rows

    def signals(self, session: Session) -> list[dict[str, Any]]:
        return [
            {
                "id": s.id,
                "Актив": s.asset,
                "Регион": s.region,
                "Направление": ru(s.direction),
                "Strength": s.suggested_strength,
                "Что изменилось": s.what_changed,
                "Сценарий": s.dominant_scenario,
                "Evidence": ru(s.evidence_status),
                "Pricing": ru(s.pricing_status),
                "Уверенность": ru(s.confidence),
                "Статус": ru(s.analyst_status),
                "Источники": "\n".join(s.source_urls),
            }
            for s in session.scalars(select(ShiftSignal).order_by(desc(ShiftSignal.created_at))).all()
            if is_production_signal(s, session)
        ]

    def matrix(self, session: Session) -> list[dict[str, Any]]:
        return [
            {
                "id": m.id,
                "Row key": m.template_row_key,
                "Регион": m.region,
                "Suggested": m.suggested_score,
                "Approved": m.approved_score,
                "Override": m.override_reason,
                "Signals": ", ".join(m.signal_ids),
            }
            for m in session.scalars(select(MatrixScore).order_by(MatrixScore.template_row_key, MatrixScore.region)).all()
            if is_production_matrix_score(m, session)
        ]

    def matrix_for_mode(self, session: Session, mode: str) -> list[dict[str, Any]]:
        return self.matrix(session)

    def update_jobs(self, session: Session) -> list[dict[str, Any]]:
        return [
            {
                "id": j.id,
                "Старт": j.started_at,
                "Финиш": j.finished_at,
                "Статус": j.status,
                "Источников": j.total_sources,
                "Успешно": j.succeeded,
                "Ошибок": j.failed,
                "Кратко": j.error_summary,
            }
            for j in session.scalars(select(UpdateJob).order_by(desc(UpdateJob.started_at))).all()
        ]

    def app_logs(self, session: Session) -> list[dict[str, Any]]:
        return [
            {
                "Время": row.created_at,
                "Уровень": row.level,
                "Событие": row.event,
                "Сообщение": row.message,
            }
            for row in session.scalars(select(AppLog).order_by(desc(AppLog.created_at))).all()
        ]

    def llm_call_logs(self, session: Session) -> list[dict[str, Any]]:
        return [
            {
                "Время": row.created_at,
                "Модель": row.model,
                "Операция": row.operation,
                "Статус": row.status,
                "Input tokens": row.input_tokens,
                "Output tokens": row.output_tokens,
                "Ошибка": row.error_code,
            }
            for row in session.scalars(select(LLMCallLog).order_by(desc(LLMCallLog.created_at))).all()
        ]

    def status_counter(self, values: list[dict[str, Any]], key: str) -> dict[str, int]:
        return dict(Counter(str(row.get(key, "")) for row in values))
