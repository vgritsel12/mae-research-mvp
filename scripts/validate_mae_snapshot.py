from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.mae_snapshot_validation import validate_snapshot  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a dated MAE snapshot and exit non-zero on any defect.")
    parser.add_argument("--as-of", required=True, help="Snapshot date in YYYY-MM-DD format.")
    parser.add_argument(
        "--allow-future-test-date",
        action="store_true",
        help="Validate a future TEST_ONLY artifact without making it production-eligible.",
    )
    args = parser.parse_args()
    snapshot_date = date.fromisoformat(args.as_of)
    report = validate_snapshot(
        snapshot_date,
        root=ROOT,
        require_database=not args.allow_future_test_date,
        allow_future_test_date=args.allow_future_test_date,
    )
    print(f"MAE validation: {report['status']}")
    for row in report["checks"]:
        print(f"[{row['status']}] {row['name']}: {row['detail']}")
    print(f"Latest publication date: {report.get('latest_publication_date') or 'N/A'}")
    print(f"Latest market observation date: {report.get('latest_market_observation_date') or 'N/A'}")
    print(f"Previous comparable snapshot: {report.get('previous_comparable_snapshot') or 'N/A'}")
    print(f"New publications: {report.get('new_publications_since_previous', 0)}")
    print(f"New market observations: {report.get('new_market_observations_since_previous', 0)}")
    print(f"Carry-forward cells: {report.get('carry_forward_cells', 0)}")
    print(f"Changed cells: {report.get('changed_cells', 0)}")
    for warning in report.get("warnings", []):
        print(f"WARNING: {warning}")
    if report["status"] not in {"PASS", "TEST_ONLY"}:
        print("Errors:", file=sys.stderr)
        for error in report["errors"]:
            print(f"- {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
