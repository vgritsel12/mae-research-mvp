from __future__ import annotations

import csv
import html
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.domain.enums import AnalystStatus, CoverageStatus, RunMode
from app.domain.models import (
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
from app.exporters.exporters import ExportService
from app.llm.provider import check_openai_connection, get_provider, llm_usage_summary, openai_status_message
from app.services.baseline import CurrentMaeCell, active_baseline_snapshot, current_mae_cells, latest_review_run, mae_status_metrics
from app.services.collectors import import_article_text, import_article_url, run_update
from app.services.evidence import EvidenceEngine
from app.services.matrix import MatrixEngine
from app.services.market_view import (
    PUBLISHED_SIGNAL,
    WATCHLIST,
    MarketSignalView,
    ScenarioView,
    build_mae_matrix_view,
    build_market_overview,
    build_market_signals,
)
from app.services.normalization import is_applicable_canonical_cell
from app.services.pipeline import PipelineService
from app.services.production import is_content_valid_article, is_production_change, is_production_research_view, is_production_signal, is_production_source
from app.services.queries import QueryService
from app.services.review import review_entity
from app.services.scenarios import ScenarioEngine
from app.services.signals import ShiftSignalEngine
from app.ui import design
from app.ui.helpers import (
    can_edit,
    disabled_hint,
    llm_mode_label,
    llm_session_key,
    render_dataframe,
    render_interactive_table,
    render_matrix_table,
    report_action_error,
    uploaded_file_text,
)


query = QueryService()


def _df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def overview_page(session: Session, settings: Settings) -> None:
    st.title("Обзор")
    today_label = design.format_date(date.today())
    articles = [a for a in session.scalars(select(Article)).all() if is_content_valid_article(a)]
    changes = [c for c in session.scalars(select(ChangeLog)).all() if is_production_change(c, session)]
    signals = [s for s in session.scalars(select(ShiftSignal).order_by(desc(ShiftSignal.created_at))).all() if is_production_signal(s, session)]
    current_cells = current_mae_cells(session, RunMode.REAL.value)
    metrics = mae_status_metrics(session, RunMode.REAL.value)
    baseline = active_baseline_snapshot(session)
    technical_review = latest_review_run(session, "technical")
    financial_review = latest_review_run(session, "financial")

    processed_articles = len([a for a in articles if a.processing_status != "NEW"])
    independent_orgs = len({a.source.institution_name for a in articles})
    active_signals = len([s for s in signals if s.analyst_status == AnalystStatus.APPROVED.value and s.suggested_strength != 0])
    changed_cells = len([cell for cell in current_cells if cell.validated_adjustment not in (None, 0)])
    baseline_text = _baseline_date_text(baseline)
    st.markdown(
        design.editorial_intro(
            "Рабочий срез показывает действующую baseline-матрицу, свежие подтверждённые изменения, покрытие источниками и качество проверки.",
            "Срез дня",
            [
                ("дата анализа", today_label),
                ("baseline", baseline_text),
                ("текущих оценок", metrics["current_scores"]),
            ],
        ),
        unsafe_allow_html=True,
    )
    cols = st.columns(6)
    cards = [
        ("Публикаций", processed_articles, "реальные обработанные"),
        ("Институтов", independent_orgs, "независимые организации"),
        ("Сырое покрытие", f"{metrics['raw_coverage_pct']}%", f"{metrics['raw_publications']} публикаций"),
        ("Качественное", f"{metrics['qualified_coverage_pct']}%", f"{metrics['qualified_research_views']} взглядов"),
        ("Сценарно устойчиво", f"{metrics['financial_reviewed_coverage_pct']}%", "по сохранённым данным"),
        ("Изменений", len([c for c in changes if c.idea_state == "MATERIAL_SHIFT"]), "существенные"),
    ]
    for col, (label, value, hint) in zip(cols, cards, strict=False):
        col.markdown(design.metric_card(label, value, hint), unsafe_allow_html=True)

    if metrics["approved_shifts"] == 0:
        if baseline and baseline.baseline_date:
            st.info(f"Новых подтверждённых изменений нет. Показана действующая baseline-матрица от {design.format_date(baseline.baseline_date)}.")
        else:
            st.info("Новых подтверждённых изменений нет. Показана импортированная baseline-матрица. Исходная дата baseline не указана.")
    if processed_articles < 20 or independent_orgs < 8 or metrics["qualified_covered"] < 25:
        st.warning(
            "Качественное покрытие пока ниже целевого минимума: нужны профессиональные публикации, "
            "независимые источники и evidence, прошедшее проверку."
        )

    left, right = st.columns([1.05, 0.95], gap="large")
    with left:
        st.subheader("Текущая MAE")
        current_visible_cells = [cell for cell in current_cells if cell.current_score is not None]
        render_matrix_table(_current_matrix_pivot(current_visible_cells))
        st.subheader("Топ baseline overweight")
        render_dataframe(_baseline_extremes(current_cells, positive=True), max_rows=5)
        st.subheader("Топ baseline underweight")
        render_dataframe(_baseline_extremes(current_cells, positive=False), max_rows=5)
        st.subheader("Свежие изменения")
        top_signals = sorted(
            [s for s in signals if s.analyst_status == AnalystStatus.APPROVED.value],
            key=lambda s: abs(s.suggested_strength),
            reverse=True,
        )[:5]
        if not top_signals:
            design.empty_state("Новых подтверждённых изменений нет.", "Baseline остаётся действующей до появления validated shifts.")
        else:
            render_interactive_table(
                [_signal_row_for_user(signal) for signal in top_signals],
                key="overview_top_signals",
                filter_columns=("Регион", "Актив", "Статус"),
                default_sort="Сила",
            )

    with right:
        st.subheader("Макро-темы")
        themes = _macro_themes(session)
        if themes:
            render_dataframe(themes, max_rows=8)
        else:
            design.empty_state("Темы появятся после анализа публикаций.", "Драйверы берутся из сохранённых аналитических взглядов.")
        st.subheader("Последние публикации")
        render_interactive_table(
            _publication_rows(session)[:8],
            key="overview_publications",
            drop_columns=("article_id",),
            filter_columns=("Источник", "Статус"),
            default_sort="Дата",
        )
        st.subheader("Проверки")
        render_dataframe(
            [
                {"Проверка": "Technical review", "Статус": technical_review.verdict if technical_review else "NOT_RUN"},
                {"Проверка": "Financial review", "Статус": financial_review.verdict if financial_review else "NOT_RUN"},
                {"Проверка": "Сырое покрытие", "Статус": f"{metrics['raw_coverage_pct']}%"},
                {"Проверка": "Качественное покрытие", "Статус": f"{metrics['qualified_coverage_pct']}%"},
                {"Проверка": "Сценарно устойчиво", "Статус": f"{metrics['financial_reviewed_coverage_pct']}%"},
            ],
            max_rows=5,
        )


def add_publication_page(session: Session, settings: Settings) -> None:
    st.title("Добавить публикацию")
    publication_count = len(session.scalars(select(Article.id)).all())
    st.markdown(
        design.editorial_intro(
            "Добавьте ссылку, текст или файл публикации. Анализ запускается только после явного действия и сохраняет каждый промежуточный результат.",
            "Новая запись",
            [
                ("публикаций в базе", publication_count),
                ("способа ввода", 3),
                ("лимит символов", settings.max_article_chars),
            ],
        ),
        unsafe_allow_html=True,
    )
    editable = can_edit(settings)
    if not editable:
        st.info(disabled_hint(settings))

    with st.container(border=True):
        st.markdown("#### Новая публикация")
        tab_url, tab_text, tab_file = st.tabs(["Ссылка", "Текст", "Файл"])
        with tab_url:
            url = st.text_input("URL", placeholder="https://example.com/research/outlook", disabled=not editable)
            source_name = st.text_input("Название источника", key="url_source", placeholder="Название института", disabled=not editable)
            pub_date = st.date_input("Дата", value=date.today(), key="url_date", disabled=not editable, format="DD.MM.YYYY")
            if st.button("Импортировать и проанализировать", type="primary", disabled=not editable, use_container_width=True):
                if not url.strip() or not source_name.strip():
                    st.warning("Заполните URL и название источника.")
                else:
                    try:
                        article = import_article_url(session, url, source_name, pub_date)
                        result = _analyze_article_with_status(session, article, settings)
                        session.commit()
                        _render_pipeline_result(article, result)
                    except Exception:  # noqa: BLE001
                        report_action_error(session, "publication_url_import_failed", "Не удалось импортировать и проанализировать ссылку.")

        with tab_text:
            title = st.text_input("Заголовок", key="text_title", disabled=not editable)
            source_name = st.text_input("Источник", key="text_source", placeholder="Название института", disabled=not editable)
            pub_date = st.date_input("Дата", value=date.today(), key="text_date", disabled=not editable, format="DD.MM.YYYY")
            text = st.text_area("Текст публикации", height=260, disabled=not editable)
            if st.button("Сохранить и проанализировать", type="primary", disabled=not editable, use_container_width=True):
                if not text.strip() or not source_name.strip():
                    st.warning("Добавьте текст публикации и источник.")
                else:
                    try:
                        article = import_article_text(session, title or "Публикация без заголовка", source_name, pub_date, text)
                        result = _analyze_article_with_status(session, article, settings)
                        session.commit()
                        _render_pipeline_result(article, result)
                    except Exception:  # noqa: BLE001
                        report_action_error(session, "publication_text_import_failed", "Не удалось сохранить и проанализировать текст.")

        with tab_file:
            st.caption("Поддерживаемые форматы: TXT, PDF, DOCX.")
            uploaded = st.file_uploader("Зона загрузки", type=["txt", "pdf", "docx"], disabled=not editable)
            title = st.text_input("Название", key="file_title", disabled=not editable)
            source_name = st.text_input("Источник", key="file_source", placeholder="Название института", disabled=not editable)
            pub_date = st.date_input("Дата", value=date.today(), key="file_date", disabled=not editable, format="DD.MM.YYYY")
            if st.button("Загрузить и проанализировать", type="primary", disabled=not editable or uploaded is None, use_container_width=True):
                if not source_name.strip():
                    st.warning("Укажите источник.")
                else:
                    try:
                        text = uploaded_file_text(uploaded)
                        article = import_article_text(session, title or uploaded.name, source_name, pub_date, text)
                        result = _analyze_article_with_status(session, article, settings)
                        session.commit()
                        _render_pipeline_result(article, result)
                    except Exception:  # noqa: BLE001
                        report_action_error(session, "publication_file_import_failed", "Не удалось загрузить и проанализировать файл.")

    rows = _publication_rows(session)
    st.subheader("История публикаций")
    if not rows:
        design.empty_state("История публикаций пуста.", "Добавьте ссылку, текст или файл, чтобы запустить анализ.")
        return
    render_interactive_table(
        rows,
        key="articles",
        filter_columns=("Источник", "Способ загрузки", "Статус"),
        default_sort="Дата",
    )
    with st.expander("Проанализировать публикацию из истории", expanded=False):
        labels = {f"{r['Дата']} · {r['Источник']} · {r['Заголовок'][:80]}": r["article_id"] for r in rows}
        selected_label = st.selectbox("Публикация", list(labels))
        selected_article = session.get(Article, labels[selected_label])
        already_processed = bool(selected_article and selected_article.views)
        confirm_repeat = False
        if already_processed:
            st.warning("Эта публикация уже анализировалась. Повторный запуск может потратить лимит OpenAI.")
            confirm_repeat = st.checkbox("Подтвердить повторный анализ")
        if st.button("Запустить анализ", disabled=not editable or (already_processed and not confirm_repeat)):
            try:
                result = _analyze_article_with_status(session, selected_article, settings, allow_repeat=True)
                session.commit()
                _render_pipeline_result(selected_article, result)
                st.rerun()
            except Exception:  # noqa: BLE001
                report_action_error(session, "publication_analysis_failed", "Не удалось выполнить анализ публикации.")


def changes_and_scenarios_page(session: Session, settings: Settings) -> None:
    st.title("Изменения")
    changes = [c for c in session.scalars(select(ChangeLog).order_by(desc(ChangeLog.created_at))).all() if is_production_change(c, session)]
    scenarios_count = len([s for s in session.scalars(select(ScenarioCard)).all() if s.linked_change_id in {c.id for c in changes}])
    st.markdown(
        design.editorial_intro(
            "Здесь новый взгляд сравнивается с предыдущим, раскладывается на три сценария и связывается с подтверждениями и сигналами.",
            "Сценарная карта",
            [
                ("изменений в ленте", len(changes)),
                ("сценариев создано", scenarios_count),
            ],
        ),
        unsafe_allow_html=True,
    )
    if not changes:
        design.empty_state("Изменений пока нет.", "Добавьте публикацию, чтобы сравнить новый взгляд с предыдущими публикациями.")
        return

    rows = _change_user_rows(session, changes)
    filtered_df = render_interactive_table(
        rows,
        key="changes_table",
        drop_columns=("change_id",),
        filter_columns=("Регион", "Актив", "Источник", "Статус"),
        default_sort="Дата",
    )
    if filtered_df.empty:
        return

    visible_ids = [row["change_id"] for row in rows if row["Дата"] in set(filtered_df["Дата"].tolist())]
    visible_changes = [change for change in changes if change.id in visible_ids] or changes
    scenarios_by_change = _scenarios_by_change(session)
    assessments = {a.scenario_id: a for a in session.scalars(select(ScenarioAssessment)).all()}
    labels = {
        f"{design.format_date(change.created_at, with_time=True)} · {_change_context(session, change)['source']} · {design.compact_text(change.explanation, 80)}": change.id
        for change in visible_changes
    }
    selected = st.selectbox("Открыть изменение", list(labels), key="change_detail_selector")
    change = session.get(ChangeLog, labels[selected])
    if change:
        with st.expander("Подробности изменения и сценарии", expanded=True):
            _render_change_detail_card(session, change, scenarios_by_change.get(change.id, []), assessments)

    signals = [s for s in session.scalars(select(ShiftSignal).order_by(desc(ShiftSignal.created_at))).all() if is_production_signal(s, session)]
    st.subheader("Сигналы")
    if not signals:
        design.empty_state("Сигналов пока нет.", "Сигнал появится после сценарного анализа и подтверждений в данных.")
    else:
        signal_rows = [_signal_row_for_user(signal) for signal in signals]
        render_interactive_table(
            signal_rows,
            key="changes_signals_table",
            drop_columns=("signal_id",),
            filter_columns=("Регион", "Актив", "Источник", "Статус"),
            default_sort="Дата",
        )
        signal_labels = {f"{row['Дата']} · {row['Регион']} · {row['Актив']} · {row['Сила']}": row["signal_id"] for row in signal_rows}
        selected_signal = st.selectbox("Открыть карточку сигнала", list(signal_labels), key="changes_signal_detail")
        signal = session.get(ShiftSignal, signal_labels[selected_signal])
        if signal:
            _render_signal_detail(session, signal, settings)


def signals_page(session: Session, settings: Settings) -> None:
    st.title("Сигналы")
    signals = [row for row in session.scalars(select(ShiftSignal).order_by(desc(ShiftSignal.created_at))).all() if is_production_signal(row, session)]
    positive_count = len([signal for signal in signals if signal.suggested_strength > 0])
    negative_count = len([signal for signal in signals if signal.suggested_strength < 0])
    st.markdown(
        design.editorial_intro(
            "Сигнал появляется только после изменения взгляда, сценарной развилки и проверки подтверждений. Карточка показывает причину, силу и связь с матрицей.",
            "Сигнальная полоса",
            [
                ("сигналов всего", len(signals)),
                ("позитивных", positive_count),
                ("негативных", negative_count),
            ],
        ),
        unsafe_allow_html=True,
    )
    if not signals:
        design.empty_state("Новых сигналов пока нет.", "Добавьте публикацию или запустите обновление источников.")
        return

    filtered = _signal_filters(signals)
    st.caption(f"Найдено сигналов: {len(filtered)} из {len(signals)}.")
    if not filtered:
        design.empty_state("По выбранным фильтрам сигналов нет.", "Измените регион, актив или статус.")
        return

    for signal in filtered:
        _render_signal_card(signal)
        if st.button("Подробнее", key=f"signal_detail_{signal.id}", use_container_width=True):
            st.session_state["selected_signal_id"] = signal.id

    selected_id = st.session_state.get("selected_signal_id")
    selected_signal = session.get(ShiftSignal, selected_id) if selected_id else filtered[0]
    if selected_signal:
        _render_signal_detail(session, selected_signal, settings)


def matrix_page(session: Session, settings: Settings) -> None:
    st.title("Матрица MAE")
    mode = RunMode.REAL.value
    layer = st.segmented_control(
        "Слой матрицы",
        ["Текущая матрица", "Только новые изменения", "Покрытие источниками"],
        default="Текущая матрица",
        key="matrix_layer",
    )
    cells = current_mae_cells(session, mode)
    rows = _matrix_scores_for_mode(session, mode)
    metrics = mae_status_metrics(session, mode)
    baseline = active_baseline_snapshot(session)
    production_articles = [article for article in session.scalars(select(Article)).all() if is_content_valid_article(article)]
    last_run = max((article.updated_at for article in production_articles), default=None)
    processed_articles = len([a for a in production_articles if a.processing_status != "NEW"])
    independent_orgs = len({a.source.institution_name for a in production_articles})
    st.markdown(
        design.status_panel(
            "Текущая MAE",
            [
                ("baseline", _baseline_date_text(baseline)),
                ("последний research run", design.format_date(last_run, with_time=True) if last_run else "нет"),
                ("сырое покрытие", f"{metrics['raw_coverage_pct']}%"),
                ("качественное покрытие", f"{metrics['qualified_coverage_pct']}%"),
                ("с устойчивыми данными", f"{metrics['financial_reviewed_coverage_pct']}%"),
                ("обновлённых ячеек", metrics["approved_shifts"]),
                ("предложенных изменений", metrics["proposed_shifts"]),
                ("watchlist", metrics["review_pending"]),
                ("публикаций обработано", processed_articles),
                ("институтов", independent_orgs),
            ],
        ),
        unsafe_allow_html=True,
    )
    cols = st.columns(4)
    cards = [
        ("Текущие оценки", metrics["current_scores"], "baseline + проверенное изменение"),
        ("Опубликовано", metrics["approved_shifts"], "поверх baseline"),
        ("Качественное", f"{metrics['qualified_coverage_pct']}%", "проверенное evidence"),
        ("Недостаточно данных", metrics["fresh_insufficient"], "не публикуются как 0"),
    ]
    for col, (label, value, hint) in zip(cols, cards, strict=False):
        col.markdown(design.metric_card(label, value, hint), unsafe_allow_html=True)

    if layer == "Текущая матрица":
        detail_cells = [cell for cell in cells if cell.current_score is not None]
        render_matrix_table(_current_matrix_pivot(detail_cells))
        table_rows = _current_mae_user_rows(detail_cells)
        st.subheader("Ячейки Current MAE")
    else:
        fresh_cells = [cell for cell in cells if cell.proposed_adjustment is not None or cell.coverage_status != CoverageStatus.NO_DATA.value]
        if layer == "Только новые изменения":
            render_matrix_table(_fresh_shift_pivot(fresh_cells))
            detail_cells = fresh_cells
            table_rows = _fresh_shift_user_rows(detail_cells)
            st.subheader("Свежие изменения")
        else:
            coverage_pivot = _coverage_pivot(cells)
            render_dataframe(coverage_pivot.reset_index().to_dict("records") if not coverage_pivot.empty else [])
            detail_cells = cells
            table_rows = _coverage_user_rows(detail_cells)
            st.subheader("Покрытие источниками")
    render_interactive_table(
        table_rows,
        key=f"matrix_cells_{layer}",
        filter_columns=("Регион", "Статус"),
        default_sort="Категория",
        empty_message="В выбранном слое нет строк.",
    )

    labels = {f"{row['Категория']} · {row['Регион']} · {row.get('Current score', row.get('Validated shift', row.get('Статус', '')))}": idx for idx, row in enumerate(table_rows)}
    if labels:
        selected = st.selectbox("Открыть карточку ячейки", list(labels), key="matrix_cell_detail")
        cell = detail_cells[labels[selected]]
        _render_current_cell_detail(session, cell)

    with st.expander("Показать технические подробности", expanded=False):
        editable = can_edit(settings)
        technical_rows = query.matrix_for_mode(session, mode)
        render_interactive_table(
            technical_rows,
            key="matrix_audit",
            filter_columns=("Регион",),
            default_sort="Row key",
        )
        if st.button("Пересчитать матрицу сейчас", disabled=not editable):
            MatrixEngine().rebuild_matrix(session, mode=RunMode.REAL.value)
            session.commit()
            st.success("Матрица пересчитана.")
            st.rerun()
        if rows:
            labels = {f"{design.row_key_label(m.template_row_key)} · {design.region_label(m.region)}": m.id for m in rows}
            selected = st.selectbox("Ячейка для ручной корректировки", list(labels))
            row = session.get(MatrixScore, labels[selected])
            score = st.number_input("Итоговая оценка", min_value=-3, max_value=3, step=1, value=int(row.approved_score or 0), disabled=not editable)
            reason = st.text_input("Комментарий к ручной корректировке", disabled=not editable)
            if st.button("Сохранить ручную корректировку", disabled=not editable):
                MatrixEngine().override_score(session, row.template_row_key, row.region, int(score), reason, is_demo=False)
                session.commit()
                st.success("Корректировка сохранена.")
                st.rerun()


def export_page(session: Session, settings: Settings) -> None:
    st.title("Экспорт")
    with st.container(border=True):
        st.markdown("#### Экспорт результатов")
        period = st.date_input("Период", value=(date.today() - timedelta(days=30), date.today()), key="export_period", format="DD.MM.YYYY")
        include_audit = st.checkbox("Включать историю расчёта", value=True)
        include_publications = st.checkbox("Включать публикации", value=True)
        include_evidence = st.checkbox("Включать подтверждения", value=True)
        exporter = ExportService()
        st.caption("Excel сохраняет структуру приложенного MAE-шаблона. CSV содержит матрицу. JSON содержит полный пакет данных.")
        if not (include_audit and include_publications and include_evidence):
            st.info("Текущий проверенный экспорт формирует полный пакет. Выбранные флаги сохранены как пользовательские параметры экрана.")
        col1, col2, col3 = st.columns(3)
        col1.download_button(
            "Скачать Excel",
            exporter.export_xlsx_bytes(session),
            "mae_export.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )
        col2.download_button("Скачать CSV", exporter.export_csv_bytes(session), "mae_matrix_export.csv", "text/csv", use_container_width=True)
        col3.download_button("Скачать JSON", exporter.export_json_bytes(session), "mae_export.json", "application/json", use_container_width=True)
        st.caption(f"Период: {_period_label(period)}.")


def sources_page(session: Session, settings: Settings) -> None:
    st.title("Источники")
    tab_add, tab_sources = st.tabs(["Добавить публикацию", "Реестр источников"])
    with tab_add:
        st.markdown("#### Новая публикация")
        editable = can_edit(settings)
        if not editable:
            st.info(disabled_hint(settings))
        source_name = st.text_input("Источник", key="sources_add_source", placeholder="Название института", disabled=not editable)
        title = st.text_input("Заголовок", key="sources_add_title", disabled=not editable)
        pub_date = st.date_input("Дата", value=date.today(), key="sources_add_date", disabled=not editable, format="DD.MM.YYYY")
        url = st.text_input("URL", key="sources_add_url", disabled=not editable)
        text = st.text_area("Текст публикации", height=220, key="sources_add_text", disabled=not editable)
        if st.button("Сохранить и проанализировать", type="primary", disabled=not editable, use_container_width=True):
            if not source_name.strip() or not (text.strip() or url.strip()):
                st.warning("Укажите источник и добавьте URL или текст публикации.")
            else:
                try:
                    article = (
                        import_article_text(session, title or "Публикация без заголовка", source_name, pub_date, text)
                        if text.strip()
                        else import_article_url(session, url, source_name, pub_date)
                    )
                    result = _analyze_article_with_status(session, article, settings)
                    session.commit()
                    _render_pipeline_result(article, result)
                except Exception:  # noqa: BLE001
                    report_action_error(session, "sources_publication_failed", "Не удалось сохранить и проанализировать публикацию.")
        st.subheader("История публикаций")
        render_interactive_table(
            _publication_rows(session),
            key="sources_publications",
            drop_columns=("article_id",),
            filter_columns=("Источник", "Способ загрузки", "Статус"),
            default_sort="Дата",
        )
    with tab_sources:
        rows = _source_status_rows(session)
        render_interactive_table(
            rows,
            key="source_registry",
            filter_columns=("Институт", "Категория", "Статус"),
            default_sort="Институт",
        )
        with st.expander("Официальные загрузчики", expanded=False):
            st.caption("Обновление сохраняет только реальные публикации с доступным текстом.")
            if st.button("Запустить обновление источников", disabled=not can_edit(settings)):
                job = run_update(session)
                session.commit()
                st.success(f"Обновление завершено: успешно {job.succeeded}, ошибок {job.failed}.")
                st.rerun()


def system_diagnostics_page(session: Session, settings: Settings) -> None:
    st.title("Система и диагностика")
    _analysis_status_block(session, settings)
    with st.expander("Технические параметры", expanded=False):
        render_dataframe(
            [
                {"Параметр": "Среда приложения", "Значение": settings.app_env},
                {"Параметр": "Публичный режим просмотра", "Значение": settings.public_demo},
                {"Параметр": "Режим базы данных", "Значение": settings.database_mode},
                {"Параметр": "Глубина сравнения, дней", "Значение": settings.lookback_days},
                {"Параметр": "Срок актуальности матрицы, дней", "Значение": settings.matrix_max_age_days},
                {"Параметр": "Дневной лимит анализов", "Значение": settings.max_llm_calls_per_day},
                {"Параметр": "Лимит анализов в сессии", "Значение": settings.max_llm_calls_per_session},
                {"Параметр": "Лимит текста публикации", "Значение": settings.max_article_chars},
            ]
        )
    with st.expander("Источники и официальные загрузчики", expanded=False):
        render_interactive_table(query.sources(session), key="sources", drop_columns=("id",), filter_columns=("Институт", "Категория", "Активен"))
        urls = settings.outlook_sources_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        st.caption(f"Список разрешённых аналитических источников: {len([u for u in urls if 'http' in u])} URL.")
        if st.button("Запустить обновление официальных источников", disabled=not can_edit(settings)):
            job = run_update(session)
            session.commit()
            st.success(f"Обновление завершено: успешно {job.succeeded}, ошибок {job.failed}.")
    with st.expander("Ручные операции анализа", expanded=False):
        _manual_operations(session, settings)
    with st.expander("Аналитические взгляды", expanded=False):
        render_interactive_table(query.views(session), key="views_system", drop_columns=("id",), filter_columns=("Институт", "Регион", "Сегмент", "Статус"))
    with st.expander("Журнал запусков и ошибок", expanded=False):
        st.subheader("Загрузки")
        render_dataframe(query.update_jobs(session))
        st.subheader("OpenAI-вызовы")
        render_dataframe(query.llm_call_logs(session))
        st.subheader("Ошибки приложения")
        render_dataframe(query.app_logs(session))


def _analysis_status_block(session: Session, settings: Settings) -> None:
    label, _ = llm_mode_label(settings)
    summary = llm_usage_summary(settings, llm_session_key())
    connection = st.session_state.get("openai_connection_status", "not_checked")
    connection_text = {
        "not_checked": "не тестировалось",
        "success": "успешно",
    }.get(connection, f"ошибка: {openai_status_message(connection)}")
    cols = st.columns(4)
    cols[0].metric("Режим анализа", label)
    cols[1].metric("Модель", settings.openai_model.upper() if settings.openai_model else "не задана")
    cols[2].metric("Подключение", connection_text)
    cols[3].metric("Доступно анализов сегодня", summary.daily_remaining)
    db_label = "облачная" if settings.database_url else "локальная"
    st.caption(f"База данных: {db_label}. Доступно в текущей сессии: {summary.session_remaining}.")
    if settings.llm_provider.strip().lower() == "openai" and settings.openai_api_key and settings.openai_model:
        if st.button("Проверить подключение", disabled=not can_edit(settings)):
            result = check_openai_connection(settings, session_key=llm_session_key())
            st.session_state["openai_connection_status"] = result.status
            if result.ok:
                st.success("Подключение OpenAI успешно протестировано.")
            else:
                st.error(result.message)
            st.rerun()
    else:
        st.warning("OpenAI API не подключён. Baseline доступна, но анализ новых публикаций недоступен до настройки подключения.")


def _manual_operations(session: Session, settings: Settings) -> None:
    editable = can_edit(settings)
    views = query.views(session)
    if views:
        labels = {f"{r['Институт']} · {r['Регион']} · {r['Сегмент']}": r["id"] for r in views}
        selected = st.selectbox("Аналитический взгляд для операции", list(labels))
        comment = st.text_input("Комментарий")
        status_options = {
            "Обработано": AnalystStatus.REVIEWED,
            "Опубликовано": AnalystStatus.APPROVED,
            "Отклонено": AnalystStatus.REJECTED,
        }
        status_label = st.selectbox("Новый статус", list(status_options))
        if st.button("Сохранить статус взгляда", disabled=not editable):
            review_entity(session, "research_view", labels[selected], status_options[status_label], comment or "Статус изменён пользователем")
            session.commit()
            st.success("Статус сохранён.")
            st.rerun()
        if st.button("Создать сценарии и сигнал", disabled=not editable):
            provider = _provider_for_session(session)
            from app.services.changes import ChangeTrackerService

            change = ChangeTrackerService(provider).track_changes(session, labels[selected])
            cards = ScenarioEngine(provider).generate_scenarios(session, change.id)
            for card in cards:
                EvidenceEngine().assess_scenario(session, card.id)
            signal = ShiftSignalEngine(provider).generate_signal(session, change.id)
            MatrixEngine().rebuild_matrix(session, mode=RunMode.REAL.value)
            session.commit()
            st.success(f"Обработка выполнена: сценариев {len(cards)}, сигнал {'создан' if signal else 'не создан'}.")
            st.rerun()

    changes = query.changes(session)
    if changes:
        labels = {f"{r['Тип идеи']} · {r['Материальность']} · {r['Типы изменений']}": r["id"] for r in changes}
        selected = st.selectbox("Изменение для ручного запуска", list(labels))
        provider = _provider_for_session(session)
        if st.button("Создать или обновить сценарии", disabled=not editable):
            cards = ScenarioEngine(provider).generate_scenarios(session, labels[selected])
            session.commit()
            st.success(f"Сценарии: {len(cards)}")
            st.rerun()
        if st.button("Сформировать сигнал", disabled=not editable):
            signal = ShiftSignalEngine(provider).generate_signal(session, labels[selected])
            change_obj = session.get(ChangeLog, labels[selected])
            MatrixEngine().rebuild_matrix(session, mode=RunMode.REAL.value)
            session.commit()
            if signal:
                st.success("Сигнал создан.")
            else:
                st.warning("Сигнал не создан: проверьте статус взгляда, три сценария и подтверждения.")
            st.rerun()

    scenarios = query.scenarios(session)
    if scenarios:
        labels = {f"{s['Тип']} · {s['Заголовок'][:80]}": s["id"] for s in scenarios}
        selected = st.selectbox("Сценарий для добавления подтверждения", list(labels))
        with st.container(border=True):
            indicator = st.text_input("Индикатор")
            expected = st.text_input("Ожидание")
            actual = st.text_input("Факт")
            source_url = st.text_input("URL источника", value="manual://evidence")
            support = st.select_slider("Поддержка сценария", options=[-1, 0, 1], value=0)
            importance = st.selectbox("Вес", ["STANDARD", "CRITICAL"])
            if st.button("Сохранить подтверждение", disabled=not editable):
                try:
                    EvidenceEngine().add_observation(
                        session,
                        labels[selected],
                        indicator,
                        date.today(),
                        expected,
                        actual,
                        source_url,
                        support,
                        importance,
                    )
                    session.commit()
                    st.success("Подтверждение сохранено.")
                    st.rerun()
                except Exception:  # noqa: BLE001
                    report_action_error(session, "evidence_observation_failed", "Не удалось сохранить подтверждение.")
        if st.button("Пересчитать проверку подтверждений", disabled=not editable):
            assessment = EvidenceEngine().assess_scenario(session, labels[selected])
            session.commit()
            st.success(f"Статус: {design.humanize(assessment.evidence_status)}, рынок: {design.humanize(assessment.pricing_status)}")
            st.rerun()


def _provider_for_session(session: Session):
    provider = get_provider(session_key=llm_session_key())
    attach = getattr(provider, "attach_session", None)
    if callable(attach):
        attach(session)
    return provider


def _period_label(period: Any) -> str:
    if isinstance(period, tuple) and len(period) == 2:
        return f"{design.format_date(period[0])} – {design.format_date(period[1])}"
    return design.format_date(period)


def _change_user_rows(session: Session, changes: list[ChangeLog]) -> list[dict[str, Any]]:
    rows = []
    signals_by_change = {s.change_id: s for s in session.scalars(select(ShiftSignal)).all() if s.change_id}
    for change in changes:
        ctx = _change_context(session, change)
        signal = signals_by_change.get(change.id)
        rows.append(
            {
                "change_id": change.id,
                "Дата": design.format_date(change.created_at, with_time=True),
                "Источник": ctx["source"],
                "Регион": ctx["region"],
                "Актив": ctx["asset_class"],
                "Статус": design.humanize(change.idea_state),
                "Материальность": design.humanize(change.materiality),
                "Сила": _score_text(signal.suggested_strength if signal else _score_delta_for_change(session, change)),
                "Суть": design.compact_text(design.user_text(change.explanation), 180),
            }
        )
    return rows


def _signal_row_for_user(signal: ShiftSignal) -> dict[str, Any]:
    return {
        "signal_id": signal.id,
        "Дата": design.format_date(signal.created_at, with_time=True),
        "Регион": design.region_label(signal.region),
        "Актив": design.row_key_label(signal.template_row_key),
        "Сила": _score_text(signal.suggested_strength),
        "Статус": design.humanize(signal.analyst_status),
        "Подтверждение": design.humanize(signal.evidence_status),
        "Рынок": design.humanize(signal.pricing_status),
        "Источник": design.source_label(signal.source_urls[0] if signal.source_urls else ""),
        "Что изменилось": design.compact_text(design.user_text(signal.what_changed), 180),
    }


def _macro_themes(session: Session) -> list[dict[str, Any]]:
    counter: dict[str, int] = defaultdict(int)
    for view in session.scalars(select(ResearchView)).all():
        if not is_production_research_view(view, session.get(Article, view.article_id)):
            continue
        for driver in view.drivers[:4]:
            counter[design.user_text(driver)] += 1
    return [
        {"Тема": theme, "Упоминаний": count}
        for theme, count in sorted(counter.items(), key=lambda item: item[1], reverse=True)[:8]
    ]


def _source_status_rows(session: Session) -> list[dict[str, Any]]:
    articles = [article for article in session.scalars(select(Article)).all() if is_content_valid_article(article)]
    jobs = list(session.scalars(select(UpdateJob)).all())
    rows = []
    for source in session.scalars(select(Source).order_by(Source.institution_name)).all():
        if not is_production_source(source):
            continue
        source_articles = [article for article in articles if article.source_id == source.id]
        failed_jobs = [
            job for job in jobs
            if any(detail.get("source") == source.institution_name and detail.get("status") == "error" for detail in (job.details or []))
        ]
        rows.append(
            {
                "Институт": source.institution_name,
                "Публикаций": len(source_articles),
                "Последняя дата": max((a.publication_date for a in source_articles), default=None),
                "Категория": source.category,
                "Статус": "активен" if source.active else "выключен",
                "Успешно": len([a for a in source_articles if a.processing_status == "ANALYSED"]),
                "Ошибок": len(failed_jobs),
                "Активы": ", ".join(
                    sorted(
                        {
                            v.asset_class
                            for article in source_articles
                            for v in article.views
                            if is_production_research_view(v, article)
                        }
                    )
                )
                or "—",
                "URL": source.website,
            }
        )
    return rows


def _publication_rows(session: Session) -> list[dict[str, Any]]:
    rows = []
    articles = list(session.scalars(select(Article).order_by(desc(Article.publication_date), desc(Article.created_at))).all())
    for article in articles:
        if not is_content_valid_article(article):
            continue
        rows.append(
            {
                "article_id": article.id,
                "Дата": design.format_date(article.publication_date),
                "Заголовок": article.title,
                "Источник": article.source.institution_name,
                "Способ загрузки": design.humanize(article.fetch_status),
                "Статус": design.humanize(article.processing_status),
                "URL": article.source_reference,
            }
        )
    return rows


def _baseline_date_text(snapshot: Any) -> str:
    if snapshot is None:
        return "нет baseline"
    if snapshot.baseline_date:
        return design.format_date(snapshot.baseline_date)
    return "UNKNOWN"


def _baseline_extremes(cells: list[CurrentMaeCell], positive: bool) -> list[dict[str, Any]]:
    scored = [cell for cell in cells if cell.baseline_score is not None]
    scored.sort(key=lambda cell: cell.baseline_score or 0, reverse=positive)
    return [
        {
            "Категория": design.row_key_label(cell.template_row_key),
            "Регион": design.region_label(cell.region),
            "Baseline": _score_text(cell.baseline_score),
            "Current": _score_text(cell.current_score),
        }
        for cell in scored[:5]
    ]


def _analyze_article_with_status(session: Session, article: Article, settings: Settings, allow_repeat: bool = False) -> dict[str, Any]:
    if article.views and not allow_repeat:
        st.warning("Публикация уже анализировалась. Повторный запуск возможен только после явного подтверждения в истории.")
        return {"views": 0, "changes": 0, "scenarios": 0, "assessments": 0, "signals": 0, "matrix_scores": 0, "llm_calls": 0}
    with st.status("Идёт анализ публикации", expanded=True) as status:
        st.write("1. Публикация загружена")
        result = PipelineService(session_key=llm_session_key()).process_article(session, article.id)
        st.write(f"2. Основные взгляды извлечены: {result.get('views', 0)}")
        st.write(f"3. Изменения определены: {result.get('changes', 0)}")
        st.write(f"4. Сценарии сформированы: {result.get('scenarios', 0)}")
        st.write(f"5. Подтверждения проверены: {result.get('assessments', 0)}")
        st.write(f"6. MAE обновлена: {result.get('matrix_scores', 0)}")
        status.update(label="Анализ завершён", state="complete")
        return result


def _render_pipeline_result(article: Article, result: dict[str, Any]) -> None:
    st.markdown(
        f"""
<div class="mae-card">
  <div class="mae-card-title">Результат анализа</div>
  <div class="mae-card-body">
    <b>{html.escape(article.title)}</b><br>
    Извлечено взглядов: {int(result.get("views", 0))} · изменений: {int(result.get("changes", 0))} · сценариев: {int(result.get("scenarios", 0))} · сигналов: {int(result.get("signals", 0))}<br>
    OpenAI-вызовов в запуске: {int(result.get("llm_calls", 0))}
  </div>
</div>
""",
        unsafe_allow_html=True,
    )


def _render_change_summary_card(session: Session, change: ChangeLog) -> None:
    current = session.get(ResearchView, change.current_view_id)
    previous = session.get(ResearchView, change.previous_view_id) if change.previous_view_id else None
    signal = session.scalar(select(ShiftSignal).where(ShiftSignal.change_id == change.id))
    strength = signal.suggested_strength if signal else _score_delta(previous, current)
    st.markdown(
        f"""
<div class="mae-card">
  <div class="mae-kicker">{html.escape(design.region_label(current.region if current else ''))} · {html.escape(design.row_key_label(current.template_row_key if current else ''))}</div>
  <div class="mae-card-title">{html.escape(design.compact_text(design.user_text(change.explanation), 180))}</div>
  <div class="mae-card-body">
    Изменение: {html.escape(_view_direction(previous))} → {html.escape(_view_direction(current))}<br>
    Сила сигнала: {strength:+d}<br>
    Дата: {html.escape(design.format_date(change.created_at))}
  </div>
</div>
""",
        unsafe_allow_html=True,
    )


def _render_change_detail_card(
    session: Session,
    change: ChangeLog,
    scenarios: list[ScenarioCard],
    assessments: dict[str, ScenarioAssessment],
) -> None:
    current = session.get(ResearchView, change.current_view_id)
    previous = session.get(ResearchView, change.previous_view_id) if change.previous_view_id else None
    source = current.article.source.institution_name if current and current.article else "источник не указан"
    scenario_html = "".join(_scenario_panel_html(s, assessments.get(s.id)) for s in _ordered_scenarios(scenarios))
    st.markdown(
        f"""
<div class="mae-card">
  <div style="display:flex;justify-content:space-between;gap:.8rem;align-items:flex-start;flex-wrap:wrap;">
    <div>
      <div class="mae-kicker">{html.escape(design.row_key_label(current.template_row_key if current else ''))} · {html.escape(design.region_label(current.region if current else ''))}</div>
      <div class="mae-card-title">{html.escape(design.compact_text(design.user_text(change.explanation), 220))}</div>
    </div>
    <div>{design.badge(design.humanize(change.materiality), 'warning')} {design.badge(design.format_date(change.created_at), 'neutral')}</div>
  </div>
  <div class="mae-grid-2" style="margin-top:.75rem;">
    <div class="mae-mini-panel"><div class="mae-kicker">Предыдущий взгляд</div>{html.escape(_view_sentence(previous))}</div>
    <div class="mae-mini-panel"><div class="mae-kicker">Новый взгляд</div>{html.escape(_view_sentence(current))}</div>
  </div>
  <div class="mae-card-body" style="margin-top:.75rem;">
    <b>Что изменилось:</b> {html.escape(design.user_text(', '.join(design.humanize(x) for x in change.change_types) or 'не указано'))}<br>
    <b>Почему это важно:</b> {html.escape(design.compact_text(design.user_text(change.explanation), 320))}<br>
    <b>Источник:</b> {html.escape(source)}
  </div>
  <div class="mae-grid-3" style="margin-top:.85rem;">{scenario_html}</div>
</div>
""",
        unsafe_allow_html=True,
    )


def _scenario_panel_html(scenario: ScenarioCard, assessment: ScenarioAssessment | None) -> str:
    status = design.humanize(assessment.evidence_status) if assessment else design.humanize(scenario.review_status)
    tone = "positive" if status == "подтверждено" else "warning" if status else "neutral"
    return f"""
<div class="mae-mini-panel">
  <div class="mae-kicker">{html.escape(design.humanize(scenario.scenario_type))}</div>
  <div style="font-weight:760;margin-bottom:.35rem;">{html.escape(design.user_text(scenario.title))}</div>
  <div class="mae-card-body">{html.escape(design.compact_text(design.user_text(scenario.description), 180))}</div>
  <div class="mae-kicker" style="margin-top:.5rem;">Триггеры</div>{design.list_html(scenario.triggers)}
  <div class="mae-kicker" style="margin-top:.5rem;">Признаки подтверждения</div>{design.list_html(scenario.early_indicators)}
  <div class="mae-kicker" style="margin-top:.5rem;">Условия отмены</div>{design.list_html(scenario.reversal_conditions)}
  <div style="margin-top:.6rem;">{design.badge(status or 'нет данных', tone)}</div>
</div>
"""


def _render_signal_card(signal: ShiftSignal, compact: bool = False) -> None:
    source = design.source_label(signal.source_urls[0] if signal.source_urls else "")
    body_limit = 180 if compact else 260
    st.markdown(
        f"""
<div class="mae-card">
  <div style="display:flex;gap:.85rem;align-items:flex-start;">
    <div>{design.score_badge(signal.suggested_strength)}</div>
    <div style="flex:1;min-width:0;">
      <div class="mae-kicker">{html.escape(design.region_label(signal.region))} · {html.escape(design.row_key_label(signal.template_row_key))}</div>
      <div class="mae-card-title">{html.escape(design.compact_text(design.user_text(signal.what_changed), body_limit))}</div>
      <div class="mae-card-body">{html.escape(design.compact_text(design.user_text(signal.why_now), body_limit))}</div>
      <div style="margin-top:.55rem;display:flex;gap:.35rem;flex-wrap:wrap;">
        {design.badge('Статус: ' + design.humanize(signal.analyst_status), 'positive' if signal.analyst_status == 'APPROVED' else 'warning')}
        {design.badge('Источник: ' + source, 'neutral')}
        {design.badge('Обновлено: ' + design.format_date(signal.updated_at), 'neutral')}
      </div>
    </div>
  </div>
</div>
""",
        unsafe_allow_html=True,
    )


def _render_signal_detail(session: Session, signal: ShiftSignal, settings: Settings) -> None:
    st.subheader("Карточка сигнала")
    change = session.get(ChangeLog, signal.change_id) if signal.change_id else None
    previous = session.get(ResearchView, change.previous_view_id) if change and change.previous_view_id else None
    current = session.get(ResearchView, change.current_view_id) if change else None
    scenarios = list(session.scalars(select(ScenarioCard).where(ScenarioCard.linked_change_id == signal.change_id)).all()) if signal.change_id else []
    evidence = _evidence_for_scenarios(session, scenarios)
    matrix_cell = session.scalar(
        select(MatrixScore).where(
            MatrixScore.template_row_key == signal.template_row_key,
            MatrixScore.region == signal.region,
            MatrixScore.is_demo.is_(False),
        )
    )
    matrix_text = _score_text(matrix_cell.approved_score) if matrix_cell else "—"
    st.markdown(
        f"""
<div class="mae-card">
  <div class="mae-card-title">{html.escape(design.row_key_label(signal.template_row_key))} · {html.escape(design.region_label(signal.region))}</div>
  <div class="mae-card-body">
    <b>Изменение аналитического взгляда:</b> {html.escape(design.user_text(_view_sentence(previous)))} → {html.escape(design.user_text(_view_sentence(current)))}<br>
    <b>Подтверждение:</b> {html.escape(design.humanize(signal.evidence_status))}<br>
    <b>Рынок:</b> {html.escape(design.humanize(signal.pricing_status))}<br>
    <b>Влияние на MAE:</b> {signal.suggested_strength:+d} · текущая ячейка {matrix_text}<br>
    <b>Источники:</b> {_source_links(signal.source_urls)}
  </div>
</div>
""",
        unsafe_allow_html=True,
    )
    if scenarios:
        st.markdown("<div class='mae-grid-3'>" + "".join(_scenario_panel_html(s, None) for s in _ordered_scenarios(scenarios)) + "</div>", unsafe_allow_html=True)
    if evidence:
        st.subheader("Подтверждения")
        render_interactive_table(
            evidence,
            key=f"signal_evidence_{signal.id}",
            filter_columns=("Статус", "Вес"),
            default_sort="Дата",
        )
    with st.expander("Показать историю сигнала", expanded=False):
        st.write(f"Создан: {design.format_date(signal.created_at, with_time=True)}")
        st.write(f"Обновлён: {design.format_date(signal.updated_at, with_time=True)}")
        st.write(f"Комментарий: {signal.analyst_comment or 'нет'}")
        editable = can_edit(settings)
        status_options = {
            "Утвердить": AnalystStatus.APPROVED,
            "Вернуть на проверку": AnalystStatus.NEEDS_REVIEW,
            "Отклонить": AnalystStatus.REJECTED,
        }
        selected_status = st.selectbox("Новый статус сигнала", list(status_options))
        comment = st.text_input("Комментарий")
        if st.button("Сохранить статус сигнала", disabled=not editable):
            review_entity(session, "shift_signal", signal.id, status_options[selected_status], comment)
            MatrixEngine().rebuild_matrix(session, mode=RunMode.REAL.value)
            session.commit()
            st.success("Сигнал обновлён, матрица пересчитана.")
            st.rerun()


def _render_matrix_cell_detail(session: Session, cell: MatrixScore) -> None:
    signals = list(session.scalars(select(ShiftSignal).where(ShiftSignal.id.in_(cell.signal_ids))).all()) if cell.signal_ids else []
    sources = sorted({url for signal in signals for url in signal.source_urls})
    why = signals[0].what_changed if signals else cell.override_reason or "Для этой ячейки нет достаточного подтверждения."
    title_score = _score_text(cell.approved_score)
    st.markdown(
        f"""
<div class="mae-card">
  <div class="mae-card-title">Почему здесь {html.escape(title_score)}</div>
  <div class="mae-card-body">
    <b>{html.escape(design.row_key_label(cell.template_row_key))} · {html.escape(design.region_label(cell.region))}</b><br>
    {html.escape(design.compact_text(design.user_text(why), 360))}<br><br>
    <b>Покрытие:</b> {html.escape(design.humanize(cell.coverage_status))} · источников {cell.source_count}<br>
    <b>Свежесть:</b> {cell.freshness_score:.0%}<br>
    <b>Какие сигналы повлияли:</b> {len(signals)}<br>
    <b>Какие источники использованы:</b> {_source_links(sources)}<br>
    <b>Когда обновлено:</b> {html.escape(design.format_date(cell.updated_at, with_time=True))}
  </div>
</div>
""",
        unsafe_allow_html=True,
    )
    if signals:
        for signal in signals:
            _render_signal_card(signal, compact=True)
    with st.expander("Показать историю расчёта", expanded=False):
        render_dataframe(
            [
                {"Показатель": "Расчётная оценка", "Значение": cell.suggested_score},
                {"Показатель": "Итоговая оценка", "Значение": cell.approved_score},
                {"Показатель": "Покрытие", "Значение": design.humanize(cell.coverage_status)},
                {"Показатель": "Источников", "Значение": cell.source_count},
                {"Показатель": "Ручная корректировка", "Значение": cell.override_reason or "нет"},
                {"Показатель": "Связанные сигналы", "Значение": ", ".join(cell.signal_ids)},
            ]
        )
    if cell.supporting_evidence:
        with st.expander("Поддерживающие подтверждения", expanded=False):
            render_interactive_table(cell.supporting_evidence, key=f"supporting_{cell.id}", default_sort="date")
    if cell.contradicting_evidence:
        with st.expander("Противоречащие подтверждения", expanded=False):
            render_interactive_table(cell.contradicting_evidence, key=f"contradicting_{cell.id}", default_sort="date")


def _render_current_cell_detail(session: Session, cell: CurrentMaeCell) -> None:
    matrix_cell = session.get(MatrixScore, cell.matrix_score_id) if cell.matrix_score_id else None
    signals = list(session.scalars(select(ShiftSignal).where(ShiftSignal.id.in_(matrix_cell.signal_ids))).all()) if matrix_cell and matrix_cell.signal_ids else []
    sources = sorted({url for signal in signals for url in signal.source_urls})
    financial_review = latest_review_run(session, "financial")
    st.markdown(
        f"""
<div class="mae-card">
  <div class="mae-card-title">{html.escape(design.row_key_label(cell.template_row_key))} · {html.escape(design.region_label(cell.region))}</div>
  <div class="mae-card-body">
    <b>Baseline score:</b> {html.escape(_score_text(cell.baseline_score))}<br>
    <b>Proposed adjustment:</b> {html.escape(_score_text(cell.proposed_adjustment))}<br>
    <b>Validated adjustment:</b> {html.escape(_score_text(cell.validated_adjustment))}<br>
    <b>Current score:</b> {html.escape(_score_text(cell.current_score))}<br>
    <b>Покрытие evidence:</b> {html.escape(design.humanize(cell.coverage_status))} · источников {cell.source_count}<br>
    <b>Confidence:</b> {html.escape(design.humanize(cell.confidence))} · <b>freshness:</b> {cell.freshness_score:.0%}<br>
    <b>Автоматическая диагностика:</b> {html.escape(financial_review.verdict if financial_review else 'NOT_RUN')}<br>
    <b>Источники:</b> {_source_links(sources)}
  </div>
</div>
""",
        unsafe_allow_html=True,
    )
    if signals:
        st.subheader("Связанные fresh signals")
        for signal in signals:
            _render_signal_card(signal, compact=True)
    if cell.supporting_evidence:
        with st.expander("Supporting evidence", expanded=False):
            render_interactive_table(cell.supporting_evidence, key=f"current_support_{cell.template_row_key}_{cell.region}", default_sort="date")
    if cell.contradicting_evidence:
        with st.expander("Contradicting evidence", expanded=False):
            render_interactive_table(cell.contradicting_evidence, key=f"current_contra_{cell.template_row_key}_{cell.region}", default_sort="date")
    overrides = []
    if matrix_cell:
        from app.domain.models import MatrixOverrideLog

        overrides = list(session.scalars(select(MatrixOverrideLog).where(MatrixOverrideLog.matrix_score_id == matrix_cell.id)).all())
    if overrides:
        with st.expander("Manual override history", expanded=False):
            render_dataframe(
                [
                    {
                        "Дата": design.format_date(row.created_at, with_time=True),
                        "Было": _score_text(row.previous_score),
                        "Стало": _score_text(row.new_score),
                        "Комментарий": row.reason,
                    }
                    for row in overrides
                ]
            )


def _change_filters(session: Session, changes: list[ChangeLog]) -> dict[str, Any]:
    contexts = [_change_context(session, change) for change in changes]
    regions = sorted({ctx["region"] for ctx in contexts if ctx["region"]})
    assets = sorted({ctx["asset_class"] for ctx in contexts if ctx["asset_class"]})
    sources = sorted({ctx["source"] for ctx in contexts if ctx["source"]})
    statuses = sorted({design.humanize(change.materiality) for change in changes})
    cols = st.columns(5)
    with cols[0]:
        region = st.multiselect("Регион", regions)
    with cols[1]:
        asset = st.multiselect("Класс активов", assets)
    with cols[2]:
        source = st.multiselect("Источник", sources)
    with cols[3]:
        status = st.multiselect("Статус", statuses)
    with cols[4]:
        since = st.date_input("С даты", value=date.today() - timedelta(days=90), format="DD.MM.YYYY")
    return {"region": region, "asset": asset, "source": source, "status": status, "since": since}


def _change_matches_filters(session: Session, change: ChangeLog, filters: dict[str, Any]) -> bool:
    ctx = _change_context(session, change)
    if filters["region"] and ctx["region"] not in filters["region"]:
        return False
    if filters["asset"] and ctx["asset_class"] not in filters["asset"]:
        return False
    if filters["source"] and ctx["source"] not in filters["source"]:
        return False
    if filters["status"] and design.humanize(change.materiality) not in filters["status"]:
        return False
    since = filters.get("since")
    if isinstance(since, date) and change.created_at.date() < since:
        return False
    return True


def _change_context(session: Session, change: ChangeLog) -> dict[str, str]:
    current = session.get(ResearchView, change.current_view_id)
    return {
        "region": design.region_label(current.region) if current else "",
        "asset_class": design.ASSET_LABELS.get(current.asset_class, design.humanize(current.asset_class)) if current else "",
        "source": current.article.source.institution_name if current and current.article else "",
    }


def _signal_filters(signals: list[ShiftSignal]) -> list[ShiftSignal]:
    cols = st.columns(4)
    regions = sorted({design.region_label(s.region) for s in signals})
    assets = sorted({design.row_key_label(s.template_row_key) for s in signals})
    sources = sorted({design.source_label(s.source_urls[0] if s.source_urls else "") for s in signals})
    statuses = sorted({design.humanize(s.analyst_status) for s in signals})
    with cols[0]:
        region = st.multiselect("Регион", regions)
    with cols[1]:
        asset = st.multiselect("Актив", assets)
    with cols[2]:
        source = st.multiselect("Источник", sources)
    with cols[3]:
        status = st.multiselect("Статус", statuses)
    filtered = []
    for signal in signals:
        if region and design.region_label(signal.region) not in region:
            continue
        if asset and design.row_key_label(signal.template_row_key) not in asset:
            continue
        if source and design.source_label(signal.source_urls[0] if signal.source_urls else "") not in source:
            continue
        if status and design.humanize(signal.analyst_status) not in status:
            continue
        filtered.append(signal)
    return filtered


def _scenarios_by_change(session: Session) -> dict[str, list[ScenarioCard]]:
    rows: dict[str, list[ScenarioCard]] = defaultdict(list)
    for scenario in session.scalars(select(ScenarioCard)).all():
        if scenario.linked_change_id:
            rows[scenario.linked_change_id].append(scenario)
    return rows


def _ordered_scenarios(scenarios: list[ScenarioCard]) -> list[ScenarioCard]:
    order = {"BASE": 0, "UPSIDE": 1, "DOWNSIDE": 2}
    return sorted(scenarios, key=lambda s: order.get(s.scenario_type, 9))


def _evidence_for_scenarios(session: Session, scenarios: list[ScenarioCard]) -> list[dict[str, Any]]:
    ids = [s.id for s in scenarios]
    if not ids:
        return []
    assessments = {a.scenario_id: a for a in session.scalars(select(ScenarioAssessment).where(ScenarioAssessment.scenario_id.in_(ids))).all()}
    rows = []
    for observation in session.scalars(select(EvidenceObservation).where(EvidenceObservation.scenario_id.in_(ids))).all():
        assessment = assessments.get(observation.scenario_id)
        rows.append(
            {
                "Индикатор": observation.indicator,
                "Дата": design.format_date(observation.observation_date),
                "Факт": design.user_text(observation.actual),
                "Вес": design.humanize(observation.importance),
                "Статус": design.humanize(assessment.evidence_status) if assessment else "",
                "Источник": observation.source_url,
            }
        )
    return rows


def _matrix_scores_for_mode(session: Session, mode: str) -> list[MatrixScore]:
    rows = [
        row
        for row in session.scalars(select(MatrixScore).order_by(MatrixScore.template_row_key, MatrixScore.region)).all()
        if is_applicable_canonical_cell(row.template_row_key, row.region) and not row.is_demo
    ]
    return rows


def _current_matrix_pivot(cells: list[CurrentMaeCell]) -> pd.DataFrame:
    return _cell_pivot(cells, "current_score", "—")


def _fresh_shift_pivot(cells: list[CurrentMaeCell]) -> pd.DataFrame:
    return _cell_pivot(cells, "validated_adjustment", "—")


def _coverage_pivot(cells: list[CurrentMaeCell]) -> pd.DataFrame:
    data = [
        {
            "Категория": design.row_key_label(cell.template_row_key),
            "Регион": design.region_label(cell.region),
            "Статус": design.humanize(cell.coverage_status),
        }
        for cell in cells
    ]
    return _pivot_from_rows(data, "Статус")


def _cell_pivot(cells: list[CurrentMaeCell], field: str, empty: str) -> pd.DataFrame:
    data = []
    for cell in cells:
        value = getattr(cell, field)
        data.append(
            {
                "Категория": design.row_key_label(cell.template_row_key),
                "Регион": design.region_label(cell.region),
                "Значение": value if value is not None else empty,
            }
        )
    return _pivot_from_rows(data, "Значение")


def _pivot_from_rows(rows: list[dict[str, Any]], value_column: str) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame()
    pivot = df.pivot_table(index="Категория", columns="Регион", values=value_column, aggfunc="first").fillna("—")
    order = ["Глобально", "США", "Европа", "Великобритания", "Япония", "EM без Китая"]
    columns = [column for column in order if column in pivot.columns] + [column for column in pivot.columns if column not in order]
    return pivot.reindex(columns=columns)


def _current_mae_user_rows(cells: list[CurrentMaeCell]) -> list[dict[str, Any]]:
    return [
        {
            "Категория": design.row_key_label(cell.template_row_key),
            "Регион": design.region_label(cell.region),
            "Baseline": _score_text(cell.baseline_score),
            "Proposed shift": _score_text(cell.proposed_adjustment),
            "Validated shift": _score_text(cell.validated_adjustment),
            "Current score": _score_text(cell.current_score),
            "Статус": design.humanize(cell.coverage_status),
            "Источников": cell.source_count,
            "Свежесть": f"{cell.freshness_score:.0%}",
        }
        for cell in cells
    ]


def _fresh_shift_user_rows(cells: list[CurrentMaeCell]) -> list[dict[str, Any]]:
    return [
        {
            "Категория": design.row_key_label(cell.template_row_key),
            "Регион": design.region_label(cell.region),
            "Baseline": _score_text(cell.baseline_score),
            "Proposed shift": _score_text(cell.proposed_adjustment),
            "Validated shift": _score_text(cell.validated_adjustment),
            "Current score": _score_text(cell.current_score),
            "Статус": design.humanize(cell.coverage_status),
            "Источников": cell.source_count,
            "Свежесть": f"{cell.freshness_score:.0%}",
        }
        for cell in cells
    ]


def _coverage_user_rows(cells: list[CurrentMaeCell]) -> list[dict[str, Any]]:
    return [
        {
            "Категория": design.row_key_label(cell.template_row_key),
            "Регион": design.region_label(cell.region),
            "Статус": design.humanize(cell.coverage_status),
            "Источников": cell.source_count,
            "Публикаций": len(cell.publication_ids),
            "Свежесть": f"{cell.freshness_score:.0%}",
            "Current score": _score_text(cell.current_score),
        }
        for cell in cells
    ]


def _default_matrix_rows(rows: list[MatrixScore]) -> list[MatrixScore]:
    return [m for m in rows if m.approved_score is not None or m.suggested_score is not None or bool(m.signal_ids) or bool(m.override_reason)]


def _matrix_summary(rows: list[MatrixScore], session: Session) -> dict[str, int]:
    pending_signals = len([s for s in session.scalars(select(ShiftSignal)).all() if s.analyst_status in {"NEW", "NEEDS_REVIEW"}])
    return {
        "total": len(rows),
        "covered": len([m for m in rows if m.coverage_status == CoverageStatus.COVERED.value and m.approved_score is not None]),
        "no_data": len([m for m in rows if m.coverage_status == CoverageStatus.NO_DATA.value]),
        "insufficient": len([m for m in rows if m.coverage_status == CoverageStatus.INSUFFICIENT_EVIDENCE.value]),
        "active": len([m for m in rows if m.approved_score not in (None, 0)]),
        "positive": len([m for m in rows if m.approved_score is not None and m.approved_score > 0]),
        "negative": len([m for m in rows if m.approved_score is not None and m.approved_score < 0]),
        "review": pending_signals,
    }


def _matrix_pivot(rows: list[MatrixScore]) -> pd.DataFrame:
    data = []
    for m in rows:
        data.append(
            {
                "Категория": design.row_key_label(m.template_row_key),
                "Регион": design.region_label(m.region),
                "Текущий сигнал": m.approved_score if m.approved_score is not None else "—",
            }
        )
    df = pd.DataFrame(data)
    pivot = df.pivot_table(index="Категория", columns="Регион", values="Текущий сигнал", aggfunc="first").fillna("—")
    order = ["Глобально", "США", "Европа", "Великобритания", "Япония", "EM без Китая"]
    columns = [column for column in order if column in pivot.columns] + [column for column in pivot.columns if column not in order]
    return pivot.reindex(columns=columns)


def _matrix_user_rows(session: Session, rows: list[MatrixScore]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        signals = list(session.scalars(select(ShiftSignal).where(ShiftSignal.id.in_(row.signal_ids))).all()) if row.signal_ids else []
        sources = sorted({design.source_label(url) for signal in signals for url in signal.source_urls})
        result.append(
            {
                "Категория": design.row_key_label(row.template_row_key),
                "Регион": design.region_label(row.region),
                "Текущий сигнал": _score_text(row.approved_score),
                "Источники": ", ".join(sources) or "нет активных источников",
                "Покрытие": f"{row.source_count} источн. · {design.humanize(row.coverage_status)}",
                "Обновлено": design.format_date(row.updated_at, with_time=True),
                "Статус": "ручная корректировка" if row.override_reason else design.humanize(row.coverage_status),
            }
        )
    return result


def _score_text(score: int | None) -> str:
    if score is None:
        return "—"
    return f"{score:+d}" if score > 0 else str(score)


def _score_delta(previous: ResearchView | None, current: ResearchView | None) -> int:
    previous_score = previous.position_score if previous and previous.position_score is not None else 0
    current_score = current.position_score if current and current.position_score is not None else 0
    return max(-3, min(3, int(current_score) - int(previous_score)))


def _score_delta_for_change(session: Session, change: ChangeLog) -> int:
    current = session.get(ResearchView, change.current_view_id)
    previous = session.get(ResearchView, change.previous_view_id) if change.previous_view_id else None
    return _score_delta(previous, current)


def _view_direction(view: ResearchView | None) -> str:
    if view is None:
        return "нет предыдущего взгляда"
    score = f"{view.position_score:+d}" if view.position_score and view.position_score > 0 else str(view.position_score or 0)
    return f"{design.humanize(view.direction)} ({score})"


def _view_sentence(view: ResearchView | None) -> str:
    if view is None:
        return "нет данных"
    drivers = "; ".join(view.drivers[:3])
    return design.user_text(f"{design.humanize(view.direction)}, оценка {view.position_score or 0}. Драйверы: {drivers or 'не указаны'}.")


def _source_links(urls: list[str]) -> str:
    links = [design.link_html(url) for url in urls[:5]]
    return ", ".join(links) if links else "источники не указаны"


# Product-facing pages. They intentionally sit after the legacy/admin page
# helpers so the public navigation can stay compact without deleting diagnostic
# tooling that is still useful behind the admin switch.
def overview_page(session: Session, settings: Settings) -> None:
    st.title("Рыночная картина")
    overview = build_market_overview(session)
    baseline = active_baseline_snapshot(session)
    st.markdown(
        design.editorial_intro(
            "Сводка сформирована автоматически на основе доступных публикаций, сценариев и рыночных подтверждений. "
            "Она показывает не внутренние статусы, а возможное развитие ситуации и влияние на активы.",
            "Scenario evolution",
            [
                ("обновлено", design.format_date(overview.last_updated, with_time=True) if overview.last_updated else "нет свежих сигналов"),
                ("baseline", _baseline_date_text(baseline)),
                ("основных сигналов", overview.published_signals),
                ("watchlist", overview.watchlist_signals),
            ],
        ),
        unsafe_allow_html=True,
    )

    if not overview.themes:
        design.empty_state(
            "Пока нет опубликованных рыночных тем.",
            "Baseline-матрица доступна, но для сценарного вывода нужны ясный тезис, transmission logic и подтверждения из источников.",
        )
        with st.expander("Добавить публикацию", expanded=False):
            _render_compact_publication_input(session, settings, key_prefix="overview")
        return

    st.subheader("Главные рыночные темы")
    columns = st.columns(2)
    for idx, theme in enumerate(overview.themes):
        with columns[idx % 2]:
            st.markdown(_theme_card_html(theme), unsafe_allow_html=True)

    left, right = st.columns([1.15, 0.85], gap="large")
    with left:
        st.subheader("Что изменилось")
        if overview.scenario_changes:
            for signal in overview.scenario_changes:
                st.markdown(_market_signal_card_html(signal, compact=True), unsafe_allow_html=True)
        else:
            design.empty_state("Существенных сценарных изменений нет.", "Текущая MAE остаётся привязанной к baseline.")
    with right:
        st.subheader("Winners / Vulnerable assets")
        st.markdown(
            _two_column_list_html("Потенциальные winners", overview.winners, "Активы под давлением", overview.vulnerable_assets),
            unsafe_allow_html=True,
        )
        st.subheader("Ключевые риски")
        st.markdown(design.list_html(overview.key_risks) if overview.key_risks else "Недостаточно данных.", unsafe_allow_html=True)
        st.subheader("Источники последнего среза")
        st.markdown(_source_links(overview.main_sources), unsafe_allow_html=True)

    with st.expander("Добавить публикацию", expanded=False):
        _render_compact_publication_input(session, settings, key_prefix="overview_bottom")


def changes_and_scenarios_page(session: Session, settings: Settings) -> None:
    st.title("Сигналы и сценарии")
    signals = build_market_signals(session)
    st.markdown(
        design.editorial_intro(
            "Карточки показывают автоматический сценарный вывод: что изменилось, какие варианты развития важны, "
            "что может усилить или отменить идею и как это связано с MAE.",
            "Market signals",
            [
                ("опубликовано", len([s for s in signals if s.outcome == PUBLISHED_SIGNAL])),
                ("watchlist", len([s for s in signals if s.outcome == WATCHLIST])),
                ("всего карточек", len(signals)),
            ],
        ),
        unsafe_allow_html=True,
    )
    if not signals:
        design.empty_state(
            "Сценарных сигналов пока нет.",
            "Материал попадёт сюда только если содержит инвестиционный тезис, сценарную логику и источники для проверки.",
        )
        return

    filtered = _market_signal_filters(signals)
    if not filtered:
        design.empty_state("По выбранным фильтрам карточек нет.", "Измените актив, регион, тему, confidence или evidence status.")
        return

    for signal in filtered:
        st.markdown(_market_signal_card_html(signal), unsafe_allow_html=True)
        if signal.scenarios:
            st.markdown(
                "<div class='mae-grid-3'>" + "".join(_scenario_view_html(scenario) for scenario in signal.scenarios) + "</div>",
                unsafe_allow_html=True,
            )


def matrix_page(session: Session, settings: Settings) -> None:
    st.title("Матрица MAE")
    cells = build_mae_matrix_view(session)
    baseline = active_baseline_snapshot(session)
    active_adjustments = len([cell for cell in cells if cell.automatic_adjustment not in (None, 0)])
    sourced_cells = len([cell for cell in cells if cell.source_count > 0])
    st.markdown(
        design.editorial_intro(
            "Матрица показывает baseline и автоматические scenario-based adjustments. "
            "Если уверенности или подтверждений недостаточно, adjustment остаётся нулевым и baseline сохраняется.",
            "Current MAE",
            [
                ("baseline", _baseline_date_text(baseline)),
                ("ячеек", len(cells)),
                ("scenario adjustments", active_adjustments),
                ("с источниками", sourced_cells),
            ],
        ),
        unsafe_allow_html=True,
    )

    render_matrix_table(_product_matrix_pivot(cells))
    rows = _product_matrix_rows(cells)
    filtered = render_interactive_table(
        rows,
        key="product_matrix",
        filter_columns=("Регион", "Confidence", "Статус данных"),
        default_sort="Категория",
        empty_message="Матрица пока пуста.",
    )
    labels = {
        f"{row['Категория']} · {row['Регион']} · {row['Current score']}": row["canonical_cell_id"]
        for row in rows
    }
    if labels:
        selected = st.selectbox("Открыть объяснение ячейки", list(labels), key="product_matrix_detail")
        cell = next((row for row in cells if row.canonical_cell_id == labels[selected]), None)
        if cell:
            st.markdown(_product_matrix_detail_html(cell), unsafe_allow_html=True)

    with st.expander("Скачать результаты", expanded=False):
        exporter = ExportService()
        col1, col2, col3 = st.columns(3)
        col1.download_button(
            "Excel",
            exporter.export_xlsx_bytes(session),
            "mae_export.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )
        col2.download_button("CSV", exporter.export_csv_bytes(session), "mae_matrix_export.csv", "text/csv", use_container_width=True)
        col3.download_button("JSON", exporter.export_json_bytes(session), "mae_export.json", "application/json", use_container_width=True)
        st.caption("Экспорт формируется из текущих данных приложения; API-вызовы при скачивании не выполняются.")


def _render_compact_publication_input(session: Session, settings: Settings, *, key_prefix: str) -> None:
    editable = can_edit(settings)
    if not editable:
        st.info(disabled_hint(settings))
        return
    tab_url, tab_text = st.tabs(["Ссылка", "Текст"])
    with tab_url:
        source_name = st.text_input("Источник", key=f"{key_prefix}_url_source", placeholder="Название института")
        url = st.text_input("URL", key=f"{key_prefix}_url")
        pub_date = st.date_input("Дата", value=date.today(), key=f"{key_prefix}_url_date", format="DD.MM.YYYY")
        if st.button("Импортировать и проанализировать", type="primary", key=f"{key_prefix}_url_button", use_container_width=True):
            if not source_name.strip() or not url.strip():
                st.warning("Укажите источник и URL.")
            else:
                try:
                    article = import_article_url(session, url, source_name, pub_date)
                    result = _analyze_article_with_status(session, article, settings)
                    session.commit()
                    _render_pipeline_result(article, result)
                except Exception:  # noqa: BLE001
                    report_action_error(session, "product_publication_url_failed", "Не удалось импортировать и проанализировать публикацию.")
    with tab_text:
        title = st.text_input("Заголовок", key=f"{key_prefix}_text_title")
        source_name = st.text_input("Источник", key=f"{key_prefix}_text_source", placeholder="Название института")
        pub_date = st.date_input("Дата", value=date.today(), key=f"{key_prefix}_text_date", format="DD.MM.YYYY")
        text = st.text_area("Текст публикации", height=220, key=f"{key_prefix}_text")
        if st.button("Сохранить и проанализировать", type="primary", key=f"{key_prefix}_text_button", use_container_width=True):
            if not source_name.strip() or not text.strip():
                st.warning("Добавьте источник и текст публикации.")
            else:
                try:
                    article = import_article_text(session, title or "Публикация без заголовка", source_name, pub_date, text)
                    result = _analyze_article_with_status(session, article, settings)
                    session.commit()
                    _render_pipeline_result(article, result)
                except Exception:  # noqa: BLE001
                    report_action_error(session, "product_publication_text_failed", "Не удалось сохранить и проанализировать публикацию.")


def _theme_card_html(theme: Any) -> str:
    return f"""
<div class="mae-card">
  <div class="mae-kicker">{html.escape(design.region_label(theme.region))} · {html.escape(_row_label(theme.primary_asset))}</div>
  <div class="mae-card-title">{html.escape(design.compact_text(design.user_text(theme.topic), 120))}</div>
  <div class="mae-card-body">
    <b>Базовый сценарий:</b> {html.escape(design.compact_text(design.user_text(theme.base_scenario), 180))}<br>
    <b>Вероятность:</b> {html.escape(_probability_label(theme.probability))}<br>
    <b>Что изменилось:</b> {html.escape(design.compact_text(design.user_text(theme.what_changed), 220))}<br>
    <b>Trigger:</b> {html.escape(design.compact_text(design.user_text(theme.trigger), 150))}<br>
    <b>Veto:</b> {html.escape(design.compact_text(design.user_text(theme.veto), 150))}
  </div>
  <div style="margin-top:.65rem;display:flex;gap:.35rem;flex-wrap:wrap;">
    {design.badge('confidence: ' + _confidence_label(theme.confidence), 'positive' if theme.confidence == 'HIGH' else 'warning')}
    {design.badge('evidence: ' + _evidence_label(theme.evidence_status), 'neutral')}
  </div>
  <div class="mae-kicker" style="margin-top:.7rem;">Affected assets</div>
  {design.list_html([_row_label(asset) for asset in theme.affected_assets[:4]])}
  <div class="mae-kicker" style="margin-top:.7rem;">Sources</div>
  {_source_links(theme.sources)}
</div>
"""


def _market_signal_card_html(signal: MarketSignalView, compact: bool = False) -> str:
    limit = 170 if compact else 260
    return f"""
<div class="mae-card">
  <div style="display:flex;justify-content:space-between;gap:.8rem;align-items:flex-start;flex-wrap:wrap;">
    <div>
      <div class="mae-kicker">{html.escape(design.region_label(signal.region))} · {html.escape(_row_label(signal.primary_asset))}</div>
      <div class="mae-card-title">{html.escape(design.compact_text(design.user_text(signal.topic), 140))}</div>
    </div>
    <div>{design.badge(_outcome_label(signal.outcome), 'positive' if signal.outcome == PUBLISHED_SIGNAL else 'warning')}</div>
  </div>
  <div class="mae-card-body" style="margin-top:.55rem;">
    <b>Investment view:</b> {html.escape(design.compact_text(design.user_text(signal.current_view), limit))}<br>
    <b>What changed:</b> {html.escape(design.compact_text(design.user_text(signal.what_changed), limit))}<br>
    <b>Probability balance:</b> {html.escape(design.user_text(signal.probability_balance))}<br>
    <b>Transmission:</b> {html.escape(design.compact_text(design.user_text(signal.transmission_chain), limit))}<br>
    <b>MAE implication:</b> {html.escape(_score_text(signal.mae_implication)) if signal.mae_implication else '0, baseline сохраняется'}
  </div>
  <div class="mae-grid-2" style="margin-top:.7rem;">
    <div class="mae-mini-panel"><div class="mae-kicker">Trigger</div>{html.escape(design.compact_text(design.user_text(signal.trigger), 160))}</div>
    <div class="mae-mini-panel"><div class="mae-kicker">Veto</div>{html.escape(design.compact_text(design.user_text(signal.veto), 160))}</div>
  </div>
  <div style="margin-top:.65rem;display:flex;gap:.35rem;flex-wrap:wrap;">
    {design.badge('confidence: ' + _confidence_label(signal.confidence), 'positive' if signal.confidence == 'HIGH' else 'warning')}
    {design.badge('evidence: ' + _evidence_label(signal.evidence_status), 'neutral')}
    {design.badge('updated: ' + (design.format_date(signal.updated_at) if signal.updated_at else 'нет даты'), 'neutral')}
  </div>
  <div class="mae-kicker" style="margin-top:.7rem;">Sources</div>
  {_source_links(signal.supporting_sources)}
</div>
"""


def _scenario_view_html(scenario: ScenarioView) -> str:
    return f"""
<div class="mae-mini-panel">
  <div class="mae-kicker">{html.escape(design.humanize(scenario.scenario_type))} · {html.escape(_probability_label(scenario.probability))}</div>
  <div style="font-weight:760;margin-bottom:.35rem;">{html.escape(design.compact_text(design.user_text(scenario.title), 120))}</div>
  <div class="mae-card-body">{html.escape(design.compact_text(design.user_text(scenario.narrative), 190))}</div>
  <div class="mae-kicker" style="margin-top:.55rem;">Expected asset effects</div>
  <div class="mae-card-body">{html.escape(design.compact_text(design.user_text(scenario.expected_reaction), 180))}</div>
  <div class="mae-kicker" style="margin-top:.55rem;">Triggers</div>{design.list_html(scenario.triggers[:3])}
  <div class="mae-kicker" style="margin-top:.55rem;">Reversal conditions</div>{design.list_html(scenario.reversal_conditions[:3])}
  <div style="margin-top:.6rem;">{design.badge(_evidence_label(scenario.evidence_status), 'neutral')}</div>
</div>
"""


def _market_signal_filters(signals: list[MarketSignalView]) -> list[MarketSignalView]:
    cols = st.columns(5)
    assets = sorted({_row_label(signal.primary_asset) for signal in signals})
    regions = sorted({design.region_label(signal.region) for signal in signals})
    themes = sorted({design.compact_text(design.user_text(signal.topic), 80) for signal in signals})
    confidences = sorted({_confidence_label(signal.confidence) for signal in signals})
    evidence = sorted({_evidence_label(signal.evidence_status) for signal in signals})
    with cols[0]:
        asset_filter = st.multiselect("Актив", assets)
    with cols[1]:
        region_filter = st.multiselect("Регион", regions)
    with cols[2]:
        theme_filter = st.multiselect("Тема", themes)
    with cols[3]:
        confidence_filter = st.multiselect("Confidence", confidences)
    with cols[4]:
        evidence_filter = st.multiselect("Evidence", evidence)
    filtered: list[MarketSignalView] = []
    for signal in signals:
        if asset_filter and _row_label(signal.primary_asset) not in asset_filter:
            continue
        if region_filter and design.region_label(signal.region) not in region_filter:
            continue
        if theme_filter and design.compact_text(design.user_text(signal.topic), 80) not in theme_filter:
            continue
        if confidence_filter and _confidence_label(signal.confidence) not in confidence_filter:
            continue
        if evidence_filter and _evidence_label(signal.evidence_status) not in evidence_filter:
            continue
        filtered.append(signal)
    return filtered


def _product_matrix_pivot(cells: list[Any]) -> pd.DataFrame:
    data = [
        {
            "Категория": design.row_key_label(cell.template_row_key),
            "Регион": design.region_label(cell.region),
            "Current score": cell.current_score if cell.current_score is not None else "—",
        }
        for cell in cells
    ]
    return _pivot_from_rows(data, "Current score")


def _product_matrix_rows(cells: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "canonical_cell_id": cell.canonical_cell_id,
            "Категория": design.row_key_label(cell.template_row_key),
            "Регион": design.region_label(cell.region),
            "Baseline": _score_text(cell.baseline_score),
            "Авто-сценарий": _score_text(cell.automatic_adjustment),
            "Current score": _score_text(cell.current_score),
            "Confidence": _confidence_label(cell.confidence),
            "Статус данных": _coverage_label(cell.coverage_status),
            "Источников": cell.source_count,
            "Связанная тема": design.compact_text(design.user_text(cell.linked_topic), 90) if cell.linked_topic else "baseline",
            "Обновлено": design.format_date(cell.last_updated, with_time=True) if cell.last_updated else "",
        }
        for cell in cells
    ]


def _product_matrix_detail_html(cell: Any) -> str:
    return f"""
<div class="mae-card">
  <div class="mae-card-title">{html.escape(design.row_key_label(cell.template_row_key))} · {html.escape(design.region_label(cell.region))}</div>
  <div class="mae-card-body">
    <b>Baseline:</b> {html.escape(_score_text(cell.baseline_score))}<br>
    <b>Automatic scenario-based adjustment:</b> {html.escape(_score_text(cell.automatic_adjustment))}<br>
    <b>Current score:</b> {html.escape(_score_text(cell.current_score))}<br>
    <b>Финансовая причина:</b> {html.escape(design.compact_text(design.user_text(cell.reason), 360))}<br>
    <b>Связанная тема:</b> {html.escape(design.user_text(cell.linked_topic or 'baseline'))}<br>
    <b>Confidence:</b> {html.escape(_confidence_label(cell.confidence))} · <b>данные:</b> {html.escape(_coverage_label(cell.coverage_status))}<br>
    <b>Источников:</b> {cell.source_count}<br>
    <b>Обновлено:</b> {html.escape(design.format_date(cell.last_updated, with_time=True) if cell.last_updated else 'нет даты')}
  </div>
</div>
"""


def _two_column_list_html(left_title: str, left_items: list[str], right_title: str, right_items: list[str]) -> str:
    return f"""
<div class="mae-grid-2">
  <div class="mae-mini-panel"><div class="mae-kicker">{html.escape(left_title)}</div>{design.list_html(left_items) if left_items else 'Недостаточно данных.'}</div>
  <div class="mae-mini-panel"><div class="mae-kicker">{html.escape(right_title)}</div>{design.list_html(right_items) if right_items else 'Недостаточно данных.'}</div>
</div>
"""


def _confidence_label(value: str) -> str:
    return {"HIGH": "высокий", "MEDIUM": "средний", "LOW": "низкий"}.get(value, design.humanize(value))


def _evidence_label(value: str) -> str:
    return {
        "CONFIRMED": "поддерживается данными",
        "MIXED": "смешанные подтверждения",
        "CONTRADICTED": "противоречит новым данным",
        "INSUFFICIENT_DATA": "недостаточно данных",
        "INSUFFICIENT_EVIDENCE": "недостаточно подтверждений",
    }.get(value, design.humanize(value))


def _coverage_label(value: str) -> str:
    return {
        "NO_DATA": "нет scenario-based сигнала",
        "INSUFFICIENT_EVIDENCE": "данных недостаточно",
        "COVERED": "есть источники",
        "STALE": "требует обновления данных",
    }.get(value, design.humanize(value))


def _probability_label(value: str) -> str:
    return {
        "HIGH": "высокая",
        "MEDIUM": "средняя",
        "LOW": "низкая",
    }.get(value, value or "недостаточно данных")


def _outcome_label(value: str) -> str:
    return {
        PUBLISHED_SIGNAL: "опубликованный сигнал",
        WATCHLIST: "watchlist",
    }.get(value, "не опубликовано")


def _row_label(value: str) -> str:
    return design.row_key_label(value) if "|" in str(value or "") else design.user_text(str(value or ""))


# Final product-facing pages for point-in-time MAE history. These definitions
# intentionally override earlier transitional page functions in this module.
def overview_page(session: Session, settings: Settings) -> None:
    overview = build_market_overview(session)
    data_date = overview.data_date.date() if overview.data_date else date(2026, 7, 12)
    st.title("Рыночная картина")
    st.markdown(
        design.editorial_intro(
            "Среднесрочная оценка рынков на горизонте 6–12 месяцев на основе институциональных прогнозов.",
            "Срез данных",
            [("данные на", design.format_date(data_date))],
        ),
        unsafe_allow_html=True,
    )

    left, right = st.columns([1.05, 0.95], gap="large")
    with left:
        st.subheader("Что изменилось")
        if overview.scenario_changes:
            for item in overview.scenario_changes[:4]:
                st.markdown(_simple_change_card(item), unsafe_allow_html=True)
        else:
            st.info("С прошлого среза подтверждённых изменений MAE не выявлено.")

        st.subheader("Динамика MAE с января 2026 года")
        st.markdown(_history_chart_html(overview.history_series), unsafe_allow_html=True)

    with right:
        if overview.themes:
            st.subheader("Основные темы")
            for theme in overview.themes[:5]:
                st.markdown(_simple_theme_card(theme), unsafe_allow_html=True)

        if overview.winners:
            st.subheader("Потенциально сильные активы")
            st.markdown(design.list_html(overview.winners[:3]), unsafe_allow_html=True)

        if overview.key_risks:
            st.subheader("Основные риски")
            st.markdown(design.list_html(overview.key_risks[:3]), unsafe_allow_html=True)

        if overview.main_sources:
            with st.expander("Источники среза", expanded=False):
                st.markdown(design.list_html(overview.main_sources[:8]), unsafe_allow_html=True)


def changes_and_scenarios_page(session: Session, settings: Settings) -> None:
    st.title("Сигналы и сценарии")
    st.markdown(
        design.editorial_intro(
            "Раздел показывает только сценарии с понятной финансовой логикой: что происходит, почему это важно и какие активы затронуты.",
            "Сценарная карта",
            [("горизонт", "6–12 месяцев")],
        ),
        unsafe_allow_html=True,
    )
    overview = build_market_overview(session)
    signals = overview.scenario_changes[:8] or [
        signal
        for signal in build_market_signals(session)
        if signal.scenarios and len(set(signal.supporting_sources)) >= 2
    ]
    if not signals:
        st.info("Содержательных рыночных векторов пока нет. Они появятся после расчёта минимум двух независимых компонентов.")
        return

    for signal in signals:
        st.markdown(_scenario_signal_card(signal), unsafe_allow_html=True)
        if signal.scenarios:
            st.markdown(
                "<div class='mae-grid-3'>" + "".join(_scenario_clean_html(scenario) for scenario in signal.scenarios) + "</div>",
                unsafe_allow_html=True,
            )


def matrix_page(session: Session, settings: Settings) -> None:
    st.title("Матрица MAE")
    cells = build_mae_matrix_view(session)
    latest_date = max((item["date"] for cell in cells for item in cell.history), default=None)
    st.markdown(
        design.editorial_intro(
            "Компактная карта среднесрочных оценок. Пустые ячейки означают, что независимых подтверждений пока недостаточно.",
            "Срез данных",
            [("данные на", design.format_date(latest_date) if latest_date else "история не сформирована")],
        ),
        unsafe_allow_html=True,
    )
    comparable_cells = [cell for cell in cells if cell.coverage_status != "NOT_APPLICABLE"]
    if comparable_cells and all(cell.previous_score is None for cell in comparable_cells):
        st.info("Это первый сопоставимый срез. Динамика появится после следующего обновления.")
    buckets = [
        ("Акции", "EQUITY"),
        ("Ставки", "GOVERNMENT_BONDS"),
        ("Кредит", "CORPORATE_CREDIT"),
        ("Защитные активы", "GOLD"),
    ]
    tabs = st.tabs([label for label, _ in buckets])
    for tab, (_, bucket) in zip(tabs, buckets):
        with tab:
            bucket_cells = [cell for cell in cells if not cell.asset_bucket or cell.asset_bucket == bucket]
            if not bucket_cells:
                st.info("Для этого блока пока нет применимых ячеек core universe.")
                continue
            st.markdown(_mae_heatmap_html(bucket_cells), unsafe_allow_html=True)
            labels = {
                f"{design.row_key_label(cell.template_row_key)} · {design.region_label(cell.region)}": cell.canonical_cell_id
                for cell in bucket_cells
                if cell.coverage_status != "NOT_APPLICABLE"
            }
            if labels:
                selected = st.selectbox("Подробности ячейки", list(labels), key=f"mae_history_cell_{bucket}")
                cell = next((row for row in bucket_cells if row.canonical_cell_id == labels[selected]), None)
                if cell:
                    st.markdown(_mae_cell_detail_html(cell), unsafe_allow_html=True)


def _simple_change_card(signal: MarketSignalView) -> str:
    return f"""
<div class="mae-card">
  <div class="mae-kicker">{html.escape(design.region_label(signal.region))}</div>
  <div class="mae-card-title">{html.escape(_row_label(signal.primary_asset))}</div>
  <div class="mae-card-body">{html.escape(design.user_text(signal.what_changed))}</div>
</div>
"""


def _simple_theme_card(theme: Any) -> str:
    effects = theme.main_effects[:1] if theme.main_effects else [theme.what_changed]
    return f"""
<div class="mae-mini-panel" style="margin-bottom:.6rem;">
  <div class="mae-kicker">{html.escape(design.region_label(theme.region))}</div>
  <div style="font-weight:760;">{html.escape(_row_label(theme.primary_asset))}</div>
  <div class="mae-card-body">{html.escape(design.compact_text(design.user_text(effects[0]), 180))}</div>
</div>
"""


def _history_chart_html(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "<div class='mae-card-body'>Исторические срезы пока не сформированы.</div>"
    use_scored = "scored" in rows[0]
    max_value = max(
        max(row.get("positive", 0), row.get("neutral", 0), row.get("negative", 0), row.get("no_data", 0), row.get("scored", 0))
        for row in rows
    ) or 1
    lines = []
    for row in rows:
        label = design.format_date(row["date"])
        segments = [
            ("Позитивные", row.get("positive", 0), "#7DAE5E"),
            ("Нейтральные", row.get("neutral", 0), "#D7D1BD"),
            ("Негативные", row.get("negative", 0), "#C9675A"),
        ]
        if not use_scored:
            segments.append(("Нет данных", row.get("no_data", 0), "#A8A398"))
        bars = "".join(
            f"<div title='{html.escape(name)}: {value}' style='width:{max(2, value / max_value * 100):.1f}%;background:{color};height:8px;margin:.15rem 0;'></div>"
            for name, value, color in segments
        )
        text = " · ".join(f"{name}: {value}" for name, value, _ in segments)
        lines.append(
            f"<div class='mae-history-row'><div class='mae-kicker'>{html.escape(label)}</div><div style='flex:1;'>{bars}</div><div class='mae-muted'>{html.escape(text)}</div></div>"
        )
    return "<div class='mae-card'>" + "".join(lines) + "</div>"


def _scenario_signal_card(signal: MarketSignalView) -> str:
    winners = _scenario_assets(signal, positive=True)
    vulnerable = _scenario_assets(signal, positive=False)
    return f"""
<div class="mae-card">
  <div class="mae-kicker">{html.escape(design.region_label(signal.region))} · {html.escape(_row_label(signal.primary_asset))}</div>
  <div class="mae-card-title">{html.escape(design.compact_text(design.user_text(signal.what_changed), 160))}</div>
  <div class="mae-grid-2" style="margin-top:.7rem;">
    <div class="mae-mini-panel"><div class="mae-kicker">Что происходит?</div>{html.escape(design.compact_text(design.user_text(signal.current_view), 180))}</div>
    <div class="mae-mini-panel"><div class="mae-kicker">Почему это важно?</div>{html.escape(design.compact_text(design.user_text(signal.transmission_chain), 220))}</div>
  </div>
  <div class="mae-grid-2" style="margin-top:.7rem;">
    <div class="mae-mini-panel"><div class="mae-kicker">Могут выиграть</div>{design.list_html(winners)}</div>
    <div class="mae-mini-panel"><div class="mae-kicker">Под давлением</div>{design.list_html(vulnerable)}</div>
  </div>
</div>
"""


def _scenario_clean_html(scenario: ScenarioView) -> str:
    status = _scenario_status_label(scenario)
    return f"""
<div class="mae-mini-panel">
  <div class="mae-kicker">{html.escape(design.humanize(scenario.scenario_type))} · {html.escape(status)}</div>
  <div style="font-weight:760;margin-bottom:.35rem;">{html.escape(design.compact_text(design.user_text(scenario.title), 110))}</div>
  <div class="mae-card-body">{html.escape(design.compact_text(design.user_text(scenario.narrative), 180))}</div>
  <div class="mae-kicker" style="margin-top:.55rem;">Что подтвердит сценарий</div>{design.list_html(scenario.triggers[:3])}
  <div class="mae-kicker" style="margin-top:.55rem;">Что отменит сценарий</div>{design.list_html(scenario.reversal_conditions[:3])}
</div>
"""


def _mae_heatmap_html(cells: list[Any]) -> str:
    regions = ["Global", "US", "Europe", "UK", "Japan", "EM ex China"]
    row_keys = []
    for cell in cells:
        if cell.template_row_key not in row_keys:
            row_keys.append(cell.template_row_key)
    by_key = {(cell.template_row_key, cell.region): cell for cell in cells}
    header = "".join(f"<th>{html.escape(design.region_label(region))}</th>" for region in regions)
    body = []
    for row_key in row_keys:
        cells_html = []
        for region in regions:
            cell = by_key.get((row_key, region))
            cells_html.append(_heatmap_cell_html(cell))
        body.append(f"<tr><th class='mae-sticky-row'>{html.escape(design.row_key_label(row_key))}</th>{''.join(cells_html)}</tr>")
    return f"""
<div class="mae-heatmap-wrap">
  <table class="mae-heatmap">
    <thead><tr><th class="mae-sticky-corner">Актив</th>{header}</tr></thead>
    <tbody>{''.join(body)}</tbody>
  </table>
</div>
"""


def _heatmap_cell_html(cell: Any | None) -> str:
    if cell is None or cell.coverage_status == "NOT_APPLICABLE":
        return "<td class='mae-cell-na'>—</td>"
    if cell.score is None:
        return "<td class='mae-cell-none'>н/д</td>"
    score = int(cell.score)
    cls = "mae-cell-pos" if score > 0 else "mae-cell-neg" if score < 0 else "mae-cell-zero"
    return f"<td class='{cls}'>{score:+d}</td>" if score > 0 else f"<td class='{cls}'>{score}</td>"


def _mae_cell_detail_html(cell: Any) -> str:
    sources = _sources_detail_html(cell.source_details)
    history = _cell_history_html(cell.history)
    change = _cell_detail_change_text(cell)
    score = "недостаточно данных" if cell.score is None else _score_text(cell.score)
    previous = "в предыдущем срезе данных было недостаточно" if cell.previous_score is None else _score_text(cell.previous_score)
    components = _component_breakdown_html(cell)
    return f"""
<div class="mae-card">
  <div class="mae-card-title">{html.escape(design.row_key_label(cell.template_row_key))} · {html.escape(design.region_label(cell.region))}</div>
  <div class="mae-card-body">
    <b>Оценка:</b> {html.escape(score)}<br>
    <b>Предыдущий срез:</b> {html.escape(previous)}<br>
    <b>Изменение:</b> {html.escape(change)}<br>
    <b>Аналитических источников:</b> {cell.independent_source_count if hasattr(cell, 'independent_source_count') else len(cell.source_details)}<br>
    <b>Краткое объяснение:</b> {html.escape(design.user_text(cell.reason))}
  </div>
  {components}
  <div class="mae-kicker" style="margin-top:.8rem;">История сопоставимых срезов</div>
  {history}
  <div class="mae-kicker" style="margin-top:.8rem;">Источники</div>
  {sources}
</div>
"""


def _sources_detail_html(sources: list[dict[str, Any]]) -> str:
    if not sources:
        return (
            "<div class='mae-card-body'>В утверждённом списке источников не найдено достаточно релевантных "
            "среднесрочных материалов с действующим сроком актуальности.</div>"
        )
    items = []
    for source in sources[:8]:
        date_text = design.format_date(source.get("publication_date"))
        title = design.user_text(source.get("title") or "Отчёт без названия")
        provider = design.user_text(source.get("provider") or "Источник не указан")
        classification = source.get("direct_or_semi_direct") or source.get("direct_or_inherited") or ""
        mapping = "прямой материал" if classification in {"DIRECT", "direct"} else "полупрямой материал" if classification in {"SEMI_DIRECT", "semi_direct"} else "контекст"
        conclusion = design.user_text(source.get("extracted_stance") or "вывод не указан")
        drivers = "; ".join(source.get("drivers") or [])
        risks = "; ".join(source.get("risks") or [])
        details = [
            f"<span class='mae-muted'>{html.escape(date_text)} · {html.escape(mapping)}</span>",
            f"Вывод: {html.escape(str(conclusion))}",
        ]
        doc_type = _document_type_ru(str(source.get("document_type") or ""))
        lifecycle = _lifecycle_ru(str(source.get("lifecycle_status") or ""), bool(source.get("carried_forward")))
        review_due = source.get("review_due") or source.get("valid_until") or ""
        details.append(f"Тип материала: {html.escape(doc_type)}")
        details.append(f"Текущий статус: {html.escape(lifecycle)}")
        if source.get("carried_forward"):
            details.append("Перенос: взгляд перенесён из предыдущего месяца")
        if review_due:
            details.append(f"Пересмотр до: {html.escape(str(review_due))}")
        if drivers:
            details.append(f"Факторы: {html.escape(drivers)}")
        if risks:
            details.append(f"Риски: {html.escape(risks)}")
        items.append(
            f"<li><b>{html.escape(str(provider))}</b><br>{html.escape(str(title))}<br>{'<br>'.join(details)}</li>"
        )
    return "<ul class='mae-list'>" + "".join(items) + "</ul>"


def _cell_detail_change_text(cell: Any) -> str:
    if cell.change is None and cell.previous_score is None and cell.score is not None:
        return "Новая оценка — в предыдущем срезе данных было недостаточно"
    if cell.change is None:
        return "Динамика пока недоступна"
    return _score_text(cell.change)


def _document_type_ru(document_type: str) -> str:
    mapping = {
        "ANNUAL_OUTLOOK": "годовой outlook",
        "MIDYEAR_OUTLOOK": "полугодовой outlook",
        "QUARTERLY_OUTLOOK": "квартальный outlook",
        "ASSET_OUTLOOK": "asset outlook",
        "MONTHLY_OUTLOOK": "месячный outlook",
        "WEEKLY_COMMENTARY": "еженедельный комментарий",
        "CENTRAL_BANK": "публикация центрального банка",
        "RATING_OUTLOOK": "рейтинговый outlook",
        "NEWS_EVENT": "новостное событие",
    }
    return mapping.get(document_type, "тип материала не указан")


def _lifecycle_ru(status: str, carried_forward: bool = False) -> str:
    if carried_forward and status == "ACTIVE":
        return "Взгляд сохраняется с предыдущего среза"
    if carried_forward and status == "AGING":
        return "Новых публикаций нет, тезис остаётся активным"
    mapping = {
        "ACTIVE": "Взгляд обновлён в текущем месяце",
        "AGING": "Взгляд устаревает и требует обновления",
        "SUPERSEDED": "Взгляд заменён новым материалом",
        "INVALIDATED": "Взгляд больше не подтверждается данными или рынком",
        "EXPIRED": "Срок действия материала истёк",
    }
    return mapping.get(status, "Статус не определён")


def _cell_history_html(history: list[dict[str, Any]]) -> str:
    if not history:
        return "<div class='mae-card-body'>История по этой ячейке пока не сформирована.</div>"
    items = []
    for row in history:
        score = "н/д" if row.get("score") is None else _score_text(int(row["score"]))
        items.append(f"<span class='mae-history-pill'>{html.escape(design.format_date(row.get('date')))} · {html.escape(score)}</span>")
    return "<div class='mae-history-pills'>" + "".join(items) + "</div>"


def _component_breakdown_html(cell: Any) -> str:
    if not getattr(cell, "component_coverage", None):
        return ""
    rows = [
        ("Исследования", getattr(cell, "research_score", None), _component_items(cell, "research")),
        ("Фундаментальные данные", getattr(cell, "data_score", None), _component_items(cell, "data")),
        ("Рынок", getattr(cell, "market_score", None), _component_items(cell, "market")),
        ("Итоговая оценка", getattr(cell, "score", None), [f"Бенчмарк: {getattr(cell, 'benchmark', '') or 'не указан'}", getattr(cell, "validation_comment", "") or "OK"]),
    ]
    items = "".join(
        f"<div class='mae-mini-panel'><div class='mae-kicker'>{html.escape(label)}</div>"
        f"<div style='font-weight:760;margin-bottom:.35rem;'>{html.escape('нет компонента' if value is None else _score_text(value))}</div>"
        f"{design.list_html([item for item in details if item])}</div>"
        for label, value, details in rows
    )
    status = _component_status_ru(getattr(cell, "divergence_status", ""))
    return f"""
<div class="mae-grid-2" style="margin-top:.8rem;">{items}</div>
<div class="mae-muted" style="margin-top:.45rem;">Статус: {html.escape(status)} · версия формулы: {html.escape(getattr(cell, 'formula_version', '') or '')}</div>
"""


def _component_items(cell: Any, layer: str) -> list[str]:
    rows = []
    for factor in (getattr(cell, "factor_details", {}) or {}).get(layer, [])[:4]:
        if layer == "research":
            source = factor.get("source") or "Research source"
            title = design.user_text(factor.get("title") or "материал без названия")
            classification = factor.get("direct_or_semi_direct") or factor.get("direct_or_inherited")
            if classification in {"DIRECT", "direct"}:
                mapping = "прямой материал"
            elif classification in {"SEMI_DIRECT", "semi_direct"}:
                mapping = "полупрямой материал"
            else:
                mapping = "контекст"
            if factor.get("not_scored_reason"):
                mapping = f"{mapping}, не влияет на оценку"
            date_text = factor.get("publication_date") or ""
            score = factor.get("score") or factor.get("raw_score")
            score_text = "нет оценки" if score is None else _score_text(int(score))
            rows.append(f"{source}: {score_text}; {title}; дата {date_text}; {mapping}")
            continue
        name = design.user_text(str(factor.get("name") or "indicator"))
        start_text = factor.get("start_date") or ""
        date_text = factor.get("end_date") or factor.get("metadata", {}).get("current_date") or ""
        score = factor.get("score")
        score_text = "нет оценки" if score is None else _score_text(int(score))
        raw_value = factor.get("raw_value")
        value_text = "" if raw_value is None else f"; значение {raw_value:.4f}" if isinstance(raw_value, float) else f"; значение {raw_value}"
        direction = "положительно" if (score or 0) > 0 else "отрицательно" if (score or 0) < 0 else "нейтрально"
        benchmark = factor.get("benchmark") or getattr(cell, "benchmark", "")
        if layer == "market" and benchmark:
            period = f"{start_text} — {date_text}" if start_text else date_text
            rows.append(f"{name}: {score_text}{value_text}; период {period}; бенчмарк {benchmark}; влияние {direction}")
        else:
            cell_specific = "cell-specific" if factor.get("cell_specific") else "контекст"
            rows.append(f"{name}: {score_text}{value_text}; дата {date_text}; влияние {direction}; {cell_specific}")
    if layer == "research" and not rows:
        return ["В утверждённом списке источников не найдено достаточно релевантных среднесрочных материалов с действующим сроком актуальности."]
    if not rows:
        return ["Нет доступного компонента."]
    return rows


def _scenario_assets(signal: MarketSignalView, *, positive: bool) -> list[str]:
    values = []
    for scenario in signal.scenarios:
        values.extend(scenario.beneficiaries if positive else scenario.vulnerable_assets)
    if values:
        return values[:4]
    if positive and signal.mae_implication > 0:
        return [_row_label(signal.primary_asset)]
    if not positive and signal.mae_implication < 0:
        return [_row_label(signal.primary_asset)]
    return ["Нет явного списка"]


def _scenario_status_label(scenario: ScenarioView) -> str:
    if scenario.evidence_status == "CONFIRMED":
        return "Подтверждённый сценарий"
    if scenario.evidence_status == "CONTRADICTED":
        return "Архивный сценарий"
    return "Требует подтверждения"


def _component_status_ru(value: str) -> str:
    return {
        "CONFIRMED": "Подтверждённый вектор",
        "EARLY": "Ранняя идея",
        "PRICE_LED": "Рынок опережает аналитику",
        "NARRATIVE_ONLY": "Только аналитический нарратив",
        "MIXED": "Смешанный сигнал",
        "INSUFFICIENT": "Недостаточно подтверждений",
    }.get(value or "", "Недостаточно подтверждений")


# Current curated MAE product surface. The CSV is intentionally the single
# source of truth for the user-facing point-in-time result dated 2026-07-12.
_CURATED_MAE_PATH = Path(__file__).resolve().parents[2] / "data" / "mae_curated_2026-07-12.csv"
_LATEST_MAE_PATH = Path(__file__).resolve().parents[2] / "outputs" / "mae_latest_scores.csv"
_CURATED_SCENARIO_PATH = Path(__file__).resolve().parents[2] / "data" / "mae_scenarios_2026-07-12.csv"
_LATEST_SCENARIO_PATH = Path(__file__).resolve().parents[2] / "outputs" / "mae_latest_scenarios.csv"


def _curated_mae_rows() -> list[dict[str, Any]]:
    source_path = _LATEST_MAE_PATH if _LATEST_MAE_PATH.exists() else _CURATED_MAE_PATH
    with source_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        row["score"] = int(row["score"])
    return rows


def _curated_scenarios() -> list[dict[str, Any]]:
    source_path = _LATEST_SCENARIO_PATH if _LATEST_SCENARIO_PATH.exists() else _CURATED_SCENARIO_PATH
    with source_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    result = []
    for row in rows:
        result.append(
            {
                "type": row["scenario_name"],
                "title": row["narrative"].split(".", 1)[0],
                "event": row["narrative"],
                "chain": row["causal_chain"],
                "winners": _curated_list(row["winners"]),
                "vulnerable": _curated_list(row["vulnerable_assets"]),
                "affected": _curated_list(row["affected_cells"]),
                "triggers": [row["trigger"]],
                "indicator": row["indicators_to_watch"],
                "invalidation": row["veto"],
                "snapshot_date": row["snapshot_date"],
            }
        )
    return result


def _curated_score(value: int) -> str:
    return f"{value:+d}" if value > 0 else str(value)


def _curated_list(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(";") if item.strip()]


def _curated_source_links(row: dict[str, Any]) -> str:
    items = []
    for index in (1, 2):
        provider = html.escape(str(row[f"source_{index}_provider"]))
        title = html.escape(str(row[f"source_{index}_title"]))
        source_date = html.escape(str(row[f"source_{index}_date"]))
        url = html.escape(str(row[f"source_{index}_url"]), quote=True)
        excerpt = html.escape(str(row[f"source_{index}_excerpt"]))
        items.append(
            f"<li><b>{provider}</b> · {source_date}<br>"
            f"<a href='{url}' target='_blank'>{title}</a><br>"
            f"<span class='mae-muted'>{excerpt}</span></li>"
        )
    return "<ul class='mae-list'>" + "".join(items) + "</ul>"


def _curated_scenario_html(scenario: dict[str, Any], *, compact: bool = False) -> str:
    if compact:
        return f"""
<div class="mae-mini-panel">
  <div class="mae-kicker">{html.escape(scenario['type'])}</div>
  <div style="font-weight:760;margin-bottom:.35rem;">{html.escape(scenario['title'])}</div>
  <div class="mae-card-body">{html.escape(scenario['chain'])}</div>
  <div class="mae-kicker" style="margin-top:.55rem;">Ключевой индикатор</div>
  <div class="mae-card-body">{html.escape(scenario['indicator'])}</div>
</div>
"""
    return f"""
<div class="mae-card">
  <div class="mae-kicker">{html.escape(scenario['type'])}</div>
  <div class="mae-card-title">{html.escape(scenario['title'])}</div>
  <div class="mae-card-body"><b>Событие или режим:</b> {html.escape(scenario['event'])}</div>
  <div class="mae-card-body" style="margin-top:.45rem;"><b>Причинная цепочка:</b> {html.escape(scenario['chain'])}</div>
  <div class="mae-grid-2" style="margin-top:.7rem;">
    <div class="mae-mini-panel"><div class="mae-kicker">Выигрывают</div>{design.list_html(scenario['winners'])}</div>
    <div class="mae-mini-panel"><div class="mae-kicker">Уязвимы</div>{design.list_html(scenario['vulnerable'])}</div>
  </div>
  <div class="mae-kicker" style="margin-top:.7rem;">Затронутые MAE-клетки</div>{design.list_html(scenario['affected'])}
  <div class="mae-kicker" style="margin-top:.7rem;">Триггеры</div>{design.list_html(scenario['triggers'])}
  <div class="mae-card-body" style="margin-top:.55rem;"><b>Подтверждающий индикатор:</b> {html.escape(scenario['indicator'])}</div>
  <div class="mae-card-body" style="margin-top:.45rem;"><b>Условие отмены:</b> {html.escape(scenario['invalidation'])}</div>
</div>
"""


def _curated_matrix_html(rows: list[dict[str, Any]]) -> str:
    body = []
    for row in rows:
        score = int(row["score"])
        cls = "mae-cell-pos" if score > 0 else "mae-cell-neg" if score < 0 else "mae-cell-zero"
        body.append(
            "<tr>"
            f"<th class='mae-sticky-row'>{html.escape(row['segment'])}</th>"
            f"<td class='{cls}'>{html.escape(_curated_score(score))}</td>"
            f"<td>{html.escape(row['view'])}</td>"
            f"<td>{html.escape(row['benchmark'])}</td>"
            f"<td>{html.escape(row['evidence_status'])}</td>"
            "</tr>"
        )
    return f"""
<div class="mae-heatmap-wrap">
  <table class="mae-heatmap">
    <thead><tr><th class="mae-sticky-corner">Актив</th><th>Оценка</th><th>Направление</th><th>Бенчмарк</th><th>Вывод</th></tr></thead>
    <tbody>{''.join(body)}</tbody>
  </table>
</div>
"""


def _curated_cell_detail_html(row: dict[str, Any]) -> str:
    drivers = design.list_html(_curated_list(row["key_drivers"]))
    risks = design.list_html(_curated_list(row["key_risks"]))
    sources = _curated_source_links(row)
    return f"""
<div class="mae-card">
  <div class="mae-card-title">{html.escape(row['segment'])}</div>
  <div class="mae-grid-2" style="margin-top:.7rem;">
    <div class="mae-mini-panel"><div class="mae-kicker">1. Оценка</div><div class="mae-card-title">{html.escape(_curated_score(int(row['score'])))} · {html.escape(row['view'])}</div></div>
    <div class="mae-mini-panel"><div class="mae-kicker">2. Что говорят источники</div><div class="mae-card-body">{html.escape(row['research_summary'])}</div></div>
  </div>
  <div class="mae-grid-2" style="margin-top:.7rem;">
    <div class="mae-mini-panel"><div class="mae-kicker">3. Почему это важно</div><div class="mae-card-body">{html.escape(row['fundamental_summary'])}</div><div class="mae-card-body" style="margin-top:.45rem;">{html.escape(row['market_summary'])}</div><div class="mae-kicker" style="margin-top:.55rem;">Ключевые факторы</div>{drivers}</div>
    <div class="mae-mini-panel"><div class="mae-kicker">4. Основные риски</div>{risks}</div>
  </div>
  <div class="mae-mini-panel" style="margin-top:.7rem;"><div class="mae-kicker">5. Что изменит оценку</div><div class="mae-card-body">{html.escape(row['reassessment_condition'])}</div></div>
  <div class="mae-kicker" style="margin-top:.8rem;">Два подтверждающих источника</div>{sources}
</div>
"""


def overview_page(session: Session, settings: Settings) -> None:
    rows = _curated_mae_rows()
    scenarios = _curated_scenarios()
    snapshot_date = rows[0].get("snapshot_date") or "2026-07-12"
    st.title("Рыночная картина")
    st.markdown(
        design.editorial_intro(
            "Готовая среднесрочная MAE на основе институциональных прогнозов, официальных макроданных и approved market benchmarks.",
            "Срез",
            [("данные на", snapshot_date), ("горизонт", "6–12 месяцев"), ("оценок", 10)],
        ),
        unsafe_allow_html=True,
    )
    st.success("Источники и данные проверены")

    st.subheader("Главные выводы")
    conclusions = [
        (f"{row['segment']} · {_curated_score(row['score'])}", row.get("current_thesis") or row["research_summary"])
        for row in sorted(rows, key=lambda item: abs(item["score"]), reverse=True)[:5]
    ]
    st.markdown(
        "<div class='mae-grid-2'>" + "".join(
            f"<div class='mae-mini-panel'><div style='font-weight:760;'>{html.escape(title)}</div><div class='mae-card-body'>{html.escape(body)}</div></div>"
            for title, body in conclusions
        ) + "</div>",
        unsafe_allow_html=True,
    )

    left, right = st.columns(2, gap="large")
    with left:
        st.subheader("Сильные активы")
        strong = [f"{row['segment']} ({_curated_score(row['score'])})" for row in rows if row["score"] >= 2]
        st.markdown(design.list_html(strong), unsafe_allow_html=True)
    with right:
        st.subheader("Слабые и ограниченные")
        weak = [f"{row['segment']} ({_curated_score(row['score'])}): {row.get('primary_risk') or row['key_risks']}" for row in rows if row["score"] <= 0]
        st.markdown(design.list_html(weak), unsafe_allow_html=True)

    st.subheader("Три текущих сценария")
    st.markdown("<div class='mae-grid-3'>" + "".join(_curated_scenario_html(item, compact=True) for item in scenarios) + "</div>", unsafe_allow_html=True)

    st.subheader("Что изменилось")
    changed_rows = [row for row in rows if row.get("change_status") not in {"", "CARRY_FORWARD", "UNCHANGED"}]
    if changed_rows:
        st.info(f"Изменения рассчитаны относительно {rows[0].get('previous_snapshot_date', 'предыдущего сопоставимого snapshot')}.")
    else:
        st.info("Новых PASS-оснований для изменения оценок нет; текущие значения перенесены без фиктивного change.")
    for row in (changed_rows or rows[:4]):
        st.markdown(
            f"<div class='mae-mini-panel' style='margin-bottom:.55rem;'><b>{html.escape(row['segment'])} · {_curated_score(row['score'])}</b><br>{html.escape(row.get('what_changed') or row['change_summary'])}</div>",
            unsafe_allow_html=True,
        )


def changes_and_scenarios_page(session: Session, settings: Settings) -> None:
    scenarios = _curated_scenarios()
    snapshot_date = scenarios[0].get("snapshot_date") or "2026-07-12"
    st.title("Сигналы и сценарии")
    st.markdown(
        design.editorial_intro(
            "Ровно три сценария связывают текущий режим с конкретными MAE-клетками, триггерами и условиями отмены.",
            "Сценарная карта",
            [("данные на", snapshot_date), ("сценариев", 3)],
        ),
        unsafe_allow_html=True,
    )
    st.success("Источники и данные проверены")
    for scenario in scenarios:
        st.markdown(_curated_scenario_html(scenario), unsafe_allow_html=True)


def matrix_page(session: Session, settings: Settings) -> None:
    rows = _curated_mae_rows()
    snapshot_date = rows[0].get("snapshot_date") or "2026-07-12"
    st.title("Матрица MAE")
    st.markdown(
        design.editorial_intro(
            "Все десять оценок core universe заполнены. Выберите клетку, чтобы увидеть инвестиционную логику, риски, reassessment condition и два первичных источника.",
            "Текущая MAE",
            [("данные на", snapshot_date), ("горизонт", "6–12 месяцев"), ("покрытие", "10 из 10")],
        ),
        unsafe_allow_html=True,
    )
    st.success("Источники и данные проверены")
    st.markdown(_curated_matrix_html(rows), unsafe_allow_html=True)

    labels = {f"{row['segment']} · {_curated_score(row['score'])}": row["cell_id"] for row in rows}
    selected = st.selectbox("Открыть карточку ячейки", list(labels), key="curated_mae_cell")
    row = next(item for item in rows if item["cell_id"] == labels[selected])
    st.markdown(_curated_cell_detail_html(row), unsafe_allow_html=True)
