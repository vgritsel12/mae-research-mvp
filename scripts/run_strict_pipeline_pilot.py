from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.repositories.database import ENGINE, SessionLocal, init_database
from app.services.staged_extraction import ConservativeStagedExtractionPipeline, write_pilot_report


def main() -> int:
    init_database(ENGINE)
    with SessionLocal() as session:
        pipeline = ConservativeStagedExtractionPipeline()
        report = pipeline.run_pilot(session, max_articles=15)
        json_path, md_path = write_pilot_report(report)
        session.commit()
    print("Strict pipeline pilot generated:")
    print(f"- JSON report: {json_path}")
    print(f"- Markdown report: {md_path}")
    print(f"- Run ID: {report['run_id']}")
    print(f"- Publications: {report['publication_count']}")
    print(f"- Candidates: {report['extracted_thesis_candidates']}")
    print(f"- Proposed views: {report['proposed_research_views']}")
    print(f"- STRICT_VALIDATED: {report['strict_validated']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
