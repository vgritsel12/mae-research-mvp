from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import ROOT_DIR, get_settings  # noqa: E402
from app.repositories.database import create_app_engine  # noqa: E402
from app.services.atomic_release import recover_atomic_releases  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile the atomic release journal without replacing a valid DB pointer.")
    parser.add_argument("--channel", default=None)
    parser.add_argument("--root", type=Path, default=ROOT_DIR)
    args = parser.parse_args()
    report = recover_atomic_releases(engine=create_app_engine(get_settings()), root=args.root, channel=args.channel)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
