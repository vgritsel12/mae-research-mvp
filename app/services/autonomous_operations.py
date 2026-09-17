from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import Engine

from app.config import ROOT_DIR, Settings, get_settings
from app.repositories.database import (
    create_app_engine,
    create_session_factory,
    init_database,
)
from app.services.atomic_release import resolve_committed_release
from app.services.autonomous_exports import (
    XLSX_NAME,
    finalize_autonomous_exports,
    prepare_autonomous_exports,
)
from app.services.autonomous_pipeline import run_autonomous_candidate
from app.services.autonomous_release import (
    AUTONOMOUS_CHANNEL,
    publish_autonomous_candidate,
)
from app.services.autonomous_release_gate import require_autonomous_write_access
from app.services.collectors import run_update


ExportBuilder = Callable[[dict[str, Any]], None]
Collector = Callable[..., Any]


@dataclass(frozen=True)
class AutonomousOperationResult:
    success: bool
    status: str
    as_of: str
    candidate_id: str = ""
    candidate_dir: str = ""
    technical_status: str = ""
    model_validation_status: str = ""
    release_status: str = ""
    human_review_status: str = "NOT_REQUESTED"
    applicable_score_count: int = 0
    not_applicable_count: int = 0
    new_source_count: int = 0
    release_id: str = ""
    pointer_release_id: str = ""
    release_manifest_hash: str = ""
    idempotent: bool = False
    artifacts: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    export_manifest: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_autonomous_update(
    *,
    as_of: date,
    settings: Settings | None = None,
    root: Path = ROOT_DIR,
    collect: bool = True,
    publish: bool = True,
    build_xlsx: bool = True,
    export_builder: ExportBuilder | None = None,
    collector: Collector | None = None,
    engine: Engine | None = None,
    today: date | None = None,
    progress: Callable[[str], None] | None = None,
) -> AutonomousOperationResult:
    """Single operation used by UI, CLI and schedule."""

    settings = settings or get_settings()
    today = today or date.today()
    if as_of > today:
        return AutonomousOperationResult(
            success=False,
            status="FUTURE_DATE_REJECTED",
            as_of=as_of.isoformat(),
            errors=("as_of cannot be in the future",),
        )
    # This check deliberately precedes engine creation, migrations and candidate files.
    require_autonomous_write_access(settings)
    if progress is not None:
        progress("Подготавливаю обновление")
    engine = engine or create_app_engine(settings)
    init_database(engine)
    factory = create_session_factory(engine)
    with factory.begin() as session:
        candidate = run_autonomous_candidate(
            session,
            snapshot_date=as_of,
            settings=settings,
            output_root=root / "outputs" / "autonomous" / "candidates",
            collect=collect,
            collector=collector or run_update,
            previous_scores_path=root / "outputs" / "mae_full_latest_scores.csv",
            progress=progress,
        )
    collection_stage = _read_json(Path(candidate["candidate_dir"]) / "stage_01_collection.json")
    if candidate["release_status"] == "BLOCKED":
        errors = tuple(candidate.get("critical_errors") or []) + tuple(
            str(item) for item in candidate.get("model_failures") or []
        )
        if _is_up_to_date_block(errors):
            return _result_from_candidate(
                candidate,
                success=True,
                status="UP_TO_DATE",
                new_source_count=int(collection_stage.get("new_source_count") or 0),
                errors=errors,
            )
        return _result_from_candidate(
            candidate,
            success=False,
            status="BLOCKED",
            new_source_count=int(collection_stage.get("new_source_count") or 0),
            errors=errors,
        )
    if not publish:
        return _result_from_candidate(
            candidate,
            success=True,
            status="CANDIDATE_READY",
            new_source_count=int(collection_stage.get("new_source_count") or 0),
        )

    if progress is not None:
        progress("Публикую обновление")
    release = publish_autonomous_candidate(
        engine=engine,
        settings=settings,
        root=root,
        candidate_dir=Path(candidate["candidate_dir"]),
    )
    resolved = resolve_committed_release(
        engine=engine,
        root=root,
        channel=AUTONOMOUS_CHANNEL,
    )
    export_errors: list[str] = []
    export_manifest: dict[str, Any] = {}
    if progress is not None:
        progress("Готовлю отчёты")
    build_input = prepare_autonomous_exports(engine=engine, root=root)
    export_dir = Path(build_input["export_dir"])
    if build_xlsx and not (export_dir / XLSX_NAME).is_file():
        if export_builder is None:
            export_errors.append("XLSX builder unavailable; JSON/CSV exports were prepared.")
        else:
            try:
                export_builder(build_input)
            except Exception as exc:  # noqa: BLE001 - release remains valid; export failure is explicit
                export_errors.append(f"XLSX build failed: {type(exc).__name__}: {exc}")
    if (export_dir / XLSX_NAME).is_file():
        try:
            export_manifest = finalize_autonomous_exports(export_dir=export_dir)
        except Exception as exc:  # noqa: BLE001
            export_errors.append(f"Export reconciliation failed: {type(exc).__name__}: {exc}")

    artifacts = tuple(
        sorted(
            {str(path) for path in resolved.artifact_paths.values()}
            | {str(path) for path in export_dir.iterdir() if path.is_file()}
        )
    )
    return _result_from_candidate(
        candidate,
        success=not export_errors,
        status="AUTO_PUBLISHED" if not export_errors else "AUTO_PUBLISHED_EXPORT_PARTIAL",
        new_source_count=int(collection_stage.get("new_source_count") or 0),
        release_id=release.release_id,
        pointer_release_id=resolved.release_id,
        release_manifest_hash=resolved.manifest_hash,
        idempotent=release.idempotent,
        artifacts=artifacts,
        errors=tuple(export_errors),
        export_manifest=export_manifest,
    )


def _result_from_candidate(
    candidate: dict[str, Any],
    *,
    success: bool,
    status: str,
    new_source_count: int,
    release_id: str = "",
    pointer_release_id: str = "",
    release_manifest_hash: str = "",
    idempotent: bool = False,
    artifacts: tuple[str, ...] = (),
    errors: tuple[str, ...] = (),
    export_manifest: dict[str, Any] | None = None,
) -> AutonomousOperationResult:
    return AutonomousOperationResult(
        success=success,
        status=status,
        as_of=str(candidate.get("snapshot_date") or ""),
        candidate_id=str(candidate.get("candidate_id") or ""),
        candidate_dir=str(candidate.get("candidate_dir") or ""),
        technical_status=str(candidate.get("technical_status") or ""),
        model_validation_status=str(candidate.get("model_validation_status") or ""),
        release_status=("AUTO_PUBLISHED" if release_id else str(candidate.get("release_status") or "")),
        human_review_status=str(candidate.get("human_review_status") or "NOT_REQUESTED"),
        applicable_score_count=int(candidate.get("applicable_score_count") or 0),
        not_applicable_count=int(candidate.get("not_applicable_count") or 0),
        new_source_count=new_source_count,
        release_id=release_id,
        pointer_release_id=pointer_release_id,
        release_manifest_hash=release_manifest_hash,
        idempotent=idempotent,
        artifacts=artifacts,
        errors=errors,
        export_manifest=export_manifest or {},
    )


def _read_json(path: Path) -> dict[str, Any]:
    import json

    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _is_up_to_date_block(errors: tuple[str, ...]) -> bool:
    return any("NO_NEW_OR_REFRESHED_ELIGIBLE_DOCUMENTS" in error for error in errors)
