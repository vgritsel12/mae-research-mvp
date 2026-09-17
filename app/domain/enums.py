from __future__ import annotations

from enum import StrEnum


class Direction(StrEnum):
    BULLISH = "BULLISH"
    NEUTRAL = "NEUTRAL"
    BEARISH = "BEARISH"


class SignalDirection(StrEnum):
    POSITIVE = "POSITIVE"
    NEUTRAL = "NEUTRAL"
    NEGATIVE = "NEGATIVE"


class Confidence(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class Materiality(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    NO_MATERIAL_CHANGE = "NO_MATERIAL_CHANGE"


class IdeaState(StrEnum):
    NEW_IDEA = "NEW_IDEA"
    MATERIAL_SHIFT = "MATERIAL_SHIFT"
    NO_MATERIAL_CHANGE = "NO_MATERIAL_CHANGE"


class ScenarioType(StrEnum):
    BASE = "BASE"
    UPSIDE = "UPSIDE"
    DOWNSIDE = "DOWNSIDE"


class EvidenceStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    MIXED = "MIXED"
    CONTRADICTED = "CONTRADICTED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    INVALIDATED = "INVALIDATED"


class CoverageStatus(StrEnum):
    NO_DATA = "NO_DATA"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    COVERED = "COVERED"
    STALE = "STALE"


class PricingStatus(StrEnum):
    NOT_PRICED = "NOT_PRICED"
    PARTLY_PRICED = "PARTLY_PRICED"
    PRICED_IN = "PRICED_IN"
    UNKNOWN = "UNKNOWN"


class AnalystStatus(StrEnum):
    NEW = "NEW"
    REVIEWED = "REVIEWED"
    APPROVED = "APPROVED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    REJECTED = "REJECTED"
    INVALIDATED = "INVALIDATED"


class ExtractionMethod(StrEnum):
    RULE_BASED = "RULE_BASED"
    LLM = "LLM"
    MANUAL = "MANUAL"
    DEMO = "DEMO"


class FetchStatus(StrEnum):
    DISCOVERED = "DISCOVERED"
    FETCHED = "FETCHED"
    PARSED = "PARSED"
    ANALYSED = "ANALYSED"
    BLOCKED = "BLOCKED"
    MANUAL_TEXT = "MANUAL_TEXT"
    MANUAL_REQUIRED = "MANUAL_REQUIRED"
    NEEDS_MANUAL_REVIEW = "NEEDS_MANUAL_REVIEW"
    INVALID = "INVALID"
    DUPLICATE = "DUPLICATE"
    ERROR = "ERROR"


class UpdateStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    FAILED = "FAILED"


class RunMode(StrEnum):
    DEMO = "DEMO"
    REAL = "REAL"
    ALL = "ALL"


class TechnicalValidationStatus(StrEnum):
    NOT_VALIDATED = "NOT_VALIDATED"
    PASSED = "PASSED"
    FAILED = "FAILED"


class FinancialValidationStatus(StrEnum):
    NOT_REVIEWED = "NOT_REVIEWED"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"
    PASSED = "PASSED"
    FAILED = "FAILED"


class AnalystReviewStatus(StrEnum):
    NOT_REVIEWED = "NOT_REVIEWED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class FinancialReleaseStatus(StrEnum):
    QUARANTINED = "QUARANTINED"
    ELIGIBLE = "ELIGIBLE"
    RELEASED = "RELEASED"
    REJECTED = "REJECTED"


class ReviewActorType(StrEnum):
    NONE = "NONE"
    HUMAN = "HUMAN"
    AUTOMATION = "AUTOMATION"


class ScoreStatus(StrEnum):
    ACTIVE = "ACTIVE"
    CARRY_FORWARD = "CARRY_FORWARD"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    REJECTED = "REJECTED"


# Autonomous MAE v2 statuses deliberately live beside the legacy, human-gated
# financial-release statuses.  The old types remain the contract for historical
# audit records; these types describe only the autonomous publication channel.
class AutonomousTechnicalStatus(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"


class ModelValidationStatus(StrEnum):
    PASSED = "PASSED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class AutonomousReleaseStatus(StrEnum):
    DRAFT = "DRAFT"
    AUTO_PUBLISHED = "AUTO_PUBLISHED"
    BLOCKED = "BLOCKED"


class HumanReviewStatus(StrEnum):
    NOT_REQUESTED = "NOT_REQUESTED"
    OPTIONAL_REVIEW = "OPTIONAL_REVIEW"
    REVIEWED = "REVIEWED"


class AutonomousEvidenceMode(StrEnum):
    DIRECT = "DIRECT"
    COMPOSITE = "COMPOSITE"
    MODEL_INFERRED = "MODEL_INFERRED"
    CARRY_FORWARD = "CARRY_FORWARD"


RU_STATUS = {
    "BULLISH": "позитивно",
    "NEUTRAL": "нейтрально",
    "BEARISH": "негативно",
    "POSITIVE": "положительный",
    "NEGATIVE": "отрицательный",
    "LOW": "низкая",
    "MEDIUM": "средняя",
    "HIGH": "высокая",
    "NEW_IDEA": "новая идея",
    "MATERIAL_SHIFT": "материальное изменение",
    "NO_MATERIAL_CHANGE": "нет материального изменения",
    "CONFIRMED": "поддерживается данными",
    "MIXED": "смешанные подтверждения",
    "CONTRADICTED": "противоречит данным",
    "INSUFFICIENT_DATA": "недостаточно данных",
    "INSUFFICIENT_EVIDENCE": "недостаточно подтверждений",
    "INVALIDATED": "исключено из расчёта",
    "NO_DATA": "нет данных",
    "NOT_APPLICABLE": "не применяется",
    "COVERED": "покрыто",
    "STALE": "устарело",
    "NOT_PRICED": "не учтено",
    "PARTLY_PRICED": "частично учтено",
    "PRICED_IN": "уже учтено",
    "UNKNOWN": "неизвестно",
    "NEW": "новый",
    "REVIEWED": "обработано",
    "APPROVED": "опубликовано",
    "NEEDS_REVIEW": "недостаточно данных",
    "REJECTED": "отклонено",
    "DEMO": "DEMO",
    "REAL": "REAL",
    "ALL": "ALL",
}


def ru(value: object) -> str:
    raw = getattr(value, "value", str(value))
    return RU_STATUS.get(raw, raw)
