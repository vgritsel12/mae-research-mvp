from __future__ import annotations

import calendar
import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any


MONTHLY_MARKET_CHANGE = "MONTHLY_MARKET_CHANGE"
SAME_DAY_REVISION = "SAME_DAY_REVISION"
INITIAL_BASELINE = "INITIAL_BASELINE"


def comparison_metadata(
    *,
    root: Path,
    sqlite_path: Path,
    current_release_id: str,
    current_snapshot_date: str,
    technical_previous_snapshot_date: str,
) -> dict[str, Any]:
    current = parse_iso_date(current_snapshot_date)
    technical_previous = parse_iso_date(technical_previous_snapshot_date)
    if current is None:
        return {
            "current_release_id": current_release_id,
            "current_snapshot_date": current_snapshot_date,
            "snapshot_type": "INTERIM_SNAPSHOT",
            "baseline_release_id": "",
            "baseline_snapshot_date": technical_previous_snapshot_date,
            "comparison_type": INITIAL_BASELINE,
            "next_official_snapshot_date": "",
            "technical_previous_snapshot_date": technical_previous_snapshot_date,
        }

    official_month_end = month_end(current.year, current.month)
    snapshot_type = "FINAL_COMPONENT" if current == official_month_end else "INTERIM_SNAPSHOT"
    baseline_date = previous_month_end(current) if snapshot_type == "INTERIM_SNAPSHOT" else technical_previous
    next_official = official_month_end if current <= official_month_end else month_end(
        current.year + int(current.month == 12),
        1 if current.month == 12 else current.month + 1,
    )
    baseline_release_id = ""
    baseline_exists = False
    if baseline_date is not None:
        baseline_release_id = find_official_baseline_release_id(root=root, sqlite_path=sqlite_path, baseline_date=baseline_date)
        baseline_exists = bool(baseline_release_id) or has_immutable_snapshot(root=root, baseline_date=baseline_date)

    if not baseline_exists:
        comparison_type = INITIAL_BASELINE
    elif technical_previous == current:
        comparison_type = SAME_DAY_REVISION
    elif technical_previous == baseline_date:
        comparison_type = MONTHLY_MARKET_CHANGE
    else:
        comparison_type = INITIAL_BASELINE

    return {
        "current_release_id": current_release_id,
        "current_snapshot_date": current.isoformat(),
        "snapshot_type": snapshot_type,
        "baseline_release_id": baseline_release_id,
        "baseline_snapshot_date": baseline_date.isoformat() if baseline_date else "",
        "comparison_type": comparison_type,
        "next_official_snapshot_date": next_official.isoformat(),
        "technical_previous_snapshot_date": technical_previous_snapshot_date,
        "technical_previous_is_monthly_baseline": bool(technical_previous and baseline_date and technical_previous == baseline_date),
        "monthly_baseline_available": baseline_exists,
    }


def find_official_baseline_release_id(*, root: Path, sqlite_path: Path, baseline_date: date) -> str:
    versions = root / "outputs" / "releases" / "versions" / "autonomous-mae"
    if versions.is_dir():
        for validation_path in sorted(versions.glob("mae-*/stage_07_validation.json")):
            try:
                validation = json.loads(validation_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if str(validation.get("snapshot_date") or "")[:10] == baseline_date.isoformat():
                release_id = validation_path.parent.name
                if release_id.startswith("mae-"):
                    return release_id
    if sqlite_path.is_file():
        try:
            with sqlite3.connect(sqlite_path) as connection:
                row = connection.execute(
                    """
                    SELECT id, status, run_metadata
                    FROM mae_snapshots
                    WHERE snapshot_date = ? AND is_demo = 0
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (baseline_date.isoformat(),),
                ).fetchone()
        except sqlite3.Error:
            row = None
        if row:
            status = str(row[1] or "")
            if status and not status.startswith("INVALID"):
                return str(row[0] or "")
    return ""


def has_immutable_snapshot(*, root: Path, baseline_date: date) -> bool:
    stamp = baseline_date.isoformat()
    full_dir = root / "outputs" / "snapshots" / stamp / "full"
    required = [
        full_dir / f"mae_full_scores_{stamp}.csv",
        full_dir / f"mae_full_validation_{stamp}.json",
    ]
    return all(path.is_file() for path in required)


def parse_iso_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def previous_month_end(value: date) -> date:
    year = value.year - int(value.month == 1)
    month = 12 if value.month == 1 else value.month - 1
    return month_end(year, month)
