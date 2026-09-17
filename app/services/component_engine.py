from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import shutil
import calendar
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import delete, desc, select
from sqlalchemy.orm import Session, object_session

from app.config import ROOT_DIR, get_settings
from app.domain.models import (
    Article,
    ExternalDataCache,
    MaeComponentCell,
    MaeComponentSnapshot,
    MaeSnapshot,
    ResearchView,
    Source,
    utcnow,
)
from app.services.mae_history import default_snapshot_dates, source_independence_group
from app.services.normalization import CanonicalCell, canonical_cell_registry, clip_score, template_region
from app.services.production import is_content_valid_article, is_production_research_view


FORMULA_VERSION = "component_v1"
EXPOSURE_MAP_VERSION = "exposure_v1"
APPLICABILITY_MAP_VERSION = "applicability_v1"
INVALID_EMPTY_BACKFILL = "INVALID_EMPTY_BACKFILL"
FINAL_COMPONENT = "FINAL_COMPONENT"
INTERIM_SNAPSHOT = "INTERIM_SNAPSHOT"
NOT_APPLICABLE = "NOT_APPLICABLE"
NO_DATA = "NO_DATA"
SCORED = "SCORED"
LIFECYCLE_CONFIG_VERSION = "research_lifecycle_v1"

ANNUAL_OUTLOOK = "ANNUAL_OUTLOOK"
MIDYEAR_OUTLOOK = "MIDYEAR_OUTLOOK"
QUARTERLY_OUTLOOK = "QUARTERLY_OUTLOOK"
ASSET_OUTLOOK = "ASSET_OUTLOOK"
MONTHLY_OUTLOOK = "MONTHLY_OUTLOOK"
WEEKLY_COMMENTARY = "WEEKLY_COMMENTARY"
CENTRAL_BANK = "CENTRAL_BANK"
RATING_OUTLOOK = "RATING_OUTLOOK"
NEWS_EVENT = "NEWS_EVENT"

ACTIVE = "ACTIVE"
AGING = "AGING"
SUPERSEDED = "SUPERSEDED"
INVALIDATED = "INVALIDATED"
EXPIRED = "EXPIRED"

DOCUMENT_TYPES = {
    ANNUAL_OUTLOOK,
    MIDYEAR_OUTLOOK,
    QUARTERLY_OUTLOOK,
    ASSET_OUTLOOK,
    MONTHLY_OUTLOOK,
    WEEKLY_COMMENTARY,
    CENTRAL_BANK,
    RATING_OUTLOOK,
    NEWS_EVENT,
}

RU_LIFECYCLE_STATUS = {
    ACTIVE: "Взгляд обновлён в текущем месяце",
    AGING: "Взгляд устаревает и требует обновления",
    SUPERSEDED: "Взгляд заменён новым материалом",
    INVALIDATED: "Взгляд больше не подтверждается данными или рынком",
    EXPIRED: "Срок действия материала истёк",
}

RU_DOCUMENT_TYPE = {
    ANNUAL_OUTLOOK: "годовой outlook",
    MIDYEAR_OUTLOOK: "полугодовой outlook",
    QUARTERLY_OUTLOOK: "квартальный outlook",
    ASSET_OUTLOOK: "asset outlook",
    MONTHLY_OUTLOOK: "месячный outlook",
    WEEKLY_COMMENTARY: "еженедельный комментарий",
    CENTRAL_BANK: "публикация центрального банка",
    RATING_OUTLOOK: "рейтинговый outlook",
    NEWS_EVENT: "новостное событие",
}

CONFIRMED = "CONFIRMED"
EARLY = "EARLY"
PRICE_LED = "PRICE_LED"
NARRATIVE_ONLY = "NARRATIVE_ONLY"
MIXED = "MIXED"
INSUFFICIENT = "INSUFFICIENT"

COMPONENT_WEIGHTS: dict[str, dict[str, float]] = {
    "EQUITY": {"data": 0.40, "research": 0.35, "market": 0.25},
    "GOVERNMENT_BONDS": {"data": 0.50, "research": 0.25, "market": 0.25},
    "CORPORATE_CREDIT": {"data": 0.40, "research": 0.25, "market": 0.35},
    "GOLD": {"data": 0.45, "research": 0.20, "market": 0.35},
}

CORE_UNIVERSE: set[tuple[str, str]] = {
    ("EQUITY|Wide Market|Wide Market", "US"),
    ("EQUITY|Other categories|Growth", "US"),
    ("EQUITY|Other categories|Market Breadth (Equal Weight)", "US"),
    ("EQUITY|Other categories|Small Cap", "US"),
    ("FIXED INCOME|GOV|Short Term", "US"),
    ("FIXED INCOME|GOV|Long Term", "US"),
    ("FIXED INCOME|CORP HY|Mid Term", "US"),
    ("FIXED INCOME|CORP IG|Mid Term", "US"),
    ("FIXED INCOME|Other categories|Inflation Linked", "US"),
    ("COMMODITIES|Commodities|Gold", "Global"),
}

FRED_SERIES = {
    "SP500": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=SP500",
    "NASDAQCOM": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=NASDAQCOM",
    "DGS2": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS2",
    "DGS10": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10",
    "DFII10": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DFII10",
    "T10Y2Y": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=T10Y2Y",
    "T10YIE": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=T10YIE",
    "BAMLH0A0HYM2": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=BAMLH0A0HYM2",
    "BAMLC0A0CM": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=BAMLC0A0CM",
    "NFCI": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=NFCI",
    "VIXCLS": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=VIXCLS",
    "CPIAUCSL": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=CPIAUCSL",
    "UNRATE": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=UNRATE",
    "INDPRO": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=INDPRO",
    "FEDFUNDS": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=FEDFUNDS",
    "DTWEXBGS": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DTWEXBGS",
    "CP": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=CP",
    "NFCICREDIT": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=NFCICREDIT",
    "NFCILEVERAGE": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=NFCILEVERAGE",
    "NFCINONFINLEVERAGE": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=NFCINONFINLEVERAGE",
    "IQ12260": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=IQ12260",
    "GVZCLS": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=GVZCLS",
}

MONTHLY_RELEASE_LAG_DAYS = 21
STRICT_RESEARCH_LOOKBACK_DAYS = 90
DIRECT_RESEARCH = "DIRECT"
SEMI_DIRECT_RESEARCH = "SEMI_DIRECT"
CONTEXT_ONLY_RESEARCH = "CONTEXT_ONLY"
REJECTED_RESEARCH = "REJECTED"
RESEARCH_LINEAGE_RULES = {
    "direct_cell": DIRECT_RESEARCH,
    "same_row_global_to_region": SEMI_DIRECT_RESEARCH,
    "same_row_region_to_global": SEMI_DIRECT_RESEARCH,
}
RESEARCH_ROLE_KEYS = {"research", "institutional_research", "ratings_credit"}
NON_RESEARCH_ROLE_KEYS = {"event_verification", "geopolitical_context", "consulting_context"}


@dataclass(frozen=True)
class TimePoint:
    observation_date: date
    value: float


@dataclass(frozen=True)
class ResearchLifecycleAssessment:
    document_type: str
    valid_from: date
    valid_until: date
    lifecycle_status: str
    freshness_weight: float
    superseded_by: str
    invalidation_reason: str
    last_confirmed_at: datetime | None
    carried_forward: bool
    review_due: date
    rejection_reason: str = ""


@dataclass(frozen=True)
class CoreCellModel:
    row_key: str
    region: str
    benchmark: str
    benchmark_series: str
    data_indicators: list[str]
    market_indicators: list[str]
    research_indicators: list[str]
    formula: str


CORE_MODELS: dict[tuple[str, str], CoreCellModel] = {
    ("EQUITY|Wide Market|Wide Market", "US"): CoreCellModel(
        "EQUITY|Wide Market|Wide Market",
        "US",
        "Global Equity",
        "UNAVAILABLE_GLOBAL_EQUITY",
        ["corporate_profitability", "financial_conditions", "growth_regime"],
        ["benchmark_relative_performance"],
        ["US equity outlook", "global equity allocation view"],
        "Equity score from growth/labor/financial data plus benchmark-relative market confirmation.",
    ),
    ("EQUITY|Other categories|Market Breadth (Equal Weight)", "US"): CoreCellModel(
        "EQUITY|Other categories|Market Breadth (Equal Weight)",
        "US",
        "US Cap-Weighted Market",
        "SP500",
        ["earnings_participation_unavailable", "profit_concentration_unavailable"],
        ["equal_weight_relative_performance"],
        ["market breadth", "participation broadening"],
        "Breadth score requires broad participation data or direct research plus separate data/market layer.",
    ),
    ("EQUITY|Other categories|Growth", "US"): CoreCellModel(
        "EQUITY|Other categories|Growth",
        "US",
        "US Wide Market",
        "SP500",
        ["relative_eps_revisions_unavailable", "valuation_premium_unavailable", "earnings_durability_unavailable"],
        ["NASDAQCOM relative to SP500"],
        ["growth equities", "technology earnings", "long-duration equity"],
        "Growth style score from macro/financial data and Nasdaq relative performance versus US wide market.",
    ),
    ("EQUITY|Other categories|Small Cap", "US"): CoreCellModel(
        "EQUITY|Other categories|Small Cap",
        "US",
        "US Large Cap / Wide Market",
        "SP500",
        ["refinancing_burden", "credit_sensitivity", "domestic_growth_sensitivity"],
        ["small_cap_relative_performance"],
        ["small-cap earnings", "financing conditions"],
        "Small-cap score requires small-cap relative benchmark data or direct research plus independent data layer.",
    ),
    ("FIXED INCOME|GOV|Long Term", "US"): CoreCellModel(
        "FIXED INCOME|GOV|Long Term",
        "US",
        "Cash / Policy Rate",
        "FEDFUNDS",
        ["inflation_trend", "policy_rate_outlook", "growth_regime"],
        ["long_yield_minus_policy_rate", "real_yield_market_proxy"],
        ["long-duration Treasuries", "duration outlook"],
        "Long Treasury score from inflation/policy fundamentals and long-yield market repricing versus cash.",
    ),
    ("FIXED INCOME|GOV|Short Term", "US"): CoreCellModel(
        "FIXED INCOME|GOV|Short Term",
        "US",
        "Cash / Policy Rate",
        "FEDFUNDS",
        ["inflation_trend", "policy_rate_outlook"],
        ["front_end_yield_minus_policy_rate"],
        ["front-end Treasuries", "policy path"],
        "Short Treasury score from inflation/policy fundamentals and front-end yield repricing versus policy rate.",
    ),
    ("FIXED INCOME|CORP HY|Mid Term", "US"): CoreCellModel(
        "FIXED INCOME|CORP HY|Mid Term",
        "US",
        "Investment Grade / duration-matched Treasuries",
        "BAMLC0A0CM",
        ["credit_conditions", "leverage_pressure", "labor_market_context", "growth_regime"],
        ["HY spread relative to IG spread", "risk_appetite"],
        ["high-yield credit outlook", "default cycle"],
        "HY score from credit/leverage fundamentals and HY spread compensation versus IG/Treasury benchmark.",
    ),
    ("FIXED INCOME|CORP IG|Mid Term", "US"): CoreCellModel(
        "FIXED INCOME|CORP IG|Mid Term",
        "US",
        "Duration-matched Treasuries",
        "DGS10",
        ["quality_trend", "credit_conditions", "leverage_pressure", "growth_regime"],
        ["IG spread", "risk_appetite"],
        ["investment-grade credit outlook", "balance-sheet quality"],
        "IG score from credit fundamentals and IG spread/carry market compensation versus Treasury benchmark.",
    ),
    ("FIXED INCOME|Other categories|Inflation Linked", "US"): CoreCellModel(
        "FIXED INCOME|Other categories|Inflation Linked",
        "US",
        "Nominal Treasuries",
        "DGS10",
        ["inflation_trend", "inflation_surprise_proxy", "policy_rate_outlook"],
        ["breakeven_inflation", "real_yields"],
        ["inflation-linked bonds", "breakevens"],
        "Inflation-linked score from inflation fundamentals and breakeven/real-yield market confirmation versus nominals.",
    ),
    ("COMMODITIES|Commodities|Gold", "Global"): CoreCellModel(
        "COMMODITIES|Commodities|Gold",
        "Global",
        "Strategic neutral Gold allocation",
        "STRATEGIC_NEUTRAL",
        ["approved inflation uncertainty or official-demand data"],
        ["approved gold price benchmark", "real_yields", "USD", "risk_regime"],
        ["gold outlook", "central-bank demand"],
        "Gold score requires an approved gold price benchmark; real yields, dollar and risk regime are supporting market drivers only.",
    ),
}


@dataclass(frozen=True)
class ComponentScore:
    score: int | None
    factors: list[dict[str, Any]] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ComponentCellDraft:
    canonical_cell_id: str
    template_row_key: str
    region: str
    asset_bucket: str
    benchmark: str
    applicability: str
    research_score: int | None
    data_score: int | None
    market_score: int | None
    composite_score: int | None
    previous_composite_score: int | None
    score_change: int | None
    divergence_status: str
    research_coverage: str
    component_coverage: list[str]
    source_details: list[dict[str, Any]]
    factor_details: dict[str, Any]
    exposure_path: list[dict[str, Any]]
    explanation: str


@dataclass(frozen=True)
class ComponentSnapshotDraft:
    snapshot_date: date
    cells: list[ComponentCellDraft]
    coverage_report: dict[str, Any]


@dataclass(frozen=True)
class ComponentBuildResult:
    snapshots: list[ComponentSnapshotDraft]
    dry_run: bool
    saved_snapshot_ids: list[str] = field(default_factory=list)
    backup_path: Path | None = None
    report_path: Path | None = None


@dataclass(frozen=True)
class ProviderRegistryEntry:
    provider: str
    provider_family: str
    domain: str
    source_role: str
    allowed_component: str
    priority: int = 9


class FredDataProvider:
    def __init__(self, session: Session | None = None, *, timeout_seconds: int | None = None, use_cache: bool = True) -> None:
        self.session = session
        self.timeout_seconds = timeout_seconds or get_settings().http_timeout_seconds
        self.use_cache = use_cache
        self._memory_cache: dict[str, list[TimePoint]] = {}

    def series(self, series_id: str) -> list[TimePoint]:
        if series_id in self._memory_cache:
            return self._memory_cache[series_id]
        url = FRED_SERIES[series_id]
        raw = self._raw_csv(series_id, url)
        rows = _parse_fred_csv(raw, series_id)
        self._memory_cache[series_id] = rows
        return rows

    def value_on_or_before(self, series_id: str, snapshot_date: date, *, lag_days: int = 0, max_age_days: int = 370) -> TimePoint | None:
        cutoff = snapshot_date - timedelta(days=lag_days)
        values = [point for point in self.series(series_id) if point.observation_date <= cutoff]
        if not values:
            return None
        point = values[-1]
        if (snapshot_date - point.observation_date).days > max_age_days:
            return None
        return point

    def change(self, series_id: str, snapshot_date: date, *, lookback_days: int, lag_days: int = 0) -> tuple[float, TimePoint, TimePoint] | None:
        end = self.value_on_or_before(series_id, snapshot_date, lag_days=lag_days)
        start = self.value_on_or_before(series_id, snapshot_date - timedelta(days=lookback_days), lag_days=lag_days, max_age_days=730)
        if end is None or start is None:
            return None
        return end.value - start.value, start, end

    def pct_change(self, series_id: str, snapshot_date: date, *, lookback_days: int, lag_days: int = 0) -> tuple[float, TimePoint, TimePoint] | None:
        result = self.change(series_id, snapshot_date, lookback_days=lookback_days, lag_days=lag_days)
        if result is None:
            return None
        _, start, end = result
        if start.value == 0:
            return None
        return end.value / start.value - 1, start, end

    def yoy_change_delta(self, series_id: str, snapshot_date: date, *, lag_days: int = MONTHLY_RELEASE_LAG_DAYS) -> tuple[float, dict[str, Any]] | None:
        current = self.pct_change(series_id, snapshot_date, lookback_days=365, lag_days=lag_days)
        previous = self.pct_change(series_id, snapshot_date - timedelta(days=90), lookback_days=365, lag_days=lag_days)
        if current is None or previous is None:
            return None
        current_yoy, _, current_end = current
        previous_yoy, _, previous_end = previous
        return current_yoy - previous_yoy, {
            "current_yoy": current_yoy,
            "previous_yoy": previous_yoy,
            "current_date": current_end.observation_date.isoformat(),
            "previous_date": previous_end.observation_date.isoformat(),
        }

    def _raw_csv(self, series_id: str, url: str) -> str:
        cache_key = f"fred:{series_id}"
        if self.use_cache and self.session is not None:
            cached = self.session.scalar(select(ExternalDataCache).where(ExternalDataCache.cache_key == cache_key))
            if cached and cached.raw_text and not cached.error_message:
                return cached.raw_text
        try:
            response = httpx.get(url, timeout=self.timeout_seconds, follow_redirects=True)
            text = response.text
            error = "" if response.status_code == 200 and "observation_date" in text else f"HTTP {response.status_code}"
        except Exception as exc:
            text = ""
            response = None
            error = f"{type(exc).__name__}: {exc}"
        if self.session is not None:
            cached = self.session.scalar(select(ExternalDataCache).where(ExternalDataCache.cache_key == cache_key))
            if cached is None:
                cached = ExternalDataCache(cache_key=cache_key, provider="FRED", url=url)
                self.session.add(cached)
            cached.fetched_at = utcnow()
            cached.status_code = response.status_code if response is not None else None
            cached.raw_text = text
            cached.error_message = error
            cached.content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""
            self.session.flush()
        if error:
            return ""
        return text


class StaticDataProvider(FredDataProvider):
    def __init__(self, series_map: dict[str, list[tuple[date, float]]]) -> None:
        super().__init__(None, use_cache=False)
        self._static = {
            key: [TimePoint(observation_date=row_date, value=value) for row_date, value in rows]
            for key, rows in series_map.items()
        }

    def series(self, series_id: str) -> list[TimePoint]:
        return self._static.get(series_id, [])


def rebuild_component_history(
    session: Session,
    *,
    snapshot_dates: list[date] | None = None,
    from_date: date = date(2026, 1, 1),
    to_date: date = date(2026, 7, 12),
    dry_run: bool = True,
    replace_existing: bool = False,
    backup: bool = False,
    report_path: Path | None = None,
    data_provider: FredDataProvider | None = None,
) -> ComponentBuildResult:
    dates = snapshot_dates or default_snapshot_dates(from_date, to_date)
    provider = data_provider or FredDataProvider(session)
    previous_scores: dict[str, int | None] = {}
    snapshots: list[ComponentSnapshotDraft] = []
    for snapshot_date in dates:
        draft = build_component_snapshot_draft(session, snapshot_date, provider, previous_scores=previous_scores)
        if any(cell.composite_score is not None for cell in draft.cells):
            snapshots.append(draft)
            previous_scores = {cell.canonical_cell_id: cell.composite_score for cell in draft.cells}

    saved_ids: list[str] = []
    backup_path = backup_database() if backup and not dry_run else None
    if not dry_run:
        invalidate_empty_backfill_snapshots(session)
        for draft in snapshots:
            saved_ids.append(save_component_snapshot(session, draft, replace_existing=replace_existing))
        session.flush()
    if report_path is not None:
        write_component_report(snapshots, report_path)
    return ComponentBuildResult(snapshots=snapshots, dry_run=dry_run, saved_snapshot_ids=saved_ids, backup_path=backup_path, report_path=report_path)


def build_component_snapshot_draft(
    session: Session,
    snapshot_date: date,
    data_provider: FredDataProvider,
    *,
    previous_scores: dict[str, int | None] | None = None,
) -> ComponentSnapshotDraft:
    previous_scores = previous_scores or {}
    cells: list[ComponentCellDraft] = []
    for canonical in core_component_cells():
        asset_bucket = asset_bucket_for(canonical.template_row_key)
        model = core_model_for(canonical.template_row_key, canonical.region)
        previous = previous_scores.get(canonical.canonical_cell_id)
        if component_applicability(canonical) == NOT_APPLICABLE:
            cells.append(_not_applicable_cell(canonical, asset_bucket, previous, model))
            continue
        research = research_component(session, canonical, snapshot_date)
        data = data_component(data_provider, canonical, snapshot_date)
        market = market_component(data_provider, canonical, snapshot_date)
        composite, coverage = composite_score(asset_bucket, research.score, data.score, market.score)
        score_change = None if composite is None or previous is None else composite - previous
        divergence = divergence_status(research.score, data.score, market.score)
        explanation = _component_explanation(canonical, composite, divergence, coverage, research, data, market)
        cells.append(
            ComponentCellDraft(
                canonical_cell_id=canonical.canonical_cell_id,
                template_row_key=canonical.template_row_key,
                region=template_region(canonical.region),
                asset_bucket=asset_bucket,
                benchmark=model.benchmark,
                applicability="APPLICABLE",
                research_score=research.score,
                data_score=data.score,
                market_score=market.score,
                composite_score=composite,
                previous_composite_score=previous,
                score_change=score_change,
                divergence_status=divergence,
                research_coverage=research_coverage(research.sources),
                component_coverage=coverage,
                source_details=research.sources,
                factor_details={"data": data.factors, "market": market.factors, "research": research.factors},
                exposure_path=research.factors,
                explanation=explanation,
            )
        )
    return ComponentSnapshotDraft(snapshot_date=snapshot_date, cells=cells, coverage_report=_component_coverage_report(snapshot_date, cells))


def save_component_snapshot(session: Session, draft: ComponentSnapshotDraft, *, replace_existing: bool = False) -> str:
    existing = session.scalar(
        select(MaeComponentSnapshot).where(
            MaeComponentSnapshot.snapshot_date == draft.snapshot_date,
            MaeComponentSnapshot.formula_version == FORMULA_VERSION,
            MaeComponentSnapshot.is_demo.is_(False),
        )
    )
    if existing and existing.immutable and not replace_existing:
        raise RuntimeError(f"Component snapshot {draft.snapshot_date.isoformat()} is immutable.")
    if existing:
        session.execute(delete(MaeComponentCell).where(MaeComponentCell.snapshot_id == existing.id))
        session.delete(existing)
        session.flush()
    snapshot_status = component_snapshot_status(draft.snapshot_date)
    snapshot = MaeComponentSnapshot(
        snapshot_date=draft.snapshot_date,
        status=snapshot_status,
        formula_version=FORMULA_VERSION,
        exposure_map_version=EXPOSURE_MAP_VERSION,
        applicability_map_version=APPLICABILITY_MAP_VERSION,
        run_metadata={
            "components": ["research", "data", "market"],
            "minimum_components_for_score": 2,
            "weights": COMPONENT_WEIGHTS,
            "no_llm_scoring": True,
            "mae_frequency": "MONTHLY",
            "snapshot_type": snapshot_status,
            "official_snapshot_rule": "last_available_business_day_of_month",
            "next_official_snapshot_date": next_official_snapshot_date(draft.snapshot_date).isoformat(),
            "research_lifecycle_config": lifecycle_config().get("version", "research_lifecycle_v1"),
        },
        coverage_report=draft.coverage_report,
        immutable=True,
        is_demo=False,
    )
    session.add(snapshot)
    session.flush()
    for cell in draft.cells:
        session.add(
            MaeComponentCell(
                snapshot_id=snapshot.id,
                snapshot_date=draft.snapshot_date,
                canonical_cell_id=cell.canonical_cell_id,
                template_row_key=cell.template_row_key,
                region=cell.region,
                asset_bucket=cell.asset_bucket,
                benchmark=cell.benchmark,
                applicability=cell.applicability,
                research_score=cell.research_score,
                data_score=cell.data_score,
                market_score=cell.market_score,
                composite_score=cell.composite_score,
                previous_composite_score=cell.previous_composite_score,
                score_change=cell.score_change,
                divergence_status=cell.divergence_status,
                research_coverage=cell.research_coverage,
                component_coverage=cell.component_coverage,
                source_details=cell.source_details,
                factor_details=cell.factor_details,
                exposure_path=cell.exposure_path,
                formula_version=FORMULA_VERSION,
                explanation=cell.explanation,
                is_demo=False,
            )
        )
    session.flush()
    return snapshot.id


def component_snapshot_status(snapshot_date: date) -> str:
    return FINAL_COMPONENT if snapshot_date == last_business_day(snapshot_date.year, snapshot_date.month) else INTERIM_SNAPSHOT


def last_business_day(year: int, month: int) -> date:
    last_day = calendar.monthrange(year, month)[1]
    candidate = date(year, month, last_day)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def next_official_snapshot_date(snapshot_date: date) -> date:
    current_official = last_business_day(snapshot_date.year, snapshot_date.month)
    if snapshot_date < current_official:
        return current_official
    year = snapshot_date.year + (1 if snapshot_date.month == 12 else 0)
    month = 1 if snapshot_date.month == 12 else snapshot_date.month + 1
    return last_business_day(year, month)


@lru_cache(maxsize=4)
def lifecycle_config(path: Path | None = None) -> dict[str, Any]:
    path = path or ROOT_DIR / "data" / "research_lifecycle_config_v1.json"
    if not path.exists():
        return _default_lifecycle_config()
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return {**_default_lifecycle_config(), **loaded}


def assess_research_lifecycle(
    view: ResearchView,
    article: Article,
    snapshot_date: date,
    *,
    cell_id: str = "",
) -> ResearchLifecycleAssessment:
    doc_type = normalize_document_type(getattr(view, "document_type", "") or infer_document_type(article, view))
    valid_from = getattr(view, "valid_from", None) or article.publication_date
    valid_until = getattr(view, "valid_until", None) or valid_until_for(article.publication_date, doc_type)
    invalidation_reason = str(getattr(view, "invalidation_reason", "") or "").strip()
    superseded_by = _superseding_view_id(view, article, snapshot_date, cell_id=cell_id)
    carried = _is_carried_forward(article.publication_date, snapshot_date)
    config = lifecycle_config()

    if doc_type == NEWS_EVENT and not bool(config.get("news_event_research_score_allowed", False)):
        status = INVALIDATED
        reason = "news_event_cannot_create_research_score"
        weight = 0.0
    elif invalidation_reason:
        status = INVALIDATED
        reason = f"invalidation_condition:{invalidation_reason[:120]}"
        weight = 0.0
    elif superseded_by:
        status = SUPERSEDED
        reason = "superseded_by_newer_provider_publication"
        weight = 0.0
    elif snapshot_date > valid_until:
        status = EXPIRED
        reason = "research_lifecycle_expired"
        weight = 0.0
    else:
        age = max((snapshot_date - article.publication_date).days, 0)
        aging_after = int(config.get("aging_threshold_days", {}).get(doc_type, 60))
        status = AGING if age > aging_after else ACTIVE
        reason = ""
        if status == AGING and carried:
            weight = float(config.get("freshness_weights", {}).get("AGING_CARRIED_FORWARD", 0.6))
        elif status == AGING:
            weight = float(config.get("freshness_weights", {}).get(AGING, 0.7))
        elif carried:
            weight = float(config.get("freshness_weights", {}).get("CARRIED_FORWARD", 0.85))
        else:
            weight = float(config.get("freshness_weights", {}).get(ACTIVE, 1.0))

    return ResearchLifecycleAssessment(
        document_type=doc_type,
        valid_from=valid_from,
        valid_until=valid_until,
        lifecycle_status=status,
        freshness_weight=weight,
        superseded_by=superseded_by,
        invalidation_reason=invalidation_reason,
        last_confirmed_at=utcnow() if not reason else getattr(view, "last_confirmed_at", None),
        carried_forward=carried,
        review_due=valid_until,
        rejection_reason=reason,
    )


def persist_lifecycle(view: ResearchView, assessment: ResearchLifecycleAssessment) -> None:
    view.document_type = assessment.document_type
    view.investment_horizon = view.investment_horizon or view.horizon
    view.valid_from = assessment.valid_from
    view.valid_until = assessment.valid_until
    view.lifecycle_status = assessment.lifecycle_status
    view.superseded_by = assessment.superseded_by
    view.invalidation_reason = assessment.invalidation_reason
    view.last_confirmed_at = assessment.last_confirmed_at
    view.freshness_weight = assessment.freshness_weight


def normalize_document_type(value: str | None) -> str:
    text = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    return text if text in DOCUMENT_TYPES else ASSET_OUTLOOK


def valid_until_for(publication_date: date, document_type: str) -> date:
    days = int(lifecycle_config().get("document_validity_days", {}).get(normalize_document_type(document_type), 90))
    return publication_date + timedelta(days=days)


def infer_document_type(article: Article, view: ResearchView | None = None) -> str:
    text = f"{article.title} {article.source_reference} {article.url or ''}".casefold()
    if "annual" in text or "2026 outlook" in text and "midyear" not in text:
        return ANNUAL_OUTLOOK
    if "midyear" in text or "mid-year" in text:
        return MIDYEAR_OUTLOOK
    if "quarterly" in text or "q1" in text or "q2" in text or "q3" in text or "q4" in text:
        return QUARTERLY_OUTLOOK
    if "monthly" in text or "july 2026" in text or "june 2026" in text:
        return MONTHLY_OUTLOOK
    if "weekly" in text or "week" in text or "commentary" in text:
        return WEEKLY_COMMENTARY
    if "central bank" in text or "fomc" in text or "federal reserve" in text:
        return CENTRAL_BANK
    if "rating" in text:
        return RATING_OUTLOOK
    if "news" in text or "event" in text:
        return NEWS_EVENT
    return ASSET_OUTLOOK


def lifecycle_public_label(status: str, *, carried_forward: bool = False) -> str:
    if carried_forward and status == ACTIVE:
        return "Взгляд сохраняется с предыдущего среза"
    if carried_forward and status == AGING:
        return "Новых публикаций нет, тезис остаётся активным"
    return RU_LIFECYCLE_STATUS.get(status, "Статус не определён")


def document_type_label(document_type: str) -> str:
    return RU_DOCUMENT_TYPE.get(normalize_document_type(document_type), "тип материала не указан")


def _is_carried_forward(publication_date: date, snapshot_date: date) -> bool:
    return publication_date.year != snapshot_date.year or publication_date.month != snapshot_date.month


def _superseding_view_id(view: ResearchView, article: Article, snapshot_date: date, *, cell_id: str = "") -> str:
    session = object_session(view)
    if session is None or article.source is None:
        return ""
    family = source_independence_group(article.source)
    target_cell = cell_id or view.canonical_cell_id
    rows = session.scalars(
        select(ResearchView)
        .where(
            ResearchView.id != view.id,
            ResearchView.is_demo.is_(False),
            ResearchView.canonical_cell_id == target_cell,
            ResearchView.review_status == view.review_status,
        )
        .order_by(ResearchView.created_at.desc())
    ).all()
    for candidate in rows:
        candidate_article = session.get(Article, candidate.article_id)
        if candidate_article is None or candidate_article.source is None:
            continue
        if source_independence_group(candidate_article.source) != family:
            continue
        if candidate_article.publication_date <= article.publication_date:
            continue
        if candidate_article.publication_date > snapshot_date:
            continue
        if normalize_document_type(getattr(candidate, "document_type", "")) == NEWS_EVENT:
            continue
        return candidate.id
    return ""


def _default_lifecycle_config() -> dict[str, Any]:
    return {
        "version": LIFECYCLE_CONFIG_VERSION,
        "mae_frequency": "MONTHLY",
        "document_validity_days": {
            ANNUAL_OUTLOOK: 180,
            MIDYEAR_OUTLOOK: 180,
            QUARTERLY_OUTLOOK: 90,
            ASSET_OUTLOOK: 90,
            MONTHLY_OUTLOOK: 60,
            WEEKLY_COMMENTARY: 30,
            CENTRAL_BANK: 180,
            RATING_OUTLOOK: 180,
            NEWS_EVENT: 14,
        },
        "aging_threshold_days": {
            ANNUAL_OUTLOOK: 90,
            MIDYEAR_OUTLOOK: 90,
            QUARTERLY_OUTLOOK: 60,
            ASSET_OUTLOOK: 60,
            MONTHLY_OUTLOOK: 45,
            WEEKLY_COMMENTARY: 21,
            CENTRAL_BANK: 90,
            RATING_OUTLOOK: 90,
            NEWS_EVENT: 7,
        },
        "freshness_weights": {
            ACTIVE: 1.0,
            AGING: 0.7,
            "CARRIED_FORWARD": 0.85,
            "AGING_CARRIED_FORWARD": 0.6,
        },
        "news_event_research_score_allowed": False,
    }


def invalidate_empty_backfill_snapshots(session: Session) -> int:
    invalidated = 0
    for snapshot in session.scalars(select(MaeSnapshot).where(MaeSnapshot.is_demo.is_(False))).all():
        if snapshot.status == INVALID_EMPTY_BACKFILL:
            continue
        if _snapshot_is_empty(session, snapshot.id):
            snapshot.status = INVALID_EMPTY_BACKFILL
            metadata = dict(snapshot.run_metadata or {})
            metadata["invalidated_reason"] = "Empty research-only backfill; superseded by Research/Data/Market component engine."
            snapshot.run_metadata = metadata
            invalidated += 1
    session.flush()
    return invalidated


def latest_component_snapshot(session: Session) -> MaeComponentSnapshot | None:
    return session.scalar(
        select(MaeComponentSnapshot)
        .where(MaeComponentSnapshot.is_demo.is_(False), MaeComponentSnapshot.status.in_([FINAL_COMPONENT, INTERIM_SNAPSHOT]))
        .order_by(desc(MaeComponentSnapshot.snapshot_date))
    )


def component_history_series(session: Session) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for snapshot in session.scalars(
        select(MaeComponentSnapshot)
        .where(MaeComponentSnapshot.is_demo.is_(False), MaeComponentSnapshot.status.in_([FINAL_COMPONENT, INTERIM_SNAPSHOT]))
        .order_by(MaeComponentSnapshot.snapshot_date)
    ).all():
        cells = list(session.scalars(select(MaeComponentCell).where(MaeComponentCell.snapshot_id == snapshot.id)).all())
        scored = [cell for cell in cells if cell.composite_score is not None]
        rows.append(
            {
                "date": snapshot.snapshot_date,
                "positive": len([cell for cell in scored if (cell.composite_score or 0) > 0]),
                "neutral": len([cell for cell in scored if cell.composite_score == 0]),
                "negative": len([cell for cell in scored if (cell.composite_score or 0) < 0]),
                "scored": len(scored),
            }
        )
    return rows


def core_component_cells() -> list[CanonicalCell]:
    return [
        cell
        for cell in canonical_cell_registry(include_not_applicable=True)
        if (cell.template_row_key, template_region(cell.region)) in CORE_UNIVERSE
    ]


def core_model_for(row_key: str, region: str) -> CoreCellModel:
    model = CORE_MODELS.get((row_key, template_region(region)))
    if model is not None:
        return model
    return CoreCellModel(row_key, template_region(region), "Не задан", "", [], [], [], "No model configured for this cell.")


def component_applicability(cell: CanonicalCell) -> str:
    region = template_region(cell.region)
    row = cell.template_row_key
    if row in {"COMMODITIES|Commodities|Gold", "COMMODITIES|Commodities|Other precious metals"}:
        return "APPLICABLE" if region == "Global" else NOT_APPLICABLE
    if row == "FIXED INCOME|Other categories|Mortgage-Backed":
        return "APPLICABLE" if region in {"Global", "US"} else NOT_APPLICABLE
    if row == "EQUITY|Other categories|Preferred":
        return "APPLICABLE" if region in {"Global", "US"} else NOT_APPLICABLE
    return "APPLICABLE" if (row, region) in CORE_UNIVERSE and cell.applicability == "APPLICABLE" else NOT_APPLICABLE


def load_provider_registry(path: Path | None = None) -> list[ProviderRegistryEntry]:
    path = path or ROOT_DIR / "data" / "approved_sources_v1.csv"
    if not path.exists():
        return []
    rows: list[ProviderRegistryEntry] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            domain = _normalize_domain(row.get("domain") or "")
            if not domain:
                continue
            try:
                priority = int(row.get("priority") or 9)
            except ValueError:
                priority = 9
            rows.append(
                ProviderRegistryEntry(
                    provider=(row.get("provider") or "").strip(),
                    provider_family=(row.get("provider_family") or "").strip() or domain,
                    domain=domain,
                    source_role=(row.get("source_role") or "").strip(),
                    allowed_component=(row.get("allowed_component") or "").strip(),
                    priority=priority,
                )
            )
    return rows


def provider_registry_entry(source: Source | None, registry: list[ProviderRegistryEntry] | None = None) -> ProviderRegistryEntry | None:
    if source is None:
        return None
    registry = registry or load_provider_registry()
    source_host = _normalize_domain(source.website or "")
    source_name = (source.institution_name or "").casefold()
    for entry in sorted(registry, key=lambda item: item.priority):
        if source_host and _domain_matches(source_host, entry.domain):
            return entry
        if entry.provider and entry.provider.casefold() in source_name:
            return entry
    return None


def canonical_provider_info(source: Source | None, registry: list[ProviderRegistryEntry] | None = None) -> dict[str, str]:
    entry = provider_registry_entry(source, registry)
    if entry is None:
        return {
            "provider": source.institution_name if source else "",
            "provider_family": source_independence_group(source),
            "canonical_domain": _normalize_domain(source.website if source else ""),
            "source_role": "",
            "allowed_component": "",
        }
    return {
        "provider": entry.provider,
        "provider_family": entry.provider_family,
        "canonical_domain": entry.domain,
        "source_role": entry.source_role,
        "allowed_component": entry.allowed_component,
    }


def provider_allows_research(entry: ProviderRegistryEntry | None) -> bool:
    if entry is None:
        return False
    role = entry.source_role.casefold()
    allowed = entry.allowed_component.casefold()
    if any(key in role for key in NON_RESEARCH_ROLE_KEYS) and "research" not in allowed:
        return False
    return "research" in allowed or any(key in role for key in RESEARCH_ROLE_KEYS)


def asset_bucket_for(row_key: str) -> str:
    if row_key.startswith("EQUITY|"):
        return "EQUITY"
    if row_key.startswith("FIXED INCOME|GOV|"):
        return "GOVERNMENT_BONDS"
    if row_key.startswith("FIXED INCOME|CORP "):
        return "CORPORATE_CREDIT"
    if row_key == "FIXED INCOME|Other categories|Inflation Linked":
        return "GOVERNMENT_BONDS"
    if row_key.startswith("COMMODITIES|"):
        return "GOLD"
    return "EQUITY"


def research_component(session: Session, cell: CanonicalCell, snapshot_date: date) -> ComponentScore:
    registry = load_provider_registry()
    rows: list[dict[str, Any]] = []
    for view in session.scalars(select(ResearchView)).all():
        article = session.get(Article, view.article_id)
        assessment = research_acceptance(view, article, cell, snapshot_date, registry=registry)
        if not assessment["accepted"]:
            continue
        rows.append(assessment)
    if not rows:
        return ComponentScore(None)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["provider_family"]].append(row)
    group_bests: list[dict[str, Any]] = []
    for group_rows in grouped.values():
        group_bests.append(
            max(
                group_rows,
                key=lambda item: (
                    item["classification"] == DIRECT_RESEARCH,
                    item["weight"],
                    item["article"].publication_date,
                ),
            )
        )
    direct_groups = {row["provider_family"] for row in group_bests if row["classification"] == DIRECT_RESEARCH}
    scoring_allowed = len(group_bests) >= 2 and bool(direct_groups)
    weighted_scores = [(row["view"].position_score or 0) * row["weight"] for row in group_bests] if scoring_allowed else []
    score_weights = [row["weight"] for row in group_bests] if scoring_allowed else []
    sources: list[dict[str, Any]] = []
    factors: list[dict[str, Any]] = []
    for row in group_bests:
        view = row["view"]
        article = row["article"]
        weight = row["weight"]
        reason = row["mapping_rule"]
        provider = row["provider"]
        provider_family = row["provider_family"]
        direct_or_semi = row["classification"]
        accepted_for_score = scoring_allowed
        quote = ""
        if view.evidence_quotes:
            quote = str(view.evidence_quotes[0].get("quote", ""))[:500]
        sources.append(
            {
                "provider": provider,
                "provider_family": provider_family,
                "provider_group": provider_family,
                "canonical_domain": row["canonical_domain"],
                "title": article.title,
                "author": article.author or "",
                "publication_date": article.publication_date.isoformat(),
                "url": article.canonical_url or article.url or article.source_reference,
                "document_type": _document_type(article),
                "horizon": view.horizon,
                "geography": template_region(view.region),
                "asset_class": view.asset_class,
                "segment": view.asset_segment,
                "extracted_stance": view.direction,
                "score": view.position_score,
                "specificity_weight": weight,
                "base_weight": row.get("base_weight"),
                "freshness_weight": row.get("freshness_weight"),
                "direct_or_inherited": "direct" if direct_or_semi == DIRECT_RESEARCH else "semi_direct",
                "direct_or_semi_direct": direct_or_semi,
                "accepted_for_score": accepted_for_score,
                "not_scored_reason": "" if accepted_for_score else "preliminary_research_view",
                "quote": quote,
                "drivers": list(view.drivers or []),
                "risks": list(view.risks or []),
                "schema_version": view.schema_version,
                "document_type": row.get("document_type") or getattr(view, "document_type", ""),
                "investment_horizon": row.get("investment_horizon") or getattr(view, "investment_horizon", "") or view.horizon,
                "lifecycle_status": row.get("lifecycle_status") or getattr(view, "lifecycle_status", ""),
                "valid_from": _date_iso(row.get("valid_from")),
                "valid_until": _date_iso(row.get("valid_until")),
                "carried_forward": article.publication_date.replace(day=1) != snapshot_date.replace(day=1),
                "review_due": _date_iso(row.get("valid_until")),
            }
        )
        factors.append(
            {
                "article_id": article.id,
                "research_view_id": view.id,
                "source": provider,
                "provider": provider,
                "provider_family": provider_family,
                "provider_group": provider_family,
                "canonical_domain": row["canonical_domain"],
                "title": article.title,
                "author": article.author or "",
                "publication_date": article.publication_date.isoformat(),
                "document_type": _document_type(article),
                "horizon": view.horizon,
                "geography": template_region(view.region),
                "asset_class": view.asset_class,
                "segment": view.asset_segment,
                "extracted_stance": view.direction,
                "weight": weight,
                "base_weight": row.get("base_weight"),
                "freshness_weight": row.get("freshness_weight"),
                "rule": reason,
                "raw_score": view.position_score,
                "score": view.position_score,
                "layer": "research",
                "underlying_signal": f"research_voice:{provider_family}",
                "benchmark": core_model_for(cell.template_row_key, cell.region).benchmark,
                "direct_or_inherited": "direct" if direct_or_semi == DIRECT_RESEARCH else "semi_direct",
                "direct_or_semi_direct": direct_or_semi,
                "accepted_for_score": accepted_for_score,
                "source_url": article.canonical_url or article.url or article.source_reference,
                "source_excerpt": quote,
                "drivers": list(view.drivers or []),
                "risks": list(view.risks or []),
                "schema_version": view.schema_version,
                "document_type": row.get("document_type") or getattr(view, "document_type", ""),
                "investment_horizon": row.get("investment_horizon") or getattr(view, "investment_horizon", "") or view.horizon,
                "lifecycle_status": row.get("lifecycle_status") or getattr(view, "lifecycle_status", ""),
                "valid_from": _date_iso(row.get("valid_from")),
                "valid_until": _date_iso(row.get("valid_until")),
                "carried_forward": article.publication_date.replace(day=1) != snapshot_date.replace(day=1),
                "review_due": _date_iso(row.get("valid_until")),
                "not_scored_reason": "" if accepted_for_score else "preliminary_research_view",
            }
        )
    if not weighted_scores:
        return ComponentScore(None, factors=factors, sources=sources)
    score = int(clip_score(_round(sum(weighted_scores) / max(sum(score_weights), 1e-9))))
    return ComponentScore(score, factors=factors, sources=sources)


def research_specificity(reason: str) -> str:
    classification = research_mapping_classification(reason)
    if classification == DIRECT_RESEARCH:
        return "direct"
    if classification == SEMI_DIRECT_RESEARCH:
        return "semi_direct"
    return "context"


def research_mapping_classification(reason: str) -> str:
    return RESEARCH_LINEAGE_RULES.get(reason, CONTEXT_ONLY_RESEARCH if reason != "not_exposed" else REJECTED_RESEARCH)


def research_acceptance(
    view: ResearchView,
    article: Article | None,
    cell: CanonicalCell,
    snapshot_date: date,
    *,
    registry: list[ProviderRegistryEntry] | None = None,
) -> dict[str, Any]:
    registry = registry or load_provider_registry()
    weight, mapping_rule = exposure_weight(view, cell)
    classification = research_mapping_classification(mapping_rule)
    rejection_reason = research_rejection_reason(view, article, cell, snapshot_date, mapping_rule, classification, registry=registry)
    accepted = not rejection_reason
    provider_info = canonical_provider_info(article.source if article else None, registry)
    lifecycle_weight = float(getattr(view, "freshness_weight", 1.0) or 1.0)
    adjusted_weight = weight * lifecycle_weight if accepted else weight
    return {
        "accepted": accepted,
        "rejection_reason": rejection_reason,
        "classification": classification if accepted else (CONTEXT_ONLY_RESEARCH if mapping_rule != "not_exposed" else REJECTED_RESEARCH),
        "weight": adjusted_weight,
        "base_weight": weight,
        "freshness_weight": lifecycle_weight,
        "lifecycle_status": getattr(view, "lifecycle_status", ""),
        "valid_from": getattr(view, "valid_from", None),
        "valid_until": getattr(view, "valid_until", None),
        "document_type": getattr(view, "document_type", ""),
        "investment_horizon": getattr(view, "investment_horizon", "") or getattr(view, "horizon", ""),
        "superseded_by": getattr(view, "superseded_by", ""),
        "invalidation_reason": getattr(view, "invalidation_reason", ""),
        "mapping_rule": mapping_rule,
        "view": view,
        "article": article,
        "provider": provider_info["provider"],
        "provider_family": provider_info["provider_family"],
        "canonical_domain": provider_info["canonical_domain"],
        "source_role": provider_info["source_role"],
        "allowed_component": provider_info["allowed_component"],
    }


def research_rejection_reason(
    view: ResearchView,
    article: Article | None,
    cell: CanonicalCell,
    snapshot_date: date,
    mapping_rule: str,
    classification: str,
    *,
    registry: list[ProviderRegistryEntry] | None = None,
) -> str:
    if article is None:
        return "missing_article"
    entry = provider_registry_entry(article.source, registry)
    if entry is None:
        return "provider_not_in_outlook_sources"
    if not provider_allows_research(entry):
        return f"source_role_not_research:{entry.source_role}"
    if not is_content_valid_article(article):
        return "article_failed_content_quality_gate"
    if not article.title.strip():
        return "missing_exact_report_title"
    if not (article.canonical_url or article.url or article.source_reference):
        return "missing_canonical_url"
    if article.publication_date > snapshot_date:
        return "lookahead_publication_date"
    if not _horizon_is_medium_term(view.horizon):
        return "horizon_not_6_12_months"
    if not _best_research_quote(view):
        return "missing_exact_excerpt"
    if view.position_score is None:
        return "missing_directional_score"
    lifecycle = assess_research_lifecycle(view, article, snapshot_date, cell_id=cell.canonical_cell_id)
    persist_lifecycle(view, lifecycle)
    if lifecycle.rejection_reason:
        return lifecycle.rejection_reason
    if mapping_rule == "not_exposed":
        return "not_mapped_to_cell"
    if classification not in {DIRECT_RESEARCH, SEMI_DIRECT_RESEARCH}:
        return f"disallowed_mapping:{mapping_rule}"
    status = view.strict_review_status or view.review_status or ""
    if "MANUAL_REVIEW_REQUIRED" in {status, view.review_status, view.strict_review_status}:
        return "manual_review_required_not_accepted"
    if not is_production_research_view(view, article):
        return f"strict_research_gate_failed:{view.review_status or status}"
    if _looks_like_landing_or_product_page(article):
        return "landing_or_product_page"
    if not _view_matches_cell_family(view, cell):
        return "asset_or_geography_not_specific_enough"
    return ""


def _normalize_domain(value: str | None) -> str:
    text = str(value or "").strip().casefold()
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else f"https://{text}")
    host = (parsed.netloc or parsed.path).split("/")[0]
    return host.removeprefix("www.").strip("/")


def _domain_matches(source_host: str, registry_domain: str) -> bool:
    source_host = _normalize_domain(source_host)
    registry_domain = _normalize_domain(registry_domain)
    return bool(source_host and registry_domain and (source_host == registry_domain or source_host.endswith(f".{registry_domain}")))


def _document_type(article: Article) -> str:
    url = (article.canonical_url or article.url or article.source_reference or "").casefold()
    if ".pdf" in url or url.endswith("/pdf"):
        return "PDF"
    return "HTML"


def _date_iso(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _horizon_is_medium_term(horizon: str | None) -> bool:
    text = str(horizon or "").casefold().replace("–", "-").replace("_", "-")
    if not text:
        return False
    return any(marker in text for marker in ("6-12", "3-12", "medium", "12m", "6m", "3-12m"))


def _best_research_quote(view: ResearchView) -> str:
    for item in view.evidence_quotes or []:
        quote = str(item.get("quote") or "").strip()
        if quote:
            return quote
    return ""


def _looks_like_landing_or_product_page(article: Article) -> bool:
    title = (article.title or "").casefold()
    url = (article.canonical_url or article.url or article.source_reference or "").casefold()
    product_markers = {
        "etf",
        "fund page",
        "product page",
        "factsheet",
        "fact sheet",
        "portfolio insights",
        "all insights",
        "research and insights",
    }
    landing_titles = {"home", "insights", "research", "outlook", "publications"}
    if title.strip() in landing_titles:
        return True
    if any(marker in title for marker in {"product launch", "fund facts", "fact sheet"}):
        return True
    if any(marker in url for marker in {"/products/", "/funds/", "/etf/", "/search", "/category/"}):
        return True
    return not article.content_text.strip() or (len(article.content_text.strip()) < 300 and any(marker in url for marker in product_markers))


def _view_matches_cell_family(view: ResearchView, cell: CanonicalCell) -> bool:
    target_region = template_region(cell.region)
    view_region = template_region(view.region)
    if view_region not in {target_region, "Global"} and target_region != "Global":
        return False
    row = cell.template_row_key
    view_row = view.template_row_key
    if row == view_row:
        return True
    if row.startswith("EQUITY|") and view_row == "EQUITY|Wide Market|Wide Market":
        return False
    return False


def exposure_weight(view: ResearchView, cell: CanonicalCell) -> tuple[float, str]:
    target_region = template_region(cell.region)
    view_region = template_region(view.region)
    target_row = cell.template_row_key
    view_row = view.template_row_key
    if view_row == target_row and view_region == target_region:
        return 1.0, "direct_cell"
    if view_row == target_row and view_region == "Global":
        return 0.70, "same_row_global_to_region"
    if view_row == target_row and target_region == "Global":
        return 0.55, "same_row_region_to_global"
    if target_row.startswith("EQUITY|") and view_row == "EQUITY|Wide Market|Wide Market" and view_region in {target_region, "Global"}:
        return 0.45, "broad_equity_to_style"
    if target_row.startswith("EQUITY|") and view_row.startswith("EQUITY|") and view_region == target_region:
        return 0.35, "same_region_equity_factor"
    if target_row.startswith("FIXED INCOME|GOV|") and view_row.startswith("FIXED INCOME|GOV|") and view_region == target_region:
        return 0.40, "same_region_rates_factor"
    if target_row.startswith("FIXED INCOME|CORP ") and view_row.startswith("FIXED INCOME|CORP ") and view_region == target_region:
        return 0.40, "same_region_credit_factor"
    if target_row.startswith("COMMODITIES|") and view_row.startswith("COMMODITIES|") and view_region in {target_region, "Global"}:
        return 0.60, "commodity_factor"
    return 0.0, "not_exposed"


def research_coverage(sources: list[dict[str, Any]]) -> str:
    groups = {source.get("provider_group") for source in sources if source.get("provider_group")}
    if len(groups) >= 2:
        return "MULTI_SOURCE"
    if len(groups) == 1:
        return "SINGLE_SOURCE"
    return "NONE"


def data_component(provider: FredDataProvider, cell: CanonicalCell, snapshot_date: date) -> ComponentScore:
    row = cell.template_row_key
    bucket = asset_bucket_for(row)
    model = core_model_for(row, cell.region)
    factors: list[dict[str, Any]] = []
    if row == "EQUITY|Wide Market|Wide Market":
        factors.extend(
            [
                _cell_factor(_change_factor(provider, "NFCI", snapshot_date, 90, "financial_conditions", positive_when_up=False, thresholds=(0.03, 0.10, 0.25)), True),
                _cell_factor(_pct_factor(provider, "CP", snapshot_date, 180, "corporate_profitability", positive_when_up=True, thresholds=(0.01, 0.03, 0.06), lag_days=MONTHLY_RELEASE_LAG_DAYS), True),
                _cell_factor(_pct_factor(provider, "INDPRO", snapshot_date, 180, "growth_regime", positive_when_up=True, thresholds=(0.0025, 0.01, 0.025), lag_days=MONTHLY_RELEASE_LAG_DAYS), False),
            ]
        )
    elif row == "EQUITY|Other categories|Market Breadth (Equal Weight)":
        factors.extend(
            [
                _cell_factor(_pct_factor(provider, "INDPRO", snapshot_date, 180, "macro_context_only", positive_when_up=True, thresholds=(0.0025, 0.01, 0.025), lag_days=MONTHLY_RELEASE_LAG_DAYS), False),
            ]
        )
    elif row == "EQUITY|Other categories|Growth":
        factors.extend(
            [
                _cell_factor(_change_factor(provider, "NFCI", snapshot_date, 90, "financial_conditions_context", positive_when_up=False, thresholds=(0.03, 0.10, 0.25)), False),
                _cell_factor(_pct_factor(provider, "CP", snapshot_date, 180, "earnings_context_only", positive_when_up=True, thresholds=(0.01, 0.03, 0.06), lag_days=MONTHLY_RELEASE_LAG_DAYS), False),
            ]
        )
    elif row == "EQUITY|Other categories|Small Cap":
        factors.extend(
            [
                _cell_factor(_change_factor(provider, "NFCINONFINLEVERAGE", snapshot_date, 90, "refinancing_burden", positive_when_up=False, thresholds=(0.02, 0.06, 0.12)), True),
                _cell_factor(_change_factor(provider, "NFCICREDIT", snapshot_date, 90, "credit_sensitivity", positive_when_up=False, thresholds=(0.02, 0.06, 0.12)), True),
                _cell_factor(_pct_factor(provider, "INDPRO", snapshot_date, 180, "domestic_growth_sensitivity", positive_when_up=True, thresholds=(0.0025, 0.01, 0.025), lag_days=MONTHLY_RELEASE_LAG_DAYS), True),
            ]
        )
    elif bucket == "GOVERNMENT_BONDS":
        factors.extend(
            [
                _cell_factor(_yoy_delta_factor(provider, "CPIAUCSL", snapshot_date, "inflation_trend", positive_when_up=False, thresholds=(0.001, 0.003, 0.006)), True),
                _cell_factor(_change_factor(provider, "FEDFUNDS", snapshot_date, 90, "policy_rate_outlook", positive_when_up=False, thresholds=(0.05, 0.15, 0.35), lag_days=MONTHLY_RELEASE_LAG_DAYS), True),
                _cell_factor(_pct_factor(provider, "INDPRO", snapshot_date, 180, "growth_regime", positive_when_up=True, thresholds=(0.0025, 0.01, 0.025), lag_days=MONTHLY_RELEASE_LAG_DAYS), row == "FIXED INCOME|GOV|Long Term"),
                _cell_factor(_change_factor(provider, "UNRATE", snapshot_date, 90, "labor_market_pressure", positive_when_up=False, thresholds=(0.05, 0.20, 0.40), lag_days=MONTHLY_RELEASE_LAG_DAYS), row == "FIXED INCOME|GOV|Short Term"),
            ]
        )
    elif bucket == "CORPORATE_CREDIT":
        if "CORP HY" in row:
            factors.extend(
                [
                    _cell_factor(_change_factor(provider, "UNRATE", snapshot_date, 90, "labor_market_context", positive_when_up=False, thresholds=(0.05, 0.20, 0.40), lag_days=MONTHLY_RELEASE_LAG_DAYS), False),
                    _cell_factor(_change_factor(provider, "NFCICREDIT", snapshot_date, 90, "credit_conditions", positive_when_up=False, thresholds=(0.02, 0.06, 0.12)), True),
                    _cell_factor(_change_factor(provider, "NFCINONFINLEVERAGE", snapshot_date, 90, "leverage_pressure", positive_when_up=False, thresholds=(0.02, 0.06, 0.12)), True),
                    _cell_factor(_pct_factor(provider, "INDPRO", snapshot_date, 180, "growth_regime", positive_when_up=True, thresholds=(0.0025, 0.01, 0.025), lag_days=MONTHLY_RELEASE_LAG_DAYS), False),
                ]
            )
        else:
            factors.extend(
                [
                    _cell_factor(_change_factor(provider, "NFCILEVERAGE", snapshot_date, 90, "quality_trend", positive_when_up=False, thresholds=(0.02, 0.06, 0.12)), True),
                    _cell_factor(_change_factor(provider, "NFCICREDIT", snapshot_date, 90, "credit_conditions", positive_when_up=False, thresholds=(0.02, 0.06, 0.12)), True),
                    _cell_factor(_pct_factor(provider, "INDPRO", snapshot_date, 180, "growth_regime", positive_when_up=True, thresholds=(0.0025, 0.01, 0.025), lag_days=MONTHLY_RELEASE_LAG_DAYS), False),
                ]
            )
    elif bucket == "GOLD":
        factors.extend(
            [
                _cell_factor(_yoy_delta_factor(provider, "CPIAUCSL", snapshot_date, "inflation_trend_context", positive_when_up=True, thresholds=(0.001, 0.003, 0.006)), False),
            ]
        )
    valid = [factor for factor in factors if factor]
    _annotate_factors(valid, layer="data", model=model)
    scoring_factors = [factor for factor in valid if factor.get("cell_specific")]
    return ComponentScore(_average_factor_score(scoring_factors), factors=valid)


def market_component(provider: FredDataProvider, cell: CanonicalCell, snapshot_date: date) -> ComponentScore:
    row = cell.template_row_key
    bucket = asset_bucket_for(row)
    model = core_model_for(row, cell.region)
    factors: list[dict[str, Any]] = []
    if bucket == "EQUITY":
        if row == "EQUITY|Other categories|Growth":
            factors.append(_relative_pct_factor(provider, "NASDAQCOM", "SP500", snapshot_date, 90, "relative_strength", thresholds=(0.01, 0.035, 0.08)))
        elif row == "EQUITY|Wide Market|Wide Market":
            factors.append(_pct_factor(provider, "SP500", snapshot_date, 90, "price_momentum", positive_when_up=True, thresholds=(0.025, 0.075, 0.15)))
        if row in {"EQUITY|Wide Market|Wide Market", "EQUITY|Other categories|Growth"}:
            factors.append(_change_factor(provider, "VIXCLS", snapshot_date, 90, "risk_appetite", positive_when_up=False, thresholds=(1.0, 3.0, 7.0)))
    elif bucket == "GOVERNMENT_BONDS":
        rate_series = "DGS2" if "Short Term" in row else "DGS10"
        factors.append(_change_factor(provider, rate_series, snapshot_date, 90, "yield_price_proxy", positive_when_up=False, thresholds=(0.10, 0.25, 0.50)))
        if row == "FIXED INCOME|Other categories|Inflation Linked":
            factors.append(_change_factor(provider, "T10YIE", snapshot_date, 90, "breakeven_inflation", positive_when_up=True, thresholds=(0.05, 0.15, 0.30)))
    elif bucket == "CORPORATE_CREDIT":
        if "CORP HY" in row:
            factors.append(_relative_change_factor(provider, "BAMLH0A0HYM2", "BAMLC0A0CM", snapshot_date, 90, "hy_spread_relative_to_ig", positive_when_up=False, thresholds=(0.10, 0.30, 0.70)))
        else:
            factors.append(_change_factor(provider, "BAMLC0A0CM", snapshot_date, 90, "ig_spread", positive_when_up=False, thresholds=(0.05, 0.15, 0.35)))
        factors.append(_change_factor(provider, "VIXCLS", snapshot_date, 90, "risk_appetite", positive_when_up=False, thresholds=(1.0, 3.0, 7.0)))
    elif bucket == "GOLD":
        factors.extend(
            [
                _change_factor(provider, "DFII10", snapshot_date, 90, "real_yield_market_proxy", positive_when_up=False, thresholds=(0.10, 0.25, 0.50)),
                _change_factor(provider, "DTWEXBGS", snapshot_date, 90, "usd", positive_when_up=False, thresholds=(0.75, 1.75, 3.50)),
                _change_factor(provider, "GVZCLS", snapshot_date, 90, "gold_volatility", positive_when_up=False, thresholds=(1.0, 3.0, 7.0)),
            ]
        )
    valid = [factor for factor in factors if factor]
    _annotate_factors(valid, layer="market", model=model)
    return ComponentScore(_average_factor_score(valid), factors=valid)


def composite_score(asset_bucket: str, research: int | None, data: int | None, market: int | None) -> tuple[int | None, list[str]]:
    available = {name: score for name, score in {"research": research, "data": data, "market": market}.items() if score is not None}
    if len(available) < 2:
        return None, list(available)
    weights = COMPONENT_WEIGHTS[asset_bucket]
    total_weight = sum(weights[name] for name in available)
    raw = sum(available[name] * weights[name] / total_weight for name in available)
    score = int(clip_score(_round(raw)))
    if "research" not in available:
        score = max(-1, min(1, score))
    elif len(available) == 2:
        score = max(-2, min(2, score))
    return score, list(available)


def divergence_status(research: int | None, data: int | None, market: int | None) -> str:
    available = [score for score in [research, data, market] if score is not None]
    if len(available) < 2:
        return INSUFFICIENT
    r, d, m = _sign(research), _sign(data), _sign(market)
    non_zero = [sign for sign in [r, d, m] if sign != 0]
    if len(non_zero) >= 2 and len(set(non_zero)) > 1:
        return MIXED
    if research is not None and data is not None and market is not None and len(set([r, d, m])) == 1:
        return CONFIRMED
    if research is not None and data is not None and r == d and (market is None or m == 0):
        return EARLY
    if data is not None and market is not None and d == m and (research is None or r == 0):
        return PRICE_LED
    if research is not None and (data is None or d == 0) and (market is None or m == 0):
        return NARRATIVE_ONLY
    if data is not None and market is not None and d == m:
        return PRICE_LED
    if research is not None and data is not None and r == d:
        return EARLY
    return MIXED


def backup_database(destination: Path | None = None) -> Path | None:
    settings = get_settings()
    if settings.database_url or not settings.sqlite_path.exists():
        return None
    destination = destination or ROOT_DIR / "outputs" / "production_cleanup" / f"pre_component_history_{utcnow().strftime('%Y%m%d_%H%M%S')}.db"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(settings.sqlite_path, destination)
    return destination


def write_component_report(snapshots: list[ComponentSnapshotDraft], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "formula_version": FORMULA_VERSION,
                "exposure_map_version": EXPOSURE_MAP_VERSION,
                "applicability_map_version": APPLICABILITY_MAP_VERSION,
                "generated_at": utcnow().isoformat(),
                "snapshots": [snapshot.coverage_report for snapshot in snapshots],
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    return path


def write_component_maps(directory: Path | None = None) -> tuple[Path, Path]:
    directory = directory or ROOT_DIR / "data"
    directory.mkdir(parents=True, exist_ok=True)
    applicability_path = directory / f"{APPLICABILITY_MAP_VERSION}.csv"
    exposure_path = directory / f"{EXPOSURE_MAP_VERSION}.csv"
    with applicability_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "version",
                "canonical_cell_id",
                "template_row_key",
                "region",
                "template_applicability",
                "component_applicability",
                "core_universe",
                "benchmark",
                "research_indicators",
                "fundamental_data_indicators",
                "market_indicators",
                "formula",
                "reason",
            ],
        )
        writer.writeheader()
        for cell in canonical_cell_registry(include_not_applicable=True):
            component_status = component_applicability(cell)
            model = core_model_for(cell.template_row_key, cell.region)
            writer.writerow(
                {
                    "version": APPLICABILITY_MAP_VERSION,
                    "canonical_cell_id": cell.canonical_cell_id,
                    "template_row_key": cell.template_row_key,
                    "region": template_region(cell.region),
                    "template_applicability": cell.applicability,
                    "component_applicability": component_status,
                    "core_universe": str((cell.template_row_key, template_region(cell.region)) in CORE_UNIVERSE).lower(),
                    "benchmark": model.benchmark,
                    "research_indicators": "; ".join(model.research_indicators),
                    "fundamental_data_indicators": "; ".join(model.data_indicators),
                    "market_indicators": "; ".join(model.market_indicators),
                    "formula": model.formula,
                    "reason": _applicability_reason(cell, component_status),
                }
            )
    with exposure_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["version", "rule", "specificity_weight", "description"])
        writer.writeheader()
        for row in _exposure_map_rows():
            writer.writerow({"version": EXPOSURE_MAP_VERSION, **row})
    return applicability_path, exposure_path


def _applicability_reason(cell: CanonicalCell, component_status: str) -> str:
    region = template_region(cell.region)
    row = cell.template_row_key
    if row in {"COMMODITIES|Commodities|Gold", "COMMODITIES|Commodities|Other precious metals"} and region != "Global":
        return "Gold and precious-metals rows are global-only in the component universe."
    if row == "FIXED INCOME|Other categories|Mortgage-Backed" and region not in {"Global", "US"}:
        return "MBS is limited to US and Global because benchmark coverage is explicit there."
    if row == "EQUITY|Other categories|Preferred" and region not in {"Global", "US"}:
        return "Preferred securities are limited to US and Global because representative benchmarks are explicit there."
    if component_status == "APPLICABLE":
        return "Included in component core universe."
    return "Outside the component core universe for this MVP; preserved in template-compatible CSV."


def _exposure_map_rows() -> list[dict[str, Any]]:
    return [
        {"rule": "direct_cell", "specificity_weight": 1.0, "description": "Same row and same region."},
        {"rule": "same_row_global_to_region", "specificity_weight": 0.70, "description": "Global row-level research applied to matching regional child cell."},
        {"rule": "same_row_region_to_global", "specificity_weight": 0.55, "description": "Regional row-level research applied to matching global aggregate."},
        {"rule": "broad_equity_to_style", "specificity_weight": 0.45, "description": "Candidate mapping only; not accepted into Research lineage for style, breadth or small-cap cells without direct segment evidence."},
        {"rule": "same_region_equity_factor", "specificity_weight": 0.35, "description": "Candidate mapping only; rejected from Research lineage unless the source is direct to the target cell."},
        {"rule": "same_region_rates_factor", "specificity_weight": 0.40, "description": "Candidate mapping only; rejected from Research lineage unless the source is direct to the target maturity bucket."},
        {"rule": "same_region_credit_factor", "specificity_weight": 0.40, "description": "Candidate mapping only; rejected from Research lineage unless the source is direct to the target credit segment."},
        {"rule": "commodity_factor", "specificity_weight": 0.60, "description": "Candidate mapping only; rejected from Gold Research lineage unless the publication is directly about Gold."},
    ]


def _snapshot_is_empty(session: Session, snapshot_id: str) -> bool:
    from app.domain.models import MaeSnapshotCell

    cells = list(session.scalars(select(MaeSnapshotCell).where(MaeSnapshotCell.snapshot_id == snapshot_id)).all())
    applicable = [cell for cell in cells if cell.applicability != NOT_APPLICABLE]
    return bool(applicable) and all(cell.mae_score is None for cell in applicable)


def _not_applicable_cell(cell: CanonicalCell, asset_bucket: str, previous: int | None, model: CoreCellModel) -> ComponentCellDraft:
    return ComponentCellDraft(
        canonical_cell_id=cell.canonical_cell_id,
        template_row_key=cell.template_row_key,
        region=template_region(cell.region),
        asset_bucket=asset_bucket,
        benchmark=model.benchmark,
        applicability=NOT_APPLICABLE,
        research_score=None,
        data_score=None,
        market_score=None,
        composite_score=None,
        previous_composite_score=previous,
        score_change=None,
        divergence_status=INSUFFICIENT,
        research_coverage="NONE",
        component_coverage=[],
        source_details=[],
        factor_details={},
        exposure_path=[],
        explanation="Категория не входит в применимый core universe компонентного engine.",
    )


def _component_coverage_report(snapshot_date: date, cells: list[ComponentCellDraft]) -> dict[str, Any]:
    applicable = [cell for cell in cells if cell.applicability != NOT_APPLICABLE]
    scored = [cell for cell in applicable if cell.composite_score is not None]
    return {
        "snapshot_date": snapshot_date.isoformat(),
        "core_cells": len(cells),
        "applicable_cells": len(applicable),
        "scored_cells": len(scored),
        "no_data_cells": len([cell for cell in applicable if cell.composite_score is None]),
        "not_applicable_cells": len([cell for cell in cells if cell.applicability == NOT_APPLICABLE]),
        "component_counts": {
            "research": len([cell for cell in applicable if cell.research_score is not None]),
            "data": len([cell for cell in applicable if cell.data_score is not None]),
            "market": len([cell for cell in applicable if cell.market_score is not None]),
        },
        "divergence_status": _count(cell.divergence_status for cell in scored),
    }


def _component_explanation(
    cell: CanonicalCell,
    composite: int | None,
    divergence: str,
    coverage: list[str],
    research: ComponentScore,
    data: ComponentScore,
    market: ComponentScore,
) -> str:
    if composite is None:
        return "Недостаточно независимых компонентов для числовой оценки."
    parts = [
        f"Итоговая оценка {composite:+d} сформирована из компонентов: {', '.join(_ru_component(name) for name in coverage)}.",
        f"Статус расхождения: {_ru_divergence(divergence)}.",
    ]
    if data.score is not None:
        parts.append(f"Данные: {data.score:+d}.")
    if market.score is not None:
        parts.append(f"Рыночная динамика: {market.score:+d}.")
    if research.score is not None:
        parts.append(f"Исследования: {research.score:+d}.")
    return " ".join(parts)


def _change_factor(
    provider: FredDataProvider,
    series_id: str,
    snapshot_date: date,
    lookback_days: int,
    name: str,
    *,
    positive_when_up: bool,
    thresholds: tuple[float, float, float],
    lag_days: int = 0,
) -> dict[str, Any] | None:
    result = provider.change(series_id, snapshot_date, lookback_days=lookback_days, lag_days=lag_days)
    if result is None:
        return None
    value, start, end = result
    return _factor(name, series_id, value, start, end, positive_when_up, thresholds, "change")


def _pct_factor(
    provider: FredDataProvider,
    series_id: str,
    snapshot_date: date,
    lookback_days: int,
    name: str,
    *,
    positive_when_up: bool,
    thresholds: tuple[float, float, float],
    lag_days: int = 0,
) -> dict[str, Any] | None:
    result = provider.pct_change(series_id, snapshot_date, lookback_days=lookback_days, lag_days=lag_days)
    if result is None:
        return None
    value, start, end = result
    return _factor(name, series_id, value, start, end, positive_when_up, thresholds, "pct_change")


def _relative_pct_factor(
    provider: FredDataProvider,
    asset_series: str,
    benchmark_series: str,
    snapshot_date: date,
    lookback_days: int,
    name: str,
    *,
    thresholds: tuple[float, float, float],
) -> dict[str, Any] | None:
    asset = provider.pct_change(asset_series, snapshot_date, lookback_days=lookback_days)
    benchmark = provider.pct_change(benchmark_series, snapshot_date, lookback_days=lookback_days)
    if asset is None or benchmark is None:
        return None
    asset_return, asset_start, asset_end = asset
    benchmark_return, _, _ = benchmark
    value = asset_return - benchmark_return
    return _factor(name, f"{asset_series}/{benchmark_series}", value, asset_start, asset_end, True, thresholds, "relative_pct_change")


def _relative_change_factor(
    provider: FredDataProvider,
    asset_series: str,
    benchmark_series: str,
    snapshot_date: date,
    lookback_days: int,
    name: str,
    *,
    positive_when_up: bool,
    thresholds: tuple[float, float, float],
) -> dict[str, Any] | None:
    asset = provider.change(asset_series, snapshot_date, lookback_days=lookback_days)
    benchmark = provider.change(benchmark_series, snapshot_date, lookback_days=lookback_days)
    if asset is None or benchmark is None:
        return None
    asset_change, asset_start, asset_end = asset
    benchmark_change, _, _ = benchmark
    value = asset_change - benchmark_change
    return _factor(name, f"{asset_series}/{benchmark_series}", value, asset_start, asset_end, positive_when_up, thresholds, "relative_change")


def _yoy_delta_factor(
    provider: FredDataProvider,
    series_id: str,
    snapshot_date: date,
    name: str,
    *,
    positive_when_up: bool,
    thresholds: tuple[float, float, float],
) -> dict[str, Any] | None:
    result = provider.yoy_change_delta(series_id, snapshot_date)
    if result is None:
        return None
    value, metadata = result
    score = _score_delta(value, positive_when_up=positive_when_up, thresholds=thresholds)
    return {
        "name": name,
        "series": series_id,
        "method": "yoy_delta",
        "raw_value": value,
        "score": score,
        "metadata": metadata,
        "source": FRED_SERIES[series_id],
    }


UNDERLYING_SIGNAL_BY_FACTOR = {
    "financial_conditions": "financial_conditions",
    "growth_regime": "economic_activity",
    "labor_market": "labor_market",
    "corporate_profitability": "profitability_data",
    "macro_context_only": "economic_activity",
    "financial_conditions_context": "financial_conditions",
    "earnings_context_only": "profitability_data",
    "refinancing_burden": "leverage_data",
    "credit_sensitivity": "credit_conditions_data",
    "domestic_growth_sensitivity": "economic_activity",
    "labor_market_pressure": "labor_market",
    "labor_market_context": "labor_market",
    "inflation_trend": "inflation_data",
    "inflation_trend_context": "inflation_data",
    "policy_rate_outlook": "policy_rate_data",
    "market_liquidity": "financial_conditions",
    "credit_conditions": "credit_conditions_data",
    "leverage_pressure": "leverage_data",
    "quality_trend": "leverage_data",
    "price_momentum": "equity_price",
    "relative_strength": "equity_relative_price",
    "risk_appetite": "risk_price",
    "yield_price_proxy": "treasury_yield_market",
    "breakeven_inflation": "breakeven_market",
    "hy_spread_relative_to_ig": "credit_spread_market",
    "ig_spread": "credit_spread_market",
    "real_yield_market_proxy": "real_yield_market",
    "gold_price": "gold_price",
    "usd": "usd_market",
    "safe_haven_proxy": "risk_price",
    "gold_volatility": "gold_market_risk",
}


def _annotate_factors(factors: list[dict[str, Any]], *, layer: str, model: CoreCellModel) -> None:
    for factor in factors:
        name = str(factor.get("name") or "")
        method = str(factor.get("method") or "")
        factor["layer"] = layer
        factor["provider"] = "FRED" if layer in {"data", "market"} else factor.get("provider", "")
        factor["benchmark"] = model.benchmark
        factor["formula_version"] = FORMULA_VERSION
        factor["underlying_signal"] = UNDERLYING_SIGNAL_BY_FACTOR.get(name, name)
        factor["benchmark_relative"] = method in {"relative_pct_change", "relative_change"} or name in {
            "yield_price_proxy",
            "breakeven_inflation",
            "ig_spread",
            "real_yield_market_proxy",
            "usd",
            "safe_haven_proxy",
        }
        factor["model_formula"] = model.formula


def _factor(
    name: str,
    series_id: str,
    value: float,
    start: TimePoint,
    end: TimePoint,
    positive_when_up: bool,
    thresholds: tuple[float, float, float],
    method: str,
) -> dict[str, Any]:
    return {
        "name": name,
        "series": series_id,
        "method": method,
        "raw_value": value,
        "score": _score_delta(value, positive_when_up=positive_when_up, thresholds=thresholds),
        "start_date": start.observation_date.isoformat(),
        "end_date": end.observation_date.isoformat(),
        "start_value": start.value,
        "end_value": end.value,
        "source": FRED_SERIES.get(series_id.split("/")[0], "FRED"),
    }


def _cell_factor(factor: dict[str, Any] | None, cell_specific: bool) -> dict[str, Any] | None:
    if factor is None:
        return None
    factor["cell_specific"] = cell_specific
    return factor


def _score_delta(value: float, *, positive_when_up: bool, thresholds: tuple[float, float, float]) -> int:
    sign = 1 if value > 0 else -1 if value < 0 else 0
    if not positive_when_up:
        sign *= -1
    magnitude = abs(value)
    if magnitude >= thresholds[2]:
        bucket = 3
    elif magnitude >= thresholds[1]:
        bucket = 2
    elif magnitude >= thresholds[0]:
        bucket = 1
    else:
        bucket = 0
    return sign * bucket


def _average_factor_score(factors: list[dict[str, Any]]) -> int | None:
    scored = [factor["score"] for factor in factors if factor.get("score") is not None]
    if not scored:
        return None
    return int(clip_score(_round(sum(scored) / len(scored))))


def _round(value: float) -> int:
    if value >= 0:
        return int(math.floor(value + 0.5))
    return int(math.ceil(value - 0.5))


def _sign(value: int | None) -> int:
    if value is None:
        return 0
    return 1 if value > 0 else -1 if value < 0 else 0


def _count(values) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[str(value)] = counts.get(str(value), 0) + 1
    return counts


def _parse_fred_csv(raw: str, series_id: str) -> list[TimePoint]:
    if not raw:
        return []
    rows: list[TimePoint] = []
    for row in csv.DictReader(io.StringIO(raw)):
        raw_value = row.get(series_id)
        if raw_value in {None, "", "."}:
            continue
        try:
            rows.append(TimePoint(date.fromisoformat(str(row["observation_date"])), float(raw_value)))
        except (KeyError, ValueError):
            continue
    return rows


def _ru_component(name: str) -> str:
    return {"research": "исследования", "data": "данные", "market": "рыночная динамика"}.get(name, name)


def _ru_divergence(value: str) -> str:
    return {
        CONFIRMED: "Подтверждённый вектор",
        EARLY: "Ранняя идея",
        PRICE_LED: "Рынок опережает аналитику",
        NARRATIVE_ONLY: "Только аналитический нарратив",
        MIXED: "Смешанный сигнал",
        INSUFFICIENT: "Недостаточно подтверждений",
    }.get(value, value)
