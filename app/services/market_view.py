from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.domain.enums import Confidence, CoverageStatus, EvidenceStatus, RunMode
from app.domain.models import (
    Article,
    ChangeLog,
    EvidenceItem,
    MaeComponentCell,
    MaeComponentSnapshot,
    MaeSnapshot,
    MaeSnapshotCell,
    MatrixScore,
    ResearchView,
    ScenarioAssessment,
    ScenarioCard,
    ShiftSignal,
)
from app.services.baseline import CurrentMaeCell, current_mae_cells
from app.services.component_engine import FINAL_COMPONENT, INTERIM_SNAPSHOT, INVALID_EMPTY_BACKFILL, component_history_series, latest_component_snapshot
from app.services.production import (
    is_content_valid_article,
    is_production_change,
    is_production_research_view,
    is_production_signal,
)


PUBLISHED_SIGNAL = "PUBLISHED_SIGNAL"
WATCHLIST = "WATCHLIST"
REJECTED = "REJECTED"
NON_PRODUCTION_SNAPSHOT_STATUSES = (INVALID_EMPTY_BACKFILL, "INVALID_FUTURE_DATE", "TEST_ONLY")

SCENARIO_ORDER = {"BASE": 0, "UPSIDE": 1, "DOWNSIDE": 2}
CONFIDENCE_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
EVIDENCE_RANK = {
    EvidenceStatus.CONFIRMED.value: 3,
    EvidenceStatus.MIXED.value: 2,
    EvidenceStatus.INSUFFICIENT_DATA.value: 1,
    EvidenceStatus.INSUFFICIENT_EVIDENCE.value: 1,
    EvidenceStatus.CONTRADICTED.value: 0,
}


@dataclass(frozen=True)
class ScenarioView:
    scenario_id: str | None
    scenario_type: str
    title: str
    probability: str
    narrative: str
    assumptions: list[str] = field(default_factory=list)
    triggers: list[str] = field(default_factory=list)
    early_indicators: list[str] = field(default_factory=list)
    beneficiaries: list[str] = field(default_factory=list)
    vulnerable_assets: list[str] = field(default_factory=list)
    expected_reaction: str = ""
    reversal_conditions: list[str] = field(default_factory=list)
    evidence_status: str = EvidenceStatus.INSUFFICIENT_DATA.value
    confidence: str = Confidence.LOW.value
    supporting_sources: list[str] = field(default_factory=list)
    contradicting_sources: list[str] = field(default_factory=list)
    updated_at: datetime | None = None


@dataclass(frozen=True)
class MarketSignalView:
    signal_id: str | None
    outcome: str
    topic: str
    primary_asset: str
    region: str
    current_view: str
    previous_view: str
    what_changed: str
    drivers: list[str]
    risks: list[str]
    scenarios: list[ScenarioView]
    probability_balance: str
    trigger: str
    veto: str
    evidence_status: str
    supporting_sources: list[str]
    contradicting_sources: list[str]
    mae_implication: int
    confidence: str
    transmission_chain: str
    updated_at: datetime | None


@dataclass(frozen=True)
class MarketThemeView:
    topic: str
    primary_asset: str
    region: str
    base_scenario: str
    probability: str
    what_changed: str
    main_effects: list[str]
    trigger: str
    veto: str
    affected_assets: list[str]
    sources: list[str]
    confidence: str
    evidence_status: str
    updated_at: datetime | None


@dataclass(frozen=True)
class MaeMatrixCellView:
    canonical_cell_id: str
    template_row_key: str
    region: str
    baseline_score: int | None
    automatic_adjustment: int | None
    current_score: int | None
    reason: str
    linked_topic: str
    confidence: str
    coverage_status: str
    source_count: int
    last_updated: datetime | None
    independent_source_count: int = 0
    score: int | None = None
    previous_score: int | None = None
    change: int | None = None
    benchmark: str = ""
    validation_status: str = ""
    validation_comment: str = ""
    source_details: list[dict[str, Any]] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    research_score: int | None = None
    data_score: int | None = None
    market_score: int | None = None
    divergence_status: str = ""
    component_coverage: list[str] = field(default_factory=list)
    research_coverage: str = ""
    asset_bucket: str = ""
    formula_version: str = ""
    factor_details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MarketOverviewView:
    themes: list[MarketThemeView]
    scenario_changes: list[MarketSignalView]
    winners: list[str]
    vulnerable_assets: list[str]
    key_risks: list[str]
    main_sources: list[str]
    last_updated: datetime | None
    published_signals: int
    watchlist_signals: int
    data_date: datetime | None = None
    history_series: list[dict[str, Any]] = field(default_factory=list)


def build_market_overview(session: Session) -> MarketOverviewView:
    component = latest_component_snapshot(session)
    if component:
        cells = build_mae_matrix_view(session)
        scored = [cell for cell in cells if cell.score is not None]
        changes = _component_changes(cells)[:4]
        positives = sorted([cell for cell in scored if (cell.score or 0) > 0], key=lambda cell: cell.score or 0, reverse=True)[:3]
        negatives = sorted([cell for cell in scored if (cell.score or 0) < 0], key=lambda cell: cell.score or 0)[:3]
        sources = _unique(
            source.get("provider", "")
            for cell in cells
            for source in cell.source_details
            if source.get("provider")
        )[:8]
        return MarketOverviewView(
            themes=_themes_from_component_cells(scored)[:8],
            scenario_changes=changes,
            winners=[_cell_asset_label(cell) for cell in positives],
            vulnerable_assets=[_cell_asset_label(cell) for cell in negatives],
            key_risks=_component_risks(scored)[:3],
            main_sources=sources,
            last_updated=component.updated_at,
            published_signals=len(scored),
            watchlist_signals=0,
            data_date=datetime.combine(component.snapshot_date, datetime.min.time()),
            history_series=component_history_series(session),
        )
    latest = latest_mae_snapshot(session)
    if latest:
        cells = build_mae_matrix_view(session)
        changes = _snapshot_changes(cells)[:4]
        positives = [cell for cell in cells if cell.score is not None and cell.score > 0][:3]
        negatives = [cell for cell in cells if cell.score is not None and cell.score < 0][:3]
        sources = _unique(
            source.get("provider", "")
            for cell in cells
            for source in cell.source_details
        )[:8]
        return MarketOverviewView(
            themes=_themes_from_snapshot_cells(cells)[:5],
            scenario_changes=changes,
            winners=[_cell_asset_label(cell) for cell in positives],
            vulnerable_assets=[_cell_asset_label(cell) for cell in negatives],
            key_risks=_snapshot_risks(cells)[:3],
            main_sources=sources,
            last_updated=latest.updated_at,
            published_signals=len(changes),
            watchlist_signals=0,
            data_date=datetime.combine(latest.snapshot_date, datetime.min.time()),
            history_series=mae_history_series(session),
        )
    signals = build_market_signals(session)
    themes = _themes_from_signals(signals)
    scenario_changes = signals[:3]
    winners = _unique(
        asset
        for signal in signals
        for scenario in signal.scenarios
        for asset in scenario.beneficiaries
    )[:8]
    vulnerable = _unique(
        asset
        for signal in signals
        for scenario in signal.scenarios
        for asset in scenario.vulnerable_assets
    )[:8]
    risks = _unique(
        risk
        for signal in signals
        for risk in ([signal.veto, *signal.risks] if signal.veto else signal.risks)
        if risk
    )[:8]
    sources = _unique(source for signal in signals for source in signal.supporting_sources)[:8]
    last_updated = max((signal.updated_at for signal in signals if signal.updated_at), default=None)
    return MarketOverviewView(
        themes=themes[:5],
        scenario_changes=scenario_changes,
        winners=winners,
        vulnerable_assets=vulnerable,
        key_risks=risks,
        main_sources=sources,
        last_updated=last_updated,
        published_signals=len([signal for signal in signals if signal.outcome == PUBLISHED_SIGNAL]),
        watchlist_signals=len([signal for signal in signals if signal.outcome == WATCHLIST]),
    )


def build_market_signals(session: Session) -> list[MarketSignalView]:
    signals = [
        _signal_view(session, signal)
        for signal in session.scalars(select(ShiftSignal).order_by(desc(ShiftSignal.updated_at))).all()
        if is_production_signal(signal, session)
    ]
    signals = [signal for signal in signals if signal.outcome != REJECTED]
    if not signals:
        signals = _watchlist_from_research_views(session)
    return sorted(signals, key=_signal_sort_key, reverse=True)


def build_mae_matrix_view(session: Session) -> list[MaeMatrixCellView]:
    component = latest_component_snapshot(session)
    if component:
        return _matrix_from_component_snapshot(session, component)
    latest = latest_mae_snapshot(session)
    if latest:
        return _matrix_from_snapshot(session, latest)
    cells = [
        cell
        for cell in current_mae_cells(session, RunMode.REAL.value)
        if cell.applicability == "APPLICABLE"
    ]
    matrix_by_id = {
        row.id: row
        for row in session.scalars(select(MatrixScore)).all()
        if row.id
    }
    signals_by_id = {
        row.id: row
        for row in session.scalars(select(ShiftSignal)).all()
        if row.id
    }
    result: list[MaeMatrixCellView] = []
    for cell in cells:
        matrix_row = matrix_by_id.get(cell.matrix_score_id or "")
        linked_signals = [
            signals_by_id[sid]
            for sid in (matrix_row.signal_ids if matrix_row else [])
            if sid in signals_by_id
        ]
        published_signals = [
            signal
            for signal in linked_signals
            if _signal_outcome(signal) == PUBLISHED_SIGNAL and is_production_signal(signal, session)
        ]
        adjustment = cell.validated_adjustment if published_signals and _mae_adjustment_allowed(cell, published_signals) else 0
        reason = _matrix_reason(cell, matrix_row, published_signals)
        topic = _signal_topic(published_signals[0], session) if published_signals else ""
        result.append(
            MaeMatrixCellView(
                canonical_cell_id=cell.canonical_cell_id,
                template_row_key=cell.template_row_key,
                region=cell.region,
                baseline_score=cell.baseline_score,
                automatic_adjustment=adjustment,
                current_score=cell.current_score if adjustment else cell.baseline_score,
                reason=reason,
                linked_topic=topic,
                confidence=cell.confidence,
                coverage_status=cell.coverage_status,
                source_count=cell.source_count,
                last_updated=matrix_row.updated_at if matrix_row else None,
                independent_source_count=cell.source_count,
                score=cell.current_score if adjustment else cell.baseline_score,
                previous_score=cell.baseline_score,
                change=adjustment or None,
                source_details=[],
                history=[],
            )
        )
    return result


def latest_mae_snapshot(session: Session) -> MaeSnapshot | None:
    return session.scalar(
        select(MaeSnapshot)
        .where(MaeSnapshot.is_demo.is_(False), MaeSnapshot.status.notin_(NON_PRODUCTION_SNAPSHOT_STATUSES))
        .order_by(desc(MaeSnapshot.snapshot_date))
    )


def previous_mae_snapshot(session: Session, snapshot: MaeSnapshot) -> MaeSnapshot | None:
    return session.scalar(
        select(MaeSnapshot)
        .where(
            MaeSnapshot.is_demo.is_(False),
            MaeSnapshot.snapshot_date < snapshot.snapshot_date,
            MaeSnapshot.status.notin_(NON_PRODUCTION_SNAPSHOT_STATUSES),
        )
        .order_by(desc(MaeSnapshot.snapshot_date))
    )


def mae_history_series(session: Session) -> list[dict[str, Any]]:
    component = component_history_series(session)
    if component:
        return component
    rows = []
    snapshots = list(
        session.scalars(
            select(MaeSnapshot)
            .where(MaeSnapshot.is_demo.is_(False), MaeSnapshot.status.notin_(NON_PRODUCTION_SNAPSHOT_STATUSES))
            .order_by(MaeSnapshot.snapshot_date)
        ).all()
    )
    for snapshot in snapshots:
        cells = list(session.scalars(select(MaeSnapshotCell).where(MaeSnapshotCell.snapshot_id == snapshot.id)).all())
        applicable = [cell for cell in cells if cell.applicability != "NOT_APPLICABLE"]
        rows.append(
            {
                "date": snapshot.snapshot_date,
                "positive": len([cell for cell in applicable if cell.mae_score is not None and cell.mae_score > 0]),
                "neutral": len([cell for cell in applicable if cell.mae_score == 0]),
                "negative": len([cell for cell in applicable if cell.mae_score is not None and cell.mae_score < 0]),
                "no_data": len([cell for cell in applicable if cell.mae_score is None]),
            }
        )
    return rows


def _matrix_from_component_snapshot(session: Session, snapshot: MaeComponentSnapshot) -> list[MaeMatrixCellView]:
    cells = list(
        session.scalars(
            select(MaeComponentCell)
            .where(MaeComponentCell.snapshot_id == snapshot.id)
            .order_by(MaeComponentCell.asset_bucket, MaeComponentCell.template_row_key, MaeComponentCell.region)
        ).all()
    )
    histories = _component_cell_histories(session)
    result: list[MaeMatrixCellView] = []
    for cell in cells:
        result.append(
            MaeMatrixCellView(
                canonical_cell_id=cell.canonical_cell_id,
                template_row_key=cell.template_row_key,
                region=cell.region,
                baseline_score=cell.previous_composite_score,
                automatic_adjustment=None,
                current_score=cell.composite_score,
                reason=cell.explanation,
                linked_topic="",
                confidence=cell.divergence_status,
                coverage_status="SCORED" if cell.composite_score is not None else cell.applicability,
                source_count=len(cell.source_details or []),
                last_updated=cell.updated_at,
                independent_source_count=len({row.get("provider_group") for row in (cell.source_details or []) if row.get("provider_group")}),
                score=cell.composite_score,
                previous_score=cell.previous_composite_score,
                change=cell.score_change,
                source_details=list(cell.source_details or []),
                history=histories.get(cell.canonical_cell_id, []),
                benchmark=cell.benchmark,
                validation_status=cell.validation_status,
                validation_comment=cell.validation_comment,
                research_score=cell.research_score,
                data_score=cell.data_score,
                market_score=cell.market_score,
                divergence_status=cell.divergence_status,
                component_coverage=list(cell.component_coverage or []),
                research_coverage=cell.research_coverage,
                asset_bucket=cell.asset_bucket,
                formula_version=cell.formula_version,
                factor_details=dict(cell.factor_details or {}),
            )
        )
    return result


def _component_cell_histories(session: Session) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    rows = list(
        session.scalars(
            select(MaeComponentCell)
            .join(MaeComponentSnapshot, MaeComponentSnapshot.id == MaeComponentCell.snapshot_id)
            .where(MaeComponentSnapshot.is_demo.is_(False), MaeComponentSnapshot.status.in_([FINAL_COMPONENT, INTERIM_SNAPSHOT]))
            .order_by(MaeComponentCell.canonical_cell_id, MaeComponentCell.snapshot_date)
        ).all()
    )
    for row in rows:
        result[row.canonical_cell_id].append(
            {
                "date": row.snapshot_date,
                "score": row.composite_score,
                "status": row.divergence_status,
                "components": row.component_coverage,
                "data_score": row.data_score,
                "market_score": row.market_score,
                "research_score": row.research_score,
            }
        )
    return result


def _matrix_from_snapshot(session: Session, snapshot: MaeSnapshot) -> list[MaeMatrixCellView]:
    cells = list(
        session.scalars(
            select(MaeSnapshotCell)
            .where(MaeSnapshotCell.snapshot_id == snapshot.id)
            .order_by(MaeSnapshotCell.template_row_key, MaeSnapshotCell.region)
        ).all()
    )
    evidence_by_cell: dict[str, list[EvidenceItem]] = defaultdict(list)
    for item in session.scalars(select(EvidenceItem).where(EvidenceItem.snapshot_id == snapshot.id)).all():
        if item.snapshot_cell_id:
            evidence_by_cell[item.snapshot_cell_id].append(item)
    histories = _cell_histories(session)
    result: list[MaeMatrixCellView] = []
    for cell in cells:
        sources = [_source_detail(item) for item in evidence_by_cell.get(cell.id, [])]
        result.append(
            MaeMatrixCellView(
                canonical_cell_id=cell.canonical_cell_id,
                template_row_key=cell.template_row_key,
                region=cell.region,
                baseline_score=cell.previous_snapshot_score,
                automatic_adjustment=cell.scenario_adjustment,
                current_score=cell.mae_score,
                reason=cell.explanation,
                linked_topic="",
                confidence="",
                coverage_status=cell.coverage_status,
                source_count=cell.source_count,
                last_updated=cell.updated_at,
                independent_source_count=cell.independent_source_count,
                score=cell.mae_score,
                previous_score=cell.previous_snapshot_score,
                change=cell.score_change,
                source_details=sources,
                history=histories.get(cell.canonical_cell_id, []),
            )
        )
    return result


def _source_detail(item: EvidenceItem) -> dict[str, Any]:
    return {
        "provider": item.provider,
        "title": item.report_title,
        "publication_date": item.publication_date,
        "url": item.url,
        "quote": item.exact_quote,
        "group": item.provider_group,
    }


def _cell_histories(session: Session) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    rows = list(
        session.scalars(
            select(MaeSnapshotCell)
            .join(MaeSnapshot, MaeSnapshot.id == MaeSnapshotCell.snapshot_id)
            .where(MaeSnapshot.is_demo.is_(False))
            .order_by(MaeSnapshotCell.canonical_cell_id, MaeSnapshotCell.snapshot_date)
        ).all()
    )
    for row in rows:
        result[row.canonical_cell_id].append(
            {
                "date": row.snapshot_date,
                "score": row.mae_score,
                "status": row.coverage_status,
                "sources": row.independent_source_count,
            }
        )
    return result


def _signal_view(session: Session, signal: ShiftSignal) -> MarketSignalView:
    change = session.get(ChangeLog, signal.change_id) if signal.change_id else None
    current = session.get(ResearchView, change.current_view_id) if change else None
    previous = session.get(ResearchView, change.previous_view_id) if change and change.previous_view_id else None
    scenarios = _scenarios_for_signal(session, signal)
    return MarketSignalView(
        signal_id=signal.id,
        outcome=_signal_outcome(signal),
        topic=_signal_topic(signal, session),
        primary_asset=signal.asset or signal.template_row_key,
        region=signal.region,
        current_view=_view_sentence(current),
        previous_view=_view_sentence(previous),
        what_changed=signal.what_changed or (change.explanation if change else ""),
        drivers=list(current.drivers[:5]) if current else [],
        risks=_unique([signal.key_risk, *(current.risks[:4] if current else [])]),
        scenarios=scenarios,
        probability_balance=_probability_balance(scenarios),
        trigger=signal.trigger,
        veto=signal.veto,
        evidence_status=signal.evidence_status,
        supporting_sources=_unique([*signal.source_urls, *[source for s in scenarios for source in s.supporting_sources]])[:6],
        contradicting_sources=_unique(source for s in scenarios for source in s.contradicting_sources)[:6],
        mae_implication=signal.suggested_strength if _signal_outcome(signal) == PUBLISHED_SIGNAL else 0,
        confidence=signal.confidence,
        transmission_chain=signal.transmission_chain,
        updated_at=signal.updated_at,
    )


def _watchlist_from_research_views(session: Session) -> list[MarketSignalView]:
    views = [
        view
        for view in session.scalars(select(ResearchView).order_by(desc(ResearchView.updated_at))).all()
        if is_production_research_view(view, session.get(Article, view.article_id))
    ][:5]
    watchlist: list[MarketSignalView] = []
    for view in views:
        article = session.get(Article, view.article_id)
        sources = [article.source_reference] if article and is_content_valid_article(article) else []
        watchlist.append(
            MarketSignalView(
                signal_id=None,
                outcome=WATCHLIST,
                topic=_view_topic(view),
                primary_asset=view.template_row_key,
                region=view.region,
                current_view=_view_sentence(view),
                previous_view="Предыдущий сопоставимый тезис не найден.",
                what_changed="Система обнаружила содержательный инвестиционный тезис, но сценарный набор пока недостаточно полон для изменения MAE.",
                drivers=list(view.drivers[:5]),
                risks=list(view.risks[:5]),
                scenarios=[],
                probability_balance="недостаточно данных",
                trigger=", ".join(view.catalysts[:2]),
                veto=", ".join(view.risks[:2]),
                evidence_status=EvidenceStatus.INSUFFICIENT_DATA.value,
                supporting_sources=sources,
                contradicting_sources=[],
                mae_implication=0,
                confidence=view.confidence,
                transmission_chain="Investment thesis → market expectations → potential asset impact. Scenario transmission needs more supporting data.",
                updated_at=view.updated_at,
            )
        )
    return watchlist


def _scenarios_for_signal(session: Session, signal: ShiftSignal) -> list[ScenarioView]:
    if not signal.change_id:
        return []
    assessments = {
        assessment.scenario_id: assessment
        for assessment in session.scalars(select(ScenarioAssessment)).all()
    }
    scenarios = list(
        session.scalars(
            select(ScenarioCard)
            .where(ScenarioCard.linked_change_id == signal.change_id)
            .order_by(ScenarioCard.scenario_type)
        ).all()
    )
    return [_scenario_view(scenario, assessments.get(scenario.id), signal.confidence) for scenario in sorted(scenarios, key=_scenario_sort_key)]


def _scenario_view(scenario: ScenarioCard, assessment: ScenarioAssessment | None, fallback_confidence: str) -> ScenarioView:
    supporting, contradicting = _scenario_sources(scenario)
    return ScenarioView(
        scenario_id=scenario.id,
        scenario_type=scenario.scenario_type,
        title=scenario.title,
        probability=scenario.probability_band or "MEDIUM",
        narrative=scenario.description,
        assumptions=list(scenario.assumptions or []),
        triggers=list(scenario.triggers or []),
        early_indicators=list(scenario.early_indicators or []),
        beneficiaries=list(scenario.beneficiaries or []),
        vulnerable_assets=list(scenario.vulnerable_assets or []),
        expected_reaction=scenario.expected_reaction,
        reversal_conditions=list(scenario.reversal_conditions or []),
        evidence_status=assessment.evidence_status if assessment else EvidenceStatus.INSUFFICIENT_DATA.value,
        confidence=fallback_confidence,
        supporting_sources=supporting,
        contradicting_sources=contradicting,
        updated_at=scenario.updated_at,
    )


def _scenario_sources(scenario: ScenarioCard) -> tuple[list[str], list[str]]:
    supporting: list[str] = []
    contradicting: list[str] = []
    for row in scenario.source_references or []:
        source = str(row.get("source_reference") or row.get("url") or row.get("source") or "").strip()
        note = str(row.get("note") or "").casefold()
        if not source:
            continue
        if "contradict" in note or "risk" in note:
            contradicting.append(source)
        else:
            supporting.append(source)
    return _unique(supporting), _unique(contradicting)


def _themes_from_signals(signals: list[MarketSignalView]) -> list[MarketThemeView]:
    themes: list[MarketThemeView] = []
    for signal in signals:
        base = next((scenario for scenario in signal.scenarios if scenario.scenario_type == "BASE"), None)
        effects = _unique(
            [
                signal.transmission_chain,
                *(scenario.expected_reaction for scenario in signal.scenarios if scenario.expected_reaction),
            ]
        )[:3]
        affected = _unique(
            [
                signal.primary_asset,
                *(asset for scenario in signal.scenarios for asset in scenario.beneficiaries),
                *(asset for scenario in signal.scenarios for asset in scenario.vulnerable_assets),
            ]
        )[:6]
        themes.append(
            MarketThemeView(
                topic=signal.topic,
                primary_asset=signal.primary_asset,
                region=signal.region,
                base_scenario=base.title if base else "Сценарий ещё формируется",
                probability=base.probability if base else signal.probability_balance,
                what_changed=signal.what_changed,
                main_effects=effects,
                trigger=signal.trigger,
                veto=signal.veto,
                affected_assets=affected,
                sources=signal.supporting_sources,
                confidence=signal.confidence,
                evidence_status=signal.evidence_status,
                updated_at=signal.updated_at,
            )
        )
    return themes


def _themes_from_snapshot_cells(cells: list[MaeMatrixCellView]) -> list[MarketThemeView]:
    themes: list[MarketThemeView] = []
    scored = [cell for cell in cells if cell.score is not None]
    changed = [cell for cell in scored if cell.change not in (None, 0)]
    source = changed or scored
    for cell in source[:5]:
        themes.append(
            MarketThemeView(
                topic=_cell_asset_label(cell),
                primary_asset=cell.template_row_key,
                region=cell.region,
                base_scenario="",
                probability="",
                what_changed=_cell_change_text(cell),
                main_effects=[cell.reason] if cell.reason else [],
                trigger="",
                veto="",
                affected_assets=[_cell_asset_label(cell)],
                sources=[row.get("provider", "") for row in cell.source_details],
                confidence="",
                evidence_status=cell.coverage_status,
                updated_at=cell.last_updated,
            )
        )
    return themes


def _snapshot_changes(cells: list[MaeMatrixCellView]) -> list[MarketSignalView]:
    changed = [cell for cell in cells if cell.change not in (None, 0)]
    changed.sort(key=lambda cell: abs(cell.change or 0), reverse=True)
    return [
        MarketSignalView(
            signal_id=None,
            outcome=PUBLISHED_SIGNAL,
            topic=_cell_asset_label(cell),
            primary_asset=cell.template_row_key,
            region=cell.region,
            current_view=_score_sentence(cell.score),
            previous_view=_score_sentence(cell.previous_score),
            what_changed=_cell_change_text(cell),
            drivers=[cell.reason] if cell.reason else [],
            risks=[],
            scenarios=[],
            probability_balance="",
            trigger="",
            veto="",
            evidence_status=cell.coverage_status,
            supporting_sources=[row.get("provider", "") for row in cell.source_details],
            contradicting_sources=[],
            mae_implication=cell.change or 0,
            confidence="",
            transmission_chain=cell.reason,
            updated_at=cell.last_updated,
        )
        for cell in changed[:4]
    ]


def _snapshot_risks(cells: list[MaeMatrixCellView]) -> list[str]:
    risks = []
    for cell in cells:
        if cell.score is not None and cell.score < 0:
            risks.append(_cell_asset_label(cell))
    return risks


def _themes_from_component_cells(cells: list[MaeMatrixCellView]) -> list[MarketThemeView]:
    themes: list[MarketThemeView] = []
    for cell in sorted(cells, key=lambda item: abs(item.score or 0), reverse=True):
        direction = "позитивный" if (cell.score or 0) > 0 else "негативный" if (cell.score or 0) < 0 else "нейтральный"
        themes.append(
            MarketThemeView(
                topic=f"{_cell_asset_label(cell)}: {direction} вектор",
                primary_asset=cell.template_row_key,
                region=cell.region,
                base_scenario=_component_status_label(cell.divergence_status),
                probability="",
                what_changed=cell.reason,
                main_effects=[
                    f"Данные: {_score_text(cell.data_score)}",
                    f"Рыночная динамика: {_score_text(cell.market_score)}",
                    f"Исследования: {_score_text(cell.research_score)}",
                ],
                trigger="",
                veto="",
                affected_assets=[_cell_asset_label(cell)],
                sources=[row.get("provider", "") for row in cell.source_details],
                confidence=cell.divergence_status,
                evidence_status=cell.divergence_status,
                updated_at=cell.last_updated,
            )
        )
    return themes


def _component_changes(cells: list[MaeMatrixCellView]) -> list[MarketSignalView]:
    changed = [cell for cell in cells if cell.score is not None and cell.change not in {None, 0}]
    if not changed:
        changed = [cell for cell in cells if cell.score is not None]
    result: list[MarketSignalView] = []
    for cell in sorted(changed, key=lambda item: (abs(item.change or 0), abs(item.score or 0)), reverse=True)[:8]:
        status = _component_status_label(cell.divergence_status)
        result.append(
            MarketSignalView(
                signal_id=cell.canonical_cell_id,
                outcome=PUBLISHED_SIGNAL,
                topic=f"{_cell_asset_label(cell)} · {status}",
                primary_asset=cell.template_row_key,
                region=cell.region,
                current_view=_score_sentence(cell.score),
                previous_view=_score_sentence(cell.previous_score),
                what_changed=_cell_change_text(cell),
                drivers=[
                    f"Данные: {_score_text(cell.data_score)}",
                    f"Рыночная динамика: {_score_text(cell.market_score)}",
                    f"Исследования: {_score_text(cell.research_score)}",
                ],
                risks=_factor_risks(cell),
                scenarios=[],
                probability_balance=status,
                trigger="",
                veto="",
                evidence_status=cell.divergence_status,
                supporting_sources=[row.get("provider", "") for row in cell.source_details],
                contradicting_sources=[],
                mae_implication=cell.score or 0,
                confidence=cell.divergence_status,
                transmission_chain=cell.reason,
                updated_at=cell.last_updated,
            )
        )
    return result


def _component_risks(cells: list[MaeMatrixCellView]) -> list[str]:
    risks = []
    for cell in cells:
        if cell.score is not None and cell.score < 0:
            risks.append(f"{_cell_asset_label(cell)}: {_component_status_label(cell.divergence_status)}")
    return risks


def _component_status_label(value: str) -> str:
    return {
        "CONFIRMED": "Подтверждённый вектор",
        "EARLY": "Ранняя идея",
        "PRICE_LED": "Рынок опережает аналитику",
        "NARRATIVE_ONLY": "Только аналитический нарратив",
        "MIXED": "Смешанный сигнал",
        "INSUFFICIENT": "Недостаточно подтверждений",
    }.get(value or "", "Недостаточно подтверждений")


def _factor_risks(cell: MaeMatrixCellView) -> list[str]:
    rows = []
    for section in ("data", "market"):
        for factor in (cell.factor_details or {}).get(section, []):
            if factor.get("score", 0) < 0:
                rows.append(str(factor.get("name", "")))
    return rows[:4]


def _cell_asset_label(cell: MaeMatrixCellView) -> str:
    segment = cell.template_row_key.split("|")[-1] if cell.template_row_key else ""
    return f"{cell.region} / {segment}"


def _cell_change_text(cell: MaeMatrixCellView) -> str:
    if cell.change is None and cell.previous_score is None and cell.score is not None:
        return "Новая оценка — в предыдущем срезе данных было недостаточно."
    if cell.change is None:
        return "Динамика появится после следующего обновления."
    if cell.change == 0:
        return "Оценка не изменилась."
    if cell.change > 0:
        return f"Оценка повышена с {_score_text(cell.previous_score)} до {_score_text(cell.score)}."
    return f"Оценка понижена с {_score_text(cell.previous_score)} до {_score_text(cell.score)}."


def _score_sentence(score: int | None) -> str:
    if score is None:
        return "Недостаточно данных для числовой оценки."
    if score == 0:
        return "Нейтральная оценка."
    if score > 0:
        return "Позитивная оценка."
    return "Негативная оценка."


def _score_text(score: int | None) -> str:
    if score is None:
        return "нет компонента"
    return f"+{score}" if score > 0 else str(score)


def _signal_outcome(signal: ShiftSignal) -> str:
    evidence_ok = signal.evidence_status in {EvidenceStatus.CONFIRMED.value, EvidenceStatus.MIXED.value}
    no_contradiction = signal.evidence_status != EvidenceStatus.CONTRADICTED.value
    has_transmission = bool((signal.transmission_chain or "").strip())
    has_direction = signal.suggested_strength != 0
    if signal.confidence == Confidence.HIGH.value and evidence_ok and has_transmission and has_direction:
        return PUBLISHED_SIGNAL
    if signal.confidence in {Confidence.HIGH.value, Confidence.MEDIUM.value} and no_contradiction:
        return WATCHLIST
    return REJECTED


def _mae_adjustment_allowed(cell: CurrentMaeCell, signals: list[ShiftSignal]) -> bool:
    return (
        cell.coverage_status != CoverageStatus.NOT_APPLICABLE.value
        and any(signal.confidence == Confidence.HIGH.value for signal in signals)
        and any(signal.evidence_status in {EvidenceStatus.CONFIRMED.value, EvidenceStatus.MIXED.value} for signal in signals)
        and bool(signals)
    )


def _matrix_reason(cell: CurrentMaeCell, matrix_row: MatrixScore | None, signals: list[ShiftSignal]) -> str:
    if signals:
        return signals[0].what_changed
    if matrix_row and matrix_row.override_reason:
        return matrix_row.override_reason
    if cell.coverage_status == CoverageStatus.NO_DATA.value:
        return "Нет опубликованного scenario-based сигнала для этой ячейки; используется baseline."
    if cell.coverage_status == CoverageStatus.INSUFFICIENT_EVIDENCE.value:
        return "Имеются наблюдения, но данных недостаточно для автоматического изменения MAE."
    return "Используется baseline до появления уверенного сценарного сигнала."


def _signal_topic(signal: ShiftSignal, session: Session) -> str:
    change = session.get(ChangeLog, signal.change_id) if signal.change_id else None
    current = session.get(ResearchView, change.current_view_id) if change else None
    if current:
        return _view_topic(current)
    return signal.asset or signal.template_row_key


def _view_topic(view: ResearchView) -> str:
    segment = view.asset_segment or view.asset_group or view.asset_class
    return f"{view.region} / {segment}"


def _view_sentence(view: ResearchView | None) -> str:
    if view is None:
        return "Нет сопоставимого предыдущего тезиса."
    score = view.position_score if view.position_score is not None else 0
    drivers = "; ".join((view.drivers or [])[:3])
    return f"{view.direction.lower()}, score {score:+d}. Drivers: {drivers or 'не указаны'}."


def _probability_balance(scenarios: list[ScenarioView]) -> str:
    if not scenarios:
        return "недостаточно данных"
    parts = [f"{scenario.scenario_type}: {scenario.probability}" for scenario in scenarios]
    return " · ".join(parts)


def _scenario_sort_key(scenario: ScenarioCard) -> int:
    return SCENARIO_ORDER.get(scenario.scenario_type, 9)


def _signal_sort_key(signal: MarketSignalView) -> tuple[int, int, float]:
    updated = signal.updated_at.timestamp() if signal.updated_at else 0.0
    return (
        1 if signal.outcome == PUBLISHED_SIGNAL else 0,
        CONFIDENCE_RANK.get(signal.confidence, 0) + EVIDENCE_RANK.get(signal.evidence_status, 0),
        updated,
    )


def _unique(values: Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result
