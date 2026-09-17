from __future__ import annotations

import argparse
import json
import os
import platform
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.services import full_snapshot_loader


OUTPUT = ROOT / "outputs" / "autonomous" / "qa" / "performance_results.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure autonomous MAE product-view performance.")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    overview_seconds = warm_app_load("market-view")
    analog_seconds = warm_app_load("historical-analogs")
    select_queries = instrument_overview_queries()

    results = {
        "status": "PASSED",
        "runtime": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "cpu_count": os.cpu_count(),
        },
        "warm_overview_seconds": overview_seconds,
        "warm_analog_seconds": analog_seconds,
        "thresholds_seconds": {"overview": 3.0, "analog": 3.0},
        "overview_select_query_count": len(select_queries),
        "overview_select_queries": select_queries,
        "n_plus_one": False,
    }
    failures = []
    if overview_seconds > 3:
        failures.append(f"overview {overview_seconds:.3f}s > 3s")
    if analog_seconds > 3:
        failures.append(f"analog {analog_seconds:.3f}s > 3s")
    if len(select_queries) > 2:
        failures.append(f"overview executed {len(select_queries)} SELECTs; possible N+1")
        results["n_plus_one"] = True
    if failures:
        results["status"] = "FAILED"
        results["failures"] = failures
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if failures:
        raise AssertionError("; ".join(failures))
    print(
        f"Performance passed: overview={overview_seconds:.3f}s analog={analog_seconds:.3f}s "
        f"SELECTs={len(select_queries)}"
    )
    print(f"Results: {args.output.resolve()}")
    return 0


def warm_app_load(slug: str) -> float:
    os.environ["MAE_APPTEST_PAGE"] = slug
    app_path = str(ROOT / "app" / "main.py")
    AppTest.from_file(app_path, default_timeout=30).run()
    started = time.perf_counter()
    app = AppTest.from_file(app_path, default_timeout=30).run()
    elapsed = time.perf_counter() - started
    if app.exception:
        raise AssertionError(f"Streamlit exception during {slug} performance check: {app.exception}")
    return round(elapsed, 6)


def instrument_overview_queries() -> list[str]:
    settings = get_settings()
    original_connect = sqlite3.connect
    selects: list[str] = []

    def traced_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connection.set_trace_callback(
            lambda statement: selects.append(" ".join(statement.split())) if statement.lstrip().upper().startswith("SELECT") else None
        )
        return connection

    full_snapshot_loader._readonly_connection.clear()
    with patch.object(full_snapshot_loader.sqlite3, "connect", traced_connect):
        bundle = full_snapshot_loader.load_full_snapshot(root=ROOT, sqlite_path=settings.sqlite_path)
    if not bundle.available or not bundle.is_autonomous:
        raise AssertionError("Committed autonomous release unavailable during query instrumentation")
    return selects


if __name__ == "__main__":
    raise SystemExit(main())
