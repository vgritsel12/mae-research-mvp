from __future__ import annotations

from sqlalchemy.orm import Session

from app.domain.models import AppLog


def record_log(session: Session, level: str, event: str, message: str, context: dict | None = None) -> AppLog:
    row = AppLog(level=level.upper(), event=event, message=message[:2000], context=context or {})
    session.add(row)
    session.flush()
    return row

