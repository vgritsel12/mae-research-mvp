from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.enums import (
    AnalystReviewStatus,
    AutonomousReleaseStatus,
    AutonomousTechnicalStatus,
    Confidence,
    Direction,
    ExtractionMethod,
    FinancialReleaseStatus,
    FinancialValidationStatus,
    HumanReviewStatus,
    ModelValidationStatus,
    ReviewActorType,
    ScoreStatus,
    ScenarioType,
    TechnicalValidationStatus,
)


class EvidenceQuote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quote: str
    locator: str = "manual/demo"
    source_url: str | None = None


class SourceReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_reference: str
    note: str = ""


class ResearchViewDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    institution: str
    horizon: str = "MEDIUM_3_12M"
    region: str
    asset_class: str
    asset_group: str
    asset_segment: str
    direction: Direction
    position_score: int | None = None
    confidence: Confidence
    drivers: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    catalysts: list[str] = Field(default_factory=list)
    evidence_quotes: list[EvidenceQuote] = Field(default_factory=list)
    extraction_method: ExtractionMethod = ExtractionMethod.RULE_BASED

    @field_validator("position_score")
    @classmethod
    def score_range(cls, value: int | None) -> int | None:
        if value is not None and not -3 <= value <= 3:
            raise ValueError("position_score must be between -3 and +3")
        return value


class ScenarioDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_type: ScenarioType
    title: str
    description: str
    assumptions: list[str]
    triggers: list[str]
    early_indicators: list[str] = Field(default_factory=list)
    beneficiaries: list[str] = Field(default_factory=list)
    vulnerable_assets: list[str] = Field(default_factory=list)
    expected_reaction: str
    reversal_conditions: list[str]
    probability_band: str = "MEDIUM"
    source_references: list[SourceReference] = Field(default_factory=list)

    @field_validator("assumptions", "triggers", "reversal_conditions")
    @classmethod
    def require_non_empty(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("scenario card requires observable assumptions, triggers and reversal conditions")
        return value


class ChangeExplanation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    explanation: str
    change_types: list[str]


class ArticleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    source_name: str
    publication_date: date
    url: str | None = None
    content_text: str
    is_demo: bool = False


class ResearchViewsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    views: list[ResearchViewDraft]


class ScenarioResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenarios: list[ScenarioDraft]


class SignalExplanationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    explanation: str


class ThemeDocumentView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    provider: str = Field(max_length=120)
    title: str = Field(max_length=220)
    date: date
    url: str
    document_type: str = Field(max_length=120)
    short_extracted_view: str = Field(max_length=240)
    relevant_regions: list[str] = Field(max_length=6)
    relevant_asset_classes: list[str] = Field(max_length=5)
    drivers: list[str] = Field(max_length=4)
    risks: list[str] = Field(max_length=4)


class MarketTheme(BaseModel):
    model_config = ConfigDict(extra="forbid")

    theme_id: str
    title: str = Field(max_length=140)
    direction: str = Field(max_length=40)
    affected_regions: list[str] = Field(max_length=6)
    affected_asset_classes: list[str] = Field(max_length=5)
    implication_6_12m: str = Field(max_length=240)
    supporting_source_ids: list[str] = Field(max_length=5)
    contradicting_source_ids: list[str] = Field(default_factory=list, max_length=5)
    confidence: Confidence
    invalidation_condition: str = Field(max_length=220)


class MarketSynthesisResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    documents: list[ThemeDocumentView]
    themes: list[MarketTheme]

    @model_validator(mode="after")
    def validate_theme_count(self) -> "MarketSynthesisResponse":
        if not 8 <= len(self.themes) <= 12:
            raise ValueError("market synthesis must contain 8-12 themes")
        return self


class ThemeMatrixCellDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_cell_id: str
    score: int = Field(ge=-3, le=3)
    confidence: Confidence
    thesis: str = Field(max_length=140)
    supporting_theme_ids: list[str] = Field(max_length=2)
    supporting_source_ids: list[str] = Field(max_length=3)
    main_risk: str = Field(max_length=100)
    invalidation_condition: str = Field(max_length=120)


class ThemeScenarioDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: ScenarioType
    scenario_name: str = Field(max_length=90)
    narrative: str = Field(max_length=220)
    probability_band: str = Field(max_length=40)
    causal_chain: str = Field(max_length=180)
    trigger: str = Field(max_length=120)
    veto: str = Field(max_length=120)
    material_affected_cells: list[str] = Field(max_length=10)


class ThemeMatrixSynthesisResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cells: list[ThemeMatrixCellDraft]
    scenarios: list[ThemeScenarioDraft]


class FinancialReleaseState(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    subject_type: str
    subject_id: str
    snapshot_date: date
    technical_status: TechnicalValidationStatus = TechnicalValidationStatus.NOT_VALIDATED
    financial_status: FinancialValidationStatus = FinancialValidationStatus.NOT_REVIEWED
    analyst_review_status: AnalystReviewStatus = AnalystReviewStatus.NOT_REVIEWED
    release_status: FinancialReleaseStatus = FinancialReleaseStatus.QUARANTINED
    reviewer_actor_type: ReviewActorType = ReviewActorType.NONE
    reviewer_identity: str = ""
    reviewed_at: datetime | None = None
    decision_reason: str = ""
    rulebook_version: str = ""
    review_version: str = ""
    baseline_manifest_hash: str = ""
    source_content_hash: str = ""
    candidate_content_hash: str = ""
    artifact_manifest_hash: str = ""
    required_artifacts_valid: bool = False
    unresolved_manual_review_count: int = Field(default=0, ge=0)
    rejection_count: int = Field(default=0, ge=0)
    is_demo: bool = False


class ReleaseGateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    eligible: bool
    blockers: list[str] = Field(default_factory=list)


class AutonomousReleaseState(BaseModel):
    """Independent governance state for a machine-generated market release."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    subject_type: str = "AUTONOMOUS_MAE_CANDIDATE"
    subject_id: str
    candidate_id: str
    snapshot_date: date
    technical_status: AutonomousTechnicalStatus
    model_validation_status: ModelValidationStatus
    release_status: AutonomousReleaseStatus = AutonomousReleaseStatus.DRAFT
    human_review_status: HumanReviewStatus = HumanReviewStatus.NOT_REQUESTED
    matrix_rows: int = Field(default=19, ge=0)
    matrix_regions: int = Field(default=6, ge=0)
    applicable_score_count: int = Field(default=0, ge=0)
    not_applicable_count: int = Field(default=0, ge=0)
    baseline_manifest_hash: str = ""
    source_manifest_hash: str = ""
    candidate_content_hash: str = ""
    artifact_manifest_hash: str = ""
    required_artifacts_valid: bool = False
    critical_error_count: int = Field(default=0, ge=0)
    model_version: str = ""
    validation_summary: dict[str, Any] = Field(default_factory=dict)
    is_demo: bool = False


class AutonomousGateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    eligible: bool
    blockers: list[str] = Field(default_factory=list)


class FinancialCandidateCell(BaseModel):
    """Strict candidate/export contract; it does not authorize production release."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    run_id: str
    snapshot_date: date
    canonical_cell_id: str
    template_row_key: str
    region: str
    applicability: str
    score_status: ScoreStatus
    score: int | None = None
    direction: Direction | None = None
    limitation_reason: str = ""
    rejection_reason: str = ""
    carry_forward_source_candidate_id: str = ""
    carry_forward_source_cell_hash: str = ""
    carry_forward_age_days: int | None = Field(default=None, ge=0)
    trace: dict[str, Any] = Field(default_factory=dict)
    cell_business_hash: str = ""

    @model_validator(mode="after")
    def validate_score_semantics(self) -> "FinancialCandidateCell":
        scored = self.score_status in {ScoreStatus.ACTIVE, ScoreStatus.CARRY_FORWARD}
        if scored and (isinstance(self.score, bool) or not isinstance(self.score, int)):
            raise ValueError("ACTIVE/CARRY_FORWARD requires an integer score")
        if not scored and self.score is not None:
            raise ValueError("non-scored status requires a null score")
        if self.score == 0 and not bool(self.trace.get("explicit_neutral")):
            raise ValueError("score zero requires an admitted explicit-neutral gate")
        if self.score_status == ScoreStatus.CARRY_FORWARD:
            if not self.carry_forward_source_candidate_id or not self.carry_forward_source_cell_hash:
                raise ValueError("CARRY_FORWARD requires immutable source lineage")
            if self.carry_forward_age_days is None:
                raise ValueError("CARRY_FORWARD requires lineage age")
        elif any(
            value not in ("", None)
            for value in (
                self.carry_forward_source_candidate_id,
                self.carry_forward_source_cell_hash,
                self.carry_forward_age_days,
            )
        ):
            raise ValueError("carry-forward lineage is forbidden for other statuses")
        if self.score_status in {ScoreStatus.INSUFFICIENT_DATA, ScoreStatus.NOT_APPLICABLE} and not self.limitation_reason:
            raise ValueError("insufficient/not-applicable status requires a limitation reason")
        if self.score_status == ScoreStatus.REJECTED and not self.rejection_reason:
            raise ValueError("REJECTED status requires a rejection reason")
        return self
