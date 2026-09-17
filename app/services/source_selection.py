from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.models import Article, Source
from app.services.normalization import canonicalize_url, normalize_space


SELECTION_ALGORITHM_VERSION = "SOURCE_PACK_SELECTOR_V1"
REQUIRED_REGIONS = ("Global", "US", "Europe", "UK", "Japan", "EM ex China")
REQUIRED_ASSET_CLASSES = ("Equities", "Government Bonds", "Credit", "Commodities")
MIN_SELECTED_DOCUMENTS = 10
PREFERRED_SELECTED_DOCUMENTS = 14
MAX_SELECTED_DOCUMENTS = 20
MIN_INDEPENDENT_PROVIDERS = 6

FORBIDDEN_TOPIC_PATTERNS = {
    "PET_INDUSTRY": re.compile(r"\b(pet|pets|veterinary|animal health)\b", re.I),
    "SPORTS_WORLD_CUP": re.compile(r"\b(world cup|fifa|sports?|football tournament)\b", re.I),
    "PAYMENTS": re.compile(r"\b(payment|payments|cards?|merchant acquiring|payment processing)\b", re.I),
    "UNRELATED_TOKENISATION": re.compile(r"\b(tokenisation|tokenization|tokenised|tokenized|digital assets?)\b", re.I),
    "REAL_ESTATE_PROPERTY": re.compile(r"\b(multifamily|commercial real estate|property sector|property market|reit)\b", re.I),
    "PODCAST": re.compile(r"\b(podcast|webcast|video transcript)\b", re.I),
    "GENERIC_HOMEPAGE": re.compile(r"\b(generic homepage|homepage|home page|latest insights landing page)\b", re.I),
    "ADMINISTRATIVE_OFFICIAL_NOTICE": re.compile(
        r"\b(fees regime|enforcement action|leadership and objectives|central bank communications that reach the public|"
        r"supervisory fees|privacy|cookie notice|cookie policy|privacy policy)\b",
        re.I,
    ),
}

ANALYTICAL_KEYWORDS = re.compile(
    r"\b(outlook|forecast|market|markets|macro|economic|economy|inflation|rates?|yields?|equity|equities|"
    r"earnings|bond|bonds|duration|credit|spreads?|commodit(?:y|ies)|gold|asset allocation|portfolio|"
    r"investment|strategy|risk|scenario|growth|recession|policy)\b",
    re.I,
)
HORIZON_KEYWORDS = re.compile(
    r"\b(6[-–]12|six[- ]to[- ]twelve|next (?:six|6|twelve|12)|12 months?|2026|2027|medium term|outlook|forecast)\b",
    re.I,
)
AUTH_FAILURE_KEYWORDS = re.compile(
    r"(access denied|sign in to continue|subscribe to continue|subscription required|enable javascript|captcha|"
    r"not found|page not found|error 404|forbidden|unauthorized|temporarily unavailable)",
    re.I,
)
MARKET_OUTLOOK_KEYWORDS = re.compile(
    r"\b(market outlook|mid[- ]year outlook|investment outlook|macro outlook|economic outlook|asset allocation|"
    r"multi[- ]asset|fixed income outlook|equity outlook|commodit(?:y|ies) tracker|energy playbook|"
    r"financial stability report|market strategy|investment strategy)\b",
    re.I,
)
ANALYTICAL_TITLE_KEYWORDS = re.compile(
    r"\b(financial stability report|mid[- ]year outlook|outlooks? and forecasts|commodit(?:y|ies) tracker|"
    r"energy playbook|market outlook|investment outlook|economic and market outlook|global economics quarterly|"
    r"credit conditions survey|bank liabilities survey|fixed income outlook|equity market outlook)\b",
    re.I,
)
BOILERPLATE_LINE_PATTERNS = (
    re.compile(r"\b(cookie|privacy|terms of use|all rights reserved|subscribe|sign in|login|newsletter)\b", re.I),
    re.compile(r"\b(related articles|recommended|share this|follow us|listen to our podcast|site map)\b", re.I),
    re.compile(r"\b(home|about us|careers|contact|skip to main content|back to menu|key links|view all)\b", re.I),
)
NAVIGATION_ONLY_PATTERNS = (
    re.compile(r"^(for companies and institutions|commercial banking|global corporate banking|investment banking|payments)$", re.I),
    re.compile(r"^(for institutional investors|asset management|markets|industries|insights|client stories|videos)$", re.I),
    re.compile(r"^(united states|news|contact|our etfs|investor center|model portfolios|how to invest|about us|search)$", re.I),
)


class SourceSelectionError(RuntimeError):
    def __init__(self, message: str, manifest: dict[str, Any]) -> None:
        super().__init__(message)
        self.manifest = manifest


@dataclass
class DocumentEvaluation:
    article_id: str
    raw_provider_name: str
    canonical_provider_name: str
    title: str
    date: str
    url: str
    document_type: str
    content_hash: str
    content_chars: int
    coverage_regions: list[str]
    coverage_asset_classes: list[str]
    source_tier: str
    score_components: dict[str, int]
    total_score: int
    eligibility_status: str
    rejection_reasons: list[str] = field(default_factory=list)
    selection_reason: str = ""
    quality_flags: list[str] = field(default_factory=list)
    article: Article | None = field(default=None, repr=False)
    source: Source | None = field(default=None, repr=False)

    def manifest_row(self) -> dict[str, Any]:
        return {
            "source_id": self.article_id,
            "raw_provider_name": self.raw_provider_name,
            "canonical_provider_name": self.canonical_provider_name,
            "title": self.title,
            "date": self.date,
            "url": self.url,
            "document_type": self.document_type,
            "content_hash": self.content_hash,
            "content_chars": self.content_chars,
            "coverage_regions": self.coverage_regions,
            "coverage_asset_classes": self.coverage_asset_classes,
            "source_tier": self.source_tier,
            "eligibility_status": self.eligibility_status,
            "total_score": self.total_score,
            "score_components": self.score_components,
            "rejection_reasons": self.rejection_reasons,
            "selection_reason": self.selection_reason,
            "why_used_user": user_facing_selection_reason(self),
            "quality_flags": self.quality_flags,
        }

    def source_pack_item(self) -> dict[str, Any]:
        text = self.article.content_text if self.article is not None else ""
        return {
            "source_id": self.article_id,
            "provider": self.canonical_provider_name,
            "raw_provider_name": self.raw_provider_name,
            "canonical_provider_name": self.canonical_provider_name,
            "title": self.title,
            "date": self.date,
            "url": self.url,
            "document_type": self.document_type,
            "selection_reason": self.selection_reason,
            "coverage_regions": self.coverage_regions,
            "coverage_asset_classes": self.coverage_asset_classes,
            "source_tier": self.source_tier,
            "why_used_user": user_facing_selection_reason(self),
            "selection_score": self.total_score,
            "score_components": self.score_components,
            "quality_flags": self.quality_flags,
            "content_hash": self.content_hash,
            "content_chars": self.content_chars,
            "content_excerpt": content_excerpt(text),
        }


def select_autonomous_source_pack(
    session: Session,
    *,
    snapshot_date: date,
    lookback_days: int = 90,
    previous_source_hashes: set[str] | None = None,
    require_new_source: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    evaluations = evaluate_candidate_documents(session, snapshot_date=snapshot_date, lookback_days=lookback_days)
    eligible = _dedupe_evaluations(evaluations)
    selected = _select_coverage_constrained_pack(eligible)
    previous_source_hashes = previous_source_hashes or set()
    selected_hashes = {item.content_hash for item in selected if item.content_hash}
    blockers = _selection_blockers(selected)
    if require_new_source and previous_source_hashes and selected_hashes and selected_hashes.issubset(previous_source_hashes):
        blockers.append("NO_NEW_OR_REFRESHED_ELIGIBLE_DOCUMENTS")
    for item in selected:
        item.eligibility_status = "SELECTED"
        item.selection_reason = _selection_reason(item, selected)
    manifest = build_source_selection_manifest(
        snapshot_date=snapshot_date,
        evaluations=evaluations,
        selected=selected,
        blockers=blockers,
    )
    if blockers:
        raise SourceSelectionError("source selection blocked: " + ", ".join(blockers), manifest)
    return [item.source_pack_item() for item in selected], manifest


def evaluate_candidate_documents(
    session: Session,
    *,
    snapshot_date: date,
    lookback_days: int = 90,
) -> list[DocumentEvaluation]:
    cutoff = snapshot_date - timedelta(days=lookback_days)
    rows = session.execute(
        select(Article, Source)
        .join(Source, Article.source_id == Source.id)
        .where(Article.is_demo.is_(False), Source.is_demo.is_(False), Source.active.is_(True))
        .order_by(Article.publication_date.desc(), Article.id)
    ).all()
    return [_evaluate_article(article, source, snapshot_date=snapshot_date, cutoff=cutoff) for article, source in rows]


def build_source_selection_manifest(
    *,
    snapshot_date: date,
    evaluations: list[DocumentEvaluation],
    selected: list[DocumentEvaluation],
    blockers: list[str] | None = None,
) -> dict[str, Any]:
    selected_rows = [item.manifest_row() for item in selected]
    rejected = [item for item in evaluations if item.eligibility_status == "REJECTED"]
    not_selected = [item for item in evaluations if item.eligibility_status == "ELIGIBLE"]
    for item in not_selected:
        if not item.selection_reason:
            item.selection_reason = "Eligible, but not selected after coverage, diversity and quality optimisation."
    selected_providers = [item.canonical_provider_name for item in selected]
    region_coverage = {region: _coverage_count(selected, region, "region") for region in REQUIRED_REGIONS}
    asset_coverage = {asset: _coverage_count(selected, asset, "asset") for asset in REQUIRED_ASSET_CLASSES}
    source_tiers = Counter(item.source_tier for item in selected)
    pack_hash = canonical_hash(
        [
            {
                "source_id": item.article_id,
                "content_hash": item.content_hash,
                "score": item.total_score,
                "provider": item.canonical_provider_name,
            }
            for item in selected
        ]
    )
    return {
        "schema_version": "SOURCE_SELECTION_MANIFEST_V1",
        "selection_algorithm_version": SELECTION_ALGORITHM_VERSION,
        "snapshot_date": snapshot_date.isoformat(),
        "candidate_document_count": len(evaluations),
        "eligible_count": sum(item.eligibility_status in {"ELIGIBLE", "SELECTED"} for item in evaluations),
        "rejected_count": len(rejected),
        "not_selected_count": len(not_selected),
        "selected_count": len(selected),
        "independent_provider_count": len(set(selected_providers)),
        "candidate_documents": [item.manifest_row() for item in evaluations],
        "selected_documents": selected_rows,
        "eligible_not_selected_documents": [item.manifest_row() for item in not_selected],
        "rejected_documents": [item.manifest_row() for item in rejected],
        "rejection_reason_codes": dict(Counter(reason for item in rejected for reason in item.rejection_reasons)),
        "provider_concentration": dict(Counter(selected_providers)),
        "region_coverage": region_coverage,
        "asset_class_coverage": asset_coverage,
        "source_tier_distribution": dict(source_tiers),
        "source_pack_hash": pack_hash,
        "blockers": sorted(set(blockers or [])),
    }


def canonical_provider_name(raw_name: str, url: str = "") -> str:
    raw = normalize_space(raw_name)
    host = urlparse(url or "").netloc.lower().removeprefix("www.")
    key = raw.casefold().replace("&", "and")
    aliases = {
        "gbm research": "HSBC Research",
        "hsbc research": "HSBC Research",
        "deutschewealth research": "Deutsche Bank Wealth Management",
        "globalxetfs research": "Global X",
        "ecb publications": "European Central Bank",
        "bank of england publications": "Bank of England",
        "morgan stanley ideas": "Morgan Stanley",
        "j.p. morgan insights": "J.P. Morgan",
        "jp morgan insights": "J.P. Morgan",
        "federal reserve publications": "Federal Reserve",
        "bank of japan outlook": "Bank of Japan",
        "imf publications": "International Monetary Fund",
        "oecd reports": "OECD",
        "blackrock investment institute": "BlackRock",
        "amundi research center": "Amundi",
        "goldman sachs insights": "Goldman Sachs",
        "vanguard research": "Vanguard",
        "state street global advisors": "State Street Global Advisors",
        "moodys insights": "Moody's",
        "moody's insights": "Moody's",
        "s&p global research": "S&P Global",
        "fitch ratings economics": "Fitch Ratings",
        "msci research": "MSCI",
        "invesco research": "Invesco",
        "ishares research": "iShares",
    }
    if "business.hsbc.com" in host:
        return "HSBC Research"
    if "deutschewealth" in key or "db.com" in host or "deutschewealth.com" in host:
        return "Deutsche Bank Wealth Management"
    if "globalxetfs" in key or "globalxetfs.com" in host:
        return "Global X"
    return aliases.get(key, raw)


def source_tier(canonical_provider: str, raw_provider: str = "", url: str = "") -> str:
    text = f"{canonical_provider} {raw_provider} {url}".casefold()
    if any(
        token in text
        for token in [
            "central bank",
            "federal reserve",
            "european central bank",
            "bank of england",
            "bank of japan",
            "international monetary fund",
            "oecd",
            "moody",
            "s&p global",
            "fitch",
            "msci",
        ]
    ):
        return "TIER_1"
    if any(
        token in text
        for token in [
            "blackrock",
            "j.p. morgan",
            "goldman",
            "morgan stanley",
            "vanguard",
            "amundi",
            "hsbc",
            "deutsche bank",
            "global x",
            "state street",
            "invesco",
            "ishares",
        ]
    ):
        return "TIER_2"
    if any(token in text for token in ["reuters", "financial times", "bloomberg"]):
        return "TIER_3"
    return "TIER_2"


def coverage_regions(title: str, text: str, provider: str = "") -> list[str]:
    combined = f" {provider} {title} {text[:8000]} ".casefold()
    checks = [
        ("Global", (" global ", " world ", " multi-asset", "cross-asset", "world economy", "global economy")),
        ("US", (" us ", " u.s.", " united states", " fed ", "fomc", "treasury", "s&p 500")),
        ("Europe", (" europe", " euro area", " eurozone", " ecb ", " european central bank")),
        ("UK", (" uk ", " united kingdom", " bank of england", "boe ", "gilt")),
        ("Japan", (" japan", " japanese", " boj ", "bank of japan", "yen")),
        ("EM ex China", ("em ex china", "emerging market", "emerging markets", " em ", "india", "latin america", "brazil")),
    ]
    regions = [region for region, tokens in checks if any(token in combined for token in tokens)]
    return list(dict.fromkeys(regions or ["Global"]))


def coverage_assets(title: str, text: str) -> list[str]:
    combined = f" {title} {text[:8000]} ".casefold()
    checks = [
        ("Equities", (" equity", " equities", " stock", " stocks", "earnings", "market breadth", "s&p 500")),
        ("Government Bonds", ("government bond", "sovereign", "treasury", "gilt", "jgb", "duration", "rates", "yield", "yields")),
        ("Credit", (" credit", "spreads", "corporate bond", "investment grade", "high yield", "leveraged loan")),
        ("Commodities", (" commodity", " commodities", " gold", "oil", "energy", "metals")),
    ]
    assets = [asset for asset, tokens in checks if any(token in combined for token in tokens)]
    return list(dict.fromkeys(assets or ["Multi-asset"]))


def content_excerpt(text: str, *, max_chars: int = 1200) -> str:
    cleaned = normalize_space(text)
    if len(cleaned) <= max_chars:
        return cleaned
    head = cleaned[: max_chars // 2]
    tail = cleaned[-max_chars // 3 :]
    return f"{head} ... {tail}"


def clean_boilerplate_text(text: str, title: str = "") -> str:
    """Remove common page furniture before document-topic classification."""

    raw_lines = [normalize_space(raw_line) for raw_line in str(text or "").splitlines()]
    raw_lines = [line for line in raw_lines if line]
    start_index = _analytical_start_index(raw_lines, title)
    kept: list[str] = []
    previous = ""
    for line in raw_lines[start_index:]:
        if line == previous:
            continue
        previous = line
        if any(pattern.search(line) for pattern in NAVIGATION_ONLY_PATTERNS):
            continue
        if len(line) < 18 and any(pattern.search(line) for pattern in BOILERPLATE_LINE_PATTERNS):
            continue
        if any(pattern.search(line) for pattern in BOILERPLATE_LINE_PATTERNS) and not ANALYTICAL_KEYWORDS.search(line):
            continue
        kept.append(line)
    cleaned = "\n".join(kept)
    return cleaned if cleaned.strip() else normalize_space(text)


def primary_forbidden_topics(title: str, text: str, *, url: str = "") -> list[str]:
    """Return hard-rejection codes only when forbidden topics are the document's main subject."""

    title_text = normalize_space(title)
    first_block = normalize_space(_first_analytical_block(text))
    title_and_url = f"{title_text} {url}".casefold()
    title_market_outlook = bool(MARKET_OUTLOOK_KEYWORDS.search(title_text))
    market_outlook = bool(MARKET_OUTLOOK_KEYWORDS.search(f"{title_text}\n{first_block}"))
    broad_analytical_document = bool(ANALYTICAL_TITLE_KEYWORDS.search(f"{title_text}\n{first_block}"))
    keyword_depth = len(ANALYTICAL_KEYWORDS.findall(f"{title_text}\n{text[:6500]}"))
    analytical_depth = (len(normalize_space(text)) >= 1800 and keyword_depth >= 6) or (
        broad_analytical_document and keyword_depth >= 4
    )
    codes: list[str] = []

    if re.search(r"\b(cookie|privacy|terms of use)\b", title_and_url):
        codes.append("ADMINISTRATIVE_OFFICIAL_NOTICE")
    if re.search(r"\b(homepage|home page|latest insights landing page)\b", title_and_url):
        codes.append("GENERIC_HOMEPAGE")

    for code, pattern in FORBIDDEN_TOPIC_PATTERNS.items():
        title_hits = len(pattern.findall(title_text))
        lead_hits = len(pattern.findall(first_block))
        full_hits = len(pattern.findall(text[:8000]))
        primary_topic = _forbidden_topic_is_primary(
            code,
            pattern,
            title_text=title_text,
            first_block=first_block,
            full_text=text,
            analytical_depth=analytical_depth,
        )
        incidental_allowed = (
            code
            in {
                "PAYMENTS",
                "UNRELATED_TOKENISATION",
                "REAL_ESTATE_PROPERTY",
                "PODCAST",
                "ADMINISTRATIVE_OFFICIAL_NOTICE",
            }
            and (market_outlook or broad_analytical_document)
            and analytical_depth
        )
        if code == "PODCAST":
            if title_hits and not analytical_depth:
                codes.append(code)
                continue
            if title_hits == 0 and market_outlook:
                continue
        if code == "ADMINISTRATIVE_OFFICIAL_NOTICE" and incidental_allowed:
            continue
        if code == "PAYMENTS" and analytical_depth and not primary_topic:
            continue
        if title_hits:
            if title_market_outlook and analytical_depth and code in {
                "PAYMENTS",
                "UNRELATED_TOKENISATION",
                "REAL_ESTATE_PROPERTY",
                "PODCAST",
                "ADMINISTRATIVE_OFFICIAL_NOTICE",
            }:
                continue
            codes.append(code)
            continue
        if incidental_allowed:
            continue
        if market_outlook and analytical_depth and lead_hits <= 1 and full_hits <= 4:
            continue
        if lead_hits >= 2 or full_hits >= 6:
            codes.append(code)
    return list(dict.fromkeys(codes))


def _forbidden_topic_is_primary(
    code: str,
    pattern: re.Pattern[str],
    *,
    title_text: str,
    first_block: str,
    full_text: str,
    analytical_depth: bool,
) -> bool:
    title_hits = len(pattern.findall(title_text))
    lead_hits = len(pattern.findall(first_block))
    if title_hits:
        return True
    if not analytical_depth:
        return lead_hits >= 2
    if code == "PAYMENTS":
        lead = normalize_space(first_block).casefold()
        payment_heading = re.search(
            r"(^|\n)\s*(payments?|digital euro|cards?|merchant acquiring|payment processing)\b",
            first_block,
            flags=re.I,
        )
        market_terms = len(ANALYTICAL_KEYWORDS.findall(first_block))
        payment_terms = len(pattern.findall(first_block))
        if payment_heading:
            return True
        if payment_terms >= 3 and payment_terms >= max(2, market_terms):
            return True
        if "payment" in lead and not re.search(r"\b(outlook|inflation|rates?|policy|growth|credit|bond|equity|market)\b", lead, re.I):
            return True
        return False
    return lead_hits >= 2


def _analytical_start_index(lines: list[str], title: str) -> int:
    if not lines:
        return 0
    title_key = _title_key(title)
    if title_key:
        for index, line in enumerate(lines):
            if title_key in _title_key(line):
                return index
    for index, line in enumerate(lines):
        if ANALYTICAL_TITLE_KEYWORDS.search(line):
            return index
    for index, line in enumerate(lines):
        if ANALYTICAL_KEYWORDS.search(line) and not any(pattern.search(line) for pattern in NAVIGATION_ONLY_PATTERNS):
            return index
    return 0


def _title_key(value: str) -> str:
    base = re.split(r"\s[|–-]\s", normalize_space(value), maxsplit=1)[0]
    return re.sub(r"[^a-z0-9]+", " ", base.casefold()).strip()


def _first_analytical_block(text: str, *, max_chars: int = 2600) -> str:
    lines = []
    for raw_line in str(text or "").splitlines():
        line = normalize_space(raw_line)
        if not line:
            continue
        if any(pattern.search(line) for pattern in NAVIGATION_ONLY_PATTERNS):
            continue
        if len(line) < 18 and any(pattern.search(line) for pattern in BOILERPLATE_LINE_PATTERNS):
            continue
        lines.append(line)
        if sum(len(item) + 1 for item in lines) >= max_chars:
            break
    return "\n".join(lines)


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _evaluate_article(article: Article, source: Source, *, snapshot_date: date, cutoff: date) -> DocumentEvaluation:
    raw_provider = normalize_space(source.institution_name)
    url = canonicalize_url(article.canonical_url or article.url or article.source_reference) or ""
    canonical_provider = canonical_provider_name(raw_provider, url)
    text = article.content_text or ""
    title = normalize_space(article.title)
    analytical_text = clean_boilerplate_text(text, title=title)
    regions = coverage_regions(title, analytical_text, canonical_provider)
    assets = coverage_assets(title, analytical_text)
    tier = source_tier(canonical_provider, raw_provider, url)
    reasons: list[str] = []
    flags: list[str] = []
    if article.publication_date > snapshot_date:
        reasons.append("FUTURE_DATED")
    if article.publication_date < cutoff:
        reasons.append("STALE")
    if not raw_provider or not canonical_provider:
        reasons.append("INVALID_PROVIDER")
    if not url or urlparse(url).scheme not in {"http", "https"}:
        reasons.append("INVALID_URL")
    if not article.content_hash or len(str(article.content_hash)) < 16:
        reasons.append("MISSING_CONTENT_HASH")
    if len(normalize_space(analytical_text)) < 700:
        reasons.append("TOO_LITTLE_ANALYTICAL_CONTENT")
    if _is_generic_homepage(url, title, analytical_text, source.website):
        reasons.append("GENERIC_OR_NAVIGATION_PAGE")
    if AUTH_FAILURE_KEYWORDS.search(f"{title}\n{analytical_text[:1500]}"):
        reasons.append("AUTH_OR_ERROR_PAGE")
    reasons.extend(primary_forbidden_topics(title, analytical_text, url=url))
    keyword_hits = len(ANALYTICAL_KEYWORDS.findall(f"{title}\n{analytical_text[:6000]}"))
    if keyword_hits < 4:
        reasons.append("LOW_MARKET_RELEVANCE")
    if len(analytical_text) < 1500:
        flags.append("LIMITED_DETAIL")
    if assets == ["Multi-asset"]:
        flags.append("BROAD_ASSET_PROXY")
    if regions == ["Global"] and not re.search(r"\b(global|world|multi-asset|cross-asset|outlook)\b", f"{title} {analytical_text[:1200]}", re.I):
        flags.append("BROAD_REGION_PROXY")
    components = _score_components(
        article=article,
        source=source,
        title=title,
        text=analytical_text,
        tier=tier,
        regions=regions,
        assets=assets,
        keyword_hits=keyword_hits,
        flags=flags,
        snapshot_date=snapshot_date,
    )
    total = sum(components.values())
    status = "REJECTED" if reasons else "ELIGIBLE"
    return DocumentEvaluation(
        article_id=article.id,
        raw_provider_name=raw_provider,
        canonical_provider_name=canonical_provider,
        title=title,
        date=article.publication_date.isoformat(),
        url=url,
        document_type=_document_type(title, canonical_provider),
        content_hash=str(article.content_hash or ""),
        content_chars=len(analytical_text),
        coverage_regions=regions,
        coverage_asset_classes=assets,
        source_tier=tier,
        score_components=components,
        total_score=total,
        eligibility_status=status,
        rejection_reasons=sorted(set(reasons)),
        quality_flags=sorted(set(flags)),
        article=article,
        source=source,
    )


def _score_components(
    *,
    article: Article,
    source: Source,
    title: str,
    text: str,
    tier: str,
    regions: list[str],
    assets: list[str],
    keyword_hits: int,
    flags: list[str],
    snapshot_date: date,
) -> dict[str, int]:
    age = max(0, (snapshot_date - article.publication_date).days)
    tier_score = {"TIER_1": 30, "TIER_2": 22, "TIER_3": 12}.get(tier, 12)
    authority = {"TIER_1": 15, "TIER_2": 10, "TIER_3": 4}.get(tier, 4)
    content_len = len(text or "")
    completeness = 16 if content_len >= 8000 else 13 if content_len >= 3000 else 9 if content_len >= 1500 else 3
    recency = max(0, 20 - age // 4)
    asset_relevance = min(14, 5 * len([asset for asset in assets if asset != "Multi-asset"]))
    region_relevance = min(12, 3 * len(regions))
    horizon = 8 if HORIZON_KEYWORDS.search(f"{title}\n{text[:4000]}") else 2
    specificity = min(12, 3 + keyword_hits)
    weak_penalty = -8 if "LIMITED_DETAIL" in flags else 0
    broad_penalty = -4 if "BROAD_ASSET_PROXY" in flags or "BROAD_REGION_PROXY" in flags else 0
    trust_bonus = min(6, int(float(getattr(source, "trust_score", 0.0) or 0.0) * 6))
    return {
        "source_quality_tier": tier_score,
        "recency": recency,
        "content_completeness": completeness,
        "institutional_authority": authority,
        "asset_class_relevance": asset_relevance,
        "regional_relevance": region_relevance,
        "horizon_relevance": horizon,
        "document_specificity": specificity,
        "source_trust": trust_bonus,
        "weak_excerpt_penalty": weak_penalty,
        "broad_document_penalty": broad_penalty,
    }


def _dedupe_evaluations(evaluations: list[DocumentEvaluation]) -> list[DocumentEvaluation]:
    eligible = [item for item in evaluations if item.eligibility_status == "ELIGIBLE"]
    keep_url = _best_by_key(eligible, lambda item: item.url)
    keep_hash = _best_by_key(eligible, lambda item: item.content_hash)
    deduped: list[DocumentEvaluation] = []
    for item in eligible:
        if keep_url.get(item.url) is not item:
            item.eligibility_status = "REJECTED"
            item.rejection_reasons.append("DUPLICATE_URL")
            continue
        if item.content_hash and keep_hash.get(item.content_hash) is not item:
            item.eligibility_status = "REJECTED"
            item.rejection_reasons.append("DUPLICATE_CONTENT_HASH")
            continue
        deduped.append(item)
    return deduped


def _best_by_key(items: list[DocumentEvaluation], key_func) -> dict[str, DocumentEvaluation]:
    result: dict[str, DocumentEvaluation] = {}
    for item in sorted(items, key=lambda candidate: (candidate.total_score, candidate.date), reverse=True):
        key = key_func(item)
        if key and key not in result:
            result[key] = item
    return result


def _select_coverage_constrained_pack(eligible: list[DocumentEvaluation]) -> list[DocumentEvaluation]:
    ranked = sorted(eligible, key=lambda item: (item.total_score, item.date, item.title), reverse=True)
    selected: list[DocumentEvaluation] = []
    for region in REQUIRED_REGIONS:
        _add_best_covering(selected, ranked, region, kind="region")
    for asset in REQUIRED_ASSET_CLASSES:
        _add_best_covering(selected, ranked, asset, kind="asset")
    while len({item.canonical_provider_name for item in selected}) < MIN_INDEPENDENT_PROVIDERS:
        candidate = _best_candidate(
            ranked,
            selected,
            prefer_new_provider=True,
        )
        if candidate is None:
            break
        selected.append(candidate)
    target = min(PREFERRED_SELECTED_DOCUMENTS, len(ranked), MAX_SELECTED_DOCUMENTS)
    while len(selected) < target:
        candidate = _best_candidate(ranked, selected)
        if candidate is None:
            break
        selected.append(candidate)
    return selected[:MAX_SELECTED_DOCUMENTS]


def _add_best_covering(selected: list[DocumentEvaluation], ranked: list[DocumentEvaluation], value: str, *, kind: str) -> None:
    if _coverage_count(selected, value, kind) > 0:
        return
    candidate = _best_candidate(ranked, selected, coverage_value=value, coverage_kind=kind)
    if candidate is not None:
        selected.append(candidate)


def _best_candidate(
    ranked: list[DocumentEvaluation],
    selected: list[DocumentEvaluation],
    *,
    coverage_value: str | None = None,
    coverage_kind: str = "region",
    prefer_new_provider: bool = False,
) -> DocumentEvaluation | None:
    selected_ids = {item.article_id for item in selected}
    provider_counts = Counter(item.canonical_provider_name for item in selected)
    selected_providers = set(provider_counts)
    for item in ranked:
        if item.article_id in selected_ids:
            continue
        if coverage_value and _item_coverage_count(item, coverage_value, coverage_kind) == 0:
            continue
        if prefer_new_provider and item.canonical_provider_name in selected_providers:
            continue
        provider_limit = 3 if item.source_tier == "TIER_1" and (coverage_value is not None or len(selected) < MIN_SELECTED_DOCUMENTS) else 2
        if provider_counts[item.canonical_provider_name] >= provider_limit:
            continue
        return item
    return None


def _selection_blockers(selected: list[DocumentEvaluation]) -> list[str]:
    blockers: list[str] = []
    if not selected:
        blockers.append("ELIGIBLE_DOCUMENT_COUNT_ZERO")
    if len(selected) < MIN_SELECTED_DOCUMENTS:
        blockers.append("SELECTED_SOURCE_COUNT_BELOW_10")
    if len({item.canonical_provider_name for item in selected}) < MIN_INDEPENDENT_PROVIDERS:
        blockers.append("INDEPENDENT_PROVIDER_COUNT_BELOW_6")
    missing_regions = [region for region in REQUIRED_REGIONS if _coverage_count(selected, region, "region") == 0]
    missing_assets = [asset for asset in REQUIRED_ASSET_CLASSES if _coverage_count(selected, asset, "asset") == 0]
    if missing_regions:
        blockers.append("REGION_COVERAGE_MISSING:" + ",".join(missing_regions))
    if missing_assets:
        blockers.append("ASSET_COVERAGE_MISSING:" + ",".join(missing_assets))
    return blockers


def _selection_reason(item: DocumentEvaluation, selected: list[DocumentEvaluation]) -> str:
    return user_facing_selection_reason(item)


def user_facing_selection_reason(item: DocumentEvaluation) -> str:
    regions = _region_phrase(item.coverage_regions)
    assets = _asset_phrase(item.coverage_asset_classes)
    title = item.title.casefold()
    doc_type = item.document_type.casefold()
    if "financial stability" in title or "financial stability" in doc_type:
        return f"Источник по финансовой устойчивости, банковскому сектору и условиям Credit для {regions}."
    if "credit conditions" in title or "bank liabilities" in title:
        return f"Источник по банковскому фондированию и условиям Credit для {regions}."
    if "commodit" in title or "energy" in title:
        return f"Источник по Commodities, энергетике и инфляционным рискам для {regions}."
    if "japan" in title or "bank of japan" in item.canonical_provider_name.casefold() or "boj" in title:
        return "Источник по политике BoJ и состоянию рынка Japan Government Bonds."
    if "emerging market" in title or "em " in title or "em ex china" in " ".join(item.coverage_regions).casefold():
        return "Источник по EM Equities, потокам капитала и динамике доллара."
    if "outlook" in doc_type or "outlook" in title or "forecast" in title:
        return f"Глобальный Multi-asset обзор для оценки {assets or 'Equities, Government Bonds и Commodities'}."
    if assets and regions:
        return f"Аналитический обзор {assets} для {regions}."
    return "Институциональный материал, использованный как часть текущего рыночного обзора."


def _region_phrase(values: list[str]) -> str:
    labels = [str(value) for value in values[:3] if value]
    return _join_labels(labels) if labels else "Global"


def _asset_phrase(values: list[str]) -> str:
    labels = [str(value) for value in values if value]
    if not labels or labels == ["Multi-asset"]:
        return "Equities, Government Bonds и Commodities"
    return _join_labels(labels)


def _join_labels(labels: list[str]) -> str:
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return " и ".join(labels)
    return ", ".join(labels[:-1]) + " и " + labels[-1]


def _coverage_count(items: list[DocumentEvaluation], value: str, kind: str) -> int:
    return sum(_item_coverage_count(item, value, kind) for item in items)


def _item_coverage_count(item: DocumentEvaluation, value: str, kind: str) -> int:
    values = item.coverage_regions if kind == "region" else item.coverage_asset_classes
    return int(value in values)


def _is_generic_homepage(url: str, title: str, text: str, source_website: str) -> bool:
    canonical = canonicalize_url(url)
    website = canonicalize_url(source_website)
    path = urlparse(url).path.strip("/")
    lowered_title = title.casefold()
    if canonical and website and canonical == website:
        return True
    if not path:
        return True
    if any(token in lowered_title for token in ["homepage", "home", "research and insights", "latest insights"]) and len(text) < 1800:
        return True
    navigation_fragments = ["/about", "/contact", "/careers", "/products", "/funds", "/podcast", "/video"]
    return any(fragment in urlparse(url).path.casefold() for fragment in navigation_fragments)


def _document_type(title: str, provider: str) -> str:
    text = f"{provider} {title}".casefold()
    if any(token in text for token in ["central bank", "federal reserve", "european central bank", "bank of england", "bank of japan"]):
        return "official policy / financial stability publication"
    if "outlook" in text or "forecast" in text:
        return "market outlook"
    if "commodit" in text or "gold" in text:
        return "commodity outlook"
    if "credit" in text or "fixed income" in text:
        return "fixed income / credit research"
    return "institutional research"
