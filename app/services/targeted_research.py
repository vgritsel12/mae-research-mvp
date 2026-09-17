from __future__ import annotations

import csv
import json
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import ROOT_DIR
from app.domain.enums import Confidence, Direction, ExtractionMethod, FetchStatus
from app.domain.models import Article, MaeComponentCell, MaeComponentSnapshot, ResearchView, utcnow
from app.services.collectors import create_article, get_or_create_manual_source
from app.services.component_engine import (
    ASSET_OUTLOOK,
    DIRECT_RESEARCH,
    MIDYEAR_OUTLOOK,
    MONTHLY_OUTLOOK,
    QUARTERLY_OUTLOOK,
    SEMI_DIRECT_RESEARCH,
    WEEKLY_COMMENTARY,
    core_component_cells,
)
from app.services.core_validation import REJECTED_RESEARCH_COLUMNS, score_type_for_cell
from app.services.methodology import STRICT_VALIDATED
from app.services.normalization import canonical_cell_for, canonicalize_url


TARGETED_SCHEMA_VERSION = "targeted_research_v1"
TARGETED_SNAPSHOT_DATE = date(2026, 7, 12)
LOOKBACK_START = date(2026, 4, 13)

RESEARCH_ITEMS_COLUMNS = [
    "cell_id",
    "provider",
    "provider_family",
    "title",
    "author",
    "publication_date",
    "canonical_url",
    "horizon",
    "geography",
    "asset_class",
    "segment",
    "stance",
    "excerpt",
    "drivers",
    "risks",
    "classification",
    "accepted_for_score",
    "score_contribution",
]

SEARCH_LOG_COLUMNS = [
    "snapshot_date",
    "cell_id",
    "provider",
    "query_or_search_path",
    "search_status",
    "documents_found",
    "relevant_documents_found",
    "accepted_documents",
    "failure_reason",
    "checked_at",
]

CHANGE_TRACKER_COLUMNS = [
    "current_snapshot_date",
    "previous_snapshot_date",
    "cell_id",
    "previous_score",
    "current_score",
    "previous_score_type",
    "current_score_type",
    "change_type",
    "research_change",
    "data_change",
    "market_change",
    "primary_reason",
    "carried_research_items",
    "expired_research_items",
]


@dataclass(frozen=True)
class TargetedResearchItem:
    cell_id: str
    row_key: str
    region: str
    provider: str
    provider_family: str
    source_website: str
    title: str
    author: str
    publication_date: date
    canonical_url: str
    horizon: str
    geography: str
    asset_class: str
    asset_group: str
    segment: str
    direction: Direction
    position_score: int
    confidence: Confidence
    excerpt: str
    drivers: tuple[str, ...]
    risks: tuple[str, ...]
    classification: str
    document_type: str = ASSET_OUTLOOK


@dataclass(frozen=True)
class TargetedRejectedDocument:
    cell_id: str
    provider: str
    title: str
    canonical_url: str
    attempted_cell: str
    classification: str
    rejection_reason: str


@dataclass(frozen=True)
class TargetedCollectionResult:
    seeded_views: int
    accepted_rows: int
    rejected_rows: int
    search_log_rows: int
    exact_duplicates_removed: int
    summary_counts: dict[str, Any]


CELL_LABELS = {
    "GA:R03:US": "US Wide Market",
    "GA:R04:US": "US Market Breadth",
    "GA:R07:US": "US Growth",
    "GA:R08:US": "US Small Cap",
    "GA:R09:US": "US Government Long Term",
    "GA:R10:US": "US Government Short Term",
    "GA:R14:US": "US Corporate High Yield Mid Term",
    "GA:R17:US": "US Corporate Investment Grade Mid Term",
    "GA:R18:US": "US Inflation Linked",
    "GA:R20:GLOBAL": "Global Gold",
}

SEARCH_PLAN: dict[str, dict[str, Any]] = {
    "GA:R03:US": {
        "terms": [
            "US equity outlook",
            "US equities versus global equities",
            "US stock market outlook",
            "US equity allocation",
            "overweight US equities",
            "underweight US equities",
            "US earnings outlook",
            "regional equity allocation",
        ],
        "providers": [
            "BlackRock Investment Institute",
            "J.P. Morgan",
            "Goldman Sachs",
            "Morgan Stanley",
            "Vanguard",
            "State Street Global Advisors",
            "Invesco",
            "Amundi",
            "HSBC Global Banking and Markets",
            "Barclays",
        ],
    },
    "GA:R04:US": {
        "terms": [
            "US market breadth outlook",
            "equity market concentration",
            "equal weight outlook",
            "broadening equity market",
            "market participation",
            "Magnificent Seven concentration",
            "earnings breadth",
            "equal weight versus cap weight",
        ],
        "providers": [
            "BlackRock Investment Institute",
            "J.P. Morgan",
            "Goldman Sachs",
            "Morgan Stanley",
            "State Street Global Advisors",
            "Invesco",
            "Vanguard",
            "Amundi",
        ],
    },
    "GA:R07:US": {
        "terms": [
            "US growth equities outlook",
            "growth versus value outlook",
            "US technology equity outlook",
            "growth stocks allocation",
            "technology earnings outlook",
            "AI equity valuation outlook",
        ],
        "providers": [
            "BlackRock Investment Institute",
            "J.P. Morgan",
            "Goldman Sachs",
            "Morgan Stanley",
            "State Street Global Advisors",
            "Invesco",
            "Vanguard",
            "Amundi",
        ],
    },
    "GA:R08:US": {
        "terms": [
            "US small cap outlook",
            "small cap versus large cap",
            "Russell 2000 outlook",
            "small cap earnings outlook",
            "small cap allocation",
        ],
        "providers": [
            "BlackRock Investment Institute",
            "J.P. Morgan",
            "Goldman Sachs",
            "Morgan Stanley",
            "State Street Global Advisors",
            "Invesco",
            "Vanguard",
            "Amundi",
        ],
    },
    "GA:R09:US": {
        "terms": [
            "long duration Treasury outlook",
            "long US Treasuries outlook",
            "10 year Treasury duration allocation",
            "long term government bonds outlook",
        ],
        "providers": [
            "BlackRock Investment Institute",
            "J.P. Morgan",
            "Goldman Sachs",
            "Morgan Stanley",
            "Vanguard",
            "State Street Global Advisors",
            "Invesco",
            "Amundi",
        ],
    },
    "GA:R10:US": {
        "terms": [
            "short Treasury outlook",
            "front end Treasury outlook",
            "short duration bond outlook",
            "cash versus short bonds",
            "Fed path short rates investment outlook",
        ],
        "providers": [
            "BlackRock Investment Institute",
            "J.P. Morgan",
            "Goldman Sachs",
            "Morgan Stanley",
            "Vanguard",
            "State Street Global Advisors",
            "Invesco",
            "Amundi",
        ],
    },
    "GA:R14:US": {
        "terms": [
            "US high yield outlook",
            "high yield spreads outlook",
            "high yield default outlook",
            "high yield refinancing outlook",
            "HY allocation",
        ],
        "providers": [
            "BlackRock Investment Institute",
            "J.P. Morgan",
            "Goldman Sachs",
            "Morgan Stanley",
            "Vanguard",
            "State Street Global Advisors",
            "Invesco",
            "Amundi",
            "S&P Global Ratings",
            "Moody's",
            "Fitch Ratings",
        ],
    },
    "GA:R17:US": {
        "terms": [
            "US investment grade credit outlook",
            "IG credit spreads outlook",
            "investment grade carry outlook",
            "high quality credit allocation",
        ],
        "providers": [
            "BlackRock Investment Institute",
            "J.P. Morgan",
            "Goldman Sachs",
            "Morgan Stanley",
            "Vanguard",
            "State Street Global Advisors",
            "Invesco",
            "Amundi",
            "S&P Global Ratings",
            "Moody's",
            "Fitch Ratings",
        ],
    },
    "GA:R18:US": {
        "terms": [
            "TIPS outlook",
            "US inflation linked bonds outlook",
            "Treasury inflation protected securities outlook",
            "breakeven inflation outlook",
            "real yield outlook",
        ],
        "providers": [
            "BlackRock Investment Institute",
            "J.P. Morgan",
            "Vanguard",
            "State Street Global Advisors",
            "Invesco",
            "Amundi",
        ],
    },
    "GA:R20:GLOBAL": {
        "terms": [
            "gold outlook 2026",
            "gold price outlook",
            "gold allocation outlook",
            "gold as diversifier",
            "central bank gold demand outlook",
        ],
        "providers": [
            "BlackRock Investment Institute",
            "J.P. Morgan",
            "Goldman Sachs",
            "Morgan Stanley",
            "State Street Global Advisors",
            "Invesco",
            "Amundi",
        ],
    },
}


def targeted_research_items() -> list[TargetedResearchItem]:
    items = [
        _item(
            "GA:R03:US",
            "EQUITY|Wide Market|Wide Market",
            "US",
            "BlackRock Investment Institute",
            "blackrock",
            "https://www.blackrock.com/corporate/insights/blackrock-investment-institute",
            "Weekly market commentary: Reconciling AI earnings and valuations",
            "Jean Boivin; Wei Li; Vivek Paul; Beata Harasim",
            date(2026, 7, 6),
            "https://www.blackrock.com/corporate/insights/blackrock-investment-institute/publications/weekly-commentary",
            "US",
            "EQUITY",
            "Wide Market",
            "Wide Market",
            Direction.BULLISH,
            1,
            "United States tactical Overweight +1. We are overweight. Strong corporate earnings, fueled by the AI buildout and a favorable macro backdrop, are outpacing higher interest rate expectations.",
            ("strong corporate earnings", "AI buildout", "favorable macro backdrop"),
            ("higher interest rate expectations", "AI earnings durability risk"),
            DIRECT_RESEARCH,
        ),
        _item(
            "GA:R03:US",
            "EQUITY|Wide Market|Wide Market",
            "US",
            "Goldman Sachs",
            "goldman-sachs",
            "https://www.goldmansachs.com/insights",
            "The S&P 500 Is Forecast to Climb as Earnings Growth Powers Stocks Higher",
            "Goldman Sachs Research; Ben Snider",
            date(2026, 5, 28),
            "https://www.goldmansachs.com/insights/articles/s-and-p-500-forecast-to-climb-as-earnings-growth-powers-stocks-higher",
            "US",
            "EQUITY",
            "Wide Market",
            "Wide Market",
            Direction.BULLISH,
            1,
            "The S&P 500 is forecast to rise to 8000 by the end of this year, up from an earlier projection of 7600, reflecting upgraded earnings estimates, according to Ben Snider, chief US equity strategist in Goldman Sachs Research.",
            ("upgraded earnings estimates", "AI infrastructure capex beneficiaries", "modest Treasury-yield support"),
            ("narrow market breadth", "geopolitical uncertainty", "input-cost pressure"),
            DIRECT_RESEARCH,
        ),
        _item(
            "GA:R03:US",
            "EQUITY|Wide Market|Wide Market",
            "US",
            "Amundi",
            "amundi",
            "https://research-center.amundi.com",
            "Global Investment Views - July 2026",
            "Amundi Investment Institute",
            date(2026, 7, 1),
            "https://research-center.amundi.com/article/global-investment-views-july-2026",
            "US",
            "EQUITY",
            "Wide Market",
            "Wide Market",
            Direction.BEARISH,
            -1,
            "We remain mildly positive on equities, supported by strong earnings, but have reduced concentration risk by lowering our exposure to US equities and diversifying into Europe and the equally-weighted S&P 500.",
            ("strong earnings keep equities mildly positive", "diversification into Europe and equal-weighted S&P 500"),
            ("US concentration risk", "persistent uncertainty"),
            DIRECT_RESEARCH,
        ),
        _item(
            "GA:R04:US",
            "EQUITY|Other categories|Market Breadth (Equal Weight)",
            "US",
            "State Street Global Advisors",
            "state-street",
            "https://www.ssga.com",
            "Position beyond US large-cap tech for the next wave of AI and economic realignment",
            "State Street Investment Management",
            date(2026, 5, 29),
            "https://www.ssga.com/us/en/intermediary/insights/etf-market-outlook/position-beyond-us-large-cap-tech-for-the-next-wave-of-ai-and-economic-realignment",
            "US",
            "EQUITY",
            "Other categories",
            "Market Breadth (Equal Weight)",
            Direction.BULLISH,
            1,
            "The next phase of the bull market may reward breadth over concentration. And that has clear implications for portfolio construction.",
            ("leadership broadening", "capital investment and economic realignment", "reduced concentration risk"),
            ("large-cap technology concentration", "policy and growth sensitivity"),
            DIRECT_RESEARCH,
        ),
        _item(
            "GA:R04:US",
            "EQUITY|Other categories|Market Breadth (Equal Weight)",
            "US",
            "Amundi",
            "amundi",
            "https://research-center.amundi.com",
            "Global Investment Views - July 2026",
            "Amundi Investment Institute",
            date(2026, 7, 1),
            "https://research-center.amundi.com/article/global-investment-views-july-2026",
            "US",
            "EQUITY",
            "Other categories",
            "Market Breadth (Equal Weight)",
            Direction.BULLISH,
            1,
            "We remain mildly positive on equities, supported by strong earnings, but have reduced concentration risk by lowering our exposure to US equities and diversifying into Europe and the equally-weighted S&P 500.",
            ("explicit diversification into equal-weighted S&P 500", "reduced US concentration risk"),
            ("persistent uncertainty", "US mega-cap concentration risk"),
            DIRECT_RESEARCH,
        ),
        _item(
            "GA:R07:US",
            "EQUITY|Other categories|Growth",
            "US",
            "BlackRock Investment Institute",
            "blackrock",
            "https://www.blackrock.com/corporate/insights/blackrock-investment-institute",
            "Weekly market commentary: Reconciling AI earnings and valuations",
            "Jean Boivin; Wei Li; Vivek Paul; Beata Harasim",
            date(2026, 7, 6),
            "https://www.blackrock.com/corporate/insights/blackrock-investment-institute/publications/weekly-commentary",
            "US",
            "EQUITY",
            "Other categories",
            "Growth",
            Direction.BULLISH,
            1,
            "Incremental margins remain above operating margins across most AI value-chain baskets, suggesting AI-related revenues are still translating into unusually strong profits.",
            ("AI-related revenues", "unusually strong profits", "US leadership in chips and AI models"),
            ("AI earnings durability", "elevated valuations"),
            DIRECT_RESEARCH,
        ),
        _item(
            "GA:R07:US",
            "EQUITY|Other categories|Growth",
            "US",
            "State Street Global Advisors",
            "state-street",
            "https://www.ssga.com",
            "Position beyond US large-cap tech for the next wave of AI and economic realignment",
            "State Street Investment Management",
            date(2026, 5, 29),
            "https://www.ssga.com/us/en/intermediary/insights/etf-market-outlook/position-beyond-us-large-cap-tech-for-the-next-wave-of-ai-and-economic-realignment",
            "US",
            "EQUITY",
            "Other categories",
            "Growth",
            Direction.BULLISH,
            1,
            "For investors, participating fully in AI-driven growth may require looking beyond traditional technology sectors and mega-cap names to the broader ecosystem supporting innovation.",
            ("AI infrastructure and deployment", "semiconductor and automation demand", "technology earnings support"),
            ("concentration risk in large-cap technology", "policy-sensitive capex cycle"),
            DIRECT_RESEARCH,
        ),
        _item(
            "GA:R08:US",
            "EQUITY|Other categories|Small Cap",
            "US",
            "Goldman Sachs",
            "goldman-sachs",
            "https://www.goldmansachs.com/insights",
            "Big Opportunities in Small Cap Equities",
            "Greg Tuorto; Goldman Sachs Asset Management",
            date(2026, 4, 24),
            "https://www.goldmansachs.com/insights/the-markets/big-opportunities-in-small-cap-equities",
            "US",
            "EQUITY",
            "Other categories",
            "Small Cap",
            Direction.BULLISH,
            1,
            "After years of underperformance, small-cap equities may be poised for a sustained rally.",
            ("small-cap underperformance reversal", "US Small and SMID Cap opportunity set"),
            ("macro volatility", "sustainability of small-cap momentum"),
            DIRECT_RESEARCH,
        ),
        _item(
            "GA:R08:US",
            "EQUITY|Other categories|Small Cap",
            "US",
            "State Street Global Advisors",
            "state-street",
            "https://www.ssga.com",
            "Position beyond US large-cap tech for the next wave of AI and economic realignment",
            "State Street Investment Management",
            date(2026, 5, 29),
            "https://www.ssga.com/us/en/intermediary/insights/etf-market-outlook/position-beyond-us-large-cap-tech-for-the-next-wave-of-ai-and-economic-realignment",
            "US",
            "EQUITY",
            "Other categories",
            "Small Cap",
            Direction.BULLISH,
            1,
            "For investors, US small caps may offer an opportunity to participate in a more diversified and domestically driven economic expansion.",
            ("domestic economic activity", "infrastructure and reshoring", "small-cap valuation discount"),
            ("trade tensions", "financing sensitivity", "policy uncertainty"),
            DIRECT_RESEARCH,
        ),
        _item(
            "GA:R09:US",
            "FIXED INCOME|GOV|Long Term",
            "US",
            "BlackRock Investment Institute",
            "blackrock",
            "https://www.blackrock.com/corporate/insights/blackrock-investment-institute",
            "Weekly market commentary: Reconciling AI earnings and valuations",
            "Jean Boivin; Wei Li; Vivek Paul; Beata Harasim",
            date(2026, 7, 6),
            "https://www.blackrock.com/corporate/insights/blackrock-investment-institute/publications/weekly-commentary",
            "US",
            "FIXED INCOME",
            "GOV",
            "Long Term",
            Direction.BEARISH,
            -1,
            "Long U.S. Treasuries tactical Neutral. We are underweight. We see investors wanting more compensation for holding long-term bonds amid persistent inflation and high debt loads.",
            ("term premium compensation required", "persistent inflation", "high debt loads"),
            ("growth slowdown could support duration", "flight-to-quality demand"),
            DIRECT_RESEARCH,
        ),
        _item(
            "GA:R10:US",
            "FIXED INCOME|GOV|Short Term",
            "US",
            "BlackRock Investment Institute",
            "blackrock",
            "https://www.blackrock.com/corporate/insights/blackrock-investment-institute",
            "Weekly market commentary: Reconciling AI earnings and valuations",
            "Jean Boivin; Wei Li; Vivek Paul; Beata Harasim",
            date(2026, 7, 6),
            "https://www.blackrock.com/corporate/insights/blackrock-investment-institute/publications/weekly-commentary",
            "US",
            "FIXED INCOME",
            "GOV",
            "Short Term",
            Direction.NEUTRAL,
            0,
            "Short U.S. Treasuries tactical Neutral. We are neutral. We prefer short- and medium-term Treasuries, given the attractive risk-adjusted income on offer.",
            ("attractive risk-adjusted income", "short and medium Treasury preference"),
            ("policy-rate uncertainty", "inflation volatility"),
            DIRECT_RESEARCH,
        ),
        _item(
            "GA:R14:US",
            "FIXED INCOME|CORP HY|Mid Term",
            "US",
            "BlackRock Investment Institute",
            "blackrock",
            "https://www.blackrock.com/corporate/insights/blackrock-investment-institute",
            "Weekly market commentary: Reconciling AI earnings and valuations",
            "Jean Boivin; Wei Li; Vivek Paul; Beata Harasim",
            date(2026, 7, 6),
            "https://www.blackrock.com/corporate/insights/blackrock-investment-institute/publications/weekly-commentary",
            "US",
            "FIXED INCOME",
            "CORP HY",
            "Mid Term",
            Direction.NEUTRAL,
            0,
            "Global high yield tactical Neutral. We are neutral. High yield offers attractive income. We prefer higher-rated U.S. and European high yield over investment grade and see dispersion of returns increasing.",
            ("attractive high-yield income", "preference for higher-rated US high yield"),
            ("tight spreads", "dispersion of returns increasing"),
            DIRECT_RESEARCH,
        ),
        _item(
            "GA:R14:US",
            "FIXED INCOME|CORP HY|Mid Term",
            "US",
            "Vanguard",
            "vanguard",
            "https://corporate.vanguard.com",
            "Active Fixed Income Perspectives Monthly Pulse: June 2026",
            "Vanguard Fixed Income Group",
            date(2026, 6, 26),
            "https://advisors.vanguard.com/insights/article/series/active-fixed-income-perspectives",
            "US",
            "FIXED INCOME",
            "CORP HY",
            "Mid Term",
            Direction.NEUTRAL,
            0,
            "In high yield, we are emphasizing lower beta exposure while generating value through selection in an environment of greater dispersion.",
            ("lower beta high yield exposure", "bottom-up selection", "dispersion creates selection value"),
            ("tight spreads", "new supply from AI-driven capital spending"),
            DIRECT_RESEARCH,
        ),
        _item(
            "GA:R17:US",
            "FIXED INCOME|CORP IG|Mid Term",
            "US",
            "Vanguard",
            "vanguard",
            "https://corporate.vanguard.com",
            "Active Fixed Income Perspectives Monthly Pulse: June 2026",
            "Vanguard Fixed Income Group",
            date(2026, 6, 26),
            "https://advisors.vanguard.com/insights/article/series/active-fixed-income-perspectives",
            "US",
            "FIXED INCOME",
            "CORP IG",
            "Mid Term",
            Direction.BULLISH,
            1,
            "Credit: We remain overweight credit, with an up-in-quality bias and a focus on generating alpha through bottom-up security selection.",
            ("overweight credit", "up-in-quality bias", "banks and utilities favored"),
            ("rangebound tight IG spreads", "restrictive Fed policy", "robust new issue pipeline"),
            DIRECT_RESEARCH,
        ),
        _item(
            "GA:R17:US",
            "FIXED INCOME|CORP IG|Mid Term",
            "US",
            "Amundi",
            "amundi",
            "https://research-center.amundi.com",
            "Global Investment Views - July 2026",
            "Amundi Investment Institute",
            date(2026, 7, 1),
            "https://research-center.amundi.com/article/global-investment-views-july-2026",
            "US",
            "FIXED INCOME",
            "CORP IG",
            "Mid Term",
            Direction.BULLISH,
            1,
            "We see some value in the 5-year tenor of the curve and are slightly positive on US IG which is more resilient to inflation shocks.",
            ("slightly positive on US IG", "inflation-shock resilience", "5-year curve value"),
            ("inflation shocks", "spread volatility"),
            DIRECT_RESEARCH,
        ),
        _item(
            "GA:R18:US",
            "FIXED INCOME|Other categories|Inflation Linked",
            "Global",
            "BlackRock Investment Institute",
            "blackrock",
            "https://www.blackrock.com/corporate/insights/blackrock-investment-institute",
            "Weekly market commentary: Reconciling AI earnings and valuations",
            "Jean Boivin; Wei Li; Vivek Paul; Beata Harasim",
            date(2026, 7, 6),
            "https://www.blackrock.com/corporate/insights/blackrock-investment-institute/publications/weekly-commentary",
            "Global",
            "FIXED INCOME",
            "Other categories",
            "Inflation Linked",
            Direction.NEUTRAL,
            0,
            "Global inflation-linked bonds tactical Neutral. We are neutral. We see inflation settling above pre-pandemic levels, but markets may not price this in the near term as economic growth could slow.",
            ("inflation above pre-pandemic levels", "market pricing may lag inflation"),
            ("growth slowdown", "near-term repricing uncertainty"),
            SEMI_DIRECT_RESEARCH,
        ),
        _item(
            "GA:R20:GLOBAL",
            "COMMODITIES|Commodities|Gold",
            "Global",
            "State Street Global Advisors",
            "state-street",
            "https://www.ssga.com",
            "Gold 2026 Midyear Outlook: A tug-of-war between tactical and structural momentum",
            "Aakash Doshi; Mohamad Abukhalaf; Diego Andrade; Robin Tsui",
            date(2026, 6, 10),
            "https://www.ssga.com/us/en/intermediary/insights/gold-2026-midyear-outlook-a-tug-of-war-between-tactical-and-structural-momentum",
            "Global",
            "COMMODITIES",
            "Commodities",
            "Gold",
            Direction.BULLISH,
            1,
            "Structural support for gold remains intact, with prices potentially reaching $5,500/oz by year-end.",
            ("structural support", "central-bank demand", "geopolitical and inflation hedge demand"),
            ("tactical consolidation", "hawkish Fed and G10 policy"),
            DIRECT_RESEARCH,
        ),
        _item(
            "GA:R20:GLOBAL",
            "COMMODITIES|Commodities|Gold",
            "Global",
            "Invesco",
            "invesco",
            "https://www.invesco.com",
            "Quarterly Gold Insights: A review of Q2 and outlook for gold",
            "Sam Whitehead; Benjamin Jones; David Scales",
            date(2026, 7, 8),
            "https://www.invesco.com/nl/en/insights/quarterly-gold-insights.html",
            "Global",
            "COMMODITIES",
            "Commodities",
            "Gold",
            Direction.NEUTRAL,
            0,
            "Structural support remains in place, including central banks buying gold, but the market could remain volatile in the near term.",
            ("central banks buying gold", "inflation protection", "portfolio diversification"),
            ("higher rates", "stronger US dollar", "near-term volatility"),
            DIRECT_RESEARCH,
        ),
    ]
    return dedupe_targeted_items(_with_document_types(items))[0]


def targeted_rejected_documents() -> list[TargetedRejectedDocument]:
    rows = [
        _rejected("GA:R04:US", "Goldman Sachs", "2026 Outlooks", "https://www.goldmansachs.com/insights/outlooks/2026-outlooks", "CONTEXT_ONLY", "landing_or_outlook_hub_not_original_cell_report"),
        _rejected("GA:R04:US", "Goldman Sachs", "Should Stock Investors Look Beyond the Tech Giants?", "https://www.goldmansachs.com/insights/articles/should-stock-investors-look-beyond-the-tech-giants", "REJECTED", "older_than_90_days"),
        _rejected("GA:R07:US", "BlackRock Investment Institute", "Equity market outlook", "https://www.blackrock.com/us/individual/insights/equity-market-outlook", "CONTEXT_ONLY", "global_equity_article_without_growth_value_style_conclusion"),
        _rejected("GA:R08:US", "Morgan Stanley", "The Small and Mid-Cap Case for Active Management", "https://www.morganstanley.com/im/en-us/individual-investor/insights/articles/the-small-and-midcap-case-for-active-management.html", "REJECTED", "outside_90_day_window_or_date_unavailable"),
        _rejected("GA:R09:US", "State Street Global Advisors", "Fixed Income Outlook 2026", "https://www.ssga.com/us/en/institutional/insights/gmo-fixed-income-outlook", "CONTEXT_ONLY", "annual_global_fixed_income_context_not_recent_us_long_duration_view"),
        _rejected("GA:R10:US", "State Street Global Advisors", "Q2 bond market outlook for ETF investors", "https://www.ssga.com/us/en/intermediary/insights/bond-market-outlook-etf", "CONTEXT_ONLY", "policy_context_without_short_treasury_allocation_conclusion"),
        _rejected("GA:R14:US", "Invesco", "Asia Fixed Income Investment Outlook - Quarterly Update", "https://www.invesco.com/apac/en/institutional/insights/fixed-income/fixed-income-outlook-update.html", "REJECTED", "asia_high_yield_not_us_cell"),
        _rejected("GA:R17:US", "BlackRock Investment Institute", "Weekly market commentary: Reconciling AI earnings and valuations", "https://www.blackrock.com/corporate/insights/blackrock-investment-institute/publications/weekly-commentary", "CONTEXT_ONLY", "short_term_ig_credit_not_mid_term_us_ig_conclusion"),
        _rejected("GA:R18:US", "BlackRock Investment Institute", "Inflation Protected Bond Fund", "https://www.blackrock.com/us/financial-professionals/products/227589/blackrock-inflation-protected-bondinst-class-fund", "REJECTED", "product_page_not_research"),
        _rejected("GA:R18:US", "Vanguard", "VTIP ETF profile", "https://investor.vanguard.com/investment-products/etfs/profile/vtip", "REJECTED", "product_page_not_research"),
        _rejected("GA:R20:GLOBAL", "World Gold Council", "Gold Outlook 2026: Push ahead or pull back", "https://www.gold.org/goldhub/research/gold-outlook-2026", "PROPOSED_NOT_USED", "provider_not_in_outlook_sources"),
        _rejected("GA:R20:GLOBAL", "J.P. Morgan", "Gold Price Predictions for 2026 and 2027", "https://www.jpmorgan.com/insights/global-research/commodities/gold-prices", "REJECTED", "original_research_page_inaccessible_for_full_excerpt"),
    ]
    return dedupe_rejected_documents(rows)[0]


def _with_document_types(items: list[TargetedResearchItem]) -> list[TargetedResearchItem]:
    typed: list[TargetedResearchItem] = []
    for item in items:
        title = item.title.casefold()
        if "weekly market commentary" in title:
            doc_type = WEEKLY_COMMENTARY
        elif "global investment views" in title or "monthly pulse" in title:
            doc_type = MONTHLY_OUTLOOK
        elif "gold 2026 midyear" in title:
            doc_type = MIDYEAR_OUTLOOK
        elif "quarterly gold" in title:
            doc_type = QUARTERLY_OUTLOOK
        else:
            doc_type = ASSET_OUTLOOK
        typed.append(replace(item, document_type=doc_type))
    return typed


def seed_targeted_research(session: Session, snapshot_date: date = TARGETED_SNAPSHOT_DATE) -> int:
    seeded = 0
    items = [item for item in targeted_research_items() if _is_eligible_date(item.publication_date, snapshot_date)]
    by_url: dict[str, list[TargetedResearchItem]] = defaultdict(list)
    for item in items:
        by_url[canonicalize_url(item.canonical_url) or item.canonical_url].append(item)
    for group_items in by_url.values():
        article = _upsert_article(session, group_items)
        for item in group_items:
            if _upsert_view(session, article, item):
                seeded += 1
    session.flush()
    return seeded


def write_component_change_tracker(
    session: Session,
    *,
    current_snapshot_date: date,
    previous_snapshot_date: date,
    output_path: Path | None = None,
    summary_output_path: Path | None = None,
) -> dict[str, Any]:
    output_path = output_path or ROOT_DIR / "outputs" / "mae_change_tracker_latest.csv"
    summary_output_path = summary_output_path or ROOT_DIR / "outputs" / "mae_latest_validation_summary.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    current_snapshot = _component_snapshot(session, current_snapshot_date)
    previous_snapshot = _component_snapshot(session, previous_snapshot_date)
    if current_snapshot is None or previous_snapshot is None:
        raise RuntimeError("Для Change Tracker нужны оба component snapshots.")
    current = _component_cells_by_id(session, current_snapshot)
    previous = _component_cells_by_id(session, previous_snapshot)
    rows = [_change_tracker_row(current[cell_id], previous.get(cell_id), previous_snapshot_date) for cell_id in sorted(current)]
    _write_csv(output_path, CHANGE_TRACKER_COLUMNS, rows)
    counts = {
        "change_tracker_path": str(output_path),
        "cells_changed": len([row for row in rows if row["change_type"] != "UNCHANGED"]),
        "cells_unchanged": len([row for row in rows if row["change_type"] == "UNCHANGED"]),
        "cells_newly_scored": len([row for row in rows if row["change_type"] == "NEW_SCORE"]),
        "cells_lost_coverage": len([row for row in rows if row["change_type"] == "LOST_COVERAGE"]),
    }
    _patch_summary(summary_output_path, counts)
    return {"path": str(output_path), "rows": len(rows), **counts}


def _change_tracker_row(current: MaeComponentCell, previous: MaeComponentCell | None, previous_snapshot_date: date) -> dict[str, Any]:
    prev_score = previous.composite_score if previous is not None else None
    curr_score = current.composite_score
    change_type = _score_change_type(prev_score, curr_score)
    research_change = _research_change(current, previous, previous_snapshot_date)
    data_change = _layer_change(current, previous, "data")
    market_change = _layer_change(current, previous, "market")
    carried = _carried_research_items(current)
    expired = _expired_research_items(current, previous)
    return {
        "current_snapshot_date": current.snapshot_date.isoformat(),
        "previous_snapshot_date": previous_snapshot_date.isoformat(),
        "cell_id": current.canonical_cell_id,
        "previous_score": "" if prev_score is None else prev_score,
        "current_score": "" if curr_score is None else curr_score,
        "previous_score_type": score_type_for_cell(previous, previous.validation_status == "PASSED") if previous is not None else "INSUFFICIENT",
        "current_score_type": score_type_for_cell(current, current.validation_status == "PASSED"),
        "change_type": change_type,
        "research_change": research_change,
        "data_change": data_change,
        "market_change": market_change,
        "primary_reason": _primary_reason(change_type, research_change, data_change, market_change),
        "carried_research_items": "; ".join(carried),
        "expired_research_items": "; ".join(expired),
    }


def _score_change_type(previous_score: int | None, current_score: int | None) -> str:
    if previous_score is None and current_score is not None:
        return "NEW_SCORE"
    if previous_score is not None and current_score is None:
        return "LOST_COVERAGE"
    if previous_score is None and current_score is None:
        return "UNCHANGED"
    if current_score == previous_score:
        return "UNCHANGED"
    if (current_score or 0) > (previous_score or 0):
        return "UPGRADED"
    return "DOWNGRADED"


def _research_change(current: MaeComponentCell, previous: MaeComponentCell | None, previous_snapshot_date: date) -> str:
    current_items = _research_factors(current)
    previous_items = _research_factors(previous) if previous is not None else []
    current_keys = {_factor_key(row) for row in current_items}
    previous_keys = {_factor_key(row) for row in previous_items}
    if not current_items and previous_items:
        expired = [row for row in previous_items if str(row.get("lifecycle_status")) == "EXPIRED"]
        return "RESEARCH_EXPIRED" if expired else "LOST_COVERAGE"
    if current_items and not previous_items:
        return "NEW_SCORE"
    if current_keys - previous_keys:
        return "RESEARCH_UPDATED"
    if current_items and all(str(row.get("publication_date") or "") <= previous_snapshot_date.isoformat() for row in current_items):
        return "RESEARCH_CARRIED_FORWARD"
    return "UNCHANGED"


def _layer_change(current: MaeComponentCell, previous: MaeComponentCell | None, layer: str) -> str:
    if previous is None:
        return "UNCHANGED"
    current_score = getattr(current, f"{layer}_score")
    previous_score = getattr(previous, f"{layer}_score")
    if current_score == previous_score:
        return "UNCHANGED"
    return f"{layer.upper()}_CHANGED"


def _primary_reason(change_type: str, research_change: str, data_change: str, market_change: str) -> str:
    if research_change == "RESEARCH_UPDATED":
        return "new Research material"
    if research_change == "RESEARCH_EXPIRED":
        return "Research expired"
    if data_change != "UNCHANGED":
        return "изменение Fundamental Data"
    if market_change != "UNCHANGED":
        return "изменение benchmark-relative Market"
    if change_type == "LOST_COVERAGE":
        return "потеря подтверждения"
    if change_type == "NEW_SCORE":
        return "улучшение подтверждения"
    if change_type in {"UPGRADED", "DOWNGRADED"}:
        return "расхождение компонентов"
    return "оценка не изменилась"


def _research_factors(cell: MaeComponentCell | None) -> list[dict[str, Any]]:
    if cell is None:
        return []
    return list((cell.factor_details or {}).get("research", []) or [])


def _factor_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (str(row.get("provider_family") or row.get("provider_group") or ""), str(row.get("source_url") or ""), str(row.get("title") or ""))


def _carried_research_items(cell: MaeComponentCell) -> list[str]:
    result = []
    for row in _research_factors(cell):
        if row.get("carried_forward"):
            result.append(str(row.get("title") or row.get("provider") or "research item"))
    return result


def _expired_research_items(current: MaeComponentCell, previous: MaeComponentCell | None) -> list[str]:
    current_keys = {_factor_key(row) for row in _research_factors(current)}
    result = []
    for row in _research_factors(previous):
        if _factor_key(row) not in current_keys and row.get("lifecycle_status") == "EXPIRED":
            result.append(str(row.get("title") or row.get("provider") or "research item"))
    return result


def _component_snapshot(session: Session, snapshot_date: date) -> MaeComponentSnapshot | None:
    return session.scalar(
        select(MaeComponentSnapshot)
        .where(MaeComponentSnapshot.is_demo.is_(False), MaeComponentSnapshot.snapshot_date == snapshot_date)
        .order_by(desc(MaeComponentSnapshot.created_at))
    )


def _component_cells_by_id(session: Session, snapshot: MaeComponentSnapshot) -> dict[str, MaeComponentCell]:
    return {
        row.canonical_cell_id: row
        for row in session.scalars(select(MaeComponentCell).where(MaeComponentCell.snapshot_id == snapshot.id)).all()
    }


def write_targeted_outputs(
    session: Session,
    snapshot_date: date = TARGETED_SNAPSHOT_DATE,
    *,
    research_items_path: Path | None = None,
    rejected_items_path: Path | None = None,
    search_log_path: Path | None = None,
    summary_output_path: Path | None = None,
) -> TargetedCollectionResult:
    research_items_path = research_items_path or ROOT_DIR / "outputs" / "research_items_latest.csv"
    rejected_items_path = rejected_items_path or ROOT_DIR / "outputs" / "rejected_research_items.csv"
    search_log_path = search_log_path or ROOT_DIR / "outputs" / "research_search_log.csv"
    summary_output_path = summary_output_path or ROOT_DIR / "outputs" / "mae_latest_validation_summary.json"
    for path in (research_items_path, rejected_items_path, search_log_path, summary_output_path):
        path.parent.mkdir(parents=True, exist_ok=True)

    snapshot = session.scalar(
        select(MaeComponentSnapshot)
        .where(MaeComponentSnapshot.snapshot_date == snapshot_date, MaeComponentSnapshot.is_demo.is_(False))
        .order_by(desc(MaeComponentSnapshot.created_at))
    )
    cells = []
    if snapshot is not None:
        cells = list(
            session.scalars(
                select(MaeComponentCell)
                .where(MaeComponentCell.snapshot_id == snapshot.id)
                .order_by(MaeComponentCell.canonical_cell_id)
            ).all()
        )
    accepted_rows = _accepted_export_rows(cells)
    rejected_rows = _rejected_export_rows()
    search_rows = build_search_log_rows(snapshot_date=snapshot_date)
    exact_duplicates_removed = 0

    _write_csv(research_items_path, RESEARCH_ITEMS_COLUMNS, accepted_rows)
    _write_csv(rejected_items_path, REJECTED_RESEARCH_COLUMNS, rejected_rows)
    _write_csv(search_log_path, SEARCH_LOG_COLUMNS, search_rows)

    summary_counts = _targeted_summary_counts(
        session,
        cells,
        accepted_rows=accepted_rows,
        rejected_rows=rejected_rows,
        search_rows=search_rows,
        exact_duplicates_removed=exact_duplicates_removed,
    )
    _patch_summary(summary_output_path, summary_counts)
    return TargetedCollectionResult(
        seeded_views=len({row.get("research_view_id") for cell in cells for row in (cell.factor_details or {}).get("research", []) if row.get("schema_version") == TARGETED_SCHEMA_VERSION}),
        accepted_rows=len(accepted_rows),
        rejected_rows=len(rejected_rows),
        search_log_rows=len(search_rows),
        exact_duplicates_removed=exact_duplicates_removed,
        summary_counts=summary_counts,
    )


def build_search_log_rows(snapshot_date: date = TARGETED_SNAPSHOT_DATE) -> list[dict[str, Any]]:
    checked_at = utcnow().isoformat()
    accepted_by_cell_provider: dict[tuple[str, str], list[TargetedResearchItem]] = defaultdict(list)
    for item in targeted_research_items():
        accepted_by_cell_provider[(item.cell_id, item.provider)].append(item)
    rejected_by_cell_provider: dict[tuple[str, str], list[TargetedRejectedDocument]] = defaultdict(list)
    for row in targeted_rejected_documents():
        rejected_by_cell_provider[(row.cell_id, row.provider)].append(row)

    rows: list[dict[str, Any]] = []
    for cell_id, plan in SEARCH_PLAN.items():
        terms = "; ".join(plan["terms"])
        for provider in plan["providers"]:
            accepted = accepted_by_cell_provider.get((cell_id, provider), [])
            rejected = rejected_by_cell_provider.get((cell_id, provider), [])
            documents_found = len({canonicalize_url(row.canonical_url) or row.canonical_url for row in accepted}) + len(
                {canonicalize_url(row.canonical_url) or row.canonical_url for row in rejected}
            )
            relevant = len(accepted) + len([row for row in rejected if row.classification in {"CONTEXT_ONLY", "PROPOSED_NOT_USED"}])
            if accepted:
                status = "accepted"
                failure = ""
            elif rejected:
                status = "rejected"
                failure = "; ".join(sorted({row.rejection_reason for row in rejected}))
            else:
                status = "no_relevant_publication"
                failure = "no relevant medium-term cell-specific publication found in approved sources"
            rows.append(
                {
                    "snapshot_date": snapshot_date.isoformat(),
                    "cell_id": cell_id,
                    "provider": provider,
                    "query_or_search_path": terms,
                    "search_status": status,
                    "documents_found": documents_found,
                    "relevant_documents_found": relevant,
                    "accepted_documents": len(accepted),
                    "failure_reason": failure,
                    "checked_at": checked_at,
                }
            )
    return rows


def dedupe_targeted_items(items: list[TargetedResearchItem]) -> tuple[list[TargetedResearchItem], int]:
    deduped: list[TargetedResearchItem] = []
    seen: set[tuple[str, str, str]] = set()
    for item in items:
        key = (canonicalize_url(item.canonical_url) or item.canonical_url, item.cell_id, item.provider_family)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped, len(items) - len(deduped)


def dedupe_rejected_documents(rows: list[TargetedRejectedDocument]) -> tuple[list[TargetedRejectedDocument], int]:
    deduped: list[TargetedRejectedDocument] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for row in rows:
        key = (
            row.provider,
            row.title,
            canonicalize_url(row.canonical_url) or row.canonical_url,
            row.attempted_cell,
            row.rejection_reason,
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped, len(rows) - len(deduped)


def _item(
    cell_id: str,
    row_key: str,
    region: str,
    provider: str,
    provider_family: str,
    source_website: str,
    title: str,
    author: str,
    publication_date: date,
    canonical_url: str,
    geography: str,
    asset_class: str,
    asset_group: str,
    segment: str,
    direction: Direction,
    position_score: int,
    excerpt: str,
    drivers: tuple[str, ...],
    risks: tuple[str, ...],
    classification: str,
) -> TargetedResearchItem:
    return TargetedResearchItem(
        cell_id=cell_id,
        row_key=row_key,
        region=region,
        provider=provider,
        provider_family=provider_family,
        source_website=source_website,
        title=title,
        author=author,
        publication_date=publication_date,
        canonical_url=canonicalize_url(canonical_url) or canonical_url,
        horizon="MEDIUM_6_12M",
        geography=geography,
        asset_class=asset_class,
        asset_group=asset_group,
        segment=segment,
        direction=direction,
        position_score=position_score,
        confidence=Confidence.HIGH,
        excerpt=excerpt,
        drivers=drivers,
        risks=risks,
        classification=classification,
    )


def _rejected(
    cell_id: str,
    provider: str,
    title: str,
    url: str,
    classification: str,
    reason: str,
) -> TargetedRejectedDocument:
    return TargetedRejectedDocument(
        cell_id=cell_id,
        provider=provider,
        title=title,
        canonical_url=canonicalize_url(url) or url,
        attempted_cell=CELL_LABELS[cell_id],
        classification=classification,
        rejection_reason=reason,
    )


def _upsert_article(session: Session, items: list[TargetedResearchItem]) -> Article:
    first = items[0]
    source = get_or_create_manual_source(session, first.provider, first.source_website)
    source.category = "allowed_research"
    source.adapter_type = "targeted_manual"
    source.active = True
    content = _combined_article_text(items)
    article = create_article(
        session,
        source,
        first.title,
        first.publication_date,
        content,
        url=first.canonical_url,
        fetch_status=FetchStatus.MANUAL_TEXT,
        is_demo=False,
    )
    article.author = first.author
    article.content_text = content
    article.excerpt = content[:600]
    article.processing_status = "ANALYSED"
    article.fetch_status = FetchStatus.MANUAL_TEXT.value
    article.is_demo = False
    return article


def _upsert_view(session: Session, article: Article, item: TargetedResearchItem) -> bool:
    canonical = canonical_cell_for(item.row_key, item.region, include_not_applicable=True)
    cell_id = canonical.canonical_cell_id if canonical is not None else item.cell_id
    existing = session.scalar(
        select(ResearchView).where(
            ResearchView.article_id == article.id,
            ResearchView.canonical_cell_id == cell_id,
            ResearchView.schema_version == TARGETED_SCHEMA_VERSION,
        )
    )
    values = {
        "institution": item.provider,
        "horizon": item.horizon,
        "document_type": item.document_type,
        "investment_horizon": item.horizon,
        "region": item.region,
        "asset_class": item.asset_class,
        "asset_group": item.asset_group,
        "asset_segment": item.segment,
        "template_row_key": item.row_key,
        "canonical_cell_id": cell_id,
        "direction": item.direction.value,
        "position_score": item.position_score,
        "confidence": item.confidence.value,
        "drivers": list(item.drivers),
        "risks": list(item.risks),
        "catalysts": list(item.drivers[:2]),
        "evidence_quotes": [{"quote": item.excerpt, "locator": "targeted search excerpt", "source_url": item.canonical_url}],
        "extraction_method": ExtractionMethod.MANUAL.value,
        "review_status": STRICT_VALIDATED,
        "legacy_review_status": "",
        "strict_review_status": STRICT_VALIDATED,
        "schema_version": TARGETED_SCHEMA_VERSION,
        "is_demo": False,
    }
    if existing is None:
        session.add(ResearchView(article_id=article.id, **values))
        return True
    for key, value in values.items():
        setattr(existing, key, value)
    return False


def _combined_article_text(items: list[TargetedResearchItem]) -> str:
    first = items[0]
    lines = [
        first.title,
        f"Provider: {first.provider}. Publication date: {first.publication_date.isoformat()}. Horizon: medium-term 6-12 months.",
    ]
    for item in items:
        lines.extend(
            [
                item.excerpt,
                f"Target cell: {item.cell_id} {item.row_key}. Region: {_region_sentence(item.region)}.",
                f"Drivers: {'; '.join(item.drivers)}.",
                f"Risks: {'; '.join(item.risks)}.",
            ]
        )
    lines.append("This saved text is a targeted excerpt ledger from the original provider document, not synthetic market data.")
    return "\n".join(lines)


def _region_sentence(region: str) -> str:
    return "U.S." if region == "US" else region


def _accepted_export_rows(cells: list[MaeComponentCell]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for cell in cells:
        for factor in (cell.factor_details or {}).get("research", []) or []:
            if factor.get("schema_version") != TARGETED_SCHEMA_VERSION:
                continue
            classification = factor.get("direct_or_semi_direct")
            if classification not in {DIRECT_RESEARCH, SEMI_DIRECT_RESEARCH}:
                continue
            key = (
                str(cell.canonical_cell_id),
                str(factor.get("provider_family") or ""),
                canonicalize_url(str(factor.get("source_url") or "")) or str(factor.get("source_url") or ""),
                classification,
            )
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "cell_id": cell.canonical_cell_id,
                    "provider": factor.get("provider") or factor.get("source") or "",
                    "provider_family": factor.get("provider_family") or factor.get("provider_group") or "",
                    "title": factor.get("title") or "",
                    "author": factor.get("author") or "",
                    "publication_date": factor.get("publication_date") or "",
                    "canonical_url": canonicalize_url(str(factor.get("source_url") or "")) or factor.get("source_url") or "",
                    "horizon": factor.get("horizon") or "",
                    "geography": factor.get("geography") or "",
                    "asset_class": factor.get("asset_class") or "",
                    "segment": factor.get("segment") or "",
                    "stance": factor.get("extracted_stance") or "",
                    "excerpt": factor.get("source_excerpt") or "",
                    "drivers": "; ".join(factor.get("drivers") or []),
                    "risks": "; ".join(factor.get("risks") or []),
                    "classification": classification,
                    "accepted_for_score": str(bool(factor.get("accepted_for_score"))).lower(),
                    "score_contribution": "" if factor.get("score") is None else factor.get("score"),
                    "research_view_id": factor.get("research_view_id") or "",
                }
            )
    visible = [{key: value for key, value in row.items() if key != "research_view_id"} for row in rows]
    return sorted(visible, key=lambda row: (row["cell_id"], row["provider_family"], row["title"]))


def _rejected_export_rows() -> list[dict[str, Any]]:
    rows = []
    for item in targeted_rejected_documents():
        rows.append(
            {
                "provider": item.provider,
                "title": item.title,
                "URL": item.canonical_url,
                "rejection_reason": item.rejection_reason,
                "attempted_cell": item.attempted_cell,
                "classification": item.classification,
            }
        )
    return sorted(rows, key=lambda row: (row["attempted_cell"], row["provider"], row["title"]))


def _targeted_summary_counts(
    session: Session,
    cells: list[MaeComponentCell],
    *,
    accepted_rows: list[dict[str, Any]],
    rejected_rows: list[dict[str, Any]],
    search_rows: list[dict[str, Any]],
    exact_duplicates_removed: int,
) -> dict[str, Any]:
    accepted_urls = {canonicalize_url(str(row["canonical_url"])) or str(row["canonical_url"]) for row in accepted_rows if row.get("canonical_url")}
    rejected_urls = {canonicalize_url(str(row["URL"])) or str(row["URL"]) for row in rejected_rows if row.get("URL")}
    factors_by_cell: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for cell in cells:
        for factor in (cell.factor_details or {}).get("research", []) or []:
            if factor.get("schema_version") == TARGETED_SCHEMA_VERSION:
                factors_by_cell[cell.canonical_cell_id].append(factor)
    one_voice = 0
    consensus = 0
    for cell in cells:
        families = {
            factor.get("provider_family") or factor.get("provider_group")
            for factor in factors_by_cell.get(cell.canonical_cell_id, [])
            if factor.get("provider_family") or factor.get("provider_group")
        }
        if len(families) == 1:
            one_voice += 1
        if cell.research_score is not None and len(families) >= 2:
            consensus += 1
    accepted_cell_ids = {row["cell_id"] for row in accepted_rows}
    no_relevant = len([cell_id for cell_id in SEARCH_PLAN if cell_id not in accepted_cell_ids])
    current_research_factors = [
        factor
        for cell in cells
        for factor in (cell.factor_details or {}).get("research", []) or []
        if factor.get("schema_version") == TARGETED_SCHEMA_VERSION
    ]
    lifecycle_statuses = [str(factor.get("lifecycle_status") or "") for factor in current_research_factors]
    persisted_statuses = [
        str(row.lifecycle_status or "")
        for row in session.scalars(select(ResearchView).where(ResearchView.schema_version == TARGETED_SCHEMA_VERSION)).all()
    ]
    return {
        "research_items_count": len(accepted_rows),
        "rejected_research_count": len(rejected_rows),
        "irrelevant_mapping_count": 0,
        "unique_documents_checked": len(accepted_urls | rejected_urls),
        "direct_documents": len({row["canonical_url"] for row in accepted_rows if row.get("classification") == DIRECT_RESEARCH}),
        "semi_direct_documents": len({row["canonical_url"] for row in accepted_rows if row.get("classification") == SEMI_DIRECT_RESEARCH}),
        "context_only_documents": len({row["URL"] for row in rejected_rows if row.get("classification") == "CONTEXT_ONLY"}),
        "rejected_documents": len({row["URL"] for row in rejected_rows if row.get("classification") == "REJECTED"}),
        "cells_with_one_research_voice": one_voice,
        "cells_with_research_consensus": consensus,
        "cells_with_no_relevant_research": no_relevant,
        "exact_duplicates_removed": exact_duplicates_removed,
        "search_log_rows": len(search_rows),
        "active_research_items": lifecycle_statuses.count("ACTIVE"),
        "aging_research_items": lifecycle_statuses.count("AGING"),
        "carried_forward_items": len([factor for factor in current_research_factors if factor.get("carried_forward")]),
        "superseded_items": persisted_statuses.count("SUPERSEDED"),
        "expired_items": persisted_statuses.count("EXPIRED"),
    }


def _patch_summary(path: Path, counts: dict[str, Any]) -> None:
    payload: dict[str, Any] = {}
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(counts)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_csv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _is_eligible_date(publication_date: date, snapshot_date: date) -> bool:
    return LOOKBACK_START <= publication_date <= snapshot_date
