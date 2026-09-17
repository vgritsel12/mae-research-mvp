from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.domain.enums import AnalystStatus, Confidence, CoverageStatus, EvidenceStatus, RunMode, SignalDirection
from app.domain.models import Article, ChangeLog, EvidenceObservation, MatrixOverrideLog, MatrixScore, ResearchView, ScenarioCard, ShiftSignal
from app.services.normalization import (
    applicable_canonical_cells,
    clip_score,
    canonical_cell_for,
    canonical_cell_id_for,
    mae_result_template_row_keys,
    round_half_away_from_zero,
    template_region,
    valid_score,
)
from app.services.methodology import is_qualified_research_view
from app.services.production import is_content_valid_article, is_production_research_view, is_production_signal


EVIDENCE_FACTOR = {
    EvidenceStatus.CONFIRMED.value: 1.0,
    EvidenceStatus.MIXED.value: 0.5,
}
CONFIDENCE_FACTOR = {
    Confidence.HIGH.value: 1.0,
    Confidence.MEDIUM.value: 0.75,
    Confidence.LOW.value: 0.5,
}


class MatrixEngine:
    def rebuild_matrix(self, session: Session, today: date | None = None, mode: str | RunMode = RunMode.ALL.value) -> list[MatrixScore]:
        today = today or date.today()
        mode_value = getattr(mode, "value", mode)
        if mode_value == RunMode.ALL.value:
            return [
                *self.rebuild_matrix(session, today=today, mode=RunMode.REAL.value),
                *self.rebuild_matrix(session, today=today, mode=RunMode.DEMO.value),
            ]
        target_is_demo = mode_value == RunMode.DEMO.value
        session.flush()
        canonical_cells = applicable_canonical_cells()
        valid_cells = {(cell.template_row_key, template_region(cell.region)) for cell in canonical_cells}
        for stale in session.scalars(select(MatrixScore).where(MatrixScore.is_demo.is_(target_is_demo))).all():
            normalized_region = template_region(stale.region)
            if stale.region != normalized_region or (stale.template_row_key, normalized_region) not in valid_cells:
                session.delete(stale)
        session.flush()
        signals = [
            s
            for s in session.scalars(select(ShiftSignal)).all()
            if self._eligible_signal(s, valid_cells, today, mode_value)
            and (mode_value != RunMode.REAL.value or is_production_signal(s, session))
        ]
        grouped: dict[tuple[str, str], list[ShiftSignal]] = {}
        for signal in signals:
            grouped.setdefault((signal.template_row_key, template_region(signal.region)), []).append(signal)
        research_grouped = self._fresh_research_views_by_cell(session, today, mode_value, valid_cells)
        scores: list[MatrixScore] = []
        for cell in canonical_cells:
            region = template_region(cell.region)
            items = grouped.get((cell.template_row_key, region), [])
            research_items = research_grouped.get((cell.template_row_key, region), [])
            if items:
                suggested, details = self.calculate_cell_score(items, today)
                metadata = self._cell_metadata(session, items, today, details)
                approved_from_calculation = suggested
            elif research_items:
                suggested, details = self.calculate_research_view_score(research_items, today)
                metadata = self._research_view_metadata(session, research_items, today, details)
                approved_from_calculation = None
            else:
                suggested, details = self.calculate_cell_score(items, today)
                metadata = self._cell_metadata(session, items, today, details)
                approved_from_calculation = None
            existing = session.scalar(
                select(MatrixScore).where(
                    MatrixScore.template_row_key == cell.template_row_key,
                    MatrixScore.region == region,
                    MatrixScore.is_demo.is_(target_is_demo),
                )
            )
            approved = existing.approved_score if existing and existing.override_reason else approved_from_calculation
            approved = valid_score(approved) if approved is not None else None
            if existing:
                existing.canonical_cell_id = cell.canonical_cell_id
                existing.suggested_score = suggested
                existing.approved_score = approved
                existing.calculation_details = {**details, **metadata["calculation_details"]}
                existing.signal_ids = [s.id for s in items]
                existing.is_demo = target_is_demo
                existing.coverage_status = (
                    CoverageStatus.COVERED.value if existing.override_reason else metadata["coverage_status"]
                )
                existing.source_count = metadata["source_count"]
                existing.publication_ids = metadata["publication_ids"]
                existing.publication_dates = metadata["publication_dates"]
                existing.supporting_evidence = metadata["supporting_evidence"]
                existing.contradicting_evidence = metadata["contradicting_evidence"]
                existing.confidence = metadata["confidence"]
                existing.freshness_score = metadata["freshness_score"]
                scores.append(existing)
            else:
                ms = MatrixScore(
                    template_row_key=cell.template_row_key,
                    canonical_cell_id=cell.canonical_cell_id,
                    region=region,
                    suggested_score=suggested,
                    approved_score=approved,
                    calculation_details={**details, **metadata["calculation_details"]},
                    signal_ids=[s.id for s in items],
                    is_demo=target_is_demo,
                    coverage_status=metadata["coverage_status"],
                    source_count=metadata["source_count"],
                    publication_ids=metadata["publication_ids"],
                    publication_dates=metadata["publication_dates"],
                    supporting_evidence=metadata["supporting_evidence"],
                    contradicting_evidence=metadata["contradicting_evidence"],
                    confidence=metadata["confidence"],
                    freshness_score=metadata["freshness_score"],
                )
                session.add(ms)
                scores.append(ms)
        session.flush()
        return scores

    @staticmethod
    def _eligible_signal(signal: ShiftSignal, valid_cells: set[tuple[str, str]], today: date, mode: str = RunMode.ALL.value) -> bool:
        if (signal.template_row_key, template_region(signal.region)) not in valid_cells:
            return False
        if mode == RunMode.DEMO.value and not signal.is_demo:
            return False
        if mode == RunMode.REAL.value and signal.is_demo:
            return False
        if signal.analyst_status != AnalystStatus.APPROVED.value:
            return False
        if signal.evidence_status == EvidenceStatus.INVALIDATED.value:
            return False
        if signal.evidence_status not in {EvidenceStatus.CONFIRMED.value, EvidenceStatus.MIXED.value}:
            return False
        if signal.next_review_date < today:
            return False
        if not signal.source_urls:
            return False
        return True

    @staticmethod
    def calculate_cell_score(signals: list[ShiftSignal], today: date | None = None) -> tuple[int | None, dict[str, Any]]:
        today = today or date.today()
        if not signals:
            return None, {
                "coverage_status": CoverageStatus.NO_DATA.value,
                "support_sum": 0.0,
                "base_weight_sum": 0.0,
                "normalizer": 0.0,
                "raw_cell_score": None,
                "contributions": [],
            }
        source_count = len({source_id for signal in signals for source_id in signal.source_ids})
        if source_count < 2:
            return None, {
                "coverage_status": CoverageStatus.INSUFFICIENT_EVIDENCE.value,
                "reason": "Нужно минимум два независимых источника для утверждённой оценки.",
                "source_count": source_count,
                "support_sum": 0.0,
                "base_weight_sum": 0.0,
                "normalizer": 0.0,
                "raw_cell_score": None,
                "contributions": [],
            }
        support_sum = 0.0
        base_weight_sum = 0.0
        contributions = []
        max_age = get_settings().matrix_max_age_days
        for signal in signals:
            evidence_factor = EVIDENCE_FACTOR.get(signal.evidence_status, 0.0)
            confidence_factor = CONFIDENCE_FACTOR.get(signal.confidence, 0.5)
            age_days = max(0, (today - signal.created_at.date()).days)
            recency_factor = max(0.0, 1.0 - age_days / max_age)
            independence_factor = min(1.0, len(set(signal.source_ids)) / 2.0)
            base_weight = evidence_factor * confidence_factor * recency_factor
            signal_strength = valid_score(signal.suggested_strength)
            contribution = signal_strength * base_weight * independence_factor
            support_sum += contribution
            base_weight_sum += base_weight
            contributions.append(
                {
                    "signal_id": signal.id,
                    "signal_strength": signal_strength,
                    "evidence_factor": evidence_factor,
                    "confidence_factor": confidence_factor,
                    "recency_factor": recency_factor,
                    "independence_factor": independence_factor,
                    "contribution": contribution,
                }
            )
        normalizer = base_weight_sum if base_weight_sum > 0 else 1.0
        raw = clip_score(support_sum / normalizer)
        suggested = valid_score(round_half_away_from_zero(raw))
        return suggested, {
            "coverage_status": CoverageStatus.COVERED.value,
            "source_count": source_count,
            "support_sum": support_sum,
            "base_weight_sum": base_weight_sum,
            "normalizer": normalizer,
            "raw_cell_score": raw,
            "contributions": contributions,
        }

    @staticmethod
    def calculate_research_view_score(views: list[ResearchView], today: date | None = None) -> tuple[int | None, dict[str, Any]]:
        today = today or date.today()
        if not views:
            return None, {"coverage_status": CoverageStatus.NO_DATA.value, "contributions": []}
        source_ids: set[str] = set()
        weighted_sum = 0.0
        weight_sum = 0.0
        contributions: list[dict[str, Any]] = []
        max_age = get_settings().matrix_max_age_days
        for view in views:
            article = getattr(view, "article", None)
            if article:
                source_ids.add(article.source_id)
                age_days = max(0, (today - article.publication_date).days)
            else:
                age_days = 0
            confidence_factor = CONFIDENCE_FACTOR.get(view.confidence, 0.5)
            recency_factor = max(0.0, 1.0 - age_days / max_age)
            score = valid_score(view.position_score or 0)
            weight = confidence_factor * recency_factor
            weighted_sum += score * weight
            weight_sum += weight
            contributions.append(
                {
                    "research_view_id": view.id,
                    "position_score": score,
                    "confidence_factor": confidence_factor,
                    "recency_factor": recency_factor,
                    "weight": weight,
                }
            )
        source_count = len(source_ids)
        if source_count < 2:
            return None, {
                "coverage_status": CoverageStatus.INSUFFICIENT_EVIDENCE.value,
                "source_count": source_count,
                "reason": "Есть fresh research view, но меньше двух независимых источников.",
                "contributions": contributions,
            }
        raw = clip_score(weighted_sum / (weight_sum or 1.0))
        suggested = valid_score(round_half_away_from_zero(raw))
        return suggested, {
            "coverage_status": CoverageStatus.COVERED.value,
            "source_count": source_count,
            "support_sum": weighted_sum,
            "base_weight_sum": weight_sum,
            "raw_cell_score": raw,
            "contributions": contributions,
            "basis": "fresh_research_views",
        }

    def override_score(
        self,
        session: Session,
        template_row_key: str,
        region: str,
        score: int,
        reason: str,
        is_demo: bool = False,
    ) -> MatrixScore:
        if not reason.strip():
            raise ValueError("Override требует комментарий.")
        valid_score(score)
        cell = canonical_cell_for(template_row_key, region, include_not_applicable=True)
        if cell is None or cell.applicability != "APPLICABLE":
            raise ValueError("Override разрешён только для применимой канонической ячейки MAE.")
        row = session.scalar(
            select(MatrixScore).where(
                MatrixScore.template_row_key == template_row_key,
                MatrixScore.region == template_region(region),
                MatrixScore.is_demo.is_(is_demo),
            )
        )
        if row is None:
            row = MatrixScore(
                template_row_key=template_row_key,
                canonical_cell_id=cell.canonical_cell_id,
                region=template_region(region),
                suggested_score=None,
                approved_score=score,
                override_reason=reason,
                calculation_details={"override_created": True, "coverage_status": CoverageStatus.COVERED.value},
                signal_ids=[],
                coverage_status=CoverageStatus.COVERED.value,
                source_count=0,
                confidence=Confidence.LOW.value,
                is_demo=is_demo,
            )
            session.add(row)
        else:
            previous_score = row.approved_score
            row.canonical_cell_id = cell.canonical_cell_id
            row.approved_score = score
            row.override_reason = reason
            row.coverage_status = CoverageStatus.COVERED.value
            row.calculation_details = {
                **(row.calculation_details or {}),
                "override_updated": True,
                "coverage_status": CoverageStatus.COVERED.value,
            }
        session.add(
            MatrixOverrideLog(
                matrix_score_id=row.id,
                template_row_key=row.template_row_key,
                region=row.region,
                previous_score=previous_score if "previous_score" in locals() else None,
                new_score=score,
                reason=reason,
                is_demo=is_demo,
            )
        )
        session.flush()
        return row

    @staticmethod
    def _fresh_research_views_by_cell(
        session: Session,
        today: date,
        mode: str,
        valid_cells: set[tuple[str, str]],
    ) -> dict[tuple[str, str], list[ResearchView]]:
        cutoff = today - timedelta(days=get_settings().lookback_days)
        grouped: dict[tuple[str, str], list[ResearchView]] = {}
        views = session.scalars(select(ResearchView).join(Article, Article.id == ResearchView.article_id)).all()
        for view in views:
            article = session.get(Article, view.article_id)
            if article is None:
                continue
            if mode == RunMode.REAL.value and article.is_demo:
                continue
            if mode == RunMode.DEMO.value and not article.is_demo:
                continue
            if mode == RunMode.REAL.value and not (is_content_valid_article(article) and is_production_research_view(view, article)):
                continue
            if article.publication_date < cutoff:
                continue
            if (view.template_row_key, template_region(view.region)) not in valid_cells:
                continue
            if view.review_status in {AnalystStatus.REJECTED.value, AnalystStatus.INVALIDATED.value}:
                continue
            if view.position_score is None or not view.evidence_quotes:
                continue
            if not is_qualified_research_view(view, article):
                continue
            grouped.setdefault((view.template_row_key, template_region(view.region)), []).append(view)
        return grouped

    @staticmethod
    def _cell_metadata(session: Session, signals: list[ShiftSignal], today: date, details: dict[str, Any]) -> dict[str, Any]:
        source_count = len({source_id for signal in signals for source_id in signal.source_ids})
        article_ids: list[str] = []
        publication_dates: list[str] = []
        supporting: list[dict[str, Any]] = []
        contradicting: list[dict[str, Any]] = []
        for signal in signals:
            change = session.get(ChangeLog, signal.change_id) if signal.change_id else None
            view = session.get(ResearchView, change.current_view_id) if change else None
            article = session.get(Article, view.article_id) if view else None
            if article:
                if article.id not in article_ids:
                    article_ids.append(article.id)
                publication_dates.append(article.publication_date.isoformat())
            if not change:
                continue
            scenarios = session.scalars(select(ScenarioCard).where(ScenarioCard.linked_change_id == change.id)).all()
            scenario_by_id = {scenario.id: scenario for scenario in scenarios}
            if not scenario_by_id:
                continue
            observations = session.scalars(
                select(EvidenceObservation).where(EvidenceObservation.scenario_id.in_(scenario_by_id))
            ).all()
            for observation in observations:
                row = {
                    "scenario": scenario_by_id[observation.scenario_id].scenario_type,
                    "indicator": observation.indicator,
                    "date": observation.observation_date.isoformat(),
                    "actual": observation.actual,
                    "source_url": observation.source_url,
                    "support": observation.support_value,
                }
                if observation.support_value < 0:
                    contradicting.append(row)
                else:
                    supporting.append(row)
        publication_dates = sorted(set(publication_dates), reverse=True)
        max_age = get_settings().matrix_max_age_days
        if publication_dates:
            latest = date.fromisoformat(publication_dates[0])
            age_days = max(0, (today - latest).days)
            freshness_score = max(0.0, min(1.0, 1.0 - age_days / max_age))
        else:
            freshness_score = 0.0
        confidence = _cell_confidence(signals, source_count)
        coverage_status = details.get("coverage_status") or CoverageStatus.NO_DATA.value
        return {
            "coverage_status": coverage_status,
            "source_count": source_count,
            "publication_ids": article_ids,
            "publication_dates": publication_dates,
            "supporting_evidence": supporting[:12],
            "contradicting_evidence": contradicting[:12],
            "confidence": confidence,
            "freshness_score": freshness_score,
            "calculation_details": {
                "coverage_status": coverage_status,
                "source_count": source_count,
                "publication_count": len(article_ids),
                "freshness_score": freshness_score,
            },
        }

    @staticmethod
    def _research_view_metadata(session: Session, views: list[ResearchView], today: date, details: dict[str, Any]) -> dict[str, Any]:
        source_ids: set[str] = set()
        article_ids: list[str] = []
        publication_dates: list[str] = []
        supporting: list[dict[str, Any]] = []
        for view in views:
            article = session.get(Article, view.article_id)
            if article:
                source_ids.add(article.source_id)
                if article.id not in article_ids:
                    article_ids.append(article.id)
                publication_dates.append(article.publication_date.isoformat())
            for quote in view.evidence_quotes or []:
                supporting.append(
                    {
                        "research_view_id": view.id,
                        "institution": view.institution,
                        "quote": quote.get("quote", ""),
                        "source_url": quote.get("source_url") or (article.source_reference if article else ""),
                        "support": view.position_score,
                    }
                )
        publication_dates = sorted(set(publication_dates), reverse=True)
        max_age = get_settings().matrix_max_age_days
        if publication_dates:
            latest = date.fromisoformat(publication_dates[0])
            age_days = max(0, (today - latest).days)
            freshness_score = max(0.0, min(1.0, 1.0 - age_days / max_age))
        else:
            freshness_score = 0.0
        return {
            "coverage_status": details.get("coverage_status") or CoverageStatus.INSUFFICIENT_EVIDENCE.value,
            "source_count": len(source_ids),
            "publication_ids": article_ids,
            "publication_dates": publication_dates,
            "supporting_evidence": supporting[:12],
            "contradicting_evidence": [],
            "confidence": _cell_confidence_from_views(views, len(source_ids)),
            "freshness_score": freshness_score,
            "calculation_details": {
                "coverage_status": details.get("coverage_status"),
                "source_count": len(source_ids),
                "publication_count": len(article_ids),
                "freshness_score": freshness_score,
                "basis": "fresh_research_views",
            },
        }


def _cell_confidence(signals: list[ShiftSignal], source_count: int) -> str:
    if not signals or source_count < 2:
        return Confidence.LOW.value
    ranks = {Confidence.LOW.value: 1, Confidence.MEDIUM.value: 2, Confidence.HIGH.value: 3}
    best = max((ranks.get(signal.confidence, 1) for signal in signals), default=1)
    if best >= 3 and source_count >= 3:
        return Confidence.HIGH.value
    return Confidence.MEDIUM.value


def _cell_confidence_from_views(views: list[ResearchView], source_count: int) -> str:
    if not views or source_count < 2:
        return Confidence.LOW.value
    ranks = {Confidence.LOW.value: 1, Confidence.MEDIUM.value: 2, Confidence.HIGH.value: 3}
    best = max((ranks.get(view.confidence, 1) for view in views), default=1)
    if best >= 3 and source_count >= 3:
        return Confidence.HIGH.value
    return Confidence.MEDIUM.value
