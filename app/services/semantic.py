from __future__ import annotations

from typing import Any

from app.domain.enums import Direction


HAWKISH_TERMS = (
    "hawkish",
    "higher-for-longer",
    "higher for longer",
    "higher rates",
    "rate hike",
    "rate hikes",
    "hike",
    "hikes",
    "tighten",
    "tightening",
    "restrictive",
    "raise rates",
    "raised rates",
    "persistent inflation",
    "sticky inflation",
    "ужесточ",
    "повышение ставок",
    "повысить ставки",
    "инфляция остаётся высокой",
    "инфляция остается высокой",
)

DOVISH_TERMS = (
    "dovish",
    "rate cut",
    "rate cuts",
    "cuts",
    "cutting rates",
    "lower rates",
    "lower policy rates",
    "easing",
    "softened inflation",
    "disinflation",
    "снижение ставок",
    "снижать ставки",
    "смягчение",
    "дезинфляц",
)

FACTUAL_BASIS_TERMS = HAWKISH_TERMS + DOVISH_TERMS + (
    "inflation",
    "wage",
    "wages",
    "policy rate",
    "policy-rate",
    "yield",
    "yields",
    "earnings",
    "eps",
    "margin",
    "guidance",
    "real yield",
    "central bank",
    "etf",
    "dollar",
    "risk",
    "valuation",
    "инфляц",
    "ставк",
    "доходност",
    "прибыл",
    "маржин",
    "прогноз",
)


def scenario_consistency_warnings(view: Any, scenarios: list[Any]) -> list[str]:
    warnings: list[str] = []
    if not _is_fixed_income_short(view):
        return warnings
    source_text = _view_evidence_text(view)
    base_text = " ".join(_scenario_text(s) for s in scenarios if _scenario_type_value(s) == "BASE")
    if not base_text:
        base_text = " ".join(_scenario_text(s) for s in scenarios)
    source_is_hawkish = _has_hawkish_bias(source_text)
    source_is_dovish = _has_dovish_bias(source_text)
    base_is_hawkish = _has_hawkish_bias(base_text)
    base_is_dovish = _has_dovish_bias(base_text)
    direction = getattr(view, "direction", "")
    if (direction == Direction.BEARISH.value or source_is_hawkish) and base_is_dovish and not source_is_dovish:
        warnings.append("Базовый сценарий говорит о снижении ставок, хотя цитаты поддерживают hawkish/higher-for-longer вывод.")
    if (direction == Direction.BULLISH.value or source_is_dovish) and base_is_hawkish and not source_is_hawkish:
        warnings.append("Базовый сценарий говорит об ужесточении, хотя цитаты поддерживают dovish/cuts вывод.")
    return warnings


def signal_consistency_warnings(view: Any, signal_payload: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    quote_warnings = evidence_quote_warnings(getattr(view, "evidence_quotes", []), bool(getattr(view, "is_demo", False)))
    warnings.extend(quote_warnings)
    if not _is_fixed_income_short(view):
        return warnings
    quote_text = _view_evidence_text(view)
    strength = int(signal_payload.get("suggested_strength") or 0)
    if strength > 0 and _has_hawkish_bias(quote_text) and not _has_dovish_bias(quote_text):
        warnings.append("Положительный сигнал по краткосрочным облигациям не поддержан hawkish/higher-for-longer цитатой.")
    if strength < 0 and _has_dovish_bias(quote_text) and not _has_hawkish_bias(quote_text):
        warnings.append("Отрицательный сигнал по краткосрочным облигациям не поддержан dovish/cuts цитатой.")
    return warnings


def evidence_quote_warnings(quotes: list[dict[str, Any]], is_demo: bool = False) -> list[str]:
    if is_demo:
        return []
    if not quotes:
        return ["Нет подтверждающей цитаты из публикации."]
    warnings: list[str] = []
    valid = False
    for item in quotes:
        quote = str(item.get("quote") or "")
        locator = str(item.get("locator") or "").casefold()
        if "title" in locator or "first sentence" in locator or "first line" in locator:
            warnings.append("Цитата из заголовка или первой строки не считается достаточным подтверждением.")
            continue
        if len(quote.strip()) < 35:
            warnings.append("Подтверждающая цитата слишком короткая.")
            continue
        if not any(term in quote.casefold() for term in FACTUAL_BASIS_TERMS):
            warnings.append("Подтверждающая цитата не содержит фактической основы для вывода.")
            continue
        valid = True
    if not valid and not warnings:
        warnings.append("Не найдена фактическая цитата для существенного вывода.")
    return warnings


def _scenario_text(scenario: Any) -> str:
    parts = [
        getattr(scenario, "title", ""),
        getattr(scenario, "description", ""),
        " ".join(getattr(scenario, "assumptions", []) or []),
        " ".join(getattr(scenario, "triggers", []) or []),
        getattr(scenario, "expected_reaction", ""),
        " ".join(getattr(scenario, "reversal_conditions", []) or []),
    ]
    return " ".join(str(part) for part in parts).casefold()


def _scenario_type_value(scenario: Any) -> str:
    raw = getattr(scenario, "scenario_type", "")
    return str(getattr(raw, "value", raw))


def _view_evidence_text(view: Any) -> str:
    quotes = getattr(view, "evidence_quotes", []) or []
    quote_text = " ".join(str(item.get("quote", "")) for item in quotes if isinstance(item, dict))
    drivers = " ".join(getattr(view, "drivers", []) or [])
    risks = " ".join(getattr(view, "risks", []) or [])
    return f"{quote_text} {drivers} {risks}".casefold()


def _is_fixed_income_short(view: Any) -> bool:
    row_key = str(getattr(view, "template_row_key", "")).upper()
    return row_key.startswith("FIXED INCOME|GOV|SHORT TERM")


def _has_hawkish_bias(text: str) -> bool:
    return _term_hits(text, HAWKISH_TERMS) > _term_hits(text, DOVISH_TERMS)


def _has_dovish_bias(text: str) -> bool:
    return _term_hits(text, DOVISH_TERMS) > _term_hits(text, HAWKISH_TERMS)


def _term_hits(text: str, terms: tuple[str, ...]) -> int:
    lowered = text.casefold()
    return sum(lowered.count(term.casefold()) for term in terms)
