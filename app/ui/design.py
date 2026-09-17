from __future__ import annotations

import html
from datetime import date, datetime
from typing import Any
from urllib.parse import urlparse

import streamlit as st

from app.config import Settings


ACCENT = "#C6E79A"
TEXT = "#292823"
MUTED = "#68675F"
BORDER = "#38372F"
SURFACE = "#F5F3EA"
BACKGROUND = "#E8E7DD"
POSITIVE = "#A9D889"
NEGATIVE = "#C9675A"

VALUE_LABELS = {
    "BASE": "Базовый",
    "UPSIDE": "Позитивный",
    "DOWNSIDE": "Негативный",
    "BULLISH": "позитивный",
    "NEUTRAL": "нейтральный",
    "BEARISH": "негативный",
    "POSITIVE": "позитивный",
    "NEGATIVE": "негативный",
    "LOW": "низкая",
    "MEDIUM": "средняя",
    "HIGH": "высокая",
    "NEW": "новое",
    "REVIEWED": "обработано",
    "APPROVED": "опубликовано",
    "NEEDS_REVIEW": "недостаточно данных",
    "REJECTED": "отклонён",
    "NEW_IDEA": "новая идея",
    "MATERIAL_SHIFT": "существенное изменение",
    "NO_MATERIAL_CHANGE": "без существенного изменения",
    "CONFIRMED": "поддерживается данными",
    "MIXED": "смешанные подтверждения",
    "CONTRADICTED": "противоречит данным",
    "INSUFFICIENT_DATA": "недостаточно данных",
    "INSUFFICIENT_EVIDENCE": "недостаточно подтверждений",
    "NO_DATA": "нет данных",
    "NOT_APPLICABLE": "не применяется",
    "COVERED": "покрыто",
    "STALE": "устарело",
    "NOT_PRICED": "не учтено рынком",
    "PARTLY_PRICED": "частично учтено рынком",
    "PRICED_IN": "учтено рынком",
    "UNKNOWN": "не определено",
    "DISCOVERED": "найдено",
    "FETCHED": "загружено по ссылке",
    "PARSED": "текст извлечён",
    "ANALYSED": "проанализировано",
    "BLOCKED": "заблокировано",
    "MANUAL_TEXT": "текст вручную",
    "MANUAL_REQUIRED": "нужен текст публикации",
    "NEEDS_MANUAL_REVIEW": "недостаточно данных",
    "INVALID": "не материал",
    "DUPLICATE": "дубликат",
    "ERROR": "ошибка",
    "VIEWS_EXTRACTED": "взгляды извлечены",
    "EVIDENCE_INVALID": "недостаточно подтверждений",
    "LLM_REQUIRED": "нужен LLM-анализ",
    "SUCCESS": "успешно",
    "FAILED": "ошибка",
    "PARTIAL_FAILURE": "частично выполнено",
    "RUNNING": "выполняется",
}

ASSET_LABELS = {
    "EQUITY": "Акции",
    "Equity": "Акции",
    "Equities": "Акции",
    "FIXED INCOME": "Облигации",
    "Fixed Income": "Облигации",
    "GOLD": "Альтернативные активы",
    "Gold": "Альтернативные активы",
    "COMMODITIES": "Сырьевые товары",
}
GROUP_LABELS = {
    "Wide Market": "Широкий рынок",
    "Other categories": "Стили",
    "Sector": "Секторы",
    "US Equities": "Акции США",
    "Europe GOV": "Государственные облигации Европы",
    "GOV": "Государственные",
    "CORP HY": "Корпоративные High Yield",
    "CORP IG": "Корпоративные Investment Grade",
    "GOLD": "Золото",
    "Gold": "Золото",
    "Commodities": "Сырьевые товары",
}
SEGMENT_LABELS = {
    "Wide Market": "Широкий рынок",
    "Broad Equity Market": "Широкий рынок акций",
    "Market Breadth": "Ширина рынка",
    "Market Breadth (Equal Weight)": "Ширина рынка",
    "Preferred": "Привилегированные",
    "Value": "Стоимость",
    "Growth": "Рост",
    "US Growth": "Рост США",
    "Small Cap": "Малая капитализация",
    "Large Cap": "Крупная капитализация",
    "Comm Serv": "Коммуникационные услуги",
    "Long Term": "Долгосрочные",
    "Short Term": "Краткосрочные",
    "Mid Term": "Среднесрочные",
    "Government Bonds Long Term": "Долгосрочные государственные облигации",
    "Government Bonds Short Term": "Краткосрочные государственные облигации",
    "Government Bonds Mid Term": "Среднесрочные государственные облигации",
    "Corporate High Yield Long Term": "Корпоративные облигации High Yield, долгосрочные",
    "Corporate High Yield Short Term": "Корпоративные облигации High Yield, краткосрочные",
    "Corporate High Yield Mid Term": "Корпоративные облигации High Yield, среднесрочные",
    "Corporate Investment Grade Long Term": "Корпоративные облигации Investment Grade, долгосрочные",
    "Corporate Investment Grade Short Term": "Корпоративные облигации Investment Grade, краткосрочные",
    "Corporate Investment Grade Mid Term": "Корпоративные облигации Investment Grade, среднесрочные",
    "Inflation Linked": "Инфляционно-защищённые",
    "Mortgage-Backed": "Ипотечные",
    "Subordinated": "Субординированные",
    "GOLD": "Золото",
    "Gold": "Золото",
    "Other precious metals": "Другие драгоценные металлы",
    "Other Precious Metals": "Другие драгоценные металлы",
}
REGION_LABELS = {
    "Global": "Глобально",
    "US": "США",
    "Europe": "Европа",
    "UK": "Великобритания",
    "Japan": "Япония",
    "EM ex Ch": "EM без Китая",
    "EM ex China": "EM без Китая",
}


def apply_global_style() -> None:
    st.markdown(
        f"""
<style>
:root {{
  --mae-bg: {BACKGROUND};
  --mae-surface: {SURFACE};
  --mae-text: {TEXT};
  --mae-muted: {MUTED};
  --mae-border: {BORDER};
  --mae-accent: {ACCENT};
  --mae-positive: {POSITIVE};
  --mae-negative: {NEGATIVE};
  --mae-font-condensed: "Avenir Next Condensed", "Arial Narrow", "Roboto Condensed", "Helvetica Neue", Arial, sans-serif;
  --mae-font-body: "Avenir Next", "Helvetica Neue", Arial, sans-serif;
  color-scheme: light !important;
}}
.stApp {{
  background: var(--mae-bg);
  color: var(--mae-text);
  -webkit-text-fill-color: var(--mae-text);
  font-family: var(--mae-font-body);
  color-scheme: light !important;
}}
.block-container {{
  max-width: 1180px;
  padding-top: 1rem;
  padding-bottom: 2.5rem;
}}
#MainMenu,
div[data-testid="stToolbar"],
div[data-testid="stDecoration"],
header[data-testid="stHeader"] {{
  display: none !important;
}}
section[data-testid="stSidebar"],
[data-testid="stSidebar"] {{
  background: var(--mae-surface);
  border-right: 1px solid var(--mae-border);
  color: var(--mae-text) !important;
  -webkit-text-fill-color: var(--mae-text) !important;
  color-scheme: light !important;
}}
[data-testid="stSidebar"] *,
[data-testid="stSidebar"] span,
[data-testid="stSidebar"] p,
[data-testid="stSidebar"] label,
[data-testid="stSidebar"] button,
[data-testid="stSidebar"] svg,
[data-testid="stSidebar"] [role="img"],
[data-testid="stSidebar"] [data-testid*="icon"] {{
  color: var(--mae-text) !important;
  -webkit-text-fill-color: var(--mae-text) !important;
  opacity: 1 !important;
  color-scheme: light !important;
}}
[data-testid="stSidebar"] > div {{
  padding-top: 1.1rem;
}}
[data-testid="stSidebar"] h3 {{
  font-family: var(--mae-font-condensed);
  font-size: 1.8rem;
  line-height: .95;
  text-transform: uppercase;
  color: var(--mae-text);
  -webkit-text-fill-color: var(--mae-text);
  border-bottom: 1px solid var(--mae-border);
  padding-bottom: .75rem;
  margin-bottom: .75rem;
}}
[data-testid="stSidebarNav"],
[data-testid="stSidebarNav"] * {{
  color: var(--mae-text) !important;
  -webkit-text-fill-color: var(--mae-text) !important;
  opacity: 1 !important;
}}
[data-testid="stSidebar"] a,
[data-testid="stSidebarNav"] a {{
  color: var(--mae-text) !important;
  -webkit-text-fill-color: var(--mae-text) !important;
  border-radius: 0 !important;
  border-bottom: 1px solid rgba(56,55,47,.22);
  min-height: 2.4rem;
  font-family: var(--mae-font-condensed);
  font-size: 1rem;
  text-transform: none;
  opacity: 1 !important;
}}
[data-testid="stSidebar"] a *,
[data-testid="stSidebarNav"] a * {{
  color: var(--mae-text) !important;
  -webkit-text-fill-color: var(--mae-text) !important;
  opacity: 1 !important;
}}
[data-testid="stSidebar"] a[aria-current="page"],
[data-testid="stSidebarNav"] a[aria-current="page"] {{
  background: var(--mae-accent) !important;
  color: var(--mae-text) !important;
  -webkit-text-fill-color: var(--mae-text) !important;
}}
[data-testid="stSidebar"] a:hover,
[data-testid="stSidebarNav"] a:hover {{
  background: #B7DB85 !important;
  color: var(--mae-text) !important;
  -webkit-text-fill-color: var(--mae-text) !important;
}}
[data-testid="stSidebar"] button:hover {{
  background: #DEDCCF !important;
  color: var(--mae-text) !important;
  -webkit-text-fill-color: var(--mae-text) !important;
}}
h1, h2, h3, h4, h5, h6, p, label, span {{
  letter-spacing: 0;
}}
h1, h2, h3 {{
  font-family: var(--mae-font-condensed);
  color: var(--mae-text);
  text-transform: none;
}}
h1 {{
  font-size: clamp(2.1rem, 4vw, 3.6rem);
  line-height: 1;
  margin-bottom: .75rem;
  border-bottom: 1px solid var(--mae-border);
  padding-bottom: .55rem;
}}
h2, h3 {{
  line-height: .98;
}}
div[data-testid="stMetric"] {{
  background: var(--mae-surface);
  border: 1px solid var(--mae-border);
  border-radius: 0;
  padding: 0.85rem 0.95rem;
  box-shadow: none;
}}
div[data-testid="stMetricLabel"] p {{
  color: var(--mae-muted);
  font-family: var(--mae-font-condensed);
  text-transform: uppercase;
}}
div[data-testid="stMetricValue"] {{
  color: var(--mae-text);
  font-family: var(--mae-font-condensed);
}}
.stButton > button,
.stDownloadButton > button {{
  border-radius: 0;
  border: 1px solid var(--mae-border);
  background: var(--mae-surface);
  color: var(--mae-text);
  min-height: 2.5rem;
  font-family: var(--mae-font-condensed);
  font-weight: 760;
  text-transform: none;
}}
.stButton > button:hover,
.stDownloadButton > button:hover {{
  border-color: var(--mae-border);
  background: var(--mae-accent);
  color: var(--mae-text);
}}
input[type="radio"],
input[type="checkbox"] {{
  accent-color: var(--mae-accent);
}}
div[role="radiogroup"] label div:first-child {{
  border-color: var(--mae-border);
}}
button[data-baseweb="tab"][aria-selected="true"] * {{
  color: var(--mae-text) !important;
}}
div[data-baseweb="tab-highlight"] {{
  background-color: var(--mae-accent) !important;
}}
div[data-testid="stTab"][aria-selected="true"] p,
div[data-testid="stTab"][data-selected="true"] p {{
  color: var(--mae-accent) !important;
}}
div[data-testid="stTab"] .react-aria-SelectionIndicator {{
  background-color: var(--mae-accent) !important;
}}
.stButton > button[kind="primary"],
.stDownloadButton > button[kind="primary"] {{
  background: var(--mae-accent);
  border-color: var(--mae-border);
  color: var(--mae-text);
}}
div[data-testid="stVerticalBlockBorderWrapper"] {{
  border: 1px solid var(--mae-border);
  border-radius: 0;
  background: var(--mae-surface);
  box-shadow: none;
}}
div[data-testid="stVerticalBlockBorderWrapper"] > div {{
  border-radius: 0 !important;
}}
input,
textarea,
div[data-baseweb="select"] > div,
div[data-baseweb="base-input"] {{
  border-radius: 0 !important;
}}
.stTabs [data-baseweb="tab-list"] {{
  border-bottom: 1px solid var(--mae-border);
}}
.stTabs [data-baseweb="tab"] {{
  font-family: var(--mae-font-condensed);
  text-transform: uppercase;
  color: var(--mae-muted);
}}
.stTabs [data-baseweb="tab"] * {{
  color: var(--mae-text) !important;
}}
.stTabs button[data-baseweb="tab"] p,
.stTabs button[data-baseweb="tab"] span {{
  color: var(--mae-text) !important;
}}
.stTabs button[aria-selected="true"] {{
  background: var(--mae-accent) !important;
}}
.stTabs button[aria-selected="true"] p,
.stTabs button[aria-selected="true"] span {{
  color: var(--mae-text) !important;
}}
.mae-topbar {{
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 1rem;
  background: transparent;
  border-top: 1px solid var(--mae-border);
  border-bottom: 1px solid var(--mae-border);
  padding: .75rem 0;
  margin-bottom: 1rem;
  box-shadow: none;
}}
.mae-brand-title {{
  font-family: var(--mae-font-condensed);
  font-size: 1.65rem;
  line-height: .95;
  font-weight: 760;
  color: var(--mae-text);
  margin-bottom: 0.1rem;
  text-transform: uppercase;
}}
.mae-brand-subtitle {{
  color: var(--mae-muted);
  font-size: 0.88rem;
}}
.mae-status-row {{
  display: flex;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: 0.45rem;
}}
.mae-badge {{
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  border-radius: 0;
  padding: 0.18rem 0.48rem;
  font-family: var(--mae-font-condensed);
  font-size: 0.82rem;
  font-weight: 700;
  border: 1px solid var(--mae-border);
  white-space: nowrap;
  text-transform: uppercase;
}}
.mae-badge::before {{
  content: "";
  width: 0.42rem;
  height: 0.42rem;
  border-radius: 999px;
  background: currentColor;
}}
.mae-badge-neutral {{
  color: var(--mae-text);
  background: var(--mae-surface);
}}
.mae-badge-accent {{
  color: var(--mae-text);
  background: var(--mae-accent);
}}
.mae-badge-positive {{
  color: var(--mae-text);
  background: var(--mae-positive);
}}
.mae-badge-negative {{
  color: var(--mae-surface);
  background: var(--mae-negative);
}}
.mae-badge-warning {{
  color: var(--mae-text);
  background: #EFE4B8;
}}
.mae-card {{
  background: var(--mae-surface);
  border: 1px solid var(--mae-border);
  border-radius: 0;
  padding: 1rem;
  box-shadow: none;
  margin-bottom: 0.85rem;
}}
.mae-card-compact {{
  padding: 0.85rem;
  border-radius: 0;
}}
.mae-status-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: .45rem .8rem;
  margin-top: .45rem;
}}
.mae-status-grid div {{
  display: flex;
  justify-content: space-between;
  gap: .65rem;
  border-top: 1px solid rgba(56,55,47,.22);
  padding-top: .35rem;
  color: var(--mae-text);
}}
.mae-status-grid span {{
  color: var(--mae-muted);
  -webkit-text-fill-color: var(--mae-muted);
}}
.mae-status-grid b {{
  color: var(--mae-text);
  -webkit-text-fill-color: var(--mae-text);
}}
.mae-kicker {{
  color: var(--mae-muted);
  font-family: var(--mae-font-condensed);
  font-size: 0.86rem;
  font-weight: 700;
  text-transform: uppercase;
  margin-bottom: 0.25rem;
}}
.mae-card-title {{
  color: var(--mae-text);
  font-family: var(--mae-font-condensed);
  font-size: 1.22rem;
  line-height: 1.05;
  font-weight: 760;
  margin-bottom: 0.35rem;
  text-transform: none;
}}
.mae-card-body {{
  color: var(--mae-text);
  font-size: 1rem;
  line-height: 1.5;
  overflow-wrap: anywhere;
  word-break: normal;
}}
.mae-muted {{
  color: var(--mae-muted);
}}
.mae-market-context {{
  color: var(--mae-text);
  font-size: 0.98rem;
  line-height: 1.45;
  margin: 0.15rem 0 0.1rem;
  overflow-wrap: anywhere;
  word-break: normal;
}}
.mae-update-progress {{
  border-top: 1px solid rgba(56,55,47,.28);
  padding-top: 0.65rem;
  margin-top: 0.6rem;
}}
.mae-update-progress__title {{
  font-family: var(--mae-font-condensed);
  font-size: 1rem;
  font-weight: 760;
  line-height: 1.15;
  overflow-wrap: anywhere;
}}
.mae-update-progress__percent {{
  color: var(--mae-muted);
  -webkit-text-fill-color: var(--mae-muted);
  font-family: var(--mae-font-condensed);
  font-size: 0.9rem;
  font-weight: 760;
  line-height: 1.2;
  margin-top: 0.1rem;
}}
.mae-update-progress__title--success,
.mae-update-progress__title--partial_success {{
  color: #2E5B28;
  -webkit-text-fill-color: #2E5B28;
}}
.mae-update-progress__title--failed {{
  color: #9E3F35;
  -webkit-text-fill-color: #9E3F35;
}}
.mae-update-progress__step {{
  font-size: 0.92rem;
  line-height: 1.35;
  margin-top: 0.15rem;
  overflow-wrap: anywhere;
  word-break: normal;
}}
.mae-grid-2 {{
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.85rem;
}}
.mae-grid-3 {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 0.75rem;
}}
.mae-mini-panel {{
  border: 1px solid var(--mae-border);
  border-radius: 0;
  padding: 0.75rem;
  background: var(--mae-bg);
}}
.mae-heatmap-wrap {{
  max-width: 100%;
  overflow-x: auto;
  border: 1px solid rgba(56,55,47,.28);
  background: var(--mae-surface);
  margin-bottom: 1rem;
}}
.mae-heatmap {{
  border-collapse: separate;
  border-spacing: 0;
  width: 100%;
  min-width: 860px;
  table-layout: fixed;
  font-size: .84rem;
}}
.mae-heatmap th,
.mae-heatmap td {{
  border-right: 1px solid rgba(56,55,47,.18);
  border-bottom: 1px solid rgba(56,55,47,.18);
  padding: .36rem .38rem;
  text-align: center;
  white-space: normal;
  overflow-wrap: anywhere;
}}
.mae-heatmap thead th {{
  position: sticky;
  top: 0;
  z-index: 2;
  background: #DEDCCF;
}}
.mae-sticky-row {{
  position: sticky;
  left: 0;
  z-index: 1;
  background: var(--mae-surface);
  text-align: left !important;
  width: 190px;
}}
.mae-sticky-corner {{
  position: sticky;
  left: 0;
  z-index: 3 !important;
  width: 190px;
  text-align: left !important;
}}
.mae-cell-pos {{
  background: #A9D889;
  color: #292823;
  font-weight: 760;
}}
.mae-cell-neg {{
  background: #C9675A;
  color: #F5F3EA;
  font-weight: 760;
}}
.mae-cell-zero {{
  background: #F5F3EA;
  color: #292823;
  font-weight: 760;
}}
.mae-cell-none {{
  background: #DDD8C8;
  color: #68675F;
}}
.mae-cell-na {{
  background: #C8C2B2;
  color: #68675F;
}}
.mae-history-row {{
  display: grid;
  grid-template-columns: 7.2rem minmax(180px, 1fr) minmax(230px, 330px);
  gap: .75rem;
  align-items: center;
  padding: .45rem 0;
  border-bottom: 1px solid rgba(56,55,47,.16);
}}
.mae-history-pills {{
  display: flex;
  flex-wrap: wrap;
  gap: .4rem;
}}
.mae-history-pill {{
  display: inline-flex;
  border: 1px solid rgba(56,55,47,.24);
  padding: .22rem .42rem;
  font-size: .86rem;
  background: #EEEBDD;
}}
.mae-score {{
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 3.1rem;
  height: 2.65rem;
  border-radius: 0;
  border: 1px solid var(--mae-border);
  font-family: var(--mae-font-condensed);
  font-size: 1.45rem;
  font-weight: 800;
}}
.mae-score-pos {{
  background: var(--mae-positive);
  color: var(--mae-text);
}}
.mae-score-neg {{
  background: var(--mae-negative);
  color: var(--mae-surface);
}}
.mae-score-zero {{
  background: var(--mae-surface);
  color: var(--mae-muted);
}}
.mae-empty {{
  background: var(--mae-surface);
  border: 1px dashed var(--mae-border);
  border-radius: 0;
  padding: 1.2rem;
  color: var(--mae-muted);
}}
.mae-nav-caption {{
  color: var(--mae-muted);
  font-size: 0.74rem;
  font-weight: 750;
  margin: 1.1rem 0 0.35rem;
}}
.mae-editorial-intro {{
  display: grid;
  grid-template-columns: minmax(0, 1.25fr) minmax(15rem, .75fr);
  gap: 1rem;
  border-bottom: 1px solid var(--mae-border);
  padding-bottom: 1rem;
  margin-bottom: 1rem;
}}
.mae-editorial-intro-main {{
  font-size: clamp(1rem, 1.8vw, 1.35rem);
  line-height: 1.25;
  color: var(--mae-text);
  max-width: 58rem;
}}
.mae-editorial-note {{
  background: var(--mae-accent);
  border: 1px solid var(--mae-border);
  padding: .85rem;
  color: var(--mae-text);
}}
.mae-editorial-stat {{
  display: grid;
  grid-template-columns: auto 1fr;
  gap: .75rem;
  align-items: end;
  border-bottom: 1px solid rgba(56,55,47,.35);
  padding: .55rem 0;
}}
.mae-editorial-stat b {{
  font-family: var(--mae-font-condensed);
  font-size: 2.15rem;
  line-height: .9;
  color: var(--mae-text);
}}
@media (max-width: 900px) {{
  .mae-topbar {{
    flex-direction: column;
  }}
  .mae-status-row {{
    justify-content: flex-start;
  }}
  .mae-grid-2,
  .mae-grid-3 {{
    grid-template-columns: 1fr;
  }}
  .mae-editorial-intro {{
    grid-template-columns: 1fr;
  }}
  h1 {{
    font-size: 2.45rem;
  }}
  .block-container {{
    padding-left: .9rem;
    padding-right: .9rem;
  }}
}}
</style>
""",
        unsafe_allow_html=True,
    )


def render_header(settings: Settings) -> None:
    st.markdown(
        f"""
<div class="mae-topbar">
  <div>
    <div class="mae-brand-title">MAE Shift Signal</div>
    <div class="mae-brand-subtitle">Мониторинг институциональных прогнозов и сценариев</div>
  </div>
</div>
""",
        unsafe_allow_html=True,
    )


def editorial_intro(body: str, note_title: str, note_lines: list[tuple[str, int | str]]) -> str:
    stats = "".join(
        f"""
<div class="mae-editorial-stat">
  <b>{html.escape(str(value))}</b>
  <span>{html.escape(label)}</span>
</div>
"""
        for label, value in note_lines
    )
    return f"""
<div class="mae-editorial-intro">
  <div class="mae-editorial-intro-main">{html.escape(body)}</div>
  <div class="mae-editorial-note">
    <div class="mae-kicker">{html.escape(note_title)}</div>
    {stats}
  </div>
</div>
"""


def badge(label: str, tone: str = "neutral") -> str:
    tone_class = {
        "positive": "mae-badge-positive",
        "negative": "mae-badge-negative",
        "warning": "mae-badge-warning",
        "accent": "mae-badge-accent",
    }.get(tone, "mae-badge-neutral")
    return f'<span class="mae-badge {tone_class}">{html.escape(str(label))}</span>'


def metric_card(label: str, value: int | str, hint: str = "") -> str:
    return f"""
<div class="mae-card mae-card-compact">
  <div class="mae-kicker">{html.escape(label)}</div>
  <div style="font-family:var(--mae-font-condensed);font-size:2.2rem;font-weight:800;color:{TEXT};line-height:.95;">{html.escape(str(value))}</div>
  <div class="mae-muted" style="font-size:.82rem;margin-top:.25rem;">{html.escape(hint)}</div>
</div>
"""


def status_panel(title: str, rows: list[tuple[str, int | str]]) -> str:
    body = "".join(
        f"<div><span>{html.escape(str(label))}</span><b>{html.escape(str(value))}</b></div>"
        for label, value in rows
    )
    return f"""
<div class="mae-card mae-card-compact">
  <div class="mae-kicker">{html.escape(str(title))}</div>
  <div class="mae-status-grid">{body}</div>
</div>
"""


def empty_state(title: str, body: str) -> None:
    st.markdown(
        f"""
<div class="mae-empty">
  <div style="font-weight:760;color:{TEXT};margin-bottom:.25rem;">{html.escape(title)}</div>
  <div>{html.escape(body)}</div>
</div>
""",
        unsafe_allow_html=True,
    )


def score_badge(score: int | str | None) -> str:
    try:
        value = int(score or 0)
    except (TypeError, ValueError):
        value = 0
    klass = "mae-score-pos" if value > 0 else "mae-score-neg" if value < 0 else "mae-score-zero"
    text = f"+{value}" if value > 0 else str(value)
    return f'<span class="mae-score {klass}">{html.escape(text)}</span>'


def score_tone(score: int | None) -> str:
    value = int(score or 0)
    if value > 0:
        return "positive"
    if value < 0:
        return "negative"
    return "neutral"


def humanize(value: Any) -> str:
    raw = getattr(value, "value", value)
    if isinstance(raw, bool):
        return "Да" if raw else "Нет"
    if raw is None:
        return ""
    text = str(raw)
    return VALUE_LABELS.get(text, text)


TEXT_REPLACEMENTS = [
    ("DEMO/RULE", "Правиловой анализ"),
    ("Europe GOV Short Term", "краткосрочные государственные облигации Европы"),
    ("US Equities", "акции США"),
    ("Broad Equity Market", "широкий рынок акций"),
    ("Global Gold", "глобальное золото"),
    ("sticky inflation keeps front-end yields high", "устойчивая инфляция удерживает краткосрочные доходности высокими"),
    ("front-end yields", "краткосрочные доходности"),
    ("front-end", "короткий конец кривой"),
    ("carry/roll-down", "доход от переноса и скатывания по кривой"),
    ("market-implied", "рыночные ожидания"),
    ("real yields", "реальные доходности"),
    ("ETF flows", "потоки ETF"),
    ("NEW_IDEA", "новая идея"),
    ("MATERIAL_SHIFT", "существенное изменение"),
    ("NO_MATERIAL_CHANGE", "без существенного изменения"),
    ("INSUFFICIENT_DATA", "недостаточно данных"),
    ("PARTLY_PRICED", "частично учтено рынком"),
    ("NOT_PRICED", "не учтено рынком"),
    ("PRICED_IN", "учтено рынком"),
    ("CONFIRMED", "поддерживается данными"),
    ("CONTRADICTED", "противоречит данным"),
    ("BULLISH", "позитивный"),
    ("BEARISH", "негативный"),
    ("NEUTRAL", "нейтральный"),
    ("POSITIVE", "позитивный"),
    ("NEGATIVE", "негативный"),
    ("UPSIDE", "позитивный сценарий"),
    ("DOWNSIDE", "негативный сценарий"),
    ("BASE", "базовый сценарий"),
    ("Short Term", "краткосрочные"),
    ("Mid Term", "среднесрочные"),
    ("Long Term", "долгосрочные"),
    ("Equities", "акции"),
    ("equities", "акции"),
    ("evidence", "подтверждение"),
    ("Confidence", "Уверенность"),
    ("confidence", "уверенность"),
    ("pricing", "рыночная оценка"),
    ("score", "оценка"),
    ("trigger/veto", "триггеры и условия отмены"),
    ("->", "→"),
]


def user_text(value: Any) -> str:
    text = compact_text(str(value or ""), 2000)
    for old, new in TEXT_REPLACEMENTS:
        text = text.replace(old, new)
    return text


def format_date(value: Any, with_time: bool = False) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%d.%m.%Y %H:%M") if with_time else value.strftime("%d.%m.%Y")
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    return parsed.strftime("%d.%m.%Y %H:%M") if with_time else parsed.strftime("%d.%m.%Y")


def region_label(value: str | None) -> str:
    return REGION_LABELS.get(str(value or ""), str(value or ""))




def row_key_label(row_key: str | None) -> str:
    if not row_key:
        return ""
    parts = str(row_key).split("|")
    if len(parts) != 3:
        return str(row_key)
    asset, group, segment = parts
    if row_key in {"GOLD|GOLD|GOLD", "COMMODITIES|Commodities|Gold"}:
        return "Сырьевые товары · Золото"
    return " · ".join(
        part
        for part in [
            ASSET_LABELS.get(asset, asset),
            GROUP_LABELS.get(group, group),
            SEGMENT_LABELS.get(segment, segment),
        ]
        if part
    )


def row_key_short(row_key: str | None) -> str:
    label = row_key_label(row_key)
    if label.startswith("Альтернативные активы · Золото"):
        return "Золото"
    return label


def source_label(url_or_name: str | None) -> str:
    value = str(url_or_name or "").strip()
    if not value:
        return "источник не указан"
    if value.startswith("manual://"):
        return "ручной ввод"
    if value.startswith("official://"):
        return value.replace("official://", "официальный источник: ")
    parsed = urlparse(value)
    if parsed.netloc:
        return parsed.netloc.replace("www.", "")
    return value


def link_html(value: str | None, label: str | None = None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("http://") or text.startswith("https://"):
        safe_href = html.escape(text, quote=True)
        safe_label = html.escape(label or source_label(text))
        return f'<a href="{safe_href}" target="_blank" rel="noopener noreferrer">{safe_label}</a>'
    return html.escape(label or humanize(text))


def list_html(items: list[str] | tuple[str, ...] | None, empty: str = "не указано") -> str:
    values = [str(item).strip() for item in (items or []) if str(item).strip()]
    if not values:
        return f"<span class='mae-muted'>{html.escape(empty)}</span>"
    return "<ul style='margin:.25rem 0 0 1rem;padding:0;'>" + "".join(f"<li>{html.escape(user_text(item))}</li>" for item in values[:5]) + "</ul>"


def compact_text(value: str | None, limit: int = 260) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def apply_full_snapshot_style() -> None:
    """Editorial product styling with explicit Safari-safe navigation rules."""
    st.markdown(
        """
<style>
:root {
  --mae-ink: #111111;
  --mae-muted: #686868;
  --mae-bg: #F4F4F1;
  --mae-surface: #FFFFFF;
  --mae-dark: #161616;
  --mae-accent: #C9FF38;
  --mae-line: #D7D7D1;
  --mae-soft: #E9E9E4;
  --mae-positive: #86A785;
  --mae-negative: #B7766E;
}
.stApp {
  background: var(--mae-bg);
  color: var(--mae-ink);
  font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif;
  color-scheme: light !important;
}
.block-container {
  max-width: 1480px;
  padding-top: 3.75rem;
  padding-bottom: 5rem;
  padding-left: clamp(1.4rem, 3vw, 3.4rem);
  padding-right: clamp(1.4rem, 3vw, 3.4rem);
}
header[data-testid="stHeader"] {
  background: rgba(244,244,241,.96);
}
div[data-testid="stToolbar"], #MainMenu, div[data-testid="stDecoration"] {
  visibility: hidden;
}
section[data-testid="stSidebar"], [data-testid="stSidebar"] {
  display: block !important;
  visibility: visible !important;
  opacity: 1 !important;
  background: var(--mae-dark);
  border-right: 0;
  color: var(--mae-ink);
  color-scheme: light !important;
}
[data-testid="stSidebar"] > div:first-child {
  width: 282px;
  padding-top: .6rem;
}
[data-testid="stSidebar"] p,
[data-testid="stSidebar"] label,
[data-testid="stSidebar"] span,
[data-testid="stSidebar"] summary {
  color: #ECECE8 !important;
  -webkit-text-fill-color: #ECECE8 !important;
}
[data-testid="stSidebarNav"] a {
  color: #D6D6D0 !important;
  border-radius: 0;
  margin: 0;
  min-height: 2.65rem;
  border-left: 3px solid transparent;
  padding-left: .7rem;
  font-size: .88rem;
  letter-spacing: .01em;
}
[data-testid="stSidebarNav"] a:not([aria-current="page"]) * {
  color: #D6D6D0 !important;
  -webkit-text-fill-color: #D6D6D0 !important;
}
[data-testid="stSidebarNav"] a[aria-current="page"] {
  background: #252525 !important;
  border-left-color: var(--mae-accent);
  color: white !important;
}
[data-testid="stSidebarNav"] a[aria-current="page"] * {
  color: white !important;
  -webkit-text-fill-color: white !important;
}
[data-testid="stSidebarNav"] a:hover { background:#202020 !important; }
[data-testid="stSidebar"] [data-testid="stExpander"] summary {
  background: #202020 !important;
  color: #ECECE8 !important;
}
[data-testid="stSidebar"] .stButton > button,
[data-testid="stSidebar"] .stDownloadButton > button {
  background: #202020 !important;
  border: 1px solid #3A3A3A !important;
  color: #F4F4F1 !important;
  min-height: 2.45rem;
  white-space: normal !important;
  height: auto !important;
}
[data-testid="stSidebar"] .stButton > button p,
[data-testid="stSidebar"] .stDownloadButton > button p {
  color: #F4F4F1 !important;
  -webkit-text-fill-color: #F4F4F1 !important;
  white-space: normal !important;
}
[data-testid="stSidebar"] .stButton > button[kind="primary"] {
  background: var(--mae-accent) !important;
  border-color: var(--mae-accent) !important;
  color: #111 !important;
}
[data-testid="stSidebar"] .stButton > button[kind="primary"] p {
  color: #111 !important;
  -webkit-text-fill-color: #111 !important;
}
button[data-testid="stSidebarCollapseButton"], [data-testid="collapsedControl"] {
  display: flex !important;
  visibility: visible !important;
  opacity: 1 !important;
}
h1 {
  color: var(--mae-ink);
  font-size: clamp(3rem, 6vw, 6.2rem);
  font-weight: 760;
  letter-spacing: -.065em;
  line-height: .92;
  margin: 2.6rem 0 1.15rem;
  text-transform: uppercase;
}
h2 {
  background: transparent;
  color: var(--mae-ink) !important;
  border-radius: 0;
  border-top: 1px solid var(--mae-ink);
  padding: 1rem 0 0;
  font-size: clamp(1.55rem, 2.6vw, 2.6rem);
  letter-spacing: -.035em;
  line-height: 1.05;
  margin-top: 4rem;
  margin-bottom: 1.25rem;
}
h3, h4 {
  color: var(--mae-ink);
  letter-spacing: -.02em;
}
div[data-testid="stMetric"] {
  background: transparent;
  border: 0;
  border-top: 1px solid var(--mae-line);
  border-radius: 0;
  padding: .8rem 0 1rem;
  min-height: 92px;
  box-shadow: none;
}
[data-testid="stMetricLabel"] p {
  color: var(--mae-muted) !important;
  -webkit-text-fill-color: var(--mae-muted) !important;
  font-size: .68rem;
  font-weight: 700;
  letter-spacing: .08em;
  text-transform: uppercase;
  line-height: 1.15;
}
div[data-testid="stMetricValue"] {
  color: var(--mae-ink) !important;
  -webkit-text-fill-color: var(--mae-ink) !important;
  font-size: 2.1rem;
  letter-spacing: -.04em;
}
[data-testid="stMain"] label p,
[data-testid="stMain"] [data-testid="stWidgetLabel"] p,
[data-testid="stMain"] [data-testid="stCaptionContainer"] p {
  color: var(--mae-ink) !important;
  -webkit-text-fill-color: var(--mae-ink) !important;
}
[data-testid="stMain"] [data-baseweb="select"] > div,
[data-testid="stSidebar"] [data-baseweb="select"] > div,
[data-testid="stMain"] [data-testid="stSelectbox"] [role="group"],
[data-testid="stSidebar"] [data-testid="stSelectbox"] [role="group"],
[data-testid="stMain"] [data-baseweb="input"] > div,
[data-testid="stSidebar"] [data-baseweb="input"] > div,
[data-testid="stMain"] [data-baseweb="textarea"],
[data-testid="stSidebar"] [data-baseweb="textarea"] {
  background: var(--mae-surface) !important;
  border-color: var(--mae-line) !important;
}
[data-testid="stMain"] [data-baseweb="select"] *,
[data-testid="stSidebar"] [data-baseweb="select"] *,
[data-testid="stMain"] input,
[data-testid="stSidebar"] input,
[data-testid="stMain"] textarea,
[data-testid="stSidebar"] textarea {
  color: var(--mae-ink) !important;
  -webkit-text-fill-color: var(--mae-ink) !important;
  caret-color: var(--mae-ink) !important;
}
[data-baseweb="popover"] [role="listbox"] {
  background: var(--mae-surface) !important;
  color: var(--mae-ink) !important;
}
[data-baseweb="popover"] [role="option"] {
  color: var(--mae-ink) !important;
  -webkit-text-fill-color: var(--mae-ink) !important;
}
div[data-testid="stExpander"], div[data-testid="stForm"] {
  background: transparent;
  border-color: var(--mae-line);
  border-radius: 0;
}
div[data-testid="stAlert"] {
  border-radius: 0;
}
div[data-testid="stDataFrame"], [data-testid="stTable"] {
  max-width: 100%;
  overflow-x: auto;
}
a:focus-visible,
button:focus-visible,
input:focus-visible,
textarea:focus-visible,
[role="button"]:focus-visible,
[role="combobox"]:focus-visible,
summary:focus-visible,
[tabindex]:focus-visible {
  outline: 3px solid #0B66C3 !important;
  outline-offset: 3px !important;
  box-shadow: none !important;
}
[data-testid="stSidebar"] a:focus-visible,
[data-testid="stSidebar"] button:focus-visible,
[data-testid="stSidebar"] [role="button"]:focus-visible {
  outline-color: var(--mae-accent) !important;
}
.mae-quarantine-banner {
  display:flex;
  align-items:flex-start;
  justify-content:space-between;
  gap:1.25rem;
  background:#8F2722;
  color:#FFFFFF;
  padding:.9rem 1rem;
  border:2px solid #691A17;
  margin:0 0 1.25rem;
  overflow-wrap:anywhere;
}
.mae-quarantine-banner strong { color:#FFFFFF; font-size:1rem; line-height:1.25; }
.mae-quarantine-banner span { color:#FFE9E6; font-size:.72rem; line-height:1.45; text-align:right; }
.mae-masthead { display:flex; align-items:flex-end; justify-content:space-between; gap:2rem; border-bottom:1px solid var(--mae-ink); padding:.2rem 0 1.05rem; }
.mae-masthead__brand { font-size:.76rem; font-weight:800; letter-spacing:.16em; text-transform:uppercase; }
.mae-masthead__brand strong { display:block; font-size:1.2rem; letter-spacing:-.02em; text-transform:none; margin-top:.22rem; }
.mae-masthead__meta { display:flex; gap:2rem; text-align:right; color:var(--mae-muted); font-size:.72rem; line-height:1.35; }
.mae-masthead__meta b { display:block; color:var(--mae-ink); font-size:.78rem; font-weight:650; }
.mae-sidebar-brand { color:#fff; font-size:2.5rem; font-weight:800; letter-spacing:-.08em; line-height:1; margin:.2rem 0 1rem; }
.mae-sidebar-snapshot { border-top:1px solid #3A3A3A; border-bottom:1px solid #3A3A3A; padding:.8rem 0; margin:.4rem 0 1rem; color:#AFAFAA; font-size:.64rem; letter-spacing:.12em; text-transform:uppercase; line-height:1.6; }
.mae-sidebar-snapshot strong { display:block; color:#fff; font-size:.92rem; letter-spacing:.04em; }
.mae-current-dot { display:inline-block; width:.45rem; height:.45rem; border-radius:50%; background:var(--mae-accent); margin-right:.4rem; }
.mae-quarantine-dot { display:inline-block; width:.45rem; height:.45rem; border-radius:50%; background:#F2776F; margin-right:.4rem; }
.mae-deck { max-width:1020px; font-size:clamp(1.05rem,1.6vw,1.45rem); line-height:1.35; letter-spacing:0; margin:0 0 2rem; color:#2B2B2B; }
.mae-footer-disclosure { border-top:1px solid var(--mae-line); color:var(--mae-muted); font-size:.78rem; line-height:1.45; margin:3rem 0 0; padding:1rem 0 0; }
.mae-editorial-copy { max-width:930px; columns:2; column-gap:4rem; font-size:1.02rem; line-height:1.68; }
.mae-editorial-copy p { margin:0 0 1.1rem; break-inside:avoid; }
.mae-section-kicker { display:block; color:var(--mae-muted); font-size:.66rem; font-weight:800; letter-spacing:.13em; text-transform:uppercase; margin-bottom:.55rem; }
.mae-ranked-list { border-top:1px solid var(--mae-ink); }
.mae-ranked-item { display:grid; grid-template-columns:58px minmax(210px,.75fr) 74px minmax(280px,1.4fr); gap:1rem; align-items:baseline; padding:1.05rem 0; border-bottom:1px solid var(--mae-line); }
.mae-ranked-item__num { color:var(--mae-muted); font-size:.7rem; }
.mae-ranked-item__name { font-size:1.1rem; font-weight:700; letter-spacing:-.02em; }
.mae-ranked-item__score { font-size:1.45rem; font-weight:750; font-variant-numeric:tabular-nums; }
.mae-ranked-item__why { color:#404040; line-height:1.45; }
.mae-topic-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); border-top:1px solid var(--mae-ink); border-bottom:1px solid var(--mae-ink); }
.mae-topic { padding:1.35rem 1.4rem 1.5rem 0; min-height:190px; }
.mae-topic + .mae-topic { border-left:1px solid var(--mae-line); padding-left:1.4rem; }
.mae-topic__num { color:var(--mae-muted); font-size:.66rem; letter-spacing:.1em; }
.mae-topic h3 { font-size:1.35rem; margin:.55rem 0 .8rem; }
.mae-topic p { color:#404040; line-height:1.5; margin:0; }
.mae-dark-story { background:var(--mae-dark); color:#F5F5F0; padding:clamp(1.5rem,3vw,3rem); margin-top:1.5rem; }
.mae-dark-story .mae-section-kicker { color:var(--mae-accent); }
.mae-dark-story h3 { color:white; font-size:clamp(1.6rem,3vw,2.8rem); margin:.4rem 0 1.2rem; }
.mae-dark-story p { color:#D8D8D2; line-height:1.55; }
.mae-story-grid { display:grid; grid-template-columns:1.15fr 1fr 1fr; gap:2rem; }
.mae-story-grid b { display:block; color:white; font-size:.69rem; letter-spacing:.1em; text-transform:uppercase; margin-bottom:.45rem; }
.mae-statline { display:flex; flex-wrap:wrap; gap:1.4rem; border-top:1px solid var(--mae-ink); border-bottom:1px solid var(--mae-line); padding:.85rem 0; margin:1.4rem 0 2rem; color:var(--mae-muted); font-size:.78rem; }
.mae-statline strong { color:var(--mae-ink); font-size:1rem; margin-right:.25rem; }
.mae-heatmap-wrap { overflow-x:auto; width:100%; margin:1rem 0 2.5rem; background:transparent; border-top:1px solid var(--mae-ink); border-bottom:1px solid var(--mae-ink); }
.mae-heatmap { border-collapse:separate; border-spacing:3px; width:100%; min-width:1040px; table-layout:fixed; padding:.35rem 0; }
.mae-heatmap th { position:sticky; top:0; z-index:2; background:var(--mae-bg); color:var(--mae-muted); padding:.7rem .4rem; font-size:.65rem; letter-spacing:.05em; text-transform:uppercase; }
.mae-heatmap th:first-child { left:0; z-index:3; width:270px; text-align:left; }
.mae-heatmap td { text-align:center; padding:.58rem .3rem; font-weight:750; font-size:.88rem; font-variant-numeric:tabular-nums; }
.mae-heatmap td:first-child { position:sticky; left:0; z-index:1; text-align:left; background:var(--mae-bg); color:var(--mae-ink); font-weight:650; width:270px; font-size:.78rem; }
.mae-score-neg-3 { background:#7E4B45; color:white; } .mae-score-neg-2 { background:#A96860; color:white; }
.mae-score-neg-1 { background:#D8B5AF; color:#3E2522; } .mae-score-0 { background:#D8D8D2; color:#363633; }
.mae-score-1 { background:#C8D5C3; color:#263127; } .mae-score-2 { background:#94B493; color:#172619; }
.mae-score-3 { background:#5E805F; color:white; } .mae-score-na { background:#E8E8E3; color:#A0A09A; }
.mae-score-new { background:#CFDBD8; color:#263A36; }
.mae-score-insufficient { background:#E2DED4; color:#545149; }
.mae-score-rejected { background:#B7766E; color:white; }
.mae-score-carry { background:#E5CE7B; color:#372F12; }
.mae-cell-hero { display:flex; justify-content:space-between; align-items:flex-end; border-top:1px solid var(--mae-ink); padding:1.2rem 0 1.4rem; gap:2rem; }
.mae-cell-hero h3 { font-size:clamp(2rem,4vw,4.2rem); letter-spacing:-.055em; margin:.2rem 0 0; text-transform:uppercase; }
.mae-cell-score { text-align:right; font-size:2.8rem; font-weight:760; line-height:1; }
.mae-cell-score span { display:block; color:var(--mae-muted); font-size:.67rem; letter-spacing:.1em; text-transform:uppercase; margin-top:.45rem; }
.mae-cell-limitation { max-width:760px; color:var(--mae-muted); line-height:1.45; overflow-wrap:anywhere; }
.mae-audit-critical { min-width:0; max-width:100%; overflow-wrap:anywhere; }
.mae-audit-empty { border-top:4px solid #8F2722; background:white; padding:1.25rem; margin:1rem 0; }
.mae-audit-empty span { display:block; color:var(--mae-muted); font-size:.66rem; font-weight:800; letter-spacing:.1em; text-transform:uppercase; }
.mae-audit-empty strong { display:block; font-size:1.6rem; margin:.35rem 0 .6rem; }
.mae-audit-empty p { line-height:1.5; margin:.2rem 0 .8rem; }
.mae-audit-empty small { color:var(--mae-muted); }
.mae-story-columns { display:grid; grid-template-columns:1.25fr 1fr; gap:4rem; border-top:1px solid var(--mae-line); padding-top:1.5rem; }
.mae-story-block { margin-bottom:2rem; }
.mae-story-block h4 { font-size:.74rem; text-transform:uppercase; letter-spacing:.1em; margin:0 0 .65rem; }
.mae-story-block p, .mae-story-block li { line-height:1.55; color:#393939; }
.mae-story-block ul { margin:.4rem 0 0 1.15rem; padding:0; }
.mae-source-card { display:grid; grid-template-columns:110px minmax(0,1fr) auto; gap:1.2rem; align-items:start; padding:1rem 0; border-top:1px solid var(--mae-line); }
.mae-source-card__issuer { font-size:.68rem; font-weight:800; letter-spacing:.08em; text-transform:uppercase; }
.mae-source-card__title { font-weight:700; margin-bottom:.25rem; }
.mae-source-card__desc { color:var(--mae-muted); font-size:.83rem; line-height:1.4; }
.mae-source-card a { color:var(--mae-ink); text-underline-offset:3px; white-space:nowrap; }
.mae-change-group { border-top:1px solid var(--mae-ink); }
.mae-change-row { display:grid; grid-template-columns:minmax(220px,.7fr) 110px minmax(300px,1.4fr); gap:1.4rem; padding:1rem 0; border-bottom:1px solid var(--mae-line); align-items:start; }
.mae-change-row__name { font-weight:700; }
.mae-change-row__type { color:var(--mae-muted); font-size:.68rem; text-transform:uppercase; letter-spacing:.07em; }
.mae-change-row__text { line-height:1.45; color:#3E3E3E; }
.mae-scenario-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:1rem; margin-top:1rem; }
.mae-scenario-card { background:white; border-top:5px solid var(--mae-ink); padding:1.35rem; min-height:620px; display:flex; flex-direction:column; }
.mae-scenario-card--base { background:var(--mae-dark); color:#F4F4F1; border-top-color:var(--mae-accent); }
.mae-scenario-card--base h3, .mae-scenario-card--base h4 { color:white; }
.mae-scenario-card__meta { display:flex; justify-content:space-between; gap:.6rem; color:var(--mae-muted); font-size:.65rem; letter-spacing:.08em; text-transform:uppercase; }
.mae-scenario-card--base .mae-scenario-card__meta { color:#BDBDB7; }
.mae-scenario-card h3 { font-size:2rem; margin:1.2rem 0 .8rem; }
.mae-scenario-card p { line-height:1.47; }
.mae-scenario-card h4 { font-size:.66rem; letter-spacing:.08em; text-transform:uppercase; margin:1rem 0 .35rem; }
.mae-scenario-card ul { margin:.3rem 0 0 1.05rem; padding:0; font-size:.84rem; line-height:1.45; }
.mae-chain { display:flex; align-items:stretch; gap:.35rem; overflow-x:auto; padding:.6rem 0 1rem; }
.mae-chain__step { min-width:170px; flex:1; background:white; border-top:2px solid var(--mae-ink); padding:.75rem .55rem; font-size:.79rem; line-height:1.35; }
.mae-chain__step b { display:block; margin-bottom:.3rem; font-size:.62rem; letter-spacing:.08em; text-transform:uppercase; }
.mae-chain__arrow { align-self:center; color:var(--mae-muted); font-size:1.2rem; }
.mae-regime-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); border-top:1px solid var(--mae-ink); }
.mae-regime-factor { padding:1.15rem 1rem 1.3rem 0; border-bottom:1px solid var(--mae-line); }
.mae-regime-factor:nth-child(3n+2), .mae-regime-factor:nth-child(3n+3) { border-left:1px solid var(--mae-line); padding-left:1rem; }
.mae-regime-factor span { color:var(--mae-muted); font-size:.64rem; letter-spacing:.08em; text-transform:uppercase; }
.mae-regime-factor strong { display:block; font-size:1.05rem; margin-top:.35rem; }
.mae-analog-card { display:grid; grid-template-columns:55px 125px 100px minmax(260px,1fr); gap:1rem; padding:1.2rem 0; border-top:1px solid var(--mae-line); align-items:start; }
.mae-analog-card__rank { color:var(--mae-muted); font-size:.68rem; }
.mae-analog-card__date { font-size:1.1rem; font-weight:750; }
.mae-analog-card__score { font-size:1.55rem; font-weight:750; }
.mae-analog-card__score span { display:block; color:var(--mae-muted); font-size:.59rem; letter-spacing:.08em; text-transform:uppercase; }
.mae-analog-card__story { line-height:1.45; }
.mae-analog-card__diff { color:var(--mae-muted); font-size:.8rem; margin-top:.4rem; }
.mae-analog-metrics { color:var(--mae-ink); font-size:.82rem; margin-top:.35rem; overflow-wrap:anywhere; }
.mae-recent-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:1px; background:var(--mae-line); border:1px solid var(--mae-line); }
.mae-recent-card { background:var(--mae-bg); padding:1rem; min-width:0; }
.mae-recent-card h4 { margin:0 0 .75rem; font-size:1.05rem; }
.mae-recent-card__metrics { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:.5rem; }
.mae-recent-card__metrics span { color:var(--mae-muted); font-size:.58rem; letter-spacing:.05em; text-transform:uppercase; min-width:0; }
.mae-recent-card__metrics strong { display:block; color:var(--mae-ink); font-size:.82rem; margin-top:.2rem; overflow-wrap:anywhere; }
.mae-recent-card p { color:var(--mae-muted); font-size:.78rem; line-height:1.45; margin:.8rem 0 0; overflow-wrap:break-word; }
.mae-simple-table { width:100%; border-collapse:collapse; border-top:1px solid var(--mae-ink); }
.mae-simple-table th { color:var(--mae-muted); font-size:.64rem; text-transform:uppercase; letter-spacing:.06em; text-align:left; padding:.75rem .6rem; border-bottom:1px solid var(--mae-line); }
.mae-simple-table td { padding:.85rem .6rem; border-bottom:1px solid var(--mae-line); white-space:normal; overflow-wrap:anywhere; }
.mae-method-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:1rem; }
.mae-method-item { border-top:1px solid var(--mae-line); padding:.75rem 0; overflow-wrap:anywhere; }
.mae-method-item span { display:block; color:var(--mae-muted); font-size:.65rem; text-transform:uppercase; letter-spacing:.06em; }
.mae-method-item strong { display:block; margin-top:.3rem; font-size:.86rem; white-space:normal; }
.mae-download-grid [data-testid="stDownloadButton"] button { min-height:3.6rem; border-radius:0; border:1px solid var(--mae-ink); background:transparent; color:var(--mae-ink); white-space:normal; height:auto; }
.mae-download-grid [data-testid="stDownloadButton"] button:hover { background:var(--mae-dark); color:white; }
.mae-download-grid [data-testid="stDownloadButton"] button p { white-space:normal; }
@media (max-width: 1280px) {
  [data-testid="stSidebar"] > div:first-child { width:260px; }
  .mae-ranked-item { grid-template-columns:42px minmax(180px,.8fr) 65px minmax(240px,1.2fr); }
  .mae-scenario-card { min-height:680px; }
}
@media (max-width: 800px) {
  h1 { font-size:3.2rem; }
  .mae-masthead { align-items:flex-start; flex-direction:column; }
  .mae-masthead__meta { text-align:left; }
  .mae-quarantine-banner { flex-direction:column; }
  .mae-quarantine-banner span { text-align:left; }
  .mae-cell-hero { align-items:flex-start; flex-direction:column; gap:.8rem; }
  .mae-cell-score { text-align:left; font-size:2rem; }
  .mae-editorial-copy { columns:1; }
  .mae-ranked-item, .mae-change-row, .mae-analog-card { grid-template-columns:1fr; gap:.35rem; }
  .mae-topic-grid, .mae-story-grid, .mae-story-columns, .mae-scenario-grid, .mae-regime-grid, .mae-method-grid { grid-template-columns:1fr; }
  .mae-recent-grid { grid-template-columns:1fr; }
  .mae-topic + .mae-topic, .mae-regime-factor:nth-child(n) { border-left:0; padding-left:0; }
  .mae-chain { flex-direction:column; }
  .mae-chain__arrow { transform:rotate(90deg); }
  .mae-source-card { grid-template-columns:1fr; gap:.3rem; }
}
</style>
        """,
        unsafe_allow_html=True,
    )
