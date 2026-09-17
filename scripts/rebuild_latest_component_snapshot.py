from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.repositories.database import ENGINE, SessionLocal, init_database
from app.services.component_engine import rebuild_component_history, write_component_maps
from app.services.core_validation import validate_latest_component_snapshot


def main() -> int:
    args = _parse_args()
    snapshot_date = date.fromisoformat(args.snapshot_date)
    init_database(ENGINE)
    print("MAE latest component snapshot rebuild")
    print(f"Snapshot date: {snapshot_date.isoformat()}")
    print(f"Mode: {'WRITE' if args.write else 'DRY RUN'}")
    maps = write_component_maps(ROOT / "data")
    print(f"Applicability map: {maps[0]}")
    print(f"Exposure map: {maps[1]}")
    with SessionLocal() as session:
        result = rebuild_component_history(
            session,
            snapshot_dates=[snapshot_date],
            dry_run=not args.write,
            replace_existing=args.replace_existing,
            backup=args.backup,
            report_path=ROOT / "outputs" / "production_cleanup" / "latest_component_report.json",
        )
        validation = None
        if args.write:
            validation = validate_latest_component_snapshot(
                session,
                snapshot_date=snapshot_date,
                factor_output_path=ROOT / "outputs" / "mae_latest_validation.csv",
                summary_output_path=ROOT / "outputs" / "mae_latest_validation_summary.json",
            )
            session.commit()
        else:
            session.rollback()
    if result.backup_path:
        print(f"Backup created: {result.backup_path}")
    for snapshot in result.snapshots:
        row = snapshot.coverage_report
        counts = row["component_counts"]
        print(
            f"{row['snapshot_date']}: scored_before_validation={row['scored_cells']} "
            f"data={counts['data']} market={counts['market']} research={counts['research']} "
            f"no_data={row['no_data_cells']}"
        )
    if validation:
        print(
            f"Latest validation: {validation['path']} "
            f"summary={validation['summary_path']} "
            f"passed={validation['passed']} insufficient={validation['insufficient']} total={validation['total']}"
        )
    print("Saved snapshots:", len(result.saved_snapshot_ids))
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rebuild only the latest MAE component snapshot.")
    parser.add_argument("--snapshot-date", default="2026-07-12")
    parser.add_argument("--backup", action="store_true", help="Create a SQLite backup before --write.")
    parser.add_argument("--write", action="store_true", help="Persist the latest component snapshot.")
    parser.add_argument("--replace-existing", action="store_true", help="Replace an existing immutable component snapshot for the date.")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
