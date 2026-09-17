from __future__ import annotations

from sqlalchemy.orm import Session

from app.domain.enums import AnalystStatus
from app.domain.models import AnalystReview, ResearchView, ShiftSignal


ENTITY_MODELS = {
    "research_view": ResearchView,
    "shift_signal": ShiftSignal,
}


def review_entity(
    session: Session,
    entity_type: str,
    entity_id: str,
    new_status: AnalystStatus,
    comment: str,
    reviewer_label: str = "Analyst",
) -> AnalystReview:
    if new_status != AnalystStatus.REVIEWED and not comment.strip():
        raise ValueError("Комментарий обязателен для review/override.")
    model = ENTITY_MODELS.get(entity_type)
    if model is None:
        raise ValueError("Unknown review entity type")
    entity = session.get(model, entity_id)
    if entity is None:
        raise ValueError("Entity not found")
    previous = entity.review_status if hasattr(entity, "review_status") else entity.analyst_status
    if hasattr(entity, "review_status"):
        entity.review_status = new_status.value
    else:
        entity.analyst_status = new_status.value
        entity.analyst_comment = comment
    row = AnalystReview(
        entity_type=entity_type,
        entity_id=entity_id,
        previous_status=previous,
        new_status=new_status.value,
        comment=comment,
        reviewer_label=reviewer_label,
    )
    session.add(row)
    session.flush()
    return row

