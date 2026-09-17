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
    FULL_PRODUCT_VERSION,
    TEST_ONLY_STATUS,
    build_full_snapshot_product,
    full_workbook_manifest,
    snapshot_status_for_date,
    write_full_snapshot_datasets,
)
from app.services.mae_snapshot_validation import validate_full_snapshot  # noqa: E402


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
    product = build_full_snapshot_product(
        snapshot_date,
        args.snapshot_type,
        root=ROOT,
        snapshot_status=snapshot_status,
    )
    metadata = full_workbook_manifest(product)
    target_dir = ROOT / "outputs" / "release_candidates" / "legacy-builds" / snapshot_date.isoformat() / "full"
    target_version = ""
    target_validation = target_dir / f"mae_full_validation_{snapshot_date.isoformat()}.json"
    if target_validation.exists():
        try:
            target_version = json.loads(target_validation.read_text(encoding="utf-8")).get("metadata", {}).get("product_version", "")
        except (OSError, json.JSONDecodeError):
            target_version = ""

    if target_dir.exists() and target_version == FULL_PRODUCT_VERSION:
        report = validate_full_snapshot(
            snapshot_date,
            root=ROOT,
            snapshot_dir=target_dir,
            build_metadata=metadata,
            require_database=False,
            allow_future_test_date=args.allow_future_test_date,
        )
        if report["status"] != "PASS":
            _print_errors(report)
            return 1
        _print_success(product, target_dir, existing=True, published=False)
        return 0

    target_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".full-staging-", dir=target_dir.parent))
    try:
        paths = write_full_snapshot_datasets(product, staging)
        workbook_path = staging / f"mae_full_snapshot_{snapshot_date.isoformat()}.xlsx"
        preview_dir = ROOT / "outputs" / "visual_qa" / f"mae_full_{snapshot_date.isoformat()}"
        _build_workbook(paths, metadata, workbook_path, preview_dir)
        report = validate_full_snapshot(
            snapshot_date,
            root=ROOT,
            snapshot_dir=staging,
            build_metadata=metadata,
            require_database=False,
            allow_future_test_date=args.allow_future_test_date,
        )
        if report["status"] != "PASS":
            _print_errors(report)
            return 1
        if target_dir.exists():
            shutil.rmtree(target_dir)
        staging.rename(target_dir)
        staging = target_dir
        report = validate_full_snapshot(
            snapshot_date,
            root=ROOT,
            snapshot_dir=target_dir,
            build_metadata=metadata,
            require_database=False,
            allow_future_test_date=args.allow_future_test_date,
        )
        if report["status"] != "PASS":
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
        raise RuntimeError("Bundled spreadsheet runtime not found; set MAE_NODE_BIN and MAE_NODE_MODULES.")
    builder = ROOT / "scripts" / "build_mae_full_workbook.mjs"
    with tempfile.TemporaryDirectory(prefix="mae-full-artifact-runtime-") as runtime_text:
        runtime = Path(runtime_text)
        (runtime / "node_modules").symlink_to(node_modules, target_is_directory=True)
        runtime_builder = runtime / builder.name
        shutil.copy2(builder, runtime_builder)
        manifest_path = runtime / "manifest.json"
        manifest_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        command = [
            str(node), str(runtime_builder),
            "--scores", str(paths["scores"]),
            "--evidence", str(paths["evidence"]),
            "--scenarios", str(paths["scenarios"]),
            "--transmission", str(paths["transmission"]),
            "--manifest", str(manifest_path),
            "--output", str(output),
            "--preview-dir", str(preview_dir),
        ]
        result = subprocess.run(command, cwd=runtime, text=True, capture_output=True, check=False)
        if result.stdout:
            print(result.stdout.rstrip())
        if result.returncode != 0:
            raise RuntimeError(f"Full Excel builder failed ({result.returncode}): {result.stderr.strip()}")
        output.with_suffix(output.suffix + ".inspect.ndjson").unlink(missing_ok=True)


def _print_errors(report: dict) -> None:
    print("Full snapshot validation FAILED", file=sys.stderr)
    for error in report.get("errors", []):
        print(f"- {error}", file=sys.stderr)
    print("Full latest outputs were not published.", file=sys.stderr)


def _print_success(product, target_dir: Path, *, existing: bool, published: bool) -> None:
    action = "Validated existing" if existing else "Built"
    print(f"{action} full MAE snapshot {product.snapshot_date.isoformat()} ({product.snapshot_type})")
    print(f"Intersections: {len(product.scores)}; applicable: {len(product.applicable_scores)}; coverage: {product.coverage}/104")
    print(f"Evidence rows: {len(product.evidence)}; scenarios: {len(product.scenarios)}; transmission rows: {len(product.transmission)}")
    print(f"Snapshot directory: {target_dir}")
    if published:
        print(f"Full latest workbook: {ROOT / 'outputs' / 'mae_full_latest.xlsx'}")
    else:
        print("Candidate validated; atomic release gate required. SQLite and current outputs were not changed.")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build the full 19x6 Excel-first MAE snapshot without changing pilot latest outputs.")
    parser.add_argument("--as-of", required=True, help="Snapshot date in YYYY-MM-DD format.")
    parser.add_argument("--snapshot-type", required=True, help="FINAL_COMPONENT (or existing compatible alias).")
    parser.add_argument("--allow-future-test-date", action="store_true", help="Allow a future TEST_ONLY artifact; never writes production SQLite or latest outputs.")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
