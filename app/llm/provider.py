from __future__ import annotations

import json
import logging
import re
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from typing import Any

from pydantic import BaseModel, ValidationError

from app.config import Settings, get_settings
from app.domain.enums import Confidence, Direction, ExtractionMethod, ScenarioType
from app.domain.schemas import (
    ChangeExplanation,
    EvidenceQuote,
    MarketSynthesisResponse,
    ThemeMatrixSynthesisResponse,
    ResearchViewDraft,
    ResearchViewsResponse,
    ScenarioDraft,
    ScenarioResponse,
    SignalExplanationResponse,
)

logger = logging.getLogger(__name__)

PROCESS_SESSION_KEY = f"process-{uuid.uuid4()}"


class LLMProvider(ABC):
    final_research_view_allowed = True

    @abstractmethod
    def extract_research_views(self, article: Any, schema: Any | None = None) -> list[ResearchViewDraft]:
        raise NotImplementedError

    @abstractmethod
    def generate_scenarios(self, context: dict[str, Any], schema: Any | None = None) -> list[ScenarioDraft]:
        raise NotImplementedError

    @abstractmethod
    def summarize_change(self, previous: Any, current: Any) -> ChangeExplanation:
        raise NotImplementedError

    @abstractmethod
    def explain_signal(self, context: dict[str, Any]) -> str:
        raise NotImplementedError

    def synthesize_market_themes(self, source_pack: list[dict[str, Any]], context: dict[str, Any]) -> MarketSynthesisResponse:
        raise NotImplementedError

    def synthesize_theme_matrix(self, context: dict[str, Any]) -> ThemeMatrixSynthesisResponse:
        raise NotImplementedError


@dataclass(frozen=True)
class LLMUsageSummary:
    daily_limit: int
    daily_used: int
    session_limit: int
    session_used: int

    @property
    def daily_remaining(self) -> int:
        return max(0, self.daily_limit - self.daily_used)

    @property
    def session_remaining(self) -> int:
        return max(0, self.session_limit - self.session_used)


@dataclass(frozen=True)
class OpenAIConnectionResult:
    status: str
    message: str
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "success"


class MockLLMProvider(LLMProvider):
    """Deterministic provider used for demo and tests; no external calls."""

    def extract_research_views(self, article: Any, schema: Any | None = None) -> list[ResearchViewDraft]:
        title = article.title.lower()
        text = article.content_text
        source_url = article.source_reference
        if "fi institution a" in title and "t1" in title:
            return [self._demo_view_for(article, "Europe", "FIXED INCOME", "GOV", "Short Term", Direction.NEUTRAL, 0, Confidence.MEDIUM, ["ставки близки к пику"], ["липкая инфляция"], ["заседание ЕЦБ"], "DEMO: Europe GOV Short Term сохраняет нейтральную оценку.")]
        if "fi institution a" in title and "t2" in title:
            return [self._demo_view_for(article, "Europe", "FIXED INCOME", "GOV", "Short Term", Direction.BULLISH, 2, Confidence.MEDIUM, ["ускорение дезинфляции", "ожидания снижения ставок"], ["неожиданный рост CPI"], ["комментарии ЕЦБ"], "DEMO: Europe Government Short Term повышен до overweight из-за дезинфляции.")]
        if "fi institution b" in title:
            return [self._demo_view_for(article, "Europe", "FIXED INCOME", "GOV", "Short Term", Direction.BULLISH, 1, Confidence.MEDIUM, ["подтверждение снижения front-end yields"], ["волатильность ставок"], ["инфляционный релиз"], "DEMO: независимый источник подтверждает поддержку Europe GOV Short Term.")]
        if "gold institution a" in title and "t1" in title:
            return [self._demo_view_for(article, "Global", "COMMODITIES", "Commodities", "Gold", Direction.NEUTRAL, 0, Confidence.MEDIUM, ["баланс real yields и геополитики"], ["сильный доллар"], ["данные по инфляции"], "DEMO: Global Gold нейтрален, драйверы сбалансированы.")]
        if "gold institution a" in title and "t2" in title:
            return [self._demo_view_for(article, "Global", "COMMODITIES", "Commodities", "Gold", Direction.BULLISH, 2, Confidence.MEDIUM, ["снижение real yields", "спрос центральных банков"], ["рост доллара"], ["данные по реальным ставкам"], "DEMO: Global Gold повышен до overweight из-за real yields и спроса ЦБ.")]
        if "gold institution b" in title:
            return [self._demo_view_for(article, "Global", "COMMODITIES", "Commodities", "Gold", Direction.BULLISH, 1, Confidence.MEDIUM, ["подтверждение спроса на защитные активы"], ["позиционирование"], ["потоки ETF"], "DEMO: независимый источник подтверждает позитивный взгляд на Global Gold.")]
        if "institution a" in title and "t1" in title:
            return [
                self._demo_view(
                    article,
                    Direction.NEUTRAL,
                    0,
                    Confidence.MEDIUM,
                    ["стабилизация спроса на технологический сектор"],
                    ["оценки остаются выше исторической медианы"],
                    ["публикация квартальных результатов"],
                    "DEMO: позиция по US Growth нейтральная, пересмотры прибыли смешанные.",
                )
            ]
        if "institution a" in title and "t2" in title:
            return [
                self._demo_view(
                    article,
                    Direction.BULLISH,
                    2,
                    Confidence.MEDIUM,
                    ["ускорение пересмотров прибыли", "расширение маржинальности"],
                    ["замедление capex в AI-инфраструктуре"],
                    ["следующий сезон отчетности"],
                    "DEMO: мы повышаем US Growth до overweight благодаря ускорению пересмотров прибыли.",
                )
            ]
        if "institution b" in title:
            return [
                self._demo_view(
                    article,
                    Direction.BULLISH,
                    2,
                    Confidence.MEDIUM,
                    ["подтверждение роста EPS", "устойчивый спрос на software"],
                    ["переполненность позиции"],
                    ["обновление прогнозов менеджмента"],
                    "DEMO: независимый источник подтверждает позитивный взгляд на US Growth.",
                )
            ]
        if "institution c" in title:
            return [
                self._demo_view(
                    article,
                    Direction.BEARISH,
                    -1,
                    Confidence.LOW,
                    ["риски оценки и регуляторного давления"],
                    ["разворот momentum"],
                    ["антимонопольные решения"],
                    "DEMO: источник C сохраняет осторожность по US Growth из-за оценки.",
                )
            ]
        if "institution d" in title or "stable negative" in title:
            return [
                ResearchViewDraft(
                    institution="DEMO Institution D",
                    region="Europe",
                    asset_class="FIXED INCOME",
                    asset_group="GOV",
                    asset_segment="Mid Term",
                    direction=Direction.NEUTRAL,
                    position_score=0,
                    confidence=Confidence.LOW,
                    drivers=["стабильные ожидания по ставкам"],
                    risks=["волатильность инфляции"],
                    catalysts=["заседание ЕЦБ"],
                    evidence_quotes=[EvidenceQuote(quote="DEMO: взгляд на короткие госбумаги Европы почти не изменился.", source_url=source_url)],
                    extraction_method=ExtractionMethod.DEMO,
                )
            ]
        return [self._manual_heuristic(article, text)]

    def generate_scenarios(self, context: dict[str, Any], schema: Any | None = None) -> list[ScenarioDraft]:
        source_ref = context.get("source_reference", "manual/demo")
        asset = context.get("asset", "asset")
        if str(asset).lower() in {"short term", "gov short term"}:
            return self._fixed_income_scenarios(source_ref, str(context.get("direction") or ""), int(context.get("position_score") or 0))
        if str(asset).lower() == "gold":
            return self._gold_scenarios(source_ref)
        return [
            ScenarioDraft(
                scenario_type=ScenarioType.BASE,
                title="BASE: пересмотры прибыли продолжают поддерживать сектор",
                description="DEMO: базовый путь предполагает устойчивые EPS revisions и умеренную реакцию рынка.",
                assumptions=["EPS revisions по US Growth остаются положительными", "маржинальность не ухудшается"],
                triggers=["две недели подряд положительных пересмотров прибыли", "сохранение guidance компаний"],
                early_indicators=["динамика earnings revisions", "ширина участия внутри сектора"],
                beneficiaries=["US Growth", "Growth equities"],
                vulnerable_assets=["Long duration defensives"],
                expected_reaction="Положительная реакция US Growth относительно широкого рынка.",
                reversal_conditions=["пересмотры EPS становятся отрицательными", "рынок полностью закладывает сценарий"],
                probability_band="MEDIUM_HIGH",
                source_references=[{"source_reference": source_ref, "note": "DEMO material change"}],
            ),
            ScenarioDraft(
                scenario_type=ScenarioType.UPSIDE,
                title="UPSIDE: AI capex ускоряет операционный leverage",
                description="DEMO: лучший путь связан с расширением спроса и повышением мультипликатора качества.",
                assumptions=["спрос на AI-инфраструктуру выше ожиданий", "облачные бюджеты растут быстрее consensus"],
                triggers=["ускорение заказов на полупроводники", "повышение guidance крупными платформами"],
                early_indicators=["book-to-bill", "cloud revenue growth"],
                beneficiaries=["Semiconductors", "Software platforms"],
                vulnerable_assets=["Low growth value sectors"],
                expected_reaction="Сильный рост Growth и Technology против Value.",
                reversal_conditions=["capex планы крупных покупателей сокращаются"],
                probability_band="MEDIUM",
                source_references=[{"source_reference": source_ref, "note": "DEMO upside path"}],
            ),
            ScenarioDraft(
                scenario_type=ScenarioType.DOWNSIDE,
                title="DOWNSIDE: оценки сжимают эффект позитивных earnings",
                description="DEMO: риск-сценарий допускает, что ставка дисконтирования и crowded positioning гасят фундаментальный плюс.",
                assumptions=["реальные доходности растут", "позиционирование в секторе перегрето"],
                triggers=["рост 10Y real yield", "сильный outflow из growth funds"],
                early_indicators=["real yields", "fund flows"],
                beneficiaries=["Short duration equities", "Cash"],
                vulnerable_assets=["US Growth", "Long duration Growth"],
                expected_reaction="Относительное отставание US Growth несмотря на стабильные earnings.",
                reversal_conditions=["доходности снижаются", "позиционирование нормализуется"],
                probability_band="LOW_MEDIUM",
                source_references=[{"source_reference": source_ref, "note": "DEMO downside path"}],
            ),
        ]

    def summarize_change(self, previous: Any, current: Any) -> ChangeExplanation:
        change_types: list[str] = []
        if previous.direction != current.direction:
            change_types.append("DIRECTION_CHANGE")
        old_score = previous.position_score if previous.position_score is not None else 0
        new_score = current.position_score if current.position_score is not None else 0
        if old_score != new_score:
            change_types.append("SCORE_CHANGE")
        previous_drivers = set(previous.drivers or [])
        current_drivers = set(current.drivers or [])
        if current_drivers - previous_drivers:
            change_types.append("NEW_DRIVER")
        if previous_drivers - current_drivers:
            change_types.append("REMOVED_DRIVER")
        if previous.confidence != current.confidence:
            change_types.append("CONFIDENCE_CHANGE")
        if not change_types:
            change_types.append("NO_MATERIAL_CHANGE")
        return ChangeExplanation(
            explanation=(
                f"DEMO/RULE: направление {previous.direction} -> {current.direction}, "
                f"score {old_score} -> {new_score}; новые драйверы: {', '.join(current_drivers - previous_drivers) or 'нет'}."
            ),
            change_types=change_types,
        )

    def explain_signal(self, context: dict[str, Any]) -> str:
        return (
            f"{context['asset']} · {context['direction']} · {context['what_changed']} · "
            f"{context['dominant_scenario']} · evidence {context['evidence_status']} · "
            f"pricing {context['pricing_status']} · trigger/veto сохранены."
        )

    def synthesize_market_themes(self, source_pack: list[dict[str, Any]], context: dict[str, Any]) -> MarketSynthesisResponse:
        docs = []
        for item in source_pack:
            docs.append(
                {
                    "source_id": str(item["source_id"]),
                    "provider": str(item["provider"]),
                    "title": str(item["title"]),
                    "date": str(item["date"]),
                    "url": str(item["url"]),
                    "document_type": str(item.get("document_type") or "market outlook"),
                    "short_extracted_view": str(item.get("selection_reason") or item["title"])[:280],
                    "relevant_regions": list(item.get("coverage_regions") or ["Global"]),
                    "relevant_asset_classes": list(item.get("coverage_asset_classes") or ["Equities"]),
                    "drivers": ["policy path", "earnings outlook", "risk appetite"],
                    "risks": ["inflation surprise", "policy repricing"],
                }
            )
        source_ids = [doc["source_id"] for doc in docs] or ["DEMO_SOURCE"]
        themes = [
            ("TH01", "Global disinflation is uneven but policy-sensitive assets remain supported", "POSITIVE", ["Global", "US", "Europe"], ["Government Bonds", "Equities"]),
            ("TH02", "US earnings resilience supports quality and growth leadership", "POSITIVE", ["US", "Global"], ["Equities"]),
            ("TH03", "Europe policy easing is conditional on inflation progress", "NEUTRAL", ["Europe"], ["Government Bonds", "Credit", "Equities"]),
            ("TH04", "UK financial conditions remain credit-sensitive", "NEUTRAL", ["UK"], ["Credit", "Government Bonds"]),
            ("TH05", "Japan reflation keeps duration risk two-sided", "MIXED", ["Japan"], ["Government Bonds", "Equities"]),
            ("TH06", "EM ex China sentiment improves but remains dollar-sensitive", "POSITIVE", ["EM ex China"], ["Equities", "Credit"]),
            ("TH07", "Credit spreads require selectivity after strong carry performance", "MIXED", ["Global", "US", "Europe", "UK"], ["Credit"]),
            ("TH08", "Gold and commodities retain hedge value against geopolitical and real-rate shocks", "POSITIVE", ["Global"], ["Commodities"]),
        ]
        return MarketSynthesisResponse.model_validate(
            {
                "documents": docs,
                "themes": [
                    {
                        "theme_id": theme_id,
                        "title": title,
                        "direction": direction,
                        "affected_regions": regions,
                        "affected_asset_classes": assets,
                        "implication_6_12m": title,
                        "supporting_source_ids": source_ids[: max(1, min(3, len(source_ids)))],
                        "contradicting_source_ids": [],
                        "confidence": "MEDIUM",
                        "invalidation_condition": "Invalidate if newer source pack evidence reverses the theme.",
                    }
                    for theme_id, title, direction, regions, assets in themes
                ],
            }
        )

    def synthesize_theme_matrix(self, context: dict[str, Any]) -> ThemeMatrixSynthesisResponse:
        from app.services.normalization import canonical_cell_registry

        themes = context.get("themes") or []
        theme_ids = [str(theme.get("theme_id")) for theme in themes] or ["TH01"]
        sources_by_theme = {
            str(theme.get("theme_id")): [str(item) for item in theme.get("supporting_source_ids") or []]
            for theme in themes
        }
        cells = []
        for cell in canonical_cell_registry(include_not_applicable=False):
            score = 0
            segment = cell.asset_segment.casefold()
            region = cell.region
            if "growth" in segment or "large cap" in segment:
                score = 1 if region in {"US", "Global"} else 0
            elif "small cap" in segment:
                score = -1 if region in {"Europe", "Japan", "EM ex China"} else 0
            elif "long term" in segment and cell.asset_class == "FIXED INCOME":
                score = 1 if region in {"US", "UK"} else -1 if region in {"Japan", "EM ex China"} else 0
            elif "inflation linked" in segment:
                score = -1
            elif "gold" in segment:
                score = 1
            support = theme_ids[:2]
            source_ids = sorted({source for theme_id in support for source in sources_by_theme.get(theme_id, [])})[:4]
            cells.append(
                {
                    "canonical_cell_id": cell.canonical_cell_id,
                    "score": score,
                    "confidence": "MEDIUM" if score else "LOW",
                    "thesis": f"{cell.region} / {cell.asset_segment}: theme-based 6-12 month assessment.",
                    "supporting_theme_ids": support,
                    "supporting_source_ids": source_ids or list(context.get("source_ids") or [])[:1],
                    "main_risk": "Theme evidence could reverse with newer macro or market publications.",
                    "invalidation_condition": "Invalidate if newer admitted documents contradict the supporting themes.",
                }
            )
        scenarios = [
            {
                "scenario_id": "BASE",
                "scenario_name": "Theme baseline",
                "narrative": "Core market themes persist over the 6-12 month horizon.",
                "probability_band": "45-55%",
                "causal_chain": "source pack -> market themes -> MAE cell implications",
                "trigger": "Incoming data remains consistent with the source pack themes.",
                "veto": "Invalidate if newer source-pack evidence reverses the core themes.",
                "material_affected_cells": [cell["canonical_cell_id"] for cell in cells[:12]],
            },
            {
                "scenario_id": "UPSIDE",
                "scenario_name": "Easing and earnings extension",
                "narrative": "Disinflation and resilient earnings broaden risk appetite.",
                "probability_band": "20-30%",
                "causal_chain": "easier policy expectations -> lower discount-rate pressure -> positive cells improve",
                "trigger": "Inflation and earnings data improve together.",
                "veto": "Invalidate if rates and risk premia rise together.",
                "material_affected_cells": [cell["canonical_cell_id"] for cell in cells if cell["score"] > 0][:12],
            },
            {
                "scenario_id": "DOWNSIDE",
                "scenario_name": "Sticky inflation and risk-off",
                "narrative": "Sticky inflation, tighter liquidity or credit stress pressures vulnerable cells.",
                "probability_band": "20-30%",
                "causal_chain": "inflation or liquidity shock -> rates/spreads repricing -> negative cells weaken",
                "trigger": "Inflation surprise, spread widening or earnings downgrades intensify.",
                "veto": "Invalidate if disinflation and liquidity conditions improve.",
                "material_affected_cells": [cell["canonical_cell_id"] for cell in cells if cell["score"] < 0][:12],
            },
        ]
        return ThemeMatrixSynthesisResponse.model_validate({"cells": cells, "scenarios": scenarios})

    def _demo_view(
        self,
        article: Any,
        direction: Direction,
        score: int,
        confidence: Confidence,
        drivers: list[str],
        risks: list[str],
        catalysts: list[str],
        quote: str,
    ) -> ResearchViewDraft:
        return ResearchViewDraft(
            institution=article.source.institution_name,
            region="US",
            asset_class="EQUITY",
            asset_group="Other categories",
            asset_segment="Growth",
            direction=direction,
            position_score=score,
            confidence=confidence,
            drivers=drivers,
            risks=risks,
            catalysts=catalysts,
            evidence_quotes=[EvidenceQuote(quote=quote, locator="DEMO article text", source_url=article.source_reference)],
            extraction_method=ExtractionMethod.DEMO,
        )

    def _demo_view_for(
        self,
        article: Any,
        region: str,
        asset_class: str,
        asset_group: str,
        asset_segment: str,
        direction: Direction,
        score: int,
        confidence: Confidence,
        drivers: list[str],
        risks: list[str],
        catalysts: list[str],
        quote: str,
    ) -> ResearchViewDraft:
        return ResearchViewDraft(
            institution=article.source.institution_name,
            region=region,
            asset_class=asset_class,
            asset_group=asset_group,
            asset_segment=asset_segment,
            direction=direction,
            position_score=score,
            confidence=confidence,
            drivers=drivers,
            risks=risks,
            catalysts=catalysts,
            evidence_quotes=[EvidenceQuote(quote=quote, locator="DEMO article text", source_url=article.source_reference)],
            extraction_method=ExtractionMethod.DEMO,
        )

    def _fixed_income_scenarios(self, source_ref: str, direction: str = "", position_score: int = 0) -> list[ScenarioDraft]:
        if direction == Direction.BEARISH.value or position_score < 0:
            return [
                ScenarioDraft(
                    scenario_type=ScenarioType.BASE,
                    title="BASE: higher-for-longer давит на короткие госбумаги Европы",
                    description="DEMO/RULE: базовый путь следует hawkish сигналу: ожидания более высоких ставок держат front-end yields повышенными.",
                    assumptions=["инфляционное давление остаётся липким", "ЕЦБ сохраняет ограничительный тон"],
                    triggers=["hawkish ECB communication", "рост short-end sovereign yields"],
                    early_indicators=["services CPI", "2Y yields", "wage trackers"],
                    beneficiaries=["Cash"],
                    vulnerable_assets=["Europe GOV Short Term"],
                    expected_reaction="Отрицательная реакция Europe Government Short Term относительно cash-like инструментов.",
                    reversal_conditions=["инфляция уверенно замедляется", "ЕЦБ явно открывает путь к cuts"],
                    probability_band="MEDIUM_HIGH",
                    source_references=[{"source_reference": source_ref, "note": "RULE FI hawkish material change"}],
                ),
                ScenarioDraft(
                    scenario_type=ScenarioType.UPSIDE,
                    title="UPSIDE: инфляция быстро смягчается и снижает давление на front-end",
                    description="DEMO/RULE: позитивная ветка требует новой фактической поддержки снижения инфляции и repricing ставок.",
                    assumptions=["CPI выходит ниже ожиданий", "market-implied cuts увеличиваются"],
                    triggers=["CPI ниже consensus", "dovish ECB minutes"],
                    early_indicators=["CPI surprises", "OIS pricing"],
                    beneficiaries=["Europe GOV Short Term"],
                    vulnerable_assets=["Cash"],
                    expected_reaction="Восстановление коротких госбумаг только при новой dovish evidence.",
                    reversal_conditions=["hawkish guidance сохраняется", "wage growth не замедляется"],
                    probability_band="MEDIUM",
                    source_references=[{"source_reference": source_ref, "note": "RULE FI upside requires new evidence"}],
                ),
                ScenarioDraft(
                    scenario_type=ScenarioType.DOWNSIDE,
                    title="DOWNSIDE: sticky inflation усиливает hawkish repricing",
                    description="DEMO/RULE: риск-сценарий усиливает исходный higher-for-longer тезис.",
                    assumptions=["services inflation ускоряется", "зарплатное давление остаётся высоким"],
                    triggers=["CPI выше ожиданий", "front-end yields rise"],
                    early_indicators=["services CPI", "2Y yields"],
                    beneficiaries=["Cash", "floating-rate instruments"],
                    vulnerable_assets=["Europe GOV Short Term"],
                    expected_reaction="Более сильное отставание краткосрочных госбумаг.",
                    reversal_conditions=["инфляция возвращается к нисходящей траектории"],
                    probability_band="LOW_MEDIUM",
                    source_references=[{"source_reference": source_ref, "note": "RULE FI downside"}],
                ),
            ]
        if direction == Direction.NEUTRAL.value and position_score == 0:
            return [
                ScenarioDraft(
                    scenario_type=ScenarioType.BASE,
                    title="BASE: короткие госбумаги Европы остаются нейтральными",
                    description="DEMO/RULE: ставки и инфляция дают сбалансированный сигнал без подтверждённого тактического уклона.",
                    assumptions=["политика ЕЦБ остаётся зависимой от данных", "инфляционные сюрпризы ограничены"],
                    triggers=["стабильные OIS expectations", "CPI около consensus"],
                    early_indicators=["OIS pricing", "CPI surprises"],
                    beneficiaries=["Diversified fixed income"],
                    vulnerable_assets=["Directional duration bets"],
                    expected_reaction="Нейтральная реакция Europe Government Short Term.",
                    reversal_conditions=["hawkish или dovish guidance становится однозначным"],
                    probability_band="MEDIUM_HIGH",
                    source_references=[{"source_reference": source_ref, "note": "RULE FI neutral"}],
                ),
                ScenarioDraft(
                    scenario_type=ScenarioType.UPSIDE,
                    title="UPSIDE: дезинфляция создаёт поддержку коротким госбумагам",
                    description="DEMO/RULE: позитивная ветка требует новых признаков cuts или устойчивой дезинфляции.",
                    assumptions=["CPI ниже ожиданий", "рынок повышает вероятность cuts"],
                    triggers=["dovish ECB communication", "снижение front-end yields"],
                    early_indicators=["CPI surprises", "2Y yields"],
                    beneficiaries=["Europe GOV Short Term"],
                    vulnerable_assets=["Cash"],
                    expected_reaction="Положительная реакция коротких госбумаг.",
                    reversal_conditions=["инфляция вновь ускоряется"],
                    probability_band="MEDIUM",
                    source_references=[{"source_reference": source_ref, "note": "RULE FI upside"}],
                ),
                ScenarioDraft(
                    scenario_type=ScenarioType.DOWNSIDE,
                    title="DOWNSIDE: higher-for-longer возвращает давление",
                    description="DEMO/RULE: негативная ветка связана с hawkish repricing и sticky inflation.",
                    assumptions=["services inflation липкая", "ЕЦБ сохраняет restrictive stance"],
                    triggers=["hawkish ECB communication", "рост 2Y yields"],
                    early_indicators=["services CPI", "wage trackers"],
                    beneficiaries=["Cash"],
                    vulnerable_assets=["Europe GOV Short Term"],
                    expected_reaction="Отрицательная реакция коротких госбумаг.",
                    reversal_conditions=["инфляция устойчиво снижается"],
                    probability_band="LOW_MEDIUM",
                    source_references=[{"source_reference": source_ref, "note": "RULE FI downside"}],
                ),
            ]
        return [
            ScenarioDraft(
                scenario_type=ScenarioType.BASE,
                title="BASE: дезинфляция поддерживает короткие госбумаги Европы",
                description="DEMO: front-end yields постепенно снижаются, улучшая carry/roll-down.",
                assumptions=["инфляция замедляется", "ЕЦБ сохраняет мягкий уклон"],
                triggers=["два релиза CPI ниже ожиданий", "снижение front-end yields"],
                early_indicators=["CPI surprises", "2Y yields"],
                beneficiaries=["Europe GOV Short Term"],
                vulnerable_assets=["Cash"],
                expected_reaction="Положительная реакция Europe Government Short Term.",
                reversal_conditions=["инфляция вновь ускоряется", "ЕЦБ ужесточает guidance"],
                probability_band="MEDIUM_HIGH",
                source_references=[{"source_reference": source_ref, "note": "DEMO FI material change"}],
            ),
            ScenarioDraft(
                scenario_type=ScenarioType.UPSIDE,
                title="UPSIDE: быстрый цикл снижения ставок",
                description="DEMO: короткий конец кривой получает дополнительный импульс от repricing ставок.",
                assumptions=["рынок быстро закладывает cuts", "реальная доходность снижается"],
                triggers=["dovish ECB minutes", "ухудшение PMI"],
                early_indicators=["OIS pricing", "PMI"],
                beneficiaries=["Short-term government bonds"],
                vulnerable_assets=["Cash-like instruments"],
                expected_reaction="Более сильный рост коротких госбумаг.",
                reversal_conditions=["рынок переоценивает скорость cuts"],
                probability_band="MEDIUM",
                source_references=[{"source_reference": source_ref, "note": "DEMO FI upside"}],
            ),
            ScenarioDraft(
                scenario_type=ScenarioType.DOWNSIDE,
                title="DOWNSIDE: sticky inflation keeps front-end yields high",
                description="DEMO: инфляционные сюрпризы задерживают снижение ставок.",
                assumptions=["services inflation липкая", "wage growth остаётся высоким"],
                triggers=["CPI выше ожиданий", "hawkish ECB communication"],
                early_indicators=["services CPI", "wage trackers"],
                beneficiaries=["Cash"],
                vulnerable_assets=["Europe GOV Short Term"],
                expected_reaction="Слабая или отрицательная реакция short-term government bonds.",
                reversal_conditions=["инфляция возвращается к нисходящей траектории"],
                probability_band="LOW_MEDIUM",
                source_references=[{"source_reference": source_ref, "note": "DEMO FI downside"}],
            ),
        ]

    def _gold_scenarios(self, source_ref: str) -> list[ScenarioDraft]:
        return [
            ScenarioDraft(
                scenario_type=ScenarioType.BASE,
                title="BASE: real yields fall and gold demand holds",
                description="DEMO: золото поддержано снижением real yields и устойчивым спросом центральных банков.",
                assumptions=["реальные ставки снижаются", "central bank demand остаётся сильным"],
                triggers=["снижение real yields", "положительные данные по flows"],
                early_indicators=["10Y real yield", "ETF flows", "central bank purchases"],
                beneficiaries=["Global Gold"],
                vulnerable_assets=["USD cash"],
                expected_reaction="Положительная реакция Global Gold.",
                reversal_conditions=["real yields резко растут", "доллар укрепляется"],
                probability_band="MEDIUM_HIGH",
                source_references=[{"source_reference": source_ref, "note": "DEMO Gold material change"}],
            ),
            ScenarioDraft(
                scenario_type=ScenarioType.UPSIDE,
                title="UPSIDE: risk aversion accelerates safe-haven demand",
                description="DEMO: геополитический риск усиливает приток в защитные активы.",
                assumptions=["volatility rises", "safe-haven demand accelerates"],
                triggers=["рост VIX", "ETF inflows"],
                early_indicators=["VIX", "gold ETF flows"],
                beneficiaries=["Gold", "defensive assets"],
                vulnerable_assets=["Cyclical equities"],
                expected_reaction="Сильный рост золота против риск-активов.",
                reversal_conditions=["risk appetite recovers"],
                probability_band="MEDIUM",
                source_references=[{"source_reference": source_ref, "note": "DEMO Gold upside"}],
            ),
            ScenarioDraft(
                scenario_type=ScenarioType.DOWNSIDE,
                title="DOWNSIDE: dollar rebound offsets gold support",
                description="DEMO: рост доллара и доходностей гасит фундаментальный спрос.",
                assumptions=["USD strengthens", "real yields rebound"],
                triggers=["DXY breakout", "hawkish policy repricing"],
                early_indicators=["DXY", "real yields"],
                beneficiaries=["USD cash"],
                vulnerable_assets=["Global Gold"],
                expected_reaction="Отставание золота или откат.",
                reversal_conditions=["доллар теряет momentum"],
                probability_band="LOW_MEDIUM",
                source_references=[{"source_reference": source_ref, "note": "DEMO Gold downside"}],
            ),
        ]

    def _manual_heuristic(self, article: Any, text: str) -> ResearchViewDraft:
        combined = f"{article.title}\n{text[:5000]}".lower()
        quote, locator = _best_supporting_quote(text)
        source_url = article.source_reference
        if re.search(r"\b(ecb|european central bank|euro area|governing council)\b", combined):
            return self._manual_fixed_income_view(article, "Europe", combined, quote, locator, source_url)
        if re.search(r"\b(bank of england|financial policy committee|monetary policy committee|uk financial system)\b", combined):
            return self._manual_fixed_income_view(article, "UK", combined, quote, locator, source_url)
        if re.search(r"\b(federal reserve|fomc|federal open market committee)\b", combined):
            return self._manual_fixed_income_view(article, "US", combined, quote, locator, source_url)
        if re.search(r"\b(gold|xau|bullion|central bank purchases)\b", combined):
            direction, score = self._manual_direction_score(combined)
            return ResearchViewDraft(
                institution=article.source.institution_name,
                region="Global",
                asset_class="GOLD",
                asset_group="GOLD",
                asset_segment="GOLD",
                direction=direction,
                position_score=score,
                confidence=Confidence.LOW,
                drivers=["правиловой сигнал из текста"],
                risks=["требуется дополнительное подтверждение источниками"],
                catalysts=[],
                evidence_quotes=[EvidenceQuote(quote=quote, locator=locator, source_url=source_url)],
                extraction_method=ExtractionMethod.RULE_BASED,
            )
        low = combined
        direction, score = self._manual_direction_score(low)
        is_technology = bool(re.search(r"\b(technology|tech|software|semiconductor|ai|технолог)\b", low))
        region = "US" if re.search(r"\b(us|u\\.s\\.|usa|сша|united states)\b", low) else "Global"
        asset_group = "Sector" if is_technology else "Wide Market"
        asset_segment = "Technology" if is_technology else "Wide Market"
        return ResearchViewDraft(
            institution=article.source.institution_name,
            region=region,
            asset_class="EQUITY",
            asset_group=asset_group,
            asset_segment=asset_segment,
            direction=direction,
            position_score=score,
            confidence=Confidence.LOW,
            drivers=["правиловой сигнал из текста"],
            risks=["требуется дополнительное подтверждение источниками"],
            catalysts=[],
            evidence_quotes=[EvidenceQuote(quote=quote, locator=locator, source_url=article.source_reference)],
            extraction_method=ExtractionMethod.RULE_BASED,
        )

    def _manual_fixed_income_view(
        self,
        article: Any,
        region: str,
        text: str,
        quote: str,
        locator: str,
        source_url: str,
    ) -> ResearchViewDraft:
        bullish_hits = len(re.findall(r"\b(cut|cuts|lower|lowered|reduced|dovish|easing|disinflation|сниж|смягч|дезинфляц)\b", text))
        bearish_hits = len(
            re.findall(
                r"\b(hawkish|tighten|tightening|raise|raised|hike|hikes|higher[- ]for[- ]longer|higher rates|persistent inflation|sticky inflation|restrictive|ужесточ|повыш)\b",
                text,
            )
        )
        direction = Direction.NEUTRAL
        score = 0
        if bullish_hits > bearish_hits:
            direction, score = Direction.BULLISH, 2 if bullish_hits >= 2 else 1
        elif bearish_hits > bullish_hits:
            direction, score = Direction.BEARISH, -2 if bearish_hits >= 2 else -1
        if direction == Direction.BULLISH:
            drivers = ["ожидания снижения ставок или дезинфляция из текста публикации"]
        elif direction == Direction.BEARISH:
            drivers = ["hawkish / higher-for-longer сигнал из текста публикации"]
        else:
            drivers = ["сбалансированный сигнал по ставкам из текста публикации"]
        return ResearchViewDraft(
            institution=article.source.institution_name,
            region=region,
            asset_class="FIXED INCOME",
            asset_group="GOV",
            asset_segment="Short Term",
            direction=direction,
            position_score=score,
            confidence=Confidence.LOW,
            drivers=drivers,
            risks=["требуется дополнительное подтверждение источниками"],
            catalysts=["следующее решение по ставке"],
            evidence_quotes=[EvidenceQuote(quote=quote, locator=locator, source_url=source_url)],
            extraction_method=ExtractionMethod.RULE_BASED,
        )

    @staticmethod
    def _manual_direction_score(text: str) -> tuple[Direction, int]:
        direction = Direction.NEUTRAL
        score = 0
        if re.search(r"\b(overweight|bullish|positive|upgrade|improved|улучш|позитив|повыш)", text):
            direction, score = Direction.BULLISH, 1
        if re.search(r"\b(underweight|bearish|negative|downgrade|deteriorated|ухудш|негатив|сниж)", text):
            direction, score = Direction.BEARISH, -1
        return direction, score


class RuleBasedProvider(MockLLMProvider):
    """Deterministic extraction for real fetched text when LLM calls are unavailable."""

    final_research_view_allowed = False

    def extract_research_views(self, article: Any, schema: Any | None = None) -> list[ResearchViewDraft]:
        combined = re.sub(r"\s+", " ", f"{article.title}\n{article.content_text[:12000]}").casefold()
        quote, locator = _best_supporting_quote(article.content_text)
        source_url = article.source_reference
        regions = _infer_rule_regions(combined, getattr(article.source, "institution_name", ""))
        views: list[ResearchViewDraft] = []

        def add_view(
            region: str,
            asset_class: str,
            asset_group: str,
            asset_segment: str,
            direction: Direction,
            score: int,
            drivers: list[str],
            risks: list[str] | None = None,
            catalysts: list[str] | None = None,
        ) -> None:
            key = (region, asset_class, asset_group, asset_segment)
            if any((v.region, v.asset_class, v.asset_group, v.asset_segment) == key for v in views):
                return
            views.append(
                ResearchViewDraft(
                    institution=article.source.institution_name,
                    region=region,
                    asset_class=asset_class,
                    asset_group=asset_group,
                    asset_segment=asset_segment,
                    direction=direction,
                    position_score=score,
                    confidence=Confidence.LOW,
                    drivers=drivers,
                    risks=risks or ["требуется дополнительное подтверждение источниками"],
                    catalysts=catalysts or ["следующая публикация источника"],
                    evidence_quotes=[EvidenceQuote(quote=quote, locator=locator, source_url=source_url)],
                    extraction_method=ExtractionMethod.RULE_BASED,
                )
            )

        equity_signal = _rule_direction_score(combined, _EQUITY_POSITIVE_TERMS, _EQUITY_NEGATIVE_TERMS)
        fixed_income_signal = _rule_direction_score(combined, _FI_POSITIVE_TERMS, _FI_NEGATIVE_TERMS)
        credit_signal = _rule_direction_score(combined, _CREDIT_POSITIVE_TERMS, _CREDIT_NEGATIVE_TERMS)
        gold_signal = _rule_direction_score(combined, _GOLD_POSITIVE_TERMS, _GOLD_NEGATIVE_TERMS)

        has_equity = _has_any(combined, _EQUITY_TERMS) or _has_any(combined, _BROAD_MARKET_TERMS)
        has_fi = _has_any(combined, _FIXED_INCOME_TERMS) or _is_central_bank_source(article.source.institution_name)
        has_credit = _has_any(combined, _CREDIT_TERMS)
        has_gold = _has_any(combined, _GOLD_TERMS)

        for region in regions:
            if has_equity:
                direction, score = equity_signal
                add_view(
                    region,
                    "EQUITY",
                    "Wide Market",
                    "Wide Market",
                    direction,
                    score,
                    ["правиловой вывод из текста о рынке акций, росте, прибыли или оценках"],
                    ["оценка требует подтверждения вторым источником"],
                    ["обновление earnings / macro outlook"],
                )
                if _has_any(combined, {"growth", "technology", "ai", "software", "semiconductor", "tech"}):
                    add_view(region, "EQUITY", "Other categories", "Growth", direction, score, ["growth/technology theme found in source text"])
                if _has_any(combined, {"value", "valuation", "cheap", "cyclical"}):
                    add_view(region, "EQUITY", "Other categories", "Value", direction, score, ["value/valuation theme found in source text"])
                if _has_any(combined, {"small cap", "small-cap", "smid"}):
                    add_view(region, "EQUITY", "Other categories", "Small Cap", direction, score, ["small-cap theme found in source text"])
                if _has_any(combined, {"large cap", "large-cap", "mega cap", "mega-cap", "magnificent", "quality large"}):
                    add_view(region, "EQUITY", "Other categories", "Large Cap", direction, score, ["large-cap / mega-cap theme found in source text"])
                if _has_any(combined, {"preferred stock", "preferred securities", "preferreds"}):
                    add_view(region, "EQUITY", "Other categories", "Preferred", direction, score, ["preferred securities theme found in source text"])
            if has_fi:
                direction, score = fixed_income_signal
                add_view(
                    region,
                    "FIXED INCOME",
                    "GOV",
                    "Short Term",
                    direction,
                    score,
                    ["ставки, инфляция или политика центрального банка обнаружены в тексте"],
                    ["duration signal requires additional source confirmation"],
                    ["следующее решение по ставке"],
                )
                if _has_any(combined, {"yield curve", "duration", "10-year", "10y", "long-term", "long term"}):
                    add_view(region, "FIXED INCOME", "GOV", "Long Term", direction, score, ["duration/yield-curve theme found in source text"])
                else:
                    add_view(region, "FIXED INCOME", "GOV", "Mid Term", direction, score, ["rates/yields theme found in source text"])
            if has_credit:
                direction, score = credit_signal
                add_view(region, "FIXED INCOME", "CORP IG", "Mid Term", direction, score, ["credit/spread theme found in source text"])
                if _has_any(combined, {"short credit", "short duration", "short-term credit", "front-end credit"}):
                    add_view(region, "FIXED INCOME", "CORP IG", "Short Term", direction, score, ["short-duration credit theme found in source text"])
                if _has_any(combined, {"long credit", "long duration", "long-term credit", "duration"}):
                    add_view(region, "FIXED INCOME", "CORP IG", "Long Term", direction, score, ["long-duration credit theme found in source text"])
                if _has_any(combined, {"high yield", "high-yield", "leveraged", "default"}):
                    add_view(region, "FIXED INCOME", "CORP HY", "Mid Term", direction, score, ["high-yield/default theme found in source text"])
                    if _has_any(combined, {"short duration", "short-term", "front-end"}):
                        add_view(region, "FIXED INCOME", "CORP HY", "Short Term", direction, score, ["short-duration high-yield theme found in source text"])
                    if _has_any(combined, {"long duration", "long-term", "duration"}):
                        add_view(region, "FIXED INCOME", "CORP HY", "Long Term", direction, score, ["long-duration high-yield theme found in source text"])

        if has_gold:
            direction, score = gold_signal
            add_view(
                "Global",
                "GOLD",
                "GOLD",
                "GOLD",
                direction,
                score,
                ["gold, real yields, dollar or commodity theme found in source text"],
                ["commodity signal requires additional source confirmation"],
                ["real-yield / USD move"],
            )

        if not views:
            views.append(self._manual_heuristic(article, article.content_text))
        return views[:14]

    def generate_scenarios(self, context: dict[str, Any], schema: Any | None = None) -> list[ScenarioDraft]:
        drafts = super().generate_scenarios(context, schema)
        cleaned: list[ScenarioDraft] = []
        for draft in drafts:
            payload = draft.model_dump()
            for field_name in ["title", "description", "expected_reaction"]:
                payload[field_name] = _strip_demo_markers(str(payload[field_name]))
            for field_name in ["assumptions", "triggers", "early_indicators", "beneficiaries", "vulnerable_assets", "reversal_conditions"]:
                payload[field_name] = [_strip_demo_markers(str(item)) for item in payload.get(field_name, [])]
            payload["source_references"] = [
                {**ref, "note": _strip_demo_markers(str(ref.get("note", "RULE_BASED")))}
                for ref in payload.get("source_references", [])
            ]
            cleaned.append(ScenarioDraft(**payload))
        return cleaned

    def summarize_change(self, previous: Any, current: Any) -> ChangeExplanation:
        explanation = super().summarize_change(previous, current)
        return ChangeExplanation(
            explanation=_strip_demo_markers(explanation.explanation),
            change_types=explanation.change_types,
        )

    def explain_signal(self, context: dict[str, Any]) -> str:
        return _strip_demo_markers(super().explain_signal(context))


QUOTE_SUPPORT_PATTERN = re.compile(
    r"\b("
    r"rate|rates|policy rate|inflation|disinflation|hawkish|dovish|hike|hikes|cut|cuts|easing|tightening|"
    r"yield|yields|wage|wages|earnings|eps|margin|guidance|valuation|real yield|central bank|"
    r"ставк|инфляц|дезинфляц|доходност|прибыл|прогноз|маржин"
    r")\b",
    re.IGNORECASE,
)


def _best_supporting_quote(text: str) -> tuple[str, str]:
    clean = " ".join((text or "").split())
    if not clean:
        return "manual", "manual text"
    sentences = [sentence.strip() for sentence in re.split(r"(?<=[.!?])\s+", clean) if sentence.strip()]
    for index, sentence in enumerate(sentences, start=1):
        if index == 1 and len(sentences) > 1 and not QUOTE_SUPPORT_PATTERN.search(sentence):
            continue
        if QUOTE_SUPPORT_PATTERN.search(sentence):
            return sentence[:420], f"source sentence {index}"
    if len(sentences) > 1:
        return sentences[1][:420], "source sentence 2"
    return sentences[0][:420], "source text"


_BROAD_MARKET_TERMS = {"market", "markets", "asset allocation", "multi-asset", "outlook", "strategy", "macro"}
_EQUITY_TERMS = {"equity", "equities", "stock", "stocks", "earnings", "eps", "valuation", "margin"}
_FIXED_INCOME_TERMS = {"bond", "bonds", "fixed income", "rates", "rate", "yield", "yields", "duration", "inflation"}
_CREDIT_TERMS = {"credit", "spread", "spreads", "corporate bond", "investment grade", "high yield", "default"}
_GOLD_TERMS = {"gold", "bullion", "real yield", "real yields", "commodity", "commodities"}
_EQUITY_POSITIVE_TERMS = {"overweight", "upgrade", "positive", "bullish", "earnings growth", "eps growth", "margin expansion", "resilient"}
_EQUITY_NEGATIVE_TERMS = {"underweight", "downgrade", "negative", "bearish", "earnings recession", "valuation risk", "margin pressure"}
_FI_POSITIVE_TERMS = {"rate cut", "rate cuts", "cuts", "lower rates", "easing", "disinflation", "yields down", "dovish"}
_FI_NEGATIVE_TERMS = {"rate hike", "rate hikes", "higher-for-longer", "higher for longer", "sticky inflation", "restrictive", "hawkish"}
_CREDIT_POSITIVE_TERMS = {"spread tightening", "spreads tighten", "credit quality", "upgrade", "low default", "resilient balance"}
_CREDIT_NEGATIVE_TERMS = {"spread widening", "spreads widen", "downgrade", "default", "credit stress", "refinancing risk"}
_GOLD_POSITIVE_TERMS = {"real yields fall", "real yields down", "central bank demand", "safe haven", "geopolitical", "dollar weakness"}
_GOLD_NEGATIVE_TERMS = {"real yields rise", "strong dollar", "dollar strength", "risk appetite", "higher real yields"}


def _infer_rule_regions(text: str, institution_name: str) -> list[str]:
    source = institution_name.casefold()
    regions: list[str] = []
    if "federal reserve" in source:
        regions.append("US")
    if "ecb" in source or "european central bank" in source:
        regions.append("Europe")
    if "bank of england" in source:
        regions.append("UK")
    if "bank of japan" in source:
        regions.append("Japan")
    padded = f" {text} "
    region_terms = [
        ("Global", {"global", "world", "multi-asset", "asset allocation"}),
        ("US", {"united states", "u.s.", " us ", "federal reserve", "america"}),
        ("Europe", {"europe", "euro area", "eurozone", "ecb"}),
        ("UK", {"united kingdom", " u.k.", " uk ", "britain", "barclays", "invesco uk", "bank of england"}),
        ("Japan", {"japan", "boj", "yen"}),
        ("EM ex China", {"emerging markets", "em ex china", "em ex-china"}),
    ]
    for region, terms in region_terms:
        if any(term in padded for term in terms) and region not in regions:
            regions.append(region)
    if not regions:
        regions.append("Global")
    return regions[:6]


def _is_central_bank_source(institution_name: str) -> bool:
    lowered = institution_name.casefold()
    return any(term in lowered for term in ["federal reserve", "ecb", "bank of england", "bank of japan"])


def _has_any(text: str, terms: set[str]) -> bool:
    padded = f" {text} "
    return any(term in padded for term in terms)


def _rule_direction_score(text: str, positive_terms: set[str], negative_terms: set[str]) -> tuple[Direction, int]:
    positive = sum(text.count(term) for term in positive_terms)
    negative = sum(text.count(term) for term in negative_terms)
    if positive > negative:
        return Direction.BULLISH, 1
    if negative > positive:
        return Direction.BEARISH, -1
    return Direction.NEUTRAL, 0


def _strip_demo_markers(value: str) -> str:
    return (
        value.replace("DEMO/RULE:", "RULE:")
        .replace("DEMO:", "RULE:")
        .replace("DEMO FI", "RULE FI")
        .replace("DEMO Gold", "RULE Gold")
        .replace("DEMO material", "RULE material")
        .strip()
    )


class OpenAIProvider(LLMProvider):
    def __init__(
        self,
        settings: Settings,
        client: Any | None = None,
        max_retries: int = 0,
        session_key: str | None = None,
        run_id: str | None = None,
        record_usage: bool = True,
    ) -> None:
        if not settings.openai_api_key or not settings.openai_model:
            raise RuntimeError("Для LLM_PROVIDER=openai нужны OPENAI_API_KEY и OPENAI_MODEL.")
        self.settings = settings
        self.max_retries = max_retries
        self.session_key = session_key or PROCESS_SESSION_KEY
        self.run_id = run_id or ""
        self.record_usage = record_usage
        self.db_session: Any | None = None
        if client is not None:
            self.client = client
        else:
            import certifi
            import httpx
            from openai import OpenAI

            http_client = httpx.Client(
                timeout=settings.openai_timeout_seconds,
                verify=certifi.where(),
            )
            self.client = OpenAI(
                api_key=settings.openai_api_key,
                timeout=settings.openai_timeout_seconds,
                max_retries=max_retries,
                http_client=http_client,
            )

    def attach_session(self, session: Any) -> None:
        self.db_session = session

    def extract_research_views(self, article: Any, schema: Any | None = None) -> list[ResearchViewDraft]:
        prompt = (
            "Извлеки Research Views из аналитической публикации. Не выдумывай актив, регион или score "
            "без текстовой поддержки. evidence_quotes должны быть короткими цитатами из текста. "
            "Используй только строки MAE: asset_class, asset_group, asset_segment."
        )
        payload = {
            "title": article.title,
            "source": article.source.institution_name,
            "publication_date": str(article.publication_date),
            "url": article.source_reference,
            "text": article.content_text[: self.settings.max_article_chars],
        }
        response = self._structured_call(
            ResearchViewsResponse,
            prompt,
            payload,
            operation="extract_research_views",
            article_id=getattr(article, "id", None),
        )
        if not response.views:
            raise LLMProviderError("OpenAI вернул пустой список Research Views.")
        return response.views

    def generate_scenarios(self, context: dict[str, Any], schema: Any | None = None) -> list[ScenarioDraft]:
        prompt = (
            "Создай ровно три разные Scenario Cards: BASE, UPSIDE, DOWNSIDE. "
            "Каждая карточка обязана иметь предпосылки, triggers, expected_reaction и reversal_conditions. "
            "BASE должен быть семантически согласован с direction, position_score, drivers и evidence_quotes. "
            "Если evidence_quotes говорят hawkish/higher-for-longer/hikes, не превращай BASE в cuts/dovish без явной цитаты. "
            "Если evidence_quotes говорят cuts/dovish/disinflation, не превращай BASE в hawkish tightening без явной цитаты. "
            "Не используй заголовок или первую строку как единственное evidence. Не возвращай классификацию стадии сигнала."
        )
        response = self._structured_call(ScenarioResponse, prompt, context, operation="generate_scenarios")
        if {s.scenario_type for s in response.scenarios} != {ScenarioType.BASE, ScenarioType.UPSIDE, ScenarioType.DOWNSIDE}:
            raise LLMProviderError("OpenAI сценарии должны быть ровно BASE, UPSIDE и DOWNSIDE.")
        return response.scenarios

    def summarize_change(self, previous: Any, current: Any) -> ChangeExplanation:
        prompt = (
            "Кратко объясни смысловое изменение между previous и current Research View. "
            "Верни change_types только из DIRECTION_CHANGE, SCORE_CHANGE, CONFIDENCE_CHANGE, "
            "NEW_DRIVER, REMOVED_DRIVER, RISK_CHANGE, CATALYST_CHANGE, NO_MATERIAL_CHANGE."
        )
        payload = {
            "previous": _view_payload(previous),
            "current": _view_payload(current),
        }
        return self._structured_call(ChangeExplanation, prompt, payload, operation="summarize_change")

    def explain_signal(self, context: dict[str, Any]) -> str:
        prompt = "Сформулируй короткое объяснение Shift Signal на русском в одну строку."
        response = self._structured_call(SignalExplanationResponse, prompt, context, operation="explain_signal")
        return response.explanation

    def synthesize_market_themes(self, source_pack: list[dict[str, Any]], context: dict[str, Any]) -> MarketSynthesisResponse:
        prompt = (
            "STAGE 1 — MARKET SYNTHESIS. Use only the admitted source pack. "
            "Do not use historical ResearchViews, old matrix scores, homepages, or unrelated snippets. "
            "For every admitted document, extract a short market view, relevant regions, relevant asset classes, "
            "drivers and risks. Then synthesize 8-12 market themes for a 6-12 month investment horizon. "
            "Every theme must reference existing source_ids from the admitted pack. "
            "Use compact theme_id values T01, T02, ... T12. "
            "All user-facing prose must be in professional Russian: document short_extracted_view, theme title, "
            "implication, drivers, risks and invalidation. Preserve English enum values, source_ids, URLs, "
            "canonical region names and asset class labels exactly as supplied. "
            "Keep all text concise: document view <= 180 characters, theme title <= 100 characters, "
            "implication and invalidation <= 160 characters, max 3 drivers and max 3 risks per document. "
            "Do not claim human review or expert approval."
        )
        payload = {
            "snapshot_date": context.get("snapshot_date"),
            "coverage_requirements": context.get("coverage_requirements", []),
            "source_pack": source_pack,
        }
        return self._structured_call(
            MarketSynthesisResponse,
            prompt,
            payload,
            operation="synthesize_market_themes",
            max_output_tokens=max(self.settings.openai_max_output_tokens, 9000),
        )

    def synthesize_theme_matrix(self, context: dict[str, Any]) -> ThemeMatrixSynthesisResponse:
        prompt = (
            "STAGE 2 — FULL MAE MATRIX SYNTHESIS. Use only the structured market themes, admitted source IDs, "
            "canonical MAE template and scoring rules supplied in the payload. "
            "Generate exactly one cell for every applicable canonical_cell_id and no N/A cells. "
            "Scores must be integers from -3 to +3. Every cell must have concise thesis, confidence, "
            "supporting_theme_ids, supporting_source_ids, main risk and invalidation condition. "
            "Several cells may derive from the same theme. The previous matrix is comparison context only: "
            "it must not determine the new score or act as evidence. "
            "Also generate exactly BASE, UPSIDE and DOWNSIDE scenarios from the same theme set. "
            "All user-facing prose must be in professional Russian: cell thesis, main_risk, invalidation, "
            "scenario names, narratives, causal chains, triggers and vetoes. Preserve English enum values, "
            "canonical_cell_id, theme IDs and source IDs exactly as supplied. "
            "Keep the response compact: thesis <= 110 characters, main_risk <= 80 characters, "
            "invalidation <= 100 characters, use 1-2 themes and 1-3 sources per cell. "
            "Use short scenario text. Do not include N/A cells. "
            "Do not claim human review or expert approval."
        )
        return self._structured_call(
            ThemeMatrixSynthesisResponse,
            prompt,
            context,
            operation="synthesize_theme_matrix",
            max_output_tokens=max(self.settings.openai_max_output_tokens, 22000),
            timeout_seconds=max(self.settings.openai_timeout_seconds, 240),
        )

    def check_connection(self) -> OpenAIConnectionResult:
        if not self.settings.openai_api_key or not self.settings.openai_model:
            return OpenAIConnectionResult("not_configured", "OpenAI не настроен.")
        try:
            response = self._responses_create(
                operation="connection_check",
                instructions="Return a tiny JSON object that confirms connectivity.",
                input_payload='{"check":"openai_connection"}',
                text_config={
                    "format": {
                        "type": "json_schema",
                        "name": "OpenAIConnectionCheck",
                        "schema": {
                            "type": "object",
                            "properties": {"ok": {"type": "boolean"}},
                            "required": ["ok"],
                            "additionalProperties": False,
                        },
                        "strict": True,
                    }
                },
                max_output_tokens=200,
            )
            text = _response_text(response).strip()
            input_tokens, output_tokens = _usage_tokens(response)
            if not text:
                return OpenAIConnectionResult("success", "Подключение успешно.", input_tokens, output_tokens)
            parsed = json.loads(text)
            if parsed.get("ok") is True:
                return OpenAIConnectionResult("success", "Подключение успешно.", input_tokens, output_tokens)
            return OpenAIConnectionResult("unexpected_response", "OpenAI ответил, но формат проверки неожиданен.")
        except LLMUsageLimitError as exc:
            return OpenAIConnectionResult("limit_reached", str(exc))
        except Exception as exc:  # noqa: BLE001
            status = classify_openai_exception(exc)
            return OpenAIConnectionResult(status, openai_status_message(status))

    def usage_summary(self) -> LLMUsageSummary:
        daily_used, session_used = _llm_usage_counts(self.settings, self.session_key)
        return LLMUsageSummary(
            daily_limit=self.settings.max_llm_calls_per_day,
            daily_used=daily_used,
            session_limit=self.settings.max_llm_calls_per_session,
            session_used=session_used,
        )

    def _structured_call(
        self,
        model_cls: type[BaseModel],
        instruction: str,
        payload: dict[str, Any],
        *,
        operation: str,
        article_id: str | None = None,
        max_output_tokens: int | None = None,
        timeout_seconds: int | None = None,
    ) -> Any:
        errors: list[str] = []
        for attempt in range(self.max_retries + 1):
            try:
                response = self._responses_create(
                    operation=operation,
                    instructions=(
                        "Ты аналитический extraction engine для MAE Shift Signal Framework. "
                        "Возвращай только структурированный JSON по схеме. Не добавляй markdown.\n\n"
                        + instruction
                    ),
                    input_payload=json.dumps(payload, ensure_ascii=False),
                    text_config=_responses_json_schema_text_config(model_cls),
                    max_output_tokens=max_output_tokens or self.settings.openai_max_output_tokens,
                    timeout_seconds=timeout_seconds,
                    article_id=article_id,
                )
                content = _response_text(response)
                if not content:
                    raise LLMProviderError("OpenAI вернул пустой content.")
                return model_cls.model_validate_json(content)
            except LLMUsageLimitError:
                raise
            except (ValidationError, json.JSONDecodeError, LLMProviderError) as exc:
                errors.append(str(exc))
                if attempt >= self.max_retries:
                    break
                time.sleep(0.25 * (attempt + 1))
            except Exception as exc:  # noqa: BLE001 - provider must surface clear app errors
                code = classify_openai_exception(exc)
                safe_message = _safe_error_message(str(exc))
                logger.warning("OpenAI provider call failed operation=%s code=%s message=%s", operation, code, safe_message)
                errors.append(f"{code}: {safe_message}")
                if attempt >= self.max_retries:
                    break
                time.sleep(0.5 * (attempt + 1))
        raise LLMProviderError("OpenAI structured output failed: " + " | ".join(errors[-3:]))

    def _responses_create(
        self,
        *,
        operation: str,
        instructions: str,
        input_payload: str,
        text_config: dict[str, Any],
        max_output_tokens: int,
        timeout_seconds: int | None = None,
        article_id: str | None = None,
    ) -> Any:
        if self.record_usage:
            self._assert_limits(operation)
        try:
            response = self.client.responses.create(
                model=self.settings.openai_model,
                instructions=instructions,
                input=input_payload,
                text=text_config,
                max_output_tokens=max_output_tokens,
                store=False,
                timeout=timeout_seconds or self.settings.openai_timeout_seconds,
            )
            input_tokens, output_tokens = _usage_tokens(response)
            if self.record_usage:
                self._record_call(
                    operation=operation,
                    article_id=article_id,
                    status="SUCCESS",
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
            logger.info(
                "OpenAI call success operation=%s model=%s input_tokens=%s output_tokens=%s",
                operation,
                self.settings.openai_model,
                input_tokens,
                output_tokens,
            )
            return response
        except Exception as exc:  # noqa: BLE001
            code = classify_openai_exception(exc)
            message = _safe_error_message(str(exc))
            if self.record_usage:
                self._record_call(operation=operation, article_id=article_id, status="ERROR", error_code=code, message=message)
            raise

    def _assert_limits(self, operation: str) -> None:
        daily_used, session_used = (
            _llm_usage_counts_from_session(self.db_session, self.session_key)
            if self.db_session is not None
            else _llm_usage_counts(self.settings, self.session_key)
        )
        if daily_used >= self.settings.max_llm_calls_per_day:
            self._record_call(
                operation=operation,
                status="BLOCKED",
                error_code="DAILY_LIMIT",
                message="Daily LLM limit reached before API call.",
            )
            raise LLMUsageLimitError("Дневной лимит OpenAI-вызовов исчерпан.")
        if session_used >= self.settings.max_llm_calls_per_session:
            self._record_call(
                operation=operation,
                status="BLOCKED",
                error_code="SESSION_LIMIT",
                message="Session LLM limit reached before API call.",
            )
            raise LLMUsageLimitError("Лимит OpenAI-вызовов для текущей сессии исчерпан.")

    def _record_call(
        self,
        *,
        operation: str,
        status: str,
        article_id: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        error_code: str = "",
        message: str = "",
    ) -> None:
        if self.db_session is not None:
            _add_llm_call_to_session(
                self.db_session,
                model=self.settings.openai_model,
                operation=operation,
                run_id=self.run_id,
                session_key=self.session_key,
                article_id=article_id,
                status=status,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                error_code=error_code,
                message=message,
            )
            return
        _record_llm_call(
            self.settings,
            model=self.settings.openai_model,
            operation=operation,
            run_id=self.run_id,
            session_key=self.session_key,
            article_id=article_id,
            status=status,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            error_code=error_code,
            message=message,
        )


class LLMProviderError(RuntimeError):
    pass


class LLMUsageLimitError(LLMProviderError):
    pass


def _json_schema_response_format(model_cls: type[BaseModel]) -> dict[str, Any]:
    schema = model_cls.model_json_schema()
    _strictify_schema(schema)
    return {
        "type": "json_schema",
        "json_schema": {
            "name": model_cls.__name__,
            "schema": schema,
            "strict": True,
        },
    }


def _responses_json_schema_text_config(model_cls: type[BaseModel]) -> dict[str, Any]:
    response_format = _json_schema_response_format(model_cls)["json_schema"]
    return {
        "format": {
            "type": "json_schema",
            "name": response_format["name"],
            "schema": response_format["schema"],
            "strict": True,
        }
    }


def _strictify_schema(node: Any) -> None:
    if isinstance(node, dict):
        node.pop("default", None)
        if node.get("type") == "object":
            node["additionalProperties"] = False
            properties = node.get("properties")
            if isinstance(properties, dict):
                node["required"] = list(properties.keys())
        for value in node.values():
            _strictify_schema(value)
    elif isinstance(node, list):
        for item in node:
            _strictify_schema(item)


def _view_payload(view: Any) -> dict[str, Any]:
    return {
        "institution": view.institution,
        "horizon": view.horizon,
        "region": view.region,
        "asset_class": view.asset_class,
        "asset_group": view.asset_group,
        "asset_segment": view.asset_segment,
        "direction": view.direction,
        "position_score": view.position_score,
        "confidence": view.confidence,
        "drivers": view.drivers,
        "risks": view.risks,
        "catalysts": view.catalysts,
        "evidence_quotes": view.evidence_quotes,
    }


def _response_text(response: Any) -> str:
    output_text = getattr(response, "output_text", None)
    if output_text:
        return str(output_text)
    output = getattr(response, "output", None) or []
    parts: list[str] = []
    for item in output:
        for content in getattr(item, "content", []) or []:
            text = getattr(content, "text", None)
            if text:
                parts.append(str(text))
    return "".join(parts)


def _usage_tokens(response: Any) -> tuple[int, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0
    return int(getattr(usage, "input_tokens", 0) or 0), int(getattr(usage, "output_tokens", 0) or 0)


def classify_openai_exception(exc: Exception) -> str:
    message = str(exc).lower()
    try:
        from openai import APIConnectionError, AuthenticationError, BadRequestError, NotFoundError, PermissionDeniedError, RateLimitError
    except Exception:  # pragma: no cover
        APIConnectionError = AuthenticationError = BadRequestError = NotFoundError = PermissionDeniedError = RateLimitError = ()  # type: ignore
    if isinstance(exc, AuthenticationError) or "invalid api key" in message or "incorrect api key" in message:
        return "invalid_key"
    if isinstance(exc, PermissionDeniedError):
        return "permission_denied"
    if isinstance(exc, NotFoundError) or ("model" in message and ("not found" in message or "does not exist" in message)):
        return "model_unavailable"
    if isinstance(exc, RateLimitError):
        if any(token in message for token in ["insufficient_quota", "quota", "billing", "credit", "funds"]):
            return "insufficient_funds"
        return "rate_limited"
    if isinstance(exc, BadRequestError) and "model" in message:
        return "model_unavailable"
    if isinstance(exc, APIConnectionError) and any(token in message for token in ["ssl", "certificate", "cert"]):
        return "ssl_error"
    if any(token in message for token in ["ssl", "certificate verify", "certifi"]):
        return "ssl_error"
    return "api_error"


def openai_status_message(status: str) -> str:
    return {
        "success": "Подключение успешно.",
        "not_configured": "OpenAI не настроен.",
        "ssl_error": "Проблема SSL-сертификатов. Используется certifi; проверьте системные сертификаты и сеть.",
        "insufficient_funds": "Недостаточно средств или исчерпана квота OpenAI.",
        "model_unavailable": "Модель недоступна для этого ключа или проекта.",
        "invalid_key": "Неверный OpenAI API key.",
        "permission_denied": "Недостаточно прав для вызова модели.",
        "rate_limited": "OpenAI временно ограничил частоту запросов.",
        "limit_reached": "Локальный лимит OpenAI-вызовов исчерпан.",
        "unexpected_response": "OpenAI ответил в неожиданном формате.",
    }.get(status, "Ошибка подключения OpenAI.")


def _safe_error_message(message: str) -> str:
    sanitized = re.sub(r"sk-[A-Za-z0-9_\\-]{8,}", "sk-***", message)
    sanitized = re.sub(r"OPENAI_API_KEY\\s*=\\s*\\S+", "OPENAI_API_KEY=***", sanitized)
    return sanitized[:500]


def _llm_usage_counts(settings: Settings, session_key: str) -> tuple[int, int]:
    from sqlalchemy import func, select

    from app.domain.models import LLMCallLog
    from app.repositories.database import ENGINE, SessionLocal

    today = date.today()
    LLMCallLog.__table__.create(ENGINE, checkfirst=True)
    with SessionLocal() as session:
        daily = session.scalar(
            select(func.count(LLMCallLog.id)).where(
                func.date(LLMCallLog.created_at) == today.isoformat(),
                LLMCallLog.status != "BLOCKED",
            )
        )
        session_count = session.scalar(
            select(func.count(LLMCallLog.id)).where(
                func.date(LLMCallLog.created_at) == today.isoformat(),
                LLMCallLog.session_key == session_key,
                LLMCallLog.status != "BLOCKED",
            )
        )
    return int(daily or 0), int(session_count or 0)


def _llm_usage_counts_from_session(session: Any, session_key: str) -> tuple[int, int]:
    from sqlalchemy import func, select

    from app.domain.models import LLMCallLog

    today = date.today()
    daily = session.scalar(
        select(func.count(LLMCallLog.id)).where(
            func.date(LLMCallLog.created_at) == today.isoformat(),
            LLMCallLog.status != "BLOCKED",
        )
    )
    session_count = session.scalar(
        select(func.count(LLMCallLog.id)).where(
            func.date(LLMCallLog.created_at) == today.isoformat(),
            LLMCallLog.session_key == session_key,
            LLMCallLog.status != "BLOCKED",
        )
    )
    return int(daily or 0), int(session_count or 0)


def _add_llm_call_to_session(
    session: Any,
    *,
    model: str,
    operation: str,
    run_id: str,
    session_key: str,
    status: str,
    article_id: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    error_code: str = "",
    message: str = "",
) -> None:
    from app.domain.models import LLMCallLog

    session.add(
        LLMCallLog(
            provider="openai",
            model=model,
            operation=operation,
            run_id=run_id,
            session_key=session_key,
            article_id=article_id,
            status=status,
            input_tokens=max(0, int(input_tokens or 0)),
            output_tokens=max(0, int(output_tokens or 0)),
            error_code=error_code,
            message=_safe_error_message(message),
        )
    )
    session.flush()


def _record_llm_call(
    settings: Settings,
    *,
    model: str,
    operation: str,
    run_id: str,
    session_key: str,
    status: str,
    article_id: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    error_code: str = "",
    message: str = "",
) -> None:
    try:
        from app.domain.models import LLMCallLog
        from app.repositories.database import ENGINE, SessionLocal

        LLMCallLog.__table__.create(ENGINE, checkfirst=True)
        with SessionLocal() as session:
            _add_llm_call_to_session(
                session,
                model=model,
                operation=operation,
                run_id=run_id,
                session_key=session_key,
                article_id=article_id,
                status=status,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                error_code=error_code,
                message=message,
            )
            session.commit()
    except Exception:  # noqa: BLE001
        logger.warning("Failed to persist OpenAI call metadata operation=%s status=%s", operation, status)


def llm_usage_summary(settings: Settings | None = None, session_key: str | None = None) -> LLMUsageSummary:
    settings = settings or get_settings()
    session_key = session_key or PROCESS_SESSION_KEY
    daily_used, session_used = _llm_usage_counts(settings, session_key)
    return LLMUsageSummary(
        daily_limit=settings.max_llm_calls_per_day,
        daily_used=daily_used,
        session_limit=settings.max_llm_calls_per_session,
        session_used=session_used,
    )


def llm_calls_for_run(run_id: str) -> int:
    if not run_id:
        return 0
    from sqlalchemy import func, select

    from app.domain.models import LLMCallLog
    from app.repositories.database import ENGINE, SessionLocal

    LLMCallLog.__table__.create(ENGINE, checkfirst=True)
    with SessionLocal() as session:
        count = session.scalar(
            select(func.count(LLMCallLog.id)).where(
                LLMCallLog.run_id == run_id,
                LLMCallLog.status != "BLOCKED",
            )
        )
    return int(count or 0)


def check_openai_connection(settings: Settings | None = None, session_key: str | None = None) -> OpenAIConnectionResult:
    settings = settings or get_settings()
    if settings.llm_provider.strip().lower() != "openai" or not settings.openai_api_key or not settings.openai_model:
        return OpenAIConnectionResult("not_configured", openai_status_message("not_configured"))
    return OpenAIProvider(settings, max_retries=0, session_key=session_key).check_connection()


def get_provider(settings: Settings | None = None, session_key: str | None = None, run_id: str | None = None) -> LLMProvider:
    settings = settings or get_settings()
    provider_name = settings.llm_provider.strip().lower()
    if provider_name == "openai":
        if settings.openai_api_key and settings.openai_model:
            return OpenAIProvider(settings, session_key=session_key, run_id=run_id)
        raise LLMProviderError("OpenAI API не настроен: задайте OPENAI_API_KEY и OPENAI_MODEL.")
    if provider_name in {"rule", "rules", "rule_based", "rule-based"}:
        logger.info("LLM_PROVIDER=%s: используется ограниченный правиловой extractor.", settings.llm_provider)
        return RuleBasedProvider()
    if provider_name == "mock" and settings.app_env == "test":
        return MockLLMProvider()
    if provider_name == "mock":
        raise LLMProviderError("Mock provider доступен только для pytest и изолированных технических проверок.")
    raise LLMProviderError(f"Неизвестный LLM_PROVIDER: {settings.llm_provider}. Поддерживается OpenAI.")
