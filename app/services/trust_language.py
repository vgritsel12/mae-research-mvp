from __future__ import annotations

from typing import Any


_TEXT_REPLACEMENTS = {
    "требуется проверка аналитиком": "требуется дополнительное подтверждение источниками",
    "требуется финансовая проверка": "требуется дополнительное подтверждение источниками",
    "duration signal requires human review": "duration signal requires additional source confirmation",
    "commodity signal requires human review": "commodity signal requires additional source confirmation",
}


def sanitize_trust_language(value: Any) -> Any:
    """Remove wording that implies a human decision when output is automated."""

    if isinstance(value, str):
        text = value
        for old, new in _TEXT_REPLACEMENTS.items():
            text = text.replace(old, new)
        return text
    if isinstance(value, list):
        return [sanitize_trust_language(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_trust_language(item) for item in value)
    if isinstance(value, dict):
        return {key: sanitize_trust_language(item) for key, item in value.items()}
    return value
