from __future__ import annotations

import csv
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
from app.services.mapping_analysis import (
    ISSUE_CATEGORIES,
    _candidate_correct_cell,
    _csv_value,
    _document_type,
    _excerpt_around_quote,
    _fieldnames,
    _invalid_mapping_rows,
    _md,
    _snapshot,
    _source_tier,
)
from app.services.methodology import INVALID_MAPPING, VERIFIED_PASS, best_verified_quote
from app.services.normalization import canonical_cell_id_for, is_applicable_canonical_cell


SECOND_REVIEW_STATUSES = {
    "TRUE_PASS",
    "WRONG_REGION",
    "WRONG_ASSET_CLASS",
    "WRONG_SEGMENT",
    "UNSUPPORTED_DIRECTION",
    "INSUFFICIENT_EVIDENCE",
    "TOO_BROAD_FOR_CELL",
    "DUPLICATE_MAPPING",
    "IRRELEVANT_DOCUMENT",
    "MANUAL_REVIEW_REQUIRED",
}

DOWNGRADED_STATUSES = SECOND_REVIEW_STATUSES - {"TRUE_PASS"}


@dataclass(frozen=True)
class SecondReviewPaths:
    json_report: Path
    markdown_report: Path
    verified_csv: Path
    invalid_actions_csv: Path
    golden_cases: Path
    strict_golden_cases: Path


def generate_financial_mapping_second_review(
    session: Session,
    output_dir: Path | None = None,
    fixtures_dir: Path | None = None,
) -> SecondReviewPaths:
    """Read-only financial second review of current VERIFIED_PASS/INVALID_MAPPING rows."""
    output_dir = output_dir or ROOT_DIR / "outputs" / "production_cleanup"
    fixtures_dir = fixtures_dir or ROOT_DIR / "tests" / "fixtures"
    output_dir.mkdir(parents=True, exist_ok=True)
    fixtures_dir.mkdir(parents=True, exist_ok=True)

    previously_verified = _second_review_verified_rows(session)
    invalid_actions = _second_review_invalid_actions(session)
    snapshot = _snapshot(session)

    status_distribution = Counter(row["second_review_status"] for row in previously_verified)
    downgraded_distribution = Counter(
        row["second_review_status"] for row in previously_verified if row["second_review_status"] != "TRUE_PASS"
    )
    revised_action_distribution = Counter(row["revised_action_group"] for row in invalid_actions)
    true_pass_cases = [row for row in previously_verified if row["second_review_status"] == "TRUE_PASS"]
    manual_cases = [
        row for row in previously_verified if row["second_review_status"] == "MANUAL_REVIEW_REQUIRED"
    ]
    rejected_cases = [
        row
        for row in previously_verified
        if row["second_review_status"] in {"IRRELEVANT_DOCUMENT", "INSUFFICIENT_EVIDENCE", "TOO_BROAD_FOR_CELL"}
    ]
    downgraded_cases = [
        row for row in previously_verified if row["second_review_status"] != "TRUE_PASS"
    ]

    report = {
        "schema_version": "2.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "status_before_analysis": snapshot["status_counts"],
        "scope": {
            "mutates_database": False,
            "runs_ingestion": False,
            "recalculates_coverage": False,
            "runs_final_controller": False,
            "changes_production_research_views": False,
        },
        "strict_review_rule": "A previous VERIFIED_PASS becomes TRUE_PASS only when asset class, canonical row, region, direction, and 6-12 month investment implication are all explicitly supported by the quote and context.",
        "previous_verified_pass_count": len(previously_verified),
        "true_pass_count": len(true_pass_cases),
        "downgraded_count": len(downgraded_cases),
        "second_review_status_distribution": dict(sorted(status_distribution.items())),
        "downgraded_distribution": dict(sorted(downgraded_distribution.items())),
        "previously_verified_second_review": previously_verified,
        "revised_golden_positive_cases": [_positive_case_from_true_pass(row) for row in true_pass_cases],
        "ambiguous_manual_cases": [_manual_case(row) for row in manual_cases],
        "rejected_cases": [_rejected_case(row) for row in rejected_cases],
        "downgraded_cases": downgraded_cases,
        "invalid_mapping_second_review_count": len(invalid_actions),
        "revised_invalid_action_distribution": dict(sorted(revised_action_distribution.items())),
        "invalid_mapping_second_review_actions": invalid_actions,
        "false_positive_classifier_rules": _false_positive_classifier_rules(previously_verified),
        "classifier_fix_recommendations": _classifier_fix_recommendations(previously_verified, invalid_actions),
    }

    json_report = output_dir / "financial_mapping_second_review_report.json"
    markdown_report = output_dir / "financial_mapping_second_review_report.md"
    verified_csv = output_dir / "verified_pass_second_review.csv"
    invalid_actions_csv = output_dir / "invalid_mapping_second_review_actions.csv"
    golden_cases = fixtures_dir / "financial_mapping_golden_cases.json"
    strict_golden_cases = fixtures_dir / "financial_mapping_second_review_golden_cases.json"

    json_report.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    markdown_report.write_text(_second_review_markdown(report), encoding="utf-8")
    _write_csv(verified_csv, previously_verified)
    _write_csv(invalid_actions_csv, invalid_actions)

    fixture_payload = _strict_fixture_payload(report)
    golden_cases.write_text(json.dumps(fixture_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    strict_golden_cases.write_text(json.dumps(fixture_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    return SecondReviewPaths(
        json_report=json_report,
        markdown_report=markdown_report,
        verified_csv=verified_csv,
        invalid_actions_csv=invalid_actions_csv,
        golden_cases=golden_cases,
        strict_golden_cases=strict_golden_cases,
    )


def strict_review_research_view(view: ResearchView, article: Article) -> dict[str, Any]:
    quote_item = (view.evidence_quotes or [{}])[0]
    quote = str(quote_item.get("quote") or best_verified_quote(article, view.evidence_quotes or []))
    context = _context(article, quote)
    answers = _five_question_answers(view, article, quote, context)
    status, primary_reason, secondary_reasons = _second_review_status(view, article, quote, context, answers)
    candidate = _strict_candidate_mapping(view, article, quote, context)
    explanation = _individual_explanation(view, article, quote, answers, status, primary_reason, candidate)
    return {
        "research_view_id": view.id,
        "institution": view.institution,
        "publication_title": article.title,
        "document_type": _document_type(article),
        "publication_date": article.publication_date.isoformat(),
        "exact_evidence_quote": quote,
        "quote_locator": str(quote_item.get("locator") or ""),
        "source_tier": _source_tier(article.source),
        "source_url": article.source_reference,
        "previous_status": view.review_status,
        "second_review_status": status,
        "primary_failure_reason": primary_reason,
        "secondary_failure_reasons": secondary_reasons,
        "canonical_cell_id": view.canonical_cell_id,
        "asset_class": view.asset_class,
        "canonical_row": view.template_row_key,
        "region": view.region,
        "time_horizon": view.horizon,
        "direction": view.direction,
        "proposed_adjustment": view.position_score,
        "asset_class_supported": answers["asset_class"]["answer"],
        "asset_class_evidence": answers["asset_class"]["evidence"],
        "canonical_row_supported": answers["canonical_row"]["answer"],
        "canonical_row_evidence": answers["canonical_row"]["evidence"],
        "region_supported": answers["region"]["answer"],
        "region_evidence": answers["region"]["evidence"],
        "direction_supported": answers["direction"]["answer"],
        "direction_evidence": answers["direction"]["evidence"],
        "investment_implication_supported": answers["investment_implication"]["answer"],
        "investment_implication_evidence": answers["investment_implication"]["evidence"],
        "financial_explanation": explanation,
        "candidate_correct_mapping": candidate or "",
        "reviewer_conclusion": _reviewer_conclusion(status, primary_reason),
        "publication_excerpt": _excerpt_around_quote(article.content_text, quote),
    }


def _second_review_verified_rows(session: Session) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    statement = select(ResearchView).where(
        ResearchView.is_demo.is_(False),
        or_(ResearchView.review_status == VERIFIED_PASS, ResearchView.legacy_review_status == VERIFIED_PASS),
    )
    for view in session.scalars(statement).all():
        article = session.get(Article, view.article_id)
        if article is not None and not article.is_demo:
            rows.append(strict_review_research_view(view, article))
    return sorted(rows, key=lambda row: (row["publication_title"], row["canonical_row"], row["region"], row["research_view_id"]))


def _second_review_invalid_actions(session: Session) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in _invalid_mapping_rows(session):
        revised = _strict_invalid_action(row)
        rows.append({**row, **revised})
    return sorted(
        rows,
        key=lambda row: (
            row["revised_action_group"],
            row["error_category"],
            row["publication_title"],
            row["research_view_id"],
        ),
    )


def _five_question_answers(view: ResearchView, article: Article, quote: str, context: str) -> dict[str, dict[str, str]]:
    return {
        "asset_class": _asset_class_answer(view, article, quote, context),
        "canonical_row": _canonical_row_answer(view, article, quote, context),
        "region": _region_answer(view, article, quote, context),
        "direction": _direction_answer(view, article, quote, context),
        "investment_implication": _investment_implication_answer(view, article, quote, context),
    }


def _second_review_status(
    view: ResearchView,
    article: Article,
    quote: str,
    context: str,
    answers: dict[str, dict[str, str]],
) -> tuple[str, str, list[str]]:
    if _is_irrelevant_document(article, quote, context):
        return "IRRELEVANT_DOCUMENT", "Quote is navigation, platform copy, index text, or non-investable research boilerplate.", _failed_questions(answers)
    if _is_title_only_evidence(article, quote):
        return "INSUFFICIENT_EVIDENCE", "Quote only identifies a document or meeting and contains no investable thesis.", _failed_questions(answers)
    if _duplicate_like(view, article, quote):
        return "DUPLICATE_MAPPING", "Same publication quote is expanded across multiple cells without cell-specific evidence.", _failed_questions(answers)
    if answers["region"]["answer"] == "no":
        return "WRONG_REGION", answers["region"]["evidence"], _failed_questions(answers)
    if answers["asset_class"]["answer"] == "no":
        return "WRONG_ASSET_CLASS", answers["asset_class"]["evidence"], _failed_questions(answers)
    if view.asset_class == "FIXED INCOME" and "|GOV|" in view.template_row_key and _generic_rates_without_total_return(_clean(context)):
        return "UNSUPPORTED_DIRECTION", answers["direction"]["evidence"], _failed_questions(answers)
    if answers["canonical_row"]["answer"] == "no":
        return "WRONG_SEGMENT", answers["canonical_row"]["evidence"], _failed_questions(answers)
    if answers["direction"]["answer"] == "no":
        return "UNSUPPORTED_DIRECTION", answers["direction"]["evidence"], _failed_questions(answers)
    if answers["investment_implication"]["answer"] == "no":
        return "INSUFFICIENT_EVIDENCE", answers["investment_implication"]["evidence"], _failed_questions(answers)
    if any(item["answer"] != "yes" for item in answers.values()):
        return "MANUAL_REVIEW_REQUIRED", "One or more required mapping questions is unclear rather than explicitly supported.", _failed_questions(answers)
    return "TRUE_PASS", "All five strict mapping questions are explicitly supported.", []


def _asset_class_answer(view: ResearchView, article: Article, quote: str, context: str) -> dict[str, str]:
    text = _clean(context)
    if view.asset_class == "EQUITY":
        if _is_fixed_income_document(article, text) and not _has_equity_terms(text):
            return _no("The publication/quote is fixed-income or rates research and contains no equity evidence.")
        if _has_equity_terms(text):
            return _yes(_matched_terms(text, ["equities", "stocks", "benchmarks", "earnings", "valuation", "companies"]))
        return _no("The quote does not identify equities, stocks, listed-company earnings, equity valuation, or an index.")
    if view.asset_class == "FIXED INCOME":
        if _has_credit_terms(text) or _has_rates_terms(text) or _has_bond_terms(text):
            return _yes(_matched_terms(text, ["fixed-income", "bond", "bonds", "rates", "yields", "credit", "spreads", "central bank"]))
        return _no("The quote does not identify bonds, rates, yields, credit spreads, or fixed-income returns.")
    if view.asset_class == "COMMODITIES":
        if _has_any(text, {" gold ", " silver ", " platinum ", " palladium ", "precious metals", "commodity", "commodities"}):
            return _yes(_matched_terms(text, ["gold", "silver", "precious metals", "commodity", "commodities"]))
        return _no("The quote does not identify commodities or precious metals.")
    return _unknown("Unsupported asset class in strict review.")


def _canonical_row_answer(view: ResearchView, article: Article, quote: str, context: str) -> dict[str, str]:
    text = _clean(context)
    quote_text = _clean(quote)
    row = view.template_row_key
    if "EQUITY|Wide Market|Wide Market" == row:
        if _has_any(text, {"constructive outlook for equities", "equities in 2026", "broad equity", "equity market", "benchmarks"}):
            return _yes(_matched_terms(text, ["constructive outlook for equities", "equities in 2026", "benchmarks", "equity market"]))
        return _no("The quote is not an index-level or broad equity-market view.")
    if "EQUITY|Other categories|Growth" == row:
        if _has_any(quote_text, {"growth equities", "growth stocks", "growth style"}) or (
            _has_any(quote_text, {"technology", "ai-linked"})
            and _has_any(quote_text, {"earnings", "companies", "benchmarks"})
        ):
            return _yes(_matched_terms(quote_text, ["growth equities", "growth stocks", "technology", "ai-linked", "earnings"]))
        return _no("The quote may mention earnings growth or AI context, but it does not prove a Growth-style equity cell.")
    if "EQUITY|Other categories|Value" == row:
        if _has_any(text, {"valuation discount", "discount", "cheap", "value stocks", "value style"}):
            return _yes(_matched_terms(text, ["valuation discount", "discount", "cheap", "value stocks"]))
        return _no("The quote does not contain Value-style valuation discount or cheapness evidence.")
    if "FIXED INCOME|GOV|Short Term" == row:
        if _has_any(text, {"front-end", "front end", "2-year", "short-term rates", "policy rate", "rate cuts", "rate hikes"}):
            return _yes(_matched_terms(text, ["front-end", "2-year", "short-term rates", "policy rate", "rate cuts", "rate hikes"]))
        return _no("The quote does not identify short-maturity government bonds or front-end yield exposure.")
    if "FIXED INCOME|GOV|Mid Term" == row:
        if _has_any(text, {"belly of the curve", "intermediate", "mid-maturity", "5-year", "government bond", "government bonds"}):
            return _yes(_matched_terms(text, ["belly of the curve", "intermediate", "mid-maturity", "government bond"]))
        return _no("The quote does not identify mid-maturity government bonds or curve-belly exposure.")
    if "FIXED INCOME|GOV|Long Term" == row:
        if _has_any(text, {"long-duration", "long duration", "10-year", "long-end", "term premium", "long-term government"}):
            return _yes(_matched_terms(text, ["long-duration", "10-year", "long-end", "term premium"]))
        return _no("The quote does not identify long-duration government bonds, 10-year yields, or term premium.")
    if "FIXED INCOME|CORP IG" in row:
        if _has_any(text, {"investment grade", "ig credit", "corporate bond", "corporate credit", "credit spreads", "refinancing"}):
            return _yes(_matched_terms(text, ["investment grade", "corporate bond", "corporate credit", "credit spreads", "refinancing"]))
        return _no("The quote does not contain investment-grade credit spread, refinancing, default, or balance-sheet evidence.")
    if "FIXED INCOME|CORP HY" in row:
        if _has_any(text, {"high yield", "high-yield", "default risk", "leveraged", "hy spreads"}):
            return _yes(_matched_terms(text, ["high yield", "default risk", "leveraged", "HY spreads"]))
        return _no("The quote does not contain high-yield spread, leverage, or default evidence.")
    if "Inflation Linked" in row:
        if _has_any(text, {"inflation-linked", "linkers", "breakeven", "real yields", "inflation risk premia"}):
            return _yes(_matched_terms(text, ["inflation-linked", "breakeven", "real yields", "inflation risk premia"]))
        return _no("The quote mentions inflation, if at all, without linker, breakeven, or real-yield evidence.")
    if "Mortgage-Backed" in row:
        if _has_any(text, {"mortgage-backed", "mbs", "prepayment", "mortgage spreads"}):
            return _yes(_matched_terms(text, ["mortgage-backed", "MBS", "prepayment", "mortgage spreads"]))
        return _no("The quote does not contain mortgage-backed securities, spread, or prepayment evidence.")
    if "Gold" in row:
        if _has_any(text, {" gold ", "bullion"}):
            return _yes(_matched_terms(text, ["gold", "bullion"]))
        return _no("The quote does not specifically support Gold.")
    if "Other precious metals" in row:
        if _has_any(text, {" silver ", " platinum ", " palladium ", "precious metals"}):
            return _yes(_matched_terms(text, ["silver", "platinum", "palladium", "precious metals"]))
        return _no("The quote does not specifically support other precious metals.")
    return _unknown("No strict row rule was available.")


def _region_answer(view: ResearchView, article: Article, quote: str, context: str) -> dict[str, str]:
    inferred = _strict_region(context)
    if inferred == "UNKNOWN":
        if view.region == "Global" and _global_region_supported(context):
            return _yes("The quote/context is genuinely global or cross-regional.")
        return _unknown("The quote/context does not anchor the thesis to the mapped region.")
    if inferred == view.region:
        return _yes(f"The quote/context explicitly anchors the thesis to {inferred}.")
    if view.region == "Global" and inferred != "Global":
        return _no(f"The evidence is explicitly {inferred}; Global cannot be used as fallback.")
    return _no(f"The evidence points to {inferred}, not {view.region}.")


def _direction_answer(view: ResearchView, article: Article, quote: str, context: str) -> dict[str, str]:
    text = _clean(context)
    direction = view.direction
    if _is_title_only_evidence(article, quote) or _is_irrelevant_document(article, quote, context):
        return _no("The quote has no directional investment thesis.")
    if direction == "NEUTRAL":
        if _has_any(text, {"neutral", "market-weight", "balanced view", "unchanged view", "no clear preference", "mixed but balanced"}):
            return _yes(_matched_terms(text, ["neutral", "market-weight", "balanced", "unchanged"]))
        return _no("NEUTRAL appears to be used as fallback; the quote does not state a balanced or unchanged view.")
    if view.asset_class == "FIXED INCOME":
        return _fixed_income_direction_answer(view, text)
    if direction == "BULLISH":
        if _has_any(text, {"constructive outlook", "outperform", "attractive", "support", "benefit", "extend rally", "stronger earnings", "earnings growth expands"}):
            return _yes(_matched_terms(text, ["constructive outlook", "outperform", "attractive", "support", "extend rally", "stronger earnings"]))
        return _no("The quote does not state positive expected relative return or attractiveness for the mapped asset.")
    if direction == "BEARISH":
        if _has_any(text, {"underperform", "negative", "pressure", "retreat", "downside", "widening risk", "less attractive"}):
            return _yes(_matched_terms(text, ["underperform", "negative", "pressure", "retreat", "downside", "less attractive"]))
        return _no("The quote does not state negative expected relative return or unattractiveness for the mapped asset.")
    return _unknown("Direction label is not one of BULLISH, BEARISH, or NEUTRAL.")


def _fixed_income_direction_answer(view: ResearchView, text: str) -> dict[str, str]:
    if _generic_rates_without_total_return(text):
        return _no("Generic inflation/rates language does not establish total-return direction, carry, duration, curve, or spread effect.")
    if view.direction == "BULLISH":
        if _has_any(text, {"bond prices should rise", "yields should fall", "rate cuts support", "attractive carry", "spreads should tighten", "positive total return"}):
            return _yes(_matched_terms(text, ["bond prices should rise", "yields should fall", "rate cuts support", "attractive carry", "spreads should tighten"]))
        return _no("Bullish fixed-income direction lacks explicit total-return, carry, duration, curve, or spread support.")
    if view.direction == "BEARISH":
        if _has_any(text, {"bond prices should fall", "yields should rise", "duration risk", "spreads should widen", "negative total return"}):
            return _yes(_matched_terms(text, ["bond prices should fall", "yields should rise", "duration risk", "spreads should widen"]))
        return _no("Bearish fixed-income direction lacks explicit total-return, duration, curve, or spread support.")
    return _no("Fixed-income direction is unclear under the strict total-return rule.")


def _investment_implication_answer(view: ResearchView, article: Article, quote: str, context: str) -> dict[str, str]:
    text = _clean(context)
    if _is_title_only_evidence(article, quote) or _is_irrelevant_document(article, quote, context):
        return _no("No 6-12 month investment implication is present.")
    if _has_any(text, {"2026", "outlook", "6-12", "12 month", "next year", "over the next year", "medium-term"}) and (
        _has_equity_terms(text) or _has_rates_terms(text) or _has_credit_terms(text) or _has_bond_terms(text) or _has_any(text, {" gold ", "precious metals"})
    ):
        return _yes(_matched_terms(text, ["2026", "outlook", "next year", "12 month", "equities", "bonds", "gold"]))
    if _has_any(text, {"extended their retreat", "weekly report", "one-day", "today", "yesterday", "index of publications"}):
        return _no("The quote is a short market update or publication index, not a 6-12 month allocation view.")
    return _unknown("The quote may be market-related, but the 6-12 month investment horizon is not explicit.")


def _strict_invalid_action(row: dict[str, Any]) -> dict[str, Any]:
    quote = str(row.get("exact_evidence_quote") or "")
    title = str(row.get("publication_title") or "")
    text = _clean(f"{title} {quote}")
    category = row["error_category"]
    legacy_candidate = str(row.get("candidate_correct_mapping") or "")
    strict_candidate = _strict_candidate_from_text(row, text)

    if _text_is_boilerplate_or_title(title, quote) or category in {"NON_CANONICAL_TOPIC", "NO_INVESTMENT_IMPLICATION"}:
        action = "REJECT"
        reason = "Reject: evidence is title/navigation/platform copy, non-canonical, or lacks investment implication."
    elif category in {"TOO_BROAD_FOR_CELL", "UNSUPPORTED_DIRECTION", "MULTIPLE_POSSIBLE_CELLS", "MISSING_TRANSMISSION_LOGIC", "DUPLICATE_OR_OVEREXPANDED_MAPPING", "HORIZON_MISMATCH"}:
        action = "MANUAL_REVIEW_REQUIRED"
        reason = "Manual review: direction, segment, horizon, or one-to-many mapping is not uniquely supported by the quote."
    elif strict_candidate and _strict_action_evidence_is_unambiguous(text, category):
        action = "AUTO_FIXABLE"
        reason = "Auto-fixable only under strict rule: one clear candidate cell, explicit region, and a supported directional thesis."
    elif legacy_candidate and legacy_candidate != "REJECT":
        action = "MANUAL_REVIEW_REQUIRED"
        reason = "Legacy candidate exists, but strict review cannot prove direction/segment from quote alone."
    else:
        action = "REJECT"
        reason = "Reject unless a human analyst supplies a new quote and transmission logic."

    return {
        "legacy_action_group": row["action_group"],
        "revised_action_group": action,
        "revised_auto_fixable": action == "AUTO_FIXABLE",
        "strict_candidate_correct_mapping": strict_candidate,
        "strict_action_reason": reason,
    }


def _strict_candidate_mapping(view: ResearchView, article: Article, quote: str, context: str) -> str:
    text = _clean(context)
    region = _strict_region(context)
    if region == "UNKNOWN":
        region = "Global" if _global_region_supported(context) else ""
    row = _strict_row_from_text(text)
    if not row or not region:
        return ""
    if not is_applicable_canonical_cell(row, region):
        return ""
    cell_id = canonical_cell_id_for(row, region) or ""
    return f"{cell_id} | {row} | {region}"


def _strict_candidate_from_text(row: dict[str, Any], text: str) -> str:
    region = _strict_region(text)
    row_key = _strict_row_from_text(text)
    if region == "UNKNOWN" or not row_key:
        return ""
    if not _directional_terms_present(text):
        return ""
    if not is_applicable_canonical_cell(row_key, region):
        return ""
    cell_id = canonical_cell_id_for(row_key, region) or ""
    return f"{cell_id} | {row_key} | {region}"


def _strict_row_from_text(text: str) -> str:
    if _has_any(text, {"valuation discount", "value stocks", "value style"}):
        return "EQUITY|Other categories|Value"
    if _has_any(text, {"growth equities", "growth stocks", "growth style"}) or (
        _has_any(text, {"technology", "ai-linked", "ai "}) and _has_any(text, {"earnings", "companies"})
    ):
        return "EQUITY|Other categories|Growth"
    if _has_any(text, {"constructive outlook for equities", "equities in 2026", "broad equity", "equity market", "benchmarks"}):
        return "EQUITY|Wide Market|Wide Market"
    if _has_any(text, {"investment grade", "corporate credit", "credit spreads", "refinancing"}):
        return "FIXED INCOME|CORP IG|Mid Term"
    if _has_any(text, {"high yield", "high-yield", "default risk", "hy spreads"}):
        return "FIXED INCOME|CORP HY|Mid Term"
    if _has_any(text, {"front-end", "2-year", "short-term rates", "rate cuts", "rate hikes"}):
        return "FIXED INCOME|GOV|Short Term"
    if _has_any(text, {"10-year", "long-end", "long-duration", "term premium"}):
        return "FIXED INCOME|GOV|Long Term"
    if _has_any(text, {"breakeven", "inflation-linked", "real yields"}):
        return "FIXED INCOME|Other categories|Inflation Linked"
    if _has_any(text, {" gold ", "bullion"}):
        return "COMMODITIES|Commodities|Gold"
    return ""


def _strict_action_evidence_is_unambiguous(text: str, category: str) -> bool:
    if _generic_rates_without_total_return(text):
        return False
    if _neutral_without_neutral_thesis(text):
        return False
    return category in {"WRONG_REGION", "WRONG_ASSET_CLASS", "WRONG_SEGMENT"} and _directional_terms_present(text)


def _individual_explanation(
    view: ResearchView,
    article: Article,
    quote: str,
    answers: dict[str, dict[str, str]],
    status: str,
    primary_reason: str,
    candidate: str,
) -> str:
    quote_snippet = _short_quote(quote)
    parts = [
        f"Evidence quote: \"{quote_snippet}\".",
        f"Mapped cell: {view.template_row_key} / {view.region} / {view.direction}.",
        f"Asset check: {answers['asset_class']['evidence']}",
        f"Row check: {answers['canonical_row']['evidence']}",
        f"Region check: {answers['region']['evidence']}",
        f"Direction check: {answers['direction']['evidence']}",
        f"Horizon/implication check: {answers['investment_implication']['evidence']}",
    ]
    if status == "TRUE_PASS":
        parts.append(
            "Conclusion: TRUE_PASS because the quote gives a 6-12 month investable view, not merely a keyword match."
        )
    else:
        parts.append(f"Conclusion: downgraded to {status}. Primary reason: {primary_reason}")
        if candidate:
            parts.append(f"Possible corrected mapping for analyst review: {candidate}.")
    return " ".join(parts)


def _reviewer_conclusion(status: str, primary_reason: str) -> str:
    if status == "TRUE_PASS":
        return "TRUE_PASS: strict second review confirms the current mapping."
    return f"DOWNGRADED: previous VERIFIED_PASS is not a financial pass under strict review. {primary_reason}"


def _false_positive_classifier_rules(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    failures = Counter()
    for row in rows:
        status = row["second_review_status"]
        quote = _clean(row["exact_evidence_quote"])
        if status == "TRUE_PASS":
            continue
        if _text_is_boilerplate_or_title(row["publication_title"], row["exact_evidence_quote"]):
            failures["title_or_navigation_quote_accepted"] += 1
        if row["region_supported"] == "no":
            failures["global_or_region_fallback_accepted"] += 1
        if row["asset_class_supported"] == "no":
            failures["asset_class_keyword_bleed"] += 1
        if row["canonical_row_supported"] == "no":
            failures["segment_keyword_bleed"] += 1
        if row["direction_supported"] == "no":
            failures["direction_inferred_from_macro_tone"] += 1
        if row["direction"] == "NEUTRAL" and row["direction_supported"] == "no":
            failures["neutral_used_as_unknown"] += 1
        if _generic_rates_without_total_return(quote):
            failures["generic_rates_or_inflation_as_bond_direction"] += 1
    return [
        {"rule": rule, "false_positive_count": count, "fix": _false_positive_fix(rule)}
        for rule, count in failures.most_common()
    ]


def _classifier_fix_recommendations(rows: list[dict[str, Any]], invalid_actions: list[dict[str, Any]]) -> list[dict[str, str]]:
    true_count = len([row for row in rows if row["second_review_status"] == "TRUE_PASS"])
    auto_count = len([row for row in invalid_actions if row["revised_action_group"] == "AUTO_FIXABLE"])
    return [
        {
            "priority": "P0",
            "recommendation": "Treat current VERIFIED_PASS as preliminary until the five strict financial questions pass.",
            "reason": f"Only {true_count} of the current 46 previous VERIFIED_PASS rows survived strict second review.",
        },
        {
            "priority": "P0",
            "recommendation": "Reject title-only, publication-index, platform-description, and boilerplate evidence before asset mapping.",
            "reason": "Document names and publication listings do not contain an investable thesis.",
        },
        {
            "priority": "P0",
            "recommendation": "Separate fixed-income direction into price, carry, duration, curve, and spread effects.",
            "reason": "Generic inflation/rates words do not prove bullish or bearish total-return attractiveness.",
        },
        {
            "priority": "P1",
            "recommendation": "Disallow Global fallback when quote/context explicitly says US, Europe, UK, Japan, or emerging markets.",
            "reason": "Region must follow the thesis, not source domicile or a missing-region default.",
        },
        {
            "priority": "P1",
            "recommendation": "Reserve AUTO_FIXABLE for cases with one proven cell, region, and direction.",
            "reason": f"Strict review leaves {auto_count} invalid mappings as auto-fixable; the rest require manual review or rejection.",
        },
    ]


def _strict_fixture_payload(report: dict[str, Any]) -> dict[str, Any]:
    positives = report["revised_golden_positive_cases"]
    downgraded = report["downgraded_cases"]
    negatives = [_negative_case_from_downgraded(row) for row in downgraded]
    negatives.extend(_negative_case_from_invalid(row) for row in report["invalid_mapping_second_review_actions"][:30])
    return {
        "schema_version": "2.0",
        "source": "strict financial second review; current VERIFIED_PASS is not accepted automatically",
        "positive_cases": positives,
        "negative_cases": negatives[: max(30, min(60, len(negatives)))],
        "manual_review_cases": report["ambiguous_manual_cases"],
        "ambiguous_cases": report["ambiguous_manual_cases"],
        "acceptance_notes": [
            "Title-only evidence cannot be TRUE_PASS.",
            "Fixed-income research cannot become equity mapping without equity evidence.",
            "Explicit EM/US/Europe/UK/Japan evidence cannot fall back to Global.",
            "Generic rates or inflation mentions cannot establish bond total-return direction.",
            "NEUTRAL cannot be used as a synonym for unknown direction.",
        ],
    }


def _positive_case_from_true_pass(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": row["research_view_id"],
        "publication_title": row["publication_title"],
        "exact_quote": row["exact_evidence_quote"],
        "expected_cell": row["canonical_cell_id"],
        "expected_row": row["canonical_row"],
        "expected_region": row["region"],
        "expected_direction": row["direction"],
        "expected_verdict": "TRUE_PASS",
        "expected_rationale": row["financial_explanation"],
    }


def _manual_case(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": row["research_view_id"],
        "publication_title": row["publication_title"],
        "exact_quote": row["exact_evidence_quote"],
        "current_mapping": f"{row['canonical_row']} | {row['region']} | {row['direction']}",
        "reason": row["primary_failure_reason"],
        "expected_verdict": "MANUAL_REVIEW_REQUIRED",
    }


def _rejected_case(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": row["research_view_id"],
        "publication_title": row["publication_title"],
        "exact_quote": row["exact_evidence_quote"],
        "current_mapping": f"{row['canonical_row']} | {row['region']} | {row['direction']}",
        "reason": row["primary_failure_reason"],
        "expected_verdict": row["second_review_status"],
    }


def _negative_case_from_downgraded(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": row["research_view_id"],
        "publication_title": row["publication_title"],
        "exact_quote": row["exact_evidence_quote"],
        "erroneous_mapping": {
            "canonical_cell_id": row["canonical_cell_id"],
            "canonical_row": row["canonical_row"],
            "region": row["region"],
            "direction": row["direction"],
        },
        "expected_rejection_reason": row["second_review_status"],
        "expected_verdict": "FAIL",
        "rationale": row["primary_failure_reason"],
    }


def _negative_case_from_invalid(row: dict[str, Any]) -> dict[str, Any]:
    current = row["current_mapping"]
    return {
        "case_id": row["research_view_id"],
        "publication_title": row["publication_title"],
        "exact_quote": row["exact_evidence_quote"],
        "erroneous_mapping": {
            "canonical_cell_id": current["canonical_cell_id"],
            "canonical_row": current["canonical_row"],
            "region": current["region"],
            "direction": current["direction"],
        },
        "expected_rejection_reason": row["error_category"],
        "expected_verdict": row["revised_action_group"],
        "rationale": row["strict_action_reason"],
    }


def _second_review_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Financial Mapping Second Review Report",
        "",
        "Read-only second review of current production Research Views. This report does not mutate production, run ingestion, recalculate coverage, or run Final Controller.",
        "",
        "## Summary",
        "",
        f"- Previous VERIFIED_PASS reviewed: {report['previous_verified_pass_count']}",
        f"- TRUE_PASS after strict review: {report['true_pass_count']}",
        f"- Downgraded: {report['downgraded_count']}",
        f"- Revised AUTO_FIXABLE among 276 INVALID_MAPPING: {report['revised_invalid_action_distribution'].get('AUTO_FIXABLE', 0)}",
        f"- Revised MANUAL_REVIEW_REQUIRED among 276 INVALID_MAPPING: {report['revised_invalid_action_distribution'].get('MANUAL_REVIEW_REQUIRED', 0)}",
        f"- Revised REJECT among 276 INVALID_MAPPING: {report['revised_invalid_action_distribution'].get('REJECT', 0)}",
        "",
        "## Downgraded Distribution",
        "",
        "| Status | Count |",
        "|---|---:|",
    ]
    for status, count in sorted(report["downgraded_distribution"].items()):
        lines.append(f"| {status} | {count} |")
    lines.extend(["", "## 46 Previous VERIFIED_PASS Rows", ""])
    lines.append("| ID | Publication | Mapping | New status | Five-question result | Explanation |")
    lines.append("|---|---|---|---|---|---|")
    for row in report["previously_verified_second_review"]:
        five = (
            f"asset={row['asset_class_supported']}; row={row['canonical_row_supported']}; "
            f"region={row['region_supported']}; direction={row['direction_supported']}; "
            f"horizon={row['investment_implication_supported']}"
        )
        mapping = f"{row['canonical_row']} / {row['region']} / {row['direction']}"
        lines.append(
            f"| {_md(row['research_view_id'])} | {_md(row['publication_title'])} | {_md(mapping)} | "
            f"{_md(row['second_review_status'])} | {_md(five)} | {_md(row['financial_explanation'])} |"
        )
    lines.extend(["", "## Classifier False Positive Rules", ""])
    for item in report["false_positive_classifier_rules"]:
        lines.append(f"- **{item['rule']}**: {item['false_positive_count']} false positives. Fix: {item['fix']}")
    lines.extend(["", "## Classifier Fix Recommendations", ""])
    for item in report["classifier_fix_recommendations"]:
        lines.append(f"- **{item['priority']}** {item['recommendation']} Reason: {item['reason']}")
    lines.append("")
    return "\n".join(lines)


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


def _context(article: Article, quote: str) -> str:
    return f"{article.title} {quote}"


def _failed_questions(answers: dict[str, dict[str, str]]) -> list[str]:
    return [name for name, item in answers.items() if item["answer"] != "yes"]


def _yes(evidence: str) -> dict[str, str]:
    return {"answer": "yes", "evidence": evidence}


def _no(evidence: str) -> dict[str, str]:
    return {"answer": "no", "evidence": evidence}


def _unknown(evidence: str) -> dict[str, str]:
    return {"answer": "unclear", "evidence": evidence}


def _clean(value: str) -> str:
    return f" {re.sub(r'\\s+', ' ', value or '').strip().casefold()} "


def _has_any(text: str, terms: set[str]) -> bool:
    return any(term in text for term in terms)


def _matched_terms(text: str, terms: list[str]) -> str:
    matched = [term for term in terms if term.casefold() in text]
    return "Matched explicit evidence: " + ", ".join(matched[:6]) if matched else "Matched explicit row evidence."


def _short_quote(quote: str, limit: int = 280) -> str:
    value = re.sub(r"\s+", " ", quote or "").strip()
    return value if len(value) <= limit else value[: limit - 3] + "..."


def _strict_region(context: str) -> str:
    text = _clean(context)
    if _has_any(text, {"emerging-market", "emerging market", "em stocks", "em equities"}):
        return "EM ex China"
    if _has_any(text, {" united states ", " u.s. ", " us ", "s&p", "nasdaq", "fed ", "federal reserve", "treasury", "us benchmarks", "us technology"}):
        return "US"
    if _has_any(text, {"euro area", "eurozone", "european central bank", " ecb ", " europe "}):
        return "Europe"
    if _has_any(text, {"united kingdom", " u.k. ", " uk ", "bank of england", " boe ", "britain"}):
        return "UK"
    if _has_any(text, {" japan ", " boj ", "bank of japan", " yen "}):
        return "Japan"
    if _global_region_supported(context):
        return "Global"
    return "UNKNOWN"


def _global_region_supported(context: str) -> bool:
    text = _clean(context)
    return _has_any(text, {" global ", "across sectors and regions", "global economy", "cross-region", "worldwide"})


def _has_equity_terms(text: str) -> bool:
    return _has_any(text, {"equity", "equities", "stocks", "benchmarks", "earnings", "valuation", "companies", "eps"})


def _has_rates_terms(text: str) -> bool:
    return _has_any(
        text,
        {
            " interest rates",
            " policy rates",
            " short-term rates",
            " rate cuts",
            " rate hikes",
            " yields",
            " yield ",
            " inflation",
            "central bank",
            " fed ",
            " ecb ",
            "bank of england",
        },
    )


def _has_bond_terms(text: str) -> bool:
    return _has_any(text, {"bond", "bonds", "fixed-income", "fixed income", "duration", "curve", "term premium"})


def _has_credit_terms(text: str) -> bool:
    return _has_any(text, {"credit", "spreads", "spread", "investment grade", "high yield", "refinancing", "default"})


def _is_fixed_income_document(article: Article, text: str) -> bool:
    return _has_any(text, {"fixed income outlook", "fixed-income outlook", "bond markets", "fed outlook", "rate forecasts"})


def _is_title_only_evidence(article: Article, quote: str) -> bool:
    title = re.sub(r"\s+", " ", article.title or "").casefold()
    q = re.sub(r"\s+", " ", quote or "").casefold()
    if not q.strip():
        return True
    if q.strip() == title.strip():
        return True
    if title.startswith("meeting of") and "account of the monetary policy meeting" in q[:260]:
        return True
    if q.startswith(title[:80]) and len(q) < 260 and not _directional_terms_present(_clean(q)):
        return True
    return False


def _is_irrelevant_document(article: Article, quote: str, context: str) -> bool:
    text = _clean(context)
    return _has_any(
        text,
        {
            "weekly report",
            "publication //",
            "staff working papers set out research in progress",
            "research and thought leadership on the challenges and opportunities",
            "physical, transition, and integrated risks",
            "state of private markets",
            "redemption management",
        },
    )


def _duplicate_like(view: ResearchView, article: Article, quote: str) -> bool:
    text = _clean(f"{article.title} {quote}")
    if _has_any(text, {"same quote mapped", "persisted research-quality status"}):
        return True
    return False


def _text_is_boilerplate_or_title(title: str, quote: str) -> bool:
    text = _clean(f"{title} {quote}")
    compact_title = re.sub(r"\s+", " ", title or "").casefold().strip()
    compact_quote = re.sub(r"\s+", " ", quote or "").casefold().strip()
    if not compact_quote or compact_quote == compact_title:
        return True
    return _has_any(
        text,
        {
            "weekly report",
            "publication //",
            "staff working papers set out research in progress",
            "research and thought leadership",
            "account of the monetary policy meeting",
        },
    ) and not _directional_terms_present(text)


def _directional_terms_present(text: str) -> bool:
    return _has_any(
        text,
        {
            "constructive outlook",
            "outperform",
            "underperform",
            "attractive",
            "support",
            "benefit",
            "extend rally",
            "stronger earnings",
            "earnings growth expands",
            "yields should fall",
            "yields should rise",
            "spreads should tighten",
            "spreads should widen",
            "bond prices should",
            "positive total return",
            "negative total return",
            "valuation discount",
        },
    )


def _generic_rates_without_total_return(text: str) -> bool:
    rates = _has_any(
        text,
        {
            " inflation ",
            " interest rates",
            " policy rates",
            " short-term rates",
            " rate cuts",
            " rate hikes",
            "monetary policy",
        },
    )
    total_return = _has_any(
        text,
        {
            "bond prices should",
            "yields should",
            "duration risk",
            "curve",
            "carry",
            "total return",
            "spreads should",
            "breakeven",
            "real yields",
        },
    )
    return rates and not total_return


def _neutral_without_neutral_thesis(text: str) -> bool:
    return not _has_any(text, {"neutral", "market-weight", "balanced view", "unchanged view", "no clear preference"})


def _false_positive_fix(rule: str) -> str:
    fixes = {
        "title_or_navigation_quote_accepted": "Require a body-text quote containing an investable thesis, not a page title or index sentence.",
        "global_or_region_fallback_accepted": "Infer region only from quoted thesis/context; block Global when a specific region is explicit.",
        "asset_class_keyword_bleed": "Run mutually exclusive asset-class checks before canonical row mapping.",
        "segment_keyword_bleed": "Require row-level style, maturity, spread, or commodity evidence.",
        "direction_inferred_from_macro_tone": "Require total-return or relative-attractiveness logic before score assignment.",
        "neutral_used_as_unknown": "Represent unknown direction as unqualified/manual review, not NEUTRAL.",
        "generic_rates_or_inflation_as_bond_direction": "Split fixed-income direction into price, carry, duration, curve, and spread effects.",
    }
    return fixes.get(rule, "Add an explicit strict gate before marking financial pass.")
