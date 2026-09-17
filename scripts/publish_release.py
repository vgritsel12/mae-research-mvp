from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import ROOT_DIR, get_settings  # noqa: E402
from app.repositories.database import create_app_engine, init_database  # noqa: E402
from app.services.atomic_release import AtomicReleaseRequest, publish_atomic_release  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Publish a validated candidate through the human-gated atomic release coordinator."
    )
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--channel", required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--snapshot-date", required=True)
    parser.add_argument("--governance-record-id", required=True)
    parser.add_argument("--baseline-manifest-hash", required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--artifact", action="append", required=True, dest="artifacts")
    parser.add_argument("--root", type=Path, default=ROOT_DIR)
    args = parser.parse_args()

    settings = get_settings()
    engine = create_app_engine(settings)
    init_database(engine)
    result = publish_atomic_release(
        engine=engine,
        settings=settings,
        root=args.root,
        request=AtomicReleaseRequest(
            release_id=args.release_id,
            channel=args.channel,
            candidate_id=args.candidate_id,
            snapshot_date=date.fromisoformat(args.snapshot_date),
            governance_record_id=args.governance_record_id,
            source_dir=args.source_dir,
            artifact_names=tuple(args.artifacts),
            expected_baseline_manifest_hash=args.baseline_manifest_hash,
        ),
    )
    print(
        json.dumps(
            {
                "release_id": result.release_id,
                "manifest_hash": result.manifest_hash,
                "release_directory": str(result.release_directory),
                "idempotent": result.idempotent,
                "trace": result.trace,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
