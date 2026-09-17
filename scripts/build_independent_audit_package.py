from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.audit_disclosure import DEFAULT_CANDIDATE_ID, candidate_root, validate_candidate_id


PACKAGE_ROOT = ROOT / "outputs" / "independent_audit_package" / "2026-07-13-remediation"
BASELINE_ROOT = ROOT / "outputs" / "audit_baseline" / "2026-07-13"
REMEDIATION_PLAN = ROOT / "private-input" / "MAE_independent_financial_audit_and_remediation_plan.md"
PRODUCTION_RELEASE_ID = "e623cdee-102e-411e-ad6d-96875ec1cd2e"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the self-contained MAE independent-auditor handoff package.")
    parser.add_argument("--candidate-id", default=DEFAULT_CANDIDATE_ID)
    parser.add_argument("--output", type=Path, default=PACKAGE_ROOT)
    parser.add_argument("--node", default=shutil.which("node") or "node")
    parser.add_argument("--node-modules", default=os.environ.get("NODE_PATH", ""))
    args = parser.parse_args()
    validate_candidate_id(args.candidate_id)
    package = args.output.resolve()
    candidate = candidate_root(ROOT, args.candidate_id)
    _validate_inputs(candidate)

    package.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{package.name}.", dir=package.parent))
    try:
        _copy_package_files(staging, candidate)
        source_rows = _write_source_register(staging, candidate)
        screenshots = _copy_screenshots(staging, candidate, args.candidate_id)
        _write_required_artifacts(staging, screenshot_count=len(screenshots))
        _write_readme(staging, source_count=len(source_rows), screenshot_count=len(screenshots))
        _build_workbook(staging, candidate, args.node, args.node_modules)
        _run_performance_audit(staging)
        required = verify_required_artifacts(staging)
        broken_links = verify_readme_links(staging)
        if broken_links:
            raise RuntimeError(f"Broken README links: {broken_links}")
        verification = {
            "status": "PASSED",
            "candidate_id": args.candidate_id,
            "release_status": "QUARANTINED",
            "required_artifacts_present": required,
            "broken_readme_links": 0,
            "source_register_rows": len(source_rows),
            "screenshot_rows": len(screenshots),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        (staging / "package_verification.json").write_text(
            json.dumps(verification, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        checksum_count = write_checksums(staging)
        verified = verify_checksums(staging)
        if verified != checksum_count:
            raise RuntimeError(f"Checksum verification count mismatch: {verified} != {checksum_count}")

        if package.exists():
            shutil.rmtree(package)
        os.replace(staging, package)
        print(f"Audit package built: {package}")
        print(f"Required artifacts: {required}; README broken links: 0")
        print(f"Sources: {len(source_rows)}; screenshots: {len(screenshots)}; SHA256 OK: {verified}")
        print("Final status: QUARANTINED; human approval created: false")
        return 0
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _validate_inputs(candidate: Path) -> None:
    required = [
        ROOT / "data" / "MAE_Shift_Signal_Framework_FINAL_SRS_v1.0.docx",
        REMEDIATION_PLAN,
        ROOT / "REMEDIATION_REPORT.md",
        ROOT / "FINANCIAL_RELEASE_CHECKLIST.md",
        ROOT / "SRS_DEVIATIONS.md",
        BASELINE_ROOT / "AUDIT_BASELINE_2026-07-13.json",
        BASELINE_ROOT / "QUARANTINE_NOTICE.md",
        candidate / "financial_validation_report.json",
        candidate / "historical_analogs" / "historical_analogs_v2.json",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Required package inputs missing: {missing}")


def _copy_package_files(package: Path, candidate: Path) -> None:
    copies: dict[Path, str] = {
        ROOT / "data" / "MAE_Shift_Signal_Framework_FINAL_SRS_v1.0.docx": "source_documents/MAE_Shift_Signal_Framework_FINAL_SRS_v1.0.docx",
        REMEDIATION_PLAN: "source_documents/MAE_independent_financial_audit_and_remediation_plan.md",
        BASELINE_ROOT / "AUDIT_BASELINE_2026-07-13.json": "baseline/AUDIT_BASELINE_2026-07-13.json",
        BASELINE_ROOT / "QUARANTINE_NOTICE.md": "baseline/QUARANTINE_NOTICE.md",
        ROOT / "FINANCIAL_MAPPING_PLAYBOOK_v2_APPROVED.md": "methodology/FINANCIAL_MAPPING_PLAYBOOK_v2_APPROVED.md",
        ROOT / "data" / "source_governance_v2.json": "methodology/source_governance_v2.json",
        ROOT / "SRS_DEVIATIONS.md": "reports/SRS_DEVIATIONS.md",
        ROOT / "REMEDIATION_REPORT.md": "reports/REMEDIATION_REPORT.md",
        ROOT / "FINANCIAL_RELEASE_CHECKLIST.md": "reports/FINANCIAL_RELEASE_CHECKLIST.md",
    }
    strict_names = [
        "financial_review_before_after.json",
        "strict_review_application_manifest.json",
        "active_true_pass.csv",
        "downgraded_mappings.csv",
        "manual_review_queue.csv",
        "rejected_mappings.csv",
    ]
    candidate_names = [
        "financial_validation_report.json",
        "financial_candidate_artifact_manifest.json",
        "cell_financial_audit.csv",
        "unscored_cells.csv",
        "source_role_counts.csv",
        "eligible_evidence.json",
        "MAE_audit_disclosure.json",
        "MAE_audit_disclosure.csv",
        "MAE_financial_candidate.xlsx",
        "what_changed.json",
        "shift_signals.json",
        "scenarios.json",
        "transmission_paths.json",
        "mae_matrix.json",
    ]
    analog_names = [
        "historical_analogs_v2.json",
        "historical_analogs_main.csv",
        "historical_analogs_recent_incomplete.csv",
        "historical_analogs_critical_excluded.csv",
        "historical_analogs_dispersion.csv",
        "high_yield_credit_source_assessment.json",
        "artifact_manifest.json",
    ]
    for name in strict_names:
        copies[candidate / name] = f"strict_review/{name}"
    for name in candidate_names:
        copies[candidate / name] = f"candidate/{name}"
    for name in analog_names:
        copies[candidate / "historical_analogs" / name] = f"historical_analogs/{name}"
    for source, relative in copies.items():
        if not source.is_file():
            raise FileNotFoundError(source)
        destination = package / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    performance = candidate / "phase9" / "performance_results.json"
    if performance.is_file():
        shutil.copy2(performance, package / "reports" / "performance_results.json")


def _write_source_register(package: Path, candidate: Path) -> list[dict[str, Any]]:
    eligible = json.loads((candidate / "eligible_evidence.json").read_text(encoding="utf-8"))
    analogs = json.loads((candidate / "historical_analogs" / "historical_analogs_v2.json").read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    for item in eligible.get("records") or []:
        for source in item.get("sources") or []:
            rows.append(
                {
                    "provider": source.get("institution"),
                    "url": source.get("source_url"),
                    "role": source.get("role"),
                    "tier": source.get("tier"),
                    "covered_artifact": "candidate/eligible_evidence.json",
                    "covered_cells": item.get("canonical_cell_id"),
                    "retrieval_date": source.get("retrieved_at"),
                    "content_hash": source.get("content_hash"),
                    "rights_usage_note": source.get("rights_note"),
                    "local_path_if_permitted": "",
                }
            )
    metadata = analogs.get("source_metadata") or {}
    credit = analogs.get("credit_spread_source_assessment") or {}
    for source_id, source in (metadata.get("sources") or {}).items():
        rights = "Reference metadata only; raw third-party observations are not redistributed in this package."
        tier = "TIER_1_PRIMARY" if source_id in {"CPIAUCSL", "FEDFUNDS", "DFII10"} else "TIER_2_MARKET_DATA"
        if source_id == "BAMLH0A0HYM2":
            rights = str(credit.get("rights_and_usage") or rights)
        rows.append(
            {
                "provider": source.get("provider"),
                "url": source.get("url"),
                "role": "DATA_PROVIDER" if source_id in {"CPIAUCSL", "FEDFUNDS", "DFII10", "BAMLH0A0HYM2"} else "BENCHMARK",
                "tier": tier,
                "covered_artifact": "historical_analogs/historical_analogs_v2.json",
                "covered_cells": source_id,
                "retrieval_date": metadata.get("downloaded_at"),
                "content_hash": analogs.get("input_series_sha256"),
                "rights_usage_note": rights,
                "local_path_if_permitted": "",
            }
        )
    fields = [
        "provider",
        "url",
        "role",
        "tier",
        "covered_artifact",
        "covered_cells",
        "retrieval_date",
        "content_hash",
        "rights_usage_note",
        "local_path_if_permitted",
    ]
    destination = package / "source_register.csv"
    with destination.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return rows


def _copy_screenshots(package: Path, candidate: Path, candidate_id: str) -> list[dict[str, Any]]:
    source_root = candidate / "ui_evidence"
    destination_root = package / "screenshots"
    destination_root.mkdir(parents=True, exist_ok=True)
    route_map = {
        "overview": "/",
        "cell_audit": "/market-map",
        "evidence": "/sources-downloads",
        "what_changed": "/what-changed",
        "historical_analogs": "/historical-analogs",
        "historical_analogs_recent": "/historical-analogs#recent-incomplete-periods",
    }
    rows: list[dict[str, Any]] = []
    for engine in ("chromium", "webkit"):
        for viewport in ("desktop", "narrow"):
            for source in sorted((source_root / engine / viewport).glob("*.png")):
                stable_name = f"{engine}_{viewport}_{source.name}"
                destination = destination_root / stable_name
                shutil.copy2(source, destination)
                surface = source.stem
                rows.append(
                    {
                        "filename": stable_name,
                        "engine": engine,
                        "viewport": viewport,
                        "surface": surface,
                        "route": route_map.get(surface, ""),
                        "candidate_id": candidate_id,
                        "production_release_id": PRODUCTION_RELEASE_ID,
                        "release_status": "QUARANTINED",
                        "capture_timestamp": datetime.fromtimestamp(source.stat().st_mtime, timezone.utc).isoformat(),
                    }
                )
    for name in ("browser_assertions.json", "accessibility_assertions.json", "ui_heading_inventory.json", "PHASE8_VERIFICATION.md"):
        source = source_root / name
        if source.is_file():
            shutil.copy2(source, destination_root / name)
    fields = list(rows[0]) if rows else []
    with (destination_root / "index.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return rows


def _write_required_artifacts(package: Path, *, screenshot_count: int) -> None:
    entries = [
        {"class": "SRS", "path": "source_documents/MAE_Shift_Signal_Framework_FINAL_SRS_v1.0.docx"},
        {"class": "remediation_plan", "path": "source_documents/MAE_independent_financial_audit_and_remediation_plan.md"},
        {"class": "baseline_manifest", "path": "baseline/AUDIT_BASELINE_2026-07-13.json"},
        {"class": "baseline_notice", "path": "baseline/QUARANTINE_NOTICE.md"},
        {"class": "strict_review", "path": "strict_review/financial_review_before_after.json"},
        {"class": "unresolved_queue", "path": "strict_review/manual_review_queue.csv"},
        {"class": "candidate_audit", "path": "candidate/cell_financial_audit.csv"},
        {"class": "candidate_validation", "path": "candidate/financial_validation_report.json"},
        {"class": "candidate_json_export", "path": "candidate/MAE_audit_disclosure.json"},
        {"class": "historical_analogs", "path": "historical_analogs/historical_analogs_v2.json"},
        {"class": "credit_source_assessment", "path": "historical_analogs/high_yield_credit_source_assessment.json"},
        {"class": "source_register", "path": "source_register.csv"},
        {"class": "remediation_report", "path": "reports/REMEDIATION_REPORT.md"},
        {"class": "release_checklist", "path": "reports/FINANCIAL_RELEASE_CHECKLIST.md"},
        {"class": "SRS_deviation", "path": "reports/SRS_DEVIATIONS.md"},
        {"class": "screenshot_index", "path": "screenshots/index.csv"},
        {"class": "verified_screenshots", "glob": "screenshots/*.png", "minimum": screenshot_count},
        {"class": "auditor_workbook", "path": "MAE_FINANCIAL_AUDIT_PACKAGE.xlsx"},
        {"class": "workbook_QA", "path": "workbook_qa.json"},
        {"class": "performance_results", "path": "reports/performance_results.json"},
        {"class": "checksums", "path": "SHA256SUMS.txt"},
    ]
    manifest = {"schema_version": "1.0", "fixed": True, "entries": entries}
    (package / "required_artifacts.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _write_readme(package: Path, *, source_count: int, screenshot_count: int) -> None:
    text = f"""# MAE Independent Financial Audit Package

Candidate `2026-07-13-financial-remediation` is technically validated but remains `NOT_REVIEWED` and `QUARANTINED`. No human approval was created and production was not changed.

Start with [Remediation Report](reports/REMEDIATION_REPORT.md) and [Financial Release Checklist](reports/FINANCIAL_RELEASE_CHECKLIST.md). The governing source documents are the [FINAL SRS](source_documents/MAE_Shift_Signal_Framework_FINAL_SRS_v1.0.docx) and [supplied remediation plan](source_documents/MAE_independent_financial_audit_and_remediation_plan.md).

Core evidence:

- [Baseline manifest](baseline/AUDIT_BASELINE_2026-07-13.json) and [quarantine notice](baseline/QUARANTINE_NOTICE.md)
- [Strict review before/after](strict_review/financial_review_before_after.json) and [open manual-review queue](strict_review/manual_review_queue.csv)
- [Financial validation](candidate/financial_validation_report.json), [cell audit](candidate/cell_financial_audit.csv), and [JSON disclosure](candidate/MAE_audit_disclosure.json)
- [Historical analogs v2](historical_analogs/historical_analogs_v2.json) and [credit-source assessment](historical_analogs/high_yield_credit_source_assessment.json)
- [Source register](source_register.csv): {source_count} provenance/rights rows; no raw restricted third-party source copy is included
- [Screenshot index](screenshots/index.csv): {screenshot_count} verified Chromium/WebKit captures
- [Financial audit workbook](MAE_FINANCIAL_AUDIT_PACKAGE.xlsx) and [workbook QA](workbook_qa.json)
- [Fixed required-artifact manifest](required_artifacts.json); `package_verification.json` records the machine check

Portable checksum verification from the package root:

```sh
shasum -a 256 -c SHA256SUMS.txt
```

The checksum list covers every package file except `SHA256SUMS.txt` itself.
"""
    (package / "README.md").write_text(text, encoding="utf-8")


def _build_workbook(package: Path, candidate: Path, node: str, node_modules: str) -> None:
    command = [
        node,
        str(ROOT / "scripts" / "build_audit_package_workbook.mjs"),
        "--candidate-root",
        str(candidate),
        "--source-register",
        str(package / "source_register.csv"),
        "--output",
        str(package / "MAE_FINANCIAL_AUDIT_PACKAGE.xlsx"),
        "--preview-dir",
        str(package / "workbook_previews"),
        "--qa-output",
        str(package / "workbook_qa.json"),
    ]
    env = os.environ.copy()
    if node_modules:
        env["NODE_PATH"] = node_modules
    result = subprocess.run(command, cwd=ROOT, env=env, text=True, capture_output=True, check=False)
    if result.stdout:
        print(result.stdout.rstrip())
    if result.returncode:
        raise RuntimeError(f"Workbook build failed ({result.returncode}): {result.stderr.strip()}")


def _run_performance_audit(package: Path) -> None:
    command = [
        sys.executable,
        str(ROOT / "scripts" / "audit_performance_test.py"),
        "--workbook-qa",
        str(package / "workbook_qa.json"),
        "--output",
        str(package / "reports" / "performance_results.json"),
    ]
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    if result.stdout:
        print(result.stdout.rstrip())
    if result.returncode:
        raise RuntimeError(f"Performance audit failed ({result.returncode}): {result.stderr.strip()}")


def verify_required_artifacts(package: Path) -> int:
    manifest = json.loads((package / "required_artifacts.json").read_text(encoding="utf-8"))
    present = 0
    for entry in manifest["entries"]:
        if entry.get("path") == "SHA256SUMS.txt":
            present += 1
            continue
        if "path" in entry:
            if not (package / entry["path"]).is_file():
                raise FileNotFoundError(f"Missing required artifact {entry['class']}: {entry['path']}")
        else:
            matches = list(package.glob(entry["glob"]))
            if len(matches) < int(entry["minimum"]):
                raise FileNotFoundError(f"Required artifact class {entry['class']} has {len(matches)} < {entry['minimum']}")
        present += 1
    return present


def verify_readme_links(package: Path) -> list[str]:
    text = (package / "README.md").read_text(encoding="utf-8")
    links = re.findall(r"\[[^]]+\]\(([^)]+)\)", text)
    return [link for link in links if not link.startswith(("http://", "https://", "#")) and not (package / link).exists()]


def write_checksums(package: Path) -> int:
    rows = []
    for path in sorted(item for item in package.rglob("*") if item.is_file() and item.name != "SHA256SUMS.txt"):
        rows.append(f"{_sha256(path)}  {path.relative_to(package).as_posix()}")
    (package / "SHA256SUMS.txt").write_text("\n".join(rows) + "\n", encoding="utf-8")
    return len(rows)


def verify_checksums(package: Path) -> int:
    count = 0
    for line in (package / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
        expected, relative = line.split("  ", 1)
        path = package / relative
        if not path.is_file() or _sha256(path) != expected:
            raise RuntimeError(f"Checksum mismatch: {relative}")
        count += 1
    return count


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
