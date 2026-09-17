from __future__ import annotations

from sqlalchemy import Connection

from app.domain.models import AtomicReleasePointer, AtomicReleaseRecord


REVISION = "20260714_atomic_release"


def upgrade(connection: Connection) -> None:
    AtomicReleaseRecord.__table__.create(bind=connection, checkfirst=True)
    AtomicReleasePointer.__table__.create(bind=connection, checkfirst=True)


def downgrade(connection: Connection) -> None:
    del connection
    raise RuntimeError("Destructive downgrade is intentionally unsupported for atomic release tables.")
