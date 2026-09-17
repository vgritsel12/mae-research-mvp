from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.repositories.database import SessionLocal
from app.services.mapping_analysis import generate_financial_mapping_analysis
from app.services.mapping_second_review import generate_financial_mapping_second_review


def main() -> int:
    session = SessionLocal()
    try:
        paths = generate_financial_mapping_analysis(session)
        print("Financial mapping analysis generated:")
        print(f"- JSON report: {paths.json_report}")
        print(f"- Markdown report: {paths.markdown_report}")
        print(f"- VERIFIED_PASS CSV: {paths.verified_csv}")
        print(f"- INVALID_MAPPING CSV: {paths.invalid_csv}")
        print(f"- Golden cases: {paths.golden_cases}")
        print(f"- Playbook: {paths.playbook}")
        print(f"- Frozen snapshot: {paths.snapshot}")
        second_paths = generate_financial_mapping_second_review(session)
        print("Financial mapping second review generated:")
        print(f"- JSON report: {second_paths.json_report}")
        print(f"- Markdown report: {second_paths.markdown_report}")
        print(f"- VERIFIED_PASS second review CSV: {second_paths.verified_csv}")
        print(f"- INVALID_MAPPING second review CSV: {second_paths.invalid_actions_csv}")
        print(f"- Strict golden cases: {second_paths.strict_golden_cases}")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
