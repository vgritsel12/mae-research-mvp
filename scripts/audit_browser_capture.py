from __future__ import annotations

import argparse
import json
from pathlib import Path

from playwright.sync_api import BrowserType, Page, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = (
    ROOT
    / "outputs"
    / "candidates"
    / "2026-07-13-financial-remediation"
    / "ui_evidence"
)
BANNER = (
    "Аналитика сформирована автоматически на основе открытых рыночных и институциональных источников. "
    "Материал не является индивидуальной инвестиционной рекомендацией."
)
SURFACES = [
    ("overview", "Рыночная картина"),
    ("cell_audit", "Матрица MAE"),
    ("evidence", "Источники и отчёты"),
    ("what_changed", "Что изменилось"),
    ("historical_analogs", "Исторические аналоги"),
]
VIEWPORTS = {
    "desktop": {"width": 1440, "height": 1000},
    "narrow": {"width": 390, "height": 844},
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture and assert the five MAE audit disclosure surfaces.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8501")
    parser.add_argument("--engine", choices=["chromium", "webkit", "all"], default="all")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    engines = ["chromium", "webkit"] if args.engine == "all" else [args.engine]
    assertions: list[dict[str, object]] = []
    with sync_playwright() as playwright:
        for engine_name in engines:
            browser_type: BrowserType = getattr(playwright, engine_name)
            browser = browser_type.launch(headless=True)
            try:
                for viewport_name, viewport in VIEWPORTS.items():
                    assertions.extend(
                        capture_viewport(
                            browser,
                            engine_name=engine_name,
                            viewport_name=viewport_name,
                            viewport=viewport,
                            base_url=args.base_url,
                            output=args.output,
                        )
                    )
            finally:
                browser.close()

    manifest = args.output / "browser_assertions.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(assertions, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Browser audit capture passed: {len(assertions)} surfaces")
    print(f"Manifest: {manifest.resolve()}")
    return 0


def capture_viewport(
    browser,
    *,
    engine_name: str,
    viewport_name: str,
    viewport: dict[str, int],
    base_url: str,
    output: Path,
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for index, (surface, heading) in enumerate(SURFACES):
        context = browser.new_context(viewport=viewport)
        page = context.new_page()
        page_errors: list[str] = []
        console_errors: list[str] = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
        page.goto(base_url, wait_until="domcontentloaded", timeout=30_000)
        if index:
            page.get_by_role("link", name=heading, exact=True).first.click(timeout=10_000)
        try:
            page.get_by_role("heading", name=heading, exact=False).first.wait_for(timeout=30_000)
            if viewport_name == "narrow":
                collapse = page.get_by_test_id("stSidebarCollapseButton")
                if collapse.count() and collapse.is_visible():
                    collapse.click(timeout=10_000)
            page.wait_for_timeout(700)
            result = assert_surface(page, page_errors, console_errors)
            focus_heading = {
                "cell_audit": "Карточка позиции",
                "evidence": "Источники",
                "what_changed": "Изменения оценок",
                "historical_analogs": "Наиболее похожие периоды",
            }.get(surface)
            if focus_heading:
                page.get_by_role("heading", name=focus_heading, exact=True).first.evaluate(
                    "element => element.scrollIntoView({block: 'start'})"
                )
                page.wait_for_timeout(250)
            destination = output / engine_name / viewport_name / f"{surface}.png"
            destination.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(destination), full_page=False)
            results.append(
                {
                    "engine": engine_name,
                    "viewport": viewport_name,
                    "surface": surface,
                    "heading": heading,
                    "screenshot": str(destination.resolve()),
                    **result,
                }
            )
        finally:
            context.close()
    return results


def assert_surface(page: Page, page_errors: list[str], console_errors: list[str]) -> dict[str, object]:
    body = page.locator("body").inner_text(timeout=10_000)
    banner_count = page.get_by_text(BANNER, exact=True).count()
    layout = page.evaluate(
        """
        () => {
          const critical = [...document.querySelectorAll('.mae-quarantine-banner,.mae-audit-critical,.mae-method-grid')];
          return {
            globalOverflow: document.documentElement.scrollWidth > window.innerWidth + 1,
            clippedCritical: critical.filter((element) => {
              const bounds = element.getBoundingClientRect();
              return bounds.left < -1 || bounds.right > window.innerWidth + 1;
            }).length,
            streamlitExceptions: document.querySelectorAll('[data-testid="stException"]').length,
          };
        }
        """
    )
    relevant_console = [message for message in console_errors if "favicon" not in message.lower()]
    if banner_count != 1:
        raise AssertionError(f"Expected one exact disclosure, found {banner_count}")
    if "Current snapshot" in body:
        raise AssertionError("Contradictory Current snapshot wording is visible")
    for token in ["QUARANTINED", "AUTO_PUBLISHED", "NOT_REQUESTED", "THEME_SYNTHESIS"]:
        if token in body:
            raise AssertionError(f"Technical status is visible: {token}")
    if layout["globalOverflow"] or layout["clippedCritical"]:
        raise AssertionError(f"Audit-critical horizontal clipping: {layout}")
    if layout["streamlitExceptions"] or page_errors or relevant_console:
        raise AssertionError(
            f"Browser exception(s): streamlit={layout['streamlitExceptions']} page={page_errors} console={relevant_console}"
        )
    return {
        "banner_count": banner_count,
        "global_overflow": layout["globalOverflow"],
        "clipped_critical": layout["clippedCritical"],
        "streamlit_exceptions": layout["streamlitExceptions"],
        "page_errors": len(page_errors),
        "console_errors": len(relevant_console),
    }


if __name__ == "__main__":
    raise SystemExit(main())
