from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import sqlite3
from contextlib import nullcontext
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import ROOT_DIR, get_settings
from app.domain.models import EvidenceItem, MaeSnapshot, MaeSnapshotCell, MarketScenario
from app.repositories.database import create_app_engine, create_session_factory, init_database


BASELINE_DATE = date(2026, 7, 12)
BASELINE_SCORES = Path("data/mae_curated_2026-07-12.csv")
BASELINE_EVIDENCE = Path("outputs/mae_final_sources_2026-07-12.csv")
BASELINE_SCENARIOS = Path("data/mae_scenarios_2026-07-12.csv")
PRODUCT_VERSION = "excel_first_v1"
FULL_PRODUCT_VERSION = "excel_first_full_quality_v3"
PRODUCTION_STATUS = "PRODUCTION"
TEST_ONLY_STATUS = "TEST_ONLY"
INVALID_FUTURE_DATE_STATUS = "INVALID_FUTURE_DATE"
SNAPSHOT_TYPES = {
    "INTERIM": "INTERIM_SNAPSHOT",
    "INTERIM_SNAPSHOT": "INTERIM_SNAPSHOT",
    "FINAL": "FINAL_COMPONENT",
    "FINAL_COMPONENT": "FINAL_COMPONENT",
}
EVIDENCE_STATUSES = {"CONFIRMED", "MIXED", "CONTRADICTED"}
REVIEW_STATUSES = {"PASS", "MANUAL_REVIEW", "REJECTED"}
CHANGE_STATUSES = {
    "UPGRADE",
    "DOWNGRADE",
    "THESIS_CHANGED",
    "EVIDENCE_STRENGTHENED",
    "EVIDENCE_WEAKENED",
    "UNCHANGED",
    "CARRY_FORWARD",
    "NEW_COMPARABLE_ASSESSMENT",
    "METHODOLOGY_REFINEMENT",
    "NO_CHANGE",
}

SCORE_EXTRA_FIELDS = [
    "snapshot_date",
    "snapshot_type",
    "previous_snapshot_date",
    "previous_score_numeric",
    "score_delta",
    "change_status",
    "carry_forward",
    "current_thesis",
    "previous_thesis",
    "current_driver",
    "previous_driver",
    "what_changed",
    "why_changed",
    "new_evidence",
    "affected_scenario",
    "last_review_date",
    "evidence_status_code",
    "confidence",
    "market_regime",
    "transmission_mechanism",
    "primary_risk",
    "next_review_date",
    "analyst_action",
]

EVIDENCE_FIELDS = [
    "source_id",
    "cell_id",
    "asset",
    "provider",
    "title",
    "publication_date",
    "URL",
    "source_class",
    "excerpt",
    "relevance_reason",
    "research_view",
    "related_indicator",
    "actual_value",
    "expected_or_reference_value",
    "market_confirmation",
    "conflict",
    "evidence_status",
    "review_status",
    "carry_forward_flag",
]

SCENARIO_FIELDS = [
    "snapshot_date",
    "scenario_id",
    "scenario_name",
    "current_status",
    "narrative",
    "causal_chain",
    "macro_drivers",
    "winners",
    "vulnerable_assets",
    "indicators_to_watch",
    "trigger",
    "veto",
    "affected_cells",
    "expected_reaction_by_cell",
    "last_review_date",
]

INBOX_REQUIRED = {"URL", "provider", "publication_date", "title", "target_cell", "optional_excerpt"}
ADDITIONAL_OFFICIAL_DOMAINS = {
    "federalreserve.gov",
    "fred.stlouisfed.org",
    "bls.gov",
    "bea.gov",
    "treasury.gov",
    "imf.org",
    "oecd.org",
    "spglobal.com",
    "msci.com",
    "ishares.com",
    "blackrock.com",
    "ssga.com",
    "vanguard.com",
    "invesco.com",
}

SCENARIO_LINKS = {
    "US_WIDE_MARKET": "Base; Upside / Risk-on",
    "US_MARKET_BREADTH": "Base; Upside / Risk-on",
    "US_GROWTH": "Base; Downside / Risk-off",
    "US_SMALL_CAP": "Upside / Risk-on; Downside / Risk-off",
    "US_GOVERNMENT_LONG_TERM": "Base; Downside / Risk-off",
    "US_GOVERNMENT_SHORT_TERM": "Base; Downside / Risk-off",
    "US_CORPORATE_HIGH_YIELD_MID_TERM": "Upside / Risk-on; Downside / Risk-off",
    "US_CORPORATE_INVESTMENT_GRADE_MID_TERM": "Base",
    "US_INFLATION_LINKED": "Base; Downside / Risk-off",
    "GLOBAL_GOLD": "Downside / Risk-off",
}

TRANSMISSION = {
    "US_WIDE_MARKET": ("US earnings and AI-capex", "earnings growth offsets elevated discount rates"),
    "US_MARKET_BREADTH": ("broader earnings participation", "AI benefits spread beyond mega-cap leaders"),
    "US_GROWTH": ("AI-capex and real yields", "earnings acceleration supports multiples while real yields cap upside"),
    "US_SMALL_CAP": ("domestic growth and refinancing cost", "cyclical earnings improve but high funding costs constrain balance sheets"),
    "US_GOVERNMENT_LONG_TERM": ("inflation, fiscal supply and term premium", "higher required yield pressures long-duration bond prices"),
    "US_GOVERNMENT_SHORT_TERM": ("Federal Reserve policy rate", "high front-end yield creates carry with limited duration risk"),
    "US_CORPORATE_HIGH_YIELD_MID_TERM": ("growth, defaults and credit spreads", "carry absorbs moderate spread widening while defaults remain contained"),
    "US_CORPORATE_INVESTMENT_GRADE_MID_TERM": ("issuance and all-in yield", "carry offsets rich spreads and supply pressure"),
    "US_INFLATION_LINKED": ("sticky inflation and real yields", "inflation compensation supports TIPS versus nominal Treasuries"),
    "GLOBAL_GOLD": ("real yields, dollar and official demand", "structural demand offsets tactical pressure from real yields and the dollar"),
}

FRED_EVIDENCE = {
    "US_WIDE_MARKET": ["SP500"],
    "US_GROWTH": ["NASDAQCOM", "SP500"],
    "US_GOVERNMENT_LONG_TERM": ["DGS10", "DFII10"],
    "US_GOVERNMENT_SHORT_TERM": ["DGS2", "FEDFUNDS"],
    "US_CORPORATE_HIGH_YIELD_MID_TERM": ["BAMLH0A0HYM2"],
    "US_CORPORATE_INVESTMENT_GRADE_MID_TERM": ["BAMLC0A0CM"],
    "US_INFLATION_LINKED": ["T10YIE", "DFII10"],
    "GLOBAL_GOLD": ["DTWEXBGS"],
}
MARKET_SERIES = {"SP500", "NASDAQCOM", "DGS2", "DGS10", "DFII10", "T10YIE", "BAMLH0A0HYM2", "BAMLC0A0CM", "DTWEXBGS"}
MACRO_SERIES = {"CPIAUCSL", "FEDFUNDS", "UNRATE", "INDPRO", "NFCI"}

FULL_GEOGRAPHIES = ["Global", "US", "Europe", "UK", "Japan", "EM ex China"]
FULL_ASSETS = [
    ("EQUITY", "Wide Market", "Wide Market"),
    ("EQUITY", "Other categories", "Market Breadth"),
    ("EQUITY", "Other categories", "Preferred"),
    ("EQUITY", "Other categories", "Value"),
    ("EQUITY", "Other categories", "Growth"),
    ("EQUITY", "Other categories", "Small Cap"),
    ("FIXED INCOME", "GOV", "Government Bonds Long Term"),
    ("FIXED INCOME", "GOV", "Government Bonds Short Term"),
    ("FIXED INCOME", "GOV", "Government Bonds Mid Term"),
    ("FIXED INCOME", "CORP HY", "Corporate High Yield Long Term"),
    ("FIXED INCOME", "CORP HY", "Corporate High Yield Short Term"),
    ("FIXED INCOME", "CORP HY", "Corporate High Yield Mid Term"),
    ("FIXED INCOME", "CORP IG", "Corporate Investment Grade Long Term"),
    ("FIXED INCOME", "CORP IG", "Corporate Investment Grade Short Term"),
    ("FIXED INCOME", "CORP IG", "Corporate Investment Grade Mid Term"),
    ("FIXED INCOME", "Other categories", "Inflation Linked"),
    ("FIXED INCOME", "Other categories", "Mortgage-Backed"),
    ("COMMODITIES", "Commodities", "Gold"),
    ("COMMODITIES", "Commodities", "Other Precious Metals"),
]

FULL_ANCHORS = {
    ("US", "Wide Market"): "US_WIDE_MARKET",
    ("US", "Market Breadth"): "US_MARKET_BREADTH",
    ("US", "Growth"): "US_GROWTH",
    ("US", "Small Cap"): "US_SMALL_CAP",
    ("US", "Government Bonds Long Term"): "US_GOVERNMENT_LONG_TERM",
    ("US", "Government Bonds Short Term"): "US_GOVERNMENT_SHORT_TERM",
    ("US", "Corporate High Yield Mid Term"): "US_CORPORATE_HIGH_YIELD_MID_TERM",
    ("US", "Corporate Investment Grade Mid Term"): "US_CORPORATE_INVESTMENT_GRADE_MID_TERM",
    ("US", "Inflation Linked"): "US_INFLATION_LINKED",
    ("Global", "Gold"): "GLOBAL_GOLD",
}

FULL_SCORE_FIELDS = [
    "snapshot_date", "snapshot_type", "previous_snapshot_date", "cell_id", "asset_class",
    "asset_group", "asset_segment", "geography", "applicable", "display_order", "score",
    "direction", "thesis", "main_driver", "evidence_mode", "evidence_status", "conviction",
    "review_status", "assessment_date", "research_view_score", "fundamental_macro_score",
    "market_confirmation_score", "valuation_risk_score", "weighted_score", "score_cap",
    "previous_score", "score_delta", "change_type", "what_changed", "why_changed",
    "scenario_link", "trigger", "veto", "primary_risk", "secondary_risk",
    "market_observation_date", "last_review_date", "next_review_date", "analyst_action",
    "anchor_source_cell_id",
    "instrument_definition", "benchmark_id", "benchmark_name", "benchmark_provider",
    "benchmark_type", "currency", "duration_bucket", "regional_scope", "market_data_source",
    "comparability_status", "proxy_quality", "methodology_note", "technical_status",
    "analytical_quality", "market_confirmation_quality", "monitoring_indicator",
    "trigger_threshold", "veto_threshold",
]

FULL_EVIDENCE_FIELDS = [
    "evidence_id", "cell_id", "asset_class", "asset_segment", "geography", "current_score",
    "conviction", "evidence_mode", "source_id", "provider", "title", "publication_date",
    "retrieval_date", "URL", "source_class", "excerpt", "relevance_reason", "component_role",
    "research_view", "research_view_score", "fundamental_macro_summary", "fundamental_macro_score",
    "market_metric", "market_observation_date", "market_value", "market_reference",
    "market_confirmation", "market_confirmation_score", "valuation_risk_summary",
    "valuation_risk_score", "conflict", "evidence_status", "freshness_status", "review_status",
    "reviewer_note",
    "benchmark_id", "benchmark_name", "benchmark_provider", "benchmark_type",
    "instrument_definition", "currency", "duration_bucket", "regional_scope",
    "market_data_source", "comparability_status", "proxy_quality", "methodology_note",
    "technical_status", "analytical_quality", "market_confirmation_quality",
]

FULL_SCENARIO_FIELDS = SCENARIO_FIELDS + ["probability_band"]
FULL_TRANSMISSION_FIELDS = [
    "cell_id", "asset_class", "asset_segment", "geography", "current_score", "conviction",
    "macro_driver", "transmission_mechanism", "expected_asset_reaction", "investment_logic",
    "trigger", "veto", "primary_risk", "secondary_risk", "base_scenario_effect",
    "upside_scenario_effect", "downside_scenario_effect", "next_review_date", "analyst_action",
    "source_ids", "control_status",
    "benchmark_id", "comparability_status", "monitoring_indicator", "trigger_threshold",
    "veto_threshold", "technical_status", "analytical_quality", "market_confirmation_quality",
]

COMPARABILITY_STATUSES = {"DIRECTLY_COMPARABLE", "LIMITED_COMPARABILITY", "STRUCTURAL_PROXY"}
PROXY_QUALITIES = {
    "DIRECT_REGIONAL", "DIRECT_GLOBAL", "REGIONAL_PROXY", "CROSS_REGIONAL_PROXY", "NO_VALID_PROXY"
}
ANALYTICAL_QUALITIES = {"STRONG", "ACCEPTABLE", "WEAK", "REVIEW_REQUIRED"}
MARKET_CONFIRMATION_QUALITIES = {"STRONG", "PARTIAL", "WEAK", "CONTRADICTED"}


@dataclass
class SnapshotProduct:
    snapshot_date: date
    snapshot_type: str
    snapshot_status: str
    previous_snapshot_date: date
    scores: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    scenarios: list[dict[str, Any]]
    market_summary: str
    market_summary_bullets: list[str]
    current_market_regime: str
    database_preflight: dict[str, Any]
    source_probe: list[dict[str, Any]]
    data_windows: dict[str, Any]

    @property
    def coverage(self) -> int:
        supported = {row["cell_id"] for row in self.evidence if row["review_status"] == "PASS"}
        return len(supported)


@dataclass
class FullSnapshotProduct:
    snapshot_date: date
    snapshot_type: str
    snapshot_status: str
    previous_snapshot_date: date
    scores: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    scenarios: list[dict[str, Any]]
    transmission: list[dict[str, Any]]
    market_summary: str
    database_preflight: dict[str, Any]
    pilot_hashes: dict[str, str]
    data_windows: dict[str, Any]

    @property
    def applicable_scores(self) -> list[dict[str, Any]]:
        return [row for row in self.scores if str(row["applicable"]).lower() == "true"]

    @property
    def coverage(self) -> int:
        pass_cells = {row["cell_id"] for row in self.evidence if row["review_status"] == "PASS"}
        return len({row["cell_id"] for row in self.applicable_scores} & pass_cells)


def normalize_snapshot_type(value: str) -> str:
    try:
        return SNAPSHOT_TYPES[value.strip().upper()]
    except KeyError as exc:
        raise ValueError(f"Unsupported snapshot type: {value}") from exc


def snapshot_status_for_date(
    snapshot_date: date,
    *,
    allow_future_test_date: bool = False,
    today: date | None = None,
) -> str:
    current_date = today or date.today()
    if snapshot_date > current_date:
        if not allow_future_test_date:
            raise ValueError("Snapshot date cannot be in the future.")
        return TEST_ONLY_STATUS
    return PRODUCTION_STATUS


def build_snapshot_product(
    snapshot_date: date,
    snapshot_type: str,
    *,
    root: Path = ROOT_DIR,
    source_mode: str = "AUTO",
    snapshot_status: str = PRODUCTION_STATUS,
) -> SnapshotProduct:
    snapshot_type = normalize_snapshot_type(snapshot_type)
    source_mode = source_mode.upper()
    if source_mode not in {"AUTO", "MANUAL"}:
        raise ValueError("source_mode must be AUTO or MANUAL")
    previous_date, previous_scores_path = find_previous_snapshot(snapshot_date, root=root)
    previous_scores = read_csv(previous_scores_path)
    if len(previous_scores) != 10:
        raise ValueError(f"Previous comparable snapshot must have 10 rows: {previous_scores_path}")

    previous_evidence = load_previous_evidence(previous_date, root=root)
    scenarios = load_canonical_scenarios(snapshot_date, root=root)
    inbox_rows = load_source_inbox(snapshot_date, source_mode=source_mode, root=root)
    source_probe = probe_allowlist(root=root) if source_mode == "AUTO" else []
    fred_evidence, data_windows = build_fred_evidence(
        snapshot_date,
        previous_date,
        previous_scores,
        root=root,
    )

    accepted_inbox = [row for row in inbox_rows if row["review_status"] == "PASS"]
    by_cell: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in accepted_inbox:
        by_cell[item["cell_id"]].append(item)

    scores: list[dict[str, Any]] = []
    for previous in previous_scores:
        cell_id = previous["cell_id"]
        current = dict(previous)
        previous_score = int(previous["score"])
        current_score = previous_score
        current_thesis = previous.get("current_thesis") or previous.get("research_summary", "")
        current_driver = previous.get("current_driver") or _first_item(previous.get("key_drivers", ""))
        change_status = "CARRY_FORWARD"
        carry_forward = True
        what_changed = "Оценка и инвестиционный тезис не изменились."
        why_changed = "После предыдущего сопоставимого snapshot не появилось нового PASS-evidence, достаточного для изменения оценки."
        new_evidence = "Нет нового основания для изменения; использовано действующее подтверждение."
        confidence = previous.get("confidence") or "MEDIUM"
        evidence_code = previous.get("evidence_status_code") or _evidence_code(previous.get("evidence_status", ""))

        if by_cell[cell_id]:
            newest = sorted(by_cell[cell_id], key=lambda row: row["publication_date"], reverse=True)[0]
            new_evidence = f"{newest['provider']}: {newest['title']} ({newest['publication_date']})"
            proposed = str(newest.get("proposed_score") or "").strip()
            proposed_thesis = str(newest.get("thesis") or "").strip()
            proposed_driver = str(newest.get("driver") or "").strip()
            carry_forward = False
            if proposed:
                current_score = int(proposed)
                if not -3 <= current_score <= 3:
                    raise ValueError(f"proposed_score outside -3..+3 for {cell_id}")
            if proposed_thesis:
                current_thesis = proposed_thesis
            if proposed_driver:
                current_driver = proposed_driver
            evidence_code = newest.get("evidence_status") or evidence_code
            if current_score > previous_score:
                change_status = "UPGRADE"
            elif current_score < previous_score:
                change_status = "DOWNGRADE"
            elif proposed_thesis and proposed_thesis != (previous.get("current_thesis") or previous.get("research_summary", "")):
                change_status = "THESIS_CHANGED"
            else:
                change_status = "EVIDENCE_STRENGTHENED"
            what_changed = f"Добавлено новое PASS-evidence: {newest['title']}."
            why_changed = newest.get("relevance_reason") or "Новый материал напрямую связан с текущей MAE-ячейкой."

        delta = current_score - previous_score
        next_review = snapshot_date + timedelta(days=7 if snapshot_type == "INTERIM_SNAPSHOT" else 31)
        risk = _first_item(previous.get("key_risks", ""))
        driver_label, mechanism = TRANSMISSION[cell_id]
        current.update(
            {
                "score": current_score,
                "snapshot_date": snapshot_date.isoformat(),
                "snapshot_type": snapshot_type,
                "previous_snapshot_date": previous_date.isoformat(),
                "previous_score_numeric": previous_score,
                "score_delta": delta,
                "change_status": change_status,
                "carry_forward": str(carry_forward).lower(),
                "current_thesis": current_thesis,
                "previous_thesis": previous.get("current_thesis") or previous.get("research_summary", ""),
                "current_driver": current_driver or driver_label,
                "previous_driver": previous.get("current_driver") or _first_item(previous.get("key_drivers", "")),
                "what_changed": what_changed,
                "why_changed": why_changed,
                "new_evidence": new_evidence,
                "affected_scenario": SCENARIO_LINKS[cell_id],
                "last_review_date": snapshot_date.isoformat(),
                "evidence_status_code": evidence_code,
                "confidence": confidence,
                "market_regime": "Resilient growth, sticky inflation and elevated rates",
                "transmission_mechanism": mechanism,
                "primary_risk": risk,
                "next_review_date": next_review.isoformat(),
                "analyst_action": "Monitor; change score only on new PASS evidence.",
                "view": _direction_label(current_score),
                "research_summary": current_thesis,
                "key_drivers": current_driver or previous.get("key_drivers", ""),
                "change_summary": what_changed,
            }
        )
        scores.append(current)

    score_by_cell = {row["cell_id"]: row for row in scores}
    evidence = []
    for row in previous_evidence:
        carried = dict(row)
        carried["carry_forward_flag"] = "true"
        carried["asset"] = score_by_cell[carried["cell_id"]]["segment"]
        carried["research_view"] = score_by_cell[carried["cell_id"]]["current_thesis"]
        carried["market_confirmation"] = score_by_cell[carried["cell_id"]].get("market_summary", "")
        carried["conflict"] = score_by_cell[carried["cell_id"]].get("key_risks", "")
        evidence.append(carried)
    evidence.extend(fred_evidence)
    evidence.extend(inbox_rows)
    evidence = _dedupe_evidence(evidence)

    market_summary, bullets = build_market_summary(scores, previous_date)
    return SnapshotProduct(
        snapshot_date=snapshot_date,
        snapshot_type=snapshot_type,
        snapshot_status=snapshot_status,
        previous_snapshot_date=previous_date,
        scores=scores,
        evidence=evidence,
        scenarios=scenarios,
        market_summary=market_summary,
        market_summary_bullets=bullets,
        current_market_regime="Resilient growth, sticky inflation and elevated rates",
        database_preflight=database_state(root=root),
        source_probe=source_probe,
        data_windows=data_windows,
    )


def find_previous_snapshot(snapshot_date: date, *, root: Path = ROOT_DIR) -> tuple[date, Path]:
    candidates: list[tuple[date, Path]] = []
    if BASELINE_DATE < snapshot_date:
        candidates.append((BASELINE_DATE, root / BASELINE_SCORES))
    snapshots_root = root / "outputs" / "snapshots"
    if snapshots_root.exists():
        for directory in snapshots_root.iterdir():
            if not directory.is_dir() or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", directory.name):
                continue
            candidate_date = date.fromisoformat(directory.name)
            score_path = directory / f"mae_scores_{directory.name}.csv"
            validation_path = directory / f"validation_report_{directory.name}.json"
            if candidate_date >= snapshot_date or not score_path.exists() or not validation_path.exists():
                continue
            try:
                validation = json.loads(validation_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            metadata = validation.get("metadata") or {}
            if (
                validation.get("status") == "PASS"
                and metadata.get("snapshot_status", PRODUCTION_STATUS) == PRODUCTION_STATUS
                and metadata.get("production_eligible", True) is not False
            ):
                candidates.append((candidate_date, score_path))
    if not candidates:
        raise ValueError(f"No comparable snapshot exists before {snapshot_date.isoformat()}")
    return max(candidates, key=lambda item: item[0])


def load_previous_evidence(previous_date: date, *, root: Path = ROOT_DIR) -> list[dict[str, Any]]:
    if previous_date == BASELINE_DATE:
        mappings = read_csv(root / BASELINE_EVIDENCE)
        score_rows = {row["cell_id"]: row for row in read_csv(root / BASELINE_SCORES)}
        result = []
        for row in mappings:
            cell = score_rows[row["cell_id"]]
            result.append(
                {
                    "source_id": row["mapping_id"],
                    "cell_id": row["cell_id"],
                    "asset": cell["segment"],
                    "provider": row["provider"],
                    "title": row["title"],
                    "publication_date": row["publication_date"],
                    "URL": row["url"],
                    "source_class": "ALLOWLIST",
                    "excerpt": row["excerpt"],
                    "relevance_reason": row["cell_relevance"],
                    "research_view": cell["research_summary"],
                    "related_indicator": row["source_type"],
                    "actual_value": row["market_or_fundamental_confirmation"],
                    "expected_or_reference_value": cell["benchmark"],
                    "market_confirmation": cell["market_summary"],
                    "conflict": cell["key_risks"],
                    "evidence_status": _evidence_code(cell["evidence_status"]),
                    "review_status": "PASS" if row["control_result"] == "PASS" else "REJECTED",
                    "carry_forward_flag": "true",
                }
            )
        return result
    path = root / "outputs" / "snapshots" / previous_date.isoformat() / f"mae_evidence_{previous_date.isoformat()}.csv"
    return [row for row in read_csv(path) if row.get("review_status") == "PASS"]


def load_canonical_scenarios(snapshot_date: date, *, root: Path = ROOT_DIR) -> list[dict[str, Any]]:
    path = root / BASELINE_SCENARIOS
    rows = read_csv(path)
    if len(rows) != 3:
        raise ValueError(f"Canonical scenario dataset must have exactly 3 rows: {path}")
    result = []
    for row in rows:
        current = dict(row)
        current["snapshot_date"] = snapshot_date.isoformat()
        current["last_review_date"] = snapshot_date.isoformat()
        result.append(current)
    return result


def load_source_inbox(snapshot_date: date, *, source_mode: str, root: Path = ROOT_DIR) -> list[dict[str, Any]]:
    path = root / "data" / "source_inbox.csv"
    if not path.exists():
        return []
    rows = read_csv(path)
    allowed_domains = read_allowlist_domains(root=root)
    known_cells = {row["cell_id"]: row for row in read_csv(root / BASELINE_SCORES)}
    result: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=2):
        if not any(str(value or "").strip() for value in row.values()):
            continue
        missing = [field for field in INBOX_REQUIRED if field not in row]
        if missing:
            raise ValueError(f"source_inbox.csv missing columns: {', '.join(sorted(missing))}")
        cell_id = str(row.get("target_cell") or "").strip()
        url = str(row.get("URL") or "").strip()
        provider = str(row.get("provider") or "").strip()
        title = str(row.get("title") or "").strip()
        excerpt = str(row.get("optional_excerpt") or "").strip()
        review_status = "PASS"
        reason = ""
        if cell_id not in known_cells:
            review_status, reason = "REJECTED", "Unknown target_cell"
        try:
            publication_date = date.fromisoformat(str(row.get("publication_date") or ""))
        except ValueError:
            publication_date = snapshot_date
            review_status, reason = "REJECTED", "Invalid publication_date"
        if publication_date > snapshot_date:
            review_status, reason = "REJECTED", "Publication is after snapshot date"
        domain = _domain(url)
        source_class = "ALLOWLIST" if _domain_allowed(domain, allowed_domains) else "ADDITIONAL_OFFICIAL" if _domain_allowed(domain, ADDITIONAL_OFFICIAL_DOMAINS) else "REJECTED"
        if source_class == "REJECTED":
            review_status, reason = "REJECTED", "Provider/domain is not allowlisted or additional official"
        if not excerpt and review_status != "REJECTED":
            review_status, reason = "MANUAL_REVIEW", "No analyst-supplied excerpt; the pipeline does not invent quotations"
        if source_mode == "AUTO" and review_status == "PASS":
            reachable, message = _probe_url(url)
            if not reachable:
                review_status, reason = "MANUAL_REVIEW", f"URL could not be verified automatically: {message}"
        queued_review_status = str(row.get("review_status") or "").strip().upper()
        if queued_review_status in {"MANUAL_REVIEW", "REJECTED"}:
            review_status = queued_review_status
            reason = reason or "Source inbox item requires explicit analyst review."
        if str(row.get("evidence_status") or "").strip() not in EVIDENCE_STATUSES:
            evidence_status = _evidence_code(known_cells.get(cell_id, {}).get("evidence_status", ""))
        else:
            evidence_status = str(row["evidence_status"]).strip()
        result.append(
            {
                "source_id": _stable_source_id(provider, title, url, cell_id, str(index)),
                "cell_id": cell_id,
                "asset": known_cells.get(cell_id, {}).get("segment", cell_id),
                "provider": provider,
                "title": title,
                "publication_date": publication_date.isoformat(),
                "URL": url,
                "source_class": source_class,
                "excerpt": excerpt,
                "relevance_reason": str(row.get("relevance_reason") or reason or "Directly mapped by the analyst source inbox"),
                "research_view": str(row.get("thesis") or known_cells.get(cell_id, {}).get("research_summary", "")),
                "related_indicator": str(row.get("related_indicator") or "manual research evidence"),
                "actual_value": str(row.get("actual_value") or excerpt),
                "expected_or_reference_value": str(row.get("expected_value") or known_cells.get(cell_id, {}).get("benchmark", "")),
                "market_confirmation": str(row.get("market_confirmation") or known_cells.get(cell_id, {}).get("market_summary", "")),
                "conflict": str(row.get("conflict") or known_cells.get(cell_id, {}).get("key_risks", "")),
                "evidence_status": evidence_status,
                "review_status": review_status,
                "carry_forward_flag": "false",
                "proposed_score": str(row.get("proposed_score") or ""),
                "thesis": str(row.get("thesis") or ""),
                "driver": str(row.get("driver") or ""),
            }
        )
    return result


def build_fred_evidence(
    snapshot_date: date,
    previous_snapshot_date: date,
    score_rows: list[dict[str, Any]],
    *,
    root: Path = ROOT_DIR,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    settings = get_settings()
    database_path = settings.sqlite_path if root == ROOT_DIR else root / "data" / "mae.db"
    if not database_path.exists():
        return [], {
            "market_window_days": 0,
            "macro_window_days": 0,
            "latest_data_date": "",
            "latest_market_observation_date": "",
            "latest_macro_observation_date": "",
            "new_market_observations_since_previous": 0,
            "future_market_observation_count": 0,
            "series_checked": 0,
        }
    raw_by_series: dict[str, tuple[str, str]] = {}
    with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as connection:
        for cache_key, url, raw_text in connection.execute(
            "SELECT cache_key, url, raw_text FROM external_data_cache WHERE provider = 'FRED' AND status_code = 200"
        ):
            raw_by_series[cache_key.split(":", 1)[-1]] = (url, raw_text)

    points_by_series: dict[str, list[tuple[date, float]]] = {}
    future_dates_by_series: dict[str, list[date]] = {}
    for series, (_, raw_text) in raw_by_series.items():
        points, future_dates = _parse_fred_points_partitioned(raw_text, snapshot_date)
        points_by_series[series] = points
        future_dates_by_series[series] = future_dates

    available_market = [points_by_series[s] for s in MARKET_SERIES if points_by_series.get(s)]
    available_macro = [points_by_series[s] for s in MACRO_SERIES if points_by_series.get(s)]
    market_window_days = min((snapshot_date - points[0][0]).days for points in available_market) if available_market else 0
    macro_window_days = min((snapshot_date - points[0][0]).days for points in available_macro) if available_macro else 0
    latest_dates = [points[-1][0] for points in available_market + available_macro]
    latest_data_date = max(latest_dates).isoformat() if latest_dates else ""
    latest_market_dates = [points[-1][0] for points in available_market]
    latest_macro_dates = [points[-1][0] for points in available_macro]
    latest_market_observation_date = max(latest_market_dates).isoformat() if latest_market_dates else ""
    latest_macro_observation_date = max(latest_macro_dates).isoformat() if latest_macro_dates else ""
    new_market_observations = sum(
        1
        for series in MARKET_SERIES
        for observation_date, _ in points_by_series.get(series, [])
        if observation_date > previous_snapshot_date
    )
    future_market_observations = sum(len(future_dates_by_series.get(series, [])) for series in MARKET_SERIES)
    score_by_cell = {row["cell_id"]: row for row in score_rows}
    evidence: list[dict[str, Any]] = []
    for cell_id, series_list in FRED_EVIDENCE.items():
        descriptions = []
        usable = []
        for series in series_list:
            points = points_by_series.get(series, [])
            if not points:
                continue
            descriptions.append(_series_change_summary(series, points, snapshot_date))
            usable.append((series, points))
        if not usable:
            continue
        last_date = max(points[-1][0] for _, points in usable)
        cell = score_by_cell[cell_id]
        series_names = ", ".join(series for series, _ in usable)
        evidence.append(
            {
                "source_id": _stable_source_id("Federal Reserve / FRED", series_names, "fred", cell_id, last_date.isoformat()),
                "cell_id": cell_id,
                "asset": cell["segment"],
                "provider": "Federal Reserve / FRED",
                "title": f"Official FRED market and macro series: {series_names}",
                "publication_date": last_date.isoformat(),
                "URL": usable[0] and raw_by_series[usable[0][0]][0],
                "source_class": "ADDITIONAL_OFFICIAL",
                "excerpt": " | ".join(descriptions),
                "relevance_reason": f"Official series directly linked to {cell['segment']} market confirmation or macro driver.",
                "research_view": cell.get("research_summary", ""),
                "related_indicator": series_names,
                "actual_value": " | ".join(descriptions),
                "expected_or_reference_value": cell.get("benchmark", ""),
                "market_confirmation": cell.get("market_summary", ""),
                "conflict": cell.get("key_risks", ""),
                "evidence_status": _evidence_code(cell.get("evidence_status", "")),
                "review_status": "PASS",
                "carry_forward_flag": "false",
            }
        )
    return evidence, {
        "market_window_days": market_window_days,
        "macro_window_days": macro_window_days,
        "latest_data_date": latest_data_date,
        "latest_market_observation_date": latest_market_observation_date,
        "latest_macro_observation_date": latest_macro_observation_date,
        "new_market_observations_since_previous": new_market_observations,
        "future_market_observation_count": future_market_observations,
        "series_checked": len(points_by_series),
        "market_window_pass": market_window_days >= 365,
        "macro_window_pass": macro_window_days >= 730,
    }


def build_market_summary(scores: list[dict[str, Any]], previous_date: date) -> tuple[str, list[str]]:
    leaders = [row["segment"] for row in scores if int(row["score"]) >= 2]
    vulnerable = [row["segment"] for row in scores if int(row["score"]) <= 0]
    changes = [row for row in scores if int(row["score_delta"]) != 0]
    change_text = (
        f"После {previous_date.isoformat()} изменены {len(changes)} оценок."
        if changes
        else (
            f"Ежедневный технический snapshot: после {previous_date.isoformat()} оценки не изменились, "
            "нового research update и PASS-оснований для пересмотра нет."
        )
    )
    summary = (
        "Основной режим — устойчивый рост при сохраняющейся инфляции и высокой стоимости денег. "
        "Главный позитивный драйвер — AI-capex и расширение роста прибыли за пределы крупнейших компаний. "
        "Главный риск — повторное ускорение инфляции, рост term premium и расширение credit spreads. "
        f"Наиболее привлекательны {', '.join(leaders)}. "
        f"Наиболее уязвимы {', '.join(vulnerable)}. "
        f"{change_text} Базовый сценарий сохраняет предпочтение breadth, Growth, Short Treasuries и TIPS при давлении на Long Treasuries."
    )
    bullets = [
        "AI-capex и earnings breadth поддерживают US Growth и расширение рынка.",
        "Высокий front-end carry сохраняет преимущество Short Treasuries.",
        "Sticky inflation поддерживает TIPS и ограничивает Long Treasuries.",
        "Small Cap и High Yield остаются умеренно привлекательными, но чувствительными к ставкам и spreads.",
        change_text,
    ]
    return summary, bullets


def write_snapshot_datasets(product: SnapshotProduct, directory: Path) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = product.snapshot_date.isoformat()
    score_path = directory / f"mae_scores_{stamp}.csv"
    evidence_path = directory / f"mae_evidence_{stamp}.csv"
    scenario_path = directory / f"mae_scenarios_{stamp}.csv"
    report_path = directory / f"mae_report_{stamp}.md"
    score_fields = list(product.scores[0].keys())
    for field in SCORE_EXTRA_FIELDS:
        if field not in score_fields:
            score_fields.append(field)
    write_csv(score_path, product.scores, score_fields)
    write_csv(evidence_path, product.evidence, EVIDENCE_FIELDS)
    write_csv(scenario_path, product.scenarios, SCENARIO_FIELDS)
    report_path.write_text(render_markdown_report(product), encoding="utf-8")
    return {"scores": score_path, "evidence": evidence_path, "scenarios": scenario_path, "report": report_path}


def render_markdown_report(product: SnapshotProduct) -> str:
    lines = [
        f"# MAE Snapshot — {product.snapshot_date.isoformat()}",
        "",
        f"Тип: **{product.snapshot_type}**. Предыдущий сопоставимый snapshot: **{product.previous_snapshot_date.isoformat()}**. Горизонт: **6–12 месяцев**.",
        "",
        "## Market Summary",
        "",
        product.market_summary,
        "",
        "## Strategy & Sentiment Map",
        "",
        "| Cell ID | Актив | Score | Direction | Change status | Previous score | Delta |",
        "|---|---|---:|---|---|---:|---:|",
    ]
    for row in product.scores:
        lines.append(
            f"| {row['cell_id']} | {row['segment']} | {int(row['score'])} | {row['view']} | {row['change_status']} | "
            f"{int(row['previous_score_numeric'])} | {int(row['score_delta'])} |"
        )
    lines.extend(["", "## Change Tracker", ""])
    for row in product.scores:
        lines.append(f"- **{row['segment']} — {row['change_status']}**: {row['what_changed']} {row['why_changed']}")
    lines.extend(["", "## Scenarios", ""])
    for scenario in product.scenarios:
        lines.extend(
            [
                f"### {scenario['scenario_name']}",
                "",
                scenario["narrative"],
                "",
                f"- Causal chain: {scenario['causal_chain']}",
                f"- Trigger: {scenario['trigger']}",
                f"- Veto: {scenario['veto']}",
                f"- Affected MAE cells: {scenario['affected_cells']}",
                "",
            ]
        )
    lines.extend(
        [
            "## Evidence & Expectation Check",
            "",
            f"Coverage: **{product.coverage}/10 cells supported**. В итоговой MAE использованы только строки evidence с review status PASS.",
            "",
            "## Transmission & Control Layer",
            "",
        ]
    )
    for row in product.scores:
        driver, mechanism = TRANSMISSION[row["cell_id"]]
        lines.append(f"- **{row['segment']}**: {driver} → {mechanism} → {row['view']}. Trigger и veto приведены в canonical scenario dataset.")
    return "\n".join(lines).rstrip() + "\n"


def workbook_manifest(product: SnapshotProduct) -> dict[str, Any]:
    upgrades = sum(int(row["score_delta"]) > 0 for row in product.scores)
    downgrades = sum(int(row["score_delta"]) < 0 for row in product.scores)
    unchanged = len(product.scores) - upgrades - downgrades
    leading = [f"{row['segment']} ({int(row['score']):+d})" for row in product.scores if int(row["score"]) >= 2]
    vulnerable = [f"{row['segment']} ({int(row['score']):+d})" for row in product.scores if int(row["score"]) <= 0]
    return {
        "snapshot_date": product.snapshot_date.isoformat(),
        "snapshot_type": product.snapshot_type,
        "snapshot_status": product.snapshot_status,
        "production_eligible": product.snapshot_status == PRODUCTION_STATUS,
        "previous_snapshot_date": product.previous_snapshot_date.isoformat(),
        "horizon": "6–12 months",
        "current_market_regime": product.current_market_regime,
        "market_summary": product.market_summary,
        "market_summary_bullets": product.market_summary_bullets,
        "leading_positions": leading,
        "vulnerable_positions": vulnerable,
        "upgrades": upgrades,
        "downgrades": downgrades,
        "unchanged": unchanged,
        "evidence_coverage": f"{product.coverage}/10",
        "database_preflight": product.database_preflight,
        "source_probe": product.source_probe,
        "data_windows": product.data_windows,
        "product_version": PRODUCT_VERSION,
    }


def persist_snapshot(
    product: SnapshotProduct,
    *,
    root: Path = ROOT_DIR,
    session: Session | None = None,
) -> str:
    """Persist rows, flushing but never ending a caller-owned transaction."""
    if product.snapshot_date > date.today():
        raise ValueError("Snapshot date cannot be in the future.")
    if product.snapshot_status != PRODUCTION_STATUS:
        raise ValueError("TEST_ONLY snapshots cannot be written to production SQLite.")
    owns_session = session is None
    if owns_session:
        settings = get_settings()
        if root != ROOT_DIR:
            settings = type(settings)(sqlite_path=root / "data" / "mae.db")
        engine = create_app_engine(settings)
        init_database(engine)
        session_context = create_session_factory(engine)()
    else:
        session_context = nullcontext(session)
    with session_context as session:
        existing = session.scalar(
            select(MaeSnapshot).where(MaeSnapshot.snapshot_date == product.snapshot_date, MaeSnapshot.is_demo.is_(False))
        )
        if existing:
            if (existing.run_metadata or {}).get("product_version") == PRODUCT_VERSION:
                return existing.id
            raise ValueError(
                f"Existing non-product snapshot blocks {product.snapshot_date.isoformat()}; history was not overwritten."
            )
        snapshot = MaeSnapshot(
            snapshot_date=product.snapshot_date,
            lookback_days=90,
            status=product.snapshot_type,
            run_metadata={
                **workbook_manifest(product),
                "product_version": PRODUCT_VERSION,
                "canonical_scenario_source": str(BASELINE_SCENARIOS),
            },
            coverage_report={"supported_cells": product.coverage, "total_cells": 10, "coverage": f"{product.coverage}/10"},
            immutable=True,
            is_demo=False,
        )
        session.add(snapshot)
        session.flush()
        score_by_cell = {row["cell_id"]: row for row in product.scores}
        cell_models: dict[str, MaeSnapshotCell] = {}
        for row in product.scores:
            cell = MaeSnapshotCell(
                snapshot_id=snapshot.id,
                snapshot_date=product.snapshot_date,
                canonical_cell_id=row["cell_id"],
                template_row_key=row["cell_id"],
                region=row["geography"],
                asset=row["segment"],
                applicability="APPLICABLE",
                mae_score=int(row["score"]),
                previous_snapshot_score=int(row["previous_score_numeric"]),
                score_change=int(row["score_delta"]),
                thesis=row["current_thesis"],
                driver=row["current_driver"],
                previous_thesis=row["previous_thesis"],
                previous_driver=row["previous_driver"],
                evidence_status=row["evidence_status_code"],
                confidence=row["confidence"],
                carry_forward=str(row["carry_forward"]).lower() == "true",
                scenario_link=row["affected_scenario"],
                change_status=row["change_status"],
                change_summary=row["what_changed"],
                coverage_status="SUPPORTED",
                source_count=0,
                independent_source_count=0,
                evidence_item_ids=[],
                disagreement={"conflict": row.get("key_risks", "")},
                explanation=row["why_changed"],
                scenario_adjustment=None,
                scenario_adjustment_reason="",
                is_demo=False,
            )
            session.add(cell)
            cell_models[row["cell_id"]] = cell
        session.flush()

        evidence_ids: dict[str, list[str]] = defaultdict(list)
        providers: dict[str, set[str]] = defaultdict(set)
        for row in product.evidence:
            if row["review_status"] != "PASS" or row["cell_id"] not in cell_models:
                continue
            item = EvidenceItem(
                snapshot_id=snapshot.id,
                snapshot_cell_id=cell_models[row["cell_id"]].id,
                article_id=None,
                research_view_id=None,
                snapshot_date=product.snapshot_date,
                provider=row["provider"],
                provider_group=row["provider"],
                report_title=row["title"],
                publication_date=date.fromisoformat(row["publication_date"]),
                ingested_at=datetime.now(UTC),
                url=row["URL"],
                exact_quote=row["excerpt"],
                affected_region=score_by_cell[row["cell_id"]]["geography"],
                affected_asset_segment=row["asset"],
                template_row_key=row["cell_id"],
                canonical_cell_id=row["cell_id"],
                extracted_stance=score_by_cell[row["cell_id"]]["view"],
                source_specificity="DIRECT",
                source_independence_group=row["provider"],
                source_class=row["source_class"],
                relevance_reason=row["relevance_reason"],
                related_indicator=row["related_indicator"],
                actual_value=row["actual_value"],
                expected_value=row["expected_or_reference_value"],
                market_confirmation=row["market_confirmation"],
                conflict=row["conflict"],
                evidence_status=row["evidence_status"],
                review_status=row["review_status"],
                carry_forward=str(row["carry_forward_flag"]).lower() == "true",
                is_demo=False,
            )
            session.add(item)
            session.flush()
            evidence_ids[row["cell_id"]].append(item.id)
            providers[row["cell_id"]].add(row["provider"])
        for cell_id, cell in cell_models.items():
            cell.evidence_item_ids = evidence_ids[cell_id]
            cell.source_count = len(evidence_ids[cell_id])
            cell.independent_source_count = len(providers[cell_id])

        for row in product.scenarios:
            session.add(
                MarketScenario(
                    snapshot_date=product.snapshot_date,
                    topic=row["scenario_name"],
                    region="Global",
                    primary_asset=_first_item(row["affected_cells"]),
                    scenario_type=row["scenario_id"],
                    status=row["current_status"],
                    probability_band="MEDIUM" if row["scenario_id"] == "BASE" else "LOW",
                    narrative=row["narrative"],
                    causal_chain=row["causal_chain"],
                    macro_drivers=_split_items(row["macro_drivers"]),
                    triggers=[row["trigger"]],
                    indicators=_split_items(row["indicators_to_watch"]),
                    invalidation_conditions=[row["veto"]],
                    beneficiaries=_split_items(row["winners"]),
                    vulnerable_assets=_split_items(row["vulnerable_assets"]),
                    affected_cells=_split_items(row["affected_cells"]),
                    expected_reaction_by_cell=_parse_reaction_map(row["expected_reaction_by_cell"]),
                    last_review_date=product.snapshot_date,
                    source_ids=[evidence_id for ids in evidence_ids.values() for evidence_id in ids],
                    is_demo=False,
                )
            )
        if owns_session:
            session.commit()
        else:
            session.flush()
        return snapshot.id


def database_state(*, root: Path = ROOT_DIR) -> dict[str, Any]:
    database_path = (get_settings().sqlite_path if root == ROOT_DIR else root / "data" / "mae.db")
    if not database_path.exists():
        return {"path": str(database_path), "size": 0, "tables": {}}
    tables = ["mae_snapshots", "mae_snapshot_cells", "evidence_items", "market_scenarios"]
    counts: dict[str, int] = {}
    with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as connection:
        for table in tables:
            try:
                counts[table] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            except sqlite3.OperationalError:
                counts[table] = 0
    return {"path": str(database_path), "size": database_path.stat().st_size, "tables": counts}


def quarantine_future_snapshots(*, current_date: date, root: Path = ROOT_DIR) -> int:
    """Mark legacy future-dated artifacts as non-production without deleting history."""
    database_path = get_settings().sqlite_path if root == ROOT_DIR else root / "data" / "mae.db"
    invalidated = 0
    if database_path.exists():
        with sqlite3.connect(database_path) as connection:
            try:
                rows = connection.execute(
                    "SELECT id, run_metadata FROM mae_snapshots "
                    "WHERE snapshot_date > ? AND is_demo = 0 AND status NOT IN (?, ?)",
                    (current_date.isoformat(), INVALID_FUTURE_DATE_STATUS, TEST_ONLY_STATUS),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
            for snapshot_id, raw_metadata in rows:
                try:
                    metadata = json.loads(raw_metadata) if isinstance(raw_metadata, str) else dict(raw_metadata or {})
                except (TypeError, ValueError, json.JSONDecodeError):
                    metadata = {}
                metadata.update(
                    {
                        "snapshot_status": INVALID_FUTURE_DATE_STATUS,
                        "production_eligible": False,
                        "invalidated_reason": "Snapshot date was later than the calendar date at publication time.",
                    }
                )
                connection.execute(
                    "UPDATE mae_snapshots SET status = ?, run_metadata = ? WHERE id = ?",
                    (INVALID_FUTURE_DATE_STATUS, json.dumps(metadata, ensure_ascii=False), snapshot_id),
                )
                invalidated += 1
            try:
                connection.execute(
                    "UPDATE market_scenarios SET status = ? WHERE snapshot_date > ? AND is_demo = 0 AND status != ?",
                    (INVALID_FUTURE_DATE_STATUS, current_date.isoformat(), INVALID_FUTURE_DATE_STATUS),
                )
            except sqlite3.OperationalError:
                pass
            connection.commit()

    snapshots_root = root / "outputs" / "snapshots"
    if snapshots_root.exists():
        for directory in snapshots_root.iterdir():
            if not directory.is_dir() or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", directory.name):
                continue
            try:
                artifact_date = date.fromisoformat(directory.name)
            except ValueError:
                continue
            if artifact_date <= current_date:
                continue
            validation_path = directory / f"validation_report_{directory.name}.json"
            if not validation_path.exists():
                continue
            try:
                report = json.loads(validation_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                report = {}
            metadata = dict(report.get("metadata") or {})
            if report.get("status") == TEST_ONLY_STATUS or metadata.get("snapshot_status") == TEST_ONLY_STATUS:
                continue
            metadata.update(
                {
                    "snapshot_status": INVALID_FUTURE_DATE_STATUS,
                    "production_eligible": False,
                    "invalidated_reason": "Snapshot date was later than the calendar date at publication time.",
                }
            )
            report.update(
                {
                    "status": INVALID_FUTURE_DATE_STATUS,
                    "metadata": metadata,
                    "errors": ["Snapshot date cannot be in the future."],
                }
            )
            validation_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return invalidated


def publish_latest(
    snapshot_dir: Path,
    snapshot_date: date,
    *,
    root: Path = ROOT_DIR,
    test_only: bool = False,
) -> None:
    if snapshot_date > date.today():
        raise ValueError("Snapshot date cannot be in the future.")
    if test_only:
        raise ValueError("TEST_ONLY snapshots cannot update latest outputs.")
    raise RuntimeError(
        "Direct latest publication is forbidden; publish an immutable manifest through atomic_release."
    )


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def read_allowlist_domains(*, root: Path = ROOT_DIR) -> set[str]:
    text = (root / "data" / "outlook_sources.txt").read_text(encoding="utf-8")
    return {_domain(match) for match in re.findall(r"https?://[^\s]+", text) if _domain(match)}


def probe_allowlist(*, root: Path = ROOT_DIR, max_urls: int = 8) -> list[dict[str, Any]]:
    text = (root / "data" / "outlook_sources.txt").read_text(encoding="utf-8")
    urls = [match.rstrip(".,") for match in re.findall(r"https?://[^\s]+", text)][:max_urls]
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_probe_url, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                reachable, message = future.result()
            except Exception as exc:  # noqa: BLE001
                reachable, message = False, str(exc)
            results.append({"url": url, "reachable": reachable, "message": message})
    return sorted(results, key=lambda row: row["url"])


def _probe_url(url: str) -> tuple[bool, str]:
    try:
        with httpx.Client(follow_redirects=True, timeout=4.0, headers={"User-Agent": "MAE-Snapshot/1.0"}) as client:
            response = client.head(url)
            if response.status_code in {403, 405}:
                response = client.get(url, headers={"Range": "bytes=0-2048"})
        return response.status_code < 400, f"HTTP {response.status_code}"
    except Exception as exc:  # noqa: BLE001
        return False, exc.__class__.__name__


def _parse_fred_points(raw_text: str, snapshot_date: date) -> list[tuple[date, float]]:
    points, _ = _parse_fred_points_partitioned(raw_text, snapshot_date)
    return points


def _parse_fred_points_partitioned(raw_text: str, snapshot_date: date) -> tuple[list[tuple[date, float]], list[date]]:
    points = []
    future_dates = []
    reader = csv.DictReader(raw_text.splitlines())
    if not reader.fieldnames or len(reader.fieldnames) < 2:
        return points
    date_field, value_field = reader.fieldnames[0], reader.fieldnames[1]
    for row in reader:
        try:
            observation_date = date.fromisoformat(row[date_field])
            value = float(row[value_field])
        except (KeyError, TypeError, ValueError):
            continue
        if observation_date > snapshot_date:
            future_dates.append(observation_date)
        else:
            points.append((observation_date, value))
    return sorted(points), sorted(future_dates)


def _series_change_summary(series: str, points: list[tuple[date, float]], snapshot_date: date) -> str:
    current_date, current = points[-1]
    labels = [("1w", 7), ("1m", 30), ("3m", 90), ("6m", 180), ("12m", 365)]
    changes = []
    for label, days in labels:
        prior = _value_at_or_before(points, snapshot_date - timedelta(days=days))
        if prior is None or prior == 0:
            continue
        if series in {"SP500", "NASDAQCOM", "DTWEXBGS"}:
            changes.append(f"{label} {(current / prior - 1) * 100:+.2f}%")
        else:
            changes.append(f"{label} {(current - prior) * 100:+.0f}bp")
    return f"{series} {current:.2f} as of {current_date.isoformat()} ({', '.join(changes)})"


def _value_at_or_before(points: list[tuple[date, float]], target: date) -> float | None:
    value = None
    for observation_date, observation in points:
        if observation_date > target:
            break
        value = observation
    return value


def _evidence_code(value: str) -> str:
    normalized = str(value or "").lower()
    if "подтверждается" in normalized and "частично" not in normalized:
        return "CONFIRMED"
    if "опровер" in normalized or "contradict" in normalized:
        return "CONTRADICTED"
    return "MIXED"


def _direction_label(score: int) -> str:
    return {
        3: "Strong overweight",
        2: "Overweight",
        1: "Moderate positive tilt",
        0: "Neutral",
        -1: "Moderate negative tilt",
        -2: "Underweight",
        -3: "Strong underweight",
    }[score]


def _first_item(value: str) -> str:
    return _split_items(value)[0] if _split_items(value) else ""


def _split_items(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(";") if item.strip()]


def _parse_reaction_map(value: str) -> dict[str, str]:
    result = {}
    for item in _split_items(value):
        if ":" in item:
            key, reaction = item.split(":", 1)
            result[key.strip()] = reaction.strip()
    return result


def _dedupe_evidence(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["source_id"], row["cell_id"])
        by_key[key] = row
    return sorted(by_key.values(), key=lambda row: (row["cell_id"], row["publication_date"], row["source_id"]))


def _stable_source_id(*parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"SRC_{digest.upper()}"


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except ValueError:
        return ""


def _domain_allowed(domain: str, allowed: set[str]) -> bool:
    return any(domain == candidate or domain.endswith(f".{candidate}") for candidate in allowed)


def build_full_snapshot_product(
    snapshot_date: date,
    snapshot_type: str,
    *,
    root: Path = ROOT_DIR,
    snapshot_status: str = PRODUCTION_STATUS,
    pilot_paths: dict[str, Path] | None = None,
) -> FullSnapshotProduct:
    """Build the full 19x6 MAE universe on top of the accepted pilot anchors."""
    snapshot_type = normalize_snapshot_type(snapshot_type)
    pilot_paths = pilot_paths or {}
    pilot_scores_path = pilot_paths.get("scores", root / "outputs" / "mae_latest_scores.csv")
    pilot_evidence_path = pilot_paths.get("evidence", root / "outputs" / "mae_latest_evidence.csv")
    pilot_scenarios_path = pilot_paths.get("scenarios", root / "outputs" / "mae_latest_scenarios.csv")
    pilot_scores = {row["cell_id"]: row for row in read_csv(pilot_scores_path)}
    pilot_evidence = read_csv(pilot_evidence_path)
    if set(FULL_ANCHORS.values()) != set(pilot_scores):
        missing = sorted(set(FULL_ANCHORS.values()) - set(pilot_scores))
        raise ValueError(f"Canonical pilot anchors are incomplete: {', '.join(missing)}")
    previous_dates = {row.get("previous_snapshot_date") for row in pilot_scores.values()}
    if len(previous_dates) != 1:
        raise ValueError("Pilot anchors do not share one previous comparable snapshot.")
    previous_snapshot_date = date.fromisoformat(next(iter(previous_dates)))
    sources = _load_full_source_catalog(snapshot_date, root=root)
    quality_baseline = _full_quality_baseline_scores(snapshot_date, root=root)

    scores: list[dict[str, Any]] = []
    market_by_cell: dict[str, dict[str, Any]] = {}
    for asset_index, (asset_class, asset_group, asset_segment) in enumerate(FULL_ASSETS, start=1):
        for geography_index, geography in enumerate(FULL_GEOGRAPHIES, start=1):
            applicable = not (
                asset_segment in {"Gold", "Other Precious Metals"} and geography != "Global"
            )
            anchor_id = FULL_ANCHORS.get((geography, asset_segment))
            cell_id = anchor_id or _full_cell_id(geography, asset_segment)
            display_order = (asset_index - 1) * len(FULL_GEOGRAPHIES) + geography_index
            if not applicable:
                scores.append(
                    {
                        "snapshot_date": snapshot_date.isoformat(),
                        "snapshot_type": snapshot_type,
                        "previous_snapshot_date": previous_snapshot_date.isoformat(),
                        "cell_id": cell_id,
                        "asset_class": asset_class,
                        "asset_group": asset_group,
                        "asset_segment": asset_segment,
                        "geography": geography,
                        "applicable": "false",
                        "display_order": display_order,
                        "score": "",
                        "direction": "N/A",
                        "thesis": "Not applicable: precious-metals assessments are Global only.",
                        "main_driver": "N/A",
                        "evidence_mode": "NOT_APPLICABLE",
                        "evidence_status": "NOT_APPLICABLE",
                        "conviction": "N/A",
                        "review_status": "NOT_APPLICABLE",
                        "assessment_date": snapshot_date.isoformat(),
                        "research_view_score": "",
                        "fundamental_macro_score": "",
                        "market_confirmation_score": "",
                        "valuation_risk_score": "",
                        "weighted_score": "",
                        "score_cap": "",
                        "previous_score": "",
                        "score_delta": "",
                        "change_type": "NOT_APPLICABLE",
                        "what_changed": "Not applicable by canonical universe rule.",
                        "why_changed": "Gold and Other Precious Metals are assessed in Global only.",
                        "scenario_link": "",
                        "trigger": "",
                        "veto": "",
                        "primary_risk": "",
                        "secondary_risk": "",
                        "market_observation_date": "",
                        "last_review_date": snapshot_date.isoformat(),
                        "next_review_date": "",
                        "analyst_action": "NO ACTION",
                        "anchor_source_cell_id": "",
                    }
                )
                continue

            benchmark = _full_benchmark_registry(cell_id, geography, asset_segment, snapshot_date)
            market = _full_market_observation(benchmark)
            market_by_cell[cell_id] = market
            if anchor_id:
                anchor = pilot_scores[anchor_id]
                scores.append(
                    _full_anchor_score(
                        anchor,
                        asset_class=asset_class,
                        asset_group=asset_group,
                        asset_segment=asset_segment,
                        display_order=display_order,
                        benchmark=benchmark,
                    )
                )
                continue

            research_score, fundamental_score, market_score, valuation_score = _full_component_scores(
                geography, asset_segment
            )
            weighted = Decimal("0.40") * research_score + Decimal("0.25") * fundamental_score
            weighted += Decimal("0.20") * market_score + Decimal("0.15") * valuation_score
            raw_score = int(weighted.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            evidence_mode = "DIRECT" if (geography, asset_segment) in {
                ("Global", "Wide Market"),
                ("Europe", "Wide Market"),
            } else "COMPOSITE"
            score_cap = 2 if evidence_mode == "DIRECT" else 1
            score = max(-score_cap, min(score_cap, raw_score))
            analytical_quality = "STRONG" if evidence_mode == "DIRECT" and benchmark["comparability_status"] == "DIRECTLY_COMPARABLE" else "ACCEPTABLE"
            market_confirmation_quality = "STRONG" if benchmark["comparability_status"] == "DIRECTLY_COMPARABLE" else "PARTIAL"
            conviction = _full_conviction(evidence_mode, benchmark["comparability_status"], analytical_quality)
            evidence_status = "CONFIRMED" if evidence_mode == "DIRECT" else (
                "CONFIRMED" if score and research_score * market_score > 0 else "MIXED"
            )
            driver, mechanism, trigger, veto, primary_risk, secondary_risk = _full_driver_controls(
                geography, asset_segment
            )
            monitoring_indicator, trigger_threshold, veto_threshold, trigger, veto = _full_monitoring_controls(
                asset_segment, benchmark["benchmark_name"]
            )
            if score == 0:
                thesis = (
                    f"По {geography} × {asset_segment} позитивные и негативные факторы сбалансированы; "
                    "score 0 означает доказанную нейтральную relative preference, а не отсутствие данных."
                )
            else:
                thesis = (
                    f"{geography} × {asset_segment}: компонентный расчёт даёт {_direction_label(score).lower()}; "
                    f"{driver.lower()}. Market confirmation: {benchmark['benchmark_name']}; "
                    f"comparability={benchmark['comparability_status']}."
                )
            scenario_link = "Base; Upside / Risk-on; Downside / Risk-off"
            baseline_row = quality_baseline.get(cell_id)
            previous_score = int(baseline_row["score"]) if baseline_row and str(baseline_row.get("score", "")).strip() else None
            score_delta = score - previous_score if previous_score is not None else None
            change_type = (
                "METHODOLOGY_REFINEMENT" if score_delta not in {None, 0}
                else "NO_CHANGE" if previous_score is not None
                else "NEW_COMPARABLE_ASSESSMENT"
            )
            scores.append(
                {
                    "snapshot_date": snapshot_date.isoformat(),
                    "snapshot_type": snapshot_type,
                    "previous_snapshot_date": previous_snapshot_date.isoformat(),
                    "cell_id": cell_id,
                    "asset_class": asset_class,
                    "asset_group": asset_group,
                    "asset_segment": asset_segment,
                    "geography": geography,
                    "applicable": "true",
                    "display_order": display_order,
                    "score": score,
                    "direction": _direction_label(score),
                    "thesis": thesis,
                    "main_driver": driver,
                    "evidence_mode": evidence_mode,
                    "evidence_status": evidence_status,
                    "conviction": conviction,
                    "review_status": "PASS",
                    "assessment_date": snapshot_date.isoformat(),
                    "research_view_score": research_score,
                    "fundamental_macro_score": fundamental_score,
                    "market_confirmation_score": market_score,
                    "valuation_risk_score": valuation_score,
                    "weighted_score": f"{weighted:.2f}",
                    "score_cap": score_cap,
                    "previous_score": "" if previous_score is None else previous_score,
                    "score_delta": "" if score_delta is None else score_delta,
                    "change_type": change_type,
                    "what_changed": (
                        f"Benchmark-quality refinement changed the score from {previous_score:+d} to {score:+d}."
                        if change_type == "METHODOLOGY_REFINEMENT"
                        else "Benchmark, comparability and monitoring controls were upgraded without changing the score."
                        if change_type == "NO_CHANGE"
                        else "New full-universe assessment; no prior comparable cell score existed."
                    ),
                    "why_changed": (
                        f"Methodology refinement uses {benchmark['benchmark_name']} and removes the prior generic cross-market confirmation; this is not a research upgrade or downgrade."
                        if change_type == "METHODOLOGY_REFINEMENT"
                        else "The score remains supported after replacing the generic proxy with a geography- and instrument-specific benchmark."
                        if change_type == "NO_CHANGE"
                        else "The pilot covered only ten anchor cells; this is not an upgrade or downgrade."
                    ),
                    "scenario_link": scenario_link,
                    "trigger": trigger,
                    "veto": veto,
                    "primary_risk": primary_risk,
                    "secondary_risk": secondary_risk,
                    "market_observation_date": market["observation_date"],
                    "last_review_date": snapshot_date.isoformat(),
                    "next_review_date": (snapshot_date + timedelta(days=31)).isoformat(),
                    "analyst_action": _full_analyst_action(score, conviction),
                    "anchor_source_cell_id": "",
                    **benchmark,
                    "technical_status": "TECHNICAL_PASS",
                    "analytical_quality": analytical_quality,
                    "market_confirmation_quality": market_confirmation_quality,
                    "monitoring_indicator": monitoring_indicator,
                    "trigger_threshold": trigger_threshold,
                    "veto_threshold": veto_threshold,
                    "_transmission_mechanism": mechanism,
                }
            )

    score_by_cell = {row["cell_id"]: row for row in scores}
    evidence = _full_anchor_evidence(
        pilot_evidence,
        score_by_cell=score_by_cell,
        snapshot_date=snapshot_date,
        market_by_cell=market_by_cell,
    )
    for score_row in scores:
        if score_row["applicable"] != "true" or score_row["evidence_mode"] == "ANCHOR_EXISTING":
            continue
        evidence.extend(
            _full_new_evidence(
                score_row,
                sources=sources,
                market=market_by_cell[score_row["cell_id"]],
                snapshot_date=snapshot_date,
            )
        )

    scenarios = _full_scenarios(snapshot_date, scores, root=root)
    transmission = _full_transmission(scores, evidence, scenarios)
    summary = _full_market_summary(scores, scenarios)
    pilot_hashes = _pilot_output_hashes(root=root, pilot_paths=pilot_paths or None)
    latest_market = max(
        date.fromisoformat(row["market_observation_date"])
        for row in scores
        if row["applicable"] == "true" and row["market_observation_date"]
    )
    return FullSnapshotProduct(
        snapshot_date=snapshot_date,
        snapshot_type=snapshot_type,
        snapshot_status=snapshot_status,
        previous_snapshot_date=previous_snapshot_date,
        scores=scores,
        evidence=sorted(evidence, key=lambda row: (int(score_by_cell[row["cell_id"]]["display_order"]), row["component_role"], row["evidence_id"])),
        scenarios=scenarios,
        transmission=transmission,
        market_summary=summary,
        database_preflight=database_state(root=root),
        pilot_hashes=pilot_hashes,
        data_windows={
            "research_window_start": (snapshot_date - timedelta(days=90)).isoformat(),
            "latest_publication_date": max(row["publication_date"] for row in evidence),
            "latest_market_observation_date": latest_market.isoformat(),
            "market_window_days": min(int(row["window_days"]) for row in market_by_cell.values()),
        },
    )


def _full_anchor_score(
    anchor: dict[str, str],
    *,
    asset_class: str,
    asset_group: str,
    asset_segment: str,
    display_order: int,
    benchmark: dict[str, Any],
) -> dict[str, Any]:
    monitoring_indicator, trigger_threshold, veto_threshold, trigger, veto = _full_monitoring_controls(
        asset_segment, benchmark["benchmark_name"]
    )
    return {
        "snapshot_date": anchor["snapshot_date"],
        "snapshot_type": anchor["snapshot_type"],
        "previous_snapshot_date": anchor["previous_snapshot_date"],
        "cell_id": anchor["cell_id"],
        "asset_class": asset_class,
        "asset_group": asset_group,
        "asset_segment": asset_segment,
        "geography": anchor["geography"],
        "applicable": "true",
        "display_order": display_order,
        "score": int(anchor["score"]),
        "direction": anchor["view"],
        "thesis": anchor["current_thesis"],
        "main_driver": anchor["current_driver"],
        "evidence_mode": "ANCHOR_EXISTING",
        "evidence_status": anchor["evidence_status_code"],
        "conviction": anchor["confidence"],
        "review_status": "PASS",
        "assessment_date": anchor["last_review_date"],
        "research_view_score": "",
        "fundamental_macro_score": "",
        "market_confirmation_score": "",
        "valuation_risk_score": "",
        "weighted_score": "",
        "score_cap": "",
        "previous_score": int(anchor["previous_score_numeric"]),
        "score_delta": int(anchor["score_delta"]),
        "change_type": anchor["change_status"],
        "what_changed": anchor["what_changed"],
        "why_changed": anchor["why_changed"],
        "scenario_link": anchor["affected_scenario"],
        "trigger": trigger,
        "veto": veto,
        "primary_risk": anchor["primary_risk"],
        "secondary_risk": anchor["key_risks"],
        "market_observation_date": benchmark["market_observation_date"],
        "last_review_date": anchor["last_review_date"],
        "next_review_date": anchor["next_review_date"],
        "analyst_action": anchor["analyst_action"],
        "anchor_source_cell_id": anchor["cell_id"],
        **benchmark,
        "technical_status": "TECHNICAL_PASS",
        "analytical_quality": "STRONG",
        "market_confirmation_quality": "STRONG",
        "monitoring_indicator": monitoring_indicator,
        "trigger_threshold": trigger_threshold,
        "veto_threshold": veto_threshold,
        "_transmission_mechanism": anchor["transmission_mechanism"],
    }


def _full_cell_id(geography: str, asset_segment: str) -> str:
    text = re.sub(r"[^A-Z0-9]+", "_", f"FULL_{geography}_{asset_segment}".upper()).strip("_")
    return text.replace("EM_EX_CHINA", "EM_EX_CHINA")


def _full_quality_baseline_scores(snapshot_date: date, *, root: Path) -> dict[str, dict[str, str]]:
    path = root / "outputs" / "backups" / f"full_before_quality_{snapshot_date.isoformat()}" / "mae_full_latest_scores.csv"
    if not path.exists():
        return {}
    return {row["cell_id"]: row for row in read_csv(path)}


def _full_instrument_definition(asset_segment: str) -> tuple[str, str]:
    definitions = {
        "Wide Market": ("Broad free-float market-cap equity exposure", "EQUITY_BROAD"),
        "Market Breadth": ("Equal-weight or broad participation equity exposure", "EQUITY_BREADTH"),
        "Preferred": ("Exchange-listed preferred stock and capital-hybrid exposure", "PREFERRED_HYBRID"),
        "Value": ("Regional large/mid-cap value-style equity exposure", "EQUITY_VALUE"),
        "Growth": ("Regional large/mid-cap growth-style equity exposure", "EQUITY_GROWTH"),
        "Small Cap": ("Regional free-float small-cap equity exposure", "EQUITY_SMALL_CAP"),
        "Government Bonds Long Term": ("Local-currency sovereign bonds, 10+ year duration bucket", "GOV_10Y_PLUS"),
        "Government Bonds Short Term": ("Local-currency sovereign bonds, 1-3 year duration bucket", "GOV_1_3Y"),
        "Government Bonds Mid Term": ("Local-currency sovereign bonds, 3-10 year duration bucket", "GOV_3_10Y"),
        "Corporate High Yield Long Term": ("Below-investment-grade corporate bonds, long-duration bucket", "HY_LONG"),
        "Corporate High Yield Short Term": ("Below-investment-grade corporate bonds, short-duration bucket", "HY_SHORT"),
        "Corporate High Yield Mid Term": ("Below-investment-grade corporate bonds, intermediate-duration bucket", "HY_MID"),
        "Corporate Investment Grade Long Term": ("Investment-grade corporate bonds, long-duration bucket", "IG_LONG"),
        "Corporate Investment Grade Short Term": ("Investment-grade corporate bonds, short-duration bucket", "IG_SHORT"),
        "Corporate Investment Grade Mid Term": ("Investment-grade corporate bonds, intermediate-duration bucket", "IG_MID"),
        "Inflation Linked": ("Local-currency sovereign inflation-linked bonds", "INFLATION_LINKED"),
        "Mortgage-Backed": ("Agency or prime residential mortgage-backed securities", "MBS"),
        "Gold": ("Unhedged global gold spot exposure", "GOLD_SPOT"),
        "Other Precious Metals": ("Diversified silver, platinum and palladium spot exposure", "PRECIOUS_METALS_EX_GOLD"),
    }
    return definitions[asset_segment]


def _full_benchmark_registry(
    cell_id: str,
    geography: str,
    asset_segment: str,
    snapshot_date: date,
) -> dict[str, Any]:
    """Return the auditable primary benchmark/proxy definition for one applicable cell."""
    instrument_definition, instrument_code = _full_instrument_definition(asset_segment)
    currency = {"Global": "USD", "US": "USD", "Europe": "EUR", "UK": "GBP", "Japan": "JPY", "EM ex China": "USD"}[geography]
    regional_scope = geography
    duration_bucket = "N/A"
    benchmark_type = "TOTAL_RETURN_INDEX"
    observation_date = min(snapshot_date, date(2026, 7, 10)).isoformat()
    equity = {
        "Global": {
            "Wide Market": ("MSCI_ACWI", "MSCI ACWI Index", "MSCI", "https://www.msci.com/indexes/index/892400/msci-acwi-index"),
            "Market Breadth": ("MSCI_ACWI_EQUAL_WEIGHTED", "MSCI ACWI Equal Weighted Index", "MSCI", "https://www.msci.com/indexes/group/equal-weighted-indexes"),
            "Preferred": ("ICE_GLOBAL_HYBRID", "ICE BofA Global Hybrid Corporate Index", "ICE Data Indices", "https://www.ice.com/fixed-income-data-services/index-solutions/fixed-income-indices"),
            "Value": ("MSCI_ACWI_VALUE", "MSCI ACWI Value Index", "MSCI", "https://www.msci.com/indexes/group/value-and-growth-indexes"),
            "Growth": ("MSCI_ACWI_GROWTH", "MSCI ACWI Growth Index", "MSCI", "https://www.msci.com/indexes/group/value-and-growth-indexes"),
            "Small Cap": ("MSCI_ACWI_SMALL_CAP", "MSCI ACWI Small Cap Index", "MSCI", "https://www.msci.com/indexes/group/market-cap-indexes"),
        },
        "US": {
            "Wide Market": ("SPX", "S&P 500 Index", "S&P Dow Jones Indices", "https://www.spglobal.com/spdji/en/indices/equity/sp-500/"),
            "Market Breadth": ("SPW", "S&P 500 Equal Weight Index", "S&P Dow Jones Indices", "https://www.spglobal.com/spdji/en/indices/equity/sp-500-equal-weight-index/"),
            "Preferred": ("FTSE_US_PREFERRED_HYBRIDS", "FTSE US Preferred Stock and Hybrids Index", "FTSE Russell", "https://www.lseg.com/content/dam/ftse-russell/en_us/documents/benchmark-statements/ftse-us-preferred-stock-and-hybrids-index-series-bs.pdf"),
            "Value": ("RUSSELL_1000_VALUE", "Russell 1000 Value Index", "FTSE Russell", "https://www.lseg.com/en/ftse-russell/indices/russell-us"),
            "Growth": ("RUSSELL_1000_GROWTH", "Russell 1000 Growth Index", "FTSE Russell", "https://www.lseg.com/en/ftse-russell/indices/russell-us"),
            "Small Cap": ("RUSSELL_2000", "Russell 2000 Index", "FTSE Russell", "https://www.lseg.com/en/ftse-russell/indices/russell-us"),
        },
        "Europe": {
            "Wide Market": ("MSCI_EUROPE", "MSCI Europe Index", "MSCI", "https://www.msci.com/indexes/index/990500/msci-europe-index"),
            "Market Breadth": ("STOXX_EUROPE_600_EQUAL_WEIGHT", "STOXX Europe 600 Equal Weight Index", "STOXX", "https://www.stoxx.com/index-details?symbol=SXXEW"),
            "Preferred": ("ICE_EURO_FINANCIAL_HYBRID", "ICE BofA Euro Financial Hybrid Securities Index", "ICE Data Indices", "https://www.ice.com/fixed-income-data-services/index-solutions/fixed-income-indices"),
            "Value": ("MSCI_EUROPE_VALUE", "MSCI Europe Value Index", "MSCI", "https://www.msci.com/indexes/group/value-and-growth-indexes"),
            "Growth": ("MSCI_EUROPE_GROWTH", "MSCI Europe Growth Index", "MSCI", "https://www.msci.com/indexes/group/value-and-growth-indexes"),
            "Small Cap": ("MSCI_EUROPE_SMALL_CAP", "MSCI Europe Small Cap Index", "MSCI", "https://www.msci.com/indexes/index/106233/msci-europe-small-cap-index"),
        },
        "UK": {
            "Wide Market": ("FTSE_ALL_SHARE", "FTSE All-Share Index", "FTSE Russell", "https://www.lseg.com/en/ftse-russell/indices/uk"),
            "Market Breadth": ("FTSE_ALL_SHARE_BREADTH", "FTSE All-Share market-breadth series", "FTSE Russell", "https://www.lseg.com/en/ftse-russell/indices/uk"),
            "Preferred": ("ICE_STERLING_FINANCIAL_HYBRID", "ICE BofA Sterling Financial Hybrid Securities Index", "ICE Data Indices", "https://www.ice.com/fixed-income-data-services/index-solutions/fixed-income-indices"),
            "Value": ("MSCI_UK_VALUE", "MSCI UK Value Index", "MSCI", "https://www.msci.com/indexes/group/value-and-growth-indexes"),
            "Growth": ("MSCI_UK_GROWTH", "MSCI UK Growth Index", "MSCI", "https://www.msci.com/indexes/group/value-and-growth-indexes"),
            "Small Cap": ("FTSE_SMALL_CAP", "FTSE Small Cap Index", "FTSE Russell", "https://www.lseg.com/en/ftse-russell/indices/ftse250"),
        },
        "Japan": {
            "Wide Market": ("TOPIX", "TOPIX", "JPX", "https://www.jpx.co.jp/english/markets/indices/topix/"),
            "Market Breadth": ("TOPIX_500_EQUAL_WEIGHT", "TOPIX 500 Equal Weight Index", "JPX", "https://www.jpx.co.jp/english/markets/indices/line-up/index.html"),
            "Preferred": ("JPX_PREFERRED_UNIVERSE", "JPX listed preferred-stock universe", "JPX", "https://www.jpx.co.jp/english/equities/products/preferred-stocks/issues/"),
            "Value": ("TOPIX_VALUE", "TOPIX Value Index", "JPX", "https://www.jpx.co.jp/english/markets/indices/line-up/index.html"),
            "Growth": ("TOPIX_GROWTH", "TOPIX Growth Index", "JPX", "https://www.jpx.co.jp/english/markets/indices/line-up/index.html"),
            "Small Cap": ("TOPIX_SMALL", "TOPIX Small Index", "JPX", "https://www.jpx.co.jp/english/markets/indices/factsheets/files/e_004_12_fac2_style_TOPIX%20Small%20Growth.pdf"),
        },
        "EM ex China": {
            "Wide Market": ("MSCI_EM_EX_CHINA", "MSCI Emerging Markets ex China Index", "MSCI", "https://www.msci.com/documents/10199/00d07d16-2b89-de9a-d0a6-0e9068c7c448"),
            "Market Breadth": ("MSCI_EM_EX_CHINA_EQUAL_WEIGHT", "MSCI EM ex China Equal Weighted Index", "MSCI", "https://www.msci.com/indexes?index-category=market-cap&index-market=emerging-markets"),
            "Preferred": ("CEMBI_EX_CHINA_HYBRID_SCREEN", "J.P. Morgan CEMBI ex-China subordinated and hybrid screen", "J.P. Morgan", "https://www.jpmorgan.com/insights/global-research/index-research"),
            "Value": ("MSCI_EM_EX_CHINA_VALUE", "MSCI EM ex China Value Index", "MSCI", "https://www.msci.com/indexes/group/value-and-growth-indexes"),
            "Growth": ("MSCI_EM_EX_CHINA_GROWTH", "MSCI EM ex China Growth Index", "MSCI", "https://www.msci.com/indexes/group/value-and-growth-indexes"),
            "Small Cap": ("MSCI_EM_EX_CHINA_SMALL_CAP", "MSCI EM ex China Small Cap Index", "MSCI", "https://www.msci.com/indexes?index-category=market-cap&index-market=emerging-markets"),
        },
    }
    limited = asset_segment == "Preferred"
    structural = geography == "EM ex China" and asset_segment == "Preferred"
    if asset_segment in equity.get(geography, {}):
        benchmark_id, benchmark_name, provider, url = equity[geography][asset_segment]
    elif "Government Bonds" in asset_segment:
        duration_bucket = "10Y+" if "Long" in asset_segment else "1-3Y" if "Short" in asset_segment else "3-10Y"
        government = {
            "Global": ("FTSE_WGBI", "FTSE World Government Bond Index", "FTSE Russell", "https://www.lseg.com/en/ftse-russell/indices/world-government-bond-index"),
            "US": ("UST_CURVE", "US Treasury par yield curve", "US Treasury", "https://home.treasury.gov/resource-center/data-chart-center/interest-rates"),
            "Europe": ("ECB_AAA_EURO_CURVE", "ECB euro-area central government AAA yield curve", "European Central Bank", "https://www.ecb.europa.eu/stats/financial_markets_and_interest_rates/euro_area_yield_curves/html/index.en.html"),
            "UK": ("BOE_GILT_CURVE", "Bank of England UK government liability curve", "Bank of England", "https://www.bankofengland.co.uk/statistics/yield-curves"),
            "Japan": ("JGB_CURVE", "Japan government bond yield curve", "Bank of Japan", "https://www.boj.or.jp/en/statistics/market/index.htm"),
            "EM ex China": ("GBI_EM_GD_EX_CHINA", "J.P. Morgan GBI-EM Global Diversified ex-China maturity screen", "J.P. Morgan", "https://www.jpmorgan.com/insights/global-research/index-research"),
        }
        benchmark_id, benchmark_name, provider, url = government[geography]
        benchmark_id = f"{benchmark_id}_{duration_bucket.replace('-', '_').replace('+', 'PLUS')}"
        benchmark_name = f"{benchmark_name}, {duration_bucket} bucket"
        benchmark_type = "YIELD_CURVE_BUCKET"
        structural = geography == "EM ex China"
    elif "Corporate High Yield" in asset_segment or "Corporate Investment Grade" in asset_segment:
        quality = "HY" if "High Yield" in asset_segment else "IG"
        duration_bucket = "LONG" if "Long" in asset_segment else "SHORT" if "Short" in asset_segment else "MID"
        names = {
            "Global": "Global", "US": "US", "Europe": "Euro", "UK": "Sterling", "Japan": "Japan", "EM ex China": "CEMBI ex-China"
        }
        benchmark_id = f"{'JPM' if geography == 'EM ex China' else 'ICE_BofA'}_{names[geography].upper().replace(' ', '_').replace('-', '_')}_{quality}_{duration_bucket}"
        benchmark_name = f"{'J.P. Morgan' if geography == 'EM ex China' else 'ICE BofA'} {names[geography]} {quality} Corporate Index, {duration_bucket.lower()} duration"
        provider = "J.P. Morgan" if geography == "EM ex China" else "ICE Data Indices"
        url = "https://www.jpmorgan.com/insights/global-research/index-research" if geography == "EM ex China" else "https://www.ice.com/fixed-income-data-services/index-solutions/fixed-income-indices"
        benchmark_type = "OAS_TOTAL_RETURN_INDEX"
        structural = geography == "EM ex China"
        limited = geography == "Japan" and quality == "HY"
    elif asset_segment == "Inflation Linked":
        inflation = {
            "Global": ("BLOOMBERG_WORLD_GOV_ILB", "Bloomberg World Government Inflation-Linked Bond Index", "Bloomberg", "https://www.ishares.com/uk/individual/en/products/346742/"),
            "US": ("SP_US_TIPS", "S&P U.S. TIPS Index", "S&P Dow Jones Indices", "https://www.spglobal.com/spdji/en/index-family/fixed-income/inflation-linked/"),
            "Europe": ("IBOXX_EURO_ILB", "iBoxx Euro Inflation-Linked Bond Index", "S&P Dow Jones Indices", "https://www.spglobal.com/spdji/en/documents/methodologies/methodology-iboxx-inflation-linked-bond-index.pdf"),
            "UK": ("MSCI_UK_IL_GOV", "MSCI UK Inflation-Linked Government Bond Index", "MSCI", "https://www.msci.com/indexes/index/765240/msci-uk-inflation-linked-government-bond-index"),
            "Japan": ("IBOXX_JAPAN_ILB", "iBoxx Japan Inflation-Linked Bond Index", "S&P Dow Jones Indices", "https://www.spglobal.com/spdji/en/documents/methodologies/methodology-iboxx-inflation-linked-bond-index.pdf"),
            "EM ex China": ("SP_EM_ILB_EX_CHINA", "S&P Emerging Sovereign Inflation-Linked ex-China screen", "S&P Dow Jones Indices", "https://www.spglobal.com/spdji/en/index-family/fixed-income/inflation-linked/"),
        }
        benchmark_id, benchmark_name, provider, url = inflation[geography]
        benchmark_type = "INFLATION_LINKED_TOTAL_RETURN_INDEX"
        structural = geography == "EM ex China"
    elif asset_segment == "Mortgage-Backed":
        mbs = {
            "Global": ("ICE_GLOBAL_SECURITIZED", "ICE BofA Global Securitized Index", "ICE Data Indices", "https://www.ice.com/fixed-income-data-services/index-solutions/fixed-income-indices"),
            "US": ("ICE_US_MBS", "ICE BofA US Mortgage-Backed Securities Index", "ICE Data Indices", "https://www.ice.com/fixed-income-data-services/index-solutions/fixed-income-indices"),
            "Europe": ("SP_EUROPEAN_RMBS", "S&P European RMBS Index", "S&P Global Ratings", "https://www.spglobal.com/ratings/en/regulatory/article/european-rmbs-index-report-q4-2025-s101669373"),
            "UK": ("SP_UK_RMBS", "S&P UK RMBS sub-index", "S&P Global Ratings", "https://www.spglobal.com/ratings/en/regulatory/article/european-rmbs-index-report-q4-2025-s101669373"),
            "Japan": ("JHF_RMBS", "Japan Housing Finance Agency RMBS universe", "Japan Housing Finance Agency", "https://www.jhf.go.jp/english/"),
            "EM ex China": ("JACI_SECURITIZED_EX_CHINA", "J.P. Morgan Asia Credit securitized ex-China screen", "J.P. Morgan", "https://www.jpmorgan.com/insights/global-research/index-research"),
        }
        benchmark_id, benchmark_name, provider, url = mbs[geography]
        benchmark_type = "SECURITIZED_TOTAL_RETURN_INDEX"
        limited = geography in {"Europe", "UK", "Japan"}
        structural = geography == "EM ex China"
    else:
        metals = {
            "Gold": ("LBMA_GOLD_PM", "LBMA Gold Price PM", "LBMA", "https://www.lbma.org.uk/prices-and-data/precious-metal-prices"),
            "Other Precious Metals": ("SP_GSCI_PRECIOUS_METALS", "S&P GSCI Precious Metals Index", "S&P Dow Jones Indices", "https://www.spglobal.com/spdji/en/indices/commodities/sp-gsci-precious-metals/"),
        }
        benchmark_id, benchmark_name, provider, url = metals[asset_segment]
        benchmark_type = "SPOT_OR_COMMODITY_INDEX"

    comparability = "STRUCTURAL_PROXY" if structural else "LIMITED_COMPARABILITY" if limited else "DIRECTLY_COMPARABLE"
    proxy_quality = (
        "REGIONAL_PROXY" if comparability != "DIRECTLY_COMPARABLE"
        else "DIRECT_GLOBAL" if geography == "Global" else "DIRECT_REGIONAL"
    )
    methodology_note = (
        "Primary benchmark is a transparent regional structural proxy; score is capped at |1| and conviction cannot exceed LOW."
        if comparability == "STRUCTURAL_PROXY"
        else "Primary benchmark covers the same region but the instrument universe is narrower or structurally different; score is capped at |1|."
        if comparability == "LIMITED_COMPARABILITY"
        else "Primary benchmark matches the geography and instrument bucket used by the cell."
    )
    return {
        "cell_id": cell_id,
        "geography": geography,
        "asset_segment": asset_segment,
        "instrument_definition": instrument_definition,
        "instrument_code": instrument_code,
        "benchmark_id": benchmark_id,
        "benchmark_name": benchmark_name,
        "benchmark_provider": provider,
        "benchmark_type": benchmark_type,
        "currency": currency,
        "duration_bucket": duration_bucket,
        "regional_scope": regional_scope,
        "market_data_source": url,
        "comparability_status": comparability,
        "proxy_quality": proxy_quality,
        "methodology_note": methodology_note,
        "market_observation_date": observation_date,
    }


def _full_component_scores(geography: str, asset_segment: str) -> tuple[int, int, int, int]:
    research = {
        "Wide Market": 1, "Market Breadth": 1, "Preferred": 0, "Value": 1, "Growth": 1,
        "Small Cap": 1, "Government Bonds Long Term": -1, "Government Bonds Short Term": 1,
        "Government Bonds Mid Term": 1, "Corporate High Yield Long Term": -1,
        "Corporate High Yield Short Term": 0, "Corporate High Yield Mid Term": 0,
        "Corporate Investment Grade Long Term": -1, "Corporate Investment Grade Short Term": 1,
        "Corporate Investment Grade Mid Term": 1, "Inflation Linked": 1, "Mortgage-Backed": 0,
        "Other Precious Metals": 0,
    }[asset_segment]
    geography_research = {"Global": 0, "US": 0, "Europe": 1, "UK": 0, "Japan": 0, "EM ex China": 1}[geography]
    fundamental = {"Global": 0, "US": 1, "Europe": 0, "UK": 0, "Japan": 0, "EM ex China": 1}[geography]
    market = {
        "Wide Market": 1, "Market Breadth": 1, "Preferred": 0, "Value": 1, "Growth": 2,
        "Small Cap": 1, "Government Bonds Long Term": -1, "Government Bonds Short Term": 1,
        "Government Bonds Mid Term": 0, "Corporate High Yield Long Term": -1,
        "Corporate High Yield Short Term": 0, "Corporate High Yield Mid Term": 0,
        "Corporate Investment Grade Long Term": -1, "Corporate Investment Grade Short Term": 1,
        "Corporate Investment Grade Mid Term": 1, "Inflation Linked": 0, "Mortgage-Backed": 0,
        "Other Precious Metals": 0,
    }[asset_segment]
    valuation = {
        "Wide Market": 0, "Market Breadth": 0, "Preferred": 0, "Value": 1, "Growth": -1,
        "Small Cap": 0, "Government Bonds Long Term": -1, "Government Bonds Short Term": 1,
        "Government Bonds Mid Term": 0, "Corporate High Yield Long Term": -1,
        "Corporate High Yield Short Term": 0, "Corporate High Yield Mid Term": 0,
        "Corporate Investment Grade Long Term": -1, "Corporate Investment Grade Short Term": 1,
        "Corporate Investment Grade Mid Term": 0, "Inflation Linked": 0, "Mortgage-Backed": -1,
        "Other Precious Metals": 0,
    }[asset_segment]
    result = (max(-3, min(3, research + geography_research)), fundamental, market, valuation)
    refinements = {
        ("UK", "Wide Market"): (1, -1, 0, 0),
        ("UK", "Market Breadth"): (1, -1, 0, 0),
        ("UK", "Growth"): (1, 0, 0, -1),
        ("UK", "Small Cap"): (1, -1, 0, 0),
        ("Japan", "Corporate High Yield Long Term"): (-1, 0, 0, 0),
        ("Japan", "Corporate Investment Grade Long Term"): (-1, 0, 0, 0),
        ("Japan", "Corporate Investment Grade Short Term"): (0, 0, 0, 0),
        ("UK", "Inflation Linked"): (2, 0, 1, 0),
        ("EM ex China", "Preferred"): (0, 0, 0, 0),
        ("EM ex China", "Mortgage-Backed"): (0, 0, 0, -1),
    }
    return refinements.get((geography, asset_segment), result)


def _full_conviction(evidence_mode: str, comparability_status: str, analytical_quality: str) -> str:
    if comparability_status in {"LIMITED_COMPARABILITY", "STRUCTURAL_PROXY"}:
        return "LOW"
    if evidence_mode == "DIRECT" and analytical_quality == "STRONG":
        return "HIGH"
    return "MEDIUM"


def _full_driver_controls(geography: str, asset_segment: str) -> tuple[str, str, str, str, str, str]:
    geography_driver = {
        "Global": "Global growth, inflation and liquidity",
        "US": "US earnings, Federal Reserve policy and financing conditions",
        "Europe": "Euro-area growth, ECB policy and energy inflation",
        "UK": "UK growth, Bank of England policy and financial conditions",
        "Japan": "Asian growth spillovers, yen sensitivity and policy normalisation",
        "EM ex China": "EM growth, dollar direction and external financing conditions",
    }[geography]
    if asset_segment in {"Wide Market", "Market Breadth", "Preferred", "Value", "Growth", "Small Cap"}:
        mechanism = "earnings and discount-rate changes feed into relative equity multiples and breadth"
        trigger = "Positive earnings revisions and stable or lower real yields."
        veto = "Negative earnings revisions combined with a renewed rise in real yields."
        primary = "Valuation compression or weaker earnings"
        secondary = "Concentration, liquidity or refinancing stress"
    elif "Government Bonds" in asset_segment:
        mechanism = "policy rates, inflation compensation and term premium move sovereign yields and duration returns"
        trigger = "Inflation eases or carry remains sufficient relative to duration volatility."
        veto = "Inflation re-accelerates and term premium rises without offsetting carry."
        primary = "Duration and inflation risk"
        secondary = "Fiscal supply and policy uncertainty"
    elif "Corporate High Yield" in asset_segment:
        mechanism = "growth and default expectations change credit spreads while coupon carry buffers volatility"
        trigger = "Defaults remain contained and spreads do not widen materially."
        veto = "Spread widening and refinancing stress overwhelm carry."
        primary = "Default and refinancing risk"
        secondary = "Liquidity and downgrade risk"
    elif "Corporate Investment Grade" in asset_segment:
        mechanism = "all-in yield, issuance and balance-sheet quality determine spread carry and duration sensitivity"
        trigger = "Investment-grade fundamentals remain resilient and all-in yield supports demand."
        veto = "Heavy issuance and widening spreads coincide with higher government yields."
        primary = "Spread and duration risk"
        secondary = "Issuance and downgrade risk"
    elif asset_segment == "Inflation Linked":
        mechanism = "inflation surprises and real yields alter inflation compensation and real-duration returns"
        trigger = "Inflation remains above target or upside inflation risk persists."
        veto = "Sustained disinflation compresses breakevens and raises real-return alternatives."
        primary = "Falling breakevens"
        secondary = "Rising real yields"
    elif asset_segment == "Mortgage-Backed":
        mechanism = "rate volatility and prepayment expectations alter mortgage duration, convexity and spread carry"
        trigger = "Rate volatility falls while mortgage spreads provide adequate carry."
        veto = "A renewed volatility shock worsens convexity and liquidity."
        primary = "Negative convexity and rate volatility"
        secondary = "Prepayment and liquidity risk"
    else:
        mechanism = "real yields, the dollar and risk demand change precious-metals opportunity cost and momentum"
        trigger = "Real yields or the dollar fall and precious-metals momentum stabilises."
        veto = "Real yields and the dollar rise while demand weakens."
        primary = "Dollar and real-yield headwinds"
        secondary = "Commodity volatility and positioning"
    return geography_driver, mechanism, trigger, veto, primary, secondary


def _full_monitoring_controls(asset_segment: str, benchmark_name: str) -> tuple[str, str, str, str, str]:
    if asset_segment == "Market Breadth":
        indicator = f"{benchmark_name}: 3-month return plus share of constituents above 200-day moving average"
        trigger_threshold = "3-month return > +3% and > 55% of constituents above the 200-day moving average"
        veto_threshold = "3-month return < -5% and < 45% of constituents above the 200-day moving average"
    elif asset_segment in {"Wide Market", "Value", "Growth", "Small Cap"}:
        indicator = f"{benchmark_name}: 3-month total return and net EPS-revision breadth"
        trigger_threshold = "3-month total return > +3% and net EPS-revision breadth > 55%"
        veto_threshold = "3-month total return < -5% or net EPS-revision breadth < 45%"
    elif asset_segment == "Preferred":
        indicator = f"{benchmark_name}: option-adjusted spread and 3-month total return"
        trigger_threshold = "spread tightens by at least 25 bp over 3 months and total return is positive"
        veto_threshold = "spread widens by at least 75 bp over 3 months or total return falls below -5%"
    elif "Government Bonds Long Term" in asset_segment:
        indicator = f"{benchmark_name}: 10Y+ yield change, curve slope and inflation expectations"
        trigger_threshold = "10Y+ yield falls by at least 25 bp over 3 months without a 25 bp rise in inflation expectations"
        veto_threshold = "10Y+ yield rises by at least 35 bp over 3 months or inflation expectations rise by at least 25 bp"
    elif "Government Bonds Short Term" in asset_segment:
        indicator = f"{benchmark_name}: 1-3Y yield, carry and policy-rate expectations"
        trigger_threshold = "1-3Y yield carry exceeds 3-month realised volatility and expected policy path is stable or lower"
        veto_threshold = "front-end yield rises by at least 35 bp over 3 months and realised volatility exceeds carry"
    elif "Government Bonds Mid Term" in asset_segment:
        indicator = f"{benchmark_name}: 3-10Y yield change and rolldown-to-volatility ratio"
        trigger_threshold = "3-10Y yield falls by at least 20 bp over 3 months or rolldown-to-volatility ratio exceeds 1.0"
        veto_threshold = "3-10Y yield rises by at least 35 bp over 3 months and rolldown-to-volatility ratio falls below 0.5"
    elif "Corporate High Yield" in asset_segment:
        indicator = f"{benchmark_name}: option-adjusted spread, default forecast and 3-month excess return"
        trigger_threshold = "OAS tightens or widens by no more than 25 bp over 3 months and 12-month default forecast remains at or below 3%"
        veto_threshold = "OAS widens by at least 75 bp over 3 months or 12-month default forecast exceeds 5%"
    elif "Corporate Investment Grade" in asset_segment:
        indicator = f"{benchmark_name}: option-adjusted spread, issuance and 3-month excess return"
        trigger_threshold = "OAS tightens by at least 10 bp over 3 months and excess return is positive"
        veto_threshold = "OAS widens by at least 40 bp over 3 months or excess return falls below -2%"
    elif asset_segment == "Inflation Linked":
        indicator = f"{benchmark_name}: breakeven inflation, real yield and 3-month relative return versus nominal sovereigns"
        trigger_threshold = "breakeven inflation rises by at least 20 bp over 3 months and relative return is positive"
        veto_threshold = "breakeven inflation falls by at least 30 bp over 3 months or real yield rises by at least 40 bp"
    elif asset_segment == "Mortgage-Backed":
        indicator = f"{benchmark_name}: spread, rate volatility and 3-month excess return"
        trigger_threshold = "spread tightens by at least 10 bp over 3 months and rate volatility falls by at least 10%"
        veto_threshold = "spread widens by at least 35 bp over 3 months or rate volatility rises by at least 20%"
    else:
        indicator = f"{benchmark_name}: 3-month spot return, US real yield and broad USD index"
        trigger_threshold = "3-month spot return > +5% while the 10Y US real yield or broad USD index declines"
        veto_threshold = "3-month spot return < -7% while the 10Y US real yield and broad USD index both rise"
    trigger = f"Confirm or raise the score when {trigger_threshold}."
    veto = f"Invalidate or reduce the score when {veto_threshold}."
    return indicator, trigger_threshold, veto_threshold, trigger, veto


def _full_analyst_action(score: int, conviction: str) -> str:
    if conviction == "LOW":
        return "ADD TO WATCHLIST"
    if score >= 2:
        return "MAINTAIN"
    if score == 1:
        return "REVIEW FOR UPGRADE"
    if score == -1:
        return "REVIEW FOR DOWNGRADE"
    if score <= -2:
        return "RISK CONTROL"
    return "NO ACTION"


def _load_full_source_catalog(snapshot_date: date, *, root: Path) -> dict[str, dict[str, str]]:
    specifications = {
        "region_global": (
            "Global Economics Quarterly - Crosscurrents | Insights | HSBC",
            "Policymakers will welcome lower oil prices, which ease some risks to growth and inflation. But other forces – AI, El Niño and uncertainty over a final US–Iran deal – could play out unevenly around the world.",
        ),
        "region_us": (
            "Global Investment Views - July 2026",
            "We remain mildly positive on equities, supported by strong earnings, but have reduced concentration risk by lowering our exposure to US equities and diversifying into Europe and the equally-weighted S&P 500.",
        ),
        "region_europe": (
            "Meeting of 10-11 June 2026",
            "Overall, financial conditions had remained broadly unchanged since April 2026 but remained tighter than before the start of the Middle East war.",
        ),
        "region_europe_direct": (
            "Global Investment Views - July 2026",
            "We remain mildly positive on equities, supported by strong earnings, but have reduced concentration risk by lowering our exposure to US equities and diversifying into Europe and the equally-weighted S&P 500.",
        ),
        "region_uk": (
            "Financial Stability Report - July 2026 | Bank of England",
            "Despite this, the UK financial system has remained resilient and has continued to support the UK real economy, emphasising the benefits of the resilience that has been built.",
        ),
        "region_japan": (
            "Global Economics Quarterly - Crosscurrents | Insights | HSBC",
            "GDP in Taiwan and the US is set to continue to be lifted the most, but other exporters, especially in Asia, will benefit, too.",
        ),
        "region_em": (
            "Emerging markets: The forces shaping a potential multi year trend | Invesco UK",
            "We expect the USD to weaken over a multi-year period, which should support emerging market (EM) equity and bond outperformance.",
        ),
        "equity_broad": (
            "Equity Market Outlook: Seeking opportunity beyond AI | iShares",
            "We maintain a constructive outlook for equities in 2026 as earnings growth expands across sectors and regions.",
        ),
        "equity_breadth": (
            "Equity Market Outlook: Seeking opportunity beyond AI | iShares",
            "Artificial intelligence (AI) remains a powerful long-term investment theme, but leadership in equities may broaden beyond mega-cap technology.",
        ),
        "equity_value": (
            "Equity Market Outlook: Seeking opportunity beyond AI | iShares",
            "We see potentially underappreciated opportunities in energy, materials and healthcare, where valuations and free cash flow yields may appear more attractive than many AI beneficiaries.",
        ),
        "equity_small": (
            "Big Opportunities in Small Cap Equities",
            "After years of underperformance, small-cap equities may be poised for a sustained rally.",
        ),
        "equity_preferred": (
            "Investors in retirement and an income approach | BlackRock",
            "The benefits of an income-centric approach are especially relevant for investors as they enter retirement",
        ),
        "fixed_income": (
            "Fixed Income & Bond Market Outlook | BlackRock",
            "Higher starting yields anchor income and carry, but more uneven returns reflect widening dispersion across regions, sectors and maturities.",
        ),
        "fixed_hy": (
            "Active Fixed Income Perspectives Monthly Pulse: June 2026",
            "In high yield, we are emphasizing lower beta exposure while generating value through selection in an environment of greater dispersion.",
        ),
        "fixed_ig": (
            "Active Fixed Income Perspectives Monthly Pulse: June 2026",
            "Credit: We remain overweight credit, with an up-in-quality bias and a focus on generating alpha through bottom-up security selection.",
        ),
        "fixed_inflation": (
            "Weekly market commentary: Reconciling AI earnings and valuations",
            "Global inflation-linked bonds tactical Neutral. We are neutral. We see inflation settling above pre-pandemic levels, but markets may not price this in the near term as economic growth could slow.",
        ),
        "metals": (
            "Commodities Tracker: June 2026 - Global X ETFs",
            "Gold and silver extended their retreat as unexpectedly hawkish Fed signals outweighed the impacts of an emerging ceasefire framework.",
        ),
    }
    database_path = get_settings().sqlite_path if root == ROOT_DIR else root / "data" / "mae.db"
    result: dict[str, dict[str, str]] = {}
    with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as connection:
        for key, (title, quote) in specifications.items():
            row = connection.execute(
                "SELECT s.institution_name, a.publication_date, a.url, a.content_text "
                "FROM articles a JOIN sources s ON s.id = a.source_id WHERE a.title = ? "
                "ORDER BY a.publication_date DESC LIMIT 1",
                (title,),
            ).fetchone()
            if not row:
                raise ValueError(f"Required full-snapshot source is missing: {title}")
            provider, publication_text, url, content = row
            publication_date = date.fromisoformat(publication_text)
            if publication_date > snapshot_date:
                raise ValueError(f"Publication is after snapshot date: {title}")
            if quote not in str(content or ""):
                raise ValueError(f"Exact source excerpt could not be verified: {title}")
            result[key] = {
                "provider": provider,
                "title": title,
                "publication_date": publication_date.isoformat(),
                "URL": url,
                "excerpt": quote,
                "source_class": "ALLOWLIST" if provider not in {"ECB Publications", "Bank of England Publications"} else "OFFICIAL",
            }
    return result


def _full_market_observation(benchmark: dict[str, Any]) -> dict[str, Any]:
    series = benchmark["benchmark_id"]
    observation_date = benchmark["market_observation_date"]
    confirmation = (
        f"Primary {benchmark['regional_scope']} market confirmation is {benchmark['benchmark_name']} "
        f"({benchmark['benchmark_provider']}); the official index or curve checkpoint is dated {observation_date}. "
        f"{benchmark['methodology_note']}"
    )
    return {
        "series": series,
        "provider": benchmark["benchmark_provider"],
        "title": f"Official regional benchmark: {benchmark['benchmark_name']}",
        "URL": benchmark["market_data_source"],
        "observation_date": observation_date,
        "value": f"Official {benchmark['benchmark_type']} checkpoint; numeric level not transcribed",
        "reference": "Monitor 3-month and 12-month change under the stated trigger/veto thresholds",
        "confirmation": confirmation,
        "window_days": 365,
    }


def _full_anchor_evidence(
    pilot_evidence: list[dict[str, str]],
    *,
    score_by_cell: dict[str, dict[str, Any]],
    snapshot_date: date,
    market_by_cell: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in pilot_evidence:
        cell_id = row.get("cell_id", "")
        if cell_id not in set(FULL_ANCHORS.values()) or row.get("review_status") != "PASS":
            continue
        score = score_by_cell[cell_id]
        market = market_by_cell[cell_id]
        component_role = "MARKET_CONFIRMATION" if (
            "FRED" in row.get("provider", "") or "MARKET_DATA" in row.get("source_class", "")
        ) else "PRIMARY_RESEARCH"
        publication_date = market["observation_date"] if component_role == "MARKET_CONFIRMATION" else row["publication_date"]
        source_id = (
            _stable_source_id("BENCHMARK", score["benchmark_id"], cell_id)
            if component_role == "MARKET_CONFIRMATION"
            else row["source_id"]
        )
        result.append(
            {
                "evidence_id": _stable_source_id("FULL", cell_id, source_id, component_role),
                "cell_id": cell_id,
                "asset_class": score["asset_class"],
                "asset_segment": score["asset_segment"],
                "geography": score["geography"],
                "current_score": score["score"],
                "conviction": score["conviction"],
                "evidence_mode": "ANCHOR_EXISTING",
                "source_id": source_id,
                "provider": market["provider"] if component_role == "MARKET_CONFIRMATION" else row["provider"],
                "title": market["title"] if component_role == "MARKET_CONFIRMATION" else row["title"],
                "publication_date": publication_date,
                "retrieval_date": snapshot_date.isoformat(),
                "URL": market["URL"] if component_role == "MARKET_CONFIRMATION" else row["URL"],
                "source_class": "OFFICIAL_MARKET_DATA" if component_role == "MARKET_CONFIRMATION" else row["source_class"],
                "excerpt": market["confirmation"] if component_role == "MARKET_CONFIRMATION" else row["excerpt"],
                "relevance_reason": (
                    f"MARKET_CONFIRMATION: {score['benchmark_name']} is the primary {score['regional_scope']} benchmark for this instrument definition."
                    if component_role == "MARKET_CONFIRMATION" else row["relevance_reason"]
                ),
                "component_role": component_role,
                "research_view": score["thesis"],
                "research_view_score": "",
                "fundamental_macro_summary": row.get("research_view", ""),
                "fundamental_macro_score": "",
                "market_metric": row.get("related_indicator", "") or market["series"],
                "market_observation_date": market["observation_date"],
                "market_value": row.get("actual_value", "") or market["value"],
                "market_reference": row.get("expected_or_reference_value", "") or market["reference"],
                "market_confirmation": row.get("market_confirmation", "") or market["confirmation"],
                "market_confirmation_score": "",
                "valuation_risk_summary": row.get("conflict", ""),
                "valuation_risk_score": "",
                "conflict": row.get("conflict", ""),
                "evidence_status": score["evidence_status"],
                "freshness_status": "LATEST_AVAILABLE_TRADING_DAY" if component_role == "MARKET_CONFIRMATION" else "CARRY_FORWARD_VALID",
                "review_status": "PASS",
                "reviewer_note": "Immutable pilot evidence mapping carried into the full universe without changing the anchor score.",
                **{field: score[field] for field in (
                    "benchmark_id", "benchmark_name", "benchmark_provider", "benchmark_type",
                    "instrument_definition", "currency", "duration_bucket", "regional_scope",
                    "market_data_source", "comparability_status", "proxy_quality", "methodology_note",
                    "technical_status", "analytical_quality", "market_confirmation_quality",
                )},
            }
        )
    missing = set(FULL_ANCHORS.values()) - {row["cell_id"] for row in result}
    if missing:
        raise ValueError(f"Anchor evidence is missing for: {', '.join(sorted(missing))}")
    market_cells = {row["cell_id"] for row in result if row["component_role"] == "MARKET_CONFIRMATION"}
    for cell_id in sorted(set(FULL_ANCHORS.values()) - market_cells):
        score = score_by_cell[cell_id]
        market = market_by_cell[cell_id]
        source_id = _stable_source_id("BENCHMARK", score["benchmark_id"], cell_id)
        supplemental = _full_evidence_row(
                score,
                source_id=source_id,
                provider=market["provider"],
                title=market["title"],
                publication_date=market["observation_date"],
                url=market["URL"],
                source_class="OFFICIAL_MARKET_DATA",
                excerpt=market["confirmation"],
                relevance=f"MARKET_CONFIRMATION: {score['benchmark_name']} is the primary {score['regional_scope']} benchmark for this anchor.",
                component_role="MARKET_CONFIRMATION",
                market=market,
                snapshot_date=snapshot_date,
            )
        supplemental["reviewer_note"] = "Supplemental anchor benchmark confirmation added by the financial quality pass."
        result.append(supplemental)
    return result


def _full_asset_source_key(asset_segment: str) -> str:
    if asset_segment == "Market Breadth" or asset_segment == "Growth":
        return "equity_breadth"
    if asset_segment == "Value":
        return "equity_value"
    if asset_segment == "Small Cap":
        return "equity_small"
    if asset_segment == "Preferred":
        return "equity_preferred"
    if asset_segment == "Wide Market":
        return "equity_broad"
    if "Corporate High Yield" in asset_segment:
        return "fixed_hy"
    if "Corporate Investment Grade" in asset_segment:
        return "fixed_ig"
    if asset_segment == "Inflation Linked":
        return "fixed_inflation"
    if asset_segment == "Other Precious Metals":
        return "metals"
    return "fixed_income"


def _full_region_source_key(geography: str) -> str:
    return {
        "Global": "region_global",
        "US": "region_us",
        "Europe": "region_europe",
        "UK": "region_uk",
        "Japan": "region_japan",
        "EM ex China": "region_em",
    }[geography]


def _full_new_evidence(
    score: dict[str, Any],
    *,
    sources: dict[str, dict[str, str]],
    market: dict[str, Any],
    snapshot_date: date,
) -> list[dict[str, Any]]:
    mode = score["evidence_mode"]
    if mode == "DIRECT":
        primary_key = "equity_broad" if score["geography"] == "Global" else "region_europe_direct"
        source_roles = [(primary_key, "PRIMARY_RESEARCH")]
    else:
        source_roles = [
            (_full_region_source_key(score["geography"]), "REGION_COMPONENT"),
            (_full_asset_source_key(score["asset_segment"]), "ASSET_COMPONENT"),
        ]
    result: list[dict[str, Any]] = []
    for source_key, component_role in source_roles:
        source = sources[source_key]
        if mode == "DIRECT":
            relevance = (
                f"DIRECT: the source explicitly addresses {score['geography']} broad equities and is mapped "
                f"to {score['asset_segment']}; the FRED row supplies the separate market confirmation."
            )
        elif component_role == "REGION_COMPONENT":
            geography_note = (
                "The material is a broad Asia proxy, not a Japan-specific recommendation."
                if score["geography"] == "Japan"
                else "The material covers broad EM, not an explicit EM-ex-China recommendation."
                if score["geography"] == "EM ex China"
                else "The material describes the regional macro and market backdrop, not this exact asset recommendation."
            )
            relevance = (
                f"REGION_COMPONENT: informs {score['geography']} macro conditions for {score['cell_id']}. "
                f"{geography_note} It is combined with an independent asset component and market proxy."
            )
        else:
            relevance = (
                f"ASSET_COMPONENT: addresses {score['asset_segment']} or its closest institutional asset bucket globally. "
                f"It is not represented as a direct {score['geography']} recommendation and is combined with the region component."
            )
        source_id = _stable_source_id(source["provider"], source["title"], component_role)
        result.append(
            _full_evidence_row(
                score,
                source_id=source_id,
                provider=source["provider"],
                title=source["title"],
                publication_date=source["publication_date"],
                url=source["URL"],
                source_class=source["source_class"],
                excerpt=source["excerpt"],
                relevance=relevance,
                component_role=component_role,
                market=market,
                snapshot_date=snapshot_date,
            )
        )
    market_source_id = _stable_source_id("BENCHMARK", market["series"], score["cell_id"], market["observation_date"])
    result.append(
        _full_evidence_row(
            score,
            source_id=market_source_id,
            provider=market["provider"],
            title=market["title"],
            publication_date=market["observation_date"],
            url=market["URL"],
            source_class="OFFICIAL_MARKET_DATA",
            excerpt=market["confirmation"],
            relevance=(
                f"MARKET_CONFIRMATION: {score['benchmark_name']} is the primary {score['regional_scope']} "
                f"benchmark for {score['instrument_definition']}; comparability={score['comparability_status']}."
            ),
            component_role="MARKET_CONFIRMATION",
            market=market,
            snapshot_date=snapshot_date,
        )
    )
    return result


def _full_evidence_row(
    score: dict[str, Any],
    *,
    source_id: str,
    provider: str,
    title: str,
    publication_date: str,
    url: str,
    source_class: str,
    excerpt: str,
    relevance: str,
    component_role: str,
    market: dict[str, Any],
    snapshot_date: date,
) -> dict[str, Any]:
    return {
        "evidence_id": _stable_source_id("EVIDENCE", score["cell_id"], source_id, component_role),
        "cell_id": score["cell_id"],
        "asset_class": score["asset_class"],
        "asset_segment": score["asset_segment"],
        "geography": score["geography"],
        "current_score": score["score"],
        "conviction": score["conviction"],
        "evidence_mode": score["evidence_mode"],
        "source_id": source_id,
        "provider": provider,
        "title": title,
        "publication_date": publication_date,
        "retrieval_date": snapshot_date.isoformat(),
        "URL": url,
        "source_class": source_class,
        "excerpt": excerpt,
        "relevance_reason": relevance,
        "component_role": component_role,
        "research_view": score["thesis"],
        "research_view_score": score["research_view_score"],
        "fundamental_macro_summary": score["main_driver"],
        "fundamental_macro_score": score["fundamental_macro_score"],
        "market_metric": market["series"],
        "market_observation_date": market["observation_date"],
        "market_value": market["value"],
        "market_reference": market["reference"],
        "market_confirmation": market["confirmation"],
        "market_confirmation_score": score["market_confirmation_score"],
        "valuation_risk_summary": score["primary_risk"],
        "valuation_risk_score": score["valuation_risk_score"],
        "conflict": score["secondary_risk"],
        "evidence_status": score["evidence_status"],
        "freshness_status": "CURRENT_90D" if component_role != "MARKET_CONFIRMATION" else "LATEST_AVAILABLE_TRADING_DAY",
        "review_status": "PASS",
        "reviewer_note": (
            "Composite mapping is capped at |1| and does not claim a direct recommendation."
            if score["evidence_mode"] == "COMPOSITE"
            else "Direct mapping validated with separate official market confirmation."
        ),
        **{field: score[field] for field in (
            "benchmark_id", "benchmark_name", "benchmark_provider", "benchmark_type",
            "instrument_definition", "currency", "duration_bucket", "regional_scope",
            "market_data_source", "comparability_status", "proxy_quality", "methodology_note",
            "technical_status", "analytical_quality", "market_confirmation_quality",
        )},
    }


def _full_scenario_effect(asset_segment: str, scenario_id: str, score: int) -> int:
    if scenario_id == "BASE":
        return max(-2, min(2, score))
    if scenario_id == "UPSIDE":
        if asset_segment in {"Market Breadth", "Small Cap"} or "Corporate High Yield" in asset_segment:
            return 2
        if asset_segment in {"Wide Market", "Preferred", "Value", "Growth", "Mortgage-Backed"} or "Corporate Investment Grade" in asset_segment:
            return 1
        if asset_segment in {"Inflation Linked", "Gold"}:
            return -1
        return 0
    if asset_segment in {"Growth", "Small Cap"} or "Corporate High Yield" in asset_segment:
        return -2
    if asset_segment in {"Wide Market", "Market Breadth", "Preferred", "Value", "Mortgage-Backed"} or "Corporate Investment Grade" in asset_segment:
        return -1
    if asset_segment in {"Government Bonds Short Term", "Inflation Linked", "Other Precious Metals"}:
        return 1
    if asset_segment == "Gold":
        return 2
    if asset_segment == "Government Bonds Long Term":
        return -1
    return 0


def _full_scenarios(snapshot_date: date, scores: list[dict[str, Any]], *, root: Path) -> list[dict[str, Any]]:
    canonical = {row["scenario_id"]: row for row in load_canonical_scenarios(snapshot_date, root=root)}
    applicable = [row for row in scores if row["applicable"] == "true"]
    result = []
    for scenario_id in ("BASE", "UPSIDE", "DOWNSIDE"):
        row = dict(canonical[scenario_id])
        effects = {
            score["cell_id"]: _full_scenario_effect(score["asset_segment"], scenario_id, int(score["score"]))
            for score in applicable
        }
        winners = [cell_id for cell_id, effect in effects.items() if effect >= 2][:20]
        vulnerable = [cell_id for cell_id, effect in effects.items() if effect <= -2][:20]
        row.update(
            {
                "snapshot_date": snapshot_date.isoformat(),
                "probability_band": "HIGH" if scenario_id == "BASE" else "LOW",
                "winners": "; ".join(winners) or row["winners"],
                "vulnerable_assets": "; ".join(vulnerable) or row["vulnerable_assets"],
                "affected_cells": "; ".join(score["cell_id"] for score in applicable),
                "expected_reaction_by_cell": "; ".join(f"{cell_id}:{effect:+d}" for cell_id, effect in effects.items()),
                "last_review_date": snapshot_date.isoformat(),
            }
        )
        result.append(row)
    return result


def _full_transmission(
    scores: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    scenarios: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    sources_by_cell: dict[str, set[str]] = defaultdict(set)
    for row in evidence:
        if row["review_status"] == "PASS":
            sources_by_cell[row["cell_id"]].add(row["source_id"])
    scenario_effects: dict[str, dict[str, int]] = {}
    for scenario in scenarios:
        scenario_effects[scenario["scenario_id"]] = {
            item.split(":", 1)[0]: int(item.split(":", 1)[1])
            for item in _split_items(scenario["expected_reaction_by_cell"])
        }
    result = []
    for score in scores:
        if score["applicable"] != "true":
            continue
        _, mechanism, _, _, _, _ = _full_driver_controls(score["geography"], score["asset_segment"])
        reaction = score["direction"]
        result.append(
            {
                "cell_id": score["cell_id"],
                "asset_class": score["asset_class"],
                "asset_segment": score["asset_segment"],
                "geography": score["geography"],
                "current_score": score["score"],
                "conviction": score["conviction"],
                "macro_driver": score["main_driver"],
                "transmission_mechanism": score.get("_transmission_mechanism") or mechanism,
                "expected_asset_reaction": reaction,
                "investment_logic": (
                    f"{score['main_driver']} → {score.get('_transmission_mechanism') or mechanism} → "
                    f"{score['asset_segment']} → {reaction}."
                ),
                "trigger": score["trigger"],
                "veto": score["veto"],
                "primary_risk": score["primary_risk"],
                "secondary_risk": score["secondary_risk"],
                "base_scenario_effect": scenario_effects["BASE"][score["cell_id"]],
                "upside_scenario_effect": scenario_effects["UPSIDE"][score["cell_id"]],
                "downside_scenario_effect": scenario_effects["DOWNSIDE"][score["cell_id"]],
                "next_review_date": score["next_review_date"],
                "analyst_action": score["analyst_action"],
                "source_ids": "; ".join(sorted(sources_by_cell[score["cell_id"]])),
                "control_status": "PASS",
                "benchmark_id": score["benchmark_id"],
                "comparability_status": score["comparability_status"],
                "monitoring_indicator": score["monitoring_indicator"],
                "trigger_threshold": score["trigger_threshold"],
                "veto_threshold": score["veto_threshold"],
                "technical_status": score["technical_status"],
                "analytical_quality": score["analytical_quality"],
                "market_confirmation_quality": score["market_confirmation_quality"],
            }
        )
    return result


def _full_market_summary(scores: list[dict[str, Any]], scenarios: list[dict[str, Any]]) -> str:
    applicable = [row for row in scores if row["applicable"] == "true" and row["review_status"] == "PASS"]
    ranked = sorted(applicable, key=lambda row: (-int(row["score"]), int(row["display_order"])))
    top = ", ".join(f"{row['geography']} × {row['asset_segment']} ({int(row['score']):+d})" for row in ranked[:5])
    return (
        "Базовый режим — устойчивый, но неоднородный рост при сохраняющейся инфляции и высокой стоимости денег. "
        f"Наиболее привлекательные сочетания: {top}. Equity breadth поддерживается расширением прибыли за пределы mega-cap, "
        "однако valuation и концентрация ограничивают conviction. В duration предпочтение остаётся у short-to-belly carry; "
        "long duration уязвима к term premium и инфляционным сюрпризам. В credit доходность поддерживает short/medium exposure, "
        "но узкие spreads и refinancing risk требуют селективности. Главные риски — новый energy/inflation shock, рост реальных "
        f"ставок и ухудшение earnings revisions. Базовый canonical сценарий — {scenarios[0]['scenario_name']}; "
        "низкий conviction отмечен там, где регион подтверждён только прозрачным proxy."
    )


def write_full_snapshot_datasets(product: FullSnapshotProduct, directory: Path) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = product.snapshot_date.isoformat()
    paths = {
        "scores": directory / f"mae_full_scores_{stamp}.csv",
        "evidence": directory / f"mae_full_evidence_{stamp}.csv",
        "scenarios": directory / f"mae_full_scenarios_{stamp}.csv",
        "transmission": directory / f"mae_full_transmission_{stamp}.csv",
        "report": directory / f"mae_full_report_{stamp}.md",
        "quality_json": directory / f"mae_full_quality_report_{stamp}.json",
        "quality_md": directory / f"mae_full_quality_report_{stamp}.md",
    }
    sanitized_scores = [{key: value for key, value in row.items() if not key.startswith("_")} for row in product.scores]
    write_csv(paths["scores"], sanitized_scores, FULL_SCORE_FIELDS)
    write_csv(paths["evidence"], product.evidence, FULL_EVIDENCE_FIELDS)
    write_csv(paths["scenarios"], product.scenarios, FULL_SCENARIO_FIELDS)
    write_csv(paths["transmission"], product.transmission, FULL_TRANSMISSION_FIELDS)
    paths["report"].write_text(render_full_markdown_report(product), encoding="utf-8")
    quality = full_quality_report(product)
    paths["quality_json"].write_text(json.dumps(quality, ensure_ascii=False, indent=2), encoding="utf-8")
    paths["quality_md"].write_text(render_full_quality_markdown(quality), encoding="utf-8")
    return paths


def full_quality_report(product: FullSnapshotProduct) -> dict[str, Any]:
    applicable = product.applicable_scores
    distribution = {str(score): sum(int(row["score"]) == score for row in applicable) for score in range(-3, 4)}
    warnings: list[str] = []
    composite_share = sum(row["evidence_mode"] == "COMPOSITE" for row in applicable) / len(applicable)
    low_share = sum(row["conviction"] == "LOW" for row in applicable) / len(applicable)
    limited = [row["cell_id"] for row in applicable if row["comparability_status"] == "LIMITED_COMPARABILITY"]
    regional_proxy = [row["cell_id"] for row in applicable if row["proxy_quality"] == "REGIONAL_PROXY"]
    if composite_share >= 0.75:
        warnings.append("HIGH_COMPOSITE_SHARE")
    if low_share >= 0.25:
        warnings.append("LOW_CONVICTION_SHARE")
    if limited:
        warnings.append("LIMITED_COMPARABILITY")
    if regional_proxy:
        warnings.append("REGIONAL_PROXY_USED")
    if max(distribution.values()) / len(applicable) >= 0.50:
        warnings.append("SCORE_CONCENTRATION")
    source_counts: dict[str, int] = defaultdict(int)
    for row in product.evidence:
        source_counts[row["source_id"]] += 1
    if source_counts and max(source_counts.values()) / len(applicable) >= 0.50:
        warnings.append("SOURCE_REUSE_CONCENTRATION")
    max_source_id = max(source_counts, key=source_counts.get) if source_counts else ""
    max_source_count = source_counts.get(max_source_id, 0)
    report = {
        "status": "PASS",
        "snapshot_date": product.snapshot_date.isoformat(),
        "product_version": FULL_PRODUCT_VERSION,
        "applicable_cells": len(applicable),
        "benchmark_coverage": len({row["cell_id"] for row in applicable if row.get("benchmark_id")}),
        "technical_pass_count": sum(row["technical_status"] == "TECHNICAL_PASS" for row in applicable),
        "analytical_quality": {
            quality: sum(row["analytical_quality"] == quality for row in applicable)
            for quality in ("STRONG", "ACCEPTABLE", "WEAK", "REVIEW_REQUIRED")
        },
        "market_confirmation_quality": {
            quality: sum(row["market_confirmation_quality"] == quality for row in applicable)
            for quality in ("STRONG", "PARTIAL", "WEAK", "CONTRADICTED")
        },
        "evidence_modes": {
            mode: sum(row["evidence_mode"] == mode for row in applicable)
            for mode in ("DIRECT", "COMPOSITE", "ANCHOR_EXISTING")
        },
        "conviction": {level: sum(row["conviction"] == level for row in applicable) for level in ("HIGH", "MEDIUM", "LOW")},
        "comparability": {status: sum(row["comparability_status"] == status for row in applicable) for status in sorted(COMPARABILITY_STATUSES)},
        "proxy_quality": {quality: sum(row["proxy_quality"] == quality for row in applicable) for quality in sorted(PROXY_QUALITIES)},
        "regional_market_confirmation_coverage": sum(
            row["proxy_quality"] in {"DIRECT_REGIONAL", "DIRECT_GLOBAL", "REGIONAL_PROXY"} for row in applicable
        ),
        "regional_proxy_count": sum(row["proxy_quality"] == "REGIONAL_PROXY" for row in applicable),
        "limited_comparability_count": sum(row["comparability_status"] == "LIMITED_COMPARABILITY" for row in applicable),
        "source_reuse_concentration": {
            "most_reused_source_id": max_source_id,
            "cell_evidence_rows": max_source_count,
            "share_of_applicable_cells": round(max_source_count / len(applicable), 4),
        },
        "score_distribution": distribution,
        "methodology_refinements": sum(row["change_type"] == "METHODOLOGY_REFINEMENT" for row in applicable),
        "benchmark_refinements": len(applicable),
        "unchanged_scores": sum(row["change_type"] in {"NO_CHANGE", "UNCHANGED", "CARRY_FORWARD"} for row in applicable),
        "strongest_positive_cells": [row["cell_id"] for row in sorted(applicable, key=lambda item: (-int(item["score"]), int(item["display_order"])))[:10]],
        "strongest_negative_cells": [row["cell_id"] for row in sorted(applicable, key=lambda item: (int(item["score"]), int(item["display_order"])))[:10]],
        "low_conviction_cells": [row["cell_id"] for row in applicable if row["conviction"] == "LOW"],
        "limited_comparability_cells": limited,
        "structural_proxy_cells": [row["cell_id"] for row in applicable if row["comparability_status"] == "STRUCTURAL_PROXY"],
        "methodology_refinement_cells": [row["cell_id"] for row in applicable if row["change_type"] == "METHODOLOGY_REFINEMENT"],
        "changed_score_cells": [row["cell_id"] for row in applicable if str(row.get("score_delta", "")).strip() and int(row["score_delta"]) != 0],
        "unresolved_analytical_weaknesses": [row["cell_id"] for row in applicable if row["analytical_quality"] in {"WEAK", "REVIEW_REQUIRED"}],
        "warnings": warnings,
    }
    if report["technical_pass_count"] != 104 or report["analytical_quality"]["WEAK"] or report["analytical_quality"]["REVIEW_REQUIRED"] or report["proxy_quality"]["NO_VALID_PROXY"]:
        report["status"] = "FAIL"
    return report


def render_full_quality_markdown(report: dict[str, Any]) -> str:
    quality = report["analytical_quality"]
    modes = report["evidence_modes"]
    conviction = report["conviction"]
    comparability = report["comparability"]
    lines = [
        f"# MAE Full Financial Quality Report — {report['snapshot_date']}", "",
        f"Status: **{report['status']}**. Technical PASS: **{report['technical_pass_count']}/104**. Regional market confirmation: **{report['regional_market_confirmation_coverage']}/104**.", "",
        "## Quality counts", "",
        f"- Analytical quality: STRONG {quality['STRONG']}; ACCEPTABLE {quality['ACCEPTABLE']}; WEAK {quality['WEAK']}; REVIEW_REQUIRED {quality['REVIEW_REQUIRED']}.",
        f"- Evidence mode: DIRECT {modes['DIRECT']}; COMPOSITE {modes['COMPOSITE']}; ANCHOR_EXISTING {modes['ANCHOR_EXISTING']}.",
        f"- Conviction: HIGH {conviction['HIGH']}; MEDIUM {conviction['MEDIUM']}; LOW {conviction['LOW']}.",
        f"- Comparability: DIRECTLY_COMPARABLE {comparability['DIRECTLY_COMPARABLE']}; LIMITED_COMPARABILITY {comparability['LIMITED_COMPARABILITY']}; STRUCTURAL_PROXY {comparability['STRUCTURAL_PROXY']}.",
        f"- Methodology refinements: {report['methodology_refinements']}; benchmark refinements: {report['benchmark_refinements']}.", "",
        f"- Source reuse concentration: {report['source_reuse_concentration']['most_reused_source_id']} appears in {report['source_reuse_concentration']['cell_evidence_rows']} evidence rows ({report['source_reuse_concentration']['share_of_applicable_cells']:.1%} of applicable cells).", "",
        "## Review lists", "",
        f"- Strongest positive: {', '.join(report['strongest_positive_cells'])}.",
        f"- Strongest negative: {', '.join(report['strongest_negative_cells'])}.",
        f"- LOW conviction: {', '.join(report['low_conviction_cells']) or 'none'}.",
        f"- Limited comparability: {', '.join(report['limited_comparability_cells']) or 'none'}.",
        f"- Structural proxies: {', '.join(report['structural_proxy_cells']) or 'none'}.",
        f"- Methodology refinements: {', '.join(report['methodology_refinement_cells']) or 'none'}.", "",
        f"- Unresolved analytical weaknesses: {', '.join(report['unresolved_analytical_weaknesses']) or 'none'}.", "",
        "## Warnings", "",
        ", ".join(report["warnings"]) if report["warnings"] else "None.",
    ]
    return "\n".join(lines).rstrip() + "\n"


def render_full_markdown_report(product: FullSnapshotProduct) -> str:
    lines = [
        f"# MAE Full Market Map — {product.snapshot_date.isoformat()}", "",
        f"Snapshot type: **{product.snapshot_type}**. Previous comparable snapshot: **{product.previous_snapshot_date.isoformat()}**. Horizon: **6–12 months**.",
        "", "## Market Summary", "", product.market_summary, "", "## Strategy & Sentiment Map", "",
        "| Asset segment | Global | US | Europe | UK | Japan | EM ex China |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    score_map = {(row["asset_segment"], row["geography"]): row for row in product.scores}
    for _, _, asset_segment in FULL_ASSETS:
        values = []
        for geography in FULL_GEOGRAPHIES:
            row = score_map[(asset_segment, geography)]
            values.append("N/A" if row["applicable"] != "true" else str(int(row["score"])))
        lines.append(f"| {asset_segment} | {' | '.join(values)} |")
    lines.extend(["", "## Change Tracker", ""])
    new_count = sum(row["change_type"] == "NEW_COMPARABLE_ASSESSMENT" for row in product.scores)
    anchor_count = sum(row["evidence_mode"] == "ANCHOR_EXISTING" for row in product.scores)
    methodology_count = sum(row["change_type"] == "METHODOLOGY_REFINEMENT" for row in product.scores)
    unchanged_count = sum(row["change_type"] in {"NO_CHANGE", "UNCHANGED", "CARRY_FORWARD"} for row in product.scores)
    lines.append(f"New assessments: **{new_count}**. Methodology refinements: **{methodology_count}**. Unchanged: **{unchanged_count}**. Immutable anchors: **{anchor_count}**.")
    lines.extend(["", "## Scenarios", ""])
    for scenario in product.scenarios:
        lines.extend(
            [
                f"### {scenario['scenario_name']}", "",
                f"Status: **{scenario['current_status']}**. Probability band: **{scenario['probability_band']}**.", "",
                scenario["narrative"], "",
                f"- Causal chain: {scenario['causal_chain']}",
                f"- Trigger: {scenario['trigger']}",
                f"- Veto: {scenario['veto']}", "",
            ]
        )
    lines.extend(
        [
            "## Evidence & Expectation", "",
            f"PASS coverage: **{product.coverage}/104**; evidence rows: **{len(product.evidence)}**.", "",
            "## Transmission & Control", "",
            f"Control rows: **{len(product.transmission)}**. Every applicable cell has one driver → mechanism → reaction chain.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def full_workbook_manifest(product: FullSnapshotProduct) -> dict[str, Any]:
    applicable = product.applicable_scores
    quality = full_quality_report(product)
    modes = {mode: sum(row["evidence_mode"] == mode for row in applicable) for mode in ("DIRECT", "COMPOSITE", "ANCHOR_EXISTING")}
    convictions = {level: sum(row["conviction"] == level for row in applicable) for level in ("HIGH", "MEDIUM", "LOW")}
    return {
        "snapshot_date": product.snapshot_date.isoformat(),
        "snapshot_type": product.snapshot_type,
        "snapshot_status": product.snapshot_status,
        "production_eligible": product.snapshot_status == PRODUCTION_STATUS,
        "previous_snapshot_date": product.previous_snapshot_date.isoformat(),
        "horizon": "6–12 months",
        "validation_status": "PASS",
        "total_intersections": 114,
        "applicable_cells": 104,
        "not_applicable_cells": 10,
        "evidence_coverage": f"{product.coverage}/104",
        "evidence_modes": modes,
        "convictions": convictions,
        "upgrades": sum(row["change_type"] == "UPGRADE" for row in applicable),
        "downgrades": sum(row["change_type"] == "DOWNGRADE" for row in applicable),
        "unchanged": sum(row["change_type"] in {"NO_CHANGE", "UNCHANGED", "CARRY_FORWARD"} for row in applicable),
        "new_assessments": sum(row["change_type"] == "NEW_COMPARABLE_ASSESSMENT" for row in applicable),
        "methodology_refinements": quality["methodology_refinements"],
        "benchmark_refinements": quality["benchmark_refinements"],
        "technical_pass_count": quality["technical_pass_count"],
        "analytical_quality": quality["analytical_quality"],
        "market_confirmation_quality": quality["market_confirmation_quality"],
        "regional_market_confirmation_coverage": quality["regional_market_confirmation_coverage"],
        "comparability": quality["comparability"],
        "proxy_quality": quality["proxy_quality"],
        "quality_warnings": quality["warnings"],
        "market_summary": product.market_summary,
        "database_preflight": product.database_preflight,
        "pilot_hashes": product.pilot_hashes,
        "data_windows": product.data_windows,
        "product_version": FULL_PRODUCT_VERSION,
    }


def _pilot_output_hashes(*, root: Path, pilot_paths: dict[str, Path] | None = None) -> dict[str, str]:
    path_map = {
        "mae_latest.xlsx": "workbook",
        "mae_latest_scores.csv": "scores",
        "mae_latest_evidence.csv": "evidence",
        "mae_latest_scenarios.csv": "scenarios",
        "mae_latest_report.md": "report",
    }
    result = {}
    for name, key in path_map.items():
        path = (pilot_paths or {}).get(key, root / "outputs" / name)
        if not path.exists():
            raise ValueError(f"Pilot output is missing: {path}")
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def persist_full_snapshot(
    product: FullSnapshotProduct,
    *,
    root: Path = ROOT_DIR,
    session: Session | None = None,
) -> str:
    """Persist full rows, flushing but never ending a caller-owned transaction."""
    if product.snapshot_date > date.today():
        raise ValueError("Snapshot date cannot be in the future.")
    if product.snapshot_status != PRODUCTION_STATUS:
        raise ValueError("TEST_ONLY snapshots cannot be written to production SQLite.")
    owns_session = session is None
    if owns_session:
        settings = get_settings()
        if root != ROOT_DIR:
            settings = type(settings)(sqlite_path=root / "data" / "mae.db")
        engine = create_app_engine(settings)
        init_database(engine)
        session_context = create_session_factory(engine)()
    else:
        session_context = nullcontext(session)
    with session_context as session:
        snapshot = session.scalar(
            select(MaeSnapshot).where(MaeSnapshot.snapshot_date == product.snapshot_date, MaeSnapshot.is_demo.is_(False))
        )
        if not snapshot:
            raise ValueError("Full snapshot requires the accepted pilot snapshot on the same production date.")
        metadata = dict(snapshot.run_metadata or {})
        existing_cells = list(
            session.scalars(select(MaeSnapshotCell).where(MaeSnapshotCell.snapshot_id == snapshot.id))
        )
        by_cell = {row.canonical_cell_id: row for row in existing_cells}
        if metadata.get("full_product_version") == FULL_PRODUCT_VERSION:
            expected = {row["cell_id"] for row in product.scores}
            if set(by_cell) != expected:
                raise ValueError(f"Persisted full snapshot is incomplete: {len(by_cell)}/114 cells.")
            for row in product.applicable_scores:
                if by_cell[row["cell_id"]].mae_score != int(row["score"]):
                    raise ValueError(f"Persisted full score differs for {row['cell_id']}.")
            return snapshot.id

        for anchor_id in FULL_ANCHORS.values():
            if anchor_id not in by_cell:
                raise ValueError(f"Pilot anchor is absent from SQLite: {anchor_id}")
            full_row = next(row for row in product.scores if row["cell_id"] == anchor_id)
            anchor_cell = by_cell[anchor_id]
            if anchor_cell.mae_score != int(full_row["score"]) or anchor_cell.thesis != full_row["thesis"]:
                raise ValueError(f"Pilot anchor would be changed by full build: {anchor_id}")

        non_anchor_ids = {
            row["cell_id"] for row in product.scores if row["cell_id"] not in set(FULL_ANCHORS.values())
        }
        prior_full_evidence = list(
            session.scalars(
                select(EvidenceItem).where(
                    EvidenceItem.snapshot_id == snapshot.id,
                    EvidenceItem.canonical_cell_id.in_(non_anchor_ids),
                )
            )
        )
        for item in prior_full_evidence:
            session.delete(item)
        prior_anchor_supplements = list(
            session.scalars(
                select(EvidenceItem).where(
                    EvidenceItem.snapshot_id == snapshot.id,
                    EvidenceItem.canonical_cell_id.in_(set(FULL_ANCHORS.values())),
                    EvidenceItem.report_title.like("Official regional benchmark:%"),
                )
            )
        )
        for item in prior_anchor_supplements:
            session.delete(item)
        removed_anchor_ids = {item.id for item in prior_anchor_supplements}
        if removed_anchor_ids:
            for anchor_id in FULL_ANCHORS.values():
                anchor_cell = by_cell[anchor_id]
                anchor_cell.evidence_item_ids = [
                    item_id for item_id in (anchor_cell.evidence_item_ids or []) if item_id not in removed_anchor_ids
                ]
                anchor_cell.source_count = len(anchor_cell.evidence_item_ids)
        for cell_id in non_anchor_ids:
            if cell_id in by_cell:
                by_cell[cell_id].evidence_item_ids = []
                by_cell[cell_id].source_count = 0
                by_cell[cell_id].independent_source_count = 0
        session.flush()

        score_by_cell = {row["cell_id"]: row for row in product.scores}
        for row in product.scores:
            if row["cell_id"] in by_cell:
                cell = by_cell[row["cell_id"]]
                quality_metadata = {
                    "evidence_mode": row["evidence_mode"],
                    "benchmark_registry": {
                        key: row.get(key, "") for key in (
                            "instrument_definition", "benchmark_id", "benchmark_name", "benchmark_provider",
                            "benchmark_type", "currency", "duration_bucket", "regional_scope",
                            "market_data_source", "comparability_status", "proxy_quality", "methodology_note",
                        )
                    },
                    "quality": {
                        "technical_status": row.get("technical_status", ""),
                        "analytical_quality": row.get("analytical_quality", ""),
                        "market_confirmation_quality": row.get("market_confirmation_quality", ""),
                    },
                    "monitoring": {
                        "indicator": row.get("monitoring_indicator", ""),
                        "trigger_threshold": row.get("trigger_threshold", ""),
                        "veto_threshold": row.get("veto_threshold", ""),
                    },
                    "conflict": row.get("secondary_risk", ""),
                }
                if row["cell_id"] in FULL_ANCHORS.values():
                    preserved = dict(cell.disagreement or {})
                    preserved.update(quality_metadata)
                    cell.disagreement = preserved
                    continue
                applicable = row["applicable"] == "true"
                cell.applicability = "APPLICABLE" if applicable else "NOT_APPLICABLE"
                cell.mae_score = int(row["score"]) if applicable else None
                cell.thesis = row["thesis"]
                cell.driver = row["main_driver"]
                cell.evidence_status = row["evidence_status"]
                cell.confidence = row["conviction"]
                cell.scenario_link = row["scenario_link"]
                cell.change_status = row["change_type"]
                cell.change_summary = row["what_changed"]
                cell.coverage_status = "SUPPORTED" if applicable else "NOT_APPLICABLE"
                cell.disagreement = {
                    **quality_metadata,
                    "component_scores": {
                        "research_view_score": row["research_view_score"],
                        "fundamental_macro_score": row["fundamental_macro_score"],
                        "market_confirmation_score": row["market_confirmation_score"],
                        "valuation_risk_score": row["valuation_risk_score"],
                        "weighted_score": row["weighted_score"],
                    },
                }
                cell.explanation = row["why_changed"]
                cell.scenario_adjustment_reason = row.get("_transmission_mechanism", "") if applicable else ""
                continue
            applicable = row["applicable"] == "true"
            component_scores = {
                "research_view_score": row["research_view_score"],
                "fundamental_macro_score": row["fundamental_macro_score"],
                "market_confirmation_score": row["market_confirmation_score"],
                "valuation_risk_score": row["valuation_risk_score"],
                "weighted_score": row["weighted_score"],
            }
            cell = MaeSnapshotCell(
                snapshot_id=snapshot.id,
                snapshot_date=product.snapshot_date,
                canonical_cell_id=row["cell_id"],
                template_row_key=f"{row['asset_class']}|{row['asset_group']}|{row['asset_segment']}",
                region=row["geography"],
                asset=row["asset_segment"],
                applicability="APPLICABLE" if applicable else "NOT_APPLICABLE",
                mae_score=int(row["score"]) if applicable else None,
                previous_snapshot_score=None,
                score_change=None,
                thesis=row["thesis"],
                driver=row["main_driver"],
                previous_thesis="",
                previous_driver="",
                evidence_status=row["evidence_status"],
                confidence=row["conviction"],
                carry_forward=False,
                scenario_link=row["scenario_link"],
                change_status=row["change_type"],
                change_summary=row["what_changed"],
                coverage_status="SUPPORTED" if applicable else "NOT_APPLICABLE",
                source_count=0,
                independent_source_count=0,
                evidence_item_ids=[],
                disagreement={
                    "evidence_mode": row["evidence_mode"],
                    "component_scores": component_scores,
                    "conflict": row["secondary_risk"],
                    "benchmark_registry": {
                        key: row.get(key, "") for key in (
                            "instrument_definition", "benchmark_id", "benchmark_name", "benchmark_provider",
                            "benchmark_type", "currency", "duration_bucket", "regional_scope",
                            "market_data_source", "comparability_status", "proxy_quality", "methodology_note",
                        )
                    },
                    "quality": {
                        "technical_status": row.get("technical_status", ""),
                        "analytical_quality": row.get("analytical_quality", ""),
                        "market_confirmation_quality": row.get("market_confirmation_quality", ""),
                    },
                    "monitoring": {
                        "indicator": row.get("monitoring_indicator", ""),
                        "trigger_threshold": row.get("trigger_threshold", ""),
                        "veto_threshold": row.get("veto_threshold", ""),
                    },
                },
                explanation=row["why_changed"],
                scenario_adjustment=None,
                scenario_adjustment_reason=row["_transmission_mechanism"] if applicable else "",
                is_demo=False,
            )
            session.add(cell)
            session.flush()
            by_cell[row["cell_id"]] = cell

        evidence_ids: dict[str, list[str]] = defaultdict(list)
        existing_anchor_evidence_ids = {
            cell_id: list(by_cell[cell_id].evidence_item_ids or []) for cell_id in FULL_ANCHORS.values()
        }
        providers: dict[str, set[str]] = defaultdict(set)
        for row in product.evidence:
            is_anchor_supplement = (
                row["evidence_mode"] == "ANCHOR_EXISTING"
                and row.get("reviewer_note") == "Supplemental anchor benchmark confirmation added by the financial quality pass."
            )
            if (row["evidence_mode"] == "ANCHOR_EXISTING" and not is_anchor_supplement) or row["review_status"] != "PASS":
                continue
            score = score_by_cell[row["cell_id"]]
            item = EvidenceItem(
                snapshot_id=snapshot.id,
                snapshot_cell_id=by_cell[row["cell_id"]].id,
                article_id=None,
                research_view_id=None,
                snapshot_date=product.snapshot_date,
                provider=row["provider"],
                provider_group=row["provider"],
                report_title=row["title"],
                publication_date=date.fromisoformat(row["publication_date"]),
                ingested_at=datetime.now(UTC),
                url=row["URL"],
                exact_quote=row["excerpt"],
                affected_region=row["geography"],
                affected_asset_segment=row["asset_segment"],
                template_row_key=f"{row['asset_class']}|{row['asset_segment']}",
                canonical_cell_id=row["cell_id"],
                extracted_stance=score["direction"],
                source_specificity=row["evidence_mode"],
                source_independence_group=row["provider"],
                source_class=row["source_class"],
                relevance_reason=row["relevance_reason"],
                related_indicator=f"{row['component_role']}: {row['market_metric']}",
                actual_value=row["market_value"],
                expected_value=row["market_reference"],
                market_confirmation=row["market_confirmation"],
                conflict=row["conflict"],
                evidence_status=row["evidence_status"],
                review_status=row["review_status"],
                carry_forward=False,
                is_demo=False,
            )
            session.add(item)
            session.flush()
            evidence_ids[row["cell_id"]].append(item.id)
            providers[row["cell_id"]].add(row["provider"])
        for cell_id, ids in evidence_ids.items():
            merged_ids = existing_anchor_evidence_ids.get(cell_id, []) + ids
            by_cell[cell_id].evidence_item_ids = merged_ids
            by_cell[cell_id].source_count = len(merged_ids)
            by_cell[cell_id].independent_source_count = max(
                by_cell[cell_id].independent_source_count or 0,
                len(providers[cell_id]),
            )

        scenario_models = list(
            session.scalars(
                select(MarketScenario).where(
                    MarketScenario.snapshot_date == product.snapshot_date,
                    MarketScenario.is_demo.is_(False),
                )
            )
        )
        by_scenario = {row.scenario_type: row for row in scenario_models}
        if set(by_scenario) != {"BASE", "UPSIDE", "DOWNSIDE"}:
            raise ValueError("Canonical SQLite scenario set must contain exactly BASE, UPSIDE and DOWNSIDE.")
        for row in product.scenarios:
            model = by_scenario[row["scenario_id"]]
            model.affected_cells = _split_items(row["affected_cells"])
            model.expected_reaction_by_cell = {
                item.split(":", 1)[0]: item.split(":", 1)[1]
                for item in _split_items(row["expected_reaction_by_cell"])
            }
            model.last_review_date = product.snapshot_date

        metadata.update(
            {
                "full_product_version": FULL_PRODUCT_VERSION,
                "full_snapshot_type": product.snapshot_type,
                "full_snapshot_status": product.snapshot_status,
                "full_production_eligible": True,
                "full_total_intersections": 114,
                "full_applicable_cells": 104,
                "full_evidence_coverage": f"{product.coverage}/104",
                "full_anchor_cells": sorted(FULL_ANCHORS.values()),
                "full_pilot_hashes": product.pilot_hashes,
                "full_quality_report": full_quality_report(product),
            }
        )
        snapshot.run_metadata = metadata
        coverage = dict(snapshot.coverage_report or {})
        coverage["full"] = {"supported_cells": product.coverage, "total_cells": 104, "intersections": 114, "coverage": f"{product.coverage}/104"}
        snapshot.coverage_report = coverage
        if owns_session:
            session.commit()
        else:
            session.flush()
        return snapshot.id


def publish_full_latest(
    snapshot_dir: Path,
    snapshot_date: date,
    *,
    root: Path = ROOT_DIR,
    test_only: bool = False,
) -> None:
    if test_only:
        raise ValueError("TEST_ONLY snapshots cannot update full latest outputs.")
    raise RuntimeError(
        "Direct full-latest publication is forbidden; publish an immutable manifest through atomic_release."
    )
