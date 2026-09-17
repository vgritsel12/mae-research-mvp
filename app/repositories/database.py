from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from importlib import import_module

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.domain.models import Base


def create_app_engine(settings: Settings | None = None) -> Engine:
    settings = settings or get_settings()
    connect_args = {"check_same_thread": False} if settings.sqlalchemy_url.startswith("sqlite") else {}
    return create_engine(settings.sqlalchemy_url, connect_args=connect_args, pool_pre_ping=True, future=True)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)


def init_database(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    _apply_lightweight_migrations(engine)


def _apply_lightweight_migrations(engine: Engine) -> None:
    with engine.begin() as conn:
        dialect = engine.dialect.name
        _ensure_text_column(conn, dialect, "research_views", "canonical_cell_id", "VARCHAR(120) NOT NULL DEFAULT ''")
        _ensure_text_column(conn, dialect, "research_views", "legacy_review_status", "VARCHAR(40) NOT NULL DEFAULT ''")
        _ensure_text_column(conn, dialect, "research_views", "strict_review_status", "VARCHAR(40) NOT NULL DEFAULT ''")
        _ensure_text_column(conn, dialect, "research_views", "valid_from", "DATE")
        _ensure_text_column(conn, dialect, "research_views", "valid_until", "DATE")
        _ensure_text_column(conn, dialect, "research_views", "document_type", "VARCHAR(40) NOT NULL DEFAULT 'ASSET_OUTLOOK'")
        _ensure_text_column(conn, dialect, "research_views", "investment_horizon", "VARCHAR(80) NOT NULL DEFAULT ''")
        _ensure_text_column(conn, dialect, "research_views", "lifecycle_status", "VARCHAR(40) NOT NULL DEFAULT 'ACTIVE'")
        _ensure_text_column(conn, dialect, "research_views", "superseded_by", "VARCHAR(36) NOT NULL DEFAULT ''")
        _ensure_text_column(conn, dialect, "research_views", "invalidation_reason", "TEXT NOT NULL DEFAULT ''")
        _ensure_text_column(conn, dialect, "research_views", "last_confirmed_at", "TIMESTAMP WITH TIME ZONE")
        _ensure_text_column(conn, dialect, "research_views", "freshness_weight", "FLOAT NOT NULL DEFAULT 1.0")
        _ensure_text_column(conn, dialect, "shift_signals", "canonical_cell_id", "VARCHAR(120) NOT NULL DEFAULT ''")
        _ensure_text_column(conn, dialect, "baseline_scores", "canonical_cell_id", "VARCHAR(120) NOT NULL DEFAULT ''")
        _ensure_text_column(conn, dialect, "mae_component_cells", "benchmark", "VARCHAR(255) NOT NULL DEFAULT ''")
        _ensure_text_column(conn, dialect, "mae_component_cells", "validation_status", "VARCHAR(40) NOT NULL DEFAULT ''")
        _ensure_text_column(conn, dialect, "mae_component_cells", "validation_comment", "TEXT NOT NULL DEFAULT ''")
        _ensure_text_column(conn, dialect, "mae_snapshot_cells", "asset", "VARCHAR(255) NOT NULL DEFAULT ''")
        _ensure_text_column(conn, dialect, "mae_snapshot_cells", "thesis", "TEXT NOT NULL DEFAULT ''")
        _ensure_text_column(conn, dialect, "mae_snapshot_cells", "driver", "TEXT NOT NULL DEFAULT ''")
        _ensure_text_column(conn, dialect, "mae_snapshot_cells", "previous_thesis", "TEXT NOT NULL DEFAULT ''")
        _ensure_text_column(conn, dialect, "mae_snapshot_cells", "previous_driver", "TEXT NOT NULL DEFAULT ''")
        _ensure_text_column(conn, dialect, "mae_snapshot_cells", "evidence_status", "VARCHAR(40) NOT NULL DEFAULT 'MIXED'")
        _ensure_text_column(conn, dialect, "mae_snapshot_cells", "confidence", "VARCHAR(30) NOT NULL DEFAULT 'MEDIUM'")
        _ensure_text_column(conn, dialect, "mae_snapshot_cells", "carry_forward", "BOOLEAN NOT NULL DEFAULT 0")
        _ensure_text_column(conn, dialect, "mae_snapshot_cells", "scenario_link", "VARCHAR(80) NOT NULL DEFAULT 'Base'")
        _ensure_text_column(conn, dialect, "mae_snapshot_cells", "change_status", "VARCHAR(60) NOT NULL DEFAULT 'UNCHANGED'")
        _ensure_text_column(conn, dialect, "mae_snapshot_cells", "change_summary", "TEXT NOT NULL DEFAULT ''")
        _ensure_text_column(conn, dialect, "evidence_items", "source_class", "VARCHAR(60) NOT NULL DEFAULT 'ALLOWLIST'")
        _ensure_text_column(conn, dialect, "evidence_items", "relevance_reason", "TEXT NOT NULL DEFAULT ''")
        _ensure_text_column(conn, dialect, "evidence_items", "related_indicator", "VARCHAR(255) NOT NULL DEFAULT ''")
        _ensure_text_column(conn, dialect, "evidence_items", "actual_value", "TEXT NOT NULL DEFAULT ''")
        _ensure_text_column(conn, dialect, "evidence_items", "expected_value", "TEXT NOT NULL DEFAULT ''")
        _ensure_text_column(conn, dialect, "evidence_items", "market_confirmation", "TEXT NOT NULL DEFAULT ''")
        _ensure_text_column(conn, dialect, "evidence_items", "conflict", "TEXT NOT NULL DEFAULT ''")
        _ensure_text_column(conn, dialect, "evidence_items", "evidence_status", "VARCHAR(40) NOT NULL DEFAULT 'MIXED'")
        _ensure_text_column(conn, dialect, "evidence_items", "review_status", "VARCHAR(40) NOT NULL DEFAULT 'PASS'")
        _ensure_text_column(conn, dialect, "evidence_items", "carry_forward", "BOOLEAN NOT NULL DEFAULT 0")
        _ensure_text_column(conn, dialect, "market_scenarios", "snapshot_date", "DATE")
        _ensure_text_column(conn, dialect, "market_scenarios", "causal_chain", "TEXT NOT NULL DEFAULT ''")
        _ensure_text_column(conn, dialect, "market_scenarios", "macro_drivers", "JSON NOT NULL DEFAULT '[]'")
        _ensure_text_column(conn, dialect, "market_scenarios", "indicators", "JSON NOT NULL DEFAULT '[]'")
        _ensure_text_column(conn, dialect, "market_scenarios", "affected_cells", "JSON NOT NULL DEFAULT '[]'")
        _ensure_text_column(conn, dialect, "market_scenarios", "expected_reaction_by_cell", "JSON NOT NULL DEFAULT '{}'")
        _ensure_text_column(conn, dialect, "market_scenarios", "last_review_date", "DATE")
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_market_scenario_snapshot_type_idx "
                "ON market_scenarios(snapshot_date, scenario_type, is_demo) "
                "WHERE snapshot_date IS NOT NULL"
            )
        )
        governance_migration = import_module("migrations.versions.20260714_financial_release_governance")
        governance_migration.upgrade(conn)
        autonomous_migration = import_module("migrations.versions.20260714_autonomous_mae_release")
        autonomous_migration.upgrade(conn)
        atomic_release_migration = import_module("migrations.versions.20260714_atomic_release")
        atomic_release_migration.upgrade(conn)
        _migrate_matrix_scores(conn, dialect)
        if dialect == "sqlite":
            conn.execute(
                text(
                    """
                    DELETE FROM scenario_cards
                    WHERE linked_change_id IS NOT NULL
                      AND rowid NOT IN (
                        SELECT MIN(rowid)
                        FROM scenario_cards
                        WHERE linked_change_id IS NOT NULL
                        GROUP BY linked_change_id, scenario_type
                      )
                    """
                )
            )
        elif dialect == "postgresql":
            conn.execute(
                text(
                    """
                    DELETE FROM scenario_cards s
                    USING (
                        SELECT id,
                               ROW_NUMBER() OVER (
                                 PARTITION BY linked_change_id, scenario_type
                                 ORDER BY created_at ASC, id ASC
                               ) AS rn
                        FROM scenario_cards
                        WHERE linked_change_id IS NOT NULL
                    ) d
                    WHERE s.id = d.id AND d.rn > 1
                    """
                )
            )
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_scenario_change_type_idx "
                "ON scenario_cards(linked_change_id, scenario_type) "
                "WHERE linked_change_id IS NOT NULL"
            )
        )


def _migrate_matrix_scores(conn, dialect: str) -> None:
    if dialect == "sqlite":
        columns = {row[1]: row for row in conn.execute(text("PRAGMA table_info(matrix_scores)")).fetchall()}
        if not columns:
            return
        if "canonical_cell_id" not in columns:
            conn.execute(text("ALTER TABLE matrix_scores ADD COLUMN canonical_cell_id VARCHAR(120) NOT NULL DEFAULT ''"))
            columns = {row[1]: row for row in conn.execute(text("PRAGMA table_info(matrix_scores)")).fetchall()}
        needs_rebuild = (
            columns.get("approved_score") is not None
            and int(columns["approved_score"][3]) == 1
        ) or "coverage_status" not in columns or not _sqlite_matrix_unique_includes_mode(conn)
        if not needs_rebuild:
            conn.execute(
                text(
                    """
                    UPDATE matrix_scores
                    SET suggested_score = NULL,
                        approved_score = NULL,
                        coverage_status = 'NO_DATA'
                    WHERE COALESCE(signal_ids, '[]') = '[]'
                      AND COALESCE(override_reason, '') = ''
                      AND COALESCE(source_count, 0) = 0
                      AND COALESCE(coverage_status, '') <> 'ARCHIVED_TEST_DATA'
                      AND COALESCE(coverage_status, '') NOT IN ('NO_DATA', 'NOT_APPLICABLE')
                    """
                )
            )
            return
        conn.execute(text("DROP TABLE IF EXISTS matrix_scores_new"))
        conn.execute(
            text(
                """
                CREATE TABLE matrix_scores_new (
                    id VARCHAR(36) NOT NULL PRIMARY KEY,
                    template_row_key VARCHAR(400) NOT NULL,
                    canonical_cell_id VARCHAR(120) NOT NULL DEFAULT '',
                    region VARCHAR(80) NOT NULL,
                    suggested_score INTEGER,
                    approved_score INTEGER,
                    override_reason TEXT NOT NULL DEFAULT '',
                    calculation_details JSON NOT NULL DEFAULT '{}',
                    signal_ids JSON NOT NULL DEFAULT '[]',
                    coverage_status VARCHAR(40) NOT NULL DEFAULT 'NO_DATA',
                    source_count INTEGER NOT NULL DEFAULT 0,
                    publication_ids JSON NOT NULL DEFAULT '[]',
                    publication_dates JSON NOT NULL DEFAULT '[]',
                    supporting_evidence JSON NOT NULL DEFAULT '[]',
                    contradicting_evidence JSON NOT NULL DEFAULT '[]',
                    confidence VARCHAR(30) NOT NULL DEFAULT 'LOW',
                    freshness_score FLOAT NOT NULL DEFAULT 0.0,
                    is_demo BOOLEAN NOT NULL DEFAULT 0,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    UNIQUE (template_row_key, region, is_demo)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO matrix_scores_new (
                    id, template_row_key, canonical_cell_id, region, suggested_score, approved_score,
                    override_reason, calculation_details, signal_ids, coverage_status,
                    source_count, publication_ids, publication_dates, supporting_evidence,
                    contradicting_evidence, confidence, freshness_score, is_demo, created_at, updated_at
                )
                SELECT
                    id,
                    template_row_key,
                    COALESCE(canonical_cell_id, ''),
                    region,
                    CASE WHEN COALESCE(signal_ids, '[]') = '[]' AND COALESCE(override_reason, '') = '' THEN NULL ELSE suggested_score END,
                    CASE WHEN COALESCE(signal_ids, '[]') = '[]' AND COALESCE(override_reason, '') = '' THEN NULL ELSE approved_score END,
                    COALESCE(override_reason, ''),
                    COALESCE(calculation_details, '{}'),
                    COALESCE(signal_ids, '[]'),
                    CASE
                        WHEN COALESCE(signal_ids, '[]') = '[]' AND COALESCE(override_reason, '') = '' THEN 'NO_DATA'
                        ELSE 'COVERED'
                    END,
                    0,
                    '[]',
                    '[]',
                    '[]',
                    '[]',
                    'LOW',
                    0.0,
                    COALESCE(is_demo, 0),
                    created_at,
                    updated_at
                FROM matrix_scores
                """
            )
        )
        conn.execute(text("DROP TABLE matrix_scores"))
        conn.execute(text("ALTER TABLE matrix_scores_new RENAME TO matrix_scores"))
        return

    if dialect == "postgresql":
        conn.execute(text("ALTER TABLE matrix_scores ADD COLUMN IF NOT EXISTS canonical_cell_id VARCHAR(120) NOT NULL DEFAULT ''"))
        for column, ddl in {
            "coverage_status": "VARCHAR(40) NOT NULL DEFAULT 'NO_DATA'",
            "source_count": "INTEGER NOT NULL DEFAULT 0",
            "publication_ids": "JSON NOT NULL DEFAULT '[]'",
            "publication_dates": "JSON NOT NULL DEFAULT '[]'",
            "supporting_evidence": "JSON NOT NULL DEFAULT '[]'",
            "contradicting_evidence": "JSON NOT NULL DEFAULT '[]'",
            "confidence": "VARCHAR(30) NOT NULL DEFAULT 'LOW'",
            "freshness_score": "FLOAT NOT NULL DEFAULT 0.0",
        }.items():
            conn.execute(text(f"ALTER TABLE matrix_scores ADD COLUMN IF NOT EXISTS {column} {ddl}"))
        conn.execute(text("ALTER TABLE matrix_scores ALTER COLUMN suggested_score DROP NOT NULL"))
        conn.execute(text("ALTER TABLE matrix_scores ALTER COLUMN approved_score DROP NOT NULL"))
        conn.execute(text("ALTER TABLE matrix_scores DROP CONSTRAINT IF EXISTS uq_matrix_row_region"))
        conn.execute(text("ALTER TABLE matrix_scores DROP CONSTRAINT IF EXISTS uq_matrix_row_region_mode"))
        conn.execute(text("DROP INDEX IF EXISTS uq_matrix_row_region"))
        conn.execute(text("DROP INDEX IF EXISTS uq_matrix_row_region_mode_idx"))
        conn.execute(
            text(
                "CREATE UNIQUE INDEX uq_matrix_row_region_mode_idx "
                "ON matrix_scores(template_row_key, region, is_demo)"
            )
        )
        conn.execute(
            text(
                """
                UPDATE matrix_scores
                SET suggested_score = NULL,
                    approved_score = NULL,
                    coverage_status = 'NO_DATA'
                WHERE COALESCE(signal_ids::text, '[]') = '[]'
                  AND COALESCE(override_reason, '') = ''
                  AND COALESCE(source_count, 0) = 0
                  AND COALESCE(coverage_status, '') <> 'ARCHIVED_TEST_DATA'
                  AND COALESCE(coverage_status, '') NOT IN ('NO_DATA', 'NOT_APPLICABLE')
                """
            )
        )


def _sqlite_matrix_unique_includes_mode(conn) -> bool:
    indexes = conn.execute(text("PRAGMA index_list(matrix_scores)")).fetchall()
    for index in indexes:
        if not bool(index[2]):
            continue
        index_name = index[1]
        index_columns = [row[2] for row in conn.execute(text(f"PRAGMA index_info({index_name})")).fetchall()]
        if index_columns == ["template_row_key", "region", "is_demo"]:
            return True
    return False


def _ensure_text_column(conn, dialect: str, table: str, column: str, ddl: str) -> None:
    if dialect == "sqlite":
        columns = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()}
        if column not in columns:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
    elif dialect == "postgresql":
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl}"))


ENGINE = create_app_engine()
SessionLocal = create_session_factory(ENGINE)


@contextmanager
def session_scope(session_factory: sessionmaker[Session] = SessionLocal) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
