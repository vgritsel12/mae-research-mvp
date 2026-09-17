from __future__ import annotations

import html
import json
import logging
import math
import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from io import BytesIO
from typing import Any
from uuid import UUID

import pandas as pd
import streamlit as st
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import Settings
from app.ui import design

logger = logging.getLogger(__name__)

MAX_TABLE_ROWS = 200
MAX_CELL_TEXT = 360

SCORE_STYLES: dict[int, tuple[str, str, str]] = {
    -3: ("#9E3F35", "#F5F3EA", "Сильный негативный сигнал"),
    -2: ("#C9675A", "#F5F3EA", "Негативный сигнал"),
    -1: ("#E7B4AD", "#292823", "Слабый негативный сигнал"),
    0: ("#F5F3EA", "#68675F", "Нейтрально"),
    1: ("#D9F0BE", "#292823", "Слабый позитивный сигнал"),
    2: ("#A9D889", "#292823", "Позитивный сигнал"),
    3: ("#7DAE5E", "#F5F3EA", "Сильный позитивный сигнал"),
}


def can_edit(settings: Settings) -> bool:
    if settings.public_demo:
        return False
    if settings.can_edit_without_password:
        return True
    return bool(st.session_state.get("authenticated"))


def render_access_box(settings: Settings) -> None:
    if settings.public_demo:
        st.session_state["authenticated"] = False
        return
    if settings.app_password:
        if st.session_state.get("authenticated"):
            if st.sidebar.button("Выйти"):
                st.session_state["authenticated"] = False
                st.rerun()
            return
        password = st.sidebar.text_input("Пароль редактирования", type="password")
        if st.sidebar.button("Войти"):
            if password == settings.app_password:
                st.session_state["authenticated"] = True
                st.rerun()
            st.sidebar.error("Неверный пароль.")
        return


def disabled_hint(settings: Settings) -> str:
    return "Недоступно в публичном просмотре." if settings.public_demo else "Требуется пароль редактирования."


def uploaded_file_text(uploaded) -> str:
    name = uploaded.name.lower()
    payload = uploaded.read()
    if name.endswith(".txt"):
        return payload.decode("utf-8", errors="replace")
    if name.endswith(".docx"):
        from docx import Document

        doc = Document(BytesIO(payload))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    if name.endswith(".pdf"):
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(payload))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        if not text.strip():
            raise ValueError("Текст не найден; OCR в MVP не выполняется.")
        return text
    raise ValueError("Поддерживаются TXT, PDF и DOCX.")


def default_publication_date() -> date:
    return date.today()


def prepare_dataframe_for_streamlit(
    data: pd.DataFrame | list[dict[str, Any]] | list[Any],
    *,
    max_rows: int = MAX_TABLE_ROWS,
    max_cell_text: int = MAX_CELL_TEXT,
    drop_columns: tuple[str, ...] = (),
) -> pd.DataFrame:
    """Return a small, string-only DataFrame safe for Streamlit/table rendering."""

    if isinstance(data, pd.DataFrame):
        df = data.copy()
    elif data:
        df = pd.DataFrame(data)
    else:
        return pd.DataFrame(dtype="string")

    if drop_columns:
        df = df.drop(columns=list(drop_columns), errors="ignore")
    if max_rows > 0:
        df = df.head(max_rows)
    if df.empty:
        return pd.DataFrame(columns=df.columns, dtype="string")

    safe = df.map(lambda value: _safe_table_value(value, max_cell_text=max_cell_text))
    for column in safe.columns:
        safe[column] = safe[column].astype("string")
    return safe


def render_dataframe(
    data: pd.DataFrame | list[dict[str, Any]] | list[Any],
    *,
    drop_columns: tuple[str, ...] = (),
    max_rows: int = MAX_TABLE_ROWS,
    max_cell_text: int = MAX_CELL_TEXT,
    empty_message: str = "Данных пока нет.",
) -> pd.DataFrame:
    """Render tabular data without passing arbitrary object columns to PyArrow."""

    original_count = len(data) if hasattr(data, "__len__") else 0
    df = prepare_dataframe_for_streamlit(
        data,
        max_rows=max_rows,
        max_cell_text=max_cell_text,
        drop_columns=drop_columns,
    )
    if df.empty:
        st.info(empty_message)
        return df
    st.markdown(_dataframe_to_html(df), unsafe_allow_html=True)
    if max_rows > 0 and original_count > max_rows:
        st.caption(f"Показано {max_rows} из {original_count} строк. Экспорт содержит полный набор данных.")
    return df


def render_interactive_table(
    data: pd.DataFrame | list[dict[str, Any]] | list[Any],
    *,
    key: str,
    drop_columns: tuple[str, ...] = (),
    filter_columns: tuple[str, ...] = (),
    default_sort: str | None = None,
    empty_message: str = "Данных пока нет.",
    max_cell_text: int = MAX_CELL_TEXT,
) -> pd.DataFrame:
    original_count = len(data) if hasattr(data, "__len__") else 0
    df = prepare_dataframe_for_streamlit(
        data,
        max_rows=0,
        max_cell_text=max_cell_text,
        drop_columns=drop_columns,
    )
    if df.empty:
        st.info(empty_message)
        return df

    search = st.text_input("Поиск", key=f"{key}_search", placeholder="Введите текст для поиска")
    filtered = df
    if search:
        needle = search.casefold()
        mask = filtered.apply(lambda row: needle in " ".join(row.astype(str).tolist()).casefold(), axis=1)
        filtered = filtered[mask]

    available_filters = [column for column in filter_columns if column in filtered.columns]
    if available_filters:
        columns = st.columns(min(4, len(available_filters)))
        for idx, column in enumerate(available_filters):
            options = sorted(value for value in filtered[column].dropna().unique().tolist() if str(value).strip())
            with columns[idx % len(columns)]:
                selected = st.multiselect(column, options, placeholder="Выберите", key=f"{key}_filter_{column}")
            if selected:
                filtered = filtered[filtered[column].isin(selected)]

    sort_options = list(filtered.columns)
    sort_default = default_sort if default_sort in sort_options else sort_options[0]
    sort_cols = st.columns([2, 1, 1])
    with sort_cols[0]:
        sort_by = st.selectbox("Сортировка", sort_options, index=sort_options.index(sort_default), key=f"{key}_sort")
    with sort_cols[1]:
        descending = st.toggle("По убыванию", value=False, key=f"{key}_desc")
    with sort_cols[2]:
        page_size = st.selectbox("Строк на странице", [10, 25, 50, 100], index=1, key=f"{key}_page_size")
    if sort_by in filtered.columns:
        filtered = filtered.sort_values(by=sort_by, ascending=not descending, kind="stable")

    total_rows = len(filtered)
    total_pages = max(1, math.ceil(total_rows / int(page_size)))
    page = st.number_input("Страница", min_value=1, max_value=total_pages, value=1, step=1, key=f"{key}_page")
    start = (int(page) - 1) * int(page_size)
    page_df = filtered.iloc[start : start + int(page_size)]
    st.caption(f"Показано {len(page_df)} из {total_rows} строк после фильтров. Всего в разделе: {original_count}.")
    st.markdown(_dataframe_to_html(page_df), unsafe_allow_html=True)
    return filtered


def render_matrix_table(pivot: pd.DataFrame) -> None:
    st.markdown(_matrix_to_html(pivot), unsafe_allow_html=True)
    st.caption("— нет достаточных данных · 0 подтверждённо нейтрально · -3…+3 сила сигнала")


def render_page_safely(page_name: str, session: Session, render_func: Any, settings: Settings) -> None:
    try:
        render_func(session, settings)
    except Exception:  # noqa: BLE001 - page-level guard keeps navigation alive
        logger.exception("Streamlit page failed: %s", page_name)
        try:
            session.rollback()
            from app.services.audit import record_log

            record_log(
                session,
                "ERROR",
                "ui_page_failed",
                "Не удалось загрузить раздел. Подробности записаны в серверный лог.",
                {"page": page_name},
            )
            session.commit()
        except Exception:  # noqa: BLE001
            logger.exception("Failed to persist UI error log")
            session.rollback()
        st.error("Не удалось загрузить раздел. Подробности записаны в журнал.")


def report_action_error(session: Session, event: str, user_message: str) -> None:
    logger.exception("Streamlit action failed: %s", event)
    try:
        session.rollback()
        from app.services.audit import record_log

        record_log(session, "ERROR", event, user_message, {})
        session.commit()
    except Exception:  # noqa: BLE001
        logger.exception("Failed to persist UI action error")
        session.rollback()
    st.error(user_message)


def llm_mode_label(settings: Settings) -> tuple[str, bool]:
    provider = settings.llm_provider.strip().lower()
    if provider == "openai" and settings.openai_api_key and settings.openai_model:
        return "OpenAI", False
    if provider in {"rule", "rules", "rule_based", "rule-based"}:
        return "Правиловой анализ", False
    return "не настроен", False


def llm_session_key() -> str:
    if "llm_session_key" not in st.session_state:
        st.session_state["llm_session_key"] = f"ui-{uuid.uuid4()}"
    return str(st.session_state["llm_session_key"])


def _safe_table_value(value: Any, *, max_cell_text: int) -> str:
    if value is None:
        return ""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return design.format_date(value, with_time=True)
    if isinstance(value, date):
        return design.format_date(value)
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return ""
    if isinstance(value, dict | list | tuple | set):
        text = json.dumps(value, ensure_ascii=False, default=_json_default)
    else:
        try:
            if pd.isna(value):
                return ""
        except (TypeError, ValueError):
            pass
        text = design.humanize(value)
    text = " ".join(text.split())
    if len(text) > max_cell_text:
        return text[: max_cell_text - 1].rstrip() + "…"
    return text


def _json_default(value: Any) -> str:
    if isinstance(value, BaseModel):
        return json.dumps(value.model_dump(mode="json"), ensure_ascii=False)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def _dataframe_to_html(df: pd.DataFrame) -> str:
    header = "".join(f"<th>{html.escape(str(col))}</th>" for col in df.columns)
    body_rows = []
    for _, row in df.iterrows():
        cells = "".join(_table_cell_html(str(column), value) for column, value in row.items())
        body_rows.append(f"<tr>{cells}</tr>")
    body = "".join(body_rows)
    return f"""
<style>
.mae-safe-table {{
  width: 100%;
  border-collapse: collapse;
  font-size: 0.86rem;
  background: #F5F3EA;
}}
.mae-safe-table th {{
  text-align: left;
  background: #E8E7DD;
  border-bottom: 1px solid #38372F;
  color: #292823;
  padding: 0.55rem 0.65rem;
  position: sticky;
  top: 0;
  z-index: 1;
  white-space: nowrap;
  font-family: var(--mae-font-condensed);
  text-transform: uppercase;
}}
.mae-safe-table td {{
  border-bottom: 1px solid rgba(56,55,47,.28);
  color: #292823;
  padding: 0.5rem 0.65rem;
  vertical-align: top;
  max-width: 34rem;
  overflow-wrap: anywhere;
  line-height: 1.35;
}}
.mae-safe-table tr:hover td {{
  background: #EEF3DB;
}}
.mae-safe-table a {{
  color: #292823;
  text-decoration: none;
  font-weight: 650;
  border-bottom: 1px solid #292823;
}}
.mae-table-scroll {{
  overflow-x: auto;
  max-height: 34rem;
  border: 1px solid #38372F;
  border-radius: 0;
  background: #F5F3EA;
  box-shadow: none;
}}
</style>
<div class="mae-table-scroll">
<table class="mae-safe-table">
  <thead><tr>{header}</tr></thead>
  <tbody>{body}</tbody>
</table>
</div>
"""


def _table_cell_html(column: str, value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "<td></td>"
    column_lower = column.casefold()
    if column_lower in {"статус", "подтверждение", "рынок", "материальность"}:
        tone = "neutral"
        if any(token in text.lower() for token in ["утверж", "подтверж", "успеш"]):
            tone = "positive"
        elif any(token in text.lower() for token in ["ошиб", "отклон", "опровер"]):
            tone = "negative"
        elif any(token in text.lower() for token in ["провер", "недостат", "частично", "ожида"]):
            tone = "warning"
        return f"<td>{design.badge(text, tone)}</td>"
    if column_lower in {"url", "источник", "источники"} and (text.startswith("http://") or text.startswith("https://")):
        return f"<td>{design.link_html(text)}</td>"
    if text.startswith("http://") or text.startswith("https://"):
        return f"<td>{design.link_html(text)}</td>"
    if column_lower in {"текущий сигнал", "сила", "strength", "score"}:
        try:
            return f"<td>{design.score_badge(int(text.replace('+', '')))}</td>"
        except ValueError:
            pass
    return f"<td>{html.escape(design.humanize(text))}</td>"


def _matrix_to_html(df: pd.DataFrame) -> str:
    index_name = html.escape(str(df.index.name or "Категория"))
    headers = "".join(f"<th>{html.escape(design.region_label(str(col)))}</th>" for col in df.columns)
    rows = []
    for row_key, row in df.iterrows():
        cells = [f"<th>{html.escape(str(row_key))}</th>"]
        for value in row.tolist():
            if value is None or str(value).strip() in {"", "—", "nan", "NaN"}:
                cells.append(
                    '<td class="mae-matrix-empty" style="background:#E8E7DD;color:#68675F;text-align:center;font-weight:650" title="Нет достаточных данных">—</td>'
                )
                continue
            score = int(value)
            bg, color, label = SCORE_STYLES.get(score, SCORE_STYLES[0])
            sign = f"+{score}" if score > 0 else str(score)
            extra_class = "mae-matrix-zero" if score == 0 else "mae-matrix-signal"
            style = (
                "background:#F5F3EA;color:#68675F;font-weight:650;"
                if score == 0
                else f"background:{bg};color:{color};font-weight:800;"
            )
            cells.append(
                f'<td class="{extra_class}" style="{style}text-align:center" title="{html.escape(label)}">{sign}</td>'
            )
        rows.append(f"<tr>{''.join(cells)}</tr>")
    return f"""
<style>
.mae-matrix {{
  width: 100%;
  border-collapse: collapse;
  font-size: 0.84rem;
  background: #F5F3EA;
}}
.mae-matrix th {{
  text-align: left;
  background: #E8E7DD;
  border: 1px solid #38372F;
  color: #292823;
  padding: 0.55rem 0.65rem;
  white-space: nowrap;
  font-family: var(--mae-font-condensed);
  text-transform: uppercase;
}}
.mae-matrix thead th:first-child,
.mae-matrix tbody th:first-child {{
  position: sticky;
  left: 0;
  z-index: 2;
  min-width: 18rem;
  max-width: 24rem;
  white-space: normal;
}}
.mae-matrix td {{
  border: 1px solid #38372F;
  padding: 0.52rem;
  min-width: 5rem;
  border-radius: 0;
}}
.mae-matrix tr:hover th,
.mae-matrix tr:hover td {{
  box-shadow: inset 0 0 0 9999px rgba(198,231,154,0.28);
}}
.mae-matrix-wrap {{
  overflow-x: auto;
  border: 1px solid #38372F;
  border-radius: 0;
  background: #F5F3EA;
  box-shadow: none;
}}
</style>
<div class="mae-matrix-wrap">
<table class="mae-matrix">
  <thead><tr><th>{index_name}</th>{headers}</tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table>
</div>
"""
