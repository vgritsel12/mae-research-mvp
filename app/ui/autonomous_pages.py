from __future__ import annotations

import html
import logging
import re
import traceback
import time
from datetime import date, datetime
from typing import Any

import pandas as pd
import streamlit as st

from app.config import Settings
from app.services.autonomous_operations import run_autonomous_update
from app.services.full_snapshot_loader import (
    ASSET_ORDER,
    GEOGRAPHY_ORDER,
    FullSnapshotData,
    get_cell,
    matrix_dimensions,
)
from app.ui import design
from app.ui.helpers import can_edit, render_access_box, render_interactive_table


logger = logging.getLogger(__name__)

QUIET_DISCLOSURE = (
    "Аналитика сформирована автоматически на основе открытых рыночных и институциональных источников. "
    "Материал не является индивидуальной инвестиционной рекомендацией."
)

MODULES = [
    "Strategy & Sentiment Map",
    "Change Tracker",
    "Scenario & Market Reaction Map",
    "Evidence & Expectation Check",
    "Transmission & Control Layer",
]

USER_SECTIONS = [
    "Рыночная картина",
    "Матрица MAE",
    "Что изменилось",
    "Сценарии",
    "Исторические аналоги",
    "Источники и отчёты",
]

SCENARIO_LABELS = {"BASE": "Базовый", "UPSIDE": "Позитивный", "DOWNSIDE": "Негативный"}
CONFIDENCE_LABELS = {"HIGH": "Высокая", "MEDIUM": "Средняя", "LOW": "Ограниченная"}
REGIME_VALUE_LABELS = {
    "Low inflation": "Низкая инфляция",
    "Moderate inflation": "Умеренная инфляция",
    "Sticky inflation": "Устойчивая повышенная инфляция",
    "Accommodative policy": "Мягкая денежно-кредитная политика",
    "Restrictive policy": "Жёсткая денежно-кредитная политика",
    "Neutral policy": "Нейтральная денежно-кредитная политика",
    "Negative real yields": "Отрицательные реальные доходности",
    "Positive real yields": "Положительные реальные доходности",
    "Elevated real yields": "Повышенные реальные доходности",
    "Tight credit spreads": "Узкие кредитные спреды",
    "Normal credit spreads": "Нормальные кредитные спреды",
    "Wide credit spreads": "Широкие кредитные спреды",
    "Stable breadth": "Стабильная ширина рынка",
    "Improving breadth": "Улучшающаяся ширина рынка",
    "Deteriorating breadth": "Ухудшающаяся ширина рынка",
    "Growth leadership": "Лидерство акций роста",
    "Balanced style leadership": "Сбалансированное лидерство стилей",
    "Value leadership": "Лидерство стоимостных акций",
    "Unavailable": "нет данных",
}
ANALOG_ASSET_LABELS = {
    "global_equities": "глобальные акции",
    "us_equities": "акции США",
    "growth": "акции роста",
    "value": "стоимостные акции",
    "small_cap": "акции малой капитализации",
    "short_treasuries": "краткосрочные государственные облигации США",
    "long_treasuries": "долгосрочные государственные облигации США",
    "high_yield": "высокодоходные корпоративные облигации",
    "inflation_linked": "инфляционно-защищённые облигации",
    "gold": "золото",
}
DIRECTION_LABELS = {
    "POSITIVE": "позитивный",
    "NEGATIVE": "негативный",
    "NEUTRAL": "нейтральный",
    "MIXED": "смешанный",
    "BULLISH": "позитивный",
    "BEARISH": "негативный",
}
REGIME_LABELS = {
    "inflation": "Инфляция",
    "inflation_regime": "Инфляция",
    "policy_stance": "Денежно-кредитная политика",
    "real_yield_regime": "Реальные доходности",
    "credit_spread_regime": "Кредитные спреды",
    "real_rates": "Реальные ставки",
    "credit_stress": "Кредитный стресс",
    "equity_breadth": "Ширина рынка акций",
    "growth_style_leadership": "Лидерство стилей",
    "style_momentum": "Факторный импульс",
    "usd_pressure": "Давление доллара",
    "regime_name": "Режим",
}
CONTROLLED_GLOSSARY = (
    (r"\bAI[- ]growth\b", "рост, связанный с AI"),
    (r"\bduration\b", "дюрация"),
    (r"\bsticky inflation\b", "устойчивая повышенная инфляция"),
    (r"\bhawkish caution\b", "жёсткая осторожность центробанков"),
    (r"\bbank buffers\b", "банковские капитальные буферы"),
    (r"\bfunding risk\b", "риск фондирования"),
    (r"\blow-for-long\b", "длительный период низких ставок"),
    (r"\bsupply shock\b", "шок предложения"),
    (r"\bsentiment\b", "рыночные настроения"),
)
RUSSIAN_ANALOG_LIMITATIONS = (
    "Историческое сходство является описательным ориентиром, а не прогнозом.",
    "Выбор аналогов основан на зарегистрированных макро- и рыночных факторах.",
    "Исторические аналоги не участвуют в расчёте оценок MAE.",
    "Периоды с одним отсутствующим фактором могут входить в основной рейтинг после перераспределения весов, применения штрафа за неполное покрытие и снижения уверенности. Синтетические значения не создаются.",
    "Если для горизонта 6 или 12 месяцев результат ещё недоступен, это явно указано в карточке периода.",
    "Структура рынка и реакция денежно-кредитной политики могут отличаться от прошлых периодов.",
)

UPDATE_PROGRESS_STAGES = (
    ("подготавливаю", 3, "Подготавливаю обновление"),
    ("получаю свежие публикации", 18, "Получаю свежие публикации"),
    ("проверяю доступные материалы", 38, "Проверяю доступные материалы"),
    ("отбираю", 52, "Отбираю релевантные источники"),
    ("формирую основные темы", 66, "Формирую основные рыночные темы"),
    ("формирую основные рыночные темы", 66, "Формирую основные рыночные темы"),
    ("пересчитываю матрицу", 80, "Пересчитываю матрицу"),
    ("проверяю результат", 92, "Проверяю результат"),
    ("готовлю отчёты", 97, "Готовлю отчёты"),
    ("публикую обновление", 99, "Публикую обновление"),
)

FORBIDDEN_PROGRESS_TOKENS = (
    "stage",
    "collect=",
    "validation schema",
    "source pack",
    "llm call",
    "candidate hash",
    "release pointer",
    "run id",
)


def render_header(bundle: FullSnapshotData) -> None:
    date_label = _format_date(bundle.snapshot_date)
    st.markdown(
        "<div class='mae-masthead'>"
        "<div class='mae-masthead__brand'>MAE<strong>Рыночная аналитика</strong>"
        f"<span>Горизонт 6–12 месяцев · обновлено {_e(date_label)}</span></div>"
        "</div>",
        unsafe_allow_html=True,
    )


def _render_market_view_context(bundle: FullSnapshotData, comparison: str) -> None:
    snapshot_date = _format_date(bundle.metadata.get("current_snapshot_date") or bundle.snapshot_date)
    st.markdown(
        "<div class='mae-market-context'>"
        f"Актуально на {_e(snapshot_date)} · инвестиционный горизонт 6–12 месяцев"
        "</div>",
        unsafe_allow_html=True,
    )
    if comparison == "INITIAL_BASELINE":
        st.caption("Текущий выпуск является исходной точкой для будущих ежемесячных сравнений.")
    elif comparison == "MONTHLY_MARKET_CHANGE":
        baseline = _format_date(bundle.metadata.get("baseline_snapshot_date") or bundle.validation.get("previous_snapshot_date"))
        st.caption(f"Изменения сравниваются с обзором от {baseline}.")


def _update_progress_state_for_message(message: str, *, current_percent: int = 0) -> dict[str, Any]:
    lowered = str(message or "").casefold()
    stage = "Проверяю результат"
    target = 92
    for token, percent, label in UPDATE_PROGRESS_STAGES:
        if token in lowered:
            target = percent
            stage = label
            break
    percent = min(99, max(int(current_percent or 0), int(target)))
    return {
        "state": "RUNNING",
        "percent": percent,
        "title": f"Обновляю аналитику — {percent}%",
        "stage": stage,
        "detail": "Обычно обновление занимает несколько минут.",
    }


def _final_update_progress_state(result: dict[str, Any], current_percent: int = 0) -> dict[str, Any]:
    status = str(result.get("status") or "").upper()
    success = bool(result.get("success"))
    errors = " ".join(str(item) for item in result.get("errors") or [])
    if status == "UP_TO_DATE":
        return {
            "state": "SUCCESS",
            "percent": 100,
            "title": "Аналитика актуальна. Значимых изменений не обнаружено.",
            "stage": "Готово",
            "detail": "Последняя подтверждённая рыночная картина остаётся активной.",
        }
    if success and ("SOURCE" in errors or "COLLECTION" in errors):
        return {
            "state": "PARTIAL_SUCCESS",
            "percent": 100,
            "title": "Обновление завершено. Не все источники были доступны.",
            "stage": "Использованы доступные материалы",
            "detail": "Использованы доступные материалы и последняя подтверждённая база.",
        }
    if success:
        return {
            "state": "SUCCESS",
            "percent": 100,
            "title": "Аналитика обновлена.",
            "stage": "Готово",
            "detail": "Отчёты и опубликованная рыночная картина подготовлены.",
        }
    return {
        "state": "FAILED",
        "percent": min(99, max(int(current_percent or 0), 96)),
        "title": "Не удалось завершить обновление.",
        "stage": "Показана последняя подтверждённая рыночная картина",
        "detail": "Предыдущая опубликованная версия остаётся активной.",
    }


def _render_update_progress(slot: Any, state: dict[str, Any]) -> None:
    percent = int(state.get("percent") or 0)
    status = str(state.get("state") or "RUNNING").lower()
    title = _sanitize_progress_text(state.get("title") or f"Обновляю аналитику — {percent}%")
    stage = _sanitize_progress_text(state.get("stage") or "Проверяю результат")
    detail = _sanitize_progress_text(state.get("detail") or "Обычно обновление занимает несколько минут.")
    with slot.container():
        st.markdown(
            "<div class='mae-update-progress' role='status' aria-live='polite'>"
            f"<div class='mae-update-progress__title mae-update-progress__title--{_e(status)}'>{_e(title)}</div>"
            f"<div class='mae-update-progress__percent'>{percent}%</div>"
            "</div>",
            unsafe_allow_html=True,
        )
        st.progress(percent)
        st.markdown(f"<div class='mae-update-progress__step'>{_e(stage)}</div>", unsafe_allow_html=True)
        st.caption(detail)


def _sanitize_progress_text(value: Any) -> str:
    text = str(value or "")
    lowered = text.casefold()
    for token in FORBIDDEN_PROGRESS_TOKENS:
        if token in lowered:
            return "Проверяю результат"
    return text


def render_sidebar(bundle: FullSnapshotData, settings: Settings) -> None:
    render_access_box(settings)
    st.sidebar.markdown(
        "<div class='mae-sidebar-snapshot'>Актуальная рыночная картина"
        f"<strong>{_e(_format_date(bundle.snapshot_date))}</strong>"
        "<span></span>Горизонт 6–12 месяцев</div>",
        unsafe_allow_html=True,
    )
    if settings.public_demo:
        st.sidebar.caption("Публичный просмотр · только чтение")
    update_running = bool(st.session_state.get("autonomous_update_running"))
    run_clicked = st.sidebar.button(
        "Обновление выполняется" if update_running else "Обновить аналитику",
        disabled=not can_edit(settings) or update_running,
        width="stretch",
        key="autonomous_run_update",
    )
    if run_clicked:
        st.session_state["autonomous_update_running"] = True
        progress_slot = st.sidebar.empty()
        progress_state = {"percent": 0, "started_at": time.monotonic()}

        def progress(message: str) -> None:
            state = _update_progress_state_for_message(message, current_percent=progress_state["percent"])
            progress_state["percent"] = int(state["percent"])
            _render_update_progress(progress_slot, state)

        progress("Подготавливаю обновление")
        try:
            result = run_autonomous_update(
                as_of=date.today(),
                settings=settings,
                collect=True,
                publish=True,
                build_xlsx=True,
                progress=progress,
            )
        except Exception:  # noqa: BLE001 - safe UI message
            logger.exception("Autonomous update failed before a product result was available")
            result_dict = {
                "success": False,
                "status": "FAILED_UPDATE",
                "errors": ("UNEXPECTED_UPDATE_FAILURE",),
            }
            _render_update_progress(progress_slot, _final_update_progress_state(result_dict, progress_state["percent"]))
            st.sidebar.button("Попробовать ещё раз", key="autonomous_retry_update", disabled=not can_edit(settings))
        else:
            result_dict = result.to_dict()
            st.session_state["autonomous_operation_result"] = result_dict
            final_state = _final_update_progress_state(result_dict, progress_state["percent"])
            _render_update_progress(progress_slot, final_state)
            if result.status == "UP_TO_DATE":
                st.sidebar.info("Аналитика актуальна. Значимых изменений не обнаружено.")
            elif result.success:
                st.cache_data.clear()
                st.sidebar.success("Аналитика обновлена.")
            else:
                st.sidebar.warning(_update_result_message(result_dict))
                st.sidebar.button("Попробовать ещё раз", key="autonomous_retry_update", disabled=not can_edit(settings))
        finally:
            st.session_state["autonomous_update_running"] = False
    with st.sidebar.expander("О продукте", expanded=False):
        st.write("MAE показывает относительную привлекательность активов на горизонте 6–12 месяцев.")
        st.write(
            "Построение индивидуальных клиентских портфелей и распределение весов по профилям риска "
            "не входят в текущую версию продукта."
        )
        st.write("Матрица содержит 19 классов/сегментов активов и 6 регионов.")
        st.caption("Детальные проверочные файлы доступны в экспортируемых отчётах.")


def render_page_safely(name: str, render_func: Any, bundle: FullSnapshotData, settings: Settings) -> None:
    try:
        render_func(bundle, settings)
    except Exception:  # noqa: BLE001
        logger.exception("Autonomous product page failed: %s", name)
        st.session_state["last_page_traceback"] = traceback.format_exc()
        st.error("Раздел временно недоступен. Опубликованная версия данных не изменялась.")
    finally:
        _render_footer()


def market_view_page(bundle: FullSnapshotData, _settings: Settings) -> None:
    st.title("Рыночная картина")
    if not _require_bundle(bundle):
        return
    themes = _market_themes(bundle)
    sources = _source_lookup(bundle)
    comparison = _comparison_type(bundle)

    st.markdown(
        "<div class='mae-deck'>Профессиональная сводка по относительной привлекательности классов активов "
        "на горизонте 6–12 месяцев.</div>",
        unsafe_allow_html=True,
    )
    _render_market_view_context(bundle, comparison)

    st.header("Главный вывод")
    st.write(_main_conclusion(bundle, themes))
    _theme_source_summary(themes[:3], sources)

    applicable_rows = bundle.applicable_scores.copy()
    applicable_rows["numeric_score"] = pd.to_numeric(applicable_rows["score"], errors="coerce")
    left, right = st.columns(2)
    with left:
        st.header("Наиболее привлекательные направления")
        _ranking(applicable_rows.sort_values(["numeric_score", "conviction"], ascending=[False, True]).head(6), bundle)
    with right:
        st.header("Наиболее уязвимые направления")
        _ranking(applicable_rows.sort_values(["numeric_score", "conviction"], ascending=[True, True]).head(6), bundle)

    st.header("Что сейчас определяет рынок")
    for theme in themes[:8]:
        st.markdown(
            f"**{_et(theme.get('title'))}**  \n"
            f"{_e(_direction(theme.get('direction')))} · {_et(theme.get('implication_6_12m'))}"
        )
        _source_links(theme.get("supporting_source_ids") or [], sources)

    st.header("Главные риски")
    for theme in themes[:6]:
        risk = theme.get("invalidation_condition")
        if risk:
            st.markdown(f"- {_et(risk)}")

    st.header("За чем следить дальше")
    for scenario in _ordered_scenarios(bundle.scenarios):
        label = _scenario_label(scenario.get("scenario_id"))
        st.markdown(f"**{label}:** {_et(scenario.get('trigger') or scenario.get('narrative') or '—')}")


def market_map_page(bundle: FullSnapshotData, _settings: Settings) -> None:
    st.title("Матрица MAE")
    if not _require_bundle(bundle):
        return
    st.markdown(
        "<div class='mae-deck'>Оценка относительной привлекательности классов активов на горизонте 6–12 месяцев.</div>",
        unsafe_allow_html=True,
    )
    st.markdown(heatmap_html(bundle.scores), unsafe_allow_html=True)
    st.caption("Шкала: -3 — выраженно негативно, +3 — выраженно позитивно. Прочерк означает, что оценка не применяется.")

    st.header("Карточка позиции")
    cell = _cell_selector(bundle)
    if cell:
        _cell_story(bundle, cell)


def what_changed_page(bundle: FullSnapshotData, _settings: Settings) -> None:
    st.title("Что изменилось")
    if not _require_bundle(bundle):
        return
    st.markdown(_comparison_intro(bundle))
    scored = bundle.applicable_scores.copy()
    scored["numeric_delta"] = pd.to_numeric(scored["score_delta"], errors="coerce").fillna(0)
    if _comparison_type(bundle) in {"SAME_DAY_REVISION", "INITIAL_BASELINE"}:
        scored["numeric_delta"] = 0
    changed = scored[scored["numeric_delta"].ne(0)].sort_values("numeric_delta", key=lambda s: s.abs(), ascending=False)
    unchanged = scored[scored["numeric_delta"].eq(0)]
    upgrades = changed[changed["numeric_delta"].gt(0)]
    downgrades = changed[changed["numeric_delta"].lt(0)]
    metrics = st.columns(3)
    metrics[0].metric("Повышения", len(upgrades))
    metrics[1].metric("Снижения", len(downgrades))
    metrics[2].metric("Без изменения", len(unchanged))
    _change_section("А. Изменения оценок", changed, "Оценки не изменились. Обновлены источники и формулировки аналитики.", bundle)
    st.header("Б. Новые или изменившиеся рыночные темы")
    for theme in _market_themes(bundle)[:6]:
        st.markdown(f"**{_et(theme.get('title'))}**  \n{_et(theme.get('implication_6_12m') or '')}")
    st.header("В. Обновления источников без изменения оценки")
    _change_section("Позиции без изменения оценки", unchanged, "Нет неизменившихся оцениваемых позиций.", bundle, limit=10)


def scenarios_page(bundle: FullSnapshotData, _settings: Settings) -> None:
    st.title("Сценарии")
    if not _require_bundle(bundle):
        return
    st.markdown(
        "<div class='mae-deck'>Три условных сценария построены из того же набора рыночных тем, что и матрица MAE.</div>",
        unsafe_allow_html=True,
    )
    st.caption("Вероятности являются ориентировочными диапазонами и могут не складываться ровно в 100%.")
    for scenario in _ordered_scenarios(bundle.scenarios):
        label = _scenario_label(scenario.get("scenario_id"))
        st.subheader(label)
        winners, losers = _scenario_winners_losers(bundle, scenario)
        st.markdown(f"**Суть сценария:** {_et(scenario.get('narrative') or '—')}")
        st.markdown(f"**Вероятность:** {_e(_probability(scenario.get('probability_band')))}")
        st.markdown(f"**Причинная цепочка:** {_et(scenario.get('causal_chain') or '—')}")
        st.markdown(f"**Условия реализации:** {_et(scenario.get('trigger') or '—')}")
        st.markdown(f"**Что отменит сценарий:** {_et(scenario.get('veto') or '—')}")
        st.markdown(f"**Какие активы выиграют:** {_e(', '.join(winners) if winners else 'нет явного списка')}")
        st.markdown(f"**Какие активы пострадают:** {_e(', '.join(losers) if losers else 'нет явного списка')}")


def historical_analogs_page(bundle: FullSnapshotData, _settings: Settings) -> None:
    st.title("Исторические аналоги")
    analogs = bundle.analogs
    if analogs.get("status") != "PASS":
        st.warning("Проверенный набор исторических аналогов сейчас недоступен; периоды не подставляются искусственно.")
        return
    st.markdown(
        "<div class='mae-deck'>Текущая рыночная конфигурация больше всего напоминает следующие периоды.</div>",
        unsafe_allow_html=True,
    )
    st.caption("Историческое сходство является описательным ориентиром, а не прогнозом. Рыночная структура и реакция денежной политики могут отличаться.")
    st.header("Наиболее похожие исторические периоды")
    for analog in analogs.get("historical_analogs") or analogs.get("analogs") or []:
        outcomes = analog.get("forward_returns") or {}
        similarity = float(analog.get("display_similarity") or analog.get("adjusted_similarity") or analog.get("similarity_score") or 0)
        factor_coverage = float(analog.get("factor_coverage") or 0)
        st.markdown(
            f"**{_e(analog.get('period'))} · рыночный режим**  \n"
            f"Сходство: {similarity:.0f}%; покрытие факторов: {factor_coverage:.0%}; "
            f"доступность результатов: {_e(_outcome_availability(outcomes))}."
        )
        missing = _missing_factor_warning(analog)
        if missing:
            st.caption(missing)
        st.markdown(f"**Почему похож:** {_e(_ru_analog_text(analog.get('why_similar') or analog.get('description') or _regime_description(analog)))}")
        st.markdown(f"**Главное отличие:** {_e(_ru_analog_text(_analog_difference(analog)))}")
        st.markdown(f"**Результат через 3 месяца:** {_e(_horizon_outcome(outcomes, '3m'))}")
        st.markdown(f"**Результат через 6 месяцев:** {_e(_horizon_outcome(outcomes, '6m'))}")
        st.markdown(f"**Результат через 12 месяцев:** {_e(_horizon_outcome(outcomes, '12m'))}")
    recent = analogs.get("recent_regime_matches") or []
    if recent:
        st.header("Похожие недавние состояния рынка")
        for analog in recent[:5]:
            outcomes = analog.get("forward_returns") or {}
            similarity = float(analog.get("display_similarity") or analog.get("adjusted_similarity") or 0)
            factor_coverage = float(analog.get("factor_coverage") or 0)
            st.markdown(
                f"**{_e(analog.get('period'))} · текущая фаза режима**  \n"
                f"Сходство: {similarity:.0f}%; покрытие факторов: {factor_coverage:.0%}. "
                f"{_e(analog.get('recent_reason') or 'Период слишком недавний, поэтому результат ещё формируется.')}"
            )
            st.caption(_outcome_availability(outcomes))
    with st.expander("Методология и ограничения", expanded=False):
        st.write("Сходство рассчитывается по месячным данным с 2000 года до последнего завершённого месяца. Будущие доходности не используются при выборе аналогов.")
        for limitation in RUSSIAN_ANALOG_LIMITATIONS:
            st.markdown(f"- {_e(limitation)}")


def sources_downloads_page(bundle: FullSnapshotData, _settings: Settings) -> None:
    st.title("Источники и отчёты")
    if not _require_bundle(bundle):
        return
    st.markdown(
        "<div class='mae-deck'>Документы, использованные для текущей рыночной картины, и файлы для детальной проверки.</div>",
        unsafe_allow_html=True,
    )
    st.header("Источники")
    source_rows = _source_table(bundle)
    if source_rows.empty:
        st.info("В опубликованной версии нет отображаемых источников.")
    else:
        render_interactive_table(
            source_rows,
            key="theme_sources",
            filter_columns=("Провайдер", "Регион", "Класс активов", "Дата"),
            default_sort="Дата",
            max_cell_text=180,
        )
        for row in source_rows.to_dict("records"):
            st.markdown(
                f"**{_e(row['Провайдер'])} · [{_e(row['Документ'])}]({_e(row['URL'])})**  \n"
                f"{_e(row['Дата'])} · {_e(row['Регион'])} · {_e(row['Класс активов'])}  \n"
                f"{_et(row.get('Почему использован') or 'использован как часть текущего рыночного обзора')}"
            )

    st.header("Отчёты для скачивания")
    downloads = [
        ("Полный JSON-отчёт", "json_export", "application/json"),
        ("Матрица CSV", "csv_export", "text/csv"),
        ("Рабочая книга Excel", "xlsx_export", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("Отчёт по источникам", "source_report", "application/json"),
        ("Исторические аналоги", "historical_analog_export", "application/json"),
    ]
    columns = st.columns(2)
    for index, (label, key, mime) in enumerate(downloads):
        path = bundle.paths.get(key)
        with columns[index % 2]:
            if path and path.is_file():
                st.download_button(label, data=path.read_bytes(), file_name=path.name, mime=mime, key=key)
            else:
                st.button(label, disabled=True, key=f"missing_{key}")
    with st.expander("Техническая информация", expanded=False):
        for label, key, mime in [
            ("Манифест сверки", "export_manifest", "application/json"),
            ("Манифест отбора источников", "source_selection_manifest", "application/json"),
        ]:
            path = bundle.paths.get(key)
            if path and path.is_file():
                st.download_button(label, data=path.read_bytes(), file_name=path.name, mime=mime, key=f"tech_{key}")
            else:
                st.button(label, disabled=True, key=f"missing_tech_{key}")


def heatmap_html(scores: pd.DataFrame) -> str:
    parts = ["<div class='mae-heatmap-wrap'><table class='mae-heatmap'><thead><tr><th>Актив</th>"]
    parts.extend(f"<th>{_e(_region(region))}</th>" for region in GEOGRAPHY_ORDER)
    parts.append("</tr></thead><tbody>")
    for asset in ASSET_ORDER:
        parts.append(f"<tr><td>{_e(_asset(asset))}</td>")
        for region in GEOGRAPHY_ORDER:
            match = scores[scores["asset_segment"].eq(asset) & scores["geography"].eq(region)]
            if match.empty or str(match.iloc[0]["applicable"]).lower() != "true":
                parts.append("<td class='mae-score-na' title='Для этого сочетания актива и региона оценка не применяется.'>-</td>")
                continue
            row = match.iloc[0]
            score = int(row["score"])
            css = f"mae-score-neg-{abs(score)}" if score < 0 else f"mae-score-{score}"
            title = f"{_asset(row['asset_segment'])} · {_region(row['geography'])} · {_confidence(row['conviction'])}"
            parts.append(f"<td class='{css}' title='{_e(title)}'>{score:+d}</td>")
        parts.append("</tr>")
    parts.append("</tbody></table></div>")
    return "".join(parts)


def parse_reaction_map(value: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in str(value or "").split(";"):
        if ":" not in item:
            continue
        key, raw = item.rsplit(":", 1)
        try:
            result[key.strip()] = int(raw.strip().replace("+", ""))
        except ValueError:
            continue
    return result


def _require_bundle(bundle: FullSnapshotData) -> bool:
    if bundle.available and bundle.is_autonomous:
        return True
    st.error("Опубликованная версия рыночной картины временно недоступна.")
    for error in bundle.errors:
        st.caption(error)
    return False


def _ranking(rows: pd.DataFrame, bundle: FullSnapshotData) -> None:
    sources = _source_lookup(bundle)
    for index, row in enumerate(rows.to_dict("records"), start=1):
        score = int(row.get("numeric_score") or 0)
        source_ids = _split_ids(row.get("supporting_source_ids"))
        st.markdown(
            f"**{index:02d}. {_asset(row.get('asset_segment'))} · {_region(row.get('geography'))} ({score:+d})**  \n"
            f"{_et(row.get('thesis') or row.get('main_driver') or '')}"
        )
        _source_links(source_ids[:2], sources)


def _cell_selector(bundle: FullSnapshotData) -> dict[str, Any] | None:
    left, right = st.columns(2)
    with left:
        asset_labels = [_asset(asset) for asset in ASSET_ORDER]
        default_index = ASSET_ORDER.index("Growth") if "Growth" in ASSET_ORDER else 0
        selected_label = st.selectbox("Класс активов", asset_labels, index=default_index)
        asset = ASSET_ORDER[asset_labels.index(selected_label)]
    regions = bundle.scores[bundle.scores["asset_segment"].eq(asset)]["geography"].tolist()
    ordered = [region for region in GEOGRAPHY_ORDER if region in regions]
    with right:
        region_labels = [_region(region) for region in ordered]
        default_region = ordered.index("US") if "US" in ordered else 0
        selected_region = st.selectbox("Регион", region_labels, index=default_region)
        region = ordered[region_labels.index(selected_region)]
    return get_cell(bundle, asset, region)


def _cell_story(bundle: FullSnapshotData, cell: dict[str, Any]) -> None:
    if str(cell.get("applicable")).lower() != "true":
        st.info("Для этого сочетания актива и региона оценка не применяется.")
        return
    previous = int(cell["previous_score"])
    current = int(cell["score"])
    delta = int(cell["score_delta"])
    metrics = st.columns(4)
    metrics[0].metric("Оценка", f"{current:+d}")
    change_label = (
        "Изменение с последнего месячного обзора"
        if _comparison_type(bundle) == "MONTHLY_MARKET_CHANGE"
        else "Изменение в текущей версии"
    )
    metrics[1].metric(change_label, f"{delta:+d}")
    metrics[2].metric("Предыдущая", f"{previous:+d}")
    metrics[3].metric("Уверенность", _confidence(_effective_confidence(bundle, cell)))
    st.subheader(f"{_asset(cell.get('asset_segment'))} · {_region(cell.get('geography'))}")
    st.markdown(f"**Краткий инвестиционный вывод:** {_et(cell.get('thesis') or '—')}")
    reasons = _cell_theme_titles(bundle, cell)
    st.markdown(f"**Почему такая оценка:** {_et('; '.join(reasons) if reasons else cell.get('main_driver') or '—')}")
    st.markdown(f"**Основные риски:** {_et(cell.get('main_risk') or '—')}")
    st.markdown(f"**Что изменит взгляд:** {_et(cell.get('invalidation') or cell.get('veto') or '—')}")
    if _effective_confidence(bundle, cell) == "LOW":
        st.caption("Оценка основана на ограниченном или косвенном наборе данных.")
    evidence = _cell_sources(bundle, cell)
    st.markdown("**Источники:**")
    if evidence.empty:
        st.caption("Для этой позиции нет отображаемых источников в опубликованной версии.")
    for row in evidence.to_dict("records"):
        st.markdown(f"- [{_e(row.get('provider'))} · {_e(row.get('title'))}]({_e(row.get('URL'))})")


def _change_section(title: str, rows: pd.DataFrame, empty: str, bundle: FullSnapshotData, *, limit: int = 20) -> None:
    st.header(title)
    if rows.empty:
        st.caption(empty)
        return
    st.caption(f"Показано {min(len(rows), limit)} из {len(rows)} позиций.")
    sources = _source_lookup(bundle)
    for row in rows.head(limit).to_dict("records"):
        st.markdown(
            f"**{_asset(row.get('asset_segment'))} · {_region(row.get('geography'))}** — "
            f"{_score_text(row.get('previous_score'))} -> {_score_text(row.get('score'))}  \n"
            f"{_et(row.get('thesis') or '')}"
        )
        _source_links(_split_ids(row.get("supporting_source_ids"))[:2], sources)


def _ordered_scenarios(frame: pd.DataFrame) -> list[dict[str, Any]]:
    order = {"BASE": 0, "UPSIDE": 1, "DOWNSIDE": 2}
    return sorted(frame.to_dict("records"), key=lambda row: order.get(str(row.get("scenario_id")), 9))


def _market_themes(bundle: FullSnapshotData) -> list[dict[str, Any]]:
    themes = bundle.metadata.get("market_themes") or []
    return [theme for theme in themes if isinstance(theme, dict)]


def _cell_theme_titles(bundle: FullSnapshotData, cell: dict[str, Any]) -> list[str]:
    themes = {str(theme.get("theme_id")): str(theme.get("title") or "") for theme in _market_themes(bundle)}
    result: list[str] = []
    for theme_id in _split_ids(cell.get("supporting_theme_ids")):
        title = themes.get(theme_id)
        if title:
            result.append(title)
    return result


def _cell_sources(bundle: FullSnapshotData, cell: dict[str, Any]) -> pd.DataFrame:
    if bundle.evidence.empty:
        return pd.DataFrame()
    source_ids = set(_split_ids(cell.get("supporting_source_ids")))
    evidence = bundle.evidence[bundle.evidence.get("cell_id", pd.Series(dtype=str)).astype(str).eq(str(cell.get("cell_id")))]
    if source_ids and "source_id" in evidence:
        evidence = evidence[evidence["source_id"].astype(str).isin(source_ids)]
    return evidence.drop_duplicates(subset=["provider", "title", "URL"])


def _deduplicated_sources(evidence: pd.DataFrame) -> pd.DataFrame:
    if evidence.empty:
        return pd.DataFrame(columns=["provider", "title", "publication_date", "url"])
    rows = evidence.rename(columns={"URL": "url"})
    return rows[["provider", "title", "publication_date", "url"]].drop_duplicates()


def _source_lookup(bundle: FullSnapshotData) -> dict[str, dict[str, Any]]:
    sources: dict[str, dict[str, Any]] = {}
    for item in bundle.metadata.get("admitted_sources") or []:
        if isinstance(item, dict):
            sources[str(item.get("source_id"))] = item
    for item in bundle.metadata.get("theme_documents") or []:
        if isinstance(item, dict):
            current = sources.setdefault(str(item.get("source_id")), {})
            current.update({k: v for k, v in item.items() if v not in (None, "", [])})
    return sources


def _source_links(source_ids: list[str], sources: dict[str, dict[str, Any]]) -> None:
    readable = []
    for source_id in source_ids:
        source = sources.get(source_id)
        if not source:
            continue
        readable.append(f"[{_e(source.get('provider'))} · {_e(source.get('title'))}]({_e(source.get('url'))})")
    if readable:
        st.caption("Источники: " + " · ".join(readable))


def _theme_source_line(themes: list[dict[str, Any]], sources: dict[str, dict[str, Any]]) -> None:
    ids: list[str] = []
    for theme in themes:
        for source_id in theme.get("supporting_source_ids") or []:
            if source_id not in ids:
                ids.append(source_id)
    _source_links(ids[:4], sources)


def _theme_source_summary(themes: list[dict[str, Any]], sources: dict[str, dict[str, Any]]) -> None:
    ids: list[str] = []
    for theme in themes:
        for source_id in theme.get("supporting_source_ids") or []:
            if source_id not in ids and source_id in sources:
                ids.append(source_id)
    if not ids:
        return
    st.caption(f"Источники: {len(ids)} документов")
    with st.expander("Показать источники", expanded=False):
        for source_id in ids[:8]:
            source = sources[source_id]
            st.markdown(f"- [{_e(source.get('provider'))} · {_e(source.get('title'))}]({_e(source.get('url'))})")


def _source_table(bundle: FullSnapshotData) -> pd.DataFrame:
    rows: list[dict[str, str]] = []
    source_by_id = _source_lookup(bundle)
    for source_id, source in source_by_id.items():
        if not source_id or source_id == "None":
            continue
        regions = source.get("relevant_regions") or source.get("coverage_regions") or []
        assets = source.get("relevant_asset_classes") or source.get("coverage_asset_classes") or []
        rows.append(
            {
                "Провайдер": str(source.get("provider") or ""),
                "Документ": str(source.get("title") or ""),
                "Дата": str(source.get("date") or ""),
                "URL": str(source.get("url") or ""),
                "Регион": ", ".join(_region(item) for item in regions) if isinstance(regions, list) else str(regions),
                "Класс активов": ", ".join(_asset_class(item) for item in assets) if isinstance(assets, list) else str(assets),
                "Почему использован": _source_why_used(source),
            }
        )
    return pd.DataFrame(rows).sort_values(["Дата", "Провайдер"], ascending=[False, True]) if rows else pd.DataFrame()


def _scenario_winners_losers(bundle: FullSnapshotData, scenario: dict[str, Any]) -> tuple[list[str], list[str]]:
    ids = _extract_cell_ids(str(scenario.get("material_affected_cells") or ""))
    if not ids:
        return [], []
    score_by_id = {str(row.get("cell_id")): row for row in bundle.applicable_scores.to_dict("records")}
    winners: list[str] = []
    losers: list[str] = []
    downside = str(scenario.get("scenario_id")).upper() == "DOWNSIDE"
    for cell_id in ids[:12]:
        row = score_by_id.get(cell_id)
        if not row:
            continue
        label = f"{_asset(row.get('asset_segment'))} · {_region(row.get('geography'))}"
        score = int(row.get("score") or 0)
        target = losers if (score > 0 and downside) or (score < 0 and not downside) else winners
        if label not in target:
            target.append(label)
    return winners[:6], losers[:6]


def _extract_cell_ids(value: str) -> list[str]:
    ids = re.findall(r"GA:R\d{2}:[A-Z]+(?:\s+ex\s+China)?", value)
    return list(dict.fromkeys(ids))


def _main_conclusion(bundle: FullSnapshotData, themes: list[dict[str, Any]]) -> str:
    attractive = bundle.applicable_scores.copy()
    attractive["numeric_score"] = pd.to_numeric(attractive["score"], errors="coerce")
    best = attractive.sort_values("numeric_score", ascending=False).head(2).to_dict("records")
    weak = attractive.sort_values("numeric_score", ascending=True).head(2).to_dict("records")
    best_text = ", ".join(f"{_asset(row.get('asset_segment'))} ({_region(row.get('geography'))})" for row in best)
    weak_text = ", ".join(f"{_asset(row.get('asset_segment'))} ({_region(row.get('geography'))})" for row in weak)
    theme_text = _controlled_ru_text(themes[0].get("implication_6_12m")) if themes else "рынок остаётся неоднородным"
    return (
        f"Матрица указывает на выборочный риск-аппетит: наиболее сильные оценки сейчас у {best_text}, "
        f"а наиболее слабые — у {weak_text}. Ключевой фон: {theme_text}"
    )


def _effective_confidence(bundle: FullSnapshotData, cell: dict[str, Any]) -> str:
    confidence = str(cell.get("conviction") or "LOW").upper()
    sources = _cell_sources(bundle, cell)
    providers = {str(row.get("provider", "")).strip().casefold() for row in sources.to_dict("records") if row.get("provider")}
    if confidence == "HIGH" and len(providers) < 2:
        return "MEDIUM"
    if cell.get("geography") not in {"Global", "US", "Europe"} and len(providers) <= 1:
        return "LOW" if confidence == "HIGH" else confidence
    return confidence


def _split_ids(value: Any) -> list[str]:
    if isinstance(value, list):
        raw = value
    else:
        raw = str(value or "").split(";")
    return [str(item).strip() for item in raw if str(item).strip()]


def _asset(value: Any) -> str:
    text = str(value or "")
    return _canonical_financial_label(text)


def _asset_class(value: Any) -> str:
    text = str(value or "")
    return _canonical_financial_label(text)


def _region(value: Any) -> str:
    text = str(value or "")
    return {"EM ex Ch": "EM ex China"}.get(text, text)


def _canonical_financial_label(text: str) -> str:
    return {
        "EQUITY": "Equities",
        "Equity": "Equities",
        "FIXED INCOME": "Government Bonds",
        "GOLD": "Gold",
        "COMMODITIES": "Commodities",
    }.get(text, text)


def _direction(value: Any) -> str:
    text = str(value or "").upper()
    return DIRECTION_LABELS.get(text, str(value or ""))


def _confidence(value: Any) -> str:
    text = str(value or "").upper()
    return CONFIDENCE_LABELS.get(text, str(value or "—"))


def _scenario_label(value: Any) -> str:
    return SCENARIO_LABELS.get(str(value or "").upper(), str(value or "—"))


def _probability(value: Any) -> str:
    text = str(value or "").upper()
    return {"HIGH": "высокая", "MEDIUM": "средняя", "LOW": "низкая"}.get(text, str(value or "—"))


def _regime_label(value: Any) -> str:
    return REGIME_LABELS.get(str(value or ""), str(value or "").replace("_", " ").title())


def _score_text(value: Any) -> str:
    try:
        return f"{int(value):+d}"
    except (TypeError, ValueError):
        return "—"


def _is_initial_theme_baseline(bundle: FullSnapshotData) -> bool:
    return str(bundle.validation.get("release_kind") or "").upper() == "INITIAL_AUTONOMOUS_BASELINE"


def _comparison_type(bundle: FullSnapshotData) -> str:
    return str(bundle.metadata.get("comparison_type") or "").upper()


def _comparison_intro(bundle: FullSnapshotData) -> str:
    comparison = _comparison_type(bundle)
    baseline = _format_date(bundle.metadata.get("baseline_snapshot_date") or bundle.validation.get("previous_snapshot_date"))
    current = _format_date(bundle.metadata.get("current_snapshot_date") or bundle.snapshot_date)
    if comparison == "SAME_DAY_REVISION":
        return "Обновлена версия расчёта. Это не означает изменение рыночной позиции за день."
    if comparison == "INITIAL_BASELINE":
        return (
            "Текущая матрица является исходной точкой для будущих месячных сравнений. "
            f"Ориентир для последнего месячного обзора: {baseline}. Текущий обзор: {current}."
        )
    return f"Изменения с последнего месячного обзора от {baseline}. Текущий обзор: {current}."


def _outcome_summary(outcomes: dict[str, Any]) -> str:
    if not outcomes:
        return "Данные по последующей динамике недоступны."
    parts: list[str] = []
    for horizon in ("3m", "6m", "12m"):
        values = outcomes.get(horizon) or {}
        available = {asset: value for asset, value in values.items() if isinstance(value, (int, float))}
        if not available:
            parts.append(f"{horizon}: данные недоступны")
            continue
        strongest = max(available.items(), key=lambda item: item[1])
        weakest = min(available.items(), key=lambda item: item[1])
        parts.append(
            f"{horizon}: сильнее {_analog_asset(strongest[0])} ({strongest[1]:+.1f}%), "
            f"слабее {_analog_asset(weakest[0])} ({weakest[1]:+.1f}%)"
        )
    return "; ".join(parts)


def _analog_difference(analog: dict[str, Any]) -> str:
    missing = analog.get("missing_factors") or []
    if missing:
        labels = ", ".join(_regime_label(item) for item in missing[:2])
        return f"Не все факторы доступны для этого периода: {labels}. Поэтому сходство и уверенность снижены."
    differences = analog.get("differences_from_today") or []
    if isinstance(differences, list) and differences:
        return "; ".join(str(item) for item in differences[:3])
    return "Главные отличия описаны в методологическом файле; полное совпадение исторических режимов невозможно."


def _missing_factor_warning(analog: dict[str, Any]) -> str:
    missing = analog.get("missing_factors") or []
    if not missing:
        return ""
    labels = ", ".join(_regime_label(item).lower() for item in missing[:2])
    return f"Ограничение данных: недоступен фактор {labels}; сходство показано уже после поправки."


def _regime_description(analog: dict[str, Any]) -> str:
    regime = analog.get("regime") or {}
    if isinstance(regime, dict) and regime:
        return "; ".join(_ru_analog_text(value) for value in list(regime.values())[:4])
    return "Сходство рассчитано по текущему набору макро- и рыночных факторов."


def _outcome_availability(outcomes: dict[str, Any]) -> str:
    labels = []
    for horizon, label in (("3m", "3 месяца"), ("6m", "6 месяцев"), ("12m", "12 месяцев")):
        values = outcomes.get(horizon) or {}
        available = any(isinstance(value, (int, float)) for value in values.values())
        labels.append(f"{label}: {'доступен' if available else 'недоступен'}")
    return "; ".join(labels)


def _horizon_outcome(outcomes: dict[str, Any], horizon: str) -> str:
    values = outcomes.get(horizon) or {}
    available = {asset: value for asset, value in values.items() if isinstance(value, (int, float))}
    if not available:
        label = {"3m": "3 месяца", "6m": "6 месяцев", "12m": "12 месяцев"}.get(horizon, horizon)
        return f"Результат за {label} пока недоступен."
    strongest = max(available.items(), key=lambda item: item[1])
    weakest = min(available.items(), key=lambda item: item[1])
    return (
        f"сильнее {_analog_asset(strongest[0])} ({strongest[1]:+.1f}%), "
        f"слабее {_analog_asset(weakest[0])} ({weakest[1]:+.1f}%)"
    )


def _analog_asset(value: Any) -> str:
    text = str(value or "")
    return ANALOG_ASSET_LABELS.get(text, text.replace("_", " "))


def _source_why_used(source: dict[str, Any]) -> str:
    user_reason = _controlled_ru_text(str(source.get("why_used_user") or "").strip())
    if user_reason:
        return user_reason
    technical = str(source.get("selection_reason") or "").strip()
    if technical and not re.search(r"\bTIER_[123]\b|score\s+\d+", technical, flags=re.I):
        return technical
    title = str(source.get("title") or "").casefold()
    regions = source.get("relevant_regions") or source.get("coverage_regions") or []
    assets = source.get("relevant_asset_classes") or source.get("coverage_asset_classes") or []
    region_text = _join_financial_labels([_region(item) for item in regions[:3]]) if isinstance(regions, list) else str(regions)
    asset_text = _natural_asset_phrase(assets) if isinstance(assets, list) else str(assets)
    if "financial stability" in title:
        return f"Источник по финансовой устойчивости, банковскому сектору и условиям Credit для {region_text or 'Global'}."
    if "commodit" in title or "energy" in title:
        return f"Источник по Commodities, энергетике и инфляционным рискам для {region_text or 'Global'}."
    if "japan" in title or "boj" in title:
        return "Источник по политике BoJ и состоянию рынка Japan Government Bonds."
    if "emerging market" in title:
        return "Источник по EM Equities, потокам капитала и динамике доллара."
    if "outlook" in title or "forecast" in title:
        return f"Глобальный Multi-asset обзор для оценки {_natural_asset_phrase(assets)}."
    if asset_text or region_text:
        return f"Аналитический обзор {asset_text or 'Markets'} для {region_text or 'Global'}."
    return "Институциональный материал, использованный как часть текущего рыночного обзора."


def _natural_asset_phrase(values: Any) -> str:
    if not isinstance(values, list):
        return "Markets"
    labels = [_asset_class(value) for value in values if value]
    if not labels or labels == ["Multi-asset"]:
        return "Equities, Government Bonds и Commodities"
    return _join_financial_labels(labels)


def _join_financial_labels(labels: list[str]) -> str:
    labels = [label for label in labels if label]
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return " и ".join(labels)
    return ", ".join(labels[:-1]) + " и " + labels[-1]


def _ru_analog_text(value: Any) -> str:
    text = str(value or "")
    replacements = {
        **REGIME_VALUE_LABELS,
        **{key.casefold(): value for key, value in REGIME_VALUE_LABELS.items()},
        "Inflation regime": "инфляционный режим",
        "Policy stance": "денежно-кредитная политика",
        "Real-yield regime": "режим реальных доходностей",
        "Credit-spread regime": "кредитные спреды",
        "Equity breadth": "ширина рынка акций",
        "Growth / style leadership": "лидерство стилей",
        "US-led market context": "рыночная конфигурация США",
        "then versus": "тогда, сейчас:",
        "now": "",
        "No major categorical regime difference across the available factors.": "По доступным факторам нет крупного категориального отличия.",
        "DESCRIPTIVE_NOT_PREDICTIVE": "описательный ориентир, не прогноз",
        "INCOMPLETE_REALIZED_OUTCOMES_SHOWN_EXPLICITLY": "неполные результаты показаны явно",
    }
    for source, target in replacements.items():
        text = re.sub(re.escape(source), target, text, flags=re.I)
    for source, target in ANALOG_ASSET_LABELS.items():
        text = re.sub(rf"\b{re.escape(source)}\b", target, text)
    text = _controlled_ru_text(text)
    return text.replace("  ", " ").strip(" .") + ("." if text and not text.rstrip().endswith(".") else "")


def _controlled_ru_text(value: Any) -> str:
    text = str(value or "")
    for pattern, replacement in CONTROLLED_GLOSSARY:
        text = re.sub(pattern, replacement, text, flags=re.I)
    return text


def _et(value: Any) -> str:
    return _e(_controlled_ru_text(value))


def _format_date(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "—"
    try:
        parsed = datetime.fromisoformat(raw[:10]).date()
    except ValueError:
        return raw
    months = [
        "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря",
    ]
    return f"{parsed.day} {months[parsed.month - 1]} {parsed.year}"


def _render_footer() -> None:
    st.markdown(
        f"<div class='mae-footer-disclosure'>{_e(QUIET_DISCLOSURE)}</div>",
        unsafe_allow_html=True,
    )


def _update_result_message(result: dict[str, Any]) -> str:
    errors = " ".join(str(item) for item in result.get("errors") or [])
    if "NO_NEW_OR_REFRESHED_ELIGIBLE_DOCUMENTS" in errors:
        return "Аналитика актуальна. Значимых новых материалов или изменений не обнаружено."
    if "SOURCE_SELECTION_BLOCKED" in errors or "COLLECTION" in errors:
        return "Не все источники были доступны. Показана последняя подтверждённая рыночная картина."
    if "LLM" in errors or "OPENAI" in errors:
        return "Новая версия не опубликована. Показана последняя подтверждённая рыночная картина."
    return "Показана последняя подтверждённая рыночная картина. Технические подробности сохранены в отчётах."


def _e(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)
