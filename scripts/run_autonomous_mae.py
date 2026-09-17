from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.services.autonomous_operations import run_autonomous_update
from app.services.financial_release_gate import ReadOnlyGovernanceError


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the autonomous MAE market-research operation.")
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    collection = parser.add_mutually_exclusive_group()
    collection.add_argument("--collect", dest="collect", action="store_true")
    collection.add_argument("--no-collect", dest="collect", action="store_false")
    publication = parser.add_mutually_exclusive_group()
    publication.add_argument("--publish", dest="publish", action="store_true")
    publication.add_argument("--no-publish", dest="publish", action="store_false")
    workbook = parser.add_mutually_exclusive_group()
    workbook.add_argument("--build-xlsx", dest="build_xlsx", action="store_true")
    workbook.add_argument("--no-build-xlsx", dest="build_xlsx", action="store_false")
    parser.set_defaults(collect=True, publish=True, build_xlsx=True)
    return parser.parse_args()


def artifact_tool_export_builder(build_input: dict) -> None:
    node = os.getenv("MAE_ARTIFACT_NODE") or shutil.which("node")
    modules = os.getenv("MAE_ARTIFACT_NODE_MODULES")
    if not node or not modules or not Path(modules).is_dir():
        raise RuntimeError(
            "Set MAE_ARTIFACT_NODE and MAE_ARTIFACT_NODE_MODULES to the approved artifact-tool runtime."
        )
    with tempfile.TemporaryDirectory(prefix="mae-artifact-runtime-") as work:
        work_path = Path(work)
        (work_path / "node_modules").symlink_to(Path(modules).resolve(), target_is_directory=True)
        previews = Path(build_input["export_dir"]) / "previews"
        subprocess.run(
            [
                node,
                str(ROOT / "scripts" / "build_autonomous_workbook.mjs"),
                str(Path(build_input["export_dir"]) / "workbook_build_input.json"),
                str(previews),
            ],
            cwd=work_path,
            check=True,
            timeout=180,
        )


def main() -> int:
    args = parse_args()
    try:
        result = run_autonomous_update(
            as_of=args.as_of,
            settings=get_settings(),
            collect=args.collect,
            publish=args.publish,
            build_xlsx=args.build_xlsx,
            export_builder=artifact_tool_export_builder if args.build_xlsx else None,
        )
    except ReadOnlyGovernanceError as exc:
        print(json.dumps({"success": False, "status": "READ_ONLY_REJECTED", "error": str(exc)}, indent=2))
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI must return a structured operational failure
        print(
            json.dumps(
                {
                    "success": False,
                    "status": "OPERATION_FAILED",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str))
    return 0 if result.success else 2


if __name__ == "__main__":
    raise SystemExit(main())
