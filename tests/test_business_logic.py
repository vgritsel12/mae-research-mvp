from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

from app.domain.enums import AnalystStatus, Confidence, Direction, EvidenceStatus, SignalDirection
from app.domain.models import ShiftSignal, utcnow
from app.services.changes import ChangeTrackerService
from app.services.evidence import EvidenceEngine
from app.services.matrix import MatrixEngine
from app.services.normalization import round_half_away_from_zero, valid_score


def test_materiality_high_for_direction_and_score_shift() -> None:
    previous = SimpleNamespace(direction=Direction.NEUTRAL.value, position_score=0)
    current = SimpleNamespace(direction=Direction.BULLISH.value, position_score=2)

    materiality = ChangeTrackerService.calculate_materiality(previous, current, ["DIRECTION_CHANGE", "SCORE_CHANGE"])

    assert materiality.value == "HIGH"


def test_evidence_thresholds_confirmed_and_critical_contradiction() -> None:
    rows = [
        SimpleNamespace(source_url="manual://1", actual="above", expected="above", support_value=1, importance="CRITICAL"),
        SimpleNamespace(source_url="manual://2", actual="above", expected="above", support_value=1, importance="STANDARD"),
        SimpleNamespace(source_url="manual://3", actual="flat", expected="above", support_value=0, importance="STANDARD"),
    ]
    status, score = EvidenceEngine.calculate_evidence_status(rows)
    assert status == EvidenceStatus.CONFIRMED
    assert score >= 0.5

    contradicted = [
        SimpleNamespace(source_url="manual://1", actual="below", expected="above", support_value=-1, importance="CRITICAL"),
        SimpleNamespace(source_url="manual://2", actual="flat", expected="above", support_value=0, importance="STANDARD"),
    ]
    status, _ = EvidenceEngine.calculate_evidence_status(contradicted)
    assert status == EvidenceStatus.CONTRADICTED


def test_matrix_rounding_and_score_range() -> None:
    assert round_half_away_from_zero(1.5) == 2
    assert round_half_away_from_zero(-1.5) == -2
    assert valid_score(3) == 3

    signal = ShiftSignal(
        id="signal-1",
        change_id="change-1",
        asset="Growth",
        template_row_key="EQUITY|Other categories|Growth",
        region="US",
        direction=SignalDirection.POSITIVE.value,
        suggested_strength=2,
        what_changed="score 0 -> 2",
        dominant_scenario="BASE",
        why_now="demo",
        evidence_status=EvidenceStatus.CONFIRMED.value,
        pricing_status="PARTLY_PRICED",
        transmission_chain="demo",
        trigger="demo",
        veto="demo",
        key_risk="demo",
        next_review_date=date.today() + timedelta(days=30),
        confidence=Confidence.MEDIUM.value,
        source_ids=["a", "b"],
        source_urls=["manual://a", "manual://b"],
        analyst_status=AnalystStatus.APPROVED.value,
        analyst_comment="ok",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    score, details = MatrixEngine.calculate_cell_score([signal], date.today())
    assert -3 <= score <= 3
    assert score == 2
    assert details["contributions"][0]["independence_factor"] == 1.0


def test_pricing_status_from_market_series(tmp_path) -> None:
    path = tmp_path / "market.csv"
    path.write_text(
        "date,asset,benchmark,asset_price,benchmark_price\n"
        "2026-06-01,A,B,100,100\n"
        "2026-06-02,A,B,105,100.2\n"
        "2026-06-03,A,B,110,100.4\n",
        encoding="utf-8",
    )

    status, metrics = EvidenceEngine.calculate_pricing_status(path)

    assert status.value in {"PARTLY_PRICED", "PRICED_IN"}
    assert metrics["normalized_move"] >= 0.5
