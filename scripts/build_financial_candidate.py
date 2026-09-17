from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.financial_candidate import FinancialCandidateError, build_financial_candidate  # noqa: E402


LOGGER = logging.getLogger("mae.financial_candidate")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a sparse, quarantined MAE financial candidate.")
    parser.add_argument("--snapshot-date", required=True, type=date.fromisoformat)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--no-publish", action="store_true", help="Required for all real candidate builds.")
    parser.add_argument("--skip-workbook", action="store_true", help="Test-only: do not author the XLSX artifact.")
    parser.add_argument("--node-executable", help="Bundled Node executable used by the spreadsheet runtime.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    try:
        result = build_financial_candidate(
            root=ROOT,
            snapshot_date=args.snapshot_date,
            candidate_id=args.candidate_id,
            no_publish=args.no_publish,
            build_workbook=not args.skip_workbook,
            node_executable=args.node_executable,
        )
    except (FinancialCandidateError, OSError, ValueError) as exc:
        LOGGER.error("FINANCIAL_CANDIDATE_HARD_STOP: %s", exc)
        return 2
    LOGGER.info(
        "Financial candidate summary %s",
        json.dumps(
            {
                "candidate_id": result.candidate_id,
                "candidate_root": str(result.candidate_root),
                "run_id": result.run_id,
                "business_data_hash": result.business_data_hash,
                "reconciliation": result.reconciliation,
                "workbook_qa": result.workbook_qa,
                "artifact_count": len(result.artifact_hashes),
                "published": result.published,
            },
            sort_keys=True,
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
