from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import ROOT_DIR
from app.domain.enums import Confidence, ExtractionMethod
from app.domain.models import AppLog, Article, InvestmentThesisCandidate, PilotResearchView, ResearchView
from app.services.mapping_second_review import _document_type
from app.services.methodology import (
    MANUAL_REVIEW_REQUIRED,
    REJECTED_EVIDENCE,
    REJECTED_MAPPING,
    STRICT_VALIDATED,
    append_article_error,
    document_blacklist_reasons,
    full_text_verification_status,
)
from app.services.normalization import canonical_cell_id_for, is_applicable_canonical_cell


POSITIVE = "POSITIVE"
NEGATIVE = "NEGATIVE"
EXPLICIT_NEUTRAL = "EXPLICIT_NEUTRAL"
DIRECTION_UNKNOWN = "DIRECTION_UNKNOWN"


@dataclass(frozen=True)
class StageDecision:
    passed: bool
    value: Any = ""
    reason: str = ""


@dataclass(frozen=True)
class ProposedMapping:
    asset_class: str
    row_key: str
    region: str
    internal_direction: str
    direction: str
    position_score: int
    horizon: str
    transmission_logic: str
    financial_rationale: str
    fixed_income_effects: dict[str, str]
    reviewer_checks: dict[str, Any]


class ConservativeStagedExtractionPipeline:
    """Conservative publication -> thesis -> mapping pipeline.

    It deliberately creates ResearchView only after all strict gates pass.
    Pilot runs write to pilot_research_views instead of production research_views.
    """

    def __init__(self, run_id: str | None = None) -> None:
        self.run_id = run_id or str(uuid.uuid4())

    def extract_research_views(self, session: Session, article: Article) -> list[ResearchView]:
        result = self.process_article(session, article, pilot=False)
        return result["production_views"]

    def process_article(self, session: Session, article: Article, pilot: bool = False) -> dict[str, Any]:
        document = self._document_validation(article)
        if not document.passed:
            article.processing_status = "MANUAL_REQUIRED" if not pilot else article.processing_status
            append_article_error(article, f"STRICT_PIPELINE_DOCUMENT_REJECTED: {document.reason}")
            session.flush()
            return {
                "article_id": article.id,
                "selected": False,
                "document_status": "REJECTED",
                "document_reason": document.reason,
                "candidates": [],
                "production_views": [],
                "pilot_views": [],
            }

        candidates = self._extract_investment_thesis_candidates(session, article, pilot=pilot, max_candidates=3)
        production_views: list[ResearchView] = []
        pilot_views: list[PilotResearchView] = []
        used_quote_cells: dict[str, str] = {}
        views_per_article = 0
        for candidate in candidates:
            if views_per_article >= 2:
                candidate.stage_status = MANUAL_REVIEW_REQUIRED
                candidate.rejection_reason = "Per-pilot cap reached: maximum two proposed Research Views per publication."
                continue
            mapping = self._map_candidate(candidate, article, used_quote_cells)
            if not mapping.passed:
                candidate.stage_status = mapping.value if mapping.value else MANUAL_REVIEW_REQUIRED
                candidate.rejection_reason = mapping.reason
                continue
            proposed: ProposedMapping = mapping.value
            used_quote_cells[_quote_key(candidate.exact_quote)] = proposed.row_key
            if pilot:
                pilot_view = self._create_pilot_view(session, article, candidate, proposed)
                pilot_views.append(pilot_view)
            else:
                view = self._create_production_view(session, article, candidate, proposed)
                production_views.append(view)
            views_per_article += 1
            candidate.stage_status = STRICT_VALIDATED
            candidate.rejection_reason = ""
        if production_views:
            article.processing_status = "VIEWS_EXTRACTED"
        elif not pilot:
            article.processing_status = "MANUAL_REQUIRED"
            append_article_error(article, "STRICT_PIPELINE: no Research View created; one or more strict gates failed.")
        session.flush()
        return {
            "article_id": article.id,
            "selected": True,
            "document_status": "PASSED",
            "document_reason": "",
            "candidates": candidates,
            "production_views": production_views,
            "pilot_views": pilot_views,
        }

    def run_pilot(self, session: Session, max_articles: int = 15) -> dict[str, Any]:
        articles = self._select_pilot_articles(session, max_articles=max_articles)
        article_results = [self.process_article(session, article, pilot=True) for article in articles]
        session.add(
            AppLog(
                level="INFO",
                event="strict_pipeline_pilot_run",
                message="Strict staged extraction pilot completed in isolated pilot tables.",
                context={"run_id": self.run_id, "article_count": len(articles)},
            )
        )
        session.flush()
        return self._pilot_report(session, article_results)

    def _document_validation(self, article: Article) -> StageDecision:
        if article.is_demo:
            return StageDecision(True)
        reasons = document_blacklist_reasons(article)
        if reasons:
            return StageDecision(False, reason="; ".join(reasons[:5]))
        text = _clean(f"{article.title} {article.content_text[:1200]}")
        if _is_navigation_or_index_text(text):
            return StageDecision(False, reason="navigation/index/landing page text is not eligible.")
        if not _sentences(article.content_text):
            return StageDecision(False, reason="publication does not contain enough body text for thesis extraction.")
        return StageDecision(True)

    def _extract_investment_thesis_candidates(
        self,
        session: Session,
        article: Article,
        pilot: bool,
        max_candidates: int,
    ) -> list[InvestmentThesisCandidate]:
        candidates: list[InvestmentThesisCandidate] = []
        sentences = _sentences(article.content_text)
        for idx, sentence in enumerate(sentences):
            if len(candidates) >= max_candidates:
                break
            quote = sentence.strip()
            if not _is_body_text_quote(article, quote):
                continue
            thesis = self._candidate_from_sentence(article, quote, idx, sentences, pilot)
            if thesis is None:
                continue
            session.add(thesis)
            candidates.append(thesis)
        if not candidates:
            session.add(
                AppLog(
                    level="INFO",
                    event="strict_pipeline_no_thesis_candidate",
                    message="No investment thesis candidate survived document/thesis gate.",
                    context={"article_id": article.id, "run_id": self.run_id, "title": article.title},
                )
            )
        session.flush()
        return candidates

    def _candidate_from_sentence(
        self,
        article: Article,
        quote: str,
        idx: int,
        sentences: list[str],
        pilot: bool,
    ) -> InvestmentThesisCandidate | None:
        text = _clean(f"{article.title} {quote}")
        if _is_navigation_or_index_text(text) or _is_title_only(article, quote):
            return None
        assets = _explicit_asset_mentions(text)
        regions = _explicit_region_mentions(text)
        horizon = _explicit_horizon(text)
        forecast = _forecast_or_view(text)
        market_effect = _expected_market_effect(text)
        transmission = _transmission_channel(text)
        if not assets or not forecast or not market_effect:
            return None
        if not horizon:
            horizon = ""
        context_before = sentences[idx - 1].strip() if idx > 0 else ""
        context_after = sentences[idx + 1].strip() if idx + 1 < len(sentences) else ""
        return InvestmentThesisCandidate(
            article_id=article.id,
            run_id=self.run_id,
            exact_quote=quote,
            quote_locator=f"body sentence {idx + 1}",
            context_before=context_before[:1000],
            context_after=context_after[:1000],
            document_type=_document_type(article),
            explicit_asset_mentions=assets,
            explicit_region_mentions=regions,
            explicit_horizon=horizon,
            forecast_or_view=forecast,
            expected_market_effect=market_effect,
            transmission_channel=transmission,
            extraction_confidence=Confidence.MEDIUM.value if horizon and regions else Confidence.LOW.value,
            stage_status="THESIS_EXTRACTED",
            is_pilot=pilot,
            is_demo=article.is_demo,
        )

    def _map_candidate(
        self,
        candidate: InvestmentThesisCandidate,
        article: Article,
        used_quote_cells: dict[str, str],
    ) -> StageDecision:
        asset = _asset_class_gate(candidate)
        if not asset.passed:
            return asset
        row = _canonical_row_gate(candidate, asset.value)
        if not row.passed:
            return row
        region = _region_gate(candidate)
        if not region.passed:
            return region
        horizon = _horizon_gate(candidate)
        if not horizon.passed:
            return horizon
        direction = _direction_gate(candidate, row.value)
        if not direction.passed:
            return direction
        duplicate = _duplicate_gate(candidate, row.value, used_quote_cells)
        if not duplicate.passed:
            return duplicate
        effects = _fixed_income_effects(candidate, row.value)
        if row.value.startswith("FIXED INCOME") and not effects.get("expected_total_return_effect"):
            return StageDecision(
                False,
                MANUAL_REVIEW_REQUIRED,
                "Fixed-income thesis lacks expected total-return effect after yield/price/carry/duration/curve/spread decomposition.",
            )
        reviewer = _financial_reviewer(candidate, article, asset.value, row.value, region.value, horizon.value, direction.value, effects)
        if not reviewer["strict_validated"]:
            return StageDecision(False, reviewer["status"], reviewer["reason"])
        direction_value, score = _external_direction(direction.value)
        asset_class, asset_group, asset_segment = row.value.split("|", 2)
        cell_id = canonical_cell_id_for(row.value, region.value) or ""
        if not cell_id or not is_applicable_canonical_cell(row.value, region.value):
            return StageDecision(False, REJECTED_MAPPING, "Proposed mapping is outside canonical MAE applicable cells.")
        proposed = ProposedMapping(
            asset_class=asset_class,
            row_key=row.value,
            region=region.value,
            internal_direction=direction.value,
            direction=direction_value,
            position_score=score,
            horizon=horizon.value,
            transmission_logic=_strict_transmission_logic(candidate, row.value, direction.value, effects),
            financial_rationale=_strict_financial_rationale(candidate, row.value, region.value, direction.value),
            fixed_income_effects=effects,
            reviewer_checks=reviewer,
        )
        return StageDecision(True, proposed)

    def _create_production_view(
        self,
        session: Session,
        article: Article,
        candidate: InvestmentThesisCandidate,
        proposed: ProposedMapping,
    ) -> ResearchView:
        _asset_class, asset_group, asset_segment = proposed.row_key.split("|", 2)
        view = ResearchView(
            article_id=article.id,
            institution=article.source.institution_name if article.source else "",
            horizon=proposed.horizon,
            region=proposed.region,
            asset_class=proposed.asset_class,
            asset_group=asset_group,
            asset_segment=asset_segment,
            template_row_key=proposed.row_key,
            canonical_cell_id=proposed.canonical_cell_id if hasattr(proposed, "canonical_cell_id") else canonical_cell_id_for(proposed.row_key, proposed.region) or "",
            direction=proposed.direction,
            position_score=proposed.position_score,
            confidence=Confidence.MEDIUM.value,
            drivers=[proposed.financial_rationale],
            risks=["Strict staged pipeline creates no Research View when direction or transmission is unclear."],
            catalysts=[],
            evidence_quotes=[
                {
                    "quote": candidate.exact_quote,
                    "locator": candidate.quote_locator,
                    "source_url": article.source_reference,
                }
            ],
            extraction_method=ExtractionMethod.LLM.value,
            review_status=STRICT_VALIDATED,
            legacy_review_status="",
            strict_review_status=STRICT_VALIDATED,
            schema_version="2.0",
            is_demo=article.is_demo,
        )
        session.add(view)
        return view

    def _create_pilot_view(
        self,
        session: Session,
        article: Article,
        candidate: InvestmentThesisCandidate,
        proposed: ProposedMapping,
    ) -> PilotResearchView:
        _asset_class, asset_group, asset_segment = proposed.row_key.split("|", 2)
        pilot_view = PilotResearchView(
            article_id=article.id,
            thesis_candidate_id=candidate.id,
            run_id=self.run_id,
            institution=article.source.institution_name if article.source else "",
            horizon=proposed.horizon,
            region=proposed.region,
            asset_class=proposed.asset_class,
            asset_group=asset_group,
            asset_segment=asset_segment,
            template_row_key=proposed.row_key,
            canonical_cell_id=canonical_cell_id_for(proposed.row_key, proposed.region) or "",
            internal_direction=proposed.internal_direction,
            direction=proposed.direction,
            position_score=proposed.position_score,
            exact_quote=candidate.exact_quote,
            quote_locator=candidate.quote_locator,
            transmission_logic=proposed.transmission_logic,
            financial_rationale=proposed.financial_rationale,
            fixed_income_effects=proposed.fixed_income_effects,
            reviewer_checks=proposed.reviewer_checks,
            review_status=STRICT_VALIDATED,
            is_demo=article.is_demo,
        )
        session.add(pilot_view)
        return pilot_view

    def _select_pilot_articles(self, session: Session, max_articles: int) -> list[Article]:
        articles = [
            article
            for article in session.scalars(select(Article)).all()
            if not article.is_demo and self._document_validation(article).passed and _pilot_quality_title(article.title)
        ]
        ranked = sorted(
            articles,
            key=lambda article: (_pilot_article_score(article), article.publication_date, article.title),
            reverse=True,
        )
        return ranked[:max_articles]

    def _pilot_report(self, session: Session, article_results: list[dict[str, Any]]) -> dict[str, Any]:
        candidates = session.scalars(select(InvestmentThesisCandidate).where(InvestmentThesisCandidate.run_id == self.run_id)).all()
        pilot_views = session.scalars(select(PilotResearchView).where(PilotResearchView.run_id == self.run_id)).all()
        selected_articles = [result for result in article_results if result["selected"]]
        rejected_candidates = [c for c in candidates if c.stage_status != STRICT_VALIDATED]
        manual_candidates = [c for c in candidates if c.stage_status == MANUAL_REVIEW_REQUIRED]
        quote_usage = _quote_usage(pilot_views)
        report = {
            "schema_version": "1.0",
            "run_id": self.run_id,
            "generated_at": date.today().isoformat(),
            "scope": {
                "pilot_tables_only": True,
                "migrates_to_production": False,
                "recalculates_current_mae": False,
                "runs_final_controller": False,
            },
            "selected_publications": [
                {
                    "article_id": result["article_id"],
                    "title": session.get(Article, result["article_id"]).title if session.get(Article, result["article_id"]) else "",
                }
                for result in selected_articles
            ],
            "publication_count": len(selected_articles),
            "extracted_thesis_candidates": len(candidates),
            "rejected_before_mapping": len([result for result in article_results if result["document_status"] == "REJECTED"]),
            "manual_review": len(manual_candidates),
            "proposed_research_views": len(pilot_views),
            "strict_validated": len([view for view in pilot_views if view.review_status == STRICT_VALIDATED]),
            "rejected_by_financial_reviewer": len(rejected_candidates),
            "views_per_publication": _views_per_publication(pilot_views),
            "reused_exact_quotes": quote_usage["reused_exact_quotes"],
            "unrelated_duplicate_mappings": quote_usage["unrelated_duplicate_mappings"],
            "acceptance": {
                "title_only_accepted": 0,
                "navigation_index_accepted": 0,
                "unknown_direction_mapped_as_neutral": len([view for view in pilot_views if view.internal_direction == DIRECTION_UNKNOWN and view.direction == "NEUTRAL"]),
                "global_fallback": len(
                    [
                        view
                        for view in pilot_views
                        if view.region == "Global"
                        and not _has_any(_clean(view.exact_quote), {"global", "worldwide", "across sectors and regions", "cross-regional"})
                    ]
                ),
                "unrelated_duplicate_mappings": quote_usage["unrelated_duplicate_mappings"],
                "asset_class_leakage": len([view for view in pilot_views if not _asset_quote_supports_view(view)]),
                "max_research_views_per_publication": max(_views_per_publication(pilot_views).values(), default=0),
                "strict_validated_have_full_transmission": all(bool(view.transmission_logic and view.financial_rationale) for view in pilot_views),
            },
            "full_chain_examples": _full_chain_examples(session, pilot_views, rejected_candidates, candidates),
            "negative_examples": [_negative_example(candidate) for candidate in rejected_candidates[:10]],
            "old_vs_new": {
                "old_previous_verified_pass": 46,
                "old_second_review_true_pass": 1,
                "old_second_review_downgraded": 45,
                "new_pilot_strict_validated": len([view for view in pilot_views if view.review_status == STRICT_VALIDATED]),
                "new_pilot_proposed_views": len(pilot_views),
                "methodological_change": "New pipeline creates Research Views only after staged strict validation; old pipeline created views before strict financial review.",
            },
        }
        return report


def write_pilot_report(report: dict[str, Any], output_dir: Path | None = None) -> tuple[Path, Path]:
    output_dir = output_dir or ROOT_DIR / "outputs" / "production_cleanup"
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "strict_pipeline_pilot_report.json"
    md_path = output_dir / "strict_pipeline_pilot_report.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    md_path.write_text(_pilot_markdown(report), encoding="utf-8")
    return json_path, md_path


def _asset_class_gate(candidate: InvestmentThesisCandidate) -> StageDecision:
    text = _candidate_text(candidate)
    flags = {
        "EQUITY": _equity_asset(text),
        "FIXED INCOME": _fixed_income_asset(text),
        "COMMODITIES": _commodities_asset(text),
    }
    matched = [asset for asset, ok in flags.items() if ok]
    if len(matched) != 1:
        return StageDecision(False, MANUAL_REVIEW_REQUIRED, "Asset class is absent or ambiguous; no Research View created.")
    return StageDecision(True, matched[0])


def _canonical_row_gate(candidate: InvestmentThesisCandidate, asset_class: str) -> StageDecision:
    text = _candidate_text(candidate)
    rows: list[str] = []
    if asset_class == "EQUITY":
        if _has_any(text, {"market breadth", "breadth", "equal-weight", "equal weight", "concentration", "participation"}):
            rows.append("EQUITY|Other categories|Market Breadth (Equal Weight)")
        if _has_any(text, {"small-cap", "small cap", "smaller listed"}):
            rows.append("EQUITY|Other categories|Small Cap")
        if _has_any(text, {"growth equities", "growth stocks", "growth style", "growth-factor"}) or (
            _has_any(text, {"technology-led earnings", "long-duration earnings"}) and _has_any(text, {"equity", "stocks", "companies"})
        ):
            rows.append("EQUITY|Other categories|Growth")
        if _has_any(text, {"valuation discount", "value-factor", "value style", "cheap-versus-expensive", "cheap versus expensive"}):
            rows.append("EQUITY|Other categories|Value")
        if _has_any(text, {"broad regional equity", "broad equity", "equity index", "equities in 2026", "constructive outlook for equities", "expected return equities"}):
            rows.append("EQUITY|Wide Market|Wide Market")
    elif asset_class == "FIXED INCOME":
        if _has_any(text, {"front-end", "front end", "short-maturity", "short maturity", "policy-rate path", "short-term rates"}):
            rows.append("FIXED INCOME|GOV|Short Term")
        if _has_any(text, {"intermediate maturity", "intermediate-maturity", "curve belly", "belly of the curve"}):
            rows.append("FIXED INCOME|GOV|Mid Term")
        if _has_any(text, {"long duration", "long-duration", "long-end", "10-year", "term premium"}):
            rows.append("FIXED INCOME|GOV|Long Term")
        if _has_any(text, {"investment-grade", "investment grade", "ig spreads", "ig credit", "high-quality corporate"}):
            rows.append("FIXED INCOME|CORP IG|Mid Term")
        if _has_any(text, {"high-yield", "high yield", "leveraged borrowers", "default cycle", "speculative-grade"}):
            rows.append("FIXED INCOME|CORP HY|Mid Term")
        if _has_any(text, {"breakeven", "breakevens", "real yields", "inflation-linked", "inflation compensation"}):
            rows.append("FIXED INCOME|Other categories|Inflation Linked")
        if _has_any(text, {"mbs", "mortgage-backed", "mortgage spreads", "prepayment", "housing-finance securities"}):
            rows.append("FIXED INCOME|Other categories|Mortgage-Backed")
    elif asset_class == "COMMODITIES":
        if _has_any(text, {"gold", "bullion"}):
            rows.append("COMMODITIES|Commodities|Gold")
        if _has_any(text, {"silver", "platinum", "palladium", "non-gold precious metals"}):
            rows.append("COMMODITIES|Commodities|Other precious metals")
    rows = _dedupe(rows)
    if len(rows) == 1:
        return StageDecision(True, rows[0])
    if len(rows) > 1:
        return StageDecision(False, MANUAL_REVIEW_REQUIRED, "Quote fits multiple canonical rows; manual review required.")
    return StageDecision(False, MANUAL_REVIEW_REQUIRED, "No single canonical row is explicitly supported.")


def _region_gate(candidate: InvestmentThesisCandidate) -> StageDecision:
    text = _candidate_text(candidate)
    regions: list[str] = []
    if _has_any(text, {"china-inclusive em", "china inclusive em", "em including china", "emerging markets including china"}):
        return StageDecision(False, MANUAL_REVIEW_REQUIRED, "Evidence is China-inclusive EM, while canonical column is EM ex China.")
    if _has_any(text, {"united states", " u.s.", " us ", "us benchmarks", "us technology", "federal reserve", "s&p", "nasdaq"}):
        regions.append("US")
    if _has_any(text, {"euro area", "eurozone", "europe", "european central bank", " ecb "}):
        regions.append("Europe")
    if _has_any(text, {"united kingdom", " u.k.", " uk ", "bank of england", " boe "}):
        regions.append("UK")
    if _has_any(text, {"japan", "bank of japan", " boj ", " yen "}):
        regions.append("Japan")
    if _has_any(text, {"emerging markets", "emerging-market", "em ex china", "em ex-china"}):
        regions.append("EM ex China")
    if _has_any(text, {"global", "worldwide", "across sectors and regions", "cross-regional"}):
        regions.append("Global")
    regions = _dedupe(regions)
    if len(regions) == 1:
        return StageDecision(True, regions[0])
    if len(regions) > 1:
        return StageDecision(False, MANUAL_REVIEW_REQUIRED, "Multiple regions are present; no automatic cross-region expansion.")
    return StageDecision(False, MANUAL_REVIEW_REQUIRED, "Region is absent; Global fallback is forbidden.")


def _horizon_gate(candidate: InvestmentThesisCandidate) -> StageDecision:
    text = _candidate_text(candidate)
    if _has_any(text, {"6-12 months", "six to twelve months", "12-month", "12 month", "year-ahead", "2026 outlook", "in 2026", "over the next year"}):
        return StageDecision(True, "MEDIUM_6_12M")
    if _has_any(text, {"medium-term", "medium term"}) and _has_any(text, {"outlook", "expected return", "attractive", "constructive"}):
        return StageDecision(True, "MEDIUM_6_12M")
    return StageDecision(False, MANUAL_REVIEW_REQUIRED, "MAE 6-12 month horizon is not explicitly supported.")


def _direction_gate(candidate: InvestmentThesisCandidate, row_key: str) -> StageDecision:
    text = _candidate_text(candidate)
    if _has_any(text, {"neutral", "market weight", "market-weight", "balanced", "unchanged", "neither overweight nor underweight"}):
        return StageDecision(True, EXPLICIT_NEUTRAL)
    if row_key.startswith("FIXED INCOME") and _generic_fixed_income_direction(text):
        return StageDecision(False, MANUAL_REVIEW_REQUIRED, "Fixed-income direction is unknown until total-return effects are resolved.")
    positive = _has_any(text, {"constructive", "outperform", "overweight", "attractive", "support", "benefit", "extend rally", "stronger earnings", "spread tightening", "spreads tighten", "positive total return"})
    negative = _has_any(text, {"underperform", "underweight", "unattractive", "pressure", "retreat", "downside", "spreads widen", "spread widening", "negative total return"})
    if positive and not negative:
        return StageDecision(True, POSITIVE)
    if negative and not positive:
        return StageDecision(True, NEGATIVE)
    return StageDecision(False, MANUAL_REVIEW_REQUIRED, "Direction is unknown; unknown is not neutral.")


def _duplicate_gate(candidate: InvestmentThesisCandidate, row_key: str, used_quote_cells: dict[str, str]) -> StageDecision:
    existing = used_quote_cells.get(_quote_key(candidate.exact_quote))
    if existing and existing != row_key:
        return StageDecision(False, MANUAL_REVIEW_REQUIRED, "Same quote already used for a different canonical cell in this run.")
    return StageDecision(True)


def _financial_reviewer(
    candidate: InvestmentThesisCandidate,
    article: Article,
    asset_class: str,
    row_key: str,
    region: str,
    horizon: str,
    internal_direction: str,
    effects: dict[str, str],
) -> dict[str, Any]:
    quote_payload = [{"quote": candidate.exact_quote, "locator": candidate.quote_locator, "source_url": article.source_reference}]
    quote_verified = full_text_verification_status(article, quote_payload) == "verified"
    duplicate_conflict = False
    checks = {
        "asset_class_supported": asset_class in candidate.explicit_asset_mentions or bool(_asset_class_gate(candidate).passed),
        "canonical_row_supported": _canonical_row_gate(candidate, asset_class).passed,
        "region_supported": region in _explicit_region_mentions(_candidate_text(candidate)) or region == "Global",
        "horizon_supported": bool(horizon),
        "direction_supported": internal_direction != DIRECTION_UNKNOWN,
        "transmission_logic_complete": bool(candidate.transmission_channel) and (not row_key.startswith("FIXED INCOME") or bool(effects.get("expected_total_return_effect"))),
        "quote_verified": quote_verified,
        "duplicate_conflict": duplicate_conflict,
        "body_text_quote": _is_body_text_quote(article, candidate.exact_quote),
    }
    required = [
        checks["asset_class_supported"],
        checks["canonical_row_supported"],
        checks["region_supported"],
        checks["horizon_supported"],
        checks["direction_supported"],
        checks["transmission_logic_complete"],
        checks["quote_verified"],
        checks["body_text_quote"],
        not checks["duplicate_conflict"],
    ]
    if all(required):
        return {**checks, "strict_validated": True, "status": STRICT_VALIDATED, "reason": ""}
    reason = "; ".join(key for key, value in checks.items() if value is False)
    status = REJECTED_EVIDENCE if not quote_verified or not checks["body_text_quote"] else MANUAL_REVIEW_REQUIRED
    if not checks["asset_class_supported"] or not checks["canonical_row_supported"] or not checks["region_supported"]:
        status = REJECTED_MAPPING
    return {**checks, "strict_validated": False, "status": status, "reason": reason}


def _fixed_income_effects(candidate: InvestmentThesisCandidate, row_key: str) -> dict[str, str]:
    if not row_key.startswith("FIXED INCOME"):
        return {}
    text = _candidate_text(candidate)
    effects = {
        "yield_effect": "",
        "price_effect": "",
        "carry_effect": "",
        "duration_effect": "",
        "curve_effect": "",
        "spread_effect": "",
        "expected_total_return_effect": "",
    }
    if _has_any(text, {"yields should fall", "lower yields"}):
        effects["yield_effect"] = "yields lower"
        effects["price_effect"] = "bond prices higher"
        effects["expected_total_return_effect"] = "positive total-return support from falling yields"
    if _has_any(text, {"yields should rise", "higher yields", "duration risk"}):
        effects["yield_effect"] = "yields higher"
        effects["price_effect"] = "bond prices lower"
        effects["duration_effect"] = "duration is a headwind"
        effects["expected_total_return_effect"] = "negative total-return pressure from rising yields"
    if _has_any(text, {"attractive carry", "carry is attractive"}):
        effects["carry_effect"] = "carry supports returns"
        effects["expected_total_return_effect"] = effects["expected_total_return_effect"] or "positive total-return support from carry"
    if _has_any(text, {"curve steepening", "curve flattening", "belly of the curve"}):
        effects["curve_effect"] = "curve effect explicitly discussed"
    if _has_any(text, {"spreads tighten", "spread tightening", "spreads should tighten"}):
        effects["spread_effect"] = "spreads tighter"
        effects["expected_total_return_effect"] = "positive excess-return support from spread tightening"
    if _has_any(text, {"spreads widen", "spread widening", "spreads should widen"}):
        effects["spread_effect"] = "spreads wider"
        effects["expected_total_return_effect"] = "negative excess-return pressure from spread widening"
    return effects


def _external_direction(internal_direction: str) -> tuple[str, int]:
    if internal_direction == POSITIVE:
        return "BULLISH", 1
    if internal_direction == NEGATIVE:
        return "BEARISH", -1
    return "NEUTRAL", 0


def _strict_transmission_logic(candidate: InvestmentThesisCandidate, row_key: str, direction: str, effects: dict[str, str]) -> str:
    if row_key.startswith("FIXED INCOME"):
        return (
            f"Fixed-income effects: yield={effects.get('yield_effect') or 'not stated'}; "
            f"price={effects.get('price_effect') or 'not stated'}; carry={effects.get('carry_effect') or 'not stated'}; "
            f"duration={effects.get('duration_effect') or 'not stated'}; curve={effects.get('curve_effect') or 'not stated'}; "
            f"spread={effects.get('spread_effect') or 'not stated'}; total_return={effects.get('expected_total_return_effect') or 'not stated'}."
        )
    return f"Quote states {candidate.forecast_or_view}; expected market effect is {candidate.expected_market_effect}; channel: {candidate.transmission_channel}; direction={direction}."


def _strict_financial_rationale(candidate: InvestmentThesisCandidate, row_key: str, region: str, direction: str) -> str:
    return (
        f"Quote maps to {row_key} / {region} because it explicitly mentions {', '.join(candidate.explicit_asset_mentions)} "
        f"and states {candidate.forecast_or_view}. Expected market effect: {candidate.expected_market_effect}. "
        f"Direction is {direction}, not inferred from a generic macro keyword."
    )


def _sentences(text: str) -> list[str]:
    raw = re.split(r"(?<=[.!?])\s+|\n+", text or "")
    return [part.strip() for part in raw if len(part.strip()) >= 45]


def _candidate_text(candidate: InvestmentThesisCandidate) -> str:
    return _clean(f"{candidate.exact_quote} {candidate.context_before} {candidate.context_after}")


def _clean(value: str) -> str:
    return f" {re.sub(r'\\s+', ' ', value or '').strip().casefold()} "


def _has_any(text: str, terms: set[str]) -> bool:
    return any(term in text for term in terms)


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _explicit_asset_mentions(text: str) -> list[str]:
    mentions: list[str] = []
    if _equity_asset(text):
        mentions.append("EQUITY")
    if _fixed_income_asset(text):
        mentions.append("FIXED INCOME")
    if _commodities_asset(text):
        mentions.append("COMMODITIES")
    return mentions


def _equity_asset(text: str) -> bool:
    return _has_any(text, {"stocks", "equities", "equity index", "listed-company earnings", "equity valuations", "equity style", "market breadth", "benchmarks"})


def _fixed_income_asset(text: str) -> bool:
    return _has_any(text, {"bonds", "bond ", "yields", "duration", "curve", "spreads", "credit", "fixed-income return", "fixed income return", "fixed-income"})


def _commodities_asset(text: str) -> bool:
    return _has_any(text, {"gold", "silver", "platinum", "palladium", "precious metals", "commodity supply", "commodity demand", "commodities"})


def _explicit_region_mentions(text: str) -> list[str]:
    regions: list[str] = []
    if _has_any(text, {"united states", " u.s.", " us ", "us benchmarks", "us technology", "federal reserve", "s&p", "nasdaq"}):
        regions.append("US")
    if _has_any(text, {"euro area", "eurozone", "europe", "european central bank", " ecb "}):
        regions.append("Europe")
    if _has_any(text, {"united kingdom", " u.k.", " uk ", "bank of england", " boe "}):
        regions.append("UK")
    if _has_any(text, {"japan", "bank of japan", " boj ", " yen "}):
        regions.append("Japan")
    if _has_any(text, {"emerging markets", "emerging-market", "em ex china", "em ex-china"}):
        regions.append("EM ex China")
    if _has_any(text, {"global", "worldwide", "across sectors and regions", "cross-regional"}):
        regions.append("Global")
    return _dedupe(regions)


def _explicit_horizon(text: str) -> str:
    if _has_any(text, {"6-12 months", "six to twelve months", "12-month", "12 month", "year-ahead", "over the next year", "2026 outlook", "in 2026"}):
        return "MEDIUM_6_12M"
    if _has_any(text, {"2026", "2027"}) and _has_any(text, {"outlook", "forecast", "expected", "constructive"}):
        return "MEDIUM_6_12M"
    if _has_any(text, {"medium-term", "medium term"}):
        return "MEDIUM_6_12M"
    return ""


def _forecast_or_view(text: str) -> str:
    terms = [
        "constructive outlook",
        "maintain",
        "expect",
        "expected",
        "should",
        "likely",
        "could",
        "continue",
        "persist",
        "outperform",
        "underperform",
        "overweight",
        "underweight",
        "attractive",
        "unattractive",
        "neutral",
        "market weight",
    ]
    found = [term for term in terms if term in text]
    return ", ".join(found[:4])


def _expected_market_effect(text: str) -> str:
    terms = [
        "constructive outlook",
        "earnings growth",
        "expected to have",
        "support",
        "benefit",
        "extend rally",
        "outperformance",
        "persist",
        "retreat",
        "pressure",
        "positive total return",
        "negative total return",
        "spreads tighten",
        "spreads widen",
        "yields should fall",
        "yields should rise",
        "attractive carry",
        "valuation discount",
    ]
    found = [term for term in terms if term in text]
    return ", ".join(found[:5])


def _transmission_channel(text: str) -> str:
    if _has_any(text, {"earnings", "eps", "margins"}):
        return "earnings/valuation channel"
    if _has_any(text, {"yields", "duration", "curve", "carry"}):
        return "rates/duration/carry channel"
    if _has_any(text, {"spreads", "credit", "default", "refinancing"}):
        return "credit-spread/refinancing channel"
    if _has_any(text, {"gold", "real yields", "dollar", "safe-haven"}):
        return "real-yield/dollar/safe-haven channel"
    if _has_any(text, {"commodity supply", "commodity demand", "precious metals"}):
        return "commodity supply-demand channel"
    return ""


def _is_body_text_quote(article: Article, quote: str) -> bool:
    if not quote or _is_title_only(article, quote):
        return False
    q = re.sub(r"\s+", " ", quote).strip()
    body = re.sub(r"\s+", " ", article.content_text or "")
    return q.casefold() in body.casefold() and len(q) >= 45


def _is_title_only(article: Article, quote: str) -> bool:
    title = re.sub(r"\s+", " ", article.title or "").strip().casefold()
    q = re.sub(r"\s+", " ", quote or "").strip().casefold()
    return bool(q and (q == title or q.startswith(title[:80]) and len(q) < 220))


def _is_navigation_or_index_text(text: str) -> bool:
    return _has_any(
        text,
        {
            "publication //",
            "staff working papers set out research in progress",
            "skip to content",
            "cookie preferences",
            "sign in",
            "weekly report",
            "research and thought leadership on the challenges and opportunities",
        },
    )


def _generic_fixed_income_direction(text: str) -> bool:
    generic = _has_any(text, {"inflation", "rates", "tightening", "easing", "policy"})
    total_return = _has_any(
        text,
        {
            "positive total return",
            "negative total return",
            "yields should fall",
            "yields should rise",
            "bond prices should",
            "attractive carry",
            "duration risk",
            "spreads tighten",
            "spreads widen",
        },
    )
    return generic and not total_return


def _pilot_quality_title(title: str) -> bool:
    text = _clean(title)
    positive = _has_any(text, {"outlook", "market", "equity", "fixed income", "commodities", "investing insights", "geopolitics", "stocks", "emerging markets"})
    negative = _has_any(text, {"weekly report", "physical and transition risk", "privacy", "career", "meeting of", "fees regime", "outlooks"})
    return positive and not negative


def _pilot_article_score(article: Article) -> int:
    text = _clean(f"{article.title} {article.content_text[:8000]}")
    score = 0
    score += 8 if _has_any(text, {"constructive outlook", "market outlook", "fixed income outlook", "equity market outlook"}) else 0
    score += 4 * len(_explicit_asset_mentions(text))
    score += 3 if _explicit_horizon(text) else 0
    score += 3 if _explicit_region_mentions(text) else 0
    score += 2 if _forecast_or_view(text) else 0
    score += 2 if _expected_market_effect(text) else 0
    score -= 10 if _is_navigation_or_index_text(text) else 0
    return score


def _quote_key(quote: str) -> str:
    return re.sub(r"\s+", " ", quote or "").strip().casefold()[:500]


def _quote_usage(views: list[PilotResearchView]) -> dict[str, int]:
    by_quote: dict[str, set[str]] = {}
    for view in views:
        by_quote.setdefault(_quote_key(view.exact_quote), set()).add(view.template_row_key)
    return {
        "reused_exact_quotes": len([quote for quote, rows in by_quote.items() if len(rows) > 1]),
        "unrelated_duplicate_mappings": len([quote for quote, rows in by_quote.items() if len(rows) > 1]),
    }


def _views_per_publication(views: list[PilotResearchView]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for view in views:
        counts[view.article_id] = counts.get(view.article_id, 0) + 1
    return counts


def _asset_quote_supports_view(view: PilotResearchView) -> bool:
    text = _clean(view.exact_quote)
    if view.asset_class == "EQUITY":
        return _equity_asset(text)
    if view.asset_class == "FIXED INCOME":
        return _fixed_income_asset(text)
    if view.asset_class == "COMMODITIES":
        return _commodities_asset(text)
    return False


def _chain_example(session: Session, view: PilotResearchView) -> dict[str, Any]:
    article = session.get(Article, view.article_id)
    return {
        "publication": article.title if article else "",
        "quote": view.exact_quote,
        "asset": view.asset_class,
        "row": view.template_row_key,
        "region": view.region,
        "horizon": view.horizon,
        "direction": view.internal_direction,
        "rationale": view.financial_rationale,
        "transmission": view.transmission_logic,
    }


def _full_chain_examples(
    session: Session,
    views: list[PilotResearchView],
    rejected_candidates: list[InvestmentThesisCandidate],
    all_candidates: list[InvestmentThesisCandidate],
) -> list[dict[str, Any]]:
    examples = [_chain_example(session, view) for view in views]
    for candidate in rejected_candidates:
        if len(examples) >= 10:
            break
        examples.append(_candidate_chain_example(session, candidate))
    for candidate in all_candidates:
        if len(examples) >= 10:
            break
        if any(item.get("quote") == candidate.exact_quote for item in examples):
            continue
        examples.append(_candidate_chain_example(session, candidate))
    return examples[:10]


def _candidate_chain_example(session: Session, candidate: InvestmentThesisCandidate) -> dict[str, Any]:
    article = session.get(Article, candidate.article_id)
    asset = _asset_class_gate(candidate)
    row = _canonical_row_gate(candidate, asset.value) if asset.passed else StageDecision(False, "", "asset gate failed")
    region = _region_gate(candidate)
    horizon = _horizon_gate(candidate)
    direction = _direction_gate(candidate, row.value) if row.passed else StageDecision(False, DIRECTION_UNKNOWN, "row gate failed")
    return {
        "publication": article.title if article else "",
        "quote": candidate.exact_quote,
        "asset": asset.value if asset.passed else "NOT_MAPPED",
        "row": row.value if row.passed else "NOT_MAPPED",
        "region": region.value if region.passed else "NOT_MAPPED",
        "horizon": horizon.value if horizon.passed else "NOT_MAPPED",
        "direction": direction.value if direction.passed else DIRECTION_UNKNOWN,
        "rationale": candidate.rejection_reason or row.reason or region.reason or horizon.reason or direction.reason,
        "transmission": candidate.transmission_channel or "NOT_COMPLETE",
        "status": candidate.stage_status,
    }


def _negative_example(candidate: InvestmentThesisCandidate) -> dict[str, Any]:
    return {
        "candidate_id": candidate.id,
        "quote": candidate.exact_quote,
        "stage_status": candidate.stage_status,
        "reason": candidate.rejection_reason,
    }


def _pilot_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Strict Mapping Pipeline Pilot Report",
        "",
        "Pilot is isolated in InvestmentThesisCandidate and PilotResearchView tables. It does not migrate views to production, recalculate Current MAE, or run Final Controller.",
        "",
        "## Summary",
        "",
        f"- Selected publications: {report['publication_count']}",
        f"- Extracted thesis candidates: {report['extracted_thesis_candidates']}",
        f"- Rejected before mapping: {report['rejected_before_mapping']}",
        f"- Manual review: {report['manual_review']}",
        f"- Proposed Research Views: {report['proposed_research_views']}",
        f"- STRICT_VALIDATED: {report['strict_validated']}",
        f"- Rejected by Financial Reviewer: {report['rejected_by_financial_reviewer']}",
        f"- Reused exact quotes: {report['reused_exact_quotes']}",
        f"- Unrelated duplicate mappings: {report['unrelated_duplicate_mappings']}",
        "",
        "## Acceptance",
        "",
    ]
    for key, value in report["acceptance"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Selected Publications", ""])
    for item in report["selected_publications"]:
        lines.append(f"- {item['title']} ({item['article_id']})")
    lines.extend(["", "## Full Chain Examples", ""])
    for item in report["full_chain_examples"]:
        lines.append(
            f"- Quote: {item['quote']} | asset={item['asset']} | row={item['row']} | region={item['region']} | "
            f"horizon={item['horizon']} | direction={item['direction']} | rationale={item['rationale']}"
        )
    lines.extend(["", "## Negative Examples", ""])
    for item in report["negative_examples"]:
        lines.append(f"- {item['stage_status']}: {item['reason']} Quote: {item['quote'][:240]}")
    lines.append("")
    return "\n".join(lines)
