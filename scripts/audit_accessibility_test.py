from __future__ import annotations

import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "outputs" / "autonomous" / "qa" / "accessibility_assertions.json"
ROUTES = [
    ("Рыночная картина", "/"),
    ("Матрица MAE", "/market-map"),
    ("Что изменилось", "/what-changed"),
    ("Сценарии", "/scenarios"),
    ("Исторические аналоги", "/historical-analogs"),
    ("Источники и отчёты", "/sources-downloads"),
]
COLOR_PAIRS = [
    ("body normal", "#111111", "#F4F4F1", 4.5),
    ("muted normal", "#686868", "#F4F4F1", 4.5),
    ("sidebar normal", "#D6D6D0", "#161616", 4.5),
    ("accent control", "#111111", "#C9FF38", 4.5),
    ("matrix neutral", "#363633", "#D8D8D2", 4.5),
    ("matrix positive", "#172619", "#94B493", 4.5),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Keyboard, accessible-name and controlled-color audit.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8501")
    parser.add_argument("--browser", choices=["chromium", "webkit", "all"], default="all")
    args = parser.parse_args()
    engines = ["chromium", "webkit"] if args.browser == "all" else [args.browser]
    findings: dict[str, object] = {"routes": [], "keyboard": [], "contrast": []}

    for label, foreground, background, threshold in COLOR_PAIRS:
        ratio = contrast_ratio(foreground, background)
        if ratio < threshold:
            raise AssertionError(f"Contrast failure {label}: {ratio:.2f}:1 < {threshold}:1")
        findings["contrast"].append({"label": label, "ratio": round(ratio, 2), "threshold": threshold})

    with sync_playwright() as playwright:
        for engine in engines:
            browser = getattr(playwright, engine).launch(headless=True)
            try:
                context = browser.new_context(viewport={"width": 1440, "height": 1000})
                page = context.new_page()
                for heading, path in ROUTES:
                    page.goto(args.base_url.rstrip("/") + path, wait_until="domcontentloaded", timeout=30_000)
                    page.get_by_role("heading", name=heading, exact=True).first.wait_for(timeout=30_000)
                    unnamed = page.locator("a:visible,button:visible,input:visible,textarea:visible,[role=combobox]:visible").evaluate_all(
                        """
                        elements => elements.filter((element) => {
                          const name = element.getAttribute('aria-label') || element.getAttribute('title') ||
                            element.innerText || element.getAttribute('placeholder') || element.value;
                          return !String(name || '').trim();
                        }).map((element) => element.outerHTML.slice(0, 180))
                        """
                    )
                    if unnamed:
                        raise AssertionError(f"Unnamed interactive control(s) on {engine} {path}: {unnamed}")
                    findings["routes"].append({"engine": engine, "route": path, "unnamed_controls": 0})

                page.goto(args.base_url, wait_until="domcontentloaded", timeout=30_000)
                page.get_by_role("heading", name="Рыночная картина", exact=True).first.wait_for(timeout=30_000)
                focus_sequence = []
                for _ in range(28):
                    page.keyboard.press("Tab")
                    focus = page.evaluate(
                        """
                        () => {
                          const element = document.activeElement;
                          const style = getComputedStyle(element);
                          return {
                            tag: element?.tagName || '',
                            name: String(element?.getAttribute('aria-label') || element?.innerText || element?.getAttribute('title') || '').trim().slice(0, 120),
                            outline: style.outlineStyle,
                            outlineWidth: style.outlineWidth,
                            visible: Boolean(element && element.getClientRects().length),
                          };
                        }
                        """
                    )
                    # Browsers may expose BODY as the transient activeElement before
                    # Streamlit's first focusable control receives focus. BODY is not
                    # an interactive control and must not be scored as one.
                    if focus["visible"] and focus["tag"] != "BODY":
                        focus_sequence.append(focus)
                unique = {(item["tag"], item["name"]) for item in focus_sequence}
                invisible_focus = [item for item in focus_sequence if item["outline"] == "none" or item["outlineWidth"] == "0px"]
                # Safari/WebKit on macOS follows the platform preference that skips
                # links during Tab traversal. Link focus/activation is asserted
                # explicitly below, so the two remaining document controls are the
                # correct minimum for that engine.
                minimum_unique = 2 if engine == "webkit" else 4
                if len(unique) < minimum_unique:
                    raise AssertionError(
                        f"Keyboard traversal reached only {len(unique)} unique controls on {engine}: {sorted(unique)}"
                    )
                if invisible_focus:
                    raise AssertionError(f"Focus indicator missing on {engine}: {invisible_focus[:3]}")
                before_back = page.evaluate("() => document.activeElement?.outerHTML")
                page.keyboard.press("Shift+Tab")
                after_back = page.evaluate("() => document.activeElement?.outerHTML")
                if before_back == after_back:
                    raise AssertionError(f"Reverse traversal did not move focus on {engine}")

                page.goto(args.base_url, wait_until="domcontentloaded", timeout=30_000)
                page.get_by_role("heading", name="Рыночная картина", exact=True).first.wait_for(timeout=30_000)
                market_map_link = page.get_by_role("link", name="Матрица MAE", exact=True)
                if market_map_link.count() != 1:
                    raise AssertionError(f"Matrix link is not unique on {engine}")
                market_map_link.focus()
                focused_link = market_map_link.evaluate(
                    "element => ({active: document.activeElement === element, outlineWidth: getComputedStyle(element).outlineWidth})"
                )
                if not focused_link["active"] or focused_link["outlineWidth"] != "3px":
                    raise AssertionError(f"Keyboard link focus is not visible on {engine}: {focused_link}")
                page.keyboard.press("Enter")
                page.wait_for_url("**/market-map", timeout=30_000)
                page.get_by_role("heading", name="Матрица MAE", exact=True).first.wait_for(timeout=30_000)
                findings["keyboard"].append(
                    {
                        "engine": engine,
                        "tab_steps": 28,
                        "unique_controls": len(unique),
                        "focus_indicator_failures": 0,
                        "reverse_traversal": "PASS",
                        "nav_link_focus_outline": "3px",
                        "nav_link_enter_activation": "PASS",
                    }
                )
                context.close()
            finally:
                browser.close()

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(findings, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Accessibility audit passed: {len(findings['routes'])} route-engine checks")
    print(f"Assertions: {OUTPUT.resolve()}")
    return 0


def contrast_ratio(foreground: str, background: str) -> float:
    lighter, darker = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def luminance(color: str) -> float:
    components = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in components]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


if __name__ == "__main__":
    raise SystemExit(main())
