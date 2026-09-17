from __future__ import annotations

import html
import json
import logging
import re
import traceback
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from app.config import ROOT_DIR, Settings
from app.services.audit_disclosure import QUARANTINE_BANNER, SOURCE_TAXONOMY_LABELS, AuditDisclosure
from app.services.full_snapshot_loader import (
    ASSET_ORDER,
    GEOGRAPHY_ORDER,
    FullSnapshotData,
    get_cell,
    matrix_dimensions,
)
from app.services.full_snapshot_update import add_source_to_inbox, run_full_snapshot_update
from app.services.historical_analogs import (
    FACTOR_CONFIG,
    OUTCOME_ASSETS,
    load_historical_analogs,
    rebuild_historical_analogs_safely,
)
from app.ui.helpers import can_edit, render_access_box


logger = logging.getLogger(__name__)

# The original analytical architecture remains the audit-facing source of truth.
MODULES = [
    "Strategy & Sentiment Map",
    "Change Tracker",
    "Scenario & Market Reaction Map",
    "Evidence & Expectation Check",
    "Transmission & Control Layer",
]

USER_SECTIONS = [
    "Market View",
    "Market Map",
    "What Changed",
    "Scenarios",
    "Historical Analogs",
    "Sources & Downloads",
]

DOWNLOADS = [
    ("Full Excel Report", "mae_full_latest.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ("Score Matrix", "mae_full_latest_scores.csv", "text/csv"),
    ("Evidence Register", "mae_full_latest_evidence.csv", "text/csv"),
    ("Scenario Data", "mae_full_latest_scenarios.csv", "text/csv"),
    ("Transmission Data", "mae_full_latest_transmission.csv", "text/csv"),
    ("Quality Report", "mae_full_latest_quality_report.json", "application/json"),
    ("Historical Analogs", "mae_historical_analogs_latest.csv", "text/csv"),
]

AUDIT_DOWNLOADS = [
    ("Audit disclosure JSON", "json_export", "application/json"),
    ("Audit disclosure CSV", "csv_export", "text/csv"),
    ("Financial candidate workbook", "xlsx_export", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
]

CHANGE_LABELS = {
    "UPGRADE": "Market View Changed",
    "DOWNGRADE": "Market View Changed",
    "MARKET_CHANGE": "Market View Changed",
    "THESIS_CHANGE": "Thesis Refined",
    "EVIDENCE_CHANGE": "Evidence Strengthened",
    "METHODOLOGY_REFINEMENT": "Benchmark Improved",
    "NEW_COMPARABLE_ASSESSMENT": "New Assessment",
    "NO_CHANGE": "No Material Change",
    "CARRY_FORWARD": "No Material Change",
    "UNCHANGED": "No Material Change",
}


def render_header(bundle: FullSnapshotData) -> None:
    audit = bundle.audit
    if audit.is_quarantined:
        st.markdown(
            "<div class='mae-quarantine-banner' role='status'>"
            f"<strong>{_e(QUARANTINE_BANNER)}</strong>"
            f"<span>Candidate {_e(audit.candidate_id)} · {_e(audit.financial_status)} · "
            f"analyst {_e(audit.analyst_review_status)} · {_e(audit.release_status)}</span></div>",
            unsafe_allow_html=True,
        )
    horizon = "6–12 month audit horizon"
    st.markdown(
        "<div class='mae-masthead'>"
        "<div class='mae-masthead__brand'>MAE<strong>Financial audit candidate</strong>"
        f"<span>{_e(_long_date(audit.snapshot_date))} · {_e(horizon)}</span></div>"
        "<div class='mae-masthead__meta'>"
        f"<div>Technical gate<b>{_e(audit.technical_status)}</b></div>"
        f"<div>Release gate<b>{_e(audit.release_status)}</b></div>"
        "</div></div>",
        unsafe_allow_html=True,
    )
    with st.expander("Candidate details", expanded=False):
        counts = audit.status_counts
        details = [
            ("Candidate date", audit.snapshot_date or "—"),
            ("Candidate ID", audit.candidate_id or "—"),
            ("Horizon", horizon),
            ("Financial status", audit.financial_status),
            ("Analyst review", audit.analyst_review_status),
            ("Reviewer actor", audit.reviewer_actor_type),
            ("Matrix", "19 × 6"),
            ("Numeric / null", f"{counts.get('ACTIVE', 0) + counts.get('CARRY_FORWARD', 0)} / {int(audit.cells['score'].isna().sum()) if audit.available else 0}"),
        ]
        st.markdown(_method_grid(details), unsafe_allow_html=True)


def render_sidebar(bundle: FullSnapshotData, settings: Settings) -> None:
    render_access_box(settings)
    audit = bundle.audit
    st.sidebar.markdown(
        "<div class='mae-sidebar-snapshot'>Audit candidate"
        f"<strong>{_e(_compact_date(audit.snapshot_date))}</strong>"
        f"<span class='mae-quarantine-dot'></span>{_e(audit.release_status)}<br>financial {_e(audit.analyst_review_status)}</div>",
        unsafe_allow_html=True,
    )
    if settings.public_demo:
        st.sidebar.caption("PUBLIC DEMO · READ ONLY")

    with st.sidebar.expander("Downloads", expanded=False):
        for label, path_key, mime in AUDIT_DOWNLOADS:
            _download_path_button(label, audit.paths.get(path_key), mime, key=f"sidebar_{path_key}")

    editable = bool(settings.is_local and can_edit(settings) and not settings.public_demo)
    with st.sidebar.expander("Admin / Methodology", expanded=False):
        if not settings.openai_api_key:
            st.caption("The read-only audit candidate remains available without an API key.")
        update_clicked = st.button(
            "Refresh Market View",
            disabled=not editable,
            type="primary",
            width="stretch",
            key="update_full_mae",
        )
        if not editable:
            st.caption("Refresh is disabled in read-only mode.")
        if update_clicked:
            progress_box = st.empty()
            steps: list[str] = []

            def on_progress(step: str) -> None:
                steps.append(step)
                progress_box.markdown("\n".join(f"- {item}" for item in steps))

            result = run_full_snapshot_update(settings=settings, progress=on_progress)
            st.session_state["last_update_result"] = result.to_dict()
            if result.success:
                st.success(result.message)
                st.cache_data.clear()
            else:
                st.warning(result.message)
        _render_update_result(st.session_state.get("last_update_result"))

        analog_clicked = st.button(
            "Refresh Historical Analogs",
            disabled=not editable or not bool(bundle.snapshot_date),
            width="stretch",
            key="refresh_historical_analogs",
        )
        if analog_clicked:
            analog_result = rebuild_historical_analogs_safely(date.fromisoformat(bundle.snapshot_date))
            if analog_result.get("status") == "PASS":
                st.cache_data.clear()
                st.success(f"Historical analogs refreshed: {analog_result.get('analogs', 0)} periods.")
            else:
                st.warning(str(analog_result.get("message") or "Previous valid analog output retained."))

        st.markdown("**Five analytical modules**")
        for module in MODULES:
            st.caption(module)

        with st.expander("Manual source import", expanded=False):
            _source_inbox_form(bundle, editable)

        with st.expander("Quality diagnostics", expanded=False):
            st.write("Matrix: 19 × 6")
            st.write(f"ACTIVE / null: {audit.status_counts.get('ACTIVE', 0)} / {int(audit.cells['score'].isna().sum()) if audit.available else 0}")
            st.write(f"Evidence items: {audit.source_taxonomy.get('evidence_items', 0)}")
            st.write(f"Financial review: {audit.financial_status}")
            if audit.errors:
                for error in audit.errors:
                    st.warning(error)
            else:
                st.info("Candidate disclosure loaded; release gate remains closed.")
            if st.session_state.get("last_page_traceback"):
                st.code(st.session_state["last_page_traceback"], language="text")


def render_page_safely(name: str, render_func: Any, bundle: FullSnapshotData, settings: Settings) -> None:
    try:
        render_func(bundle, settings)
    except Exception:  # noqa: BLE001 - page guard keeps the canonical snapshot usable
        logger.exception("Editorial snapshot page failed: %s", name)
        st.session_state["last_page_traceback"] = traceback.format_exc()
        st.error("This section is temporarily unavailable. The active market view has not changed.")
        with st.expander("Diagnostics", expanded=False):
            st.code(st.session_state["last_page_traceback"], language="text")


def market_view_page(bundle: FullSnapshotData, _settings: Settings) -> None:
    st.title("Market View")
    audit = bundle.audit
    if not _require_audit(audit):
        return
    counts = audit.status_counts
    st.markdown(
        "<div class='mae-deck'>A sparse, technically validated candidate prepared for independent financial review. "
        "It is not an approved investment view.</div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        "<div class='mae-section-kicker'>Candidate setup</div>"
        "<div class='mae-editorial-copy'><p>Only financially eligible evidence may produce a numeric score. "
        f"This candidate has {counts.get('ACTIVE', 0)} ACTIVE cell, {counts.get('INSUFFICIENT_DATA', 0)} "
        f"insufficient-data cells and {counts.get('REJECTED', 0)} rejected cells.</p>"
        "<p>Engineering validation confirms contract and geometry, but does not substitute for analyst approval.</p></div>",
        unsafe_allow_html=True,
    )

    st.header("Most Attractive")
    active = audit.cells[audit.cells["score_status"].isin(["ACTIVE", "CARRY_FORWARD"])].copy()
    if active.empty:
        st.caption("No eligible numeric candidate cells are available.")
    else:
        st.markdown(_audit_ranked_cells(active.sort_values("score", ascending=False)), unsafe_allow_html=True)

    st.header("Most Vulnerable")
    vulnerable = active[active["score"] < 0]
    if vulnerable.empty:
        st.caption("No eligible negative candidate score exists; null cells are not treated as zero or neutral.")
    else:
        st.markdown(_audit_ranked_cells(vulnerable.sort_values("score")), unsafe_allow_html=True)

    st.header("What Matters Now")
    topics = [
        ("01", "Financial gate", f"{audit.financial_status}; analyst state {audit.analyst_review_status}."),
        ("02", "Eligible evidence", f"{audit.source_taxonomy.get('evidence_items', 0)} item and {audit.source_taxonomy.get('approved_direct_evidence', 0)} approved direct input."),
        ("03", "Sparse coverage", f"{int(audit.cells['score'].notna().sum())} numeric score; {int(audit.cells['score'].isna().sum())} explicit nulls."),
    ]
    st.markdown(
        "<div class='mae-topic-grid'>"
        + "".join(
            f"<div class='mae-topic'><div class='mae-topic__num'>{number}</div>"
            f"<h3>{_e(title)}</h3><p>{_e(copy)}</p></div>"
            for number, title, copy in topics
        )
        + "</div>",
        unsafe_allow_html=True,
    )

    st.header("Base Case")
    derived = audit.scenarios
    st.markdown(
        "<div class='mae-dark-story'><div class='mae-section-kicker'>Derived output</div>"
        f"<h3>{_e(derived.get('status') or 'INSUFFICIENT_DATA')}</h3>"
        f"<p>{_e(derived.get('reason') or 'No eligible scenario was generated.')}</p>"
        "<div class='mae-story-grid'><div><b>Scenario records</b><p>0</p></div>"
        "<div><b>Null handling</b><p>Unscored cells remain null.</p></div>"
        "<div><b>Approval</b><p>No human approval was created.</p></div></div></div>",
        unsafe_allow_html=True,
    )


def market_map_page(bundle: FullSnapshotData, _settings: Settings) -> None:
    st.title("Market Map")
    audit = bundle.audit
    if not _require_audit(audit):
        return
    st.markdown(
        "<div class='mae-deck'>A complete 19 × 6 audit matrix. Every absence has an explicit status; null never means zero.</div>",
        unsafe_allow_html=True,
    )
    counts = audit.status_counts
    st.markdown(
        "<div class='mae-statline'>"
        f"<span><strong>{counts.get('ACTIVE', 0)}</strong> ACTIVE</span>"
        f"<span><strong>{counts.get('INSUFFICIENT_DATA', 0)}</strong> insufficient</span>"
        f"<span><strong>{counts.get('REJECTED', 0)}</strong> rejected</span>"
        f"<span><strong>{counts.get('NOT_APPLICABLE', 0)}</strong> not applicable</span>"
        "</div>",
        unsafe_allow_html=True,
    )
    st.markdown(heatmap_html(audit.cells, kind="audit"), unsafe_allow_html=True)
    st.caption("ACTIVE cells show a score. INS, REJ and N/A are explicit non-numeric states.")

    st.header("Cell View")
    cell = _audit_cell_selector(audit, "market_map", default_asset="Wide Market", default_geography="Global")
    if cell:
        _render_audit_cell_story(audit, cell)


def what_changed_page(bundle: FullSnapshotData, _settings: Settings) -> None:
    st.title("What Changed")
    audit = bundle.audit
    if not _require_audit(audit):
        return
    st.markdown(
        "<div class='mae-deck'>Only ACTIVE or CARRY_FORWARD cells admitted by the strict financial gate can enter this comparison.</div>",
        unsafe_allow_html=True,
    )
    changed = audit.what_changed
    records = list(changed.get("records") or [])
    material = [record for record in records if record.get("material_shift")]
    st.markdown(
        f"<div class='mae-statline'><span><strong>{_e(changed.get('status') or '—')}</strong> candidate status</span>"
        f"<span><strong>{len(material)}</strong> material changes</span>"
        f"<span><strong>{len(records)}</strong> eligible comparisons</span></div>",
        unsafe_allow_html=True,
    )

    st.header("Market Changes")
    st.caption("No eligible material market change was identified." if not material else f"{len(material)} eligible changes.")
    st.header("Methodology Refinements")
    st.caption("Methodology admission is recorded separately and does not create analyst approval.")
    st.header("New Assessments")
    st.caption("No new assessment has passed the analyst gate; the candidate remains quarantined.")

    st.header("Unchanged Views")
    if not records:
        st.caption("No eligible comparison record is available.")
    else:
        rows = [
            [record.get("canonical_cell_id"), record.get("prior_score"), record.get("candidate_score"), record.get("delta"), record.get("status")]
            for record in records
        ]
        st.markdown(_simple_table(["Cell", "Prior", "Candidate", "Delta", "Status"], rows), unsafe_allow_html=True)
    st.caption("Rejected and insufficient-data cells are excluded from derived change records, not silently treated as unchanged.")


def scenarios_page(bundle: FullSnapshotData, _settings: Settings) -> None:
    st.title("Scenarios")
    audit = bundle.audit
    if not _require_audit(audit):
        return
    st.markdown(
        "<div class='mae-deck'>Derived scenarios require a materially shifted eligible cell. This candidate does not meet that gate.</div>",
        unsafe_allow_html=True,
    )
    scenario_state = audit.scenarios
    st.markdown(_empty_audit_panel("Scenario set", scenario_state), unsafe_allow_html=True)
    st.markdown(_empty_audit_panel("Shift Signal", audit.shift_signals), unsafe_allow_html=True)

    st.header("Scenario Transmission")
    st.markdown(_empty_audit_panel("Transmission paths", audit.transmission_paths), unsafe_allow_html=True)
    st.caption("Ineligible decisions cannot enter Shift Signal, scenario or transmission outputs.")


def historical_analogs_page(bundle: FullSnapshotData, _settings: Settings) -> None:
    st.title("Historical Analogs")
    audit = bundle.audit
    if not _require_audit(audit):
        return
    st.markdown(
        "<div class='mae-deck'>When did the US-led market context look similar — and which periods are complete enough to compare?</div>",
        unsafe_allow_html=True,
    )
    analogs = audit.analogs
    if analogs.get("status") != "PASS" or not analogs.get("analogs"):
        st.warning("A technically validated analog candidate is unavailable. No periods have been invented.")
        return
    st.caption("Historical similarity is descriptive, not predictive. The candidate remains quarantined and is not an investment recommendation.")

    st.header("US-led Candidate Setup")
    regime = analogs.get("current_regime") or {}
    factor_config = analogs.get("factor_config") or {}
    st.markdown(
        "<div class='mae-regime-grid'>"
        + "".join(
            "<div class='mae-regime-factor'>"
            f"<span>{_e(config.get('label') or key)}</span><strong>{_e(regime.get(key) or 'Unavailable')}</strong></div>"
            for key, config in factor_config.items()
        )
        + "</div>",
        unsafe_allow_html=True,
    )

    st.header("Closest Historical Setups")
    for analog in analogs["analogs"]:
        limitations = ", ".join(analog.get("limitations") or [])
        st.markdown(
            "<div class='mae-analog-card'>"
            f"<div class='mae-analog-card__rank'>#{int(analog['rank']):02d}</div>"
            f"<div class='mae-analog-card__date'>{_e(_analog_date(analog['period']))}</div>"
            f"<div class='mae-analog-card__score'>{float(analog['adjusted_similarity']):.1f}<span>Adjusted similarity</span></div>"
            f"<div class='mae-analog-card__story'><strong>{_e(analog.get('regime_name') or '—')}</strong>"
            f"<div class='mae-analog-metrics'>Raw {float(analog['raw_similarity']):.1f} · "
            f"coverage {float(analog['factor_coverage']):.0%} · confidence {_e(analog.get('confidence') or '—')}</div>"
            f"<div class='mae-analog-card__diff'>{_e(limitations)}</div></div>"
            "</div>",
            unsafe_allow_html=True,
        )

    st.header("What Happened Next")
    horizon = st.radio("Forward horizon", ["1M", "3M", "6M", "12M"], horizontal=True, index=1)
    horizon_key = horizon.lower()
    table_rows = []
    for analog in analogs["analogs"]:
        outcomes = (analog.get("forward_returns") or {}).get(horizon_key, {})
        table_rows.append(
            [
                _analog_date(analog["period"]),
                _return_label(outcomes.get("us_equities")),
                _return_label(outcomes.get("gold")),
                _return_label(outcomes.get("long_treasuries")),
                _return_label(outcomes.get("global_equities")),
            ]
        )
    st.markdown(
        _simple_table(
            ["Analog", f"{horizon} US Equities", f"{horizon} Gold", f"{horizon} Long Bonds", f"{horizon} Global Equities"],
            table_rows,
        ),
        unsafe_allow_html=True,
    )

    st.header("Interpretation")
    dispersion = (analogs.get("dispersion") or {}).get(horizon_key, {})
    dispersion_rows = [
        [
            (analogs.get("outcome_assets") or {}).get(asset, asset),
            values.get("count"),
            _return_label(values.get("lower_quartile")),
            _return_label(values.get("median")),
            _return_label(values.get("upper_quartile")),
        ]
        for asset, values in dispersion.items()
    ]
    st.markdown(_simple_table(["Asset", "N", "Lower quartile", "Median", "Upper quartile"], dispersion_rows), unsafe_allow_html=True)
    st.caption("Dispersion is shown even when the complete sample contains one period; no predictive claim is made.")

    st.subheader("Recent / Incomplete Periods")
    recent = list(analogs.get("recent_incomplete") or [])
    st.markdown(_recent_analog_cards(recent), unsafe_allow_html=True)
    st.caption(f"{len(recent)} recent periods are kept separate; {len(analogs.get('critical_excluded') or [])} periods failed a critical-factor gate.")

    with st.expander("Methodology and limitations", expanded=False):
        st.write("Similarity is a weighted normalized distance; adjusted similarity equals raw similarity multiplied by factor coverage.")
        st.markdown(
            _method_grid(
                [(config.get("label") or key, f"{float(config.get('weight') or 0):.0%}") for key, config in factor_config.items()]
                + [
                    ("Mode", analogs.get("mode") or "—"),
                    ("Minimum separation", f"{analogs.get('minimum_separation_months', 0)} months"),
                    ("No look-ahead", (analogs.get("no_look_ahead") or {}).get("status") or "—"),
                ]
            ),
            unsafe_allow_html=True,
        )
        for limitation in analogs.get("limitations") or []:
            st.markdown(f"- {limitation}")
        st.markdown("- High-yield credit-spread history is limited to the locally observed official series window; it is not backfilled, replaced or synthesized.")


def sources_downloads_page(bundle: FullSnapshotData, _settings: Settings) -> None:
    st.title("Sources & Downloads")
    audit = bundle.audit
    if not _require_audit(audit):
        return
    st.markdown(
        "<div class='mae-deck'>Eligible publications, lineage counts and reconciled candidate files for independent review.</div>",
        unsafe_allow_html=True,
    )

    st.header("Sources")
    taxonomy_rows = [(SOURCE_TAXONOMY_LABELS[key], audit.source_taxonomy.get(key, 0)) for key in SOURCE_TAXONOMY_LABELS]
    st.markdown(_method_grid(taxonomy_rows), unsafe_allow_html=True)
    for item in audit.evidence:
        source = (item.get("sources") or [{}])[0]
        st.markdown(
            "<div class='mae-source-card'>"
            f"<div class='mae-source-card__issuer'>{_e(source.get('institution') or 'Research')}</div>"
            f"<div><div class='mae-source-card__title'>{_e(item.get('publication_title') or 'Untitled')}</div>"
            f"<div class='mae-source-card__desc'>{_e(item.get('exact_quote') or '')}</div>"
            f"<div class='mae-source-card__desc'>Cell {_e(item.get('canonical_cell_id'))} · {_e(item.get('evidence_mode'))} · "
            f"confidence {_e(item.get('confidence'))} · approved sources {_e(item.get('approved_source_count'))}</div></div>"
            f"<a href='{_e(source.get('source_url') or '#')}' target='_blank' rel='noopener noreferrer'>Open source ↗</a></div>",
            unsafe_allow_html=True,
        )

    st.header("Downloads")
    st.markdown("<div class='mae-download-grid'>", unsafe_allow_html=True)
    for start in range(0, len(AUDIT_DOWNLOADS), 2):
        columns = st.columns(2)
        for column, (label, path_key, mime) in zip(columns, AUDIT_DOWNLOADS[start : start + 2], strict=False):
            with column:
                _download_path_button(label, audit.paths.get(path_key), mime, key=f"source_{path_key}")
    st.markdown("</div>", unsafe_allow_html=True)

    st.header("Why This View")
    st.markdown(
        "The disclosure follows the strict financial mapping gate. Methodology acceptance is not analyst approval, and the release remains quarantined.",
    )
    with st.expander("Methodology & Quality", expanded=False):
        details = [
            ("Technical", audit.technical_status),
            ("Financial", audit.financial_status),
            ("Analyst", audit.analyst_review_status),
            ("Release", audit.release_status),
            ("Human approval created", str(audit.human_approval_created)),
            ("Reviewer actor", audit.reviewer_actor_type),
        ]
        st.markdown(_method_grid(details), unsafe_allow_html=True)
        st.markdown("**Analytical module mapping**")
        mapping = [
            (MODULES[0], "Market View + Market Map"),
            (MODULES[1], "What Changed"),
            (MODULES[2], "Scenarios"),
            (MODULES[3], "Why This View + Sources"),
            (MODULES[4], "What Would Change Our Mind"),
        ]
        st.markdown(_simple_table(["Analytical module", "User-facing layer"], mapping), unsafe_allow_html=True)


def heatmap_html(
    scores: pd.DataFrame,
    *,
    kind: str,
    values: dict[str, int] | None = None,
) -> str:
    lookup = {str(row.get("cell_id")): row for row in scores.to_dict("records")}
    rows = ["<div class='mae-heatmap-wrap'><table class='mae-heatmap'><thead><tr><th>Asset segment</th>"]
    rows.extend(f"<th>{_e(geo)}</th>" for geo in GEOGRAPHY_ORDER)
    rows.append("</tr></thead><tbody>")
    for asset in ASSET_ORDER:
        rows.append(f"<tr><td>{_e(asset)}</td>")
        for geography in GEOGRAPHY_ORDER:
            row = next(
                (
                    candidate
                    for candidate in lookup.values()
                    if candidate.get("asset_segment") == asset and candidate.get("geography") == geography
                ),
                None,
            )
            value, css_class, title = _heatmap_value(row, kind=kind, values=values or {})
            rows.append(f"<td class='{css_class}' title='{_e(title)}'>{_e(value)}</td>")
        rows.append("</tr>")
    rows.append("</tbody></table></div>")
    return "".join(rows)


def parse_reaction_map(value: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in _split_items(value):
        if ":" not in item:
            continue
        cell_id, raw = item.rsplit(":", 1)
        try:
            result[cell_id.strip()] = int(raw.strip().replace("+", ""))
        except ValueError:
            continue
    return result


def _render_cell_story(bundle: FullSnapshotData, cell: dict[str, Any]) -> None:
    name = f"{cell.get('geography')} {cell.get('asset_segment')}"
    applicable = str(cell.get("applicable") or "").lower() == "true"
    if not applicable:
        st.markdown(
            "<div class='mae-cell-hero'><div><div class='mae-section-kicker'>Cell view</div>"
            f"<h3>{_e(name)}</h3></div><div class='mae-cell-score'>N/A<span>Not applicable</span></div></div>",
            unsafe_allow_html=True,
        )
        return
    score = _score_int(cell.get("score"))
    evidence = bundle.evidence[bundle.evidence["cell_id"].astype(str).eq(str(cell.get("cell_id")))]
    support = []
    for value in evidence.get("excerpt", pd.Series(dtype=str)).astype(str):
        clean = _shorten(value, 190)
        if clean and clean not in support:
            support.append(clean)
    support = support[:4]
    sources = _deduplicated_sources(evidence).head(4)
    st.markdown(
        "<div class='mae-cell-hero'><div><div class='mae-section-kicker'>Cell view</div>"
        f"<h3>{_e(name)}</h3></div>"
        f"<div class='mae-cell-score'>{_e(_format_score(score))}<span>{_e(_score_direction(score))}</span></div></div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        "<div class='mae-story-columns'><div>"
        f"<div class='mae-story-block'><h4>Why now</h4><p>{_e(_plain_thesis(cell.get('thesis')))}</p></div>"
        f"<div class='mae-story-block'><h4>Key driver</h4><p>{_e(cell.get('main_driver') or '—')}</p></div>"
        "<div class='mae-story-block'><h4>What supports the view</h4>"
        f"{_html_list(support or [cell.get('instrument_definition') or 'Canonical evidence remains available.'])}</div>"
        "</div><div>"
        "<div class='mae-story-block'><h4>What could challenge it</h4>"
        f"{_html_list([cell.get('primary_risk'), cell.get('secondary_risk')])}</div>"
        "<div class='mae-story-block'><h4>What would change our mind</h4>"
        f"<p><strong>Trigger.</strong> {_e(cell.get('trigger') or '—')}</p>"
        f"<p><strong>Invalidation.</strong> {_e(cell.get('veto') or '—')}</p></div>"
        "<div class='mae-story-block'><h4>Benchmark</h4>"
        f"<p>{_e(cell.get('benchmark_name') or '—')}</p></div>"
        "</div></div>",
        unsafe_allow_html=True,
    )
    st.subheader("Sources")
    if sources.empty:
        st.caption("No source card is available for this cell.")
    for row in sources.to_dict("records"):
        st.markdown(_source_card(row), unsafe_allow_html=True)

    with st.expander("Methodology details", expanded=False):
        fields = [
            ("Evidence mode", cell.get("evidence_mode")),
            ("Conviction", cell.get("conviction")),
            ("Analytical quality", cell.get("analytical_quality")),
            ("Comparability", cell.get("comparability_status")),
            ("Proxy quality", cell.get("proxy_quality")),
            ("Research View", cell.get("research_view_score")),
            ("Fundamental & Macro", cell.get("fundamental_macro_score")),
            ("Market Confirmation", cell.get("market_confirmation_score")),
            ("Valuation & Risk", cell.get("valuation_risk_score")),
            ("Methodology note", cell.get("methodology_note")),
        ]
        st.markdown(_method_grid(fields), unsafe_allow_html=True)
        with st.expander("Advanced evidence", expanded=False):
            for row in evidence.head(8).to_dict("records"):
                st.markdown(
                    f"**{row.get('component_role') or 'Evidence'}** — {_e(row.get('title') or 'Untitled')}  \n"
                    f"{_e(row.get('relevance_reason') or '')}"
                )


def _render_audit_cell_story(audit: AuditDisclosure, cell: dict[str, Any]) -> None:
    name = f"{cell.get('geography')} {cell.get('asset_segment')}"
    status = str(cell.get("score_status") or "UNAVAILABLE")
    raw_score = cell.get("score")
    score_text = "Not scored" if pd.isna(raw_score) else _format_score(raw_score)
    limitation = str(cell.get("limitation_reason") or cell.get("rejection_reason") or "No limitation recorded.")
    st.markdown(
        "<div class='mae-cell-hero mae-audit-critical'><div><div class='mae-section-kicker'>Cell audit</div>"
        f"<h3>{_e(name)}</h3><p class='mae-cell-limitation'>{_e(limitation)}</p></div>"
        f"<div class='mae-cell-score'>{_e(score_text)}<span>{_e(status)}</span></div></div>",
        unsafe_allow_html=True,
    )
    fields = [
        ("Score status", status),
        ("Nullable score", score_text),
        ("Evidence mode", cell.get("evidence_mode") or "NONE"),
        ("Confidence", cell.get("confidence") or "NOT_ASSIGNED"),
        ("Approved-source count", int(cell.get("approved_source_count") or 0)),
        ("Review state", cell.get("review_status") or audit.analyst_review_status),
    ]
    st.markdown(_method_grid(fields), unsafe_allow_html=True)
    st.markdown(
        "<div class='mae-story-columns mae-audit-critical'><div>"
        f"<div class='mae-story-block'><h4>Limitation</h4><p>{_e(limitation)}</p></div>"
        f"<div class='mae-story-block'><h4>Rejection reason</h4><p>{_e(cell.get('rejection_reason') or 'Not rejected.')}</p></div>"
        "</div><div>"
        f"<div class='mae-story-block'><h4>Direction</h4><p>{_e(cell.get('direction') or 'Not inferred.')}</p></div>"
        f"<div class='mae-story-block'><h4>Cell ID</h4><p>{_e(cell.get('canonical_cell_id'))}</p></div>"
        "</div></div>",
        unsafe_allow_html=True,
    )
    evidence = [item for item in audit.evidence if item.get("canonical_cell_id") == cell.get("canonical_cell_id")]
    st.subheader("Evidence")
    if not evidence:
        st.caption("No financially eligible evidence is attached. The cell remains explicitly unscored or rejected.")
    for item in evidence:
        st.markdown(
            f"**{_e(item.get('publication_title') or 'Eligible evidence')}**  \n"
            f"{_e(item.get('exact_quote') or '')}  \n"
            f"Mode: {_e(item.get('evidence_mode'))} · confidence: {_e(item.get('confidence'))} · "
            f"approved sources: {_e(item.get('approved_source_count'))}"
        )


def _audit_cell_selector(
    audit: AuditDisclosure,
    key: str,
    *,
    default_asset: str,
    default_geography: str,
) -> dict[str, Any] | None:
    columns = st.columns(2)
    assets = [asset for asset in ASSET_ORDER if asset in set(audit.cells["asset_segment"])]
    with columns[0]:
        asset_index = assets.index(default_asset) if default_asset in assets else 0
        asset = st.selectbox("Asset segment", assets, index=asset_index, key=f"{key}_asset")
    available = audit.cells[audit.cells["asset_segment"].eq(asset)]["geography"].tolist()
    geographies = [geography for geography in GEOGRAPHY_ORDER if geography in available]
    with columns[1]:
        geography_index = geographies.index(default_geography) if default_geography in geographies else 0
        geography = st.selectbox("Geography", geographies, index=geography_index, key=f"{key}_geography")
    matches = audit.cells[audit.cells["asset_segment"].eq(asset) & audit.cells["geography"].eq(geography)]
    return matches.iloc[0].to_dict() if not matches.empty else None


def _render_change_section(title: str, rows: pd.DataFrame, empty_message: str) -> None:
    st.header(title)
    if rows.empty:
        st.caption(empty_message)
        return
    st.markdown(_change_rows(rows), unsafe_allow_html=True)


def _change_rows(rows: pd.DataFrame) -> str:
    if rows.empty:
        return ""
    rendered = ["<div class='mae-change-group'>"]
    for row in rows.to_dict("records"):
        label = CHANGE_LABELS.get(str(row.get("change_type")), "View Refined")
        rendered.append(
            "<div class='mae-change-row'>"
            f"<div class='mae-change-row__name'>{_e(row.get('geography'))} · {_e(row.get('asset_segment'))}</div>"
            f"<div class='mae-change-row__type'>{_e(label)}</div>"
            f"<div class='mae-change-row__text'>{_e(row.get('what_changed') or row.get('thesis') or '—')}</div>"
            "</div>"
        )
    rendered.append("</div>")
    return "".join(rendered)


def _scenario_card_html(bundle: FullSnapshotData, scenario: dict[str, Any]) -> str:
    scenario_id = str(scenario.get("scenario_id") or "")
    title = {"BASE": "BASE", "UPSIDE": "UPSIDE", "DOWNSIDE": "DOWNSIDE"}.get(scenario_id, scenario_id)
    winners = _friendly_cells(bundle, scenario.get("winners", ""))[:4]
    vulnerable = _friendly_cells(bundle, scenario.get("vulnerable_assets", ""))[:3]
    indicators = _split_items(scenario.get("indicators_to_watch"))[:4]
    causal = " → ".join(_split_chain(scenario.get("causal_chain", ""))[:5])
    base_class = " mae-scenario-card--base" if scenario_id == "BASE" else ""
    return (
        f"<article class='mae-scenario-card{base_class}'>"
        "<div class='mae-scenario-card__meta'>"
        f"<span>{_e(scenario.get('current_status') or '—')}</span>"
        f"<span>{_e(scenario.get('probability_band') or '—')} probability</span></div>"
        f"<h3>{_e(title)}</h3><p>{_e(scenario.get('narrative') or '—')}</p>"
        f"<h4>Causal chain</h4><p>{_e(causal)}</p>"
        f"<h4>Beneficiaries</h4>{_html_list(winners)}"
        f"<h4>Vulnerable assets</h4>{_html_list(vulnerable)}"
        f"<h4>Key indicators</h4>{_html_list(indicators)}"
        f"<h4>Trigger</h4><p>{_e(scenario.get('trigger') or '—')}</p>"
        f"<h4>Invalidation</h4><p>{_e(scenario.get('veto') or '—')}</p>"
        "</article>"
    )


def _scenario_chain_html(value: Any) -> str:
    steps = _split_chain(value)
    labels = ["Driver", "Macro transmission", "Market impact", "Asset winners / losers"]
    if len(steps) > 4:
        steps = [steps[0], " → ".join(steps[1:-2]), steps[-2], steps[-1]]
    while len(steps) < 4:
        steps.append("—")
    parts = ["<div class='mae-chain'>"]
    for index, (label, step) in enumerate(zip(labels, steps[:4], strict=True)):
        if index:
            parts.append("<div class='mae-chain__arrow'>→</div>")
        parts.append(f"<div class='mae-chain__step'><b>{_e(label)}</b>{_e(step)}</div>")
    parts.append("</div>")
    return "".join(parts)


def _market_map_filters(scores: pd.DataFrame) -> pd.DataFrame:
    columns = st.columns(3)
    with columns[0]:
        assets = st.multiselect("Asset segments", ASSET_ORDER, key="map_assets")
    with columns[1]:
        geographies = st.multiselect("Geographies", GEOGRAPHY_ORDER, key="map_geographies")
    with columns[2]:
        score_range = st.slider("Score range", -3, 3, (-3, 3), key="map_score_range")
    filtered = scores.copy()
    if assets:
        filtered = filtered[filtered["asset_segment"].isin(assets)]
    if geographies:
        filtered = filtered[filtered["geography"].isin(geographies)]
    applicable = filtered["applicable"].astype(str).str.lower().eq("true")
    numeric = filtered["score"].map(_score_int)
    return filtered[(~applicable) | numeric.between(*score_range)]


def _cell_selector(
    bundle: FullSnapshotData,
    key: str,
    *,
    default_asset: str,
    default_geography: str,
) -> dict[str, Any] | None:
    columns = st.columns(2)
    with columns[0]:
        asset_index = ASSET_ORDER.index(default_asset) if default_asset in ASSET_ORDER else 0
        asset = st.selectbox("Asset segment", ASSET_ORDER, index=asset_index, key=f"{key}_asset")
    available_geographies = bundle.scores[bundle.scores["asset_segment"].eq(asset)]["geography"].tolist()
    geographies = [geo for geo in GEOGRAPHY_ORDER if geo in available_geographies]
    with columns[1]:
        geo_index = geographies.index(default_geography) if default_geography in geographies else 0
        geography = st.selectbox("Geography", geographies, index=geo_index, key=f"{key}_geography")
    return get_cell(bundle, asset, geography)


def _deduplicated_sources(evidence: pd.DataFrame) -> pd.DataFrame:
    columns = ["provider", "title", "publication_date", "URL", "excerpt", "geography", "asset_segment"]
    if evidence.empty or not set(columns).issubset(evidence.columns):
        return pd.DataFrame(columns=["provider", "title", "publication_date", "url", "description", "affected_areas"])
    rows = []
    grouped = evidence.groupby(["provider", "title", "publication_date", "URL"], dropna=False, sort=False)
    for (provider, title, publication_date, url), group in grouped:
        areas = sorted({f"{row.geography} · {row.asset_segment}" for row in group.itertuples(index=False)})
        description = next((str(value) for value in group["excerpt"] if str(value).strip()), "")
        rows.append(
            {
                "provider": str(provider),
                "title": str(title),
                "publication_date": str(publication_date),
                "url": str(url),
                "description": _shorten(description, 210),
                "affected_areas": ", ".join(areas[:5]) + ("…" if len(areas) > 5 else ""),
            }
        )
    return pd.DataFrame(rows).sort_values(["publication_date", "provider"], ascending=[False, True], kind="stable")


def _source_card(row: dict[str, Any]) -> str:
    url = str(row.get("url") or row.get("URL") or "")
    link = (
        f"<a href='{_e(url)}' target='_blank' rel='noopener noreferrer'>Open source ↗</a>"
        if url.startswith(("https://", "http://"))
        else ""
    )
    affected = row.get("affected_areas") or ""
    description = row.get("description") or row.get("excerpt") or ""
    return (
        "<div class='mae-source-card'>"
        f"<div class='mae-source-card__issuer'>{_e(row.get('provider') or 'Source')}<br>{_e(row.get('publication_date') or '')}</div>"
        "<div>"
        f"<div class='mae-source-card__title'>{_e(row.get('title') or 'Untitled publication')}</div>"
        f"<div class='mae-source-card__desc'>{_e(description)}"
        + (f"<br><strong>Affected:</strong> {_e(affected)}" if affected else "")
        + "</div></div>"
        f"<div>{link}</div></div>"
    )


def _source_inbox_form(bundle: FullSnapshotData, editable: bool) -> None:
    if bundle.applicable_scores.empty:
        st.caption("Target cells are unavailable.")
        return
    labels = {
        f"{row.geography} × {row.asset_segment}": row.cell_id
        for row in bundle.applicable_scores.itertuples(index=False)
    }
    with st.form("source_inbox_form", clear_on_submit=True):
        url = st.text_input("URL", disabled=not editable)
        provider = st.text_input("Provider", disabled=not editable)
        publication_date = st.date_input("Publication date", value=date.today(), disabled=not editable)
        title = st.text_input("Title", disabled=not editable)
        target_label = st.selectbox("Target MAE cell", list(labels), disabled=not editable)
        excerpt = st.text_area("Optional excerpt", height=80, disabled=not editable)
        submitted = st.form_submit_button("Add to source inbox", disabled=not editable, width="stretch")
    if submitted:
        try:
            result = add_source_to_inbox(
                url=url,
                provider=provider,
                publication_date=publication_date,
                title=title,
                target_cell=labels[target_label],
                optional_excerpt=excerpt,
            )
            st.info(result["message"])
        except ValueError as exc:
            st.warning(str(exc))


def _heatmap_value(
    row: dict[str, Any] | None,
    *,
    kind: str,
    values: dict[str, int],
) -> tuple[str, str, str]:
    if not row or str(row.get("applicable") or "").lower() != "true":
        return "N/A", "mae-score-na", "Canonical non-applicability"
    if kind == "audit":
        status = str(row.get("score_status") or "UNAVAILABLE")
        labels = {
            "INSUFFICIENT_DATA": ("INS", "mae-score-insufficient"),
            "REJECTED": ("REJ", "mae-score-rejected"),
            "CARRY_FORWARD": ("CF", "mae-score-carry"),
        }
        if status in labels:
            value, css_class = labels[status]
            return value, css_class, f"{row.get('geography')} · {row.get('asset_segment')}: {status} — {row.get('limitation_reason') or row.get('rejection_reason')}"
        if status != "ACTIVE" or pd.isna(row.get("score")):
            return "?", "mae-score-insufficient", f"Unexpected audit state: {status}"
        value = int(float(row["score"]))
    elif kind == "scenario":
        value = values.get(str(row.get("cell_id")), 0)
    elif kind == "delta":
        raw = str(row.get("score_delta") or "").strip()
        if not raw or str(row.get("change_type")) == "NEW_COMPARABLE_ASSESSMENT":
            return "NEW", "mae-score-new", "New comparable assessment"
        value = _score_int(raw)
    else:
        value = _score_int(row.get("score"))
    css = f"mae-score-neg-{abs(value)}" if value < 0 else f"mae-score-{value}"
    title = f"{row.get('geography')} · {row.get('asset_segment')}: {_format_score(value)}"
    return _format_score(value), css, title


def _audit_ranked_cells(rows: pd.DataFrame) -> str:
    parts = ["<div class='mae-ranked-list'>"]
    for index, row in enumerate(rows.to_dict("records"), start=1):
        parts.append(
            "<div class='mae-ranked-item'>"
            f"<div class='mae-ranked-item__num'>{index:02d}</div>"
            f"<div class='mae-ranked-item__name'>{_e(row.get('geography'))} · {_e(row.get('asset_segment'))}</div>"
            f"<div class='mae-ranked-item__score'>{_e(_format_score(row.get('score')))}</div>"
            f"<div class='mae-ranked-item__why'><span class='mae-section-kicker'>{_e(row.get('score_status'))}</span>"
            f"{_e(row.get('limitation_reason') or 'Eligible candidate score.')}</div></div>"
        )
    parts.append("</div>")
    return "".join(parts)


def _empty_audit_panel(title: str, state: dict[str, Any]) -> str:
    return (
        "<div class='mae-audit-empty mae-audit-critical'>"
        f"<span>{_e(title)}</span><strong>{_e(state.get('status') or 'UNAVAILABLE')}</strong>"
        f"<p>{_e(state.get('reason') or 'No eligible records were generated.')}</p>"
        f"<small>Eligible records: {len(state.get('records') or [])}</small></div>"
    )


def _ranked_cells(rows: pd.DataFrame, *, positive: bool) -> str:
    parts = ["<div class='mae-ranked-list'>"]
    for index, row in enumerate(rows.to_dict("records"), start=1):
        why = row.get("main_driver") or _plain_thesis(row.get("thesis"))
        label = "Why now" if positive else "Key pressure"
        parts.append(
            "<div class='mae-ranked-item'>"
            f"<div class='mae-ranked-item__num'>{index:02d}</div>"
            f"<div class='mae-ranked-item__name'>{_e(row.get('geography'))} · {_e(row.get('asset_segment'))}</div>"
            f"<div class='mae-ranked-item__score'>{_e(_format_score(row.get('score')))}</div>"
            f"<div class='mae-ranked-item__why'><span class='mae-section-kicker'>{label}</span>{_e(why)}</div>"
            "</div>"
        )
    parts.append("</div>")
    return "".join(parts)


def _topic_copy(bundle: FullSnapshotData, cells: list[tuple[str, str]]) -> str:
    parts = []
    for asset, geography in cells:
        cell = get_cell(bundle, asset, geography)
        if not cell:
            continue
        value = str(cell.get("main_driver") or "").strip()
        if value and value not in parts:
            parts.append(value.rstrip(".") + ".")
    return " ".join(parts[:2]) or "The canonical view remains under review."


def _change_summary(market: int, methodology: int, new: int, unchanged: int) -> str:
    if market == 0:
        lead = "No material market-view changes were identified."
    else:
        lead = f"{market} market-driven view changes were identified."
    details = []
    if methodology:
        details.append(f"{methodology} benchmark or methodology refinements improved comparability")
    if new:
        details.append(f"{new} new comparable assessments were added")
    if unchanged:
        details.append(f"{unchanged} views were maintained")
    return lead + (" " + "; ".join(details) + "." if details else "")


def _market_deck(summary: str) -> str:
    lowered = summary.casefold()
    parts = []
    if "устойчив" in lowered or "resilien" in lowered:
        parts.append("Resilient but uneven growth")
    if "инфляц" in lowered or "inflation" in lowered:
        parts.append("sticky inflation")
    if "стоимост" in lowered or "став" in lowered or "rates" in lowered:
        parts.append("a high cost of capital")
    if "селектив" in lowered or "selective" in lowered or "концентрац" in lowered:
        parts.append("selective risk appetite")
    if parts:
        return ", ".join(parts[:-1]) + (" and " + parts[-1] if len(parts) > 1 else parts[0]) + "."
    sentences = _sentences(summary)
    return sentences[0] if sentences else "A selective 6–12 month view across global assets."


def _editorial_paragraphs(summary: str) -> list[str]:
    sentences = [
        sentence
        for sentence in _sentences(summary)
        if not any(token in sentence.casefold() for token in ["canonical", "conviction", "proxy"])
    ][:6]
    if not sentences:
        return ["The canonical market summary is temporarily unavailable."]
    if len(sentences) <= 4:
        return sentences
    return [" ".join(sentences[:2]), " ".join(sentences[2:4]), " ".join(sentences[4:])]


def _plain_thesis(value: Any) -> str:
    text = " ".join(str(value or "").split())
    text = re.sub(r"^[^:]{1,80}:\s*", "", text)
    text = re.sub(r"\s*Market confirmation:.*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*comparability=[A-Z_]+\.?", "", text, flags=re.IGNORECASE)
    return text or "The canonical thesis remains active."


def _base_case_label(bundle: FullSnapshotData) -> str:
    base = _scenario_row(bundle, "BASE")
    narrative = str(base.get("narrative") or "").casefold()
    if "устойчив" in narrative or "resilien" in narrative:
        return "Resilient growth"
    return str(base.get("scenario_name") or "Base")


def _scenario_row(bundle: FullSnapshotData, scenario_id: str) -> dict[str, Any]:
    if bundle.scenarios.empty:
        return {}
    rows = bundle.scenarios[bundle.scenarios["scenario_id"].astype(str).eq(scenario_id)]
    return rows.iloc[0].to_dict() if not rows.empty else {}


def _ordered_scenarios(scenarios: pd.DataFrame) -> list[dict[str, Any]]:
    order = {"BASE": 0, "UPSIDE": 1, "DOWNSIDE": 2}
    return sorted(scenarios.to_dict("records"), key=lambda row: order.get(str(row.get("scenario_id")), 9))


def _friendly_cells(bundle: FullSnapshotData, value: Any) -> list[str]:
    lookup = {
        str(row.cell_id): f"{row.geography} {row.asset_segment}"
        for row in bundle.scores.itertuples(index=False)
    }
    return [lookup.get(item, item.replace("_", " ").title()) for item in _split_items(value)]


def _cached_historical_analogs() -> dict[str, Any]:
    path = ROOT_DIR / "outputs" / "mae_historical_analogs_latest.json"
    token = path.stat().st_mtime_ns if path.exists() else 0
    return _load_analogs_cached(str(path), token)


@st.cache_data(show_spinner=False)
def _load_analogs_cached(path: str, _mtime_ns: int) -> dict[str, Any]:
    del _mtime_ns
    return load_historical_analogs(root=Path(path).parents[1])


def _simple_table(headers: list[str], rows: list[list[Any]] | list[tuple[Any, ...]]) -> str:
    head = "".join(f"<th>{_e(value)}</th>" for value in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{_e(value)}</td>" for value in row) + "</tr>"
        for row in rows
    )
    return f"<table class='mae-simple-table'><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _method_grid(fields: list[tuple[str, Any]]) -> str:
    return "<div class='mae-method-grid'>" + "".join(
        f"<div class='mae-method-item'><span>{_e(label)}</span><strong>{_e('—' if value is None or value == '' else value)}</strong></div>"
        for label, value in fields
    ) + "</div>"


def _html_list(values: list[Any]) -> str:
    items = [str(value).strip() for value in values if str(value or "").strip()]
    return "<ul>" + "".join(f"<li>{_e(value)}</li>" for value in items[:6]) + "</ul>"


def _sentences(value: Any) -> list[str]:
    text = " ".join(str(value or "").split())
    return [item.strip() for item in re.split(r"(?<=[.!?])\s+", text) if item.strip()]


def _split_chain(value: Any) -> list[str]:
    return [item.strip(" .") for item in re.split(r"\s*→\s*", str(value or "")) if item.strip(" .")]


def _split_items(value: Any) -> list[str]:
    return [item.strip() for item in re.split(r"\s*[;\n]\s*", str(value or "")) if item.strip()]


def _shorten(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _score_int(value: Any) -> int:
    try:
        return int(float(str(value).replace("+", "").strip()))
    except (TypeError, ValueError):
        return 0


def _format_score(value: Any) -> str:
    score = _score_int(value)
    return f"{score:+d}" if score else "0"


def _score_direction(score: int) -> str:
    if score >= 2:
        return "Overweight"
    if score > 0:
        return "Positive tilt"
    if score <= -2:
        return "Underweight"
    if score < 0:
        return "Negative tilt"
    return "Neutral"


def _return_label(value: Any) -> str:
    if value is None or pd.isna(value):
        return "N/A"
    return f"{float(value):+.1f}%"


def _analog_incomplete_reasons(reasons: list[str]) -> str:
    labels = {
        "TOO_RECENT_FOR_MINIMUM_SEPARATION": "Too recent for minimum separation",
        "INCOMPLETE_REALIZED_6M_12M_OUTCOMES": "Incomplete realized 6m/12m outcomes",
    }
    return "; ".join(labels.get(reason, reason.replace("_", " ").title()) for reason in reasons) or "Incomplete outcome window"


def _recent_analog_cards(records: list[dict[str, Any]]) -> str:
    cards = []
    for item in records:
        cards.append(
            "<article class='mae-recent-card'>"
            f"<h4>{_e(item.get('period') or '—')}</h4>"
            "<div class='mae-recent-card__metrics'>"
            f"<span>Adjusted<strong>{float(item.get('adjusted_similarity') or 0):.1f}</strong></span>"
            f"<span>Coverage<strong>{float(item.get('factor_coverage') or 0):.0%}</strong></span>"
            f"<span>Confidence<strong>{_e(item.get('confidence') or '—')}</strong></span></div>"
            f"<p>{_e(_analog_incomplete_reasons(item.get('incomplete_reasons') or []))}</p></article>"
        )
    return "<div class='mae-recent-grid'>" + "".join(cards) + "</div>"


def _analog_date(period: str) -> str:
    try:
        return datetime.strptime(period, "%Y-%m").strftime("%b %Y")
    except ValueError:
        return period


def _long_date(value: Any) -> str:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").strftime("%-d %B %Y")
    except (TypeError, ValueError):
        return str(value or "—")


def _compact_date(value: Any) -> str:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").strftime("%-d %b %Y").upper()
    except (TypeError, ValueError):
        return str(value or "—").upper()


def _require_snapshot(bundle: FullSnapshotData) -> bool:
    if bundle.available:
        return True
    st.error("The production baseline is unavailable. Check Admin / Methodology.")
    return False


def _require_audit(audit: AuditDisclosure) -> bool:
    if audit.available and audit.is_quarantined:
        return True
    st.error("The quarantined financial-audit candidate is unavailable or has an invalid release state.")
    for error in audit.errors:
        st.caption(error)
    return False


def _render_update_result(result: dict[str, Any] | None) -> None:
    if not result:
        return
    with st.expander("Last refresh", expanded=False):
        st.write(f"Status: {result.get('status', '—')}")
        st.write(f"Validation: {result.get('validation_result', '—')}")
        st.write(f"Snapshot: {result.get('snapshot_date', '—')}")


def _download_button(label: str, filename: str, mime: str, *, key: str) -> None:
    path = ROOT_DIR / "outputs" / filename
    if not path.exists():
        st.caption(f"{label} · temporarily unavailable")
        return
    st.download_button(
        label,
        data=_file_bytes(str(path), path.stat().st_mtime_ns),
        file_name=filename,
        mime=mime,
        key=key,
        width="stretch",
    )


def _download_path_button(label: str, path: Path | None, mime: str, *, key: str) -> None:
    if not path or not path.is_file():
        st.caption(f"{label} · temporarily unavailable")
        return
    st.download_button(
        label,
        data=_file_bytes(str(path), path.stat().st_mtime_ns),
        file_name=path.name,
        mime=mime,
        key=key,
        width="stretch",
    )


@st.cache_data(show_spinner=False)
def _file_bytes(path: str, _mtime_ns: int) -> bytes:
    del _mtime_ns
    return Path(path).read_bytes()


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


# Compatibility aliases keep the analytical module entry points available to
# non-navigation consumers while the public UI uses the editorial names.
strategy_page = market_map_page
change_tracker_page = what_changed_page
evidence_page = sources_downloads_page
transmission_page = sources_downloads_page
