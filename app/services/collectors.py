from __future__ import annotations

import csv
import hashlib
import io
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.domain.enums import FetchStatus, UpdateStatus
from app.domain.models import Article, Source, UpdateJob, utcnow
from app.services.normalization import canonicalize_url, content_hash
from app.services.pipeline import PipelineService
from app.services.security import validate_public_http_url


OFFICIAL_ADAPTERS = [
    ("IMF Publications", "https://www.imf.org/en/home"),
    ("Federal Reserve Publications", "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("ECB Publications", "https://www.ecb.europa.eu/rss/press.html"),
    ("Bank of England Publications", "https://www.bankofengland.co.uk/news/publications"),
    ("Bank of Japan Outlook", "https://www.boj.or.jp/en/mopo/outlook"),
    ("OECD Reports", "https://www.oecd.org/en/publications/reports.html?orderBy=mostRelevant&page=0"),
]


@dataclass(frozen=True)
class PublicationCandidate:
    title: str
    url: str
    publication_date: date | None
    summary: str = ""


@dataclass(frozen=True)
class FetchedPublication:
    title: str
    text: str
    publication_date: date | None


PRIORITY_INGESTION_SOURCES = {
    "IMF Publications",
    "OECD Reports",
    "Federal Reserve Publications",
    "ECB Publications",
    "Bank of England Publications",
    "Bank of Japan Outlook",
    "BlackRock Investment Institute",
    "J.P. Morgan Insights",
    "Vanguard Research",
    "Amundi Research Center",
    "Morgan Stanley Ideas",
    "Goldman Sachs Insights",
    "MSCI Research",
    "S&P Global Research",
    "Moody's Insights",
    "Fitch Ratings Economics",
}


ADAPTER_CONFIGS = {
    "Federal Reserve Publications": {"kind": "rss", "url": "https://www.federalreserve.gov/feeds/press_all.xml"},
    "ECB Publications": {"kind": "rss", "url": "https://www.ecb.europa.eu/rss/press.html"},
    "IMF Publications": {"kind": "imf_latest", "url": "https://www.imf.org/en/home"},
    "Bank of England Publications": {"kind": "boe_news_api", "url": "https://www.bankofengland.co.uk/news/publications"},
    "BlackRock Investment Institute": {"kind": "generic_page", "url": "https://www.blackrock.com/us/individual/insights"},
    "J.P. Morgan Insights": {"kind": "generic_page", "url": "https://www.jpmorgan.com/insights"},
    "Vanguard Research": {"kind": "generic_page", "url": "https://corporate.vanguard.com/content/corporatesite/us/en/corp/articles.html"},
    "Amundi Research Center": {"kind": "generic_page", "url": "https://research-center.amundi.com/"},
    "Morgan Stanley Ideas": {"kind": "generic_page", "url": "https://www.morganstanley.com/insights?filter=market-trends"},
    "Goldman Sachs Insights": {"kind": "generic_page", "url": "https://www.goldmansachs.com/insights"},
    "MSCI Research": {"kind": "generic_page", "url": "https://www.msci.com/research-and-insights"},
    "S&P Global Research": {"kind": "generic_page", "url": "https://www.spglobal.com/ratings/en/research/economic-research"},
    "Moody's Insights": {"kind": "generic_page", "url": "https://www.moodys.com/web/en/us/insights.html"},
    "Fitch Ratings Economics": {"kind": "generic_page", "url": "https://www.fitchratings.com/topics/economics"},
}


def load_outlook_sources(path=None) -> list[str]:
    path = path or get_settings().outlook_sources_path
    if not path.exists():
        return []
    urls = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.lower().startswith("outlook_sources"):
            continue
        if "." in stripped and "http" in stripped:
            url = stripped.split(maxsplit=1)[-1]
            urls.append(url)
    return urls


def seed_sources(session: Session) -> list[Source]:
    rows: list[Source] = []
    seen_names: set[str] = set()
    for name, url in OFFICIAL_ADAPTERS:
        if name in seen_names:
            continue
        existing = session.scalar(select(Source).where(Source.institution_name == name))
        if existing:
            existing.website = ADAPTER_CONFIGS.get(name, {}).get("url", existing.website)
            existing.adapter_type = "official_index"
            rows.append(existing)
            seen_names.add(name)
            continue
        source = Source(
            institution_name=name,
            website=url,
            category="official",
            trust_score=1.0,
            update_frequency="manual/on-demand",
            adapter_type="official_index",
            active=True,
        )
        session.add(source)
        rows.append(source)
        seen_names.add(name)
    for url in load_outlook_sources():
        name = _source_name_from_url(url)
        if name in seen_names:
            continue
        existing = session.scalar(select(Source).where(Source.institution_name == name))
        if existing:
            config = ADAPTER_CONFIGS.get(name)
            if config:
                existing.website = config["url"]
            rows.append(existing)
            seen_names.add(name)
            continue
        source = Source(
            institution_name=name,
            website=url,
            category="allowed_research",
            trust_score=0.8,
            update_frequency="manual/url",
            adapter_type="outlook_registry",
            active=True,
        )
        session.add(source)
        rows.append(source)
        seen_names.add(name)
    session.flush()
    return rows


def _source_name_from_url(url: str) -> str:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    known = {
        "imf.org": "IMF Publications",
        "federalreserve.gov": "Federal Reserve Publications",
        "ecb.europa.eu": "ECB Publications",
        "data.ecb.europa.eu": "ECB Publications",
        "bankofengland.co.uk": "Bank of England Publications",
        "boj.or.jp": "Bank of Japan Outlook",
        "oecd.org": "OECD Reports",
        "blackrock.com": "BlackRock Investment Institute",
        "jpmorgan.com": "J.P. Morgan Insights",
        "goldmansachs.com": "Goldman Sachs Insights",
        "morganstanley.com": "Morgan Stanley Ideas",
        "corporate.vanguard.com": "Vanguard Research",
        "research-center.amundi.com": "Amundi Research Center",
        "moodys.com": "Moody's Insights",
        "spglobal.com": "S&P Global Research",
        "fitchratings.com": "Fitch Ratings Economics",
        "msci.com": "MSCI Research",
        "reuters.com": "Reuters Markets",
        "ft.com": "Financial Times",
    }
    for domain, name in known.items():
        if host.endswith(domain):
            return name
    label = host.split(".")[0].replace("-", " ").title() or "Allowed Research Source"
    return f"{label} Research"


def get_or_create_manual_source(session: Session, name: str, website: str | None = None, is_demo: bool = False) -> Source:
    existing = session.scalar(select(Source).where(Source.institution_name == name))
    if existing:
        return existing
    source = Source(
        institution_name=name,
        website=website or f"manual://{hashlib.sha1(name.encode()).hexdigest()[:12]}",
        category="manual",
        trust_score=0.7 if not is_demo else 1.0,
        update_frequency="manual",
        adapter_type="manual",
        active=True,
        is_demo=is_demo,
    )
    session.add(source)
    session.flush()
    return source


def create_article(
    session: Session,
    source: Source,
    title: str,
    publication_date: date,
    content_text: str,
    url: str | None = None,
    fetch_status: FetchStatus = FetchStatus.MANUAL_TEXT,
    is_demo: bool = False,
) -> Article:
    canonical = canonicalize_url(url)
    source_reference = canonical or f"manual://{source.id}"
    digest = content_hash(content_text + title + source_reference)
    existing = session.scalar(select(Article).where(Article.source_id == source.id, Article.content_hash == digest))
    if existing is None and canonical:
        existing = session.scalar(select(Article).where(Article.canonical_url == canonical))
    if existing:
        return existing
    article = Article(
        source_id=source.id,
        title=title,
        publication_date=publication_date,
        url=url,
        canonical_url=canonical,
        source_reference=source_reference,
        language="ru",
        content_text=content_text,
        excerpt=content_text[:600],
        content_hash=digest,
        fetch_status=fetch_status.value,
        processing_status="NEW",
        is_demo=is_demo,
    )
    session.add(article)
    session.flush()
    return article


def import_article_text(session: Session, title: str, source_name: str, publication_date: date, text: str) -> Article:
    source = get_or_create_manual_source(session, source_name)
    return create_article(session, source, title, publication_date, text, fetch_status=FetchStatus.MANUAL_TEXT)


def import_article_url(session: Session, url: str, source_name: str, publication_date: date | None = None) -> Article:
    ok, message = validate_public_http_url(url)
    if not ok:
        raise ValueError(message)
    settings = get_settings()
    source = get_or_create_manual_source(session, source_name, website=url)
    with _http_client(settings) as client:
        fetched = _fetch_publication_document(client, url, settings)
    if not fetched.text.strip():
        raise ValueError("Текст публикации не найден; добавьте материал вручную.")
    return create_article(
        session,
        source,
        fetched.title,
        publication_date or fetched.publication_date or date.today(),
        fetched.text,
        url=url,
        fetch_status=FetchStatus.FETCHED,
    )


def run_update(
    session: Session,
    source_ids: list[str] | None = None,
    settings: Settings | None = None,
    *,
    process_articles: bool = True,
) -> UpdateJob:
    settings = settings or get_settings()
    seed_sources(session)
    sources_query = select(Source).where(Source.active.is_(True))
    if source_ids:
        sources_query = sources_query.where(Source.id.in_(source_ids))
    else:
        sources_query = sources_query.where(Source.institution_name.in_(PRIORITY_INGESTION_SOURCES))
    sources = list(session.scalars(sources_query).all())
    job = UpdateJob(status=UpdateStatus.RUNNING.value, requested_mode="priority_ingestion", total_sources=len(sources))
    session.add(job)
    session.flush()
    details = []
    succeeded = 0
    partially_succeeded = 0
    failed = 0
    documents_collected = 0
    documents_reused = 0
    started = time.monotonic()
    stopped_by_budget = False
    with _http_client(settings) as client:
        for source in sources:
            if _budget_exceeded(started, settings.collection_wall_clock_seconds):
                stopped_by_budget = True
                break
            source_started = time.monotonic()
            try:
                config = ADAPTER_CONFIGS.get(source.institution_name) or {"kind": "generic_page", "url": source.website}
                candidates = _load_candidates(client, config, settings)
                saved = 0
                created = 0
                reused = 0
                analysed = 0
                manual_required = 0
                skipped = 0
                errors: list[str] = []
                fallback_runs = 0
                existing_article_ids = {
                    row.id for row in session.scalars(select(Article.id).where(Article.source_id == source.id)).all()
                }
                for candidate in candidates[: max(settings.max_articles_per_source * 2, settings.max_articles_per_source)]:
                    if analysed >= settings.max_articles_per_source:
                        break
                    if _budget_exceeded(source_started, settings.source_adapter_timeout_seconds):
                        errors.append("SOURCE_ADAPTER_TIMEOUT")
                        skipped += 1
                        break
                    if _budget_exceeded(started, settings.collection_wall_clock_seconds):
                        errors.append("COLLECTION_WALL_CLOCK_TIMEOUT")
                        stopped_by_budget = True
                        break
                    try:
                        if canonicalize_url(candidate.url) == canonicalize_url(source.website):
                            skipped += 1
                            continue
                        fetched = _fetch_publication_document(client, candidate.url, settings)
                        publication_date = candidate.publication_date or fetched.publication_date
                        if publication_date is None:
                            raise ValueError("publication date not found")
                        if publication_date < datetime.now(UTC).date() - timedelta(days=settings.lookback_days):
                            skipped += 1
                            continue
                        if len(fetched.text) < 300:
                            raise ValueError("publication text too short; likely index/empty page")
                        article = create_article(
                            session,
                            source,
                            fetched.title or candidate.title,
                            publication_date,
                            fetched.text,
                            url=candidate.url,
                            fetch_status=FetchStatus.FETCHED,
                        )
                        article.fetch_status = FetchStatus.PARSED.value
                        saved += 1
                        if article.id in existing_article_ids:
                            reused += 1
                        else:
                            created += 1
                            existing_article_ids.add(article.id)
                        if process_articles:
                            result = PipelineService().process_article(session, article.id)
                            if result.get("views", 0) <= 0:
                                article.processing_status = "MANUAL_REQUIRED"
                                manual_required += 1
                            else:
                                article.fetch_status = FetchStatus.ANALYSED.value
                                fallback_runs += int(bool(result.get("fallback")))
                                analysed += 1
                        else:
                            article.fetch_status = FetchStatus.PARSED.value
                            article.processing_status = "SOURCE_SELECTION_READY"
                            analysed += 1
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"{candidate.url}: {str(exc)[:180]}")
                        skipped += 1
                    if stopped_by_budget:
                        break
                if saved == 0:
                    raise ValueError("No publication was saved; " + "; ".join(errors[:2]))
                source.last_checked_at = utcnow()
                succeeded += 1
                if errors:
                    partially_succeeded += 1
                documents_collected += created
                documents_reused += reused
                details.append(
                    {
                        "source": source.institution_name,
                        "status": "partial" if errors else "ok",
                        "saved_articles": saved,
                        "created_articles": created,
                        "reused_articles": reused,
                        "analysed_articles": analysed,
                        "manual_required_articles": manual_required,
                        "fallback_runs": fallback_runs,
                        "skipped": skipped,
                        "candidate_count": len(candidates),
                        "errors": errors[:3],
                    }
                )
            except Exception as exc:  # noqa: BLE001 - adapter isolation is a product requirement
                failed += 1
                details.append({"source": source.institution_name, "status": "error", "error": str(exc)[:500]})
            if stopped_by_budget:
                break
    job.succeeded = succeeded
    job.failed = failed
    job.status = UpdateStatus.SUCCESS.value if failed == 0 else UpdateStatus.PARTIAL_FAILURE.value if succeeded else UpdateStatus.FAILED.value
    job.finished_at = utcnow()
    job.error_summary = "; ".join(d["error"] for d in details if d.get("error"))[:2000]
    attempted = succeeded + failed
    total_duration = round(time.monotonic() - started, 2)
    details.append(
        {
            "source": "__collection_summary__",
            "status": "summary",
            "sources_attempted": attempted,
            "sources_succeeded": max(0, succeeded - partially_succeeded),
            "sources_partially_succeeded": partially_succeeded,
            "sources_failed": failed,
            "sources_not_attempted": max(0, len(sources) - attempted),
            "documents_collected": documents_collected,
            "documents_reused": documents_reused,
            "total_duration_seconds": total_duration,
            "stopped_by_budget": stopped_by_budget,
            "wall_clock_budget_seconds": settings.collection_wall_clock_seconds,
            "source_budget_seconds": settings.source_adapter_timeout_seconds,
        }
    )
    job.details = details
    session.flush()
    return job


def _http_client(settings: Settings) -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(
            timeout=settings.http_timeout_seconds,
            connect=settings.http_connect_timeout_seconds,
            read=settings.http_read_timeout_seconds,
            write=settings.http_timeout_seconds,
            pool=settings.http_connect_timeout_seconds,
        ),
        follow_redirects=True,
        headers={
            "User-Agent": "MAE-Shift-Signal-MVP/1.0 (+manual research tool)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )


def _budget_exceeded(started: float, budget_seconds: int) -> bool:
    return budget_seconds > 0 and (time.monotonic() - started) >= budget_seconds


def _request(client: httpx.Client, method: str, url: str, settings: Settings, **kwargs):
    attempts = max(1, settings.http_retries + 1)
    last_error: Exception | None = None
    request = getattr(client, method.lower())
    for attempt in range(attempts):
        try:
            return request(url, **kwargs)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
            last_error = exc
            if attempt + 1 >= attempts:
                break
            time.sleep(min(1.5, 0.3 * (attempt + 1)))
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"HTTP request failed without response: {method} {url}")


def _load_candidates(client: httpx.Client, config: dict, settings: Settings) -> list[PublicationCandidate]:
    ok, reason = validate_public_http_url(config["url"])
    if not ok:
        raise ValueError(reason)
    response = _request(client, "GET", config["url"], settings)
    response.raise_for_status()
    if config["kind"] == "rss":
        return _parse_rss_candidates(response.text, settings.lookback_days)
    if config["kind"] == "imf_latest":
        return _parse_imf_latest_candidates(response.text, response.url, settings.lookback_days)
    if config["kind"] == "boe_news_api":
        return _parse_boe_news_candidates(client, response.text, str(response.url), settings)
    if config["kind"] == "generic_page":
        return _parse_generic_page_candidates(response.text, str(response.url), settings)
    raise ValueError("Unknown adapter kind")


def _parse_rss_candidates(xml_text: str, lookback_days: int) -> list[PublicationCandidate]:
    root = ET.fromstring(xml_text.lstrip("\ufeff"))
    cutoff = datetime.now(UTC).date() - timedelta(days=lookback_days)
    candidates: list[PublicationCandidate] = []
    for item in root.findall(".//item"):
        title = _xml_text(item, "title")
        link = _xml_text(item, "link") or _xml_text(item, "guid")
        pub_raw = _xml_text(item, "pubDate")
        summary = _xml_text(item, "description")
        if not title or not link or not pub_raw:
            continue
        try:
            pub_date = parsedate_to_datetime(pub_raw).date()
        except (KeyError, TypeError, ValueError):
            continue
        if pub_date < cutoff:
            continue
        candidates.append(PublicationCandidate(_clean_text(title), link.strip(), pub_date, _clean_text(summary)))
    return candidates


def _parse_imf_latest_candidates(html: str, base_url: str, lookback_days: int) -> list[PublicationCandidate]:
    cutoff = datetime.now(UTC).date() - timedelta(days=lookback_days)
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[PublicationCandidate] = []
    for block in soup.select("div.high"):
        link_tag = block.find("a", href=True)
        date_tag = block.find(class_="date")
        if not link_tag or not date_tag:
            continue
        title = _clean_text(link_tag.get_text(" "))
        url = urljoin(str(base_url), link_tag["href"])
        date_text = _clean_text(date_tag.get_text(" "))
        pub_date = _parse_imf_date(date_text)
        if not title or not pub_date or pub_date < cutoff:
            continue
        candidates.append(PublicationCandidate(title, url, pub_date))
    return candidates


def _parse_boe_news_candidates(client: httpx.Client, html: str, base_url: str, settings: Settings) -> list[PublicationCandidate]:
    source_id = _regex_group(r"NewsPageDataSourceID\s*=\s*\"([^\"]+)\"", html)
    news_types = re.findall(r"NewsTypes\s*=\s*\[\"([a-f0-9]+)\"\]", html)
    if not source_id:
        raise ValueError("Bank of England publication list metadata not found")
    page_size = max(settings.max_articles_per_source * 3, 10)
    response = _request(
        client,
        "POST",
        urljoin(base_url, "/_api/News/RefreshPagedNewsList"),
        settings,
        data={
            "SearchTerm": "",
            "Id": source_id,
            "PageSize": str(page_size),
            "NewsTypes": news_types or ["ce90163e489841e0b66d06243d35d5cb"],
            "NewsTypesAvailable": news_types or ["ce90163e489841e0b66d06243d35d5cb"],
            "Taxonomies": [],
            "TaxonomiesAvailable": [],
            "Page": "1",
            "Direction": "1",
            "Grid": "false",
            "InfiniteScrolling": "false",
        },
        headers={
            "Referer": "https://www.bankofengland.co.uk/news/publications",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    response.raise_for_status()
    payload = response.json()
    results_html = payload.get("Results", "")
    soup = BeautifulSoup(results_html, "html.parser")
    cutoff = datetime.now(UTC).date() - timedelta(days=settings.lookback_days)
    candidates: list[PublicationCandidate] = []
    seen: set[str] = set()
    for link in soup.select("a.release[href]"):
        url = urljoin("https://www.bankofengland.co.uk", link["href"])
        canonical = canonicalize_url(url)
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        time_tag = link.find("time")
        title_tag = link.select_one("h3.list-view") or link.find("h3", itemprop="name") or link.find("h3")
        if not time_tag or not title_tag:
            continue
        try:
            pub_date = datetime.fromisoformat(str(time_tag["datetime"])).date()
        except (KeyError, ValueError):
            continue
        if pub_date < cutoff:
            continue
        title = _clean_text(title_tag.get_text(" "))
        if not title:
            continue
        tag = link.select_one(".release-tag")
        candidates.append(
            PublicationCandidate(
                title=title,
                url=url,
                publication_date=pub_date,
                summary=_clean_text(tag.get_text(" ")) if tag else "",
            )
        )
    return candidates


GENERIC_CANDIDATE_KEYWORDS = re.compile(
    r"(outlook|market|markets|investment|investing|research|insight|global|weekly|monthly|macro|econom|"
    r"equity|fixed|income|bond|credit|rates|inflation|portfolio|strategy|asset|allocation|gold|2026|2025)",
    re.I,
)

GENERIC_SKIP_KEYWORDS = re.compile(
    r"(privacy|cookie|login|sign in|subscribe|newsletter|contact|career|location|language|terms|"
    r"fund|product|webcast|podcast|video|about us|press release|skip to)",
    re.I,
)


def _parse_generic_page_candidates(html: str, base_url: str, settings: Settings) -> list[PublicationCandidate]:
    soup = BeautifulSoup(html, "html.parser")
    base_host = urlparse(base_url).netloc.lower().removeprefix("www.")
    seen: set[str] = set()
    ranked: list[tuple[int, PublicationCandidate]] = []
    for link in soup.find_all("a", href=True):
        raw_title = _clean_text(link.get_text(" "))
        if not raw_title or GENERIC_SKIP_KEYWORDS.search(raw_title):
            continue
        url = urljoin(base_url, link["href"])
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            continue
        host = parsed.netloc.lower().removeprefix("www.")
        if base_host and base_host not in host and host not in base_host:
            continue
        canonical = canonicalize_url(url)
        if not canonical or canonical in seen or canonical == canonicalize_url(base_url):
            continue
        context = _clean_text(" ".join([raw_title, link.get("aria-label") or "", link.get("title") or "", link.parent.get_text(" ") if link.parent else "", url]))
        if not GENERIC_CANDIDATE_KEYWORDS.search(context):
            continue
        if _looks_like_navigation_url(url):
            continue
        seen.add(canonical)
        pub_date = _parse_date_from_text(context) or _parse_date_from_url(url)
        if pub_date and pub_date < datetime.now(UTC).date() - timedelta(days=settings.lookback_days):
            continue
        score = _candidate_relevance_score(raw_title, url, context, pub_date)
        ranked.append((score, PublicationCandidate(raw_title[:240], url, pub_date, context[:500])))
    ranked.sort(key=lambda item: item[0], reverse=True)
    limit = max(settings.max_articles_per_source * 4, 12)
    return [candidate for _, candidate in ranked[:limit]]


def _fetch_publication_text(client: httpx.Client, url: str, settings: Settings) -> tuple[str, str]:
    fetched = _fetch_publication_document(client, url, settings)
    return fetched.title, fetched.text


def _fetch_publication_document(client: httpx.Client, url: str, settings: Settings) -> FetchedPublication:
    ok, reason = validate_public_http_url(url)
    if not ok:
        raise ValueError(reason)
    response = _request(client, "GET", url, settings)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").lower()
    raw_bytes = response.content[: settings.max_download_mb * 1024 * 1024]
    if "pdf" in content_type or url.lower().endswith(".pdf"):
        text = _extract_pdf_text(raw_bytes)
        pub_date = _parse_date_from_url(url) or _parse_last_modified(response.headers.get("last-modified"))
        return FetchedPublication(_clean_text(url.rsplit("/", 1)[-1]), text, pub_date)
    html = raw_bytes.decode(response.encoding or "utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    title = _clean_text((soup.title.string if soup.title and soup.title.string else "") or url)
    pub_date = _parse_date_from_html(soup) or _parse_date_from_url(url)
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "form"]):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.find(id=re.compile("content|article|main", re.I)) or soup.body or soup
    text = "\n".join(_clean_text(part) for part in main.get_text("\n").splitlines() if _clean_text(part))
    if len(text) < 300 and soup.body is not None:
        text = "\n".join(_clean_text(part) for part in soup.body.get_text("\n").splitlines() if _clean_text(part))
    if _looks_like_index_page(url, title, text):
        raise ValueError("fetched page looks like an index, not a publication")
    return FetchedPublication(title[:500], text, pub_date)


def _extract_pdf_text(raw_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(raw_bytes))
    text = "\n".join(page.extract_text() or "" for page in reader.pages[:20])
    if not text.strip():
        raise ValueError("PDF has no text layer")
    return text


def _looks_like_index_page(url: str, title: str, text: str) -> bool:
    lowered_title = title.lower()
    if len(text) < 300:
        return True
    index_words = ["publications", "latest releases", "press releases", "home", "404", "research and insights"]
    if any(word in lowered_title for word in index_words) and len(text) < 1200:
        return True
    if canonicalize_url(url) in {canonicalize_url(config["url"]) for config in ADAPTER_CONFIGS.values()}:
        return True
    return False


def _looks_like_navigation_url(url: str) -> bool:
    path = urlparse(url).path.lower().strip("/")
    if not path:
        return True
    navigation_patterns = [
        r"(^|/)what-we-do($|/)",
        r"(^|/)solutions($|/)",
        r"(^|/)coverage($|/)",
        r"(^|/)topics($|/)",
        r"(^|/)asset-class($|/)",
        r"(^|/)latest-rating-actions($|/)",
        r"(^|/)research-and-insights/asset-class/",
        r"(^|/)insights/research$",
        r"(^|/)insights/markets$",
        r"(^|/)insights/markets-and-economy$",
    ]
    if any(re.search(pattern, path) for pattern in navigation_patterns):
        return True
    blocked_fragments = [
        "sign-on",
        "login",
        "contact",
        "about",
        "careers",
        "privacy",
        "terms",
        "products",
        "funds",
        "preference",
        "newsletter",
        "events",
        "podcasts",
        "videos",
    ]
    return any(fragment in path for fragment in blocked_fragments)


def _candidate_relevance_score(title: str, url: str, context: str, pub_date: date | None) -> int:
    lowered = f"{title} {url} {context}".casefold()
    score = 0
    for term, weight in {
        "outlook": 8,
        "market": 5,
        "markets": 5,
        "investment": 4,
        "research": 4,
        "insight": 3,
        "macro": 5,
        "economic": 5,
        "equity": 4,
        "fixed income": 5,
        "credit": 4,
        "rates": 4,
        "inflation": 4,
        "asset allocation": 6,
        "weekly": 2,
        "monthly": 2,
    }.items():
        if term in lowered:
            score += weight
    if pub_date:
        age = max(0, (datetime.now(UTC).date() - pub_date).days)
        score += max(0, 20 - age // 5)
    if re.search(r"/(article|articles|insights|research|reports|publications)/", url, re.I):
        score += 4
    if GENERIC_SKIP_KEYWORDS.search(title):
        score -= 20
    return score


def _parse_date_from_html(soup: BeautifulSoup) -> date | None:
    meta_selectors = [
        ("property", "article:published_time"),
        ("property", "og:published_time"),
        ("property", "article:modified_time"),
        ("property", "og:updated_time"),
        ("name", "date"),
        ("name", "dc.date"),
        ("name", "dcterms.date"),
        ("name", "publication_date"),
        ("name", "publishdate"),
        ("name", "content_publishedAt"),
        ("name", "content_publishDate"),
        ("itemprop", "datePublished"),
        ("itemprop", "dateModified"),
    ]
    for key, value in meta_selectors:
        tag = soup.find("meta", attrs={key: re.compile(f"^{re.escape(value)}$", re.I)})
        content = tag.get("content") if tag else None
        parsed = _parse_date_value(content or "")
        if parsed:
            return parsed
    for tag in soup.find_all("meta"):
        marker = " ".join(str(tag.get(key) or "") for key in ["name", "property", "itemprop"]).casefold()
        content = str(tag.get("content") or "")
        if not content:
            continue
        if "alg-search-date" in marker:
            parsed = _parse_date_value(content)
            if parsed:
                return parsed
        if any(term in marker for term in ["published", "publish", "date", "modified"]):
            parsed = _parse_date_value(content)
            if parsed:
                return parsed
    for time_tag in soup.find_all("time"):
        parsed = _parse_date_value(str(time_tag.get("datetime") or "")) or _parse_date_from_text(time_tag.get_text(" "))
        if parsed:
            return parsed
    for script in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        parsed = _parse_date_from_text(script.get_text(" ")[:5000])
        if parsed:
            return parsed
    article_text = _clean_text((soup.find("article") or soup.find("main") or soup).get_text(" "))[:5000]
    return _parse_date_from_text(article_text)


def _parse_date_from_url(url: str) -> date | None:
    return _parse_date_from_text(url)


def _parse_date_from_text(value: str) -> date | None:
    if not value:
        return None
    patterns = [
        ("ymd", r"(20[2-9][0-9])[-/](0?[1-9]|1[0-2])[-/](0?[1-9]|[12][0-9]|3[01])"),
        ("dmy_numeric", r"(0?[1-9]|[12][0-9]|3[01])[-/](0?[1-9]|1[0-2])[-/](20[2-9][0-9])"),
        ("mdy_text", r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+(0?[1-9]|[12][0-9]|3[01]),?\s+(20[2-9][0-9])"),
        ("dmy_text", r"(0?[1-9]|[12][0-9]|3[01])\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+(20[2-9][0-9])"),
    ]
    for kind, pattern in patterns:
        match = re.search(pattern, value, flags=re.I)
        if not match:
            continue
        try:
            groups = match.groups()
            if kind == "ymd":
                year, month, day = int(groups[0]), int(groups[1]), int(groups[2])
            elif kind == "dmy_numeric":
                day, month, year = int(groups[0]), int(groups[1]), int(groups[2])
            elif kind == "mdy_text":
                month = _month_number(groups[0])
                day, year = int(groups[1]), int(groups[2])
            else:
                day = int(groups[0])
                month = _month_number(groups[1])
                year = int(groups[2])
            return date(year, month, day)
        except (TypeError, ValueError):
            continue
    return None


def _parse_date_value(value: str) -> date | None:
    cleaned = (value or "").strip()
    if not cleaned:
        return None
    if re.fullmatch(r"\d{10,13}", cleaned):
        try:
            timestamp = int(cleaned[:10])
            return datetime.fromtimestamp(timestamp, tz=UTC).date()
        except (OSError, ValueError):
            return None
    try:
        return datetime.fromisoformat(cleaned.replace("Z", "+00:00")).date()
    except ValueError:
        return _parse_date_from_text(cleaned)


def _parse_last_modified(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).date()
    except (TypeError, ValueError):
        return None


def _month_number(value: str) -> int:
    months = {
        "jan": 1,
        "feb": 2,
        "mar": 3,
        "apr": 4,
        "may": 5,
        "jun": 6,
        "jul": 7,
        "aug": 8,
        "sep": 9,
        "sept": 9,
        "oct": 10,
        "nov": 11,
        "dec": 12,
    }
    return months[value.casefold()[:4] if value.casefold().startswith("sept") else value.casefold()[:3]]


def _parse_imf_date(value: str) -> date | None:
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(value.split("\n")[0].strip(), fmt).date()
        except ValueError:
            continue
    return None


def _regex_group(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text)
    return match.group(1) if match else None


def _xml_text(item: ET.Element, tag: str) -> str:
    found = item.find(tag)
    return "".join(found.itertext()).strip() if found is not None else ""


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def export_articles_csv(session: Session) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["id", "title", "publication_date", "source", "url", "fetch_status"])
    for article in session.scalars(select(Article)).all():
        writer.writerow(
            [
                article.id,
                article.title,
                article.publication_date.isoformat(),
                article.source.institution_name,
                article.source_reference,
                article.fetch_status,
            ]
        )
    return out.getvalue()
