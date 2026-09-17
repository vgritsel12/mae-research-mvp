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

from app.services.audit_baseline import (  # noqa: E402
    BaselineError,
    freeze_audit_baseline,
    verify_audit_baseline,
)


LOGGER = logging.getLogger("mae.audit_baseline")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Freeze or verify an immutable MAE production audit baseline.")
    parser.add_argument("--snapshot-date", required=True, type=date.fromisoformat)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument(
        "--allow-schema-only-db-change",
        action="store_true",
        help="Ignore SQLite binary changes only when all frozen business columns and the current pointer still match.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    output_root = args.output_root or ROOT / "outputs" / "audit_baseline" / args.snapshot_date.isoformat()
    manifest_path = output_root / f"AUDIT_BASELINE_{args.snapshot_date.isoformat()}.json"
    try:
        if not manifest_path.exists():
            manifest = freeze_audit_baseline(
                root=ROOT,
                snapshot_date=args.snapshot_date,
                output_root=output_root,
            )
            LOGGER.info("Frozen baseline aggregate_sha256=%s", manifest["aggregate_sha256"])
        if args.verify:
            result = verify_audit_baseline(
                root=ROOT,
                manifest_path=manifest_path,
                strict_database_binary=not args.allow_schema_only_db_change,
            )
            LOGGER.info("Verification summary %s", json.dumps(result, sort_keys=True))
            if not result["ok"]:
                LOGGER.error("BASELINE_MISMATCH: %s", "; ".join(result["mismatches"]))
                return 2
        return 0
    except (BaselineError, OSError, ValueError, json.JSONDecodeError) as exc:
        LOGGER.error("BASELINE_HARD_STOP: %s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

