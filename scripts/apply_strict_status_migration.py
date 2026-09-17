from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.repositories.database import ENGINE, SessionLocal, init_database
from app.services.strict_status_migration import apply_strict_second_review_status_migration


def main() -> int:
    init_database(ENGINE)
    with SessionLocal() as session:
        result = apply_strict_second_review_status_migration(session)
        session.commit()
    output_path = ROOT / "outputs" / "production_cleanup" / "strict_status_migration_report.json"
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("Strict status migration applied:")
    print(f"- Updated Research Views: {result['updated']}")
    print(f"- Report: {output_path}")
    print(f"- Recalculates coverage: {result['recalculates_coverage']}")
    print(f"- Rebuilds Current MAE: {result['rebuilds_current_mae']}")
    print(f"- Runs Final Controller: {result['runs_final_controller']}")
    print("- Status counts:")
    for status, count in result["status_counts"].items():
        print(f"  {status}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
