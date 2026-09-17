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

from app.services.mae_snapshot_product import (  # noqa: E402
    TEST_ONLY_STATUS,
    build_snapshot_product,
    snapshot_status_for_date,
    workbook_manifest,
    write_snapshot_datasets,
)
from app.services.mae_snapshot_validation import validate_snapshot  # noqa: E402


DEFAULT_NODE = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
DEFAULT_NODE_MODULES = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules"


def main() -> int:
    args = _parser().parse_args()
    snapshot_date = date.fromisoformat(args.as_of)
    try:
        snapshot_status = snapshot_status_for_date(
            snapshot_date,
            allow_future_test_date=args.allow_future_test_date,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    test_only = snapshot_status == TEST_ONLY_STATUS
    target_dir = ROOT / "outputs" / "release_candidates" / "legacy-builds" / snapshot_date.isoformat() / "pilot"
    product = build_snapshot_product(
        snapshot_date,
        args.snapshot_type,
        root=ROOT,
        source_mode=args.source_mode,
        snapshot_status=snapshot_status,
    )
    metadata = workbook_manifest(product)
    expected_validation_status = TEST_ONLY_STATUS if test_only else "PASS"

    if target_dir.exists():
        report = validate_snapshot(
            snapshot_date,
            root=ROOT,
            snapshot_dir=target_dir,
            build_metadata=metadata,
            require_database=False,
            allow_future_test_date=args.allow_future_test_date,
        )
        if report["status"] != expected_validation_status:
            _print_errors(report)
            return 1
        _print_success(product, target_dir, existing=True, published=False)
        return 0

    snapshots_root = target_dir.parent
    snapshots_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".staging-{snapshot_date.isoformat()}-", dir=snapshots_root))
    try:
        paths = write_snapshot_datasets(product, staging)
        workbook_path = staging / f"mae_snapshot_{snapshot_date.isoformat()}.xlsx"
        preview_dir = Path(tempfile.gettempdir()) / f"mae_snapshot_previews_{snapshot_date.isoformat()}"
        _build_workbook(paths, metadata, workbook_path, preview_dir)
        report = validate_snapshot(
            snapshot_date,
            root=ROOT,
            snapshot_dir=staging,
            build_metadata=metadata,
            require_database=False,
            allow_future_test_date=args.allow_future_test_date,
        )
        if report["status"] != expected_validation_status:
            _print_errors(report)
            return 1
        staging.rename(target_dir)
        staging = target_dir
        report = validate_snapshot(
            snapshot_date,
            root=ROOT,
            snapshot_dir=target_dir,
            build_metadata=metadata,
            require_database=False,
            allow_future_test_date=args.allow_future_test_date,
        )
        if report["status"] != expected_validation_status:
            _print_errors(report)
            return 1
        _print_success(product, target_dir, existing=False, published=False)
        print(f"Workbook previews: {preview_dir}")
        return 0
    finally:
        if staging.exists() and staging != target_dir:
            shutil.rmtree(staging, ignore_errors=True)


def _build_workbook(paths: dict[str, Path], metadata: dict, output: Path, preview_dir: Path) -> None:
    node = Path(os.getenv("MAE_NODE_BIN", str(DEFAULT_NODE)))
    node_modules = Path(os.getenv("MAE_NODE_MODULES", str(DEFAULT_NODE_MODULES)))
    if not node.exists() or not node_modules.exists():
        raise RuntimeError(
            "Bundled spreadsheet runtime not found. Set MAE_NODE_BIN and MAE_NODE_MODULES to the Codex workspace runtime paths."
        )
    builder = ROOT / "scripts" / "build_mae_workbook.mjs"
    with tempfile.TemporaryDirectory(prefix="mae-artifact-runtime-") as runtime_dir_text:
        runtime_dir = Path(runtime_dir_text)
        (runtime_dir / "node_modules").symlink_to(node_modules, target_is_directory=True)
        runtime_builder = runtime_dir / builder.name
        shutil.copy2(builder, runtime_builder)
        manifest_path = runtime_dir / "manifest.json"
        manifest_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        command = [
            str(node),
            str(runtime_builder),
            "--scores",
            str(paths["scores"]),
            "--evidence",
            str(paths["evidence"]),
            "--scenarios",
            str(paths["scenarios"]),
            "--manifest",
            str(manifest_path),
            "--output",
            str(output),
            "--preview-dir",
            str(preview_dir),
        ]
        result = subprocess.run(command, cwd=runtime_dir, text=True, capture_output=True, check=False)
        if result.stdout:
            print(result.stdout.rstrip())
        if result.returncode != 0:
            raise RuntimeError(f"Excel builder failed ({result.returncode}): {result.stderr.strip()}")
        # artifact-tool may spill a large inspect payload next to the workbook;
        # previews and validation are retained, this internal QA sidecar is not.
        inspect_sidecar = output.with_suffix(output.suffix + ".inspect.ndjson")
        inspect_sidecar.unlink(missing_ok=True)


def _print_errors(report: dict) -> None:
    print("Snapshot validation FAILED", file=sys.stderr)
    for error in report.get("errors", []):
        print(f"- {error}", file=sys.stderr)
    print("Latest outputs were not published.", file=sys.stderr)


def _print_success(product, target_dir: Path, *, existing: bool, published: bool) -> None:
    action = "Validated existing" if existing else "Built"
    print(f"{action} MAE snapshot {product.snapshot_date.isoformat()} ({product.snapshot_type})")
    print(f"Previous comparable snapshot: {product.previous_snapshot_date.isoformat()}")
    print(f"Coverage: {product.coverage}/10")
    print(f"Scenarios: {len(product.scenarios)}")
    print(f"Excel sheets: 5")
    print(f"Snapshot directory: {target_dir}")
    if published:
        print(f"Latest workbook: {ROOT / 'outputs' / 'mae_latest.xlsx'}")
    else:
        print("Candidate validated; atomic release gate required. SQLite and current outputs were not changed.")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build an Excel-first MAE snapshot without replacing prior history.")
    parser.add_argument("--as-of", required=True, help="Snapshot date in YYYY-MM-DD format.")
    parser.add_argument("--snapshot-type", required=True, help="INTERIM, INTERIM_SNAPSHOT, FINAL or FINAL_COMPONENT.")
    parser.add_argument(
        "--allow-future-test-date",
        action="store_true",
        help="Allow a future date as TEST_ONLY; never writes production SQLite or latest outputs.",
    )
    parser.add_argument(
        "--source-mode",
        choices=["AUTO", "MANUAL"],
        default="AUTO",
        help="AUTO probes allowed sources and inbox URLs; MANUAL uses analyst-supplied inbox excerpts without network access.",
    )
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
