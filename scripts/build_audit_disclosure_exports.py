from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.audit_disclosure import DEFAULT_CANDIDATE_ID, export_audit_disclosure


def main() -> int:
    parser = argparse.ArgumentParser(description="Build deterministic audit disclosure JSON and CSV exports.")
    parser.add_argument("--candidate-id", default=DEFAULT_CANDIDATE_ID)
    args = parser.parse_args()
    paths = export_audit_disclosure(root=ROOT, candidate_id=args.candidate_id)
    for kind, path in paths.items():
        print(f"{kind}: {path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
