from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def new_uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class Source(Base, TimestampMixin):
    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    institution_name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    website: Mapped[str] = mapped_column(String(700), nullable=False)
    category: Mapped[str] = mapped_column(String(100), default="research", nullable=False)
    trust_score: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    update_frequency: Mapped[str] = mapped_column(String(100), default="manual", nullable=False)
    adapter_type: Mapped[str] = mapped_column(String(100), default="manual", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    articles: Mapped[list["Article"]] = relationship(back_populates="source")


class Article(Base, TimestampMixin):
    __tablename__ = "articles"
    __table_args__ = (UniqueConstraint("source_id", "content_hash", name="uq_article_source_hash"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    author: Mapped[str | None] = mapped_column(String(255))
    publication_date: Mapped[date] = mapped_column(Date, nullable=False)
    url: Mapped[str | None] = mapped_column(String(1000))
    canonical_url: Mapped[str | None] = mapped_column(String(1000), index=True)
    source_reference: Mapped[str] = mapped_column(String(1000), nullable=False)
    language: Mapped[str] = mapped_column(String(20), default="ru", nullable=False)
    content_text: Mapped[str] = mapped_column(Text, nullable=False)
    excerpt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    fetch_status: Mapped[str] = mapped_column(String(50), nullable=False)
    processing_status: Mapped[str] = mapped_column(String(80), default="NEW", nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    source: Mapped[Source] = relationship(back_populates="articles")
    views: Mapped[list["ResearchView"]] = relationship(back_populates="article")


class ResearchView(Base, TimestampMixin):
    __tablename__ = "research_views"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    article_id: Mapped[str] = mapped_column(ForeignKey("articles.id"), nullable=False)
    institution: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    horizon: Mapped[str] = mapped_column(String(50), nullable=False)
    region: Mapped[str] = mapped_column(String(80), nullable=False)
    asset_class: Mapped[str] = mapped_column(String(120), nullable=False)
    asset_group: Mapped[str] = mapped_column(String(120), nullable=False)
    asset_segment: Mapped[str] = mapped_column(String(160), nullable=False)
    template_row_key: Mapped[str] = mapped_column(String(400), nullable=False, index=True)
    canonical_cell_id: Mapped[str] = mapped_column(String(120), default="", nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(30), nullable=False)
    position_score: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[str] = mapped_column(String(30), nullable=False)
    drivers: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    risks: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    catalysts: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    evidence_quotes: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    extraction_method: Mapped[str] = mapped_column(String(30), nullable=False)
    review_status: Mapped[str] = mapped_column(String(30), default="NEW", nullable=False)
    legacy_review_status: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    strict_review_status: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    schema_version: Mapped[str] = mapped_column(String(30), default="1.0", nullable=False)
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_until: Mapped[date | None] = mapped_column(Date)
    document_type: Mapped[str] = mapped_column(String(40), default="ASSET_OUTLOOK", nullable=False)
    investment_horizon: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(String(40), default="ACTIVE", nullable=False)
    superseded_by: Mapped[str] = mapped_column(String(36), default="", nullable=False)
    invalidation_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    last_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    freshness_weight: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    article: Mapped[Article] = relationship(back_populates="views")


class InvestmentThesisCandidate(Base, TimestampMixin):
    __tablename__ = "investment_thesis_candidates"
    __table_args__ = (UniqueConstraint("article_id", "exact_quote", "run_id", name="uq_thesis_article_quote_run"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    article_id: Mapped[str] = mapped_column(ForeignKey("articles.id"), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(80), default="", nullable=False, index=True)
    exact_quote: Mapped[str] = mapped_column(Text, nullable=False)
    quote_locator: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    context_before: Mapped[str] = mapped_column(Text, default="", nullable=False)
    context_after: Mapped[str] = mapped_column(Text, default="", nullable=False)
    document_type: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    explicit_asset_mentions: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    explicit_region_mentions: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    explicit_horizon: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    forecast_or_view: Mapped[str] = mapped_column(Text, default="", nullable=False)
    expected_market_effect: Mapped[str] = mapped_column(Text, default="", nullable=False)
    transmission_channel: Mapped[str] = mapped_column(Text, default="", nullable=False)
    extraction_confidence: Mapped[str] = mapped_column(String(30), default="LOW", nullable=False)
    stage_status: Mapped[str] = mapped_column(String(40), default="CANDIDATE", nullable=False)
    rejection_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    is_pilot: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class PilotResearchView(Base, TimestampMixin):
    __tablename__ = "pilot_research_views"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    article_id: Mapped[str] = mapped_column(ForeignKey("articles.id"), nullable=False, index=True)
    thesis_candidate_id: Mapped[str] = mapped_column(ForeignKey("investment_thesis_candidates.id"), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(80), default="", nullable=False, index=True)
    institution: Mapped[str] = mapped_column(String(255), nullable=False)
    horizon: Mapped[str] = mapped_column(String(50), nullable=False)
    region: Mapped[str] = mapped_column(String(80), nullable=False)
    asset_class: Mapped[str] = mapped_column(String(120), nullable=False)
    asset_group: Mapped[str] = mapped_column(String(120), nullable=False)
    asset_segment: Mapped[str] = mapped_column(String(160), nullable=False)
    template_row_key: Mapped[str] = mapped_column(String(400), nullable=False, index=True)
    canonical_cell_id: Mapped[str] = mapped_column(String(120), default="", nullable=False, index=True)
    internal_direction: Mapped[str] = mapped_column(String(40), nullable=False)
    direction: Mapped[str] = mapped_column(String(30), nullable=False)
    position_score: Mapped[int | None] = mapped_column(Integer)
    exact_quote: Mapped[str] = mapped_column(Text, nullable=False)
    quote_locator: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    transmission_logic: Mapped[str] = mapped_column(Text, nullable=False)
    financial_rationale: Mapped[str] = mapped_column(Text, nullable=False)
    fixed_income_effects: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    reviewer_checks: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    review_status: Mapped[str] = mapped_column(String(40), default="MANUAL_REVIEW_REQUIRED", nullable=False)
    rejection_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class ChangeLog(Base, TimestampMixin):
    __tablename__ = "change_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    previous_view_id: Mapped[str | None] = mapped_column(ForeignKey("research_views.id"))
    current_view_id: Mapped[str] = mapped_column(ForeignKey("research_views.id"), nullable=False)
    change_types: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    old_values: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    new_values: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    materiality: Mapped[str] = mapped_column(String(40), nullable=False)
    machine_confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    idea_state: Mapped[str] = mapped_column(String(40), nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class ScenarioCard(Base, TimestampMixin):
    __tablename__ = "scenario_cards"
    __table_args__ = (UniqueConstraint("linked_change_id", "scenario_type", name="uq_scenario_change_type"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    linked_change_id: Mapped[str | None] = mapped_column(ForeignKey("change_logs.id"))
    linked_view_id: Mapped[str | None] = mapped_column(ForeignKey("research_views.id"))
    scenario_type: Mapped[str] = mapped_column(String(30), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    assumptions: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    triggers: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    early_indicators: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    beneficiaries: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    vulnerable_assets: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    expected_reaction: Mapped[str] = mapped_column(Text, nullable=False)
    reversal_conditions: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    probability_band: Mapped[str] = mapped_column(String(40), default="MEDIUM", nullable=False)
    review_status: Mapped[str] = mapped_column(String(30), default="NEW", nullable=False)
    source_references: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class EvidenceObservation(Base, TimestampMixin):
    __tablename__ = "evidence_observations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    scenario_id: Mapped[str] = mapped_column(ForeignKey("scenario_cards.id"), nullable=False)
    indicator: Mapped[str] = mapped_column(String(255), nullable=False)
    observation_date: Mapped[date] = mapped_column(Date, nullable=False)
    expected: Mapped[str] = mapped_column(Text, nullable=False)
    actual: Mapped[str] = mapped_column(Text, nullable=False)
    surprise: Mapped[str] = mapped_column(Text, default="", nullable=False)
    market_implied: Mapped[str] = mapped_column(Text, default="", nullable=False)
    direction_for_scenario: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    importance: Mapped[str] = mapped_column(String(20), default="STANDARD", nullable=False)
    source_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    support_value: Mapped[int] = mapped_column(Integer, nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class ScenarioAssessment(Base, TimestampMixin):
    __tablename__ = "scenario_assessments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    scenario_id: Mapped[str] = mapped_column(ForeignKey("scenario_cards.id"), nullable=False, unique=True)
    evidence_status: Mapped[str] = mapped_column(String(40), nullable=False)
    weighted_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    pricing_status: Mapped[str] = mapped_column(String(40), nullable=False)
    pricing_metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    assessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class ShiftSignal(Base, TimestampMixin):
    __tablename__ = "shift_signals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    change_id: Mapped[str | None] = mapped_column(ForeignKey("change_logs.id"))
    asset: Mapped[str] = mapped_column(String(200), nullable=False)
    template_row_key: Mapped[str] = mapped_column(String(400), nullable=False, index=True)
    canonical_cell_id: Mapped[str] = mapped_column(String(120), default="", nullable=False, index=True)
    region: Mapped[str] = mapped_column(String(80), nullable=False)
    direction: Mapped[str] = mapped_column(String(30), nullable=False)
    suggested_strength: Mapped[int] = mapped_column(Integer, nullable=False)
    what_changed: Mapped[str] = mapped_column(Text, nullable=False)
    dominant_scenario: Mapped[str] = mapped_column(String(40), nullable=False)
    why_now: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_status: Mapped[str] = mapped_column(String(40), nullable=False)
    pricing_status: Mapped[str] = mapped_column(String(40), nullable=False)
    transmission_chain: Mapped[str] = mapped_column(Text, nullable=False)
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    veto: Mapped[str] = mapped_column(Text, nullable=False)
    key_risk: Mapped[str] = mapped_column(Text, nullable=False)
    next_review_date: Mapped[date] = mapped_column(Date, nullable=False)
    confidence: Mapped[str] = mapped_column(String(30), nullable=False)
    source_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    source_urls: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    analyst_status: Mapped[str] = mapped_column(String(30), default="NEW", nullable=False)
    analyst_comment: Mapped[str] = mapped_column(Text, default="", nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class MatrixScore(Base, TimestampMixin):
    __tablename__ = "matrix_scores"
    __table_args__ = (UniqueConstraint("template_row_key", "region", "is_demo", name="uq_matrix_row_region_mode"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    template_row_key: Mapped[str] = mapped_column(String(400), nullable=False)
    canonical_cell_id: Mapped[str] = mapped_column(String(120), default="", nullable=False, index=True)
    region: Mapped[str] = mapped_column(String(80), nullable=False)
    suggested_score: Mapped[int | None] = mapped_column(Integer)
    approved_score: Mapped[int | None] = mapped_column(Integer)
    override_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    calculation_details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    signal_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    coverage_status: Mapped[str] = mapped_column(String(40), default="NO_DATA", nullable=False)
    source_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    publication_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    publication_dates: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    supporting_evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    contradicting_evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    confidence: Mapped[str] = mapped_column(String(30), default="LOW", nullable=False)
    freshness_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class SourceIndependenceGroup(Base, TimestampMixin):
    __tablename__ = "source_independence_groups"
    __table_args__ = (UniqueConstraint("source_id", name="uq_source_independence_source"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.id"), index=True)
    provider: Mapped[str] = mapped_column(String(255), nullable=False)
    group_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    domain: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    rationale: Mapped[str] = mapped_column(Text, default="", nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class MaeSnapshot(Base, TimestampMixin):
    __tablename__ = "mae_snapshots"
    __table_args__ = (UniqueConstraint("snapshot_date", "is_demo", name="uq_mae_snapshot_date_mode"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    lookback_days: Mapped[int] = mapped_column(Integer, default=90, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="FINAL", nullable=False)
    run_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    coverage_report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    immutable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class FinancialReleaseRecord(Base, TimestampMixin):
    __tablename__ = "financial_release_records"
    __table_args__ = (UniqueConstraint("subject_type", "subject_id", name="uq_financial_release_subject"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    subject_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    subject_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    technical_status: Mapped[str] = mapped_column(String(40), default="NOT_VALIDATED", nullable=False)
    financial_status: Mapped[str] = mapped_column(String(40), default="NOT_REVIEWED", nullable=False)
    analyst_review_status: Mapped[str] = mapped_column(String(40), default="NOT_REVIEWED", nullable=False)
    release_status: Mapped[str] = mapped_column(String(40), default="QUARANTINED", nullable=False)
    reviewer_actor_type: Mapped[str] = mapped_column(String(40), default="NONE", nullable=False)
    reviewer_identity: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    rulebook_version: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    review_version: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    baseline_manifest_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    source_content_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    candidate_content_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    artifact_manifest_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    required_artifacts_valid: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    unresolved_manual_review_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rejection_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class FinancialReviewDecision(Base, TimestampMixin):
    __tablename__ = "financial_review_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    release_record_id: Mapped[str] = mapped_column(
        ForeignKey("financial_release_records.id"), nullable=False, index=True
    )
    previous_status: Mapped[str] = mapped_column(String(40), nullable=False)
    new_status: Mapped[str] = mapped_column(String(40), nullable=False)
    reviewer_actor_type: Mapped[str] = mapped_column(String(40), nullable=False)
    reviewer_identity: Mapped[str] = mapped_column(String(160), nullable=False)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decision_reason: Mapped[str] = mapped_column(Text, nullable=False)
    rulebook_version: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    review_version: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    source_content_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    decision_content_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class FinancialCandidateCellRecord(Base, TimestampMixin):
    """Candidate-only score persistence contract; no current/release pointer lives here."""

    __tablename__ = "financial_candidate_cells"
    __table_args__ = (
        UniqueConstraint("candidate_id", "canonical_cell_id", name="uq_financial_candidate_cell"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    candidate_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    canonical_cell_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    template_row_key: Mapped[str] = mapped_column(String(400), nullable=False)
    region: Mapped[str] = mapped_column(String(80), nullable=False)
    applicability: Mapped[str] = mapped_column(String(40), nullable=False)
    score_status: Mapped[str] = mapped_column(String(40), nullable=False)
    score: Mapped[int | None] = mapped_column(Integer)
    direction: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    limitation_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    rejection_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    carry_forward_source_candidate_id: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    carry_forward_source_cell_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    carry_forward_age_days: Mapped[int | None] = mapped_column(Integer)
    trace: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    cell_business_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class AutonomousReleaseRecord(Base, TimestampMixin):
    """Governance record for autonomous releases; it contains no approval actor."""

    __tablename__ = "autonomous_release_records"
    __table_args__ = (
        UniqueConstraint("subject_type", "subject_id", name="uq_autonomous_release_subject"),
        UniqueConstraint("candidate_id", "source_manifest_hash", name="uq_autonomous_release_content"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    subject_type: Mapped[str] = mapped_column(
        String(40), default="AUTONOMOUS_MAE_CANDIDATE", nullable=False, index=True
    )
    subject_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    candidate_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    technical_status: Mapped[str] = mapped_column(String(40), nullable=False)
    model_validation_status: Mapped[str] = mapped_column(String(40), nullable=False)
    release_status: Mapped[str] = mapped_column(String(40), default="DRAFT", nullable=False)
    human_review_status: Mapped[str] = mapped_column(
        String(40), default="NOT_REQUESTED", nullable=False
    )
    matrix_rows: Mapped[int] = mapped_column(Integer, default=19, nullable=False)
    matrix_regions: Mapped[int] = mapped_column(Integer, default=6, nullable=False)
    applicable_score_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    not_applicable_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    baseline_manifest_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    source_manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    required_artifacts_valid: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    critical_error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    model_version: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    validation_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class AtomicReleaseRecord(Base, TimestampMixin):
    __tablename__ = "atomic_release_records"
    __table_args__ = (
        UniqueConstraint("channel", "candidate_id", "source_manifest_hash", name="uq_atomic_release_content"),
    )

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    channel: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    candidate_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    governance_record_id: Mapped[str] = mapped_column(String(36), nullable=False)
    baseline_manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    release_directory: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="CURRENT", nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class AtomicReleasePointer(Base, TimestampMixin):
    __tablename__ = "atomic_release_pointers"

    channel: Mapped[str] = mapped_column(String(80), primary_key=True)
    release_id: Mapped[str] = mapped_column(
        ForeignKey("atomic_release_records.id"), nullable=False, unique=True
    )
    artifact_manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    release_directory: Mapped[str] = mapped_column(String(500), nullable=False)


class ExternalDataCache(Base, TimestampMixin):
    __tablename__ = "external_data_cache"
    __table_args__ = (UniqueConstraint("cache_key", name="uq_external_data_cache_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    cache_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(120), nullable=False)
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)


class MaeComponentSnapshot(Base, TimestampMixin):
    __tablename__ = "mae_component_snapshots"
    __table_args__ = (UniqueConstraint("snapshot_date", "formula_version", "is_demo", name="uq_component_snapshot_date_formula_mode"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), default="FINAL", nullable=False)
    formula_version: Mapped[str] = mapped_column(String(40), default="component_v1", nullable=False)
    exposure_map_version: Mapped[str] = mapped_column(String(40), default="exposure_v1", nullable=False)
    applicability_map_version: Mapped[str] = mapped_column(String(40), default="applicability_v1", nullable=False)
    run_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    coverage_report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    immutable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class MaeComponentCell(Base, TimestampMixin):
    __tablename__ = "mae_component_cells"
    __table_args__ = (UniqueConstraint("snapshot_id", "canonical_cell_id", name="uq_component_snapshot_cell"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("mae_component_snapshots.id"), nullable=False, index=True)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    canonical_cell_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    template_row_key: Mapped[str] = mapped_column(String(400), nullable=False, index=True)
    region: Mapped[str] = mapped_column(String(80), nullable=False)
    asset_bucket: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    benchmark: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    applicability: Mapped[str] = mapped_column(String(40), nullable=False)
    research_score: Mapped[int | None] = mapped_column(Integer)
    data_score: Mapped[int | None] = mapped_column(Integer)
    market_score: Mapped[int | None] = mapped_column(Integer)
    composite_score: Mapped[int | None] = mapped_column(Integer)
    previous_composite_score: Mapped[int | None] = mapped_column(Integer)
    score_change: Mapped[int | None] = mapped_column(Integer)
    divergence_status: Mapped[str] = mapped_column(String(40), default="INSUFFICIENT", nullable=False)
    research_coverage: Mapped[str] = mapped_column(String(40), default="NONE", nullable=False)
    component_coverage: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    source_details: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    factor_details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    exposure_path: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    formula_version: Mapped[str] = mapped_column(String(40), default="component_v1", nullable=False)
    validation_status: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    validation_comment: Mapped[str] = mapped_column(Text, default="", nullable=False)
    explanation: Mapped[str] = mapped_column(Text, default="", nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class MaeSnapshotCell(Base, TimestampMixin):
    __tablename__ = "mae_snapshot_cells"
    __table_args__ = (UniqueConstraint("snapshot_id", "canonical_cell_id", name="uq_mae_snapshot_cell"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("mae_snapshots.id"), nullable=False, index=True)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    canonical_cell_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    template_row_key: Mapped[str] = mapped_column(String(400), nullable=False, index=True)
    region: Mapped[str] = mapped_column(String(80), nullable=False)
    asset: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    applicability: Mapped[str] = mapped_column(String(40), nullable=False)
    mae_score: Mapped[int | None] = mapped_column(Integer)
    previous_snapshot_score: Mapped[int | None] = mapped_column(Integer)
    score_change: Mapped[int | None] = mapped_column(Integer)
    thesis: Mapped[str] = mapped_column(Text, default="", nullable=False)
    driver: Mapped[str] = mapped_column(Text, default="", nullable=False)
    previous_thesis: Mapped[str] = mapped_column(Text, default="", nullable=False)
    previous_driver: Mapped[str] = mapped_column(Text, default="", nullable=False)
    evidence_status: Mapped[str] = mapped_column(String(40), default="MIXED", nullable=False)
    confidence: Mapped[str] = mapped_column(String(30), default="MEDIUM", nullable=False)
    carry_forward: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    scenario_link: Mapped[str] = mapped_column(String(80), default="Base", nullable=False)
    change_status: Mapped[str] = mapped_column(String(60), default="UNCHANGED", nullable=False)
    change_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    coverage_status: Mapped[str] = mapped_column(String(40), nullable=False)
    source_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    independent_source_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    evidence_item_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    disagreement: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, default="", nullable=False)
    scenario_adjustment: Mapped[int | None] = mapped_column(Integer)
    scenario_adjustment_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class EvidenceItem(Base, TimestampMixin):
    __tablename__ = "evidence_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("mae_snapshots.id"), nullable=False, index=True)
    snapshot_cell_id: Mapped[str | None] = mapped_column(ForeignKey("mae_snapshot_cells.id"), index=True)
    article_id: Mapped[str | None] = mapped_column(ForeignKey("articles.id"), index=True)
    research_view_id: Mapped[str | None] = mapped_column(ForeignKey("research_views.id"), index=True)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_group: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    report_title: Mapped[str] = mapped_column(String(500), nullable=False)
    publication_date: Mapped[date] = mapped_column(Date, nullable=False)
    ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    url: Mapped[str] = mapped_column(String(1000), default="", nullable=False)
    exact_quote: Mapped[str] = mapped_column(Text, default="", nullable=False)
    affected_region: Mapped[str] = mapped_column(String(80), nullable=False)
    affected_asset_segment: Mapped[str] = mapped_column(String(160), nullable=False)
    template_row_key: Mapped[str] = mapped_column(String(400), nullable=False, index=True)
    canonical_cell_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    extracted_stance: Mapped[str] = mapped_column(String(40), nullable=False)
    source_specificity: Mapped[str] = mapped_column(String(40), nullable=False)
    source_independence_group: Mapped[str] = mapped_column(String(255), nullable=False)
    source_class: Mapped[str] = mapped_column(String(60), default="ALLOWLIST", nullable=False)
    relevance_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    related_indicator: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    actual_value: Mapped[str] = mapped_column(Text, default="", nullable=False)
    expected_value: Mapped[str] = mapped_column(Text, default="", nullable=False)
    market_confirmation: Mapped[str] = mapped_column(Text, default="", nullable=False)
    conflict: Mapped[str] = mapped_column(Text, default="", nullable=False)
    evidence_status: Mapped[str] = mapped_column(String(40), default="MIXED", nullable=False)
    review_status: Mapped[str] = mapped_column(String(40), default="PASS", nullable=False)
    carry_forward: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class MarketScenario(Base, TimestampMixin):
    __tablename__ = "market_scenarios"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    snapshot_date: Mapped[date | None] = mapped_column(Date, index=True)
    topic: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    region: Mapped[str] = mapped_column(String(80), nullable=False)
    primary_asset: Mapped[str] = mapped_column(String(400), nullable=False)
    scenario_type: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="REQUIRES_CONFIRMATION", nullable=False)
    probability_band: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    narrative: Mapped[str] = mapped_column(Text, default="", nullable=False)
    causal_chain: Mapped[str] = mapped_column(Text, default="", nullable=False)
    macro_drivers: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    triggers: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    indicators: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    invalidation_conditions: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    beneficiaries: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    vulnerable_assets: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    affected_cells: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    expected_reaction_by_cell: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)
    last_review_date: Mapped[date | None] = mapped_column(Date)
    source_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class ScenarioAdjustment(Base, TimestampMixin):
    __tablename__ = "scenario_adjustments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("mae_snapshots.id"), index=True)
    scenario_id: Mapped[str | None] = mapped_column(ForeignKey("market_scenarios.id"), index=True)
    template_row_key: Mapped[str] = mapped_column(String(400), nullable=False, index=True)
    canonical_cell_id: Mapped[str] = mapped_column(String(120), default="", nullable=False, index=True)
    region: Mapped[str] = mapped_column(String(80), nullable=False)
    adjustment: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="OBSERVATION", nullable=False)
    trigger: Mapped[str] = mapped_column(Text, default="", nullable=False)
    invalidation_condition: Mapped[str] = mapped_column(Text, default="", nullable=False)
    independent_confirmation_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    evidence_item_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, default="", nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class BaselineSnapshot(Base, TimestampMixin):
    __tablename__ = "baseline_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    source_file: Mapped[str] = mapped_column(String(700), nullable=False)
    source_sheet: Mapped[str] = mapped_column(String(120), default="ex", nullable=False)
    baseline_date: Mapped[date | None] = mapped_column(Date)
    baseline_date_status: Mapped[str] = mapped_column(String(40), default="UNKNOWN", nullable=False)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    immutable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)


class BaselineScore(Base, TimestampMixin):
    __tablename__ = "baseline_scores"
    __table_args__ = (UniqueConstraint("snapshot_id", "template_row_key", "region", name="uq_baseline_snapshot_cell"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("baseline_snapshots.id"), nullable=False, index=True)
    template_row_key: Mapped[str] = mapped_column(String(400), nullable=False, index=True)
    canonical_cell_id: Mapped[str] = mapped_column(String(120), default="", nullable=False, index=True)
    region: Mapped[str] = mapped_column(String(80), nullable=False)
    baseline_score: Mapped[int] = mapped_column(Integer, nullable=False)
    source_row: Mapped[int] = mapped_column(Integer, nullable=False)
    source_column: Mapped[str] = mapped_column(String(8), nullable=False)
    deprecated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class MatrixOverrideLog(Base, TimestampMixin):
    __tablename__ = "matrix_override_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    matrix_score_id: Mapped[str | None] = mapped_column(ForeignKey("matrix_scores.id"))
    template_row_key: Mapped[str] = mapped_column(String(400), nullable=False)
    region: Mapped[str] = mapped_column(String(80), nullable=False)
    previous_score: Mapped[int | None] = mapped_column(Integer)
    new_score: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    reviewer_label: Mapped[str] = mapped_column(String(120), default="Analyst", nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class ReviewRun(Base, TimestampMixin):
    __tablename__ = "review_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    role: Mapped[str] = mapped_column(String(80), nullable=False)
    verdict: Mapped[str] = mapped_column(String(80), nullable=False)
    overall_quality: Mapped[int | None] = mapped_column(Integer)
    source_quality: Mapped[int | None] = mapped_column(Integer)
    evidence_to_score_logic: Mapped[int | None] = mapped_column(Integer)
    cross_matrix_consistency: Mapped[int | None] = mapped_column(Integer)
    practical_usefulness: Mapped[int | None] = mapped_column(Integer)
    critical_findings: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    cell_findings: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    run_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class UpdateJob(Base, TimestampMixin):
    __tablename__ = "update_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    requested_mode: Mapped[str] = mapped_column(String(80), nullable=False)
    total_sources: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    succeeded: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    details: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)


class LLMCallLog(Base, TimestampMixin):
    __tablename__ = "llm_call_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    provider: Mapped[str] = mapped_column(String(40), default="openai", nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    operation: Mapped[str] = mapped_column(String(120), nullable=False)
    run_id: Mapped[str] = mapped_column(String(80), default="", nullable=False, index=True)
    session_key: Mapped[str] = mapped_column(String(120), default="", nullable=False, index=True)
    article_id: Mapped[str | None] = mapped_column(String(36), index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_code: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    message: Mapped[str] = mapped_column(Text, default="", nullable=False)


class AnalystReview(Base, TimestampMixin):
    __tablename__ = "analyst_reviews"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False)
    previous_status: Mapped[str] = mapped_column(String(40), nullable=False)
    new_status: Mapped[str] = mapped_column(String(40), nullable=False)
    comment: Mapped[str] = mapped_column(Text, nullable=False)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    reviewer_label: Mapped[str] = mapped_column(String(120), default="Analyst", nullable=False)


class AppLog(Base, TimestampMixin):
    __tablename__ = "app_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    level: Mapped[str] = mapped_column(String(20), nullable=False)
    event: Mapped[str] = mapped_column(String(120), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
