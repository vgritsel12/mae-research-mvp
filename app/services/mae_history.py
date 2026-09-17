from __future__ import annotations

import calendar
import json
import shutil
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import ROOT_DIR
from app.domain.models import (
    Article,
    EvidenceItem,
    MaeSnapshot,
    MaeSnapshotCell,
    ResearchView,
    Source,
    SourceIndependenceGroup,
    utcnow,
)
from app.services.normalization import canonical_cell_registry, clip_score, template_region, valid_score
from app.services.production import is_content_valid_article, is_production_research_view


HISTORY_LOOKBACK_DAYS = 90
SCORED = "SCORED"
NO_DATA = "NO_DATA"
NOT_APPLICABLE = "NOT_APPLICABLE"
DIRECT = "DIRECT"
MEDIUM_TERM_HORIZON = "6-12M"


class MaeHistoryImmutableError(RuntimeError):
    pass


@dataclass(frozen=True)
class EvidenceCandidate:
    article_id: str
    research_view_id: str
    provider: str
    provider_group: str
    report_title: str
    publication_date: date
    ingested_at: datetime | None
    url: str
    exact_quote: str
    affected_region: str
    affected_asset_segment: str
    template_row_key: str
    canonical_cell_id: str
    extracted_stance: str
    score: int
    source_specificity: str = DIRECT


@dataclass(frozen=True)
class SnapshotCellDraft:
    canonical_cell_id: str
    template_row_key: str
    region: str
    applicability: str
    mae_score: int | None
    previous_snapshot_score: int | None
    score_change: int | None
    coverage_status: str
    source_count: int
    independent_source_count: int
    evidence: list[EvidenceCandidate]
    disagreement: dict[str, Any]
    explanation: str
    scenario_adjustment: int | None = None
    scenario_adjustment_reason: str = ""


@dataclass(frozen=True)
class SnapshotDraft:
    snapshot_date: date
    cells: list[SnapshotCellDraft]
    coverage_report: dict[str, Any]


@dataclass(frozen=True)
class HistoryBuildResult:
    snapshots: list[SnapshotDraft]
    dry_run: bool
    saved_snapshot_ids: list[str] = field(default_factory=list)
    backup_path: Path | None = None
    coverage_report_path: Path | None = None

    @property
    def coverage_by_date(self) -> list[dict[str, Any]]:
        return [snapshot.coverage_report for snapshot in self.snapshots]


def default_snapshot_dates(from_date: date = date(2026, 1, 1), to_date: date = date(2026, 7, 12)) -> list[date]:
    dates: list[date] = []
    current = date(from_date.year, from_date.month, 1)
    while current <= to_date:
        last_day = calendar.monthrange(current.year, current.month)[1]
        month_end = date(current.year, current.month, last_day)
        if from_date <= month_end <= to_date:
            dates.append(month_end)
        year = current.year + (1 if current.month == 12 else 0)
        month = 1 if current.month == 12 else current.month + 1
        current = date(year, month, 1)
    if to_date not in dates:
        dates.append(to_date)
    return dates


def reconstruct_mae_history(
    session: Session,
    *,
    snapshot_dates: list[date] | None = None,
    from_date: date = date(2026, 1, 1),
    to_date: date = date(2026, 7, 12),
    lookback_days: int = HISTORY_LOOKBACK_DAYS,
    dry_run: bool = True,
    replace_existing: bool = False,
    coverage_report_path: Path | None = None,
    backup_path: Path | None = None,
) -> HistoryBuildResult:
    dates = snapshot_dates or default_snapshot_dates(from_date, to_date)
    previous_scores: dict[tuple[str, str], int | None] = {}
    snapshots: list[SnapshotDraft] = []
    for snapshot_date in dates:
        draft = build_snapshot_draft(session, snapshot_date, previous_scores=previous_scores, lookback_days=lookback_days)
        snapshots.append(draft)
        previous_scores = {
            (cell.template_row_key, template_region(cell.region)): cell.mae_score
            for cell in draft.cells
            if cell.applicability != NOT_APPLICABLE
        }

    saved_ids: list[str] = []
    if not dry_run:
        if backup_path is not None:
            backup_database(backup_path)
        ensure_source_independence_groups(session)
        for draft in snapshots:
            saved_ids.append(save_snapshot(session, draft, replace_existing=replace_existing))
        session.flush()
    if coverage_report_path is not None:
        write_coverage_report(snapshots, coverage_report_path)
    return HistoryBuildResult(
        snapshots=snapshots,
        dry_run=dry_run,
        saved_snapshot_ids=saved_ids,
        backup_path=backup_path,
        coverage_report_path=coverage_report_path,
    )


def build_snapshot_draft(
    session: Session,
    snapshot_date: date,
    *,
    previous_scores: dict[tuple[str, str], int | None] | None = None,
    lookback_days: int = HISTORY_LOOKBACK_DAYS,
) -> SnapshotDraft:
    previous_scores = previous_scores or {}
    start_date = snapshot_date - timedelta(days=lookback_days)
    candidates = _evidence_candidates(session, snapshot_date, start_date)
    candidates_by_cell: dict[tuple[str, str], list[EvidenceCandidate]] = defaultdict(list)
    for candidate in candidates:
        candidates_by_cell[(candidate.template_row_key, template_region(candidate.affected_region))].append(candidate)

    lookahead = _lookahead_articles(session, snapshot_date)
    stale = _stale_articles(session, snapshot_date, start_date)
    cells: list[SnapshotCellDraft] = []
    canonical_cells = canonical_cell_registry(include_not_applicable=True)
    for canonical in canonical_cells:
        key = (canonical.template_row_key, template_region(canonical.region))
        previous = previous_scores.get(key)
        if canonical.applicability == NOT_APPLICABLE:
            cells.append(
                SnapshotCellDraft(
                    canonical_cell_id=canonical.canonical_cell_id,
                    template_row_key=canonical.template_row_key,
                    region=template_region(canonical.region),
                    applicability=NOT_APPLICABLE,
                    mae_score=None,
                    previous_snapshot_score=previous,
                    score_change=None,
                    coverage_status=NOT_APPLICABLE,
                    source_count=0,
                    independent_source_count=0,
                    evidence=[],
                    disagreement={},
                    explanation="Категория неприменима в канонической MAE-матрице.",
                )
            )
            continue
        cell_evidence = _dedupe_candidates(candidates_by_cell.get(key, []))
        score, disagreement, explanation = _score_from_evidence(cell_evidence)
        score_change = None if score is None or previous is None else score - previous
        cells.append(
            SnapshotCellDraft(
                canonical_cell_id=canonical.canonical_cell_id,
                template_row_key=canonical.template_row_key,
                region=template_region(canonical.region),
                applicability="APPLICABLE",
                mae_score=score,
                previous_snapshot_score=previous,
                score_change=score_change,
                coverage_status=SCORED if score is not None else NO_DATA,
                source_count=len({item.article_id for item in cell_evidence}),
                independent_source_count=len({item.provider_group for item in cell_evidence}),
                evidence=cell_evidence,
                disagreement=disagreement,
                explanation=explanation,
            )
        )

    report = _coverage_report(snapshot_date, cells, candidates, lookahead, stale)
    return SnapshotDraft(snapshot_date=snapshot_date, cells=cells, coverage_report=report)


def save_snapshot(session: Session, draft: SnapshotDraft, *, replace_existing: bool = False) -> str:
    existing = session.scalar(
        select(MaeSnapshot).where(MaeSnapshot.snapshot_date == draft.snapshot_date, MaeSnapshot.is_demo.is_(False))
    )
    if existing and existing.immutable and not replace_existing:
        raise MaeHistoryImmutableError(f"Snapshot {draft.snapshot_date.isoformat()} is immutable.")
    if existing:
        session.execute(delete(EvidenceItem).where(EvidenceItem.snapshot_id == existing.id))
        session.execute(delete(MaeSnapshotCell).where(MaeSnapshotCell.snapshot_id == existing.id))
        session.delete(existing)
        session.flush()
    snapshot = MaeSnapshot(
        snapshot_date=draft.snapshot_date,
        lookback_days=HISTORY_LOOKBACK_DAYS,
        status="FINAL",
        run_metadata={
            "method": "point_in_time_reconstruction",
            "no_lookahead": True,
            "minimum_independent_sources": 2,
            "score_range": "-3..+3",
        },
        coverage_report=draft.coverage_report,
        immutable=True,
        is_demo=False,
    )
    session.add(snapshot)
    session.flush()
    for cell in draft.cells:
        row = MaeSnapshotCell(
            snapshot_id=snapshot.id,
            snapshot_date=draft.snapshot_date,
            canonical_cell_id=cell.canonical_cell_id,
            template_row_key=cell.template_row_key,
            region=cell.region,
            applicability=cell.applicability,
            mae_score=cell.mae_score,
            previous_snapshot_score=cell.previous_snapshot_score,
            score_change=cell.score_change,
            coverage_status=cell.coverage_status,
            source_count=cell.source_count,
            independent_source_count=cell.independent_source_count,
            evidence_item_ids=[],
            disagreement=cell.disagreement,
            explanation=cell.explanation,
            scenario_adjustment=cell.scenario_adjustment,
            scenario_adjustment_reason=cell.scenario_adjustment_reason,
            is_demo=False,
        )
        session.add(row)
        session.flush()
        evidence_ids: list[str] = []
        for evidence in cell.evidence:
            item = EvidenceItem(
                snapshot_id=snapshot.id,
                snapshot_cell_id=row.id,
                article_id=evidence.article_id,
                research_view_id=evidence.research_view_id,
                snapshot_date=draft.snapshot_date,
                provider=evidence.provider,
                provider_group=evidence.provider_group,
                report_title=evidence.report_title,
                publication_date=evidence.publication_date,
                ingested_at=evidence.ingested_at,
                url=evidence.url,
                exact_quote=evidence.exact_quote,
                affected_region=evidence.affected_region,
                affected_asset_segment=evidence.affected_asset_segment,
                template_row_key=evidence.template_row_key,
                canonical_cell_id=evidence.canonical_cell_id,
                extracted_stance=evidence.extracted_stance,
                source_specificity=evidence.source_specificity,
                source_independence_group=evidence.provider_group,
                is_demo=False,
            )
            session.add(item)
            session.flush()
            evidence_ids.append(item.id)
        row.evidence_item_ids = evidence_ids
    session.flush()
    return snapshot.id


def ensure_source_independence_groups(session: Session) -> dict[str, str]:
    mapping: dict[str, str] = {}
    existing_by_source = {
        row.source_id: row
        for row in session.scalars(select(SourceIndependenceGroup)).all()
        if row.source_id
    }
    for source in session.scalars(select(Source)).all():
        group_key = source_independence_group(source)
        mapping[source.id] = group_key
        row = existing_by_source.get(source.id)
        if row:
            row.provider = source.institution_name
            row.group_key = group_key
            row.domain = _source_domain(source)
            row.is_demo = source.is_demo
            continue
        session.add(
            SourceIndependenceGroup(
                source_id=source.id,
                provider=source.institution_name,
                group_key=group_key,
                domain=_source_domain(source),
                rationale="Grouped by normalized provider identity and source domain.",
                is_demo=source.is_demo,
            )
        )
    session.flush()
    return mapping


def source_independence_group(source: Source | None) -> str:
    if source is None:
        return "unknown"
    domain = _source_domain(source)
    name = (source.institution_name or "").casefold()
    provider_family = {
        "blackrock": "blackrock",
        "ishares": "blackrock",
        "j.p. morgan": "jpmorgan",
        "jp morgan": "jpmorgan",
        "jpmorgan": "jpmorgan",
        "goldman": "goldman-sachs",
        "morgan stanley": "morgan-stanley",
        "amundi": "amundi",
        "barclays": "barclays",
        "home.barclays": "barclays",
        "hsbc": "hsbc",
        "gbm.hsbc": "hsbc",
        "invesco": "invesco",
        "vanguard": "vanguard",
        "state street": "state-street",
        "ssga": "state-street",
        "spdr": "state-street",
        "moody": "moody's",
        "fitch": "fitch",
        "s&p global": "sp-global",
        "spglobal": "sp-global",
        "federal reserve": "federal-reserve",
        "fred": "federal-reserve",
        "stlouisfed": "federal-reserve",
        "ecb": "ecb",
        "european central bank": "ecb",
        "bank of england": "bank-of-england",
        "bank of japan": "bank-of-japan",
        "imf": "imf",
        "oecd": "oecd",
    }
    for token, group in provider_family.items():
        if token in name or token in domain:
            return group
    return domain or _slug(name)


def write_coverage_report(snapshots: list[SnapshotDraft], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": utcnow().isoformat(),
        "method": "point_in_time_mae_history",
        "snapshots": [snapshot.coverage_report for snapshot in snapshots],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def backup_database(destination: Path | None = None) -> Path:
    source = ROOT_DIR / "data" / "mae.db"
    destination = destination or ROOT_DIR / "outputs" / "production_cleanup" / f"pre_mae_history_{utcnow().strftime('%Y%m%d_%H%M%S')}.db"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.exists():
        shutil.copy2(source, destination)
    return destination


def _evidence_candidates(session: Session, snapshot_date: date, start_date: date) -> list[EvidenceCandidate]:
    result: list[EvidenceCandidate] = []
    views = session.scalars(select(ResearchView).order_by(ResearchView.template_row_key, ResearchView.region)).all()
    for view in views:
        article = session.get(Article, view.article_id)
        if not _article_eligible(article, snapshot_date, start_date):
            continue
        if not is_production_research_view(view, article):
            continue
        score = view.position_score
        if score is None:
            continue
        quote = _best_quote(view)
        source = article.source if article else None
        result.append(
            EvidenceCandidate(
                article_id=article.id,
                research_view_id=view.id,
                provider=source.institution_name if source else view.institution,
                provider_group=source_independence_group(source),
                report_title=article.title,
                publication_date=article.publication_date,
                ingested_at=article.created_at,
                url=article.source_reference,
                exact_quote=quote,
                affected_region=template_region(view.region),
                affected_asset_segment=view.asset_segment,
                template_row_key=view.template_row_key,
                canonical_cell_id=view.canonical_cell_id,
                extracted_stance=view.direction,
                score=int(clip_score(score)),
            )
        )
    return result


def _article_eligible(article: Article | None, snapshot_date: date, start_date: date) -> bool:
    if not is_content_valid_article(article):
        return False
    if article is None:
        return False
    if article.publication_date > snapshot_date:
        return False
    if article.publication_date < start_date:
        return False
    return True


def _lookahead_articles(session: Session, snapshot_date: date) -> list[dict[str, Any]]:
    rows = []
    for article in session.scalars(select(Article).where(Article.is_demo.is_(False))).all():
        if article.publication_date > snapshot_date and is_content_valid_article(article):
            rows.append(_article_report_row(article, "after_snapshot_date"))
    return rows


def _stale_articles(session: Session, snapshot_date: date, start_date: date) -> list[dict[str, Any]]:
    rows = []
    for article in session.scalars(select(Article).where(Article.is_demo.is_(False))).all():
        if article.publication_date <= snapshot_date and article.publication_date < start_date and is_content_valid_article(article):
            rows.append(_article_report_row(article, "older_than_lookback"))
    return rows


def _article_report_row(article: Article, reason: str) -> dict[str, Any]:
    return {
        "article_id": article.id,
        "provider": article.source.institution_name if article.source else "",
        "title": article.title,
        "publication_date": article.publication_date.isoformat(),
        "ingested_at": article.created_at.isoformat() if article.created_at else "",
        "url": article.source_reference,
        "reason": reason,
    }


def _dedupe_candidates(candidates: list[EvidenceCandidate]) -> list[EvidenceCandidate]:
    by_article: dict[tuple[str, str], EvidenceCandidate] = {}
    for candidate in candidates:
        key = (candidate.provider_group, candidate.article_id)
        current = by_article.get(key)
        if current is None or abs(candidate.score) > abs(current.score):
            by_article[key] = candidate
    return list(by_article.values())


def _score_from_evidence(evidence: list[EvidenceCandidate]) -> tuple[int | None, dict[str, Any], str]:
    groups = {item.provider_group for item in evidence}
    if len(groups) < 2:
        if not evidence:
            return None, {}, "Недостаточно релевантных источников с действующим сроком актуальности."
        return None, {"reason": "one_independent_source", "providers": sorted(groups)}, "Есть только один независимый источник; MAE-оценка не формируется."
    scores_by_group: dict[str, int] = {}
    for group in sorted(groups):
        group_scores = [item.score for item in evidence if item.provider_group == group]
        scores_by_group[group] = _round_score(sum(group_scores) / len(group_scores))
    signs = Counter(_score_sign(score) for score in scores_by_group.values())
    non_zero_signs = {sign: count for sign, count in signs.items() if sign != 0}
    disagreement = {
        "scores_by_group": scores_by_group,
        "sign_distribution": dict(signs),
    }
    if len(non_zero_signs) > 1:
        top = max(non_zero_signs.values())
        if top < 2 or top / sum(non_zero_signs.values()) < 0.66:
            return None, disagreement | {"reason": "insufficient_consensus"}, "Источники расходятся; достаточного consensus нет."
    average = sum(scores_by_group.values()) / len(scores_by_group)
    score = _round_score(average)
    return score, disagreement, _score_explanation(score, len(groups))


def _round_score(value: float) -> int:
    if -0.4 < value < 0.4:
        return 0
    return valid_score(int(clip_score(round(value))))


def _score_sign(value: int) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def _score_explanation(score: int, group_count: int) -> str:
    if score == 0:
        return f"{group_count} независимых источника дают нейтральную среднесрочную оценку."
    direction = "позитивную" if score > 0 else "негативную"
    return f"{group_count} независимых источника поддерживают {direction} среднесрочную оценку."


def _coverage_report(
    snapshot_date: date,
    cells: list[SnapshotCellDraft],
    candidates: list[EvidenceCandidate],
    lookahead: list[dict[str, Any]],
    stale: list[dict[str, Any]],
) -> dict[str, Any]:
    applicable = [cell for cell in cells if cell.applicability != NOT_APPLICABLE]
    one_source = [cell for cell in applicable if cell.mae_score is None and cell.independent_source_count == 1]
    two_plus = [cell for cell in applicable if cell.independent_source_count >= 2]
    providers = sorted({candidate.provider for candidate in candidates})
    duplicate_groups = {
        group: count
        for group, count in Counter(candidate.provider_group for candidate in candidates).items()
        if count > 1
    }
    return {
        "snapshot_date": snapshot_date.isoformat(),
        "covered_cells": len([cell for cell in applicable if cell.mae_score is not None]),
        "no_data_cells": len([cell for cell in applicable if cell.mae_score is None]),
        "not_applicable_cells": len([cell for cell in cells if cell.applicability == NOT_APPLICABLE]),
        "one_source_cells": len(one_source),
        "two_plus_independent_source_cells": len(two_plus),
        "providers": providers,
        "publication_date_errors": [],
        "potential_lookahead": lookahead,
        "stale_articles_excluded": stale,
        "duplicate_source_groups": duplicate_groups,
    }


def _best_quote(view: ResearchView) -> str:
    for item in view.evidence_quotes or []:
        quote = str(item.get("quote") or "").strip()
        if quote:
            return quote
    return ""


def _source_domain(source: Source | None) -> str:
    if source is None:
        return ""
    parsed = urlparse(source.website or "")
    host = parsed.netloc or parsed.path
    host = host.casefold().replace("www.", "").strip("/")
    return host


def _slug(value: str) -> str:
    text = "".join(ch if ch.isalnum() else "-" for ch in value.casefold())
    return "-".join(part for part in text.split("-") if part) or "unknown"
