from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work"
SCREENSHOTS = ROOT / "outputs" / "ui_qa_autonomous"
PID_FILE = WORK / "demo_server.pid"
SERVER_LOG = WORK / "demo_server.log"
BASE_URL = "http://127.0.0.1:8501"

PAGES = [
    ("Рыночная картина", "market-view"),
    ("Матрица MAE", "market-map"),
    ("Что изменилось", "what-changed"),
    ("Сценарии", "scenarios"),
    ("Исторические аналоги", "historical-analogs"),
    ("Источники и отчёты", "sources-downloads"),
]

SCREENSHOT_NAMES = {
    "Рыночная картина": "market_view.png",
    "Матрица MAE": "market_map.png",
    "Что изменилось": "what_changed.png",
    "Сценарии": "scenarios.png",
    "Исторические аналоги": "historical_analogs.png",
    "Источники и отчёты": "sources_downloads.png",
}

FORBIDDEN_PAGE_TEXT = [
    "Connection error",
    "Streamlit server is not responding",
    "Traceback",
    "QUARANTINED",
    "Awaiting human approval",
    "Human approval required",
    "AUTO_PUBLISHED",
    "NOT_REQUESTED",
    "THEME_SYNTHESIS",
    "MODEL_INFERRED",
    "CARRY_FORWARD",
    "business_hash",
    "schema_version",
    "release_id",
    "candidate_id",
    "Change Log",
    "Whitelist",
    "APP_ENV",
    "PUBLIC_DEMO",
    "change_id",
    "Evidence assessment",
    "Override score",
    "No-look-ahead",
    "DATA_DRIVEN",
]
FORBIDDEN_CONSOLE = [
    "websocket",
    "connection error",
    "failed to fetch",
    "streamlit server is not responding",
]

DISCLOSURE = (
    "Аналитика сформирована автоматически на основе открытых рыночных и институциональных источников. "
    "Материал не является индивидуальной инвестиционной рекомендацией."
)

SURFACE_READY_HEADINGS = {
    "Рыночная картина": "Главный вывод",
    "Матрица MAE": "Карточка позиции",
    "Что изменилось": "Изменения оценок",
    "Сценарии": "Сценарии",
    "Исторические аналоги": "Наиболее похожие исторические периоды",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Real-browser MAE Streamlit navigation/stability test.")
    parser.add_argument("--cycles", type=int, default=50, help="Navigation cycles before idle wait.")
    parser.add_argument("--idle-seconds", type=int, default=600, help="Idle browser time after refresh.")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--skip-start", action="store_true", help="Use already-running server.")
    parser.add_argument("--headed", action="store_true", help="Run browser headed.")
    parser.add_argument("--browser", choices=["chromium", "webkit"], default="chromium")
    parser.add_argument("--timeout-seconds", type=int, default=900, help="Hard timeout for the whole E2E run.")
    parser.add_argument("--progress-interval", type=int, default=30, help="Seconds between idle progress messages.")
    args = parser.parse_args()

    WORK.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = WORK / f"browser_e2e_{run_id}.log"
    console_events: list[str] = []
    opened: list[str] = []
    route_assertions: list[str] = []
    clipping_checks: list[str] = []
    started_at = time.monotonic()

    def check_timeout(stage: str) -> None:
        elapsed = time.monotonic() - started_at
        if elapsed > args.timeout_seconds:
            raise TimeoutError(f"E2E timeout after {elapsed:.0f}s during {stage}; limit={args.timeout_seconds}s")

    try:
        screenshot_dir = SCREENSHOTS / args.browser
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        if not args.skip_start:
            run_script("scripts/start_demo.sh")
        pid = read_pid()
        log(log_path, f"started pid={pid} timeout={args.timeout_seconds}s progress_interval={args.progress_interval}s")
        assert_single_streamlit_process(pid)
        assert_health(args.base_url)

        with sync_playwright() as pw:
            check_timeout("browser startup")
            browser_type = getattr(pw, args.browser)
            browser = browser_type.launch(headless=not args.headed)
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            page = context.new_page()
            page.on("console", lambda msg: console_events.append(f"{msg.type}: {msg.text}"))
            page.on("pageerror", lambda exc: console_events.append(f"pageerror: {exc}"))
            page.goto(args.base_url, wait_until="domcontentloaded")
            page.get_by_role("heading", name=PAGES[0][0]).wait_for(timeout=30_000)
            assert_page_clean(page, console_events, PAGES[0][0], PAGES[0][1])
            assert_keyboard_focus(page)

            for cycle in range(args.cycles):
                check_timeout(f"cycle {cycle + 1}/{args.cycles}")
                for title, slug in PAGES:
                    open_page_by_click(page, title)
                    opened.append(title)
                    assert_heading(page, title)
                    assert_page_clean(page, console_events, title, slug)
                    route_assertions.append(f"desktop:{title}:{slug}:ok")
                    assert_pid_stable(pid)
                    assert_health(args.base_url)
                    if cycle == 0:
                        page.wait_for_timeout(1000)
                        page.screenshot(path=str(screenshot_dir / SCREENSHOT_NAMES[title]), full_page=True)
                        if title == "Матрица MAE":
                            page.get_by_role("heading", name="Карточка позиции").scroll_into_view_if_needed()
                            page.wait_for_timeout(300)
                            page.screenshot(path=str(screenshot_dir / "cell_story.png"), full_page=False)
                log(log_path, f"cycle {cycle + 1}/{args.cycles}: " + " | ".join(title for title, _ in PAGES))

            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_timeout(500)
            for title, slug in PAGES:
                open_page_direct(page, args.base_url, title, slug)
                assert_page_clean(page, console_events, title, slug)
                assert_no_page_clipping(page, title)
                route_assertions.append(f"narrow:{title}:{slug}:ok")
                clipping_checks.append(f"narrow:{title}:overflow=0")
                page.screenshot(
                    path=str(screenshot_dir / SCREENSHOT_NAMES[title].replace(".png", "_narrow.png")),
                    full_page=True,
                )

            page.set_viewport_size({"width": 1440, "height": 900})
            page.wait_for_timeout(300)

            page.reload(wait_until="domcontentloaded")
            page.get_by_role("heading", name=PAGES[-1][0]).wait_for(timeout=30_000)
            page.wait_for_timeout(1500)
            cleared = discard_expected_reload_events(console_events)
            if cleared:
                log(log_path, f"refresh console grace: cleared {cleared} expected WebSocket onclose event(s)")
            assert_page_clean(page, console_events, PAGES[-1][0], PAGES[-1][1])
            assert_pid_stable(pid)
            assert_health(args.base_url)
            log(log_path, "refresh: ok")

            deadline = time.monotonic() + args.idle_seconds
            idle_started = time.monotonic()
            next_progress = idle_started
            while time.monotonic() < deadline:
                check_timeout("idle")
                assert_pid_stable(pid)
                assert_health(args.base_url)
                assert_page_clean(page, console_events, PAGES[-1][0], PAGES[-1][1])
                now = time.monotonic()
                if now >= next_progress:
                    elapsed_idle = int(now - idle_started)
                    remaining_idle = max(0, int(deadline - now))
                    log(log_path, f"idle progress: {elapsed_idle}/{args.idle_seconds}s elapsed, {remaining_idle}s remaining")
                    next_progress = now + max(1, args.progress_interval)
                sleep_for = min(10, max(0.1, deadline - time.monotonic()), max(0.1, next_progress - time.monotonic()))
                time.sleep(sleep_for)
            log(log_path, f"idle {args.idle_seconds}s: ok")

            for title, slug in PAGES:
                check_timeout("final navigation")
                open_page_by_click(page, title)
                opened.append(title)
                assert_heading(page, title)
                assert_page_clean(page, console_events, title, slug)
                assert_pid_stable(pid)
                assert_health(args.base_url)
            browser.close()

        assert_no_forbidden_console(console_events)
        assert_single_streamlit_process(pid)
        assert_health(args.base_url)
        log(log_path, "opened pages: " + " | ".join(opened))
        log(log_path, "route assertions: " + " | ".join(route_assertions))
        log(log_path, "clipping checks: " + " | ".join(clipping_checks))
        log(log_path, f"console exceptions: 0; clipping failures: 0; screenshots={screenshot_dir}")
        print(f"Browser E2E passed. Browser={args.browser}. PID={pid}. Log={log_path}", flush=True)
        print("Opened pages: " + " | ".join(title for title, _ in PAGES), flush=True)
        print("Route/state assertions: 12 passed. Exception count=0. Clipping count=0.", flush=True)
        return 0
    except Exception as exc:  # noqa: BLE001
        diag = save_diagnostic(run_id, exc, console_events)
        print(f"Browser E2E failed: {exc}. Diagnostic: {diag}", file=sys.stderr)
        return 1


def run_script(script: str) -> None:
    result = subprocess.run([str(ROOT / script)], cwd=ROOT, text=True, capture_output=True, check=False)
    if result.stdout:
        print(result.stdout, end="", flush=True)
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr, flush=True)
    if result.returncode != 0:
        raise RuntimeError(f"{script} failed with exit code {result.returncode}")


def read_pid() -> int:
    if not PID_FILE.exists():
        raise RuntimeError(f"PID file missing: {PID_FILE}")
    return int(PID_FILE.read_text(encoding="utf-8").strip())


def assert_health(base_url: str) -> None:
    response = requests.get(base_url.rstrip("/") + "/_stcore/health", timeout=5)
    response.raise_for_status()
    if "ok" not in response.text.lower():
        raise AssertionError(f"Unexpected health response: {response.text[:200]}")


def assert_pid_stable(pid: int) -> None:
    if not Path(f"/proc/{pid}").exists() and sys.platform != "darwin":
        raise AssertionError(f"Server PID disappeared: {pid}")
    result = subprocess.run(["ps", "-p", str(pid), "-o", "pid="], text=True, capture_output=True, check=False)
    if result.returncode != 0 or str(pid) not in result.stdout:
        raise AssertionError(f"Server PID disappeared: {pid}")


def assert_single_streamlit_process(expected_pid: int) -> None:
    result = subprocess.run(["ps", "-axo", "pid=,command="], text=True, capture_output=True, check=True)
    rows = [
        line.strip()
        for line in result.stdout.splitlines()
        if "streamlit run" in line and ("app/main.py" in line or str(ROOT) in line)
    ]
    pids = [int(row.split(maxsplit=1)[0]) for row in rows if row.split(maxsplit=1)[0].isdigit()]
    if expected_pid not in pids:
        raise AssertionError(f"Expected Streamlit PID {expected_pid} not found in {pids}")
    if len(pids) != 1:
        raise AssertionError(f"Expected one Streamlit process, found {pids}")


def open_page_by_click(page: Page, title: str) -> None:
    locator = page.get_by_role("link", name=title).first
    if locator.count() == 0:
        locator = page.get_by_text(title, exact=True).first
    locator.click(timeout=10_000)
    page.get_by_role("heading", name=title).wait_for(timeout=30_000)


def open_page_direct(page: Page, base_url: str, title: str, slug: str) -> None:
    url = base_url.rstrip("/") + ("/" if title == "Рыночная картина" else f"/{slug}")
    page.goto(url, wait_until="domcontentloaded")
    page.get_by_role("heading", name=title).wait_for(timeout=30_000)


def assert_heading(page: Page, title: str) -> None:
    page.get_by_role("heading", name=title).first.wait_for(timeout=30_000)


def assert_page_clean(page: Page, console_events: list[str], title: str, slug: str) -> None:
    if title == "Источники и отчёты":
        page.get_by_role("button", name="Полный JSON-отчёт").wait_for(timeout=30_000)
    else:
        page.get_by_role("heading", name=SURFACE_READY_HEADINGS[title]).wait_for(timeout=30_000)
    body = page.locator("body").inner_text(timeout=10_000)
    for token in FORBIDDEN_PAGE_TEXT:
        if token in body:
            raise AssertionError(f"Forbidden page text found: {token}")
    if DISCLOSURE not in body:
        raise AssertionError("Exact autonomous disclosure is missing")
    valid_route = page.url.rstrip("/").endswith(slug)
    if title == "Рыночная картина" and page.url.rstrip("/") == BASE_URL:
        valid_route = True
    if not valid_route:
        raise AssertionError(f"Unexpected route for {title}: {page.url}")
    assert_surface_content(body, title)
    run_update = page.get_by_role("button", name="Обновить аналитику")
    if "Публичный просмотр · только чтение" in body and (run_update.count() != 1 or not run_update.is_disabled()):
        raise AssertionError("Public demo update control is not disabled")
    assert_no_forbidden_console(console_events)


def assert_surface_content(body: str, title: str) -> None:
    expected = {
        "Рыночная картина": ["Профессиональная сводка", "Главный вывод", "Наиболее привлекательные направления", "Главные риски"],
        "Матрица MAE": [
            "Карточка позиции",
            "Оценка относительной привлекательности",
            "Краткий инвестиционный вывод",
            "Почему такая оценка",
        ],
        "Что изменилось": ["Изменения оценок", "Обновления источников без изменения оценки"],
        "Сценарии": ["Базовый", "Позитивный", "Негативный", "Причинная цепочка"],
        "Исторические аналоги": ["Текущая рыночная конфигурация", "Наиболее похожие исторические периоды"],
        "Источники и отчёты": ["Источники", "Полный JSON-отчёт", "Рабочая книга Excel"],
    }
    missing = [token for token in expected[title] if token not in body]
    if missing:
        raise AssertionError(f"{title} is missing expected content: {missing}")


def assert_keyboard_focus(page: Page) -> None:
    page.keyboard.press("Tab")
    focused = page.evaluate("document.activeElement && document.activeElement.tagName")
    if focused in {None, "BODY", "HTML"}:
        raise AssertionError("Keyboard navigation did not move focus to an interactive element")


def assert_no_page_clipping(page: Page, title: str) -> None:
    overflow = page.evaluate(
        """() => ({
          document: Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth),
          body: Math.max(0, document.body.scrollWidth - document.body.clientWidth),
          main: (() => {
            const node = document.querySelector('[data-testid="stMain"]');
            return node ? Math.max(0, node.scrollWidth - node.clientWidth) : 0;
          })()
        })"""
    )
    if any(int(value) > 2 for value in overflow.values()):
        raise AssertionError(f"Horizontal clipping on {title}: {overflow}")


def assert_no_forbidden_console(console_events: list[str]) -> None:
    for event in console_events:
        lowered = event.lower()
        if "favicon" in lowered:
            continue
        if any(token in lowered for token in FORBIDDEN_CONSOLE):
            raise AssertionError(f"Forbidden browser console event: {event}")


def discard_expected_reload_events(console_events: list[str]) -> int:
    """Remove the WebSocket close emitted by Streamlit during an intentional page reload."""

    before = len(console_events)
    console_events[:] = [
        event
        for event in console_events
        if "websocket onclose" not in event.lower()
    ]
    return before - len(console_events)


def save_diagnostic(run_id: str, exc: Exception, console_events: list[str]) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = WORK / f"browser_e2e_failed_{run_id}_{stamp}.log"
    pid = PID_FILE.read_text(encoding="utf-8").strip() if PID_FILE.exists() else "missing"
    health = ""
    try:
        response = requests.get(BASE_URL + "/_stcore/health", timeout=5)
        health = f"{response.status_code} {response.text[:200]}"
    except Exception as health_exc:  # noqa: BLE001
        health = repr(health_exc)
    with path.open("w", encoding="utf-8") as f:
        f.write(f"time={datetime.now().isoformat()}\n")
        f.write(f"pid={pid}\n")
        f.write(f"health={health}\n")
        f.write(f"exception={type(exc).__name__}: {exc}\n")
        f.write("processes:\n")
        f.write(subprocess.run(["ps", "-axo", "pid=,ppid=,stat=,command="], text=True, capture_output=True).stdout)
        f.write("\nconsole_events:\n")
        f.write("\n".join(console_events[-200:]))
        f.write("\nlast_200_server_log_lines:\n")
        if SERVER_LOG.exists():
            f.write("\n".join(SERVER_LOG.read_text(encoding="utf-8", errors="ignore").splitlines()[-200:]))
    subprocess.run([str(ROOT / "scripts" / "status_demo.sh")], cwd=ROOT, check=False)
    return path


def log(path: Path, message: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} {message}"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
