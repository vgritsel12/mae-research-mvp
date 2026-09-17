from __future__ import annotations

import logging
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st

from app.config import get_settings
from app.logging_config import configure_logging
from app.services.full_snapshot_loader import load_full_snapshot_cached, snapshot_cache_token
from app.ui import design
from app.ui.autonomous_pages import (
    USER_SECTIONS,
    historical_analogs_page,
    market_map_page,
    market_view_page,
    render_header,
    render_page_safely,
    render_sidebar,
    scenarios_page,
    sources_downloads_page,
    what_changed_page,
)


st.set_page_config(
    page_title="MAE · Рыночная картина",
    layout="wide",
    initial_sidebar_state="expanded",
)
configure_logging()
settings = get_settings()
design.apply_full_snapshot_style()

# Startup is intentionally read-only. Database migrations, baseline seeding,
# collectors, builds, validation writes, and API requests run only from their
# explicit service actions—not from a page view.
token = snapshot_cache_token(ROOT, settings.sqlite_path)
bundle = load_full_snapshot_cached(str(ROOT), str(settings.sqlite_path), token)
render_sidebar(bundle, settings)

logging.getLogger(__name__).info(
    "MAE full UI startup source=%s snapshot=%s public_demo=%s",
    bundle.source_mode,
    bundle.snapshot_date,
    settings.public_demo,
)

PAGE_SPECS = {
    "market-view": (USER_SECTIONS[0], market_view_page),
    "market-map": (USER_SECTIONS[1], market_map_page),
    "what-changed": (USER_SECTIONS[2], what_changed_page),
    "scenarios": (USER_SECTIONS[3], scenarios_page),
    "historical-analogs": (USER_SECTIONS[4], historical_analogs_page),
    "sources-downloads": (USER_SECTIONS[5], sources_downloads_page),
}


def _page_runner(name: str, render_func):
    def run_page() -> None:
        render_header(bundle)
        render_page_safely(name, render_func, bundle, settings)

    run_page.__name__ = f"render_{name.lower().replace(' ', '_').replace('&', 'and')}"
    return run_page


LEGACY_SLUGS = {
    "strategy-map": "market-map",
    "change-tracker": "what-changed",
    "evidence": "sources-downloads",
    "transmission": "sources-downloads",
}
requested_slug = os.getenv("MAE_APPTEST_PAGE", "market-view")
requested_slug = LEGACY_SLUGS.get(requested_slug, requested_slug)
if requested_slug not in PAGE_SPECS:
    requested_slug = "market-view"

pages = []
for slug, (title, render_func) in PAGE_SPECS.items():
    pages.append(
        st.Page(
            _page_runner(title, render_func),
            title=title,
            url_path=slug,
            default=slug == requested_slug,
        )
    )

navigation = st.navigation({"MAE": pages}, position="sidebar", expanded=True)
navigation.run()
