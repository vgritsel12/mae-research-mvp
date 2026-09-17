from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.domain.models import MaeSnapshot
from app.repositories.database import ENGINE, SessionLocal, init_database
from app.services.mae_history import backup_database, default_snapshot_dates, reconstruct_mae_history


def main() -> int:
    args = _parse_args()
    from_date = date.fromisoformat(args.from_date)
    to_date = date.fromisoformat(args.to_date)
    snapshot_dates = default_snapshot_dates(from_date, to_date)
    report_path = (
        ROOT / "outputs" / "production_cleanup" / "mae_history_coverage_report.json"
        if args.coverage_report
        else None
    )
    backup_path = None
    if args.write:
        init_database(ENGINE)
        if args.backup:
            backup_path = backup_database()
            print(f"Backup created: {backup_path}")
    print("MAE history reconstruction")
    print(f"Mode: {'WRITE' if args.write else 'DRY RUN'}")
    print("Snapshot dates:", ", ".join(d.isoformat() for d in snapshot_dates))
    with SessionLocal() as session:
        existing = session.query(MaeSnapshot).count() if args.write else 0
        if existing and not args.replace_existing:
            print(f"Existing immutable snapshots: {existing}. Use --replace-existing to rebuild them.")
            return 2
        result = reconstruct_mae_history(
            session,
            snapshot_dates=snapshot_dates,
            from_date=from_date,
            to_date=to_date,
            dry_run=not args.write,
            replace_existing=args.replace_existing,
            coverage_report_path=report_path,
            backup_path=None,
        )
        if args.write:
            session.commit()
        else:
            session.rollback()
    for row in result.coverage_by_date:
        print(
            f"{row['snapshot_date']}: covered={row['covered_cells']} "
            f"NO_DATA={row['no_data_cells']} one_source={row['one_source_cells']} "
            f"two_plus={row['two_plus_independent_source_cells']} providers={len(row['providers'])}"
        )
    if report_path:
        print(f"Coverage report: {report_path}")
    print("Saved snapshots:", len(result.saved_snapshot_ids))
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rebuild point-in-time MAE history snapshots.")
    parser.add_argument("--from-date", default="2026-01-01")
    parser.add_argument("--to-date", default="2026-07-12")
    parser.add_argument("--frequency", default="month-end", choices=["month-end"])
    parser.add_argument("--backup", action="store_true", help="Create a local SQLite backup before --write.")
    parser.add_argument("--coverage-report", action="store_true", help="Write JSON coverage report.")
    parser.add_argument("--write", action="store_true", help="Persist snapshots. Without this flag the script is read-only.")
    parser.add_argument("--replace-existing", action="store_true", help="Replace existing immutable snapshots.")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
