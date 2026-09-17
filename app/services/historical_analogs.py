from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import requests

from app.config import ROOT_DIR


V2_CONFIG_PATH = ROOT_DIR / "data" / "historical_analogs_v2.json"
HIGH_YIELD_ASSESSMENT_PATH = ROOT_DIR / "data" / "high_yield_credit_source_assessment_v2.json"
V2_CONFIG = json.loads(V2_CONFIG_PATH.read_text(encoding="utf-8"))
METHODOLOGY_VERSION = str(V2_CONFIG["methodology_version"])
MINIMUM_SEPARATION_MONTHS = 6
TOP_ANALOGS = 5
HORIZONS = (1, 3, 6, 12)
MAIN_OUTCOME_HORIZONS = tuple(int(item) for item in V2_CONFIG["main_outcome_horizons_months"])
CRITICAL_FACTORS = tuple(str(item) for item in V2_CONFIG["critical_factors"])
MINIMUM_FACTOR_COVERAGE = float(V2_CONFIG["minimum_factor_coverage"])
CPI_PUBLICATION_LAG_MONTHS = 2

FACTOR_CONFIG: dict[str, dict[str, Any]] = {
    "inflation_regime": {
        "label": "Inflation regime",
        "weight": 0.20,
        "source": "CPIAUCSL",
    },
    "policy_stance": {
        "label": "Policy stance",
        "weight": 0.20,
        "source": "FEDFUNDS minus lagged CPI inflation",
    },
    "real_yield_regime": {
        "label": "Real-yield regime",
        "weight": 0.15,
        "source": "DFII10",
    },
    "credit_spread_regime": {
        "label": "Credit-spread regime",
        "weight": 0.15,
        "source": "BAMLH0A0HYM2",
    },
    "equity_breadth": {
        "label": "Equity breadth",
        "weight": 0.15,
        "source": "RSP / SPY six-month relative return",
    },
    "growth_style_leadership": {
        "label": "Growth / style leadership",
        "weight": 0.15,
        "source": "IWF / IWD six-month relative return",
    },
}

FRED_SERIES = {
    "cpi": "CPIAUCSL",
    "policy_rate": "FEDFUNDS",
    "real_yield": "DFII10",
    "hy_spread": "BAMLH0A0HYM2",
}

MARKET_SERIES = {
    "spy": "SPY",
    "rsp": "RSP",
    "growth": "IWF",
    "value": "IWD",
    "small_cap": "IWM",
    "short_treasuries": "SHY",
    "long_treasuries": "TLT",
    "high_yield": "HYG",
    "inflation_linked": "TIP",
    "gold": "GLD",
    "global_equities": "ACWI",
}

OUTCOME_ASSETS = {
    "global_equities": "Global Equities",
    "us_equities": "US Equities",
    "growth": "Growth",
    "value": "Value",
    "small_cap": "Small Cap",
    "short_treasuries": "Short Treasuries",
    "long_treasuries": "Long Treasuries",
    "high_yield": "High Yield",
    "inflation_linked": "Inflation-Linked",
    "gold": "Gold",
}

OUTCOME_COLUMNS = {
    "global_equities": "global_equities",
    "us_equities": "spy",
    "growth": "growth",
    "value": "value",
    "small_cap": "small_cap",
    "short_treasuries": "short_treasuries",
    "long_treasuries": "long_treasuries",
    "high_yield": "high_yield",
    "inflation_linked": "inflation_linked",
    "gold": "gold",
}

SOURCE_METADATA = {
    "CPIAUCSL": {
        "provider": "U.S. Bureau of Labor Statistics via FRED",
        "url": "https://fred.stlouisfed.org/series/CPIAUCSL",
        "use": "Inflation regime",
    },
    "FEDFUNDS": {
        "provider": "Federal Reserve Board via FRED",
        "url": "https://fred.stlouisfed.org/series/FEDFUNDS",
        "use": "Policy stance",
    },
    "DFII10": {
        "provider": "Federal Reserve Board via FRED",
        "url": "https://fred.stlouisfed.org/series/DFII10",
        "use": "10-year real-yield regime",
    },
    "BAMLH0A0HYM2": {
        "provider": "ICE Data Indices via FRED",
        "url": "https://fred.stlouisfed.org/series/BAMLH0A0HYM2",
        "use": "US high-yield option-adjusted spread",
    },
    "RSP": {
        "provider": "Invesco / market price history",
        "url": "https://www.invesco.com/us/financial-products/etfs/product-detail?productId=RSP",
        "use": "S&P 500 Equal Weight breadth proxy",
    },
    "IWF": {
        "provider": "iShares / market price history",
        "url": "https://www.ishares.com/us/products/239706/ishares-russell-1000-growth-etf",
        "use": "Russell 1000 Growth proxy",
    },
    "IWD": {
        "provider": "iShares / market price history",
        "url": "https://www.ishares.com/us/products/239708/ishares-russell-1000-value-etf",
        "use": "Russell 1000 Value proxy",
    },
    "MARKET_PRICES": {
        "provider": "Yahoo Finance chart history",
        "url": "https://finance.yahoo.com/",
        "use": "Adjusted-close benchmark proxies for regime and forward outcomes",
    },
}


@dataclass(frozen=True)
class HistoricalAnalogPaths:
    latest_json: Path
    latest_csv: Path
    dated_json: Path
    dated_csv: Path
    series_csv: Path
    sources_json: Path


@dataclass(frozen=True)
class HistoricalAnalogCandidatePaths:
    directory: Path
    result_json: Path
    main_csv: Path
    recent_csv: Path
    excluded_csv: Path
    dispersion_csv: Path
    source_assessment_json: Path
    manifest_json: Path
    failed_diagnostics_json: Path


def analog_paths(snapshot_date: date | str, *, root: Path = ROOT_DIR) -> HistoricalAnalogPaths:
    stamp = snapshot_date.isoformat() if isinstance(snapshot_date, date) else str(snapshot_date)
    dated = root / "outputs" / "snapshots" / stamp / "historical_analogs"
    return HistoricalAnalogPaths(
        latest_json=root / "outputs" / "mae_historical_analogs_latest.json",
        latest_csv=root / "outputs" / "mae_historical_analogs_latest.csv",
        dated_json=dated / f"mae_historical_analogs_{stamp}.json",
        dated_csv=dated / f"mae_historical_analogs_{stamp}.csv",
        series_csv=root / "data" / "historical_market_series.csv",
        sources_json=root / "data" / "historical_market_series_sources.json",
    )


def candidate_analog_paths(candidate_id: str, *, root: Path = ROOT_DIR) -> HistoricalAnalogCandidatePaths:
    if not candidate_id or any(part in candidate_id for part in ("/", "\\", "..")):
        raise ValueError("Unsafe candidate_id")
    directory = root / "outputs" / "candidates" / candidate_id / "historical_analogs"
    return HistoricalAnalogCandidatePaths(
        directory=directory,
        result_json=directory / "historical_analogs_v2.json",
        main_csv=directory / "historical_analogs_main.csv",
        recent_csv=directory / "historical_analogs_recent_incomplete.csv",
        excluded_csv=directory / "historical_analogs_critical_excluded.csv",
        dispersion_csv=directory / "historical_analogs_dispersion.csv",
        source_assessment_json=directory / "high_yield_credit_source_assessment.json",
        manifest_json=directory / "artifact_manifest.json",
        failed_diagnostics_json=(
            root / "outputs" / "candidates" / candidate_id / "failed_runs" / "historical_analogs_v2_failed.json"
        ),
    )


def download_market_series(
    snapshot_date: date,
    *,
    http_get: Callable[..., requests.Response] = requests.get,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Download real observations for the supplementary analog layer only."""
    start = date(1999, 1, 1)
    monthly: dict[str, pd.Series] = {}
    for column, series_id in FRED_SERIES.items():
        monthly[column] = _download_fred_series(series_id, start, snapshot_date, http_get=http_get)
    for column, ticker in MARKET_SERIES.items():
        monthly[column] = _download_market_prices(ticker, start, snapshot_date, http_get=http_get)

    frame = pd.concat(monthly, axis=1).sort_index()
    frame.index = frame.index.astype(str)
    frame.index.name = "period"
    frame = frame.loc[(frame.index >= "2000-01") & (frame.index <= snapshot_date.strftime("%Y-%m"))]
    metadata = {
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_cutoff": snapshot_date.isoformat(),
        "period_start": str(frame.index.min()) if not frame.empty else "",
        "period_end": str(frame.index.max()) if not frame.empty else "",
        "frequency": "month-end / last available observation",
        "sources": SOURCE_METADATA,
        "price_tickers": MARKET_SERIES,
        "fred_series": FRED_SERIES,
        "notes": [
            "No observation after the snapshot cutoff is requested or stored.",
            "ETF adjusted-close histories are transparent benchmark proxies, not investable recommendations.",
            f"CPI is lagged {CPI_PUBLICATION_LAG_MONTHS} months in regime construction to avoid publication look-ahead.",
        ],
    }
    return frame, metadata


def build_historical_analogs(
    series: pd.DataFrame,
    snapshot_date: date,
    *,
    top_n: int = TOP_ANALOGS,
    minimum_separation_months: int = MINIMUM_SEPARATION_MONTHS,
) -> dict[str, Any]:
    """Build deterministic US-led analog v2; outcomes never enter similarity."""
    clean = _normalize_series_frame(series, snapshot_date)
    features = build_regime_features(clean)
    if features.empty:
        raise ValueError("Historical regime features are unavailable.")

    current_period = features.dropna(how="all").index.max()
    current = features.loc[current_period]
    zscores = _trailing_zscores(features)
    current_z = zscores.loc[current_period]

    cutoff = current_period - minimum_separation_months
    scored_complete_12m: list[dict[str, Any]] = []
    scored_partial_12m: list[dict[str, Any]] = []
    recent_regime_matches: list[dict[str, Any]] = []
    critical_excluded: list[dict[str, Any]] = []
    for period, row in features.loc[features.index < current_period].iterrows():
        raw_similarity, coverage, distances = similarity_score(current_z, zscores.loc[period])
        if raw_similarity is None:
            continue
        missing_factors = [
            key
            for key in FACTOR_CONFIG
            if pd.isna(current_z.get(key)) or pd.isna(zscores.at[period, key])
        ]
        missing_critical = [key for key in CRITICAL_FACTORS if key in missing_factors]
        critical_factor_penalty = _critical_factor_penalty(missing_critical)
        adjusted_similarity = raw_similarity * coverage * critical_factor_penalty
        outcomes = calculate_forward_returns(clean, period, snapshot_date=snapshot_date)
        missing_windows = _missing_outcome_columns(outcomes, MAIN_OUTCOME_HORIZONS)
        candidate = {
            "period": period,
            "date": period.to_timestamp("M").date().isoformat(),
            "regime_name": f"US-led market context — {period.strftime('%b %Y')}",
            "raw_similarity": raw_similarity,
            "factor_coverage": coverage,
            "critical_factor_penalty": critical_factor_penalty,
            "adjusted_similarity": adjusted_similarity,
            "display_similarity": round_similarity_display(adjusted_similarity),
            "critical_factor_state": "PASS" if not missing_critical else "PENALIZED",
            "missing_critical_factors": missing_critical,
            "missing_factors": missing_factors,
            "confidence": _confidence_bucket(adjusted_similarity, coverage, missing_factors),
            "limitations": _candidate_limitations(missing_factors, missing_critical),
            "distances": distances,
            "features": row,
            "forward_returns": outcomes,
            "missing_window_columns": missing_windows,
            "similarity_input_end": period.to_timestamp("M").date().isoformat(),
        }
        if coverage < MINIMUM_FACTOR_COVERAGE:
            candidate["confidence"] = "EXCLUDED"
            candidate["exclusion_reason"] = "INSUFFICIENT_FACTOR_COVERAGE"
            candidate["limitations"] = sorted(set(candidate["limitations"] + ["INSUFFICIENT_FACTOR_COVERAGE"]))
            critical_excluded.append(candidate)
            continue
        if period > cutoff:
            candidate["recent_reason"] = "Период слишком недавний, поэтому результат ещё формируется."
            recent_regime_matches.append(candidate)
            continue
        if not _has_any_horizon_outcome(outcomes, "3m") or not _has_any_horizon_outcome(outcomes, "6m"):
            candidate["recent_reason"] = "Для периода ещё нет завершённых результатов за 3 и 6 месяцев."
            recent_regime_matches.append(candidate)
            continue
        if _has_any_horizon_outcome(outcomes, "12m"):
            scored_complete_12m.append(candidate)
        else:
            scored_partial_12m.append(candidate)

    selected = select_separated_analogs(
        scored_complete_12m,
        top_n=top_n,
        minimum_separation_months=minimum_separation_months,
    )
    if len(selected) < top_n:
        selected = _fill_with_partial_12m_analogs(
            selected,
            scored_partial_12m,
            top_n=top_n,
            minimum_separation_months=minimum_separation_months,
        )
    analogs = [_public_candidate(candidate, rank=rank) for rank, candidate in enumerate(selected, start=1)]

    if not analogs:
        raise ValueError("No historical periods passed separation and realized-outcome gates.")

    period_start = features.dropna(how="all").index.min()
    data_last_available_period = _data_last_available_period(clean)
    regime_cutoff_date = _period_observation_cutoff(current_period, snapshot_date)
    data_last_available_date = (
        _period_observation_cutoff(data_last_available_period, snapshot_date)
        if data_last_available_period is not None
        else None
    )
    coverage = {
        key: round(float(features[key].notna().mean()), 3)
        for key in FACTOR_CONFIG
    }
    result = {
        "status": "PASS",
        "mode": "DATA_DRIVEN_CANDIDATE",
        "methodology_version": METHODOLOGY_VERSION,
        "schema_version": "2.0",
        "snapshot_date": snapshot_date.isoformat(),
        "release_snapshot_date": snapshot_date.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "regime_observation_cutoff": regime_cutoff_date.isoformat(),
        "data_last_available_date": (
            data_last_available_date.isoformat()
            if data_last_available_date is not None
            else ""
        ),
        "current_regime_period": str(current_period),
        "historical_period": {
            "start": str(period_start),
            "end": str(current_period),
            "frequency": "monthly",
        },
        "universe_label": V2_CONFIG["universe_label"],
        "current_regime_name": f"US-led market context — {current_period.strftime('%b %Y')}",
        "current_regime": describe_regime(current),
        "current_regime_values": {key: _nullable_float(current.get(key)) for key in FACTOR_CONFIG},
        "factor_config": FACTOR_CONFIG,
        "critical_factors": list(CRITICAL_FACTORS),
        "factor_coverage": coverage,
        "minimum_separation_months": minimum_separation_months,
        "horizons_months": list(HORIZONS),
        "outcome_assets": OUTCOME_ASSETS,
        "historical_analogs": analogs,
        "analogs": analogs,
        "recent_regime_matches": [
            _public_candidate(candidate)
            for candidate in sorted(
                recent_regime_matches,
                key=lambda item: (-item["adjusted_similarity"], str(item["period"])),
            )[:10]
        ],
        "recent_incomplete": [
            _public_candidate(candidate)
            for candidate in sorted(
                recent_regime_matches,
                key=lambda item: (-item["adjusted_similarity"], str(item["period"])),
            )[:10]
        ],
        "critical_excluded": [
            _public_candidate(candidate)
            for candidate in sorted(
                critical_excluded,
                key=lambda item: (-item["adjusted_similarity"], str(item["period"])),
            )
        ],
        "excluded_candidates": [
            _public_candidate(candidate)
            for candidate in sorted(
                critical_excluded,
                key=lambda item: (-item["adjusted_similarity"], str(item["period"])),
            )
        ],
        "dispersion": _outcome_dispersion(analogs),
        "reconciliation": {
            "input_rows": int(len(clean)),
            "main_eligible_before_separation": len(scored_complete_12m) + len(scored_partial_12m),
            "main_selected": len(analogs),
            "recent_regime_matches": len(recent_regime_matches),
            "recent_incomplete": len(recent_regime_matches),
            "critical_excluded": len(critical_excluded),
        },
        "no_look_ahead": {
            "status": "PASS",
            "similarity_uses_forward_returns": False,
            "current_period_excluded": all(item["period"] != str(current_period) for item in analogs),
            "candidate_cutoff_period": str(cutoff),
            "cpi_publication_lag_months": CPI_PUBLICATION_LAG_MONTHS,
            "source_observation_cutoff": snapshot_date.isoformat(),
            "regime_observation_cutoff": regime_cutoff_date.isoformat(),
            "data_last_available_date": (
                data_last_available_date.isoformat()
                if data_last_available_date is not None
                else ""
            ),
        },
        "downstream_use": V2_CONFIG["downstream_use"],
        "credit_spread_source_assessment": json.loads(
            HIGH_YIELD_ASSESSMENT_PATH.read_text(encoding="utf-8")
        ),
        "limitations": [
            "Historical similarity is descriptive, not predictive.",
            "The sample of genuinely comparable periods is limited.",
            (
                "Periods missing one critical factor may enter the main analog ranking after "
                "weight renormalisation, an explicit coverage penalty and reduced confidence. "
                "No synthetic observations are created."
            ),
            "Very recent regime matches are shown separately from historical analogs.",
            "Missing credit-spread history reduces similarity and confidence but does not create synthetic data.",
            "Market structure and the policy reaction function may differ from today.",
            "Historical similarity does not establish causality.",
            "Analog ranks and outcomes must not feed MAE cell scores or scenario probabilities.",
        ],
    }
    result["validation_assertions"] = _historical_validation_assertions(result)
    return result


def build_regime_features(series: pd.DataFrame) -> pd.DataFrame:
    frame = series.copy()
    lagged_inflation = frame["cpi"].pct_change(12, fill_method=None).mul(100).shift(CPI_PUBLICATION_LAG_MONTHS)
    ratio_breadth = frame["rsp"].div(frame["spy"])
    ratio_style = frame["growth"].div(frame["value"])
    features = pd.DataFrame(index=frame.index)
    features["inflation_regime"] = lagged_inflation
    features["policy_stance"] = frame["policy_rate"].sub(lagged_inflation)
    features["real_yield_regime"] = frame["real_yield"]
    features["credit_spread_regime"] = frame["hy_spread"]
    features["equity_breadth"] = ratio_breadth.pct_change(6, fill_method=None).mul(100)
    features["growth_style_leadership"] = ratio_style.pct_change(6, fill_method=None).mul(100)
    return features.replace([math.inf, -math.inf], pd.NA).astype(float)


def similarity_score(current: pd.Series, candidate: pd.Series) -> tuple[float | None, float, dict[str, float]]:
    available = [key for key in FACTOR_CONFIG if pd.notna(current.get(key)) and pd.notna(candidate.get(key))]
    available_weight = sum(float(FACTOR_CONFIG[key]["weight"]) for key in available)
    if not available or available_weight <= 0:
        return None, 0.0, {}
    distances = {
        key: min(abs(float(current[key]) - float(candidate[key])) / 4.0, 1.0)
        for key in available
    }
    weighted_distance = sum(
        distances[key] * float(FACTOR_CONFIG[key]["weight"])
        for key in available
    ) / available_weight
    return 100.0 * (1.0 - weighted_distance), available_weight, distances


def round_similarity_display(value: float) -> int:
    """Round non-negative similarity half-up, independent of Python bankers' rounding."""
    return int(math.floor(float(value) + 0.5))


def select_separated_analogs(
    scored: list[dict[str, Any]],
    *,
    top_n: int,
    minimum_separation_months: int,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for candidate in sorted(scored, key=lambda item: (-item["adjusted_similarity"], str(item["period"]))):
        period = candidate["period"]
        if all(abs(period.ordinal - item["period"].ordinal) >= minimum_separation_months for item in selected):
            selected.append(candidate)
        if len(selected) >= top_n:
            break
    return selected


def _fill_with_partial_12m_analogs(
    selected: list[dict[str, Any]],
    partial: list[dict[str, Any]],
    *,
    top_n: int,
    minimum_separation_months: int,
) -> list[dict[str, Any]]:
    result = list(selected)
    selected_periods = {item["period"] for item in result}
    for candidate in sorted(partial, key=lambda item: (-item["adjusted_similarity"], str(item["period"]))):
        if candidate["period"] in selected_periods:
            continue
        if any(abs(candidate["period"].ordinal - item["period"].ordinal) < minimum_separation_months for item in result):
            continue
        enriched = dict(candidate)
        enriched["included_with_partial_12m_outcome"] = True
        enriched.setdefault("limitations", [])
        if "TWELVE_MONTH_OUTCOME_INCOMPLETE_SHOWN_EXPLICITLY" not in enriched["limitations"]:
            enriched["limitations"] = list(enriched["limitations"]) + ["TWELVE_MONTH_OUTCOME_INCOMPLETE_SHOWN_EXPLICITLY"]
        result.append(enriched)
        selected_periods.add(candidate["period"])
        if len(result) >= top_n:
            break
    return sorted(result, key=lambda item: (-float(item["adjusted_similarity"]), str(item["period"])))[:top_n]


def _critical_factor_penalty(missing_critical: list[str]) -> float:
    if not missing_critical:
        return 1.0
    return max(0.72, 0.92 ** len(missing_critical))


def _has_any_horizon_outcome(outcomes: dict[str, dict[str, float | None]], horizon: str) -> bool:
    return any(isinstance(value, (int, float)) for value in (outcomes.get(horizon) or {}).values())


def calculate_forward_returns(
    series: pd.DataFrame,
    period: pd.Period,
    *,
    snapshot_date: date,
) -> dict[str, dict[str, float | None]]:
    result: dict[str, dict[str, float | None]] = {}
    snapshot_period = _last_complete_period(snapshot_date)
    for months in HORIZONS:
        target = period + months
        values: dict[str, float | None] = {}
        for asset, column in OUTCOME_COLUMNS.items():
            if target > snapshot_period or period not in series.index or target not in series.index:
                values[asset] = None
                continue
            start_value = series.at[period, column] if column in series else pd.NA
            end_value = series.at[target, column] if column in series else pd.NA
            if pd.isna(start_value) or pd.isna(end_value) or float(start_value) == 0:
                values[asset] = None
            else:
                values[asset] = round((float(end_value) / float(start_value) - 1.0) * 100.0, 2)
        result[f"{months}m"] = values
    return result


def _last_complete_period(snapshot_date: date) -> pd.Period:
    current = pd.Period(snapshot_date, freq="M")
    month_end = current.to_timestamp("M").date()
    return current if snapshot_date >= month_end else current - 1


def _data_last_available_period(series: pd.DataFrame) -> pd.Period | None:
    available = series.dropna(how="all")
    if available.empty:
        return None
    index = available.index
    if not isinstance(index, pd.PeriodIndex):
        index = pd.PeriodIndex(index.astype(str), freq="M")
    return index.max()


def _period_observation_cutoff(period: pd.Period, snapshot_date: date) -> date:
    return min(period.to_timestamp("M").date(), snapshot_date)


def _missing_outcome_columns(
    outcomes: dict[str, dict[str, float | None]],
    horizons: tuple[int, ...],
) -> list[str]:
    missing = []
    for months in horizons:
        horizon = f"{months}m"
        for asset in OUTCOME_ASSETS:
            if outcomes.get(horizon, {}).get(asset) is None:
                missing.append(f"{horizon}_{asset}")
    return missing


def _confidence_bucket(adjusted: float, coverage: float, missing_factors: list[str]) -> str:
    if coverage >= 0.95 and adjusted >= 70.0 and not missing_factors:
        return "HIGH"
    if coverage >= 0.80 and adjusted >= 50.0:
        return "MEDIUM"
    if coverage >= MINIMUM_FACTOR_COVERAGE:
        return "LOW"
    return "EXCLUDED"


def _factor_limitation_code(value: str) -> str:
    return {
        "credit_spread_regime": "MISSING_CREDIT_SPREAD",
        "inflation_regime": "MISSING_INFLATION_REGIME",
        "policy_stance": "MISSING_POLICY_STANCE",
        "real_yield_regime": "MISSING_REAL_YIELD",
        "equity_breadth": "MISSING_MARKET_BREADTH",
        "growth_style_leadership": "MISSING_GROWTH_STYLE",
    }.get(value, "MISSING_FACTOR:" + value)


def _candidate_limitations(missing: list[str], missing_critical: list[str]) -> list[str]:
    limitations = ["DESCRIPTIVE_NOT_PREDICTIVE"]
    if missing:
        limitations.extend(_factor_limitation_code(item) for item in missing)
    if missing_critical:
        limitations.extend(["COVERAGE_PENALTY_APPLIED", "REDUCED_CONFIDENCE"])
    return sorted(set(limitations))


def _historical_validation_assertions(result: dict[str, Any]) -> dict[str, bool]:
    main = result.get("historical_analogs") or result.get("analogs") or []
    recent = result.get("recent_regime_matches") or []
    excluded = (result.get("critical_excluded") or []) + (result.get("excluded_candidates") or [])
    main_periods = {str(item.get("period")) for item in main}
    recent_periods = {str(item.get("period")) for item in recent}
    methodology_text = " ".join(str(item) for item in result.get("limitations") or [])
    return {
        "main_analogs_have_non_excluded_confidence": all(
            item.get("confidence") in {"HIGH", "MEDIUM", "LOW"} for item in main
        ),
        "excluded_confidence_only_in_excluded_collections": all(
            item.get("confidence") != "EXCLUDED" for item in main + recent
        ) and all(item.get("confidence") == "EXCLUDED" for item in excluded),
        "every_excluded_object_has_exclusion_reason": all(
            bool(item.get("exclusion_reason")) for item in excluded
        ),
        "recent_matches_not_in_main_analogs": not (main_periods & recent_periods),
        "main_analogs_have_3m_and_6m_outcomes": all(
            _has_any_horizon_outcome(item.get("forward_returns") or {}, "3m")
            and _has_any_horizon_outcome(item.get("forward_returns") or {}, "6m")
            for item in main
        ),
        "historical_methodology_matches_actual_selection": (
            "may enter the main analog ranking" in methodology_text
            and "No synthetic observations are created" in methodology_text
            and "excluded from main analogs" not in methodology_text
        ),
    }


def _public_candidate(candidate: dict[str, Any], *, rank: int | None = None) -> dict[str, Any]:
    result = {
        key: value
        for key, value in candidate.items()
        if key not in {"period", "features"}
    }
    result["period"] = str(candidate["period"])
    result["regime"] = describe_regime(candidate["features"])
    result["description"] = _regime_sentence(candidate["features"])
    if rank is not None:
        result["rank"] = rank
    return result


def _outcome_dispersion(analogs: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, float | int | None]]]:
    result: dict[str, dict[str, dict[str, float | int | None]]] = {}
    for months in HORIZONS:
        horizon = f"{months}m"
        result[horizon] = {}
        for asset in OUTCOME_ASSETS:
            values = [
                float(item["forward_returns"][horizon][asset])
                for item in analogs
                if item["forward_returns"][horizon][asset] is not None
            ]
            result[horizon][asset] = {
                "count": len(values),
                "median": round(float(pd.Series(values).median()), 2) if values else None,
                "lower_quartile": round(float(pd.Series(values).quantile(0.25)), 2) if values else None,
                "upper_quartile": round(float(pd.Series(values).quantile(0.75)), 2) if values else None,
            }
    return result


def describe_regime(row: pd.Series) -> dict[str, str]:
    inflation = row.get("inflation_regime")
    policy = row.get("policy_stance")
    real_yield = row.get("real_yield_regime")
    spreads = row.get("credit_spread_regime")
    breadth = row.get("equity_breadth")
    style = row.get("growth_style_leadership")
    return {
        "inflation_regime": _bucket(inflation, ((3, "Sticky inflation"), (2, "Moderate inflation")), "Low inflation"),
        "policy_stance": _bucket(policy, ((0.5, "Restrictive policy"), (-1, "Neutral policy")), "Accommodative policy"),
        "real_yield_regime": _bucket(real_yield, ((1.5, "Elevated real yields"), (0, "Positive real yields")), "Negative real yields"),
        "credit_spread_regime": _credit_bucket(spreads),
        "equity_breadth": _bucket(breadth, ((2, "Improving breadth"), (-2, "Stable breadth")), "Deteriorating breadth"),
        "growth_style_leadership": _bucket(style, ((2, "Growth leadership"), (-2, "Balanced style leadership")), "Value leadership"),
    }


def _bucket(value: Any, thresholds: tuple[tuple[float, str], ...], fallback: str) -> str:
    if pd.isna(value):
        return "Unavailable"
    number = float(value)
    for threshold, label in thresholds:
        if number >= threshold:
            return label
    return fallback


def _credit_bucket(value: Any) -> str:
    if pd.isna(value):
        return "Unavailable"
    number = float(value)
    if number < 4:
        return "Tight credit spreads"
    if number <= 6:
        return "Normal credit spreads"
    return "Wide credit spreads"


def publish_historical_analogs(
    result: dict[str, Any],
    series: pd.DataFrame,
    source_metadata: dict[str, Any],
    *,
    root: Path = ROOT_DIR,
) -> HistoricalAnalogPaths:
    raise RuntimeError(
        "Direct latest analog publication is forbidden; build a release candidate and use the release gate."
    )


def validate_historical_analogs_v2(result: dict[str, Any]) -> None:
    if result.get("status") != "PASS" or result.get("methodology_version") != METHODOLOGY_VERSION:
        raise ValueError("Historical analog candidate status/version is invalid.")
    if result.get("downstream_use") != V2_CONFIG["downstream_use"]:
        raise ValueError("Historical analog non-contamination contract is missing.")
    main = result.get("analogs") or []
    if not main:
        raise ValueError("Historical analog main set is empty.")
    previous_sort_key: tuple[float, str] | None = None
    for analog in main:
        if analog["critical_factor_state"] not in {"PASS", "PENALIZED"}:
            raise ValueError("Main analog has an invalid critical-factor state.")
        if analog.get("confidence") not in {"HIGH", "MEDIUM", "LOW"}:
            raise ValueError("Main analog confidence must be HIGH, MEDIUM or LOW.")
        if "CRITICAL_FACTOR_GATE_FAILED" in (analog.get("limitations") or []):
            raise ValueError("Main analog limitation cannot claim critical-factor exclusion.")
        expected = analog["raw_similarity"] * analog["factor_coverage"] * analog.get("critical_factor_penalty", 1.0)
        if not math.isclose(analog["adjusted_similarity"], expected, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError("Adjusted similarity does not equal raw similarity times coverage and critical-factor penalty.")
        if "missing_window_columns" not in analog:
            raise ValueError("Main analog outcome-availability field is missing.")
        if not _has_any_horizon_outcome(analog.get("forward_returns") or {}, "3m"):
            raise ValueError("Main analog lacks a completed 3M outcome.")
        if not _has_any_horizon_outcome(analog.get("forward_returns") or {}, "6m"):
            raise ValueError("Main analog lacks a completed 6M outcome.")
        if not str(analog["regime_name"]).startswith("US-led market context"):
            raise ValueError("Analog regime name does not identify the US-led context.")
        sort_key = (-float(analog["adjusted_similarity"]), str(analog["period"]))
        if previous_sort_key is not None and sort_key < previous_sort_key:
            raise ValueError("Main analog ordering is not deterministic.")
        previous_sort_key = sort_key
    for analog in result.get("critical_excluded") or []:
        if analog.get("exclusion_reason") != "INSUFFICIENT_FACTOR_COVERAGE":
            raise ValueError("Excluded record is not tied to insufficient factor coverage.")
        if analog.get("confidence") != "EXCLUDED":
            raise ValueError("Excluded record must carry confidence=EXCLUDED.")
    for analog in result.get("recent_regime_matches") or result.get("recent_incomplete") or []:
        if not analog.get("recent_reason"):
            raise ValueError("Recent regime match has no explicit user-facing reason.")
        if analog.get("confidence") == "EXCLUDED":
            raise ValueError("Recent regime match cannot carry confidence=EXCLUDED.")
    for horizon in (f"{months}m" for months in HORIZONS):
        for asset in OUTCOME_ASSETS:
            metrics = result["dispersion"][horizon][asset]
            if set(metrics) != {"count", "median", "lower_quartile", "upper_quartile"}:
                raise ValueError("Dispersion schema is incomplete.")
    assertions = result.get("validation_assertions") or {}
    required_assertions = {
        "main_analogs_have_non_excluded_confidence",
        "excluded_confidence_only_in_excluded_collections",
        "every_excluded_object_has_exclusion_reason",
        "recent_matches_not_in_main_analogs",
        "main_analogs_have_3m_and_6m_outcomes",
        "historical_methodology_matches_actual_selection",
    }
    if any(assertions.get(name) is not True for name in required_assertions):
        raise ValueError("Historical analog validation assertions are incomplete or failing.")


def build_historical_analogs_candidate(
    snapshot_date: date,
    *,
    candidate_id: str,
    root: Path = ROOT_DIR,
    refresh_series: bool = False,
    http_get: Callable[..., requests.Response] = requests.get,
    validator: Callable[[dict[str, Any]], None] = validate_historical_analogs_v2,
) -> tuple[dict[str, Any], HistoricalAnalogCandidatePaths]:
    """Stage, validate and atomically replace only a candidate analog directory."""
    paths = candidate_analog_paths(candidate_id, root=root)
    legacy = analog_paths(snapshot_date, root=root)
    legacy_before = _sha256_path(legacy.latest_json)
    candidate_before = _sha256_path(paths.result_json)
    stage: Path | None = None
    backup: Path | None = None
    try:
        if refresh_series:
            series, source_metadata = download_market_series(snapshot_date, http_get=http_get)
        else:
            if not legacy.series_csv.is_file():
                raise FileNotFoundError("Local historical market series is unavailable.")
            series = pd.read_csv(legacy.series_csv, dtype={"period": str}).set_index("period")
            source_metadata = (
                json.loads(legacy.sources_json.read_text(encoding="utf-8"))
                if legacy.sources_json.is_file()
                else {"sources": SOURCE_METADATA, "snapshot_cutoff": snapshot_date.isoformat()}
            )
        result = build_historical_analogs(series, snapshot_date)
        result["candidate_id"] = candidate_id
        result["candidate_status"] = "QUARANTINED"
        result["analyst_review_status"] = "NOT_REVIEWED"
        result["legacy_latest_before_sha256"] = legacy_before
        result["input_series_sha256"] = _sha256_path(legacy.series_csv) if not refresh_series else "REFRESHED"
        result["methodology_config_sha256"] = _sha256_path(V2_CONFIG_PATH)
        result["source_assessment_sha256"] = _sha256_path(HIGH_YIELD_ASSESSMENT_PATH)
        result["source_metadata"] = source_metadata
        validator(result)

        paths.directory.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".historical-analogs-v2-", dir=paths.directory.parent))
        artifacts = {
            "historical_analogs_v2.json": json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            "historical_analogs_main.csv": _candidate_records_csv(result["analogs"]),
            "historical_analogs_recent_incomplete.csv": _candidate_records_csv(result["recent_incomplete"]),
            "historical_analogs_critical_excluded.csv": _candidate_records_csv(result["critical_excluded"]),
            "historical_analogs_dispersion.csv": _dispersion_csv(result["dispersion"]),
            "high_yield_credit_source_assessment.json": (
                json.dumps(result["credit_spread_source_assessment"], ensure_ascii=False, indent=2) + "\n"
            ),
        }
        for name, content in artifacts.items():
            (stage / name).write_text(content, encoding="utf-8")
        manifest = {
            "schema_version": "1.0",
            "candidate_id": candidate_id,
            "candidate_status": "QUARANTINED",
            "analyst_review_status": "NOT_REVIEWED",
            "files": {
                name: {"sha256": _sha256_path(stage / name), "byte_size": (stage / name).stat().st_size}
                for name in sorted(artifacts)
            },
        }
        manifest["aggregate_sha256"] = hashlib.sha256(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        (stage / "artifact_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        for name, metadata in manifest["files"].items():
            if _sha256_path(stage / name) != metadata["sha256"]:
                raise ValueError(f"Staged candidate artifact hash mismatch: {name}")

        if paths.directory.exists():
            backup = paths.directory.with_name(paths.directory.name + ".previous")
            if backup.exists():
                shutil.rmtree(backup)
            paths.directory.rename(backup)
        stage.rename(paths.directory)
        stage = None
        if backup is not None:
            shutil.rmtree(backup)
            backup = None
        if _sha256_path(legacy.latest_json) != legacy_before:
            raise RuntimeError("Legacy latest analog artifact changed during candidate build.")
        return result, paths
    except Exception as exc:
        if stage is not None and stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        if backup is not None and backup.exists() and not paths.directory.exists():
            backup.rename(paths.directory)
        diagnostics = {
            "status": "FAILED",
            "candidate_id": candidate_id,
            "error_type": type(exc).__name__,
            "prior_candidate_sha256": candidate_before,
            "prior_candidate_preserved": _sha256_path(paths.result_json) == candidate_before,
            "legacy_latest_sha256": _sha256_path(legacy.latest_json),
            "legacy_latest_preserved": _sha256_path(legacy.latest_json) == legacy_before,
        }
        _atomic_write(paths.failed_diagnostics_json, json.dumps(diagnostics, indent=2, sort_keys=True) + "\n")
        raise


def build_release_historical_analog_artifact(snapshot_date: date, *, root: Path = ROOT_DIR) -> dict[str, Any]:
    paths = analog_paths(snapshot_date, root=root)
    if not paths.series_csv.is_file():
        raise FileNotFoundError("Local historical market series is unavailable.")
    series = pd.read_csv(paths.series_csv, dtype={"period": str}).set_index("period")
    result = build_historical_analogs(series, snapshot_date)
    if paths.sources_json.is_file():
        result["source_metadata"] = json.loads(paths.sources_json.read_text(encoding="utf-8"))
    validate_historical_analogs_v2(result)
    return result


def build_and_publish_historical_analogs(
    snapshot_date: date,
    *,
    root: Path = ROOT_DIR,
    refresh_series: bool = False,
    http_get: Callable[..., requests.Response] = requests.get,
) -> tuple[dict[str, Any], HistoricalAnalogPaths]:
    raise RuntimeError(
        "Direct latest analog publication is forbidden; use build_historical_analogs_candidate()."
    )


def rebuild_historical_analogs_safely(snapshot_date: date, *, root: Path = ROOT_DIR) -> dict[str, Any]:
    """Refresh into quarantine; preserve the previous valid latest on every path."""
    try:
        result, paths = build_historical_analogs_candidate(
            snapshot_date,
            candidate_id=f"{snapshot_date.isoformat()}-historical-analogs-refresh",
            root=root,
            refresh_series=True,
        )
        return {
            "status": "PASS",
            "candidate_status": "QUARANTINED",
            "analogs": len(result["analogs"]),
            "path": str(paths.result_json),
        }
    except Exception as exc:  # noqa: BLE001 - the core MAE publish must remain available
        return {
            "status": "WARNING",
            "message": f"Historical analog refresh failed ({type(exc).__name__}); previous valid output retained.",
        }


def load_historical_analogs(*, root: Path = ROOT_DIR) -> dict[str, Any]:
    path = root / "outputs" / "mae_historical_analogs_latest.json"
    if not path.exists():
        return {"status": "UNAVAILABLE", "mode": "UNAVAILABLE", "analogs": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "UNAVAILABLE", "mode": "UNAVAILABLE", "analogs": []}
    if payload.get("status") != "PASS" or not payload.get("analogs"):
        return {"status": "UNAVAILABLE", "mode": "UNAVAILABLE", "analogs": []}
    return payload


def _download_fred_series(
    series_id: str,
    start: date,
    end: date,
    *,
    http_get: Callable[..., requests.Response],
) -> pd.Series:
    url = (
        "https://fred.stlouisfed.org/graph/fredgraph.csv"
        f"?id={series_id}&cosd={start.isoformat()}&coed={end.isoformat()}"
    )
    # FRED's graph endpoint can stall on unknown bot-style user agents; a plain
    # curl-compatible agent is both transparent and reliably accepted.
    response = http_get(url, timeout=30, headers={"User-Agent": "curl/8.7.1"})
    response.raise_for_status()
    content = response.content
    if content[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            candidates = [name for name in archive.namelist() if name.endswith(".csv")]
            if not candidates:
                raise ValueError(f"FRED archive for {series_id} contains no CSV.")
            content = archive.read(candidates[0])
    data = pd.read_csv(io.BytesIO(content), na_values=".")
    date_column = "observation_date" if "observation_date" in data else "DATE"
    value_column = series_id if series_id in data else data.columns[-1]
    data[date_column] = pd.to_datetime(data[date_column], errors="coerce")
    data[value_column] = pd.to_numeric(data[value_column], errors="coerce")
    data = data.dropna(subset=[date_column]).set_index(date_column)[value_column]
    data = data.loc[data.index.date <= end]
    return data.groupby(data.index.to_period("M")).last()


def _download_market_prices(
    ticker: str,
    start: date,
    end: date,
    *,
    http_get: Callable[..., requests.Response],
) -> pd.Series:
    start_ts = int(datetime.combine(start, time.min, tzinfo=timezone.utc).timestamp())
    end_ts = int(datetime.combine(end, time.max, tzinfo=timezone.utc).timestamp()) + 1
    url = (
        f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}"
        f"?period1={start_ts}&period2={end_ts}&interval=1d&events=div%2Csplits&includeAdjustedClose=true"
    )
    response = http_get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0 MAE-Historical-Analogs/1.0"})
    response.raise_for_status()
    payload = response.json()
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not result:
        raise ValueError(f"Market price history unavailable for {ticker}.")
    timestamps = result.get("timestamp") or []
    indicators = result.get("indicators") or {}
    adjusted = ((indicators.get("adjclose") or [{}])[0]).get("adjclose")
    if adjusted is None:
        adjusted = ((indicators.get("quote") or [{}])[0]).get("close") or []
    data = pd.Series(
        adjusted,
        index=pd.to_datetime(timestamps, unit="s", utc=True).tz_convert(None),
        dtype=float,
        name=ticker,
    ).dropna()
    data = data.loc[data.index.date <= end]
    return data.groupby(data.index.to_period("M")).last()


def _normalize_series_frame(series: pd.DataFrame, snapshot_date: date) -> pd.DataFrame:
    frame = series.copy()
    if "period" in frame.columns:
        frame = frame.set_index("period")
    if not isinstance(frame.index, pd.PeriodIndex):
        frame.index = pd.PeriodIndex(frame.index.astype(str), freq="M")
    frame = frame.sort_index().loc[: pd.Period(snapshot_date, freq="M")]
    for column in set(FRED_SERIES) | set(MARKET_SERIES):
        if column not in frame:
            frame[column] = pd.NA
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    macro = list(FRED_SERIES)
    frame[macro] = frame[macro].ffill(limit=2)
    return frame


def _trailing_zscores(features: pd.DataFrame) -> pd.DataFrame:
    means = features.rolling(window=60, min_periods=24).mean()
    stds = features.rolling(window=60, min_periods=24).std(ddof=0).replace(0, pd.NA)
    return features.sub(means).div(stds).replace([math.inf, -math.inf], pd.NA).astype(float)


def _main_differences(current: pd.Series, analog: pd.Series, distances: dict[str, float]) -> list[str]:
    current_labels = describe_regime(current)
    analog_labels = describe_regime(analog)
    changed = [
        key
        for key, _distance in sorted(distances.items(), key=lambda item: item[1], reverse=True)
        if current_labels.get(key) != analog_labels.get(key)
    ][:2]
    if not changed:
        return ["No major categorical regime difference across the available factors."]
    return [
        f"{FACTOR_CONFIG[key]['label']}: {analog_labels[key]} then versus {current_labels[key].lower()} now."
        for key in changed
    ]


def _regime_sentence(row: pd.Series) -> str:
    labels = list(describe_regime(row).values())
    return "; ".join(labels[:3]) + "."


def _nullable_float(value: Any) -> float | None:
    return round(float(value), 4) if pd.notna(value) else None


def _candidate_records_csv(records: list[dict[str, Any]]) -> str:
    base_fields = [
        "rank",
        "period",
        "date",
        "regime_name",
        "raw_similarity",
        "factor_coverage",
        "adjusted_similarity",
        "display_similarity",
        "critical_factor_state",
        "confidence",
        "missing_critical_factors",
        "missing_factors",
        "missing_window_columns",
        "limitations",
        "exclusion_reason",
        "incomplete_reasons",
    ]
    outcome_fields = [
        f"{months}m_{asset}"
        for months in HORIZONS
        for asset in OUTCOME_ASSETS
    ]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[*base_fields, *outcome_fields], extrasaction="ignore")
    writer.writeheader()
    for record in records:
        row = {key: record.get(key, "") for key in base_fields}
        for key in (
            "missing_critical_factors",
            "missing_factors",
            "missing_window_columns",
            "limitations",
            "incomplete_reasons",
        ):
            row[key] = " | ".join(record.get(key) or [])
        for months in HORIZONS:
            for asset in OUTCOME_ASSETS:
                value = record.get("forward_returns", {}).get(f"{months}m", {}).get(asset)
                row[f"{months}m_{asset}"] = "" if value is None else value
        writer.writerow(row)
    return output.getvalue()


def _dispersion_csv(dispersion: dict[str, dict[str, dict[str, Any]]]) -> str:
    output = io.StringIO()
    fields = ["horizon", "asset", "count", "median", "lower_quartile", "upper_quartile"]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for horizon in (f"{months}m" for months in HORIZONS):
        for asset in OUTCOME_ASSETS:
            writer.writerow({"horizon": horizon, "asset": asset, **dispersion[horizon][asset]})
    return output.getvalue()


def _sha256_path(path: Path) -> str:
    if not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
