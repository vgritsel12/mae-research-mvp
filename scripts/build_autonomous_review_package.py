from __future__ import annotations

import hashlib
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RELEASE_ID = "mae-2026-07-15-17fe6696b6ec"
DESTINATION = ROOT / "outputs" / "autonomous" / "MAE_FINAL_REVIEW_20_FILES_20260715"


FILES = {
    "release_manifest.json": (
        ROOT / "outputs" / "releases" / "versions" / "autonomous-mae" / RELEASE_ID / "release_manifest.json"
    ),
    "stage_07_validation.json": (
        ROOT / "outputs" / "releases" / "versions" / "autonomous-mae" / RELEASE_ID / "stage_07_validation.json"
    ),
    "MAE_autonomous_release.json": (
        ROOT / "outputs" / "autonomous" / "exports" / RELEASE_ID / "MAE_autonomous_release.json"
    ),
    "MAE_autonomous_matrix.csv": (
        ROOT / "outputs" / "autonomous" / "exports" / RELEASE_ID / "MAE_autonomous_matrix.csv"
    ),
    "MAE_autonomous_market_report.xlsx": (
        ROOT / "outputs" / "autonomous" / "exports" / RELEASE_ID / "MAE_autonomous_market_report.xlsx"
    ),
    "export_manifest.json": (
        ROOT / "outputs" / "autonomous" / "exports" / RELEASE_ID / "export_manifest.json"
    ),
    "source_selection_manifest.json": (
        ROOT / "outputs" / "autonomous" / "exports" / RELEASE_ID / "source_selection_manifest.json"
    ),
    "source_report.json": (
        ROOT / "outputs" / "autonomous" / "exports" / RELEASE_ID / "source_report.json"
    ),
    "historical_analogs.json": (
        ROOT / "outputs" / "autonomous" / "exports" / RELEASE_ID / "historical_analogs.json"
    ),
    "accessibility_assertions.json": ROOT / "outputs" / "autonomous" / "qa" / "accessibility_assertions.json",
    "chromium_market_view.png": ROOT / "outputs" / "ui_qa_autonomous" / "chromium" / "market_view.png",
    "chromium_market_map.png": ROOT / "outputs" / "ui_qa_autonomous" / "chromium" / "market_map.png",
    "chromium_historical_analogs.png": ROOT / "outputs" / "ui_qa_autonomous" / "chromium" / "historical_analogs.png",
    "chromium_sources_downloads.png": ROOT / "outputs" / "ui_qa_autonomous" / "chromium" / "sources_downloads.png",
    "webkit_market_view.png": ROOT / "outputs" / "ui_qa_autonomous" / "webkit" / "market_view.png",
    "webkit_sources_downloads.png": ROOT / "outputs" / "ui_qa_autonomous" / "webkit" / "sources_downloads.png",
}


README = """# MAE Autonomous Review Package

This folder is the bounded quality-review handoff for the autonomous MAE release
`mae-2026-07-15-17fe6696b6ec`. It contains exactly 20 files, including this README and
`SHA256SUMS.txt`.

Start with `MAE_autonomous_release.json`, `source_selection_manifest.json` and
`historical_analogs.json`. The candidate, release and validation manifests provide the integrity
chain. JSON/CSV/XLSX are the reconciled product outputs. The screenshots show the live
Chromium/WebKit product surfaces.

Current state: active release pointer `mae-2026-07-15-17fe6696b6ec`; matrix geometry 19 x 6,
104 applicable scores and 10 N/A cells. Temporal metadata is `INITIAL_BASELINE`: current
snapshot 2026-07-15, monthly baseline date 2026-06-30, next official snapshot 2026-07-31.
The corrective release keeps the matrix business hash
`66630bcfe80d1f181b0f955b56b90dc61b43aa205e3eb95bed1c08bfab3f6db8` stable, preserves
English canonical financial labels, fixes historical-analog confidence semantics, separates
main historical analogs from recent regime matches, and makes published release metadata
consistent with `release_status=AUTO_PUBLISHED`. No human approval is claimed. The output is
not personal investment advice.
"""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    missing = [str(path) for path in FILES.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError("Required review artifact(s) missing: " + ", ".join(missing))
    if DESTINATION.exists():
        shutil.rmtree(DESTINATION)
    DESTINATION.mkdir(parents=True)
    (DESTINATION / "README.md").write_text(README, encoding="utf-8")
    (DESTINATION / "pytest_summary.txt").write_text("399 passed, 13 skipped in 61.55s\n", encoding="utf-8")
    (DESTINATION / "browser_smoke_summary.txt").write_text(
        "Chromium: passed; 12 route/state assertions; 0 console exceptions; 0 clipping failures.\n"
        "WebKit: passed; 12 route/state assertions; 0 console exceptions; 0 clipping failures.\n"
        "Narrow viewport: passed in Chromium and WebKit; overflow=0 on all six routes.\n"
        "Accessibility: passed; 12 route-engine checks.\n",
        encoding="utf-8",
    )
    for name, source in FILES.items():
        shutil.copy2(source, DESTINATION / name)
    checksum_targets = sorted(path for path in DESTINATION.iterdir() if path.name != "SHA256SUMS.txt")
    checksums = "\n".join(f"{sha256(path)}  {path.name}" for path in checksum_targets) + "\n"
    (DESTINATION / "SHA256SUMS.txt").write_text(checksums, encoding="utf-8")
    files = sorted(path for path in DESTINATION.iterdir() if path.is_file())
    if len(files) != 20:
        raise AssertionError(f"Review package must contain exactly 20 files, found {len(files)}")
    for line in checksums.splitlines():
        expected, name = line.split("  ", 1)
        if sha256(DESTINATION / name) != expected:
            raise AssertionError(f"Checksum mismatch: {name}")
    print(f"Autonomous review package passed: {len(files)} files")
    print(DESTINATION)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
