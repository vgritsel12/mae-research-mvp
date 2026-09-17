from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.mae_snapshot_validation import validate_full_snapshot  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a full Excel-first MAE snapshot.")
    parser.add_argument("--as-of", required=True, help="Snapshot date in YYYY-MM-DD format.")
    args = parser.parse_args()
    report = validate_full_snapshot(date.fromisoformat(args.as_of), root=ROOT)
    print(f"Full MAE validation: {report['status']}")
    print(f"Total intersections: {report['total_intersections']}")
    print(f"Applicable / N/A: {report['applicable_cells']} / {report['not_applicable_cells']}")
    print(f"Evidence coverage: {report['evidence_coverage']}")
    if report["status"] != "PASS":
        for error in report.get("errors", []):
            print(f"- {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
