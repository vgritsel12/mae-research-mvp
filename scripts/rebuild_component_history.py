from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.repositories.database import ENGINE, SessionLocal, init_database
from app.services.component_engine import default_snapshot_dates, invalidate_empty_backfill_snapshots, rebuild_component_history, write_component_maps
from app.services.core_validation import validate_core_history


def main() -> int:
    args = _parse_args()
    from_date = date.fromisoformat(args.from_date)
    to_date = date.fromisoformat(args.to_date)
    snapshot_dates = default_snapshot_dates(from_date, to_date)
    report_path = ROOT / "outputs" / "production_cleanup" / "component_history_report.json" if args.coverage_report else None
    init_database(ENGINE)
    print("MAE component history reconstruction")
    print(f"Mode: {'WRITE' if args.write else 'DRY RUN'}")
    print("Snapshot dates:", ", ".join(item.isoformat() for item in snapshot_dates))
    maps = write_component_maps(ROOT / "data")
    print(f"Applicability map: {maps[0]}")
    print(f"Exposure map: {maps[1]}")
    with SessionLocal() as session:
        if args.invalidate_empty_backfill and not args.write:
            invalid = invalidate_empty_backfill_snapshots(session)
            session.rollback()
            print(f"Empty research-only snapshots that would be invalidated: {invalid}")
        result = rebuild_component_history(
            session,
            snapshot_dates=snapshot_dates,
            from_date=from_date,
            to_date=to_date,
            dry_run=not args.write,
            replace_existing=args.replace_existing,
            backup=args.backup,
            report_path=report_path,
        )
        validation = None
        if args.write:
            validation = validate_core_history(session, ROOT / "outputs" / "mae_core_validation.csv")
        if args.write:
            session.commit()
        else:
            session.rollback()
    if result.backup_path:
        print(f"Backup created: {result.backup_path}")
    for snapshot in result.snapshots:
        row = snapshot.coverage_report
        counts = row["component_counts"]
        print(
            f"{row['snapshot_date']}: scored={row['scored_cells']} "
            f"core={row['core_cells']} data={counts['data']} market={counts['market']} research={counts['research']} "
            f"no_data={row['no_data_cells']}"
        )
    if report_path:
        print(f"Coverage report: {report_path}")
    if validation:
        print(
            f"Validation CSV: {validation['path']} "
            f"passed={validation['passed']} insufficient={validation['insufficient']} total={validation['total']}"
        )
    print("Saved snapshots:", len(result.saved_snapshot_ids))
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rebuild Research/Data/Market MAE component snapshots.")
    parser.add_argument("--from-date", default="2026-01-01")
    parser.add_argument("--to-date", default="2026-07-12")
    parser.add_argument("--frequency", default="month-end", choices=["month-end"])
    parser.add_argument("--backup", action="store_true", help="Create a SQLite backup before --write.")
    parser.add_argument("--coverage-report", action="store_true", help="Write JSON coverage report.")
    parser.add_argument("--write", action="store_true", help="Persist component snapshots.")
    parser.add_argument("--replace-existing", action="store_true", help="Replace existing immutable component snapshots.")
    parser.add_argument("--invalidate-empty-backfill", action="store_true", help="Mark old empty research-only snapshots invalid during --write.")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
