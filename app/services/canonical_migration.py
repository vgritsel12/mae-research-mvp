from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.models import ResearchView, ShiftSignal
from app.services.normalization import canonical_cell_id_for, is_applicable_canonical_cell, map_to_canonical_row_key, normalize_region


def sync_canonical_cell_references(session: Session) -> dict[str, int]:
    """Backfill canonical cell ids for rows created before the canonical registry existed."""
    stats = {
        "research_views_updated": 0,
        "research_views_unmapped": 0,
        "shift_signals_updated": 0,
        "shift_signals_unmapped": 0,
    }
    for view in session.scalars(select(ResearchView)).all():
        if _sync_research_view(view):
            stats["research_views_updated"] += 1
        elif not view.canonical_cell_id:
            stats["research_views_unmapped"] += 1
    for signal in session.scalars(select(ShiftSignal)).all():
        if _sync_shift_signal(signal):
            stats["shift_signals_updated"] += 1
        elif not signal.canonical_cell_id:
            stats["shift_signals_unmapped"] += 1
    session.flush()
    return stats


def _sync_research_view(view: ResearchView) -> bool:
    mapped = _canonical_payload(view.asset_class, view.asset_group, view.asset_segment, view.region)
    if mapped is None:
        view.canonical_cell_id = ""
        return False
    row_key, cell_id, region, asset_class, group, segment = mapped
    changed = (
        view.template_row_key != row_key
        or view.canonical_cell_id != cell_id
        or view.region != region
        or view.asset_class != asset_class
        or view.asset_group != group
        or view.asset_segment != segment
    )
    view.template_row_key = row_key
    view.canonical_cell_id = cell_id
    view.region = region
    view.asset_class = asset_class
    view.asset_group = group
    view.asset_segment = segment
    return changed


def _sync_shift_signal(signal: ShiftSignal) -> bool:
    parts = signal.template_row_key.split("|", 2)
    if len(parts) == 3:
        asset_class, group, segment = parts
    else:
        asset_class, group, segment = signal.asset, "", signal.asset
    mapped = _canonical_payload(asset_class, group, segment, signal.region)
    if mapped is None:
        signal.canonical_cell_id = ""
        return False
    row_key, cell_id, region, _asset_class, _group, segment = mapped
    changed = signal.template_row_key != row_key or signal.canonical_cell_id != cell_id or signal.region != region
    signal.template_row_key = row_key
    signal.canonical_cell_id = cell_id
    signal.region = region
    signal.asset = segment
    return changed


def _canonical_payload(
    asset_class: str,
    asset_group: str,
    asset_segment: str,
    region: str,
) -> tuple[str, str, str, str, str, str] | None:
    row_key = map_to_canonical_row_key(asset_class, asset_group, asset_segment)
    normalized_region = normalize_region(region)
    if row_key is None or not is_applicable_canonical_cell(row_key, normalized_region):
        return None
    cell_id = canonical_cell_id_for(row_key, normalized_region)
    if not cell_id:
        return None
    canonical_asset_class, group, segment = row_key.split("|", 2)
    return row_key, cell_id, normalized_region, canonical_asset_class, group, segment
