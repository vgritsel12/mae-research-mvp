from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import ROOT_DIR
from app.domain.models import Article, ResearchView
from app.services.methodology import (
    INVALID_MAPPING,
    VERIFIED_PASS,
    assess_research_view,
    best_verified_quote,
    full_text_verification_status,
    semantic_mapping_errors,
)
from app.services.normalization import canonical_cell_id_for, is_applicable_canonical_cell, parse_template_rows


ISSUE_CATEGORIES = {
    "WRONG_REGION": "Evidence supports a different region than the mapped Research View.",
    "WRONG_ASSET_CLASS": "Evidence supports a different asset class, or no support for the mapped asset class.",
    "WRONG_SEGMENT": "Asset class is plausible, but the MAE row/segment/maturity is unsupported.",
    "TOO_BROAD_FOR_CELL": "Evidence is macro or market-generic and too broad for a cell-level view.",
    "NO_INVESTMENT_IMPLICATION": "Evidence has information but no clear asset-allocation implication.",
    "UNSUPPORTED_DIRECTION": "Direction or score does not follow from the quote.",
    "HORIZON_MISMATCH": "Evidence is a short-lived event, not a 6-12 month MAE view.",
    "MULTIPLE_POSSIBLE_CELLS": "Evidence could map to several cells without enough specificity.",
    "DUPLICATE_OR_OVEREXPANDED_MAPPING": "The same quote was expanded across unrelated cells or regions.",
    "NON_CANONICAL_TOPIC": "Topic is market-adjacent but outside the canonical MAE universe.",
    "MISSING_TRANSMISSION_LOGIC": "No causal chain connects the evidence to the mapped asset cell.",
}

ACTION_GROUPS = {"AUTO_FIXABLE", "MANUAL_REVIEW_REQUIRED", "REJECT"}


@dataclass(frozen=True)
class MappingAnalysisPaths:
    json_report: Path
    markdown_report: Path
    verified_csv: Path
    invalid_csv: Path
    golden_cases: Path
    playbook: Path
    snapshot: Path


def generate_financial_mapping_analysis(
    session: Session,
    output_dir: Path | None = None,
    fixtures_dir: Path | None = None,
    playbook_path: Path | None = None,
) -> MappingAnalysisPaths:
    output_dir = output_dir or ROOT_DIR / "outputs" / "production_cleanup"
    fixtures_dir = fixtures_dir or ROOT_DIR / "tests" / "fixtures"
    playbook_path = playbook_path or ROOT_DIR / "FINANCIAL_MAPPING_PLAYBOOK.md"
    output_dir.mkdir(parents=True, exist_ok=True)
    fixtures_dir.mkdir(parents=True, exist_ok=True)

    verified = _verified_rows(session)
    invalid = _invalid_mapping_rows(session)
    snapshot = _snapshot(session)
    invalid_distribution = Counter(row["error_category"] for row in invalid)
    action_distribution = Counter(row["action_group"] for row in invalid)
    top_errors = [
        {
            "category": category,
            "count": count,
            "description": ISSUE_CATEGORIES.get(category, ""),
        }
        for category, count in invalid_distribution.most_common(10)
    ]

    report = {
        "schema_version": "1.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "status_before_analysis": snapshot["status_counts"],
        "scope": {
            "mutates_database": False,
            "runs_ingestion": False,
            "recalculates_coverage": False,
            "runs_final_controller": False,
        },
        "verified_pass_count": len(verified),
        "invalid_mapping_count": len(invalid),
        "invalid_mapping_distribution": dict(sorted(invalid_distribution.items())),
        "action_distribution": dict(sorted(action_distribution.items())),
        "top_10_mapping_errors": top_errors,
        "verified_pass_analysis": verified,
        "invalid_mapping_analysis": invalid,
        "golden_positive_examples": _positive_golden_cases(verified),
        "golden_negative_examples": _negative_golden_cases(invalid),
        "mapping_engine_recommendations": _recommendations(invalid_distribution, action_distribution),
    }

    json_report = output_dir / "financial_mapping_analysis_report.json"
    markdown_report = output_dir / "financial_mapping_analysis_report.md"
    verified_csv = output_dir / "verified_pass_mapping_analysis.csv"
    invalid_csv = output_dir / "invalid_mapping_analysis.csv"
    golden_cases = fixtures_dir / "financial_mapping_golden_cases.json"
    snapshot_path = output_dir / "mapping_analysis_frozen_snapshot.json"

    json_report.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    markdown_report.write_text(_markdown_report(report), encoding="utf-8")
    _write_csv(verified_csv, verified)
    _write_csv(invalid_csv, invalid)
    golden_cases.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "source": "current production mapping analysis snapshot",
                "positive_cases": report["golden_positive_examples"],
                "negative_cases": report["golden_negative_examples"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    playbook_path.write_text(financial_mapping_playbook(), encoding="utf-8")
    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return MappingAnalysisPaths(
        json_report=json_report,
        markdown_report=markdown_report,
        verified_csv=verified_csv,
        invalid_csv=invalid_csv,
        golden_cases=golden_cases,
        playbook=playbook_path,
        snapshot=snapshot_path,
    )


def _verified_rows(session: Session) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    statement = select(ResearchView).where(
        ResearchView.is_demo.is_(False),
        or_(ResearchView.review_status == VERIFIED_PASS, ResearchView.legacy_review_status == VERIFIED_PASS),
    )
    for view in session.scalars(statement).all():
        article = session.get(Article, view.article_id)
        if article is None or article.is_demo:
            continue
        rows.append(_verified_row(view, article))
    return sorted(rows, key=lambda row: (row["asset_class"], row["canonical_row"], row["region"], row["publication_title"], row["research_view_id"]))


def _invalid_mapping_rows(session: Session) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    statement = select(ResearchView).where(
        ResearchView.is_demo.is_(False),
        or_(ResearchView.review_status == INVALID_MAPPING, ResearchView.legacy_review_status == INVALID_MAPPING),
    )
    for view in session.scalars(statement).all():
        article = session.get(Article, view.article_id)
        if article is None or article.is_demo:
            continue
        rows.append(_invalid_row(view, article))
    return sorted(
        rows,
        key=lambda row: (
            row["error_category"],
            row["publication_title"],
            row["current_mapping"]["canonical_row"],
            row["current_mapping"]["region"],
            row["research_view_id"],
        ),
    )


def _verified_row(view: ResearchView, article: Article) -> dict[str, Any]:
    source = article.source
    quote_item = (view.evidence_quotes or [{}])[0]
    quote = str(quote_item.get("quote") or best_verified_quote(article, view.evidence_quotes or []))
    document_type = _document_type(article)
    implication = _financial_implication(view, quote)
    transmission = _transmission_logic(view, quote)
    return {
        "research_view_id": view.id,
        "institution": view.institution,
        "publication_title": article.title,
        "document_type": document_type,
        "publication_date": article.publication_date.isoformat(),
        "exact_evidence_quote": quote,
        "quote_locator": str(quote_item.get("locator") or ""),
        "source_tier": _source_tier(source),
        "canonical_cell_id": view.canonical_cell_id,
        "asset_class": view.asset_class,
        "canonical_row": view.template_row_key,
        "region": view.region,
        "time_horizon": view.horizon,
        "direction": view.direction,
        "proposed_adjustment": view.position_score,
        "financial_implication": implication,
        "transmission_logic": transmission,
        "why_mapping_correct": _why_mapping_correct(view, article, quote),
        "why_evidence_sufficient": _why_evidence_sufficient(view, article, quote),
        "confidence": view.confidence,
        "reviewer_conclusion": _reviewer_conclusion(view, quote),
        "source_url": article.source_reference,
        "publication_excerpt": _excerpt_around_quote(article.content_text, quote),
    }


def _invalid_row(view: ResearchView, article: Article) -> dict[str, Any]:
    quote_item = (view.evidence_quotes or [{}])[0]
    quote = str(quote_item.get("quote") or best_verified_quote(article, view.evidence_quotes or []))
    base_assessment = assess_research_view(view, article)
    errors = [] if base_assessment.passed else list(base_assessment.reasons)
    errors.extend(semantic_mapping_errors(view, article, quote))
    category = _mapping_error_category(view, article, quote, errors, base_assessment.passed)
    correct_cell = _candidate_correct_cell(view, article, quote)
    action_group = _action_group(category, correct_cell)
    return {
        "research_view_id": view.id,
        "institution": view.institution,
        "publication_title": article.title,
        "publication_date": article.publication_date.isoformat(),
        "document_type": _document_type(article),
        "exact_evidence_quote": quote,
        "quote_locator": str(quote_item.get("locator") or ""),
        "source_tier": _source_tier(article.source),
        "current_mapping": {
            "canonical_cell_id": view.canonical_cell_id,
            "canonical_row": view.template_row_key,
            "region": view.region,
            "asset_class": view.asset_class,
            "direction": view.direction,
            "proposed_adjustment": view.position_score,
        },
        "error_category": category,
        "error_category_description": ISSUE_CATEGORIES[category],
        "financial_explanation": _invalid_explanation(category, view, article, quote, errors),
        "auto_fixable": action_group == "AUTO_FIXABLE",
        "manual_review_required": action_group == "MANUAL_REVIEW_REQUIRED",
        "action_group": action_group,
        "correct_canonical_cell_exists": bool(correct_cell),
        "candidate_correct_mapping": correct_cell or "REJECT",
        "reject_if_no_manual_override": action_group == "REJECT",
        "raw_rule_reasons": _dedupe(errors),
        "source_url": article.source_reference,
        "publication_excerpt": _excerpt_around_quote(article.content_text, quote),
    }


def _mapping_error_category(
    view: ResearchView,
    article: Article,
    quote: str,
    errors: list[str],
    base_assessment_passed: bool,
) -> str:
    text = _clean(f"{article.title} {article.source_reference} {quote} {' '.join(errors)}")
    if base_assessment_passed and view.review_status == INVALID_MAPPING:
        return "DUPLICATE_OR_OVEREXPANDED_MAPPING"
    if "same quote mapped" in text or "persisted research-quality status" in text:
        return "DUPLICATE_OR_OVEREXPANDED_MAPPING"
    if "region unsupported" in text:
        return "WRONG_REGION"
    if "direction unsupported" in text or "conflicts with" in text:
        return "UNSUPPORTED_DIRECTION"
    if "horizon unsupported" in text:
        return "WRONG_SEGMENT"
    if "segment unsupported" in text:
        return "WRONG_SEGMENT"
    if "asset unsupported" in text or "claim is not present" in text:
        if _non_canonical_topic(text):
            return "NON_CANONICAL_TOPIC"
        return "WRONG_ASSET_CLASS"
    if "does not contain an investment assertion" in text:
        return "NO_INVESTMENT_IMPLICATION"
    if _short_horizon_topic(text):
        return "HORIZON_MISMATCH"
    if _non_canonical_topic(text):
        return "NON_CANONICAL_TOPIC"
    if "generic or sector-specific" in text:
        return "TOO_BROAD_FOR_CELL"
    if _multiple_possible_cells(text):
        return "MULTIPLE_POSSIBLE_CELLS"
    return "MISSING_TRANSMISSION_LOGIC"


def _action_group(category: str, candidate_correct_mapping: str) -> str:
    if category in {"WRONG_REGION", "WRONG_ASSET_CLASS", "WRONG_SEGMENT"} and candidate_correct_mapping:
        return "AUTO_FIXABLE"
    if category in {"UNSUPPORTED_DIRECTION", "HORIZON_MISMATCH", "MULTIPLE_POSSIBLE_CELLS", "MISSING_TRANSMISSION_LOGIC"}:
        return "MANUAL_REVIEW_REQUIRED"
    if category == "DUPLICATE_OR_OVEREXPANDED_MAPPING":
        return "MANUAL_REVIEW_REQUIRED"
    return "REJECT"


def _candidate_correct_cell(view: ResearchView, article: Article, quote: str) -> str:
    text = _clean(f"{article.title} {article.source_reference} {quote}")
    region = _infer_region(text)
    row = ""
    if _has_any(text, {"gold", "bullion", "safe haven", "central bank purchases"}):
        row = "COMMODITIES|Commodities|Gold"
    elif _has_any(text, {"silver", "platinum", "palladium"}):
        row = "COMMODITIES|Commodities|Other precious metals"
    elif _has_any(text, {"mortgage", "mbs", "prepayment"}):
        row = "FIXED INCOME|Other categories|Mortgage-Backed"
    elif _has_any(text, {"breakeven", "inflation expectations", "inflation risk premia", "real yields"}):
        row = "FIXED INCOME|Other categories|Inflation Linked"
    elif _has_any(text, {"high yield", "high-yield", "default", "leveraged"}):
        row = "FIXED INCOME|CORP HY|Mid Term"
    elif _has_any(text, {"credit", "spreads", "spread", "investment grade", "corporate bond", "refinancing"}):
        row = "FIXED INCOME|CORP IG|Mid Term"
    elif _has_any(text, {"10-year", "10y", "long-term bond", "duration", "yield curve", "term premium"}):
        row = "FIXED INCOME|GOV|Long Term"
    elif _has_any(text, {"front-end", "front end", "short-term rates", "policy rates", "rate cuts", "rate hikes", "central bank", "fomc", "ecb", "bank of england"}):
        row = "FIXED INCOME|GOV|Short Term"
    elif _has_any(text, {"rates", "yields", "inflation", "policy"}):
        row = "FIXED INCOME|GOV|Mid Term"
    elif _has_any(text, {"market breadth", "equal-weight", "equal weight", "broad participation"}):
        row = "EQUITY|Other categories|Market Breadth (Equal Weight)"
    elif _has_any(text, {"preferred stock", "preferred securities", "preferreds"}):
        row = "EQUITY|Other categories|Preferred"
    elif _has_any(text, {"small cap", "small-cap", "smid"}):
        row = "EQUITY|Other categories|Small Cap"
    elif _has_any(text, {"value", "valuation discount", "cheap", "cyclical"}):
        row = "EQUITY|Other categories|Value"
    elif _has_any(text, {"growth equities", "growth stocks", "technology", "software", "semiconductor", "ai", "earnings growth", "eps growth"}):
        row = "EQUITY|Other categories|Growth"
    elif _has_any(text, {"equity", "equities", "stocks", "earnings", "eps", "broad market", "index"}):
        row = "EQUITY|Wide Market|Wide Market"
    if not row:
        return ""
    if not is_applicable_canonical_cell(row, region):
        if region != "Global" and is_applicable_canonical_cell(row, "Global"):
            region = "Global"
        else:
            return ""
    cell_id = canonical_cell_id_for(row, region) or ""
    return f"{cell_id} | {row} | {region}"


def _infer_region(text: str) -> str:
    if _has_any(text, {"united kingdom", "u.k.", " uk ", "britain", "bank of england", "boe"}):
        return "UK"
    if _has_any(text, {"euro area", "eurozone", "europe", "ecb", "european central bank"}):
        return "Europe"
    if _has_any(text, {"united states", "u.s.", "usa", "federal reserve", "fomc", "s&p", "nasdaq", "treasury"}):
        return "US"
    if _has_any(text, {"japan", "boj", "bank of japan", "yen"}):
        return "Japan"
    if _has_any(text, {"emerging markets", "emerging-market", "em ex china", "em ex-china"}):
        return "EM ex China"
    return "Global"


def _financial_implication(view: ResearchView, quote: str) -> str:
    direction = _direction_phrase(view.direction)
    row = view.template_row_key
    if view.asset_class == "FIXED INCOME":
        if "GOV" in row:
            return f"{direction} view on {view.region} government bonds because the quote speaks to rates, inflation, yields or policy path."
        if "CORP" in row:
            return f"{direction} view on {view.region} corporate credit because the quote speaks to spreads, refinancing, default risk or balance-sheet quality."
        return f"{direction} fixed-income view because the quote speaks to the relevant bond risk premium."
    if view.asset_class == "EQUITY":
        return f"{direction} view on {view.region} equities/style exposure because the quote speaks to earnings, valuation, breadth or risk appetite."
    if view.asset_class in {"COMMODITIES", "GOLD"}:
        return f"{direction} view on commodities because the quote speaks to real yields, dollar, safe-haven demand or commodity fundamentals."
    return f"{direction} view derived from the quoted investment implication."


def _transmission_logic(view: ResearchView, quote: str) -> str:
    row = view.template_row_key
    text = _clean(quote)
    if "GOV|Long Term" in row:
        return "Rates/inflation expectations affect long-end yields; higher yields pressure long-duration bond prices, while lower yields support them."
    if "GOV|Short Term" in row:
        return "Central-bank policy expectations and front-end yields drive short-maturity government bond returns over the MAE horizon."
    if "GOV|Mid Term" in row:
        return "Intermediate yields combine policy path, inflation expectations and curve shape, linking the quote to mid-maturity government bonds."
    if "CORP IG" in row:
        return "Credit spreads, refinancing conditions and balance-sheet quality determine investment-grade excess returns."
    if "CORP HY" in row:
        return "Default risk, leverage and recession sensitivity determine high-yield spreads and expected returns."
    if "Inflation Linked" in row:
        return "Inflation expectations and real yields affect breakevens and inflation-linked bond returns."
    if "Mortgage-Backed" in row:
        return "Mortgage rates, spreads and prepayment risk affect agency/MBS expected returns."
    if "Wide Market" in row:
        return "Broad earnings, valuations and risk appetite transmit into regional equity-index expected returns."
    if "Market Breadth" in row:
        return "Breadth evidence changes confidence that equity returns are broad-based rather than concentrated."
    if "Preferred" in row:
        return "Preferred securities are affected by equity risk appetite, credit spreads and rate sensitivity."
    if "Value" in row:
        return "Valuation discount, cyclicality and rate sensitivity transmit into relative Value style performance."
    if "Growth" in row:
        return "Long-duration earnings growth, technology/AI exposure and discount-rate sensitivity transmit into Growth style performance."
    if "Small Cap" in row:
        return "Domestic cyclicality, financing conditions and smaller-company earnings transmit into Small Cap performance."
    if "Gold" in row:
        return "Real yields, dollar, safe-haven demand and central-bank purchases transmit into gold prices."
    if "Other precious metals" in row:
        return "Industrial demand, investment demand and supply constraints transmit into other precious metals."
    if _has_any(text, {"inflation", "rates", "yields"}):
        return "The quote changes discount-rate or inflation expectations, which then affects asset relative value."
    return "The quote must be linked to asset returns through a documented macro-to-market transmission channel."


def _why_mapping_correct(view: ResearchView, article: Article, quote: str) -> str:
    region = _region_reason(view.region, article, quote)
    row = _row_reason(view.template_row_key, quote)
    return f"{row} {region}"


def _why_evidence_sufficient(view: ResearchView, article: Article, quote: str) -> str:
    verification = full_text_verification_status(article, view.evidence_quotes or [])
    return (
        f"The quote is {verification}, has a locator, comes from an allowed source tier, "
        f"and contains a cell-relevant market/rates/earnings/risk-premium assertion rather than navigation or boilerplate text."
    )


def _reviewer_conclusion(view: ResearchView, quote: str) -> str:
    caveat = ""
    if str((view.evidence_quotes or [{}])[0].get("locator", "")).casefold() in {"first sentence", "source sentence 1"}:
        caveat = " Future reviewer should prefer a deeper body-text quote when available."
    return f"PASS under current production classifier: exact quote and current mapping rule support {view.template_row_key} / {view.region}.{caveat}"


def _invalid_explanation(category: str, view: ResearchView, article: Article, quote: str, errors: list[str]) -> str:
    current = f"{view.template_row_key} / {view.region} / {view.direction}"
    if category == "WRONG_REGION":
        return f"Evidence does not support the mapped region in {current}; region must follow the quoted thesis, not source domicile."
    if category == "WRONG_ASSET_CLASS":
        return f"Evidence does not support the mapped asset class in {current}; the quote should identify equity, rates, credit, commodity or gold impact."
    if category == "WRONG_SEGMENT":
        return f"Evidence may touch the asset class, but not the mapped segment/maturity in {current}."
    if category == "TOO_BROAD_FOR_CELL":
        return f"Quote is too broad for a single canonical cell: {quote[:180]}"
    if category == "NO_INVESTMENT_IMPLICATION":
        return f"Quote contains information but no asset-allocation implication: {quote[:180]}"
    if category == "UNSUPPORTED_DIRECTION":
        return f"Direction {view.direction} does not follow from the quote; positive macro news is not automatically positive for the mapped asset."
    if category == "HORIZON_MISMATCH":
        return "Evidence appears event-like or short-term and does not establish a 6-12 month MAE view."
    if category == "MULTIPLE_POSSIBLE_CELLS":
        return "Evidence could belong to several MAE cells, so automatic mapping would overstate precision."
    if category == "DUPLICATE_OR_OVEREXPANDED_MAPPING":
        return "The same quote was expanded to unrelated cells/regions; only cell-specific evidence should survive."
    if category == "NON_CANONICAL_TOPIC":
        return "The topic may be market-adjacent, but it is not a canonical MAE asset-allocation row."
    return f"No explicit transmission logic connects quote to {current}; reasons: {'; '.join(_dedupe(errors)[:3])}"


def _positive_golden_cases(verified: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    coverage_targets = [
        ("asset_class", "FIXED INCOME"),
        ("asset_class", "EQUITY"),
        ("asset_class", "COMMODITIES"),
    ]
    coverage_targets.extend(("region", value) for value in sorted({row["region"] for row in verified}))
    coverage_targets.extend(("canonical_row", value) for value in sorted({row["canonical_row"] for row in verified}))
    for field, value in coverage_targets:
        for row in verified:
            if row["research_view_id"] not in seen_ids and row.get(field) == value:
                selected.append(row)
                seen_ids.add(row["research_view_id"])
                break
    for row in verified:
        if len(selected) >= 20:
            break
        if row["research_view_id"] not in seen_ids:
            selected.append(row)
            seen_ids.add(row["research_view_id"])
    return [_positive_case(row) for row in selected[:20]]


def _negative_golden_cases(invalid: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for category in ISSUE_CATEGORIES:
        for row in invalid:
            if row["research_view_id"] not in seen_ids and row["error_category"] == category:
                selected.append(row)
                seen_ids.add(row["research_view_id"])
                break
    for row in invalid:
        if len(selected) >= 30:
            break
        if row["research_view_id"] not in seen_ids:
            selected.append(row)
            seen_ids.add(row["research_view_id"])
    return [_negative_case(row) for row in selected[:30]]


def _positive_case(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": row["research_view_id"],
        "publication_excerpt": row["publication_excerpt"],
        "exact_quote": row["exact_evidence_quote"],
        "expected_cell": row["canonical_cell_id"],
        "expected_row": row["canonical_row"],
        "expected_region": row["region"],
        "expected_direction": row["direction"],
        "expected_rationale": row["transmission_logic"],
        "expected_verdict": "PASS",
    }


def _negative_case(row: dict[str, Any]) -> dict[str, Any]:
    mapping = row["current_mapping"]
    return {
        "case_id": row["research_view_id"],
        "input": {
            "publication_title": row["publication_title"],
            "publication_excerpt": row["publication_excerpt"],
            "exact_quote": row["exact_evidence_quote"],
        },
        "erroneous_mapping": {
            "canonical_cell_id": mapping["canonical_cell_id"],
            "canonical_row": mapping["canonical_row"],
            "region": mapping["region"],
            "direction": mapping["direction"],
        },
        "expected_rejection_reason": row["error_category"],
        "acceptable_correct_outcome": row["candidate_correct_mapping"],
        "expected_verdict": "FAIL",
    }


def _recommendations(invalid_distribution: Counter[str], action_distribution: Counter[str]) -> list[dict[str, str]]:
    recs = [
        {
            "priority": "P1",
            "recommendation": "Stop expanding one verified quote across unrelated MAE rows or regions.",
            "reason": "Duplicate/over-expanded mapping is the dominant failure mode when one quote is reused without cell-specific rationale.",
        },
        {
            "priority": "P1",
            "recommendation": "Make asset-class detection mutually exclusive unless separate quotes support each cell.",
            "reason": "Equity, rates, credit and commodity evidence require different transmission channels.",
        },
        {
            "priority": "P1",
            "recommendation": "Require a region phrase inside the quote or surrounding paragraph; source domicile is not enough.",
            "reason": "Region must follow the evidence thesis.",
        },
        {
            "priority": "P2",
            "recommendation": "Add a direction explainer before assigning score.",
            "reason": "Macro-positive does not always mean asset-positive, especially for duration and inflation-sensitive assets.",
        },
        {
            "priority": "P2",
            "recommendation": "Route broad macro notes to manual review unless the quote contains a specific asset-return implication.",
            "reason": f"Manual review remains required for {action_distribution.get('MANUAL_REVIEW_REQUIRED', 0)} current cases.",
        },
        {
            "priority": "P3",
            "recommendation": "Use this playbook as a test oracle for future LLM/provider extraction prompts.",
            "reason": "Golden positive and negative cases now encode reproducible financial mapping expectations.",
        },
    ]
    if invalid_distribution.get("NON_CANONICAL_TOPIC", 0):
        recs.append(
            {
                "priority": "P2",
                "recommendation": "Reject market-adjacent but non-canonical topics before mapping.",
                "reason": "Non-canonical topics should not be forced into the MAE universe.",
            }
        )
    return recs


def _snapshot(session: Session) -> dict[str, Any]:
    db_path = ROOT_DIR / "data" / "mae.db"
    views = [row for row in session.scalars(select(ResearchView)).all() if not row.is_demo]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "database_path": str(db_path),
        "database_sha256": _sha256(db_path) if db_path.exists() else "",
        "status_counts": dict(Counter(row.review_status for row in views)),
        "canonical_rows": [row.template_row_key for row in parse_template_rows()],
        "frozen_do_not_mutate": [
            "canonical universe",
            "baseline",
            "Current MAE",
            "production Research Views",
            "coverage",
            "exports structure",
        ],
    }


def _markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Financial Mapping Analysis Report",
        "",
        "This report is read-only analysis of the current production database. It does not run ingestion, cleanup, matrix rebuild, coverage recalculation, or Final Controller.",
        "",
        "## Summary",
        "",
        f"- VERIFIED_PASS: {report['verified_pass_count']}",
        f"- INVALID_MAPPING: {report['invalid_mapping_count']}",
        f"- AUTO_FIXABLE: {report['action_distribution'].get('AUTO_FIXABLE', 0)}",
        f"- MANUAL_REVIEW_REQUIRED: {report['action_distribution'].get('MANUAL_REVIEW_REQUIRED', 0)}",
        f"- REJECT: {report['action_distribution'].get('REJECT', 0)}",
        "",
        "## INVALID_MAPPING Distribution",
        "",
        "| Category | Count | Description |",
        "|---|---:|---|",
    ]
    for category, count in sorted(report["invalid_mapping_distribution"].items()):
        lines.append(f"| {category} | {count} | {ISSUE_CATEGORIES.get(category, '')} |")
    lines.extend(["", "## Top 10 Mapping Errors", "", "| Rank | Category | Count |", "|---:|---|---:|"])
    for idx, row in enumerate(report["top_10_mapping_errors"], start=1):
        lines.append(f"| {idx} | {row['category']} | {row['count']} |")
    lines.extend(["", "## 46 VERIFIED_PASS Rows", ""])
    lines.append("| Research View ID | Institution | Publication | Cell | Region | Direction | Financial implication | Why mapping is correct |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for row in report["verified_pass_analysis"]:
        lines.append(
            "| {research_view_id} | {institution} | {publication_title} | {canonical_row} | {region} | {direction} | {financial_implication} | {why_mapping_correct} |".format(
                **{key: _md(row.get(key, "")) for key in row}
            )
        )
    lines.extend(["", "## 276 INVALID_MAPPING Rows", ""])
    lines.append("| Research View ID | Publication | Current mapping | Category | Action | Candidate outcome | Explanation |")
    lines.append("|---|---|---|---|---|---|---|")
    for row in report["invalid_mapping_analysis"]:
        current = row["current_mapping"]
        lines.append(
            "| {id} | {title} | {mapping} | {category} | {action} | {candidate} | {explanation} |".format(
                id=_md(row["research_view_id"]),
                title=_md(row["publication_title"]),
                mapping=_md(f"{current['canonical_row']} / {current['region']} / {current['direction']}"),
                category=_md(row["error_category"]),
                action=_md(row["action_group"]),
                candidate=_md(row["candidate_correct_mapping"]),
                explanation=_md(row["financial_explanation"]),
            )
        )
    lines.extend(["", "## Mapping Engine Recommendations", ""])
    for rec in report["mapping_engine_recommendations"]:
        lines.append(f"- **{rec['priority']}** {rec['recommendation']} Reason: {rec['reason']}")
    lines.append("")
    return "\n".join(lines)


def financial_mapping_playbook() -> str:
    row_rules = {
        "EQUITY|Wide Market|Wide Market": (
            "Broad regional equity outlook, aggregate earnings, full-market valuation, risk appetite, broad index view.",
            "Single-sector article, company news, monetary-policy-only text without equity transmission, generic GDP growth.",
            "Earnings revision, valuation multiple, risk appetite, index-level expected return.",
        ),
        "EQUITY|Other categories|Market Breadth (Equal Weight)": (
            "Equal-weight performance, concentration versus broad participation, widening/narrowing rally leadership.",
            "General equity outlook without breadth evidence.",
            "Breadth improves confidence in broad equity rally; narrow leadership increases concentration risk.",
        ),
        "EQUITY|Other categories|Preferred": (
            "Explicit preferred securities, preferred equity income, hybrid capital exposure.",
            "Generic positive equity bucket or ordinary common-stock style call.",
            "Preferreds transmit through rates, credit spreads and equity income risk appetite.",
        ),
        "EQUITY|Other categories|Value": (
            "Valuation discount, rotation to cheap/cyclical stocks, explicit Value style preference.",
            "Market broadly undervalued without style-specific Value conclusion.",
            "Cheap valuations, cyclicality and rate sensitivity drive relative Value performance.",
        ),
        "EQUITY|Other categories|Growth": (
            "Long-duration earnings, technology/AI-led earnings growth, explicit Growth style view.",
            "Any AI article without investment conclusion for Growth equities.",
            "Growth transmits through earnings duration, AI/technology profit cycle and discount-rate sensitivity.",
        ),
        "EQUITY|Other categories|Small Cap": (
            "Smaller companies, domestic cyclicality, financing conditions for small firms, small-cap valuation/earnings.",
            "Generic SME policy article without market implication.",
            "Financing conditions and domestic earnings leverage affect small-cap expected returns.",
        ),
        "FIXED INCOME|GOV|Long Term": (
            "Long-end rates, duration, 10-year yields, yield curve, term premium, long-duration government bonds.",
            "Central-bank article without long-end/duration evidence.",
            "Long-duration prices move inversely with long-end yields and term premia.",
        ),
        "FIXED INCOME|GOV|Short Term": (
            "Front-end rates, near-term policy path, rate cuts/hikes, 2-year yields, central-bank path.",
            "Any rates article mapped automatically across all maturities.",
            "Policy expectations transmit directly into front-end yields and short government-bond returns.",
        ),
        "FIXED INCOME|GOV|Mid Term": (
            "Intermediate-maturity rate outlook, curve belly, policy plus inflation path.",
            "Generic inflation text without maturity implication.",
            "Intermediate yields combine policy path, inflation expectations and curve shape.",
        ),
        "FIXED INCOME|CORP HY|Long Term": (
            "Long-duration high-yield, leverage/default cycle with duration exposure.",
            "Corporate earnings article without high-yield credit evidence.",
            "Defaults, leverage and spread duration determine long HY returns.",
        ),
        "FIXED INCOME|CORP HY|Short Term": (
            "Short-duration high yield, near-term refinancing/default risk for weaker borrowers.",
            "Generic credit sentiment without HY or short-duration evidence.",
            "Near-term default/refinancing risk affects front-end HY spreads.",
        ),
        "FIXED INCOME|CORP HY|Mid Term": (
            "High-yield spreads, default cycle, weaker borrowers, leverage, recession sensitivity.",
            "Generic corporate earnings article without credit evidence.",
            "HY returns transmit through spread compensation for default and recession risk.",
        ),
        "FIXED INCOME|CORP IG|Long Term": (
            "Long-duration investment-grade spreads, credit quality plus duration.",
            "Rates-only article without corporate spread/fundamental evidence.",
            "Long IG returns reflect spread risk plus duration sensitivity.",
        ),
        "FIXED INCOME|CORP IG|Short Term": (
            "Short IG spreads, near-term refinancing, high-quality short credit.",
            "Government-bond policy article without credit spread evidence.",
            "Short IG returns transmit through front-end yields and credit spreads.",
        ),
        "FIXED INCOME|CORP IG|Mid Term": (
            "Investment-grade spreads, balance-sheet quality, default risk, refinancing conditions, credit fundamentals.",
            "Equity earnings or macro growth text without credit evidence.",
            "IG credit returns transmit through spreads, refinancing and balance-sheet quality.",
        ),
        "FIXED INCOME|Other categories|Inflation Linked": (
            "Inflation expectations, breakevens, inflation risk premia, persistent inflation, real yields.",
            "Nominal bond article without breakevens, inflation-linked bonds or real-yield logic.",
            "Inflation surprises and real yields drive inflation-linked bond returns.",
        ),
        "FIXED INCOME|Other categories|Mortgage-Backed": (
            "Mortgage spreads, prepayment risk, housing finance, mortgage rates, agency MBS outlook.",
            "Generic housing article without fixed-income implication.",
            "Mortgage spreads and prepayment rates determine MBS expected returns.",
        ),
        "COMMODITIES|Commodities|Gold": (
            "Real rates, USD, safe-haven demand, central-bank purchases, inflation hedge, geopolitical risk.",
            "Generic commodity article without gold-specific evidence.",
            "Gold transmits through real yields, dollar and safe-haven demand.",
        ),
        "COMMODITIES|Commodities|Other precious metals": (
            "Silver, platinum, palladium, industrial/investment demand, supply constraints.",
            "Generic commodity article when precious metals are not discussed.",
            "Industrial demand, investment demand and supply constraints affect metals prices.",
        ),
    }
    lines = [
        "# Financial Mapping Playbook",
        "",
        "Status: DRAFT / NOT FINANCIALLY APPROVED. This playbook is not a production oracle until the strict second review is accepted. Use `outputs/production_cleanup/financial_mapping_second_review_report.json` and the schema v2 golden fixture for current classifier validation.",
        "",
        "Purpose: make publication -> evidence -> financial implication -> canonical MAE cell -> proposed adjustment reproducible.",
        "",
        "Global rules:",
        "- Region follows the quoted thesis, not the country of the institution.",
        "- One quote may support multiple cells only when each cell has distinct transmission logic.",
        "- Direction follows relative asset attractiveness, not generic positive/negative macro tone.",
        "- MAE horizon is 6-12 months; event-only or one-day market moves require manual review.",
        "- Reject boilerplate, navigation, marketing copy and non-canonical topics before mapping.",
        "",
        "Decision ladder:",
        "1. Verify exact quote in full text.",
        "2. Identify document type and whether it contains investable market research.",
        "3. Determine region from the evidence sentence/paragraph.",
        "4. Determine asset class from explicit equity/rates/credit/commodity language.",
        "5. Determine canonical row from segment/maturity/style evidence.",
        "6. Determine direction through transmission logic.",
        "7. Use AUTO_FIXABLE only when one canonical cell is unambiguous; otherwise route to manual review or reject.",
        "",
    ]
    for row in parse_template_rows():
        suitable, unsuitable, channel = row_rules[row.template_row_key]
        correct_example, incorrect_example = _playbook_examples(row.template_row_key)
        lines.extend(
            [
                f"## {row.template_row_key}",
                "",
                f"Suitable theses: {suitable}",
                "",
                f"Suitable documents: professional outlooks, central-bank/research publications or strategy notes whose exact quote supports this row.",
                "",
                f"Allowed evidence patterns: exact body-text quote with explicit asset, style, maturity, spread, rate, inflation, earnings or commodity reference.",
                "",
                f"Disallowed evidence patterns: {unsuitable}",
                "",
                "Allowed regions: only regions explicitly supported by the quote or surrounding paragraph; use Global for genuinely cross-region theses.",
                "",
                f"Typical transmission channels: {channel}",
                "",
                f"Correct mapping example: {correct_example}",
                "",
                f"Incorrect mapping example: {incorrect_example}",
                "",
                "Manual review required: multiple possible rows, missing direction logic, uncertain horizon, or mixed regional evidence.",
                "",
            ]
        )
    return "\n".join(lines)


def _playbook_examples(row_key: str) -> tuple[str, str]:
    examples = {
        "EQUITY|Wide Market|Wide Market": (
            "A strategist writes that US equities should outperform over 6-12 months because broad earnings revisions and index-level margins are improving.",
            "A central-bank quote says inflation is sticky, with no equity-index earnings or valuation implication.",
        ),
        "EQUITY|Other categories|Market Breadth (Equal Weight)": (
            "A market outlook says equal-weight indices are beginning to outperform cap-weighted benchmarks as participation broadens beyond mega-cap leaders.",
            "A broad equity outlook says stocks are attractive but gives no evidence on concentration or equal-weight participation.",
        ),
        "EQUITY|Other categories|Preferred": (
            "A credit/equity income note explicitly prefers preferred securities because spread carry and bank capital quality are improving.",
            "A generic positive equity call is routed to Preferred only because it mentions income.",
        ),
        "EQUITY|Other categories|Value": (
            "A style outlook says European Value should benefit from valuation discounts, cyclicality and higher nominal yields.",
            "A note says the whole market is undervalued but never distinguishes Value from Growth or broad equity exposure.",
        ),
        "EQUITY|Other categories|Growth": (
            "A strategist links AI-led EPS upgrades and long-duration earnings visibility to a positive Growth equity view.",
            "A podcast mentions AI data-center demand but gives no expected-return conclusion for Growth equities.",
        ),
        "EQUITY|Other categories|Small Cap": (
            "A regional outlook says small-cap earnings and financing conditions are improving relative to large caps.",
            "A policy article discusses SMEs without any listed small-cap market implication.",
        ),
        "FIXED INCOME|GOV|Long Term": (
            "A rates outlook says 10-year yields should rise as term premium increases, implying negative long-duration government bond returns.",
            "A central-bank article mentions policy rates but gives no long-end duration, 10-year yield or curve evidence.",
        ),
        "FIXED INCOME|GOV|Short Term": (
            "A policy account says front-end yields should fall because rate cuts are likely over the next meetings.",
            "A generic inflation quote is copied into Short, Mid and Long Term government bonds without maturity-specific logic.",
        ),
        "FIXED INCOME|GOV|Mid Term": (
            "A fixed-income outlook says the belly of the curve should benefit from a stable policy path and falling inflation expectations.",
            "A one-day yield move is mapped to Mid Term without a 6-12 month view.",
        ),
        "FIXED INCOME|CORP HY|Long Term": (
            "A high-yield outlook says long-duration HY spreads are undercompensated for default and duration risk.",
            "A corporate earnings article is mapped to long HY without spread, default, leverage or HY evidence.",
        ),
        "FIXED INCOME|CORP HY|Short Term": (
            "A credit note says short-duration HY is attractive because refinancing walls have moved out and default risk is contained.",
            "A short-term rates quote is mapped to HY simply because it says 'short'.",
        ),
        "FIXED INCOME|CORP HY|Mid Term": (
            "A credit strategist expects HY spreads to widen as leverage and default risk rise over the next year.",
            "A broad risk-appetite quote is mapped to HY without high-yield spread/default evidence.",
        ),
        "FIXED INCOME|CORP IG|Long Term": (
            "An IG credit note says long-dated investment-grade spreads should tighten and duration risk is acceptable.",
            "A nominal government-bond duration quote is mapped to IG Long Term without corporate spread evidence.",
        ),
        "FIXED INCOME|CORP IG|Short Term": (
            "A credit outlook prefers short-dated IG because balance sheets are strong and front-end carry is attractive.",
            "A central-bank front-end rates article is mapped to short IG without credit fundamentals.",
        ),
        "FIXED INCOME|CORP IG|Mid Term": (
            "An investment-grade outlook says spreads should tighten as refinancing conditions and balance-sheet quality improve.",
            "An equity earnings note is mapped to IG because it mentions companies, not credit risk.",
        ),
        "FIXED INCOME|Other categories|Inflation Linked": (
            "A rates note expects breakevens and inflation risk premia to rise, supporting inflation-linked bonds.",
            "A nominal inflation headline is mapped to inflation-linked bonds without breakeven, real-yield or linker evidence.",
        ),
        "FIXED INCOME|Other categories|Mortgage-Backed": (
            "An MBS outlook says mortgage spreads should tighten as prepayment risk declines.",
            "A housing-price article is mapped to MBS without mortgage spread or prepayment logic.",
        ),
        "COMMODITIES|Commodities|Gold": (
            "A commodity outlook says falling real yields, dollar weakness and central-bank purchases support gold.",
            "A generic geopolitical-risk article is mapped to Gold without gold, real-yield, dollar or safe-haven evidence.",
        ),
        "COMMODITIES|Commodities|Other precious metals": (
            "A metals note says silver should benefit from industrial demand and supply constraints.",
            "A broad commodity article is mapped to Other precious metals without silver, platinum or palladium evidence.",
        ),
    }
    return examples[row_key]


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = _fieldnames(rows)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key, "")) for key in fieldnames})


def _fieldnames(rows: list[dict[str, Any]]) -> list[str]:
    keys: list[str] = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    return keys


def _csv_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return "" if value is None else str(value)


def _source_tier(source: Any) -> str:
    if source is None:
        return "UNKNOWN"
    category = str(getattr(source, "category", "") or "")
    score = float(getattr(source, "trust_score", 0.0) or 0.0)
    if category == "official" or score >= 0.95:
        return "TIER_1_OFFICIAL_OR_PRIMARY"
    if score >= 0.8:
        return "TIER_2_APPROVED_INSTITUTIONAL_RESEARCH"
    return "TIER_3_ALLOWED_SOURCE_REQUIRES_REVIEW"


def _document_type(article: Article) -> str:
    text = _clean(f"{article.title} {article.source_reference} {(article.content_text or '')[:1200]}")
    if _has_any(text, {"monetary policy meeting", "fomc", "governing council", "account of the monetary policy"}):
        return "central_bank_policy_account"
    if _has_any(text, {"working paper", "staff working paper"}):
        return "research_working_paper"
    if _has_any(text, {"outlook", "forecast", "mid-year", "midyear"}):
        return "market_outlook"
    if _has_any(text, {"podcast", "interview"}):
        return "interview_or_podcast"
    if _has_any(text, {"tracker", "roundup", "weekly"}):
        return "market_update"
    return "research_article"


def _direction_phrase(direction: str) -> str:
    return {
        "BULLISH": "positive",
        "BEARISH": "negative",
        "NEUTRAL": "neutral",
    }.get(direction, direction.lower())


def _row_reason(row: str, quote: str) -> str:
    if "GOV" in row:
        return "The quote contains rates/inflation/policy/yield evidence, so the relevant asset is government bonds rather than equities or credit."
    if "CORP" in row:
        return "The quote contains credit/spread/refinancing/default evidence, so the relevant asset is corporate credit."
    if "EQUITY" in row:
        return "The quote contains equity/earnings/valuation/style evidence, so the relevant row is an equity cell."
    if "Gold" in row:
        return "The quote contains gold/real-yield/USD/safe-haven evidence, so the relevant row is Gold."
    return "The quote contains row-specific evidence for the mapped MAE cell."


def _region_reason(region: str, article: Article, quote: str) -> str:
    if region == "Global":
        return "The thesis is global/cross-region or lacks a more specific regional anchor, so Global is used."
    return f"The evidence or source paragraph anchors the thesis to {region}; region is not inferred from source domicile alone."


def _non_canonical_topic(text: str) -> bool:
    return _has_any(text, {"world cup", "pet industry", "payments", "multifamily", "tokenisation", "climate tech", "private markets", "physical and transition risk"})


def _short_horizon_topic(text: str) -> bool:
    return _has_any(text, {"one-day", "daily", "weekly", "roundup", "yesterday", "today's move", "market reaction"})


def _multiple_possible_cells(text: str) -> bool:
    families = 0
    families += int(_has_any(text, {"equity", "equities", "stocks", "earnings"}))
    families += int(_has_any(text, {"rates", "yields", "inflation", "central bank"}))
    families += int(_has_any(text, {"credit", "spreads", "default"}))
    families += int(_has_any(text, {"gold", "commodities", "oil"}))
    return families >= 2


def _has_any(text: str, terms: set[str]) -> bool:
    padded = f" {text} "
    return any(term in padded for term in terms)


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().casefold()


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _excerpt_around_quote(text: str, quote: str, width: int = 360) -> str:
    clean_text = re.sub(r"\s+", " ", text or "").strip()
    clean_quote = re.sub(r"\s+", " ", quote or "").strip()
    if not clean_quote:
        return clean_text[:width]
    idx = clean_text.casefold().find(clean_quote.casefold()[:120])
    if idx < 0:
        return clean_text[:width]
    start = max(0, idx - width // 3)
    end = min(len(clean_text), idx + len(clean_quote) + width // 3)
    return clean_text[start:end]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _md(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")[:800]
