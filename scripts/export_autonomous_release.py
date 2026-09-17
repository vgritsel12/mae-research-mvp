from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import ROOT_DIR, get_settings
from app.repositories.database import create_app_engine
from app.services.autonomous_exports import finalize_autonomous_exports, prepare_autonomous_exports


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare or finalize committed autonomous MAE exports.")
    parser.add_argument("--finalize", action="store_true")
    parser.add_argument("--export-dir", type=Path)
    args = parser.parse_args()
    if args.finalize:
        if args.export_dir is None:
            parser.error("--export-dir is required with --finalize")
        result = finalize_autonomous_exports(export_dir=args.export_dir.resolve())
    else:
        result = prepare_autonomous_exports(
            engine=create_app_engine(get_settings()),
            root=ROOT_DIR,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
