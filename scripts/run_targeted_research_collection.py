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
from app.services.targeted_research import TARGETED_SNAPSHOT_DATE, seed_targeted_research, write_component_change_tracker, write_targeted_outputs


def main() -> int:
    args = _parse_args()
    snapshot_date = date.fromisoformat(args.snapshot_date)
    previous_snapshot_date = date.fromisoformat(args.previous_snapshot_date)
    init_database(ENGINE)
    print("MAE targeted research collection")
    print(f"Snapshot date: {snapshot_date.isoformat()}")
    print(f"Previous comparable snapshot: {previous_snapshot_date.isoformat()}")
    print("Scope: 10 core cells only")
    write_component_maps(ROOT / "data")
    with SessionLocal() as session:
        seeded = seed_targeted_research(session, snapshot_date)
        print(f"Targeted research views inserted/updated: {seeded}")
        result = rebuild_component_history(
            session,
            snapshot_dates=[previous_snapshot_date, snapshot_date],
            dry_run=False,
            replace_existing=True,
            backup=args.backup,
            report_path=ROOT / "outputs" / "production_cleanup" / "targeted_research_component_report.json",
        )
        previous_validation = validate_latest_component_snapshot(
            session,
            snapshot_date=previous_snapshot_date,
            factor_output_path=ROOT / "outputs" / "mae_previous_validation.csv",
            summary_output_path=ROOT / "outputs" / "mae_previous_validation_summary.json",
        )
        validation = validate_latest_component_snapshot(
            session,
            snapshot_date=snapshot_date,
            factor_output_path=ROOT / "outputs" / "mae_latest_validation.csv",
            summary_output_path=ROOT / "outputs" / "mae_latest_validation_summary.json",
        )
        change_tracker = write_component_change_tracker(
            session,
            current_snapshot_date=snapshot_date,
            previous_snapshot_date=previous_snapshot_date,
            output_path=ROOT / "outputs" / "mae_change_tracker_latest.csv",
            summary_output_path=ROOT / "outputs" / "mae_latest_validation_summary.json",
        )
        targeted = write_targeted_outputs(
            session,
            snapshot_date=snapshot_date,
            research_items_path=ROOT / "outputs" / "research_items_latest.csv",
            rejected_items_path=ROOT / "outputs" / "rejected_research_items.csv",
            search_log_path=ROOT / "outputs" / "research_search_log.csv",
            summary_output_path=ROOT / "outputs" / "mae_latest_validation_summary.json",
        )
        session.commit()
    if result.backup_path:
        print(f"Backup created: {result.backup_path}")
    print(
        f"Previous validation: passed={previous_validation['passed']} insufficient={previous_validation['insufficient']} total={previous_validation['total']}"
    )
    print(
        f"Latest validation: passed={validation['passed']} insufficient={validation['insufficient']} total={validation['total']}"
    )
    print(f"Change tracker: rows={change_tracker['rows']} path={change_tracker['path']}")
    print(
        "Targeted outputs: "
        f"accepted_rows={targeted.accepted_rows} rejected_rows={targeted.rejected_rows} "
        f"search_log_rows={targeted.search_log_rows} duplicates_removed={targeted.exact_duplicates_removed}"
    )
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect targeted research for the 10 MAE core cells.")
    parser.add_argument("--snapshot-date", default=TARGETED_SNAPSHOT_DATE.isoformat())
    parser.add_argument("--previous-snapshot-date", default="2026-06-30")
    parser.add_argument("--backup", action="store_true", help="Create a SQLite backup before replacing the snapshot.")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
