from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.domain.models import Article, ResearchView
from app.domain.schemas import ResearchViewDraft


BLACKLIST_TITLE_URL_TERMS = {
    "enforcement action",
    "aml",
    "sanction",
    "sanctions",
    "career",
    "careers",
    "job",
    "vacancy",
    "product launch",
    "marketing",
    "etf promotion",
    "corporate news",
    "press release",
    "sustainability report",
    "cookie",
    "privacy",
    "contact",
    "service description",
    "about us",
}

BOILERPLATE_TEXT_TERMS = {
    "accept cookies",
    "cookie preferences",
    "sign in",
    "subscribe to our newsletter",
    "skip to content",
    "all rights reserved",
}

SECURITY_BANNER_TERMS = {
    "official websites use .gov",
    "secure .gov websites use https",
    "locked padlock icon",
    "share sensitive information only on official, secure websites",
}

NAVIGATION_QUOTE_TERMS = {
    "accept cookies",
    "cookie preferences",
    "sign in",
    "skip to content",
    "skip to main content",
    "back to menu",
    "share by email",
    "share via email",
    "share on facebook",
    "share on linkedin",
    "share by twitter",
    "more like this less like this",
    "jump to featured outlooks",
    "read more insights",
    "ask research assistant",
    "you're now leaving",
}

GENERIC_MARKETING_QUOTE_TERMS = {
    "we provide tailored credit, financing, treasury and payment solutions",
    "our dedicated experts deliver industry-specific guidance and services",
    "help clients achieve their long-term strategic goals through financing",
    "learn more about how we cultivate and harness world-class intellectual capital",
    "we harness every resource, insight, relationship, and competitive advantage",
    "solutions capabilities insights about us support",
}

ARTICLE_IRRELEVANT_TERMS = {
    "world cup",
    "pet industry",
    "payments outlook",
    "multifamily market",
    "tokenisation as a catalyst",
}

VERIFIED_PASS = "VERIFIED_PASS"
STRICT_VALIDATED = "STRICT_VALIDATED"
MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"
REJECTED_MAPPING = "REJECTED_MAPPING"
REJECTED_EVIDENCE = "REJECTED_EVIDENCE"
ARCHIVED_LEGACY = "ARCHIVED_LEGACY"
INVALID_MAPPING = "INVALID_MAPPING"
INVALID_EVIDENCE = "INVALID_EVIDENCE"
IRRELEVANT_DOCUMENT = "IRRELEVANT_DOCUMENT"
BOILERPLATE = "BOILERPLATE"
MANUAL_REVIEW = "MANUAL_REVIEW"

FULL_TEXT_VERIFIED = "verified"
FULL_TEXT_NOT_VERIFIED = "not verified"
FULL_TEXT_MISSING = "missing"
FULL_TEXT_PARTIAL = "partial"
FULL_TEXT_FAILED = "failed"

FAIL_RESEARCH_VIEW_STATUSES = {
    INVALID_MAPPING,
    INVALID_EVIDENCE,
    REJECTED_MAPPING,
    REJECTED_EVIDENCE,
    IRRELEVANT_DOCUMENT,
    BOILERPLATE,
    MANUAL_REVIEW,
    MANUAL_REVIEW_REQUIRED,
    ARCHIVED_LEGACY,
}

STRICT_PRODUCTION_REVIEW_STATUSES = {
    STRICT_VALIDATED,
    MANUAL_REVIEW_REQUIRED,
    REJECTED_MAPPING,
    REJECTED_EVIDENCE,
    IRRELEVANT_DOCUMENT,
    BOILERPLATE,
    ARCHIVED_LEGACY,
}


@dataclass(frozen=True)
class ResearchViewAssessment:
    status: str
    full_text_verification_status: str
    reviewer_verdict: str
    reasons: list[str]
    mapping_rationale: str

    @property
    def passed(self) -> bool:
        return self.status == VERIFIED_PASS


def document_blacklist_reasons(article: Article) -> list[str]:
    title_url = f"{article.title} {article.url or ''} {article.canonical_url or ''}".casefold()
    text = article.content_text or ""
    text_sample = text[:2500].casefold()
    reasons = [f"blacklisted document marker: {term}" for term in sorted(BLACKLIST_TITLE_URL_TERMS) if term in title_url]
    reasons.extend(f"boilerplate text marker: {term}" for term in sorted(BOILERPLATE_TEXT_TERMS) if term in text_sample)
    reasons.extend(f"boilerplate security banner: {term}" for term in sorted(SECURITY_BANNER_TERMS) if term in text_sample)
    if _is_predominantly_boilerplate(text):
        reasons.append("boilerplate document: content is predominantly navigation/security/service text")
    if _is_irrelevant_article(article):
        reasons.append("irrelevant document: article topic is outside MAE asset-allocation evidence")
    return reasons


def research_view_draft_errors(article: Article, draft: ResearchViewDraft) -> list[str]:
    payload = {
        "institution": draft.institution,
        "horizon": draft.horizon,
        "region": draft.region,
        "asset_class": draft.asset_class,
        "asset_group": draft.asset_group,
        "asset_segment": draft.asset_segment,
        "direction": draft.direction.value,
        "confidence": draft.confidence.value,
    }
    errors = _required_value_errors(payload)
    if draft.position_score is None:
        errors.append("missing investment implication score")
    if not draft.drivers:
        errors.append("missing positive factor / investment driver")
    if not draft.risks:
        errors.append("missing negative factor / risk")
    quote_payload = [quote.model_dump() for quote in draft.evidence_quotes]
    errors.extend(_evidence_quote_errors(article, quote_payload))
    if not article.is_demo:
        errors.extend(document_blacklist_reasons(article))
    return errors


def research_view_quality_errors(view: ResearchView, article: Article | None = None) -> list[str]:
    article = article or getattr(view, "article", None)
    errors = _required_value_errors(
        {
            "institution": view.institution,
            "horizon": view.horizon,
            "region": view.region,
            "asset_class": view.asset_class,
            "asset_group": view.asset_group,
            "asset_segment": view.asset_segment,
            "direction": view.direction,
            "confidence": view.confidence,
        }
    )
    if view.position_score is None:
        errors.append("missing investment implication score")
    if not view.drivers:
        errors.append("missing positive factor / investment driver")
    if not view.risks:
        errors.append("missing negative factor / risk")
    if article is not None:
        errors.extend(_evidence_quote_errors(article, view.evidence_quotes or []))
        if not article.is_demo:
            errors.extend(document_blacklist_reasons(article))
            assessment = assess_research_view(view, article)
            if not assessment.passed:
                errors.extend(assessment.reasons)
    elif not view.is_demo:
        errors.append("source article is missing")
    return errors


def is_qualified_research_view(view: ResearchView, article: Article | None = None) -> bool:
    return not research_view_quality_errors(view, article)


def assess_research_view(view: ResearchView, article: Article | None = None) -> ResearchViewAssessment:
    article = article or getattr(view, "article", None)
    if article is None:
        return ResearchViewAssessment(
            INVALID_EVIDENCE,
            FULL_TEXT_MISSING,
            "FAIL",
            ["source article is missing"],
            "Mapping cannot be checked because the source article is missing.",
        )
    if article.is_demo or view.is_demo:
        return ResearchViewAssessment(
            VERIFIED_PASS,
            FULL_TEXT_VERIFIED,
            "PASS",
            [],
            "DEMO fixture is explicitly excluded from production research-quality gates.",
        )
    document_reasons = document_blacklist_reasons(article)
    if document_reasons:
        status = BOILERPLATE if any("boilerplate" in reason or "security banner" in reason for reason in document_reasons) else IRRELEVANT_DOCUMENT
        return ResearchViewAssessment(
            status,
            full_text_verification_status(article, view.evidence_quotes or []),
            "FAIL",
            document_reasons,
            "Publication failed the document-quality gate before asset mapping could be accepted.",
        )

    verification = full_text_verification_status(article, view.evidence_quotes or [])
    if verification != FULL_TEXT_VERIFIED:
        return ResearchViewAssessment(
            INVALID_EVIDENCE,
            verification,
            "FAIL",
            [f"full-text verification is {verification}"],
            "The quoted evidence must be an exact excerpt from the publication text.",
        )

    quote = best_verified_quote(article, view.evidence_quotes or [])
    quote_errors = _quote_content_errors(quote)
    if quote_errors:
        return ResearchViewAssessment(
            BOILERPLATE if any("boilerplate" in reason for reason in quote_errors) else INVALID_EVIDENCE,
            verification,
            "FAIL",
            quote_errors,
            "The quoted sentence is not usable investment evidence for MAE scoring.",
        )

    mapping_errors = semantic_mapping_errors(view, article, quote)
    if mapping_errors:
        return ResearchViewAssessment(
            INVALID_MAPPING,
            verification,
            "FAIL",
            mapping_errors,
            _mapping_failure_rationale(view, quote, mapping_errors),
        )

    rationale = semantic_mapping_rationale(view, quote)
    return ResearchViewAssessment(VERIFIED_PASS, verification, "PASS", [], rationale)


def full_text_verification_status(article: Article, quotes: list[dict[str, Any]]) -> str:
    if not quotes:
        return FULL_TEXT_MISSING
    found_any = False
    partial = False
    for item in quotes:
        quote = str(item.get("quote") or "").strip()
        if not quote:
            continue
        found_any = True
        if _quote_in_text(quote, article.content_text):
            return FULL_TEXT_VERIFIED
        normalized = _normalize_text(quote)
        normalized_text = _normalize_text(article.content_text)
        if normalized and len(normalized) >= 80 and (normalized[:80] in normalized_text or normalized[-80:] in normalized_text):
            partial = True
    if partial:
        return FULL_TEXT_PARTIAL
    return FULL_TEXT_NOT_VERIFIED if found_any else FULL_TEXT_MISSING


def best_verified_quote(article: Article, quotes: list[dict[str, Any]]) -> str:
    for item in quotes:
        quote = str(item.get("quote") or "").strip()
        if quote and _quote_in_text(quote, article.content_text):
            return quote
    return str((quotes[0] if quotes else {}).get("quote") or "").strip()


def semantic_mapping_rationale(view: ResearchView, quote: str) -> str:
    region = view.region
    row = view.template_row_key
    if view.asset_class == "FIXED INCOME":
        return (
            f"Quote discusses rates, yields, central-bank policy, credit or inflation in a way that supports {row} "
            f"for {region} over the MAE tactical horizon."
        )
    if view.asset_class == "EQUITY":
        return (
            f"Quote discusses equity-market earnings, valuations, growth or risk appetite, supporting {row} "
            f"for {region} rather than a generic macro tag."
        )
    if view.asset_class in {"GOLD", "COMMODITIES"}:
        return f"Quote links real yields, dollar/commodity dynamics or safe-haven demand to {row} for {region}."
    return f"Quote directly supports {row} for {region} with a cell-specific investment implication."


def semantic_mapping_errors(view: ResearchView, article: Article, quote: str) -> list[str]:
    text = _normalize_text(f"{article.title} {article.source_reference} {quote} {' '.join(view.drivers or [])} {' '.join(view.risks or [])}")
    article_text = _normalize_text(f"{article.title} {article.content_text[:8000]}")
    errors: list[str] = []
    if not _region_supported(view.region, text, article_text):
        errors.append(f"region unsupported: {view.region}")
    asset_error = _asset_mapping_error(view, text, article_text)
    if asset_error:
        errors.append(asset_error)
    direction_error = _direction_error(view, text)
    if direction_error:
        errors.append(direction_error)
    if _quote_is_too_generic_for_cell(view, text, article_text):
        errors.append("mapping unsupported: quote is generic or sector-specific and lacks cell-specific transmission")
    return errors


def append_article_error(article: Article, message: str) -> None:
    existing = (article.error_message or "").strip()
    if not existing:
        article.error_message = message[:2000]
        return
    if message in existing:
        return
    article.error_message = f"{existing}\n{message}"[:2000]


def _required_value_errors(payload: dict[str, Any]) -> list[str]:
    return [f"missing required field: {field}" for field, value in payload.items() if not str(value or "").strip()]


def _evidence_quote_errors(article: Article, quotes: list[dict[str, Any]]) -> list[str]:
    if article.is_demo:
        return [] if quotes else ["DEMO research view requires DEMO evidence quote"]
    if not quotes:
        return ["missing exact evidence quote"]
    exact_quote_found = False
    locator_found = False
    for item in quotes:
        quote = str(item.get("quote") or "").strip()
        locator = str(item.get("locator") or "").strip()
        locator_found = locator_found or bool(locator and locator.casefold() not in {"manual", "manual/demo"})
        if quote and _quote_in_text(quote, article.content_text):
            exact_quote_found = True
    errors: list[str] = []
    if not exact_quote_found:
        errors.append("evidence quote is not an exact excerpt from publication text")
    if not locator_found:
        errors.append("missing source locator for evidence quote")
    return errors


def _quote_content_errors(quote: str) -> list[str]:
    lowered = _normalize_text(quote)
    errors: list[str] = []
    if len(lowered) < 45:
        errors.append("invalid evidence: quote is too short for an investment conclusion")
    if any(term in lowered for term in NAVIGATION_QUOTE_TERMS | SECURITY_BANNER_TERMS):
        errors.append("boilerplate evidence: quote contains navigation, sharing, cookie or security-banner text")
    if any(term in lowered for term in GENERIC_MARKETING_QUOTE_TERMS):
        errors.append("invalid evidence: quote is generic service/marketing copy, not investment analysis")
    if not _has_investment_assertion(lowered):
        errors.append("invalid evidence: quote does not contain an investment assertion")
    return errors


def _has_investment_assertion(text: str) -> bool:
    positive_or_negative = {
        "overweight",
        "underweight",
        "constructive",
        "cautious",
        "bullish",
        "bearish",
        "outperform",
        "underperform",
        "support",
        "pressure",
        "rally",
        "retreat",
        "attractive",
        "unattractive",
        "improve",
        "improved",
        "deteriorate",
        "deteriorated",
        "resilient",
        "risk",
        "risks",
        "valuation",
        "valuations",
        "opportunity",
        "opportunities",
        "poised",
        "portfolio construction",
        "diversification",
        "diversify",
        "exposure",
        "earnings",
        "eps",
        "margin",
        "growth",
        "inflation",
        "rates",
        "rate",
        "yields",
        "yield",
        "spreads",
        "spread",
        "duration",
        "credit",
        "equities",
        "equity",
        "stocks",
        "bonds",
        "dollar",
        "gold",
        "commodities",
        "central bank",
        "policy",
        "cuts",
        "hikes",
        "easing",
        "tightening",
    }
    return any(term in text for term in positive_or_negative)


def _region_supported(region: str, quote_text: str, article_text: str) -> bool:
    text = f" {quote_text} {article_text[:3000]} "
    region = (region or "").strip()
    if region == "Global":
        if _has_any(text, {"world cup", "pet industry", "new york multifamily", "uk unlocked", "tokenisation as a catalyst"}):
            return False
        return _has_any(text, {"global", "worldwide", "world", "multi-asset", "developed-market", "developed market"}) or not _specific_region_only(text)
    if region == "US":
        if _has_any(text, {"emerging markets", " em ", "uk unlocked", "united kingdom", " u.k.", " bank of england", "euro area", "eurozone"}):
            return _has_any(text, {"united states", "u.s.", " us ", "usa", "federal reserve", "fomc", "s&p", "nasdaq"})
        return _has_any(text, {"united states", "u.s.", " us ", "usa", "america", "federal reserve", "fomc", "s&p", "nasdaq", "treasury", "treasuries"})
    if region == "Europe":
        return _has_any(text, {"europe", "european", "euro area", "eurozone", "ecb", "european central bank"})
    if region == "UK":
        return _has_any(text, {"united kingdom", " u.k.", " uk ", "britain", "british", "bank of england", "boe", "barclays", "invesco uk"})
    if region == "Japan":
        return _has_any(text, {"japan", "japanese", "boj", "bank of japan", "yen"})
    if region == "EM ex China":
        return _has_any(text, {"emerging markets", "emerging-market", " em ", "em ex china", "em ex-china"})
    return False


def _specific_region_only(text: str) -> bool:
    return _has_any(
        text,
        {
            "united kingdom",
            " u.k.",
            " uk ",
            "euro area",
            "eurozone",
            "bank of england",
            "emerging markets",
            "emerging-market",
            "japan",
            "boj",
        },
    )


def _asset_mapping_error(view: ResearchView, quote_text: str, article_text: str) -> str:
    row = _normalize_text(view.template_row_key)
    asset_text = f" {quote_text} {article_text[:3000]} "
    if view.asset_class == "EQUITY":
        if not _has_any(asset_text, {"equity", "equities", "stock", "stocks", "share", "shares", "earnings", "eps", "valuation", "market rally", "s&p", "nasdaq"}):
            return "asset unsupported: equity claim is not present in source evidence"
        if "growth" in row and not _has_any(asset_text, {"growth", "technology", "tech", "ai", "software", "semiconductor", "earnings growth", "eps growth"}):
            return "segment unsupported: Growth requires growth/technology/earnings-growth evidence"
        if "value" in row and not _has_any(asset_text, {"value", "valuation", "valuations", "cheap", "cyclical", "discount"}):
            return "segment unsupported: Value requires valuation/value evidence"
        return ""
    if view.asset_class == "FIXED INCOME":
        is_gov = "fixed income|gov" in row
        is_credit = "fixed income|corp" in row
        if is_gov and not _has_any(asset_text, {"rate", "rates", "policy", "central bank", "yield", "yields", "treasury", "treasuries", "government bond", "sovereign", "inflation", "duration"}):
            return "asset unsupported: government-bond claim is not present in source evidence"
        if is_credit and not _has_any(asset_text, {"credit", "spread", "spreads", "corporate bond", "investment grade", "high yield", "default", "refinancing"}):
            return "asset unsupported: corporate-credit claim is not present in source evidence"
        if "short term" in row and not _has_any(asset_text, {"short", "front-end", "front end", "2y", "two-year", "policy rate", "rates", "cuts", "hikes", "central bank"}):
            return "horizon unsupported: short-term fixed income requires policy/front-end evidence"
        if "long term" in row and not _has_any(asset_text, {"long", "duration", "10-year", "10y", "yield curve", "term premium", "treasury yields"}):
            return "horizon unsupported: long-term fixed income requires duration/long-end evidence"
        if "high yield" in quote_text and "corp hy" not in row:
            return "segment unsupported: high-yield evidence cannot map to non-HY credit"
        if "investment grade" in quote_text and "high yield" not in quote_text and "corp ig" not in row and is_credit:
            return "segment unsupported: investment-grade evidence cannot map to HY credit"
        return ""
    if view.asset_class in {"GOLD", "COMMODITIES"} or "commodities" in row or "gold" in row:
        if not _has_any(asset_text, {"gold", "commodity", "commodities", "oil", "metal", "metals", "real yield", "dollar", "safe haven"}):
            return "asset unsupported: commodity/gold claim is not present in source evidence"
        return ""
    return "asset unsupported: unknown MAE asset class"


def _direction_error(view: ResearchView, quote_text: str) -> str:
    direction = str(view.direction or "")
    score = view.position_score or 0
    positive_terms = {
        "overweight",
        "constructive",
        "bullish",
        "outperform",
        "support",
        "supports",
        "rally",
        "attractive",
        "improved",
        "resilient",
        "reward",
        "opportunity",
        "opportunities",
        "lower rates",
        "rate cuts",
        "cuts",
        "easing",
        "disinflation",
        "spread tightening",
    }
    negative_terms = {
        "underweight",
        "cautious",
        "bearish",
        "underperform",
        "pressure",
        "pressures",
        "retreat",
        "unattractive",
        "deteriorated",
        "risk",
        "risks",
        "higher rates",
        "hikes",
        "tightening",
        "sticky inflation",
        "spread widening",
    }
    positive = _term_hits(quote_text, positive_terms)
    negative = _term_hits(quote_text, negative_terms)
    if direction == "BULLISH" and score > 0 and positive == 0 and negative > 0:
        return "direction unsupported: bullish score conflicts with negative quote language"
    if direction == "BEARISH" and score < 0 and negative == 0 and positive > 0:
        return "direction unsupported: bearish score conflicts with positive quote language"
    return ""


def _quote_is_too_generic_for_cell(view: ResearchView, quote_text: str, article_text: str) -> bool:
    text = f" {quote_text} {article_text[:1000]} "
    if _has_any(text, {"world cup", "pet owners", "pet industry", "payments outlook", "multifamily", "tokenisation"}):
        return True
    if any(term in quote_text for term in GENERIC_MARKETING_QUOTE_TERMS):
        return True
    row = _normalize_text(view.template_row_key)
    if "emerging market" in text and view.region == "US":
        return True
    if _has_any(text, {"bank of england", "ecb", "european central bank", "euro area", "eurozone"}) and view.region == "US":
        return True
    if "uk unlocked" in text and view.region == "US":
        return True
    if "emerging market" in text and "fixed income|gov" in row and view.region == "US":
        return True
    return False


def _mapping_failure_rationale(view: ResearchView, quote: str, errors: list[str]) -> str:
    return (
        f"Rejected mapping to {view.template_row_key} / {view.region}: "
        f"{'; '.join(errors[:4])}. Quote: {quote[:180]}"
    )


def _is_predominantly_boilerplate(text: str) -> bool:
    normalized = _normalize_text(text)
    if not normalized:
        return True
    words = normalized.split()
    if len(words) < 80 and any(term in normalized for term in SECURITY_BANNER_TERMS):
        return True
    marker_hits = sum(normalized.count(term) for term in NAVIGATION_QUOTE_TERMS | SECURITY_BANNER_TERMS | GENERIC_MARKETING_QUOTE_TERMS)
    investment_hits = _term_hits(
        normalized,
        {
            "equity",
            "equities",
            "stocks",
            "bonds",
            "rates",
            "yields",
            "inflation",
            "earnings",
            "valuation",
            "credit",
            "spreads",
            "outlook",
            "market",
        },
    )
    return marker_hits >= 4 and investment_hits <= 2


def _is_irrelevant_article(article: Article) -> bool:
    title_url = _normalize_text(f"{article.title} {article.url or ''} {article.source_reference}")
    return any(term in title_url for term in ARTICLE_IRRELEVANT_TERMS)


def _has_any(text: str, terms: set[str]) -> bool:
    padded = f" {text} "
    return any(term in padded for term in terms)


def _term_hits(text: str, terms: set[str]) -> int:
    return sum(text.count(term) for term in terms)


def _quote_in_text(quote: str, text: str) -> bool:
    normalized_quote = _normalize_text(quote)
    normalized_text = _normalize_text(text)
    if not normalized_quote:
        return False
    if normalized_quote in normalized_text:
        return True
    if len(normalized_quote) > 180 and normalized_quote[:180] in normalized_text:
        return True
    return False


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().casefold()
