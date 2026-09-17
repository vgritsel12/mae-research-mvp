from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Iterable

import requests
from streamlit.testing.v1 import AppTest


ROOT = Path(__file__).resolve().parents[1]
PAGES = [
    "Рыночная картина",
    "Матрица MAE",
    "Что изменилось",
    "Сценарии",
    "Исторические аналоги",
    "Источники и отчёты",
]
PAGE_SLUGS = {
    "Рыночная картина": "market-view",
    "Матрица MAE": "market-map",
    "Что изменилось": "what-changed",
    "Сценарии": "scenarios",
    "Исторические аналоги": "historical-analogs",
    "Источники и отчёты": "sources-downloads",
}
FORBIDDEN_UI_TEXT = [
    "Traceback",
    "/Users/",
    "DATABASE_URL=",
    "Демонстрационная",
    "Демо",
    "DEMO",
    "REAL / DEMO",
    "Режим данных",
]
FORBIDDEN_LOG_TEXT = ["segmentation fault", "sigsegv", "exc_bad_access"]


def main() -> int:
    parser = argparse.ArgumentParser(description="MAE Streamlit UI smoke/stress test.")
    parser.add_argument("--url", default="http://127.0.0.1:8501", help="Running Streamlit base URL.")
    parser.add_argument("--cycles", type=int, default=20, help="Navigation cycles.")
    parser.add_argument("--skip-server", action="store_true", help="Skip HTTP health checks.")
    args = parser.parse_args()

    if not args.skip_server:
        check_health(args.url)

    for cycle in range(args.cycles):
        for page in PAGES:
            app = open_app_page(page)
            assert_clean(app)
            if page == "Матрица MAE":
                selectors = [item for item in app.selectbox if item.label in {"Класс активов", "Регион"}]
                if selectors:
                    selectors[-1].set_value(selectors[-1].options[0]).run()
                    assert_clean(app)
        print(f"cycle {cycle + 1}/{args.cycles}: ok")

    if not args.skip_server:
        check_health(args.url)
    check_logs()
    print("UI smoke test passed")
    return 0


def check_health(base_url: str) -> None:
    url = base_url.rstrip("/") + "/_stcore/health"
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    if "ok" not in response.text.lower():
        raise AssertionError(f"Unexpected health response from {url}: {response.text[:200]}")


def assert_clean(app: AppTest) -> None:
    if app.exception:
        raise AssertionError(f"Streamlit exception: {app.exception}")
    visible = visible_text(app)
    for token in FORBIDDEN_UI_TEXT:
        if token in visible:
            raise AssertionError(f"Forbidden UI text found: {token}")


def open_app_page(page: str) -> AppTest:
    os.environ["MAE_APPTEST_PAGE"] = PAGE_SLUGS[page]
    app = AppTest.from_file(str(ROOT / "app" / "main.py"), default_timeout=30)
    app.run()
    return app


def visible_text(app: AppTest) -> str:
    chunks: list[str] = []
    for collection in ["title", "header", "subheader", "markdown", "caption", "info", "warning", "error", "success"]:
        chunks.extend(str(getattr(element, "value", "")) for element in getattr(app, collection, []))
    return "\n".join(chunks)


def check_logs(paths: Iterable[Path] | None = None) -> None:
    candidates = list(paths or [ROOT / "work" / "streamlit.log"])
    for path in candidates:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        for token in FORBIDDEN_LOG_TEXT:
            if token in text:
                raise AssertionError(f"Native crash marker found in {path}: {token}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"UI smoke test failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
