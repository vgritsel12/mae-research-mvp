from __future__ import annotations

from sqlalchemy import Connection, inspect, text

from app.domain.models import AutonomousReleaseRecord


REVISION = "20260714_autonomous_mae_release"


def upgrade(connection: Connection) -> dict[str, int]:
    """Add the autonomous channel without modifying legacy governance rows."""

    existed = AutonomousReleaseRecord.__tablename__ in connection.dialect.get_table_names(connection)
    AutonomousReleaseRecord.__table__.create(bind=connection, checkfirst=True)
    columns = {item["name"] for item in inspect(connection).get_columns(AutonomousReleaseRecord.__tablename__)}
    if "baseline_manifest_hash" not in columns:
        connection.execute(
            text(
                "ALTER TABLE autonomous_release_records "
                "ADD COLUMN baseline_manifest_hash VARCHAR(64) NOT NULL DEFAULT ''"
            )
        )
    return {"created": 0 if existed else 1, "existing": 1 if existed else 0}


def downgrade(connection: Connection) -> None:
    del connection
    raise RuntimeError("Destructive downgrade is unsupported for autonomous release history.")


def postgres_ddl() -> dict[str, str]:
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    return {
        AutonomousReleaseRecord.__tablename__: str(
            CreateTable(AutonomousReleaseRecord.__table__).compile(
                dialect=postgresql.dialect()
            )
        )
    }
