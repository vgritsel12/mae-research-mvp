from __future__ import annotations

import csv
import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.enums import AutonomousEvidenceMode, Confidence
from app.services.normalization import CanonicalCell, canonical_cell_registry


DEFAULT_PREVIOUS_SCORES = Path("outputs/mae_full_latest_scores.csv")
ALLOWED_SCHEMES = ("https://", "http://")


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=lambda item: item.isoformat() if isinstance(item, date) else str(item),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AutonomousSourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    title: str
    publication_date: date
    url: str
    content_hash: str = ""
    source_id: str = ""

    @field_validator("provider", "title", "url")
    @classmethod
    def non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source fields must not be blank")
        return value.strip()


class AutonomousSignal(BaseModel):
    """Validated material signal supplied by extraction/mapping stages."""

    model_config = ConfigDict(extra="forbid")

    canonical_cell_id: str
    mode: AutonomousEvidenceMode
    proposed_score: int = Field(ge=-3, le=3)
    confidence: Confidence
    sources: list[AutonomousSourceRef]
    reasoning: str
    invalidation: str
    transmission_chain: list[str] = Field(default_factory=list)
    registered_inputs: list[str] = Field(default_factory=list)
    explicit_balanced_conclusion: bool = False
    material: bool = True

    @model_validator(mode="after")
    def validate_mode_contract(self) -> "AutonomousSignal":
        if self.mode == AutonomousEvidenceMode.CARRY_FORWARD:
            raise ValueError("CARRY_FORWARD is generated only by the no-material-signal rule")
        if not self.reasoning.strip() or not self.invalidation.strip():
            raise ValueError("reasoning and invalidation conditions are required")
        if self.mode == AutonomousEvidenceMode.DIRECT and len(self.sources) < 1:
            raise ValueError("DIRECT requires at least one source")
        if self.mode == AutonomousEvidenceMode.COMPOSITE and len(self.sources) < 2:
            raise ValueError("COMPOSITE requires at least two sources")
        if self.mode == AutonomousEvidenceMode.MODEL_INFERRED:
            if not self.transmission_chain or not self.registered_inputs:
                raise ValueError("MODEL_INFERRED requires a transmission chain and registered inputs")
            if self.confidence == Confidence.HIGH:
                raise ValueError("MODEL_INFERRED confidence is capped below HIGH")
        if self.proposed_score == 0 and not self.explicit_balanced_conclusion:
            raise ValueError("a fresh zero requires an explicit balanced conclusion")
        return self


class AutonomousMatrixCell(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_cell_id: str
    row_index: int
    asset_class: str
    asset_group: str
    asset_segment: str
    region: str
    applicability: Literal["APPLICABLE", "NOT_APPLICABLE"]
    current: int | None
    previous: int | None
    delta: int | None
    mode: AutonomousEvidenceMode | None
    confidence: Confidence | None
    sources: list[AutonomousSourceRef] = Field(default_factory=list)
    reasoning: str
    invalidation: str
    transmission_chain: list[str] = Field(default_factory=list)
    registered_inputs: list[str] = Field(default_factory=list)
    previous_snapshot_date: date
    assessment_date: date
    business_hash: str


class AutonomousMatrixResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_date: date
    previous_snapshot_date: date
    cells: list[AutonomousMatrixCell]
    applicable_score_count: int
    not_applicable_count: int
    mode_counts: dict[str, int]
    business_hash: str


def load_previous_official_scores(
    path: Path = DEFAULT_PREVIOUS_SCORES,
) -> tuple[date, dict[str, int | None]]:
    """Bind the official 114-row artifact to the immutable canonical template order."""

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    registry = canonical_cell_registry()
    if len(rows) != len(registry):
        raise ValueError(f"previous matrix geometry mismatch: {len(rows)} != {len(registry)}")
    snapshot_dates = {row.get("snapshot_date", "")[:10] for row in rows}
    if len(snapshot_dates) != 1 or "" in snapshot_dates:
        raise ValueError("previous matrix must have one valid snapshot date")
    result: dict[str, int | None] = {}
    for canonical, row in zip(registry, rows, strict=True):
        if row.get("geography") != canonical.region:
            raise ValueError(
                f"previous matrix order mismatch for {canonical.canonical_cell_id}: "
                f"{row.get('geography')} != {canonical.region}"
            )
        applicable = row.get("applicable", "").casefold() == "true"
        if applicable != (canonical.applicability == "APPLICABLE"):
            raise ValueError(f"previous applicability mismatch for {canonical.canonical_cell_id}")
        raw = row.get("score", "").strip()
        if applicable:
            if not raw:
                raise ValueError(f"missing previous score for {canonical.canonical_cell_id}")
            score = int(raw)
            if not -3 <= score <= 3:
                raise ValueError(f"previous score out of range for {canonical.canonical_cell_id}")
            result[canonical.canonical_cell_id] = score
        else:
            if raw:
                raise ValueError(f"N/A cell carries a score: {canonical.canonical_cell_id}")
            result[canonical.canonical_cell_id] = None
    return date.fromisoformat(next(iter(snapshot_dates))), result


def build_autonomous_matrix(
    *,
    snapshot_date: date,
    previous_snapshot_date: date,
    previous_scores: dict[str, int | None],
    signals: list[AutonomousSignal] | None = None,
    registry: list[CanonicalCell] | None = None,
    infer_missing: bool = False,
) -> AutonomousMatrixResult:
    registry = registry or canonical_cell_registry()
    signal_map = {signal.canonical_cell_id: signal for signal in (signals or []) if signal.material}
    if len(signal_map) != len([signal for signal in (signals or []) if signal.material]):
        raise ValueError("multiple material signals target the same canonical cell")
    canonical_ids = {cell.canonical_cell_id for cell in registry}
    unknown = sorted(set(signal_map) - canonical_ids)
    if unknown:
        raise ValueError(f"signals target unknown canonical cells: {unknown}")
    if snapshot_date < previous_snapshot_date:
        raise ValueError("snapshot date cannot predate the previous official snapshot")

    records: list[AutonomousMatrixCell] = []
    for canonical in registry:
        previous = previous_scores.get(canonical.canonical_cell_id)
        if canonical.canonical_cell_id not in previous_scores:
            raise ValueError(f"previous score missing for {canonical.canonical_cell_id}")
        if canonical.applicability == "NOT_APPLICABLE":
            if previous is not None:
                raise ValueError(f"N/A cell has previous score: {canonical.canonical_cell_id}")
            if canonical.canonical_cell_id in signal_map:
                raise ValueError(f"signal cannot target N/A cell: {canonical.canonical_cell_id}")
            values = _cell_values(
                canonical,
                current=None,
                previous=None,
                mode=None,
                confidence=None,
                sources=[],
                reasoning="Not applicable under the signed canonical template.",
                invalidation="Applicability changes require a versioned template decision.",
                transmission_chain=[],
                registered_inputs=[],
                previous_snapshot_date=previous_snapshot_date,
                snapshot_date=snapshot_date,
            )
        else:
            if isinstance(previous, bool) or not isinstance(previous, int):
                raise ValueError(f"applicable cell lacks integer previous score: {canonical.canonical_cell_id}")
            signal = signal_map.get(canonical.canonical_cell_id)
            if signal is None:
                if infer_missing:
                    inferred = _model_inferred_signal(canonical, snapshot_date)
                    values = _cell_values(
                        canonical,
                        current=inferred.proposed_score,
                        previous=previous,
                        mode=inferred.mode,
                        confidence=inferred.confidence,
                        sources=inferred.sources,
                        reasoning=inferred.reasoning,
                        invalidation=inferred.invalidation,
                        transmission_chain=inferred.transmission_chain,
                        registered_inputs=inferred.registered_inputs,
                        previous_snapshot_date=previous_snapshot_date,
                        snapshot_date=snapshot_date,
                    )
                else:
                    values = _cell_values(
                        canonical,
                        current=previous,
                        previous=previous,
                        mode=AutonomousEvidenceMode.CARRY_FORWARD,
                        confidence=Confidence.MEDIUM,
                        sources=[],
                        reasoning=(
                            "No material signal passed mapping and validation; the previous committed "
                            "assessment is retained without manufacturing a neutral score."
                        ),
                        invalidation="Reassess when a dated material signal passes the canonical mapping gate.",
                        transmission_chain=[],
                        registered_inputs=[],
                        previous_snapshot_date=previous_snapshot_date,
                        snapshot_date=snapshot_date,
                    )
            else:
                _validate_signal_dates_and_urls(signal, snapshot_date)
                values = _cell_values(
                    canonical,
                    current=max(-3, min(3, signal.proposed_score)),
                    previous=previous,
                    mode=signal.mode,
                    confidence=signal.confidence,
                    sources=signal.sources,
                    reasoning=signal.reasoning.strip(),
                    invalidation=signal.invalidation.strip(),
                    transmission_chain=signal.transmission_chain,
                    registered_inputs=signal.registered_inputs,
                    previous_snapshot_date=previous_snapshot_date,
                    snapshot_date=snapshot_date,
                )
        values["business_hash"] = _canonical_hash(values)
        records.append(AutonomousMatrixCell.model_validate(values))

    result_values = {
        "snapshot_date": snapshot_date,
        "previous_snapshot_date": previous_snapshot_date,
        "cells": [record.model_dump(mode="json") for record in records],
        "applicable_score_count": sum(record.current is not None for record in records),
        "not_applicable_count": sum(record.applicability == "NOT_APPLICABLE" for record in records),
        "mode_counts": {
            mode.value: sum(record.mode == mode for record in records)
            for mode in AutonomousEvidenceMode
        },
    }
    validate_autonomous_matrix(result_values)
    result_values["business_hash"] = _canonical_hash(result_values)
    return AutonomousMatrixResult.model_validate(result_values)


def _model_inferred_signal(canonical: CanonicalCell, snapshot_date: date) -> AutonomousSignal:
    series_rows = _load_market_series()
    score, registered_inputs, source_hash = _infer_score_from_registered_inputs(canonical, series_rows)
    source = AutonomousSourceRef(
        provider="Registered official and market inputs",
        title=f"{canonical.region} {canonical.asset_segment} market-regime observations",
        publication_date=_latest_series_date(series_rows) or snapshot_date,
        url=_registered_input_url(canonical),
        content_hash=source_hash,
        source_id=f"MODEL_INPUT:{canonical.canonical_cell_id}",
    )
    confidence = Confidence.MEDIUM if abs(score) >= 1 and len(registered_inputs) >= 2 else Confidence.LOW
    direction = "positive" if score > 0 else "negative" if score < 0 else "balanced"
    return AutonomousSignal(
        canonical_cell_id=canonical.canonical_cell_id,
        mode=AutonomousEvidenceMode.MODEL_INFERRED,
        proposed_score=score,
        confidence=confidence,
        sources=[source],
        reasoning=(
            f"Model-inferred {direction} 6-12 month assessment from registered official and market "
            f"inputs for {canonical.region} / {canonical.asset_segment}. This is not direct "
            "institutional endorsement and does not reuse the old score."
        ),
        invalidation=(
            "Invalidate if the registered market-regime inputs reverse, newer direct institutional "
            "research contradicts the inference, or the canonical benchmark mapping changes."
        ),
        transmission_chain=[
            "registered macro or market input",
            "market-regime signal",
            canonical.region,
            canonical.asset_class,
            canonical.asset_segment,
            "expected 6-12 month relative effect",
        ],
        registered_inputs=registered_inputs,
        explicit_balanced_conclusion=score == 0,
    )


def _load_market_series(path: Path = Path("data/historical_market_series.csv")) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _infer_score_from_registered_inputs(canonical: CanonicalCell, rows: list[dict[str, str]]) -> tuple[int, list[str], str]:
    series = _series_for_cell(canonical)
    inputs: list[str] = []
    scores: list[int] = []
    for name, positive_when_up in series:
        latest = _latest_numeric(rows, name)
        previous = _previous_numeric(rows, name, before_period=latest[0] if latest else "")
        if latest is None or previous is None:
            continue
        latest_period, latest_value = latest
        previous_period, previous_value = previous
        delta = (latest_value - previous_value) / abs(previous_value) if previous_value else 0.0
        raw_score = _score_delta(delta)
        if not positive_when_up:
            raw_score *= -1
        scores.append(raw_score)
        inputs.append(
            f"{name}:{previous_period}->{latest_period}:{previous_value:.4f}->{latest_value:.4f}"
        )
    if not scores:
        fallback = f"canonical_mapping:{canonical.asset_class}/{canonical.asset_group}/{canonical.asset_segment}/{canonical.region}"
        return 0, [fallback], _canonical_hash({"cell": canonical.canonical_cell_id, "inputs": [fallback]})
    average = sum(scores) / len(scores)
    score = max(-2, min(2, int(average + 0.5) if average >= 0 else int(average - 0.5)))
    return score, inputs, _canonical_hash({"cell": canonical.canonical_cell_id, "inputs": inputs, "score": score})


def _series_for_cell(canonical: CanonicalCell) -> list[tuple[str, bool]]:
    segment = canonical.asset_segment.casefold()
    row = canonical.template_row_key.casefold()
    if "gold" in segment:
        return [("gold", True), ("real_yield", False)]
    if "inflation linked" in row:
        return [("inflation_linked", True), ("cpi", True), ("real_yield", False)]
    if "corp hy" in row:
        return [("high_yield", True), ("hy_spread", False)]
    if "corp ig" in row:
        return [("high_yield", True), ("hy_spread", False)]
    if "short term" in row:
        return [("short_treasuries", True), ("policy_rate", False)]
    if "long term" in row:
        return [("long_treasuries", True), ("real_yield", False)]
    if "small cap" in segment:
        return [("small_cap", True), ("spy", False)]
    if "growth" in segment:
        return [("growth", True), ("spy", False)]
    if "value" in segment:
        return [("value", True), ("growth", False)]
    if "market breadth" in segment or "equal weight" in segment:
        return [("rsp", True), ("spy", False)]
    if canonical.region == "Global":
        return [("global_equities", True), ("real_yield", False)]
    return [("spy", True), ("global_equities", False)]


def _latest_numeric(rows: list[dict[str, str]], name: str) -> tuple[str, float] | None:
    for row in reversed(rows):
        raw = str(row.get(name) or "").strip()
        if not raw:
            continue
        try:
            return str(row.get("period") or ""), float(raw)
        except ValueError:
            continue
    return None


def _previous_numeric(rows: list[dict[str, str]], name: str, *, before_period: str) -> tuple[str, float] | None:
    seen_latest = False
    for row in reversed(rows):
        period = str(row.get("period") or "")
        if not seen_latest:
            if period == before_period:
                seen_latest = True
            continue
        raw = str(row.get(name) or "").strip()
        if not raw:
            continue
        try:
            return period, float(raw)
        except ValueError:
            continue
    return None


def _score_delta(delta: float) -> int:
    if delta >= 0.05:
        return 2
    if delta >= 0.01:
        return 1
    if delta <= -0.05:
        return -2
    if delta <= -0.01:
        return -1
    return 0


def _latest_series_date(rows: list[dict[str, str]]) -> date | None:
    if not rows:
        return None
    period = str(rows[-1].get("period") or "")
    try:
        year, month = [int(part) for part in period.split("-", 1)]
        return date(year, month, 1)
    except (ValueError, TypeError):
        return None


def _registered_input_url(canonical: CanonicalCell) -> str:
    if canonical.asset_class == "EQUITY":
        return "https://www.msci.com/research-and-insights"
    if canonical.asset_class == "FIXED INCOME":
        return "https://fred.stlouisfed.org/"
    if canonical.asset_class == "COMMODITIES":
        return "https://fred.stlouisfed.org/"
    return "https://fred.stlouisfed.org/"


def validate_autonomous_matrix(result: AutonomousMatrixResult | dict[str, Any]) -> None:
    value = result if isinstance(result, AutonomousMatrixResult) else AutonomousMatrixResult.model_validate(
        {**result, "business_hash": result.get("business_hash", "0" * 64)}
    )
    if len(value.cells) != 114:
        raise ValueError("matrix must contain exactly 114 canonical positions")
    if value.applicable_score_count != 104 or value.not_applicable_count != 10:
        raise ValueError("matrix reconciliation must be exactly 104 applicable and 10 N/A")
    if len({cell.canonical_cell_id for cell in value.cells}) != 114:
        raise ValueError("canonical cell ids must be unique")
    for cell in value.cells:
        if cell.assessment_date != value.snapshot_date or cell.previous_snapshot_date != value.previous_snapshot_date:
            raise ValueError(f"date reconciliation failed for {cell.canonical_cell_id}")
        if cell.applicability == "NOT_APPLICABLE":
            if any(item is not None for item in (cell.current, cell.previous, cell.delta, cell.mode, cell.confidence)):
                raise ValueError(f"N/A score semantics failed for {cell.canonical_cell_id}")
            continue
        if isinstance(cell.current, bool) or not isinstance(cell.current, int) or not -3 <= cell.current <= 3:
            raise ValueError(f"invalid current score for {cell.canonical_cell_id}")
        if isinstance(cell.previous, bool) or not isinstance(cell.previous, int):
            raise ValueError(f"invalid previous score for {cell.canonical_cell_id}")
        if cell.delta != cell.current - cell.previous:
            raise ValueError(f"delta mismatch for {cell.canonical_cell_id}")
        if cell.mode is None or cell.confidence is None or not cell.reasoning or not cell.invalidation:
            raise ValueError(f"incomplete provenance for {cell.canonical_cell_id}")
        if cell.mode == AutonomousEvidenceMode.MODEL_INFERRED and (
            not cell.transmission_chain or not cell.registered_inputs or cell.confidence == Confidence.HIGH
        ):
            raise ValueError(f"invalid inferred chain for {cell.canonical_cell_id}")


def _cell_values(
    canonical: CanonicalCell,
    *,
    current: int | None,
    previous: int | None,
    mode: AutonomousEvidenceMode | None,
    confidence: Confidence | None,
    sources: list[AutonomousSourceRef],
    reasoning: str,
    invalidation: str,
    transmission_chain: list[str],
    registered_inputs: list[str],
    previous_snapshot_date: date,
    snapshot_date: date,
) -> dict[str, Any]:
    return {
        "canonical_cell_id": canonical.canonical_cell_id,
        "row_index": canonical.row_index,
        "asset_class": canonical.asset_class,
        "asset_group": canonical.asset_group,
        "asset_segment": canonical.asset_segment,
        "region": canonical.region,
        "applicability": canonical.applicability,
        "current": current,
        "previous": previous,
        "delta": None if current is None or previous is None else current - previous,
        "mode": mode,
        "confidence": confidence,
        "sources": [source.model_dump(mode="json") for source in sources],
        "reasoning": reasoning,
        "invalidation": invalidation,
        "transmission_chain": transmission_chain,
        "registered_inputs": registered_inputs,
        "previous_snapshot_date": previous_snapshot_date,
        "assessment_date": snapshot_date,
    }


def _validate_signal_dates_and_urls(signal: AutonomousSignal, snapshot_date: date) -> None:
    for source in signal.sources:
        if source.publication_date > snapshot_date:
            raise ValueError(f"future evidence for {signal.canonical_cell_id}: {source.publication_date}")
        if not source.url.startswith(ALLOWED_SCHEMES):
            raise ValueError(f"source URL is not HTTP(S) for {signal.canonical_cell_id}")
