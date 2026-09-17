from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.enums import AnalystStatus
from app.domain.models import Article, ResearchView, Source
from app.services.baseline import current_mae_cells, latest_review_run, mae_status_metrics, write_review_run
from app.services.methodology import (
    BOILERPLATE,
    INVALID_EVIDENCE,
    INVALID_MAPPING,
    IRRELEVANT_DOCUMENT,
    MANUAL_REVIEW,
    VERIFIED_PASS,
    ResearchViewAssessment,
    assess_research_view,
    document_blacklist_reasons,
)
from app.services.production import ARCHIVED_TEST_DATA, REJECTED_QUALITY, is_archived_test_record, is_content_valid_article, is_production_research_view


def run_technical_review(session: Session, pytest_result: str, e2e_result: str, notes: str = ""):
    passed = pytest_result == "passed" and e2e_result == "passed"
    findings = []
    if not passed:
        findings.append({"severity": "critical", "finding": "Automated technical checks did not pass."})
    return write_review_run(
        session,
        "technical",
        "TECHNICAL PASS" if passed else "TECHNICAL FAIL",
        overall_quality=92 if passed else 40,
        critical_findings=findings,
        run_metadata={"pytest": pytest_result, "e2e": e2e_result, "notes": notes},
    )


def run_financial_review(session: Session, mode: str = "REAL"):
    mode_value = "REAL"
    metrics = mae_status_metrics(session, mode_value)
    cells = current_mae_cells(session, mode_value)
    sources = [row for row in session.scalars(select(Source)).all() if not is_archived_test_record(row)]
    articles = [row for row in session.scalars(select(Article)).all() if not is_archived_test_record(row)]
    production_articles = [row for row in articles if is_content_valid_article(row)]
    views = []
    for row in session.scalars(select(ResearchView)).all():
        if is_archived_test_record(row):
            continue
        article = session.get(Article, row.article_id)
        if article is None or is_archived_test_record(article):
            continue
        views.append(row)
    critical: list[dict[str, Any]] = []
    cell_findings: list[dict[str, Any]] = []
    current_scores = [cell for cell in cells if cell.current_score is not None]
    if not current_scores:
        critical.append({"severity": "critical", "finding": "Current MAE has no baseline/current scores."})
    if metrics["qualified_coverage_pct"] < 20:
        critical.append(
            {
                "severity": "comment",
                "finding": "Qualified evidence coverage is below professional acceptance target; baseline remains visible but needs refresh.",
            }
        )
    for article in articles:
        blacklist = document_blacklist_reasons(article)
        if blacklist and article.processing_status == "ANALYSED":
            critical.append(
                {
                    "severity": "critical",
                    "publication": article.title,
                    "finding": "Blacklisted or boilerplate publication reached ANALYSED status.",
                    "reasons": blacklist[:4],
                }
            )
    qualified_view_ids: set[str] = set()
    invalid_views: list[dict[str, Any]] = []
    views_by_article: dict[str, list[ResearchView]] = {}
    assessments: dict[str, ResearchViewAssessment] = {}
    assessment_counts: Counter[str] = Counter()
    for view in views:
        views_by_article.setdefault(view.article_id, []).append(view)
        article = session.get(Article, view.article_id)
        assessment = _effective_assessment(view, article)
        assessments[view.id] = assessment
        assessment_counts[assessment.status] += 1
        if not assessment.passed:
            invalid_views.append(
                {
                    "view_id": view.id,
                    "publication": article.title if article else "<missing>",
                    "asset_segment": view.template_row_key,
                    "region": view.region,
                    "status": assessment.status,
                    "full_text_verification_status": assessment.full_text_verification_status,
                    "problems": assessment.reasons[:5],
                }
            )
        else:
            if is_production_research_view(view, article):
                qualified_view_ids.add(view.id)
    analysed_without_qualified = [
        article
        for article in production_articles
        if article.processing_status == "ANALYSED"
        and not any(view.id in qualified_view_ids for view in views_by_article.get(article.id, []))
    ]
    for article in analysed_without_qualified:
        critical.append(
            {
                "severity": "critical",
                "publication": article.title,
                "finding": "Publication reached ANALYSED status without a qualified Research View.",
            }
        )
    if invalid_views:
        critical.append(
            {
                "severity": "critical",
                "finding": "Research Views failed evidence-quality validation.",
                "examples": invalid_views[:6],
            }
        )
    cell_findings.extend(_cell_level_review_rows(session, cells, views, assessments))
    strong_cells = [cell for cell in cells if cell.validated_adjustment is not None and abs(cell.validated_adjustment) >= 2]
    for cell in strong_cells:
        if cell.source_count < 2 or not cell.supporting_evidence or cell.coverage_status != "COVERED":
            cell_findings.append(
                {
                    "canonical_cell_id": cell.canonical_cell_id,
                    "canonical_row": cell.template_row_key,
                    "region": cell.region,
                    "publication_title": "",
                    "institution": "",
                    "exact_quote": "",
                    "quote_locator": "",
                    "full_text_verification_status": "not verified",
                    "mapping_rationale": "strong adjustment lacks sufficient supporting evidence",
                    "proposed_direction": "",
                    "proposed_score_or_adjustment": cell.validated_adjustment,
                    "reviewer_verdict": "FAIL",
                    "reviewer_comment": "Strong fresh adjustment lacks sufficient qualified evidence.",
                    "baseline_score": cell.baseline_score,
                    "current_score": cell.current_score,
                }
            )
    failing_cell_findings = [row for row in cell_findings if row.get("reviewer_verdict") == "FAIL"]
    if not cell_findings and production_articles:
        critical.append({"severity": "critical", "finding": "No cell-level financial review evidence rows were created."})
    has_critical = any(item.get("severity") == "critical" for item in critical)
    if has_critical or failing_cell_findings:
        verdict = "FINANCIAL FAIL"
    elif critical:
        verdict = "PASS WITH COMMENTS"
    elif metrics["approved_shifts"] == 0:
        verdict = "FINANCIAL PASS WITH NO APPROVED SHIFTS"
    else:
        verdict = "FINANCIAL PASS"
    total_assessed = sum(assessment_counts.values())
    passed_assessed = assessment_counts.get(VERIFIED_PASS, 0)
    source_quality = round((passed_assessed / total_assessed) * 100) if total_assessed else 0
    if source_quality == 100 and (invalid_views or has_critical or failing_cell_findings):
        source_quality = 99
    if len({a.source_id for a in production_articles}) < 2 and production_articles:
        source_quality = min(source_quality, 70)
    if invalid_views:
        source_quality = min(source_quality, 60)
    if analysed_without_qualified:
        source_quality = min(source_quality, 50)
    if has_critical or failing_cell_findings:
        source_quality = min(source_quality, 50)
    return write_review_run(
        session,
        "financial",
        verdict,
        overall_quality=75 if verdict == "PASS WITH COMMENTS" else 90 if verdict.startswith("FINANCIAL PASS") else 45,
        source_quality=source_quality,
        evidence_to_score_logic=35 if failing_cell_findings else 82,
        cross_matrix_consistency=82 if current_scores else 30,
        practical_usefulness=78 if current_scores else 25,
        critical_findings=critical,
        cell_findings=cell_findings,
        run_metadata={
            "environment": "production",
            "registered_sources": len(sources),
            "publications": len(production_articles),
            "raw_coverage_pct": metrics["raw_coverage_pct"],
            "qualified_coverage_pct": metrics["qualified_coverage_pct"],
            "financial_reviewed_coverage_pct": 0.0 if verdict == "FINANCIAL FAIL" else metrics["financial_reviewed_coverage_pct"],
            "current_scores": metrics["current_scores"],
            "qualified_research_views": metrics["qualified_research_views"],
            "research_views_rechecked": total_assessed,
            "VERIFIED_PASS": assessment_counts.get(VERIFIED_PASS, 0),
            "INVALID_MAPPING": assessment_counts.get(INVALID_MAPPING, 0),
            "INVALID_EVIDENCE": assessment_counts.get(INVALID_EVIDENCE, 0),
            "IRRELEVANT_DOCUMENT": assessment_counts.get(IRRELEVANT_DOCUMENT, 0),
            "BOILERPLATE": assessment_counts.get(BOILERPLATE, 0),
            "MANUAL_REVIEW": assessment_counts.get(MANUAL_REVIEW, 0),
        },
    )


def run_final_controller(session: Session, pytest_result: str, e2e_result: str, screenshots: list[str]):
    metrics = mae_status_metrics(session, "REAL")
    cells = current_mae_cells(session, "REAL")
    articles = [row for row in session.scalars(select(Article)).all() if is_content_valid_article(row)]
    orgs = len({article.source_id for article in articles})
    technical_review = latest_review_run(session, "technical")
    financial_review = latest_review_run(session, "financial")
    passed = (
        pytest_result == "passed"
        and e2e_result == "passed"
        and technical_review is not None
        and technical_review.verdict == "TECHNICAL PASS"
        and financial_review is not None
        and financial_review.verdict in {"FINANCIAL PASS", "FINANCIAL PASS WITH NO APPROVED SHIFTS"}
        and metrics["current_scores"] > 0
        and bool(screenshots)
    )
    findings = []
    if technical_review is None or technical_review.verdict != "TECHNICAL PASS":
        findings.append({"severity": "critical", "finding": "Technical PASS is missing."})
    if financial_review is None or financial_review.verdict not in {"FINANCIAL PASS", "FINANCIAL PASS WITH NO APPROVED SHIFTS"}:
        findings.append({"severity": "critical", "finding": "Financial PASS is missing."})
    if metrics["current_scores"] <= 0:
        findings.append({"severity": "critical", "finding": "Current MAE is empty."})
    technical = write_review_run(
        session,
        "final_controller",
        "FINAL PASS" if passed else "FINAL FAIL",
        overall_quality=90 if passed else 55,
        critical_findings=findings,
        run_metadata={
            "pytest": pytest_result,
            "e2e": e2e_result,
            "current_scores": metrics["current_scores"],
            "technical_verdict": technical_review.verdict if technical_review else "NOT_RUN",
            "financial_verdict": financial_review.verdict if financial_review else "NOT_RUN",
            "fresh_coverage_pct": metrics["coverage_pct"],
            "articles": len(articles),
            "organizations": orgs,
            "raw_coverage_pct": metrics["raw_coverage_pct"],
            "qualified_coverage_pct": metrics["qualified_coverage_pct"],
            "financial_reviewed_coverage_pct": metrics["financial_reviewed_coverage_pct"],
            "qualified_covered": metrics["qualified_covered"],
            "screenshots": screenshots,
            "cells": len(cells),
        },
    )
    return technical


def _cell_level_review_rows(
    session: Session,
    cells: list[Any],
    views: list[ResearchView],
    assessments: dict[str, ResearchViewAssessment],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cells_by_id = {cell.canonical_cell_id: cell for cell in cells if cell.canonical_cell_id}
    for view in sorted(views, key=lambda item: (item.article_id, item.template_row_key, item.region, item.id)):
        article = session.get(Article, view.article_id)
        if article is None:
            continue
        cell = cells_by_id.get(view.canonical_cell_id)
        quote = (view.evidence_quotes or [{}])[0]
        exact_quote = str(quote.get("quote") or "")
        locator = str(quote.get("locator") or "")
        assessment = assessments.get(view.id) or _effective_assessment(view, article)
        verdict = "PASS" if assessment.passed and assessment.full_text_verification_status == "verified" else "FAIL"
        status = assessment.status
        comment = (
            f"{status}: {assessment.mapping_rationale}"
            if verdict == "PASS"
            else f"{status}: {'; '.join(assessment.reasons[:4]) or assessment.mapping_rationale}"
        )
        rows.append(
            {
                "canonical_cell_id": view.canonical_cell_id,
                "canonical_row": view.template_row_key,
                "region": view.region,
                "publication_title": article.title,
                "institution": view.institution,
                "exact_quote": exact_quote,
                "quote_locator": locator,
                "full_text_verification_status": assessment.full_text_verification_status,
                "mapping_rationale": assessment.mapping_rationale,
                "proposed_direction": view.direction,
                "proposed_score_or_adjustment": view.position_score,
                "reviewer_verdict": verdict,
                "review_status_result": status,
                "reviewer_comment": comment[:1000],
                "baseline_score": cell.baseline_score if cell else None,
                "current_score": cell.current_score if cell else None,
                "source_url": (quote.get("source_url") or article.source_reference),
            }
        )
    for cell in cells:
        if cell.proposed_adjustment is None:
            continue
        if any(row["canonical_cell_id"] == cell.canonical_cell_id for row in rows):
            continue
        rows.append(
            {
                "canonical_cell_id": cell.canonical_cell_id,
                "canonical_row": cell.template_row_key,
                "region": cell.region,
                "publication_title": "",
                "institution": "",
                "exact_quote": "",
                "quote_locator": "",
                "full_text_verification_status": "not verified",
                "mapping_rationale": "Proposed adjustment exists without qualified evidence row.",
                "proposed_direction": "",
                "proposed_score_or_adjustment": cell.proposed_adjustment,
                "reviewer_verdict": "FAIL",
                "reviewer_comment": "Every proposed adjustment must be backed by checked qualified evidence.",
                "baseline_score": cell.baseline_score,
                "current_score": cell.current_score,
                "source_url": "",
            }
        )
    return rows


def _effective_assessment(view: ResearchView, article: Article | None) -> ResearchViewAssessment:
    assessment = assess_research_view(view, article)
    persisted_fail_statuses = {
        INVALID_MAPPING,
        INVALID_EVIDENCE,
        IRRELEVANT_DOCUMENT,
        BOILERPLATE,
        MANUAL_REVIEW,
        REJECTED_QUALITY,
        AnalystStatus.INVALIDATED.value,
        AnalystStatus.REJECTED.value,
    }
    if view.review_status in persisted_fail_statuses and assessment.passed:
        status = view.review_status if view.review_status in {INVALID_MAPPING, INVALID_EVIDENCE, IRRELEVANT_DOCUMENT, BOILERPLATE, MANUAL_REVIEW} else INVALID_EVIDENCE
        return ResearchViewAssessment(
            status=status,
            full_text_verification_status=assessment.full_text_verification_status,
            reviewer_verdict="FAIL",
            reasons=[f"persisted research-quality status is {view.review_status}"],
            mapping_rationale=f"Persisted review status {view.review_status} excludes this Research View from financial PASS.",
        )
    return assessment


def _mode_matches(is_demo: bool, mode: str) -> bool:
    if mode == "DEMO":
        return is_demo
    if mode == "REAL":
        return not is_demo
    return True
