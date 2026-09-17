from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.domain.models import (
    AtomicReleasePointer,
    AtomicReleaseRecord,
    AutonomousReleaseRecord,
    FinancialReleaseRecord,
)
from app.services.autonomous_release_gate import evaluate_autonomous_release_gate
from app.services.audit_baseline import canonical_hash, sha256_file
from app.services.financial_release_gate import evaluate_release_gate, require_governance_write_access


POSTGRES_ADVISORY_LOCK_SQL = "SELECT pg_try_advisory_xact_lock(:lock_key)"
RELEASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,119}$")
JOURNAL_STATES = ("STAGED", "FILES_READY", "DB_COMMITTED", "CURRENT", "ROLLED_BACK")
DbWriter = Callable[[Session, "AtomicReleaseRequest"], None]
DbValidator = Callable[[Session, "AtomicReleaseRequest"], None]


class AtomicReleaseError(RuntimeError):
    """Base error for the logical DB-pointer/versioned-directory release boundary."""


class ReleaseGateNotSatisfied(AtomicReleaseError):
    pass


class ReleaseLockConflict(AtomicReleaseError):
    pass


class ReleaseIntegrityError(AtomicReleaseError):
    pass


class ReleaseRecoveryRequired(AtomicReleaseError):
    pass


@dataclass(frozen=True)
class AtomicReleaseRequest:
    release_id: str
    channel: str
    candidate_id: str
    snapshot_date: date
    governance_record_id: str
    source_dir: Path
    artifact_names: tuple[str, ...]
    expected_baseline_manifest_hash: str
    gate_type: str = "HUMAN_GATED"


@dataclass(frozen=True)
class AtomicReleaseResult:
    release_id: str
    channel: str
    manifest_hash: str
    release_directory: Path
    previous_release_id: str
    idempotent: bool
    trace: tuple[str, ...]


@dataclass(frozen=True)
class ResolvedRelease:
    release_id: str
    channel: str
    release_directory: Path
    manifest_path: Path
    manifest_hash: str
    artifact_paths: dict[str, Path]


def publish_atomic_release(
    *,
    engine: Engine,
    settings: Settings,
    root: Path,
    request: AtomicReleaseRequest,
    db_writer: DbWriter | None = None,
    db_validator: DbValidator | None = None,
    fault_at: str | None = None,
) -> AtomicReleaseResult:
    """Publish one immutable version behind one committed DB pointer.

    This is a logical atomicity boundary, not a claim of distributed ACID across
    the database and filesystem. A pre-commit crash can leave only an unreferenced
    version directory, which readers ignore and recovery can remove safely.
    """

    root = root.resolve()
    _validate_request(request)
    require_governance_write_access(settings)  # before lock, stage, journal, or write session
    source_files = _source_files(request)
    source_manifest_hash = canonical_hash(source_files)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    existing = _idempotent_result(engine, factory, root, request, source_manifest_hash)
    if existing is not None:
        return existing
    gate_record = _load_gate_record(factory, request.governance_record_id, request.gate_type)
    if request.gate_type == "AUTONOMOUS":
        _validate_autonomous_candidate_contract(request, gate_record)
        gate = evaluate_autonomous_release_gate(
            gate_record,
            expected_baseline_manifest_hash=request.expected_baseline_manifest_hash,
        )
    else:
        gate = evaluate_release_gate(
            gate_record,
            expected_baseline_manifest_hash=request.expected_baseline_manifest_hash,
        )
    if not gate.eligible:
        raise ReleaseGateNotSatisfied("Release gate closed: " + ", ".join(gate.blockers))
    expected_release_status = "DRAFT" if request.gate_type == "AUTONOMOUS" else "ELIGIBLE"
    if gate_record.release_status != expected_release_status:
        raise ReleaseGateNotSatisfied(
            f"Release gate closed: RELEASE_STATUS_NOT_{expected_release_status}"
        )

    releases_root = root / "outputs" / "releases"
    versions_root = releases_root / "versions" / _safe_channel(request.channel)
    journal_path = releases_root / "publish_journal.jsonl"
    lock_path = releases_root / "locks" / f"{_safe_channel(request.channel)}.lock"
    final_dir = versions_root / request.release_id
    stage_dir: Path | None = None
    committed = False
    renamed = False
    trace: list[str] = ["PREFLIGHT_GATE_PASS"]
    previous_release_id = ""
    manifest_hash = ""

    with _local_writer_lock(lock_path, enabled=engine.dialect.name == "sqlite"):
        trace.append("SQLITE_LOCK_ACQUIRED" if engine.dialect.name == "sqlite" else "DB_LOCK_PENDING")
        existing = _idempotent_result(engine, factory, root, request, source_manifest_hash)
        if existing is not None:
            return existing
        if final_dir.exists():
            raise ReleaseIntegrityError(f"Unreferenced/conflicting release directory already exists: {final_dir}")
        versions_root.mkdir(parents=True, exist_ok=True)
        stage_dir = Path(tempfile.mkdtemp(prefix=f".staging-{request.release_id}-", dir=str(versions_root)))
        _append_journal(
            journal_path,
            release_id=request.release_id,
            channel=request.channel,
            state="STAGED",
            details={"stage_directory": str(stage_dir.relative_to(root))},
        )
        trace.append("STAGED")
        try:
            if fault_at == "stage_write":
                raise OSError("injected stage write failure")
            for item in source_files:
                source = request.source_dir / item["relative_path"]
                target = stage_dir / item["relative_path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                if sha256_file(target) != item["sha256"]:
                    raise ReleaseIntegrityError(f"Staged artifact hash mismatch: {item['relative_path']}")
            manifest = {
                "schema_version": "1.0",
                "release_id": request.release_id,
                "channel": request.channel,
                "candidate_id": request.candidate_id,
                "snapshot_date": request.snapshot_date.isoformat(),
                "governance_record_id": request.governance_record_id,
                "gate_type": request.gate_type,
                "baseline_manifest_hash": request.expected_baseline_manifest_hash,
                "source_manifest_hash": source_manifest_hash,
                "files": source_files,
            }
            manifest["aggregate_sha256"] = canonical_hash(manifest)
            manifest_path = stage_dir / "release_manifest.json"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            manifest_hash = sha256_file(manifest_path)
            _append_journal(
                journal_path,
                release_id=request.release_id,
                channel=request.channel,
                state="FILES_READY",
                details={"manifest_hash": manifest_hash, "source_manifest_hash": source_manifest_hash},
            )
            trace.append("FILES_READY")

            with factory() as session:
                with session.begin():
                    if engine.dialect.name == "postgresql":
                        acquired = bool(
                            session.execute(
                                text(POSTGRES_ADVISORY_LOCK_SQL),
                                {"lock_key": _advisory_lock_key(request.channel)},
                            ).scalar()
                        )
                        if not acquired:
                            raise ReleaseLockConflict(f"PostgreSQL advisory lock busy for {request.channel}")
                        trace.append("POSTGRES_ADVISORY_XACT_LOCK_ACQUIRED")
                    pointer = session.get(AtomicReleasePointer, request.channel)
                    previous_release_id = pointer.release_id if pointer else ""
                    if pointer is not None:
                        previous_record = session.get(AtomicReleaseRecord, pointer.release_id)
                        if previous_record is None:
                            raise ReleaseIntegrityError("Current pointer lost its release record.")
                        previous_record.status = "SUPERSEDED"
                    governance_model = (
                        AutonomousReleaseRecord
                        if request.gate_type == "AUTONOMOUS"
                        else FinancialReleaseRecord
                    )
                    governance = session.get(governance_model, request.governance_record_id)
                    if governance is None:
                        raise ReleaseGateNotSatisfied("Governance record disappeared inside release transaction.")
                    if request.gate_type == "AUTONOMOUS":
                        transactional_gate = evaluate_autonomous_release_gate(
                            governance,
                            expected_baseline_manifest_hash=request.expected_baseline_manifest_hash,
                        )
                        required_status = "DRAFT"
                    else:
                        transactional_gate = evaluate_release_gate(
                            governance,
                            expected_baseline_manifest_hash=request.expected_baseline_manifest_hash,
                        )
                        required_status = "ELIGIBLE"
                    if not transactional_gate.eligible or governance.release_status != required_status:
                        raise ReleaseGateNotSatisfied(
                            "Transactional release gate closed: " + ", ".join(transactional_gate.blockers)
                        )
                    if fault_at == "db_write":
                        raise RuntimeError("injected DB write failure")
                    if db_writer is not None:
                        db_writer(session, request)
                    session.add(
                        AtomicReleaseRecord(
                            id=request.release_id,
                            channel=request.channel,
                            candidate_id=request.candidate_id,
                            snapshot_date=request.snapshot_date,
                            governance_record_id=request.governance_record_id,
                            baseline_manifest_hash=request.expected_baseline_manifest_hash,
                            source_manifest_hash=source_manifest_hash,
                            artifact_manifest_hash=manifest_hash,
                            release_directory=str(final_dir.relative_to(root)),
                            status="CURRENT",
                            is_demo=governance.is_demo,
                        )
                    )
                    session.flush()
                    if db_validator is not None:
                        db_validator(session, request)
                    if fault_at == "validation":
                        raise ReleaseIntegrityError("injected final validation failure")
                    if pointer is None:
                        pointer = AtomicReleasePointer(
                            channel=request.channel,
                            release_id=request.release_id,
                            artifact_manifest_hash=manifest_hash,
                            release_directory=str(final_dir.relative_to(root)),
                        )
                        session.add(pointer)
                    else:
                        # AUTHORIZED_CURRENT_POINTER_WRITE — sole pointer mutation site.
                        pointer.release_id = request.release_id
                        pointer.artifact_manifest_hash = manifest_hash
                        pointer.release_directory = str(final_dir.relative_to(root))
                    governance.release_status = (
                        "AUTO_PUBLISHED" if request.gate_type == "AUTONOMOUS" else "RELEASED"
                    )
                    session.flush()
                    if fault_at == "rename":
                        raise OSError("injected atomic rename failure")
                    os.rename(stage_dir, final_dir)
                    renamed = True
                    trace.append("VERSION_RENAMED")
                    _make_immutable(final_dir)
                    if fault_at == "commit":
                        raise RuntimeError("injected commit failure")
                committed = True
                trace.append("DB_TRANSACTION_COMMITTED")

            if fault_at == "journal_after_commit":
                raise ReleaseRecoveryRequired("injected post-commit journal failure")
            _append_journal(
                journal_path,
                release_id=request.release_id,
                channel=request.channel,
                state="DB_COMMITTED",
                details={"manifest_hash": manifest_hash, "previous_release_id": previous_release_id},
            )
            trace.append("DB_COMMITTED")
            _append_journal(
                journal_path,
                release_id=request.release_id,
                channel=request.channel,
                state="CURRENT",
                details={"manifest_hash": manifest_hash},
            )
            trace.append("CURRENT")
        except Exception as exc:
            if committed:
                raise
            if stage_dir is not None and stage_dir.exists():
                shutil.rmtree(stage_dir, ignore_errors=True)
            if renamed and final_dir.exists():
                _make_writable(final_dir)
                shutil.rmtree(final_dir, ignore_errors=True)
            try:
                _append_journal(
                    journal_path,
                    release_id=request.release_id,
                    channel=request.channel,
                    state="ROLLED_BACK",
                    details={"error_type": type(exc).__name__, "previous_release_id": previous_release_id},
                )
            except OSError:
                pass
            trace.append("ROLLED_BACK")
            raise

    resolved = resolve_committed_release(engine=engine, root=root, channel=request.channel)
    if resolved.release_id != request.release_id or resolved.manifest_hash != manifest_hash:
        raise ReleaseIntegrityError("Committed pointer/manifest reconciliation failed.")
    return AtomicReleaseResult(
        release_id=request.release_id,
        channel=request.channel,
        manifest_hash=manifest_hash,
        release_directory=final_dir,
        previous_release_id=previous_release_id,
        idempotent=False,
        trace=tuple(trace),
    )


def resolve_committed_release(*, engine: Engine, root: Path, channel: str) -> ResolvedRelease:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    with factory() as session:
        pointer = session.get(AtomicReleasePointer, channel)
        if pointer is None:
            raise ReleaseIntegrityError(f"No committed release pointer for channel {channel}")
        record = session.get(AtomicReleaseRecord, pointer.release_id)
        if record is None or record.status != "CURRENT":
            raise ReleaseIntegrityError("Committed pointer does not resolve to a current release record.")
        release_id = record.id
        release_directory = (root.resolve() / pointer.release_directory).resolve()
        manifest_hash = pointer.artifact_manifest_hash
    try:
        release_directory.relative_to(root.resolve() / "outputs" / "releases" / "versions")
    except ValueError as exc:
        raise ReleaseIntegrityError("Committed release directory escaped the versioned release root.") from exc
    manifest_path = release_directory / "release_manifest.json"
    if not manifest_path.is_file() or sha256_file(manifest_path) != manifest_hash:
        raise ReleaseIntegrityError("Committed release manifest is missing or has a hash mismatch.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("release_id") != release_id or manifest.get("channel") != channel:
        raise ReleaseIntegrityError("Committed release manifest identity mismatch.")
    unhashed = dict(manifest)
    aggregate = unhashed.pop("aggregate_sha256", "")
    if canonical_hash(unhashed) != aggregate:
        raise ReleaseIntegrityError("Committed release manifest aggregate mismatch.")
    artifacts: dict[str, Path] = {}
    for item in manifest["files"]:
        artifact = release_directory / item["relative_path"]
        if not artifact.is_file() or sha256_file(artifact) != item["sha256"]:
            raise ReleaseIntegrityError(f"Committed artifact mismatch: {item['relative_path']}")
        artifacts[item["relative_path"]] = artifact
    return ResolvedRelease(
        release_id=release_id,
        channel=channel,
        release_directory=release_directory,
        manifest_path=manifest_path,
        manifest_hash=manifest_hash,
        artifact_paths=artifacts,
    )


def recover_atomic_releases(*, engine: Engine, root: Path, channel: str | None = None) -> dict[str, Any]:
    """Reconcile journal/files to the DB pointer without changing a valid pointer."""
    root = root.resolve()
    journal_path = root / "outputs" / "releases" / "publish_journal.jsonl"
    events = _read_journal(journal_path)
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for event in events:
        key = (str(event.get("channel") or ""), str(event.get("release_id") or ""))
        if all(key) and (channel is None or key[0] == channel):
            latest[key] = event
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    with factory() as session:
        pointers = {
            pointer.channel: pointer.release_id
            for pointer in session.scalars(select(AtomicReleasePointer)).all()
            if channel is None or pointer.channel == channel
        }
    completed = 0
    rolled_back = 0
    removed_orphans = 0
    for (event_channel, release_id), event in sorted(latest.items()):
        state = str(event.get("state") or "")
        if pointers.get(event_channel) == release_id:
            resolved = resolve_committed_release(engine=engine, root=root, channel=event_channel)
            if state != "CURRENT":
                _append_journal(
                    journal_path,
                    release_id=release_id,
                    channel=event_channel,
                    state="CURRENT",
                    details={"manifest_hash": resolved.manifest_hash, "recovered": True},
                )
                completed += 1
            continue
        if state in {"CURRENT", "ROLLED_BACK"}:
            continue
        versions_root = root / "outputs" / "releases" / "versions" / _safe_channel(event_channel)
        orphan = versions_root / release_id
        if orphan.exists():
            _make_writable(orphan)
            shutil.rmtree(orphan, ignore_errors=True)
            removed_orphans += 1
        for staging in versions_root.glob(f".staging-{release_id}-*") if versions_root.exists() else []:
            shutil.rmtree(staging, ignore_errors=True)
            removed_orphans += 1
        _append_journal(
            journal_path,
            release_id=release_id,
            channel=event_channel,
            state="ROLLED_BACK",
            details={"recovered": True, "valid_pointer_preserved": pointers.get(event_channel, "")},
        )
        rolled_back += 1
    return {
        "completed_current": completed,
        "rolled_back": rolled_back,
        "removed_orphans": removed_orphans,
        "pointers_preserved": dict(sorted(pointers.items())),
    }


def journal_events(root: Path, *, release_id: str | None = None) -> list[dict[str, Any]]:
    events = _read_journal(root.resolve() / "outputs" / "releases" / "publish_journal.jsonl")
    return [event for event in events if release_id is None or event.get("release_id") == release_id]


def postgres_advisory_lock_sql() -> str:
    return POSTGRES_ADVISORY_LOCK_SQL


def _validate_request(request: AtomicReleaseRequest) -> None:
    for label, value in (
        ("release_id", request.release_id),
        ("channel", request.channel),
        ("candidate_id", request.candidate_id),
    ):
        if not RELEASE_ID_PATTERN.fullmatch(value):
            raise AtomicReleaseError(f"Invalid {label}")
    if not re.fullmatch(r"[0-9a-f]{64}", request.expected_baseline_manifest_hash):
        raise AtomicReleaseError("Expected baseline manifest hash must be lowercase SHA-256.")
    if request.gate_type not in {"HUMAN_GATED", "AUTONOMOUS"}:
        raise AtomicReleaseError("Unknown release gate type.")
    if not request.source_dir.is_dir():
        raise AtomicReleaseError("Release source directory is missing.")
    if not request.artifact_names or len(set(request.artifact_names)) != len(request.artifact_names):
        raise AtomicReleaseError("Release artifact list must be non-empty and unique.")


def _source_files(request: AtomicReleaseRequest) -> list[dict[str, Any]]:
    root = request.source_dir.resolve()
    files = []
    for name in sorted(request.artifact_names):
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise AtomicReleaseError(f"Unsafe release artifact path: {name}")
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise AtomicReleaseError(f"Release artifact escaped source directory: {name}") from exc
        if not path.is_file() or path.is_symlink():
            raise AtomicReleaseError(f"Release artifact is missing or symlinked: {name}")
        files.append({"relative_path": relative.as_posix(), "byte_size": path.stat().st_size, "sha256": sha256_file(path)})
    return files


def _load_gate_record(
    factory: sessionmaker[Session],
    record_id: str,
    gate_type: str,
) -> FinancialReleaseRecord | AutonomousReleaseRecord:
    with factory() as session:
        model = AutonomousReleaseRecord if gate_type == "AUTONOMOUS" else FinancialReleaseRecord
        record = session.get(model, record_id)
        if record is None:
            raise ReleaseGateNotSatisfied("Release governance record not found.")
        session.expunge(record)
        return record


def _validate_autonomous_candidate_contract(
    request: AtomicReleaseRequest,
    record: AutonomousReleaseRecord,
) -> None:
    """Bind governance hashes to the exact persisted candidate files before staging."""

    required = {"stage_07_validation.json", "candidate_manifest.json"}
    if not required.issubset(set(request.artifact_names)):
        raise ReleaseIntegrityError("Autonomous release is missing validation or candidate manifest.")
    try:
        validation = json.loads(
            (request.source_dir / "stage_07_validation.json").read_text(encoding="utf-8")
        )
        manifest = json.loads(
            (request.source_dir / "candidate_manifest.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseIntegrityError("Autonomous candidate metadata cannot be read.") from exc
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or canonical_hash(artifacts) != manifest.get("artifact_manifest_hash"):
        raise ReleaseIntegrityError("Candidate artifact manifest hash mismatch.")
    for item in artifacts:
        name = str(item.get("name") or "")
        path = request.source_dir / name
        if (
            name not in request.artifact_names
            or not path.is_file()
            or sha256_file(path) != item.get("sha256")
            or path.stat().st_size != item.get("bytes")
        ):
            raise ReleaseIntegrityError(f"Candidate artifact mismatch: {name}")
    checks = {
        "candidate_id": (validation.get("candidate_id"), record.candidate_id),
        "source_manifest_hash": (validation.get("source_manifest_hash"), record.source_manifest_hash),
        "candidate_content_hash": (validation.get("candidate_content_hash"), record.candidate_content_hash),
        "artifact_manifest_hash": (manifest.get("artifact_manifest_hash"), record.artifact_manifest_hash),
        "applicable_score_count": (validation.get("applicable_score_count"), record.applicable_score_count),
        "not_applicable_count": (validation.get("not_applicable_count"), record.not_applicable_count),
        "technical_status": (validation.get("technical_status"), record.technical_status),
        "model_validation_status": (
            validation.get("model_validation_status"),
            record.model_validation_status,
        ),
        "human_review_status": (validation.get("human_review_status"), record.human_review_status),
    }
    mismatches = [name for name, values in checks.items() if values[0] != values[1]]
    if mismatches:
        raise ReleaseIntegrityError(
            "Autonomous governance/candidate mismatch: " + ", ".join(mismatches)
        )


def _idempotent_result(
    engine: Engine,
    factory: sessionmaker[Session],
    root: Path,
    request: AtomicReleaseRequest,
    source_manifest_hash: str,
) -> AtomicReleaseResult | None:
    with factory() as session:
        record = session.get(AtomicReleaseRecord, request.release_id)
        pointer = session.get(AtomicReleasePointer, request.channel)
        if record is None:
            return None
        if record.source_manifest_hash != source_manifest_hash or record.candidate_id != request.candidate_id:
            raise ReleaseIntegrityError("Release ID already exists with different content.")
        if pointer is None or pointer.release_id != record.id:
            raise ReleaseIntegrityError("Release exists but is not the committed channel pointer.")
    resolved = resolve_committed_release(engine=engine, root=root, channel=request.channel)
    return AtomicReleaseResult(
        release_id=record.id,
        channel=request.channel,
        manifest_hash=resolved.manifest_hash,
        release_directory=resolved.release_directory,
        previous_release_id=record.id,
        idempotent=True,
        trace=("PREFLIGHT_GATE_PASS", "IDEMPOTENT_CURRENT"),
    )


@contextmanager
def _local_writer_lock(path: Path, *, enabled: bool) -> Iterator[None]:
    if not enabled:
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(descriptor, f"pid={os.getpid()}\n".encode("utf-8"))
        os.fsync(descriptor)
    except FileExistsError as exc:
        raise ReleaseLockConflict(f"SQLite release lock is busy: {path}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def _append_journal(
    path: Path,
    *,
    release_id: str,
    channel: str,
    state: str,
    details: dict[str, Any],
) -> None:
    if state not in JOURNAL_STATES:
        raise AtomicReleaseError(f"Unknown journal state: {state}")
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "timestamp": datetime.now(UTC).isoformat(),
        "release_id": release_id,
        "channel": channel,
        "state": state,
        "details": details,
    }
    descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, (json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_journal(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _make_immutable(directory: Path) -> None:
    for path in sorted(directory.rglob("*"), reverse=True):
        path.chmod(0o555 if path.is_dir() else 0o444)
    directory.chmod(0o555)


def _make_writable(directory: Path) -> None:
    for path in directory.rglob("*"):
        try:
            path.chmod(0o755 if path.is_dir() else 0o644)
        except OSError:
            pass
    try:
        directory.chmod(0o755)
    except OSError:
        pass


def _safe_channel(channel: str) -> str:
    return channel.replace(".", "_")


def _advisory_lock_key(channel: str) -> int:
    return int(canonical_hash({"channel": channel})[:15], 16)
