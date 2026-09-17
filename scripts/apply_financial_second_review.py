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

from app.services.strict_review_application import (  # noqa: E402
    StrictReviewApplicationError,
    StrictReviewInputs,
    apply_financial_second_review,
    default_inputs,
)


LOGGER = logging.getLogger("mae.strict_review_application")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply signed financial second-review decisions to a candidate.")
    parser.add_argument("--snapshot-date", required=True, type=date.fromisoformat)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--report-json", type=Path)
    parser.add_argument("--reviewed-csv", type=Path)
    parser.add_argument("--invalid-actions-csv", type=Path)
    parser.add_argument("--baseline-manifest", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    defaults = default_inputs(ROOT, args.snapshot_date)
    inputs = StrictReviewInputs(
        report_json=args.report_json or defaults.report_json,
        reviewed_csv=args.reviewed_csv or defaults.reviewed_csv,
        invalid_actions_csv=args.invalid_actions_csv or defaults.invalid_actions_csv,
        baseline_manifest=args.baseline_manifest or defaults.baseline_manifest,
    )
    try:
        result = apply_financial_second_review(
            root=ROOT,
            snapshot_date=args.snapshot_date,
            candidate_id=args.candidate_id,
            inputs=inputs,
            output_root=args.output_root,
            dry_run=args.dry_run,
        )
    except (StrictReviewApplicationError, OSError, ValueError) as exc:
        LOGGER.error("STRICT_REVIEW_HARD_STOP: %s", exc)
        return 2
    LOGGER.info(
        "Strict review summary %s",
        json.dumps(
            {
                **result.summary,
                "business_content_hash": result.business_content_hash,
                "candidate_root": str(result.candidate_root),
                "dry_run": result.dry_run,
                "input_hashes": result.input_hashes,
            },
            sort_keys=True,
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

