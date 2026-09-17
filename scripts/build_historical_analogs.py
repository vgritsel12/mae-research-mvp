from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.historical_analogs import build_historical_analogs_candidate


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a quarantined deterministic Historical Analogs v2 candidate.")
    parser.add_argument("--snapshot-date", default="", help="Canonical snapshot date (YYYY-MM-DD).")
    parser.add_argument("--candidate-id", required=True, help="Isolated candidate namespace.")
    parser.add_argument("--no-publish", action="store_true", help="Required safety acknowledgement; never updates latest/current.")
    parser.add_argument("--refresh-series", action="store_true", help="Refresh source observations before candidate build.")
    args = parser.parse_args()
    if not args.no_publish:
        parser.error("--no-publish is required; analog v2 can only be built as a quarantined candidate.")

    snapshot = date.fromisoformat(args.snapshot_date) if args.snapshot_date else _snapshot_date()
    result, paths = build_historical_analogs_candidate(
        snapshot,
        candidate_id=args.candidate_id,
        root=ROOT,
        refresh_series=args.refresh_series,
    )
    print(
        json.dumps(
            {
                "status": result["status"],
                "mode": result["mode"],
                "candidate_id": result["candidate_id"],
                "candidate_status": result["candidate_status"],
                "analyst_review_status": result["analyst_review_status"],
                "snapshot_date": result["snapshot_date"],
                "historical_period": result["historical_period"],
                "main_analogs": len(result["analogs"]),
                "recent_incomplete": len(result["recent_incomplete"]),
                "critical_excluded": len(result["critical_excluded"]),
                "no_look_ahead": result["no_look_ahead"]["status"],
                "candidate_json": str(paths.result_json),
                "manifest": str(paths.manifest_json),
                "legacy_latest_sha256": result["legacy_latest_before_sha256"],
            },
            indent=2,
        )
    )
    return 0


def _snapshot_date() -> date:
    path = ROOT / "outputs" / "mae_full_latest_scores.csv"
    if not path.exists():
        raise FileNotFoundError("Canonical score output is unavailable.")
    import pandas as pd

    scores = pd.read_csv(path, nrows=1, dtype=str)
    return date.fromisoformat(str(scores.iloc[0]["snapshot_date"]))


if __name__ == "__main__":
    raise SystemExit(main())
