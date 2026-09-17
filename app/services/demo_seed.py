from __future__ import annotations

from datetime import date

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.domain.enums import AnalystStatus, RunMode, ScenarioType
from app.domain.models import (
    AnalystReview,
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
    UpdateJob,
)
from app.llm.provider import MockLLMProvider
from app.services.changes import ChangeTrackerService
from app.services.collectors import create_article, get_or_create_manual_source, seed_sources
from app.services.evidence import EvidenceEngine
from app.services.extraction import ResearchExtractionService
from app.services.matrix import MatrixEngine
from app.services.scenarios import ScenarioEngine
from app.services.signals import ShiftSignalEngine


DEMO_ARTICLES = [
    (
        "DEMO Institution A",
        "DEMO Institution A T1 - US Growth baseline",
        date(2026, 5, 1),
        "DEMO ONLY. Позиция по US Growth нейтральная: спрос стабилен, но пересмотры прибыли смешанные.",
        "manual://demo/institution-a/t1",
    ),
    (
        "DEMO Institution A",
        "DEMO Institution A T2 - US Growth upgrade",
        date(2026, 6, 15),
        "DEMO ONLY. Мы повышаем US Growth до overweight: ускорение пересмотров прибыли и маржинальность стали сильнее.",
        "manual://demo/institution-a/t2",
    ),
    (
        "DEMO Institution B",
        "DEMO Institution B confirmation - US Growth",
        date(2026, 6, 20),
        "DEMO ONLY. Независимое подтверждение: US Growth получает поддержку от роста EPS и спроса на software.",
        "manual://demo/institution-b/confirm",
    ),
    (
        "DEMO Institution C",
        "DEMO Institution C conflict - US Growth valuation risk",
        date(2026, 6, 21),
        "DEMO ONLY. Конфликтующий взгляд: оценки US Growth повышают риск краткосрочного underweight.",
        "manual://demo/institution-c/conflict",
    ),
    (
        "DEMO FI Institution A",
        "DEMO FI Institution A T1 - Europe GOV Short Term baseline",
        date(2026, 5, 7),
        "DEMO ONLY. Европа, короткие госбумаги: нейтрально, ставки выглядят стабильными.",
        "manual://demo/fi-institution-a/t1",
    ),
    (
        "DEMO FI Institution A",
        "DEMO FI Institution A T2 - Europe GOV Short Term upgrade",
        date(2026, 6, 7),
        "DEMO ONLY. Европа, короткие госбумаги: overweight, дезинфляция ускорилась и ожидания снижения ставок усилились.",
        "manual://demo/fi-institution-a/t2",
    ),
    (
        "DEMO FI Institution B",
        "DEMO FI Institution B confirmation - Europe GOV Short Term",
        date(2026, 6, 12),
        "DEMO ONLY. Независимое подтверждение: Europe GOV Short Term получает поддержку от снижения front-end yields.",
        "manual://demo/fi-institution-b/confirm",
    ),
    (
        "DEMO Gold Institution A",
        "DEMO Gold Institution A T1 - Global Gold baseline",
        date(2026, 5, 10),
        "DEMO ONLY. Global Gold нейтрален: real yields и геополитический спрос сбалансированы.",
        "manual://demo/gold-institution-a/t1",
    ),
    (
        "DEMO Gold Institution A",
        "DEMO Gold Institution A T2 - Global Gold upgrade",
        date(2026, 6, 18),
        "DEMO ONLY. Global Gold повышен до overweight: real yields снижаются, спрос центральных банков сильный.",
        "manual://demo/gold-institution-a/t2",
    ),
    (
        "DEMO Gold Institution B",
        "DEMO Gold Institution B confirmation - Global Gold",
        date(2026, 6, 22),
        "DEMO ONLY. Независимое подтверждение: Global Gold поддержан спросом на защитные активы и ETF flows.",
        "manual://demo/gold-institution-b/confirm",
    ),
    (
        "DEMO Institution D",
        "DEMO Institution D stable negative T1 - Europe GOV Mid Term",
        date(2026, 5, 9),
        "DEMO ONLY. Europe GOV Mid Term нейтрально, ставки стабильны и драйверы не меняются.",
        "manual://demo/institution-d/t1",
    ),
    (
        "DEMO Institution D",
        "DEMO Institution D stable negative T2 - Europe GOV Mid Term",
        date(2026, 6, 9),
        "DEMO ONLY. Europe GOV Mid Term без материального изменения: ставки и инфляционный профиль стабильны.",
        "manual://demo/institution-d/t2",
    ),
]


def reset_demo_data(session: Session) -> None:
    for model in [
        MatrixScore,
    ]:
        session.execute(delete(model))
    for model in [
        ShiftSignal,
        ScenarioAssessment,
        EvidenceObservation,
        ScenarioCard,
        ChangeLog,
        ResearchView,
        Article,
        AnalystReview,
        UpdateJob,
        AppLog,
    ]:
        if hasattr(model, "is_demo"):
            session.execute(delete(model).where(model.is_demo.is_(True)))
    session.execute(delete(Source).where(Source.is_demo.is_(True)))
    session.flush()


def seed_demo(session: Session, reset: bool = False) -> dict:
    if reset:
        reset_demo_data(session)
    seed_sources(session)
    provider = MockLLMProvider()
    extractor = ResearchExtractionService(provider)
    tracker = ChangeTrackerService(provider)
    scenarios = ScenarioEngine(provider)
    evidence = EvidenceEngine()
    signals = ShiftSignalEngine(provider)
    matrix = MatrixEngine()

    articles: list[Article] = []
    for institution, title, pub_date, text, url in DEMO_ARTICLES:
        source = get_or_create_manual_source(session, institution, website=url, is_demo=True)
        article = create_article(session, source, title, pub_date, text, url=url, is_demo=True)
        articles.append(article)

    views: list[ResearchView] = []
    changes: list[ChangeLog] = []
    for article in sorted(articles, key=lambda a: a.publication_date):
        for view in extractor.extract_views(session, article.id):
            view.review_status = AnalystStatus.REVIEWED.value
            views.append(view)
            changes.append(tracker.track_changes(session, view.id))

    material_changes = [c for c in changes if c.idea_state == "MATERIAL_SHIFT"]
    no_material_changes = [c for c in changes if c.idea_state == "NO_MATERIAL_CHANGE"]
    scenario_cards: list[ScenarioCard] = []
    assessments: list[ScenarioAssessment] = []
    for change in material_changes:
        cards = scenarios.generate_scenarios(session, change.id)
        scenario_cards.extend(cards)
        for card in cards:
            _seed_observations_for_card(session, evidence, card)
            assessments.append(evidence.assess_scenario(session, card.id))

    shift_signals: list[ShiftSignal] = []
    for change in material_changes:
        signal = signals.generate_signal(session, change.id)
        if signal:
            signal.analyst_status = AnalystStatus.APPROVED.value
            signal.analyst_comment = "DEMO: утверждено для проверки end-to-end сценария."
            shift_signals.append(signal)
    scores = matrix.rebuild_matrix(session, mode=RunMode.DEMO.value)
    session.flush()
    return {
        "articles": len(articles),
        "views": len(views),
        "changes": len(changes),
        "material_changes": len(material_changes),
        "no_material_changes": len(no_material_changes),
        "scenarios": len(scenario_cards),
        "assessments": len(assessments),
        "signals": len(shift_signals),
        "matrix_scores": len(scores),
    }


def ensure_demo_seed(session: Session) -> dict:
    existing = session.scalar(select(Article).where(Article.is_demo.is_(True)))
    if existing:
        matrix_count = len(session.scalars(select(MatrixScore).where(MatrixScore.is_demo.is_(True))).all())
        if matrix_count == 0:
            MatrixEngine().rebuild_matrix(session, mode=RunMode.DEMO.value)
        return {"status": "exists", "matrix_scores": matrix_count}
    return seed_demo(session, reset=False)


def _seed_observations_for_card(session: Session, engine: EvidenceEngine, card: ScenarioCard) -> None:
    existing = session.scalar(select(EvidenceObservation).where(EvidenceObservation.scenario_id == card.id))
    if existing:
        return
    base_url = "manual://demo/evidence"
    family = engine.scenario_asset_family(session, card)
    if family == "EUROPE_GOV_SHORT":
        rows = _fixed_income_evidence_rows(card.scenario_type)
    elif family == "GLOBAL_GOLD":
        rows = _gold_evidence_rows(card.scenario_type)
    else:
        rows = _growth_evidence_rows(card.scenario_type)
    for idx, (indicator, expected, actual, support, importance) in enumerate(rows, start=1):
        engine.add_observation(
            session,
            scenario_id=card.id,
            indicator=indicator,
            observation_date=date(2026, 6, 25),
            expected=expected,
            actual=actual,
            source_url=f"{base_url}/{card.scenario_type.lower()}/{idx}",
            support_value=support,
            importance=importance,
            surprise="DEMO",
            market_implied="DEMO",
            is_demo=True,
        )


def _growth_evidence_rows(scenario_type: str) -> list[tuple[str, str, str, int, str]]:
    if scenario_type == ScenarioType.BASE.value:
        return [
            ("DEMO EPS revisions", "положительные пересмотры EPS", "два апдейта подряд выше consensus", 1, "CRITICAL"),
            ("DEMO earnings guidance", "guidance компаний улучшается", "менеджмент повысил ориентиры по марже", 1, "STANDARD"),
            ("DEMO market breadth", "ширина роста улучшается", "нейтральная ширина внутри growth universe", 0, "STANDARD"),
        ]
    if scenario_type == ScenarioType.UPSIDE.value:
        return [
            ("DEMO software demand", "спрос на software выше ожиданий", "заказы выросли в DEMO-опросе", 1, "STANDARD"),
            ("DEMO semiconductor demand", "book-to-bill растет", "полупроводниковый спрос ускорился", 1, "STANDARD"),
            ("DEMO valuation", "valuation не мешает rerating", "мультипликатор уже высокий", -1, "STANDARD"),
        ]
    return [
        ("DEMO real yields", "real yields растут", "real yields не выросли критично", -1, "CRITICAL"),
        ("DEMO valuation", "valuation сжимается", "оценка остаётся высокой", -1, "STANDARD"),
        ("DEMO earnings guidance", "guidance ухудшается", "earnings guidance всё ещё стабилен", -1, "STANDARD"),
    ]


def _fixed_income_evidence_rows(scenario_type: str) -> list[tuple[str, str, str, int, str]]:
    if scenario_type == ScenarioType.BASE.value:
        return [
            ("DEMO ECB policy-rate expectations", "рынок ждёт cuts", "OIS закладывает более мягкий путь ЕЦБ", 1, "CRITICAL"),
            ("DEMO short-end sovereign yields", "2Y yields снижаются", "short-end sovereign yields снизились", 1, "STANDARD"),
            ("DEMO inflation actual versus consensus", "CPI ниже consensus", "inflation actual оказался ниже consensus", 1, "STANDARD"),
        ]
    if scenario_type == ScenarioType.UPSIDE.value:
        return [
            ("DEMO market-implied ECB cuts", "market-implied ECB cuts растут", "кривая закладывает дополнительные cuts", 1, "STANDARD"),
            ("DEMO PMI growth surprise", "PMI слабее ожиданий", "PMI/growth surprise отрицательный", 1, "STANDARD"),
            ("DEMO wage growth", "wage growth замедляется", "wage growth снизился умеренно", 1, "STANDARD"),
        ]
    return [
        ("DEMO inflation actual versus consensus", "CPI выше consensus", "inflation actual не подтвердил upside surprise", -1, "CRITICAL"),
        ("DEMO wage growth", "wage growth ускоряется", "wage growth стабилизировался", -1, "STANDARD"),
        ("DEMO ECB policy-rate expectations", "ЕЦБ звучит hawkish", "policy-rate expectations остались dovish", -1, "STANDARD"),
    ]


def _gold_evidence_rows(scenario_type: str) -> list[tuple[str, str, str, int, str]]:
    if scenario_type == ScenarioType.BASE.value:
        return [
            ("DEMO real yields", "real yields снижаются", "real yields снизились", 1, "CRITICAL"),
            ("DEMO central-bank purchases", "central-bank purchases устойчивы", "покупки центральных банков остаются высокими", 1, "STANDARD"),
            ("DEMO ETF flows", "ETF flows стабилизируются", "ETF flows стали нейтральными", 0, "STANDARD"),
        ]
    if scenario_type == ScenarioType.UPSIDE.value:
        return [
            ("DEMO geopolitical volatility", "geopolitical risk растёт", "volatility и risk aversion выросли", 1, "STANDARD"),
            ("DEMO ETF flows", "ETF inflows ускоряются", "ETF flows стали положительными", 1, "STANDARD"),
            ("DEMO inflation expectations", "inflation expectations растут", "breakevens умеренно выросли", 1, "STANDARD"),
        ]
    return [
        ("DEMO DXY", "DXY растёт", "DXY не пробил верхний диапазон", -1, "CRITICAL"),
        ("DEMO real yields", "real yields rebound", "real yields остались ниже", -1, "STANDARD"),
        ("DEMO central-bank purchases", "central-bank purchases падают", "покупки ЦБ устойчивы", -1, "STANDARD"),
    ]
