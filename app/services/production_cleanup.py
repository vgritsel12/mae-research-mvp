from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.enums import AnalystStatus, EvidenceStatus
from app.domain.models import (
    AppLog,
    Article,
    ChangeLog,
    EvidenceObservation,
    MatrixScore,
    ResearchView,
    ScenarioAssessment,
    ScenarioCard,
    ShiftSignal,
    Source,
)
from app.services.methodology import (
    ARCHIVED_LEGACY,
    BOILERPLATE,
    INVALID_EVIDENCE,
    INVALID_MAPPING,
    IRRELEVANT_DOCUMENT,
    MANUAL_REVIEW_REQUIRED,
    REJECTED_EVIDENCE,
    REJECTED_MAPPING,
    STRICT_PRODUCTION_REVIEW_STATUSES,
    STRICT_VALIDATED,
    VERIFIED_PASS,
    assess_research_view,
    document_blacklist_reasons,
    research_view_quality_errors,
)
from app.services.normalization import is_applicable_canonical_cell
from app.services.production import (
    ARCHIVED_TEST_DATA,
    REJECTED_QUALITY,
    has_test_marker,
    is_archived_test_record,
    is_content_valid_article,
    is_production_research_view,
    status_note,
)


def apply_production_migration(session: Session) -> dict[str, Any]:
    archived = archive_demo_test_records(session)
    invalidated = invalidate_legacy_invalid_research(session)
    result = {**archived, **invalidated}
    if any(value for value in result.values() if isinstance(value, int)):
        session.add(
            AppLog(
                level="INFO",
                event="production_cleanup_applied",
                message="Production cleanup archived test data and invalidated legacy research that failed methodology gates.",
                context=result,
            )
        )
    session.flush()
    return result


def archive_demo_test_records(session: Session) -> dict[str, int]:
    archived_sources = _archive_sources(session)
    archived_articles = _archive_articles(session)
    archived_views = _archive_views(session)
    archived_changes = _archive_changes(session)
    archived_scenarios = _archive_scenarios(session)
    archived_evidence = _archive_evidence(session)
    archived_assessments = _archive_assessments(session)
    archived_signals = _archive_signals(session)
    archived_matrix = _archive_matrix_scores(session)
    session.flush()
    return {
        "archived_test_sources": archived_sources,
        "archived_test_articles": archived_articles,
        "archived_test_research_views": archived_views,
        "archived_test_changes": archived_changes,
        "archived_test_scenarios": archived_scenarios,
        "archived_test_evidence": archived_evidence,
        "archived_test_assessments": archived_assessments,
        "archived_test_shifts": archived_signals,
        "archived_test_matrix_scores": archived_matrix,
    }


def invalidate_legacy_invalid_research(session: Session) -> dict[str, Any]:
    timestamp = datetime.now(UTC).isoformat()
    invalidated_articles: list[dict[str, Any]] = []
    manual_required_articles: list[dict[str, Any]] = []
    invalidated_views: list[dict[str, Any]] = []

    for article in session.scalars(select(Article)).all():
        if is_archived_test_record(article):
            continue
        reasons = document_blacklist_reasons(article)
        if reasons:
            _mark_article_rejected(article, reasons, timestamp)
            invalidated_articles.append(_article_record(article, reasons))

    for view in session.scalars(select(ResearchView)).all():
        article = session.get(Article, view.article_id)
        if is_archived_test_record(view):
            continue
        if view.review_status in STRICT_PRODUCTION_REVIEW_STATUSES:
            continue
        reasons: list[str] = []
        assessment = assess_research_view(view, article)
        if article is None:
            reasons.append("methodology/source-link: source article missing")
        elif not is_content_valid_article(article):
            reasons.append(f"methodology/content-gate: article status {article.processing_status}")
        else:
            reasons.extend(research_view_quality_errors(view, article))
            if not view.canonical_cell_id or not is_applicable_canonical_cell(view.template_row_key, view.region):
                reasons.append("methodology/canonical-map: view is outside applicable MAE universe")
        if reasons:
            _mark_view_rechecked(view, _status_for_assessment(assessment, reasons))
            invalidated_views.append(_view_record(view, article, reasons))
            _invalidate_downstream_for_view(session, view.id, reasons, timestamp)
        else:
            view.review_status = VERIFIED_PASS

    duplicate_views = _invalidate_duplicate_quote_mappings(session, timestamp)
    invalidated_views.extend(duplicate_views)

    for article in session.scalars(select(Article)).all():
        if is_archived_test_record(article):
            continue
        if article.processing_status != "ANALYSED":
            continue
        qualified = [
            view
            for view in session.scalars(select(ResearchView).where(ResearchView.article_id == article.id)).all()
            if is_production_research_view(view, article)
        ]
        if qualified:
            continue
        reasons = ["methodology/evidence-gate: ANALYSED publication has no qualified Research View"]
        if document_blacklist_reasons(article):
            _mark_article_rejected(article, reasons, timestamp)
            invalidated_articles.append(_article_record(article, reasons))
        else:
            article.processing_status = "MANUAL_REQUIRED"
            article.error_message = status_note(
                article.error_message,
                f"[{timestamp}] MANUAL_REQUIRED: publication has no qualified Research View after production cleanup.",
            )
            manual_required_articles.append(_article_record(article, reasons))

    if invalidated_articles or invalidated_views or manual_required_articles:
        session.add(
            AppLog(
                level="WARNING",
                event="legacy_research_invalidated",
                message="Legacy publications/views failing content, evidence or canonical-map gates were excluded from production calculations.",
                context={
                    "invalidated_publications": invalidated_articles[:100],
                    "manual_required_publications": manual_required_articles[:100],
                    "invalidated_research_views": invalidated_views[:200],
                    "rule": "SRS methodology gates: content validation, evidence validation, canonical cell mapping.",
                },
            )
        )
    session.flush()
    return {
        "invalidated_legacy_publications": len({row["article_id"] for row in invalidated_articles}),
        "manual_required_legacy_publications": len({row["article_id"] for row in manual_required_articles}),
        "invalidated_research_views": len({row["view_id"] for row in invalidated_views}),
        "rechecked_research_views": _production_research_view_audit_counts(session)["total"],
        "research_view_audit_counts": _production_research_view_audit_counts(session),
        "invalidated_legacy_publication_list": invalidated_articles[:100],
        "manual_required_publication_list": manual_required_articles[:100],
        "invalidated_research_view_list": invalidated_views[:200],
    }


def invalidated_legacy_register(session: Session) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for article in session.scalars(select(Article).order_by(Article.updated_at.desc())).all():
        if article.processing_status == ARCHIVED_TEST_DATA or article.is_demo or has_test_marker(article.title, article.source_reference):
            continue
        if article.processing_status not in {ARCHIVED_TEST_DATA, REJECTED_QUALITY, AnalystStatus.INVALIDATED.value, "MANUAL_REQUIRED", "PARSE_FAILED"}:
            continue
        rows.append(
            {
                "type": "publication",
                "id": article.id,
                "title": article.title,
                "institution": article.source.institution_name if article.source else "",
                "url": article.source_reference,
                "status": article.processing_status,
                "reason": article.error_message or "",
                "updated_at": article.updated_at,
            }
        )
    for view in session.scalars(select(ResearchView).order_by(ResearchView.updated_at.desc())).all():
        if view.review_status == ARCHIVED_TEST_DATA or view.is_demo or has_test_marker(view.institution):
            continue
        if view.review_status not in {ARCHIVED_TEST_DATA, AnalystStatus.INVALIDATED.value, REJECTED_QUALITY}:
            continue
        article = session.get(Article, view.article_id)
        rows.append(
            {
                "type": "research_view",
                "id": view.id,
                "title": article.title if article else "",
                "institution": view.institution,
                "url": article.source_reference if article else "",
                "status": view.review_status,
                "reason": article.error_message if article else "source article missing",
                "updated_at": view.updated_at,
            }
        )
    return rows


def _archive_sources(session: Session) -> int:
    count = 0
    for source in session.scalars(select(Source)).all():
        if not _source_is_test_like(source):
            continue
        count += 1
        source.is_demo = True
        source.active = False
        source.category = ARCHIVED_TEST_DATA
    return count


def _archive_articles(session: Session) -> int:
    archived_source_ids = {
        source.id
        for source in session.scalars(select(Source)).all()
        if source.category == ARCHIVED_TEST_DATA or source.is_demo or _source_is_test_like(source)
    }
    count = 0
    for article in session.scalars(select(Article)).all():
        if not (article.is_demo or article.source_id in archived_source_ids or has_test_marker(article.title, article.source_reference, article.url)):
            continue
        count += 1
        article.is_demo = True
        article.processing_status = ARCHIVED_TEST_DATA
        article.error_message = status_note(
            article.error_message,
            f"[{datetime.now(UTC).isoformat()}] ARCHIVED_TEST_DATA: excluded from production workflow.",
        )
    return count


def _archive_views(session: Session) -> int:
    archived_article_ids = {
        article.id
        for article in session.scalars(select(Article)).all()
        if article.is_demo or article.processing_status == ARCHIVED_TEST_DATA
    }
    count = 0
    for view in session.scalars(select(ResearchView)).all():
        if not (view.is_demo or view.article_id in archived_article_ids or has_test_marker(view.institution)):
            continue
        count += 1
        view.is_demo = True
        view.review_status = ARCHIVED_TEST_DATA
    return count


def _archive_changes(session: Session) -> int:
    archived_view_ids = {
        view.id
        for view in session.scalars(select(ResearchView)).all()
        if view.is_demo or view.review_status == ARCHIVED_TEST_DATA
    }
    count = 0
    for change in session.scalars(select(ChangeLog)).all():
        if not (change.is_demo or change.current_view_id in archived_view_ids or change.previous_view_id in archived_view_ids):
            continue
        count += 1
        change.is_demo = True
        change.explanation = status_note(change.explanation, "[ARCHIVED_TEST_DATA] Excluded from production workflow.")
    return count


def _archive_scenarios(session: Session) -> int:
    archived_change_ids = {change.id for change in session.scalars(select(ChangeLog)).all() if change.is_demo}
    archived_view_ids = {view.id for view in session.scalars(select(ResearchView)).all() if view.is_demo}
    count = 0
    for scenario in session.scalars(select(ScenarioCard)).all():
        if not (
            scenario.is_demo
            or scenario.linked_change_id in archived_change_ids
            or scenario.linked_view_id in archived_view_ids
            or has_test_marker(scenario.title, scenario.description)
        ):
            continue
        count += 1
        scenario.is_demo = True
        scenario.review_status = ARCHIVED_TEST_DATA
        scenario.source_references = [
            *(scenario.source_references or []),
            {"type": ARCHIVED_TEST_DATA, "note": "Excluded from production workflow."},
        ]
    return count


def _archive_evidence(session: Session) -> int:
    archived_scenario_ids = {scenario.id for scenario in session.scalars(select(ScenarioCard)).all() if scenario.is_demo}
    count = 0
    for evidence in session.scalars(select(EvidenceObservation)).all():
        if not (evidence.is_demo or evidence.scenario_id in archived_scenario_ids or has_test_marker(evidence.source_url, evidence.indicator)):
            continue
        count += 1
        evidence.is_demo = True
    return count


def _archive_assessments(session: Session) -> int:
    archived_scenario_ids = {scenario.id for scenario in session.scalars(select(ScenarioCard)).all() if scenario.is_demo}
    count = 0
    for assessment in session.scalars(select(ScenarioAssessment)).all():
        if not (assessment.is_demo or assessment.scenario_id in archived_scenario_ids):
            continue
        count += 1
        assessment.is_demo = True
        assessment.evidence_status = ARCHIVED_TEST_DATA
        assessment.explanation = status_note(assessment.explanation, "ARCHIVED_TEST_DATA: excluded from production workflow.")
    return count


def _archive_signals(session: Session) -> int:
    archived_change_ids = {change.id for change in session.scalars(select(ChangeLog)).all() if change.is_demo}
    count = 0
    for signal in session.scalars(select(ShiftSignal)).all():
        if not (signal.is_demo or signal.change_id in archived_change_ids or any(has_test_marker(url) for url in signal.source_urls or [])):
            continue
        count += 1
        signal.is_demo = True
        signal.analyst_status = ARCHIVED_TEST_DATA
        signal.evidence_status = EvidenceStatus.INVALIDATED.value
        signal.analyst_comment = status_note(signal.analyst_comment, "ARCHIVED_TEST_DATA: excluded from production workflow.")
    return count


def _archive_matrix_scores(session: Session) -> int:
    count = 0
    for row in session.scalars(select(MatrixScore)).all():
        if not row.is_demo:
            continue
        count += 1
        row.coverage_status = ARCHIVED_TEST_DATA
        row.suggested_score = None
        row.approved_score = None
        row.signal_ids = []
        row.publication_ids = []
        row.supporting_evidence = []
        row.contradicting_evidence = []
        row.calculation_details = {**(row.calculation_details or {}), "archived": ARCHIVED_TEST_DATA}
    return count


def _source_is_test_like(source: Source) -> bool:
    return bool(source.is_demo or source.category == ARCHIVED_TEST_DATA or has_test_marker(source.institution_name, source.website, source.category))


def _mark_article_rejected(article: Article, reasons: list[str], timestamp: str) -> None:
    article.processing_status = REJECTED_QUALITY
    article.error_message = status_note(
        article.error_message,
        f"[{timestamp}] REJECTED_QUALITY: excluded by methodology gates. Rule: content validation / source quality. Reasons: {'; '.join(reasons[:8])}",
    )


def _mark_view_rechecked(view: ResearchView, status: str) -> None:
    view.review_status = status


def _status_for_assessment(assessment, reasons: list[str]) -> str:
    if assessment.status != VERIFIED_PASS:
        return assessment.status
    text = " ".join(reasons).casefold()
    if "boilerplate" in text or "security banner" in text:
        return BOILERPLATE
    if "irrelevant" in text or "blacklisted document marker" in text:
        return IRRELEVANT_DOCUMENT
    if "canonical" in text or "mapping" in text or "region unsupported" in text or "asset unsupported" in text:
        return INVALID_MAPPING
    return INVALID_EVIDENCE


def _invalidate_duplicate_quote_mappings(session: Session, timestamp: str) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[ResearchView]] = {}
    for view in session.scalars(select(ResearchView)).all():
        if is_archived_test_record(view) or view.review_status != VERIFIED_PASS:
            continue
        quote = _normalized_first_quote(view)
        if not quote:
            continue
        grouped.setdefault((view.article_id, quote), []).append(view)
    invalidated: list[dict[str, Any]] = []
    for (_article_id, _quote), views in grouped.items():
        row_keys = {view.template_row_key for view in views}
        asset_groups = {(view.asset_class, view.asset_group) for view in views}
        regions = {view.region for view in views}
        if len(row_keys) <= 2 and len(asset_groups) <= 1:
            continue
        if len(row_keys) <= 3 and len(asset_groups) <= 2 and len(regions) == 1:
            continue
        allowed_ids = _allowed_duplicate_quote_view_ids(views)
        reasons = [
            "methodology/cell-specific-rationale: same quote mapped to multiple unrelated canonical cells without separate evidence"
        ]
        for view in views:
            if view.id in allowed_ids:
                continue
            view.review_status = INVALID_MAPPING
            article = session.get(Article, view.article_id)
            invalidated.append(_view_record(view, article, reasons))
            _invalidate_downstream_for_view(session, view.id, reasons, timestamp)
    return invalidated


def _allowed_duplicate_quote_view_ids(views: list[ResearchView]) -> set[str]:
    if not views:
        return set()
    quote_text = _normalized_first_quote(views[0])
    allowed: set[str] = set()
    category_order = [
        "fi_gov_short",
        "fi_gov_mid",
        "fi_gov_long",
        "credit_ig",
        "credit_hy",
        "equity_wide",
        "equity_growth",
        "equity_value",
        "gold",
    ]
    for category in category_order:
        candidates = [view for view in views if _duplicate_category(view, quote_text) == category]
        if not candidates:
            continue
        best = sorted(candidates, key=lambda view: (_region_specificity(view.region, quote_text), view.template_row_key), reverse=True)[0]
        allowed.add(best.id)
    return allowed


def _duplicate_category(view: ResearchView, quote_text: str) -> str:
    row = view.template_row_key.casefold()
    text = f" {quote_text} "
    if "fixed income|gov|short term" in row and _has_any(text, {"rate", "rates", "policy", "central bank", "inflation", "yield", "yields", "cut", "cuts", "hike", "hikes", "front-end", "short-term"}):
        return "fi_gov_short"
    if "fixed income|gov|mid term" in row and _has_any(text, {"rate", "rates", "policy", "central bank", "inflation", "yield", "yields", "duration"}):
        return "fi_gov_mid"
    if "fixed income|gov|long term" in row and _has_any(text, {"long", "duration", "10-year", "10y", "yield curve", "term premium"}):
        return "fi_gov_long"
    if "fixed income|corp ig" in row and _has_any(text, {"credit", "spread", "spreads", "investment grade", "corporate bond", "refinancing"}):
        return "credit_ig"
    if "fixed income|corp hy" in row and _has_any(text, {"high yield", "high-yield", "default", "leveraged", "refinancing"}):
        return "credit_hy"
    if "equity|wide market|wide market" in row and _has_any(text, {"equity", "equities", "stock", "stocks", "earnings", "eps", "market", "markets", "s&p", "nasdaq"}):
        return "equity_wide"
    if "equity|other categories|growth" in row and _has_any(text, {"growth stocks", "technology", "tech", "ai", "software", "semiconductor", "earnings growth", "eps growth"}):
        return "equity_growth"
    if "equity|other categories|value" in row and _has_any(text, {"value", "valuation", "valuations", "cheap", "discount", "cyclical"}):
        return "equity_value"
    if ("gold" in row or "commodities" in row) and _has_any(text, {"gold", "commodity", "commodities", "oil", "metals", "real yields", "dollar"}):
        return "gold"
    return ""


def _region_specificity(region: str, quote_text: str) -> int:
    text = f" {quote_text} "
    if region == "Global" and _has_any(text, {"global", "worldwide", "world", "developed-market"}):
        return 3
    if region == "US" and _has_any(text, {"united states", "u.s.", "federal reserve", "fomc", "s&p", "nasdaq", "treasury"}):
        return 3
    if region == "Europe" and _has_any(text, {"europe", "euro area", "eurozone", "ecb", "european central bank"}):
        return 3
    if region == "UK" and _has_any(text, {"united kingdom", "u.k.", " uk ", "bank of england", "boe"}):
        return 3
    if region == "EM ex China" and _has_any(text, {"emerging markets", "emerging-market", "em ex china"}):
        return 3
    if region == "Japan" and _has_any(text, {"japan", "boj", "bank of japan"}):
        return 3
    return 1 if region == "Global" else 0


def _has_any(text: str, terms: set[str]) -> bool:
    return any(term in text for term in terms)


def _normalized_first_quote(view: ResearchView) -> str:
    quotes = view.evidence_quotes or []
    if not quotes:
        return ""
    quote = str(quotes[0].get("quote") or "")
    return " ".join(quote.casefold().split())[:500]


def _production_research_view_audit_counts(session: Session) -> dict[str, int]:
    statuses = {
        VERIFIED_PASS: 0,
        STRICT_VALIDATED: 0,
        INVALID_MAPPING: 0,
        INVALID_EVIDENCE: 0,
        REJECTED_MAPPING: 0,
        REJECTED_EVIDENCE: 0,
        IRRELEVANT_DOCUMENT: 0,
        BOILERPLATE: 0,
        "MANUAL_REVIEW": 0,
        MANUAL_REVIEW_REQUIRED: 0,
        ARCHIVED_LEGACY: 0,
    }
    total = 0
    for view in session.scalars(select(ResearchView)).all():
        if is_archived_test_record(view):
            continue
        article = session.get(Article, view.article_id)
        if article is None or is_archived_test_record(article):
            continue
        total += 1
        if view.review_status in statuses:
            statuses[view.review_status] += 1
    return {"total": total, **statuses}


def _invalidate_downstream_for_view(session: Session, view_id: str, reasons: list[str], timestamp: str) -> None:
    changes = list(
        session.scalars(
            select(ChangeLog).where((ChangeLog.current_view_id == view_id) | (ChangeLog.previous_view_id == view_id))
        ).all()
    )
    for change in changes:
        change.explanation = status_note(
            change.explanation,
            f"[{timestamp}] INVALIDATED: linked Research View failed methodology gates ({'; '.join(reasons[:4])}).",
        )
        for scenario in session.scalars(select(ScenarioCard).where(ScenarioCard.linked_change_id == change.id)).all():
            scenario.review_status = AnalystStatus.INVALIDATED.value
            refs = list(scenario.source_references or [])
            refs.append(
                {
                    "type": "methodology_invalidation",
                    "timestamp": timestamp,
                    "rule": "content/evidence/canonical-map gate",
                    "reasons": reasons[:8],
                }
            )
            scenario.source_references = refs
            assessment = session.scalar(select(ScenarioAssessment).where(ScenarioAssessment.scenario_id == scenario.id))
            if assessment:
                assessment.evidence_status = EvidenceStatus.INVALIDATED.value
                assessment.explanation = status_note(assessment.explanation, "INVALIDATED: linked Research View failed methodology gates.")
        for signal in session.scalars(select(ShiftSignal).where(ShiftSignal.change_id == change.id)).all():
            signal.analyst_status = AnalystStatus.INVALIDATED.value
            signal.evidence_status = EvidenceStatus.INVALIDATED.value
            signal.analyst_comment = status_note(signal.analyst_comment, "INVALIDATED: linked Research View failed methodology gates.")


def _article_record(article: Article, reasons: list[str]) -> dict[str, Any]:
    return {
        "article_id": article.id,
        "title": article.title,
        "institution": article.source.institution_name if article.source else "",
        "url": article.source_reference,
        "status": article.processing_status,
        "reasons": reasons[:8],
    }


def _view_record(view: ResearchView, article: Article | None, reasons: list[str]) -> dict[str, Any]:
    return {
        "view_id": view.id,
        "article_id": view.article_id,
        "title": article.title if article else "",
        "institution": view.institution,
        "canonical_cell_id": view.canonical_cell_id,
        "row_key": view.template_row_key,
        "region": view.region,
        "status": view.review_status,
        "reasons": reasons[:8],
    }
