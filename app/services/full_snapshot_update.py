from __future__ import annotations

import csv
import shutil
import tempfile
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from app.config import ROOT_DIR, Settings, get_settings
from app.services.component_engine import component_snapshot_status
from app.services.mae_snapshot_product import (
    PRODUCTION_STATUS,
    build_full_snapshot_product,
    build_snapshot_product,
    full_workbook_manifest,
    workbook_manifest,
    write_full_snapshot_datasets,
    write_snapshot_datasets,
)
from app.services.mae_snapshot_validation import validate_full_snapshot, validate_snapshot


ProgressCallback = Callable[[str], None]

INBOX_FIELDS = [
    "URL",
    "provider",
    "publication_date",
    "title",
    "target_cell",
    "optional_excerpt",
    "optional_full_text",
    "review_status",
    "proposed_score",
    "thesis",
    "driver",
    "evidence_status",
    "related_indicator",
    "actual_value",
    "expected_value",
    "market_confirmation",
    "conflict",
    "relevance_reason",
]


@dataclass(frozen=True)
class UpdateResult:
    status: str
    success: bool
    message: str
    snapshot_date: str
    previous_snapshot: str
    validation_result: str
    new_publications: int
    changed_cells: int
    carry_forward_cells: int
    evidence_coverage: str
    output_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_full_snapshot_update(
    *,
    as_of: date | None = None,
    root: Path = ROOT_DIR,
    settings: Settings | None = None,
    progress: ProgressCallback | None = None,
    today: date | None = None,
) -> UpdateResult:
    """Run the existing canonical snapshot services only after an explicit UI action."""
    settings = settings or get_settings()
    current_date = today or date.today()
    snapshot_date = as_of or current_date
    latest_date = _latest_snapshot_date(root)
    emit = progress or (lambda _step: None)

    if snapshot_date > current_date:
        return _result(
            "FUTURE_DATE_REJECTED",
            False,
            "Дата snapshot не может быть в будущем.",
            snapshot_date,
            latest_date,
        )

    emit("Проверка источников")
    inbox_rows = _new_inbox_rows(root)
    future_rows = [row for row in inbox_rows if _parse_date(row.get("publication_date")) > snapshot_date]
    if future_rows:
        return _result(
            "FUTURE_DATE_REJECTED",
            False,
            "Источник с будущей датой отклонён; active latest не изменён.",
            snapshot_date,
            latest_date,
            new_publications=len(inbox_rows),
        )

    if not inbox_rows:
        emit("Анализ новых материалов")
        emit("Формирование snapshot")
        emit("Validation")
        result = _validate_active_full_snapshot(root, latest_date)
        emit("Публикация latest")
        return result

    emit("Анализ новых материалов")
    reviewable = [row for row in inbox_rows if str(row.get("review_status") or "").upper() == "PASS"]
    if not reviewable:
        return _result(
            "MANUAL_REVIEW_REQUIRED",
            False,
            "Новые материалы сохранены в source inbox и требуют review; scores и latest не изменены.",
            snapshot_date,
            latest_date,
            new_publications=len(inbox_rows),
        )

    requires_automatic_analysis = any(
        not str(row.get("optional_excerpt") or row.get("optional_full_text") or "").strip()
        for row in reviewable
    )
    if requires_automatic_analysis and not settings.openai_api_key:
        return _result(
            "OPENAI_API_KEY_REQUIRED",
            False,
            "Для автоматического анализа новых публикаций требуется OPENAI_API_KEY. Текущий подтверждённый snapshot доступен без API.",
            snapshot_date,
            latest_date,
            new_publications=len(inbox_rows),
        )

    return _build_new_snapshot(
        snapshot_date=snapshot_date,
        previous_snapshot=latest_date,
        new_publications=len(inbox_rows),
        root=root,
        source_mode="AUTO" if requires_automatic_analysis else "MANUAL",
        emit=emit,
    )


def add_source_to_inbox(
    *,
    url: str,
    provider: str,
    publication_date: date,
    title: str,
    target_cell: str,
    optional_excerpt: str = "",
    optional_full_text: str = "",
    root: Path = ROOT_DIR,
    today: date | None = None,
) -> dict[str, str]:
    """Queue a source for later review; never changes a score or publishes a snapshot."""
    current_date = today or date.today()
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Укажите корректный URL с http:// или https://.")
    if publication_date > current_date:
        raise ValueError("Дата публикации не может быть в будущем.")
    if not provider.strip() or not title.strip():
        raise ValueError("Укажите provider и title.")
    valid_cells = _applicable_cell_ids(root)
    if target_cell not in valid_cells:
        raise ValueError("Target MAE cell отсутствует в текущем applicable universe.")

    path = root / "data" / "source_inbox.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows, fieldnames = _read_inbox(path)
    duplicate = any(
        row.get("URL", "").strip() == url.strip()
        and row.get("target_cell", "").strip() == target_cell
        and row.get("title", "").strip() == title.strip()
        for row in rows
    )
    if duplicate:
        return {"status": "DUPLICATE", "message": "Источник уже находится в source inbox."}

    row = {field: "" for field in dict.fromkeys([*fieldnames, *INBOX_FIELDS])}
    row.update(
        {
            "URL": url.strip(),
            "provider": provider.strip(),
            "publication_date": publication_date.isoformat(),
            "title": title.strip(),
            "target_cell": target_cell,
            "optional_excerpt": optional_excerpt.strip(),
            "optional_full_text": optional_full_text.strip(),
            "review_status": "MANUAL_REVIEW",
            "relevance_reason": "Queued by analyst; requires review before canonical processing.",
        }
    )
    rows.append(row)
    final_fields = list(dict.fromkeys([*fieldnames, *INBOX_FIELDS]))
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=final_fields, extrasaction="ignore", quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)
    return {
        "status": "QUEUED_FOR_REVIEW",
        "message": "Источник добавлен в source inbox. Score не изменён; требуется review и validation.",
    }


def publish_after_validation(validation: dict[str, Any], publisher: Callable[[], None]) -> bool:
    """Small release gate used by the update flow and its regression tests."""
    if validation.get("status") != "PASS":
        return False
    publisher()
    return True


def _build_new_snapshot(
    *,
    snapshot_date: date,
    previous_snapshot: date | None,
    new_publications: int,
    root: Path,
    source_mode: str,
    emit: ProgressCallback,
) -> UpdateResult:
    stamp = snapshot_date.isoformat()
    target = root / "outputs" / "release_candidates" / "ui-updates" / stamp
    if target.exists():
        report = validate_full_snapshot(
            snapshot_date,
            root=root,
            snapshot_dir=target / "full",
            require_database=False,
            write_report=False,
        )
        return _result(
            "CANDIDATE_QUARANTINED",
            report.get("status") == "PASS",
            "Существующий candidate проверен; release gate и подпись аналитика всё ещё обязательны.",
            snapshot_date,
            previous_snapshot,
            validation_result=str(report.get("status") or "FAIL"),
            new_publications=new_publications,
            output_path=str(target / "full"),
        )

    snapshots_root = target.parent
    snapshots_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".ui-update-{stamp}-", dir=snapshots_root))
    full_staging = staging / "full"
    validation_status = "FAIL"
    try:
        snapshot_type = component_snapshot_status(snapshot_date)
        emit("Формирование snapshot")
        pilot = build_snapshot_product(
            snapshot_date,
            snapshot_type,
            root=root,
            source_mode=source_mode,
            snapshot_status=PRODUCTION_STATUS,
        )
        pilot_metadata = workbook_manifest(pilot)
        pilot_paths = write_snapshot_datasets(pilot, staging)

        # The existing builders are the canonical Excel pipeline. They are only
        # invoked here, after an explicit update action.
        from scripts.build_mae_full_snapshot import _build_workbook as build_full_workbook
        from scripts.build_mae_snapshot import _build_workbook as build_pilot_workbook

        pilot_workbook = staging / f"mae_snapshot_{stamp}.xlsx"
        pilot_paths["workbook"] = pilot_workbook
        build_pilot_workbook(pilot_paths, pilot_metadata, pilot_workbook, staging / "previews")

        emit("Validation")
        pilot_validation = validate_snapshot(
            snapshot_date,
            root=root,
            snapshot_dir=staging,
            build_metadata=pilot_metadata,
            require_database=False,
        )
        if pilot_validation.get("status") != "PASS":
            return _failed_candidate(
                snapshot_date,
                previous_snapshot,
                new_publications,
                staging,
                pilot_validation,
            )

        full = build_full_snapshot_product(
            snapshot_date,
            snapshot_type,
            root=root,
            snapshot_status=PRODUCTION_STATUS,
            pilot_paths=pilot_paths,
        )
        full_metadata = full_workbook_manifest(full)
        full_paths = write_full_snapshot_datasets(full, full_staging)
        full_workbook = full_staging / f"mae_full_snapshot_{stamp}.xlsx"
        build_full_workbook(full_paths, full_metadata, full_workbook, full_staging / "previews")

        full_validation = validate_full_snapshot(
            snapshot_date,
            root=root,
            snapshot_dir=full_staging,
            build_metadata=full_metadata,
            require_database=False,
            pilot_output_paths=pilot_paths,
        )
        if full_validation.get("status") != "PASS":
            return _failed_candidate(
                snapshot_date,
                previous_snapshot,
                new_publications,
                staging,
                full_validation,
            )

        staging.rename(target)
        staging = target
        pilot_paths = _dated_pilot_paths(target, stamp)
        full_validation = validate_full_snapshot(
            snapshot_date,
            root=root,
            snapshot_dir=target / "full",
            build_metadata=full_metadata,
            require_database=False,
            pilot_output_paths=pilot_paths,
        )
        validation_status = str(full_validation.get("status") or "FAIL")
        if validation_status != "PASS":
            return _failed_candidate(
                snapshot_date,
                previous_snapshot,
                new_publications,
                target,
                full_validation,
            )

        emit("Карантин release candidate")

        changed = sum(
            row.get("change_type") in {"UPGRADE", "DOWNGRADE", "THESIS_CHANGE", "EVIDENCE_CHANGE"}
            for row in full.applicable_scores
        )
        carry = sum(row.get("change_type") in {"NO_CHANGE", "UNCHANGED", "CARRY_FORWARD"} for row in full.applicable_scores)
        return UpdateResult(
            status="CANDIDATE_QUARANTINED",
            success=True,
            message=(
                "Новый full snapshot прошёл инженерную validation и сохранён как candidate. "
                "Current не изменён: необходимы financial gate и отдельная подпись аналитика."
            ),
            snapshot_date=stamp,
            previous_snapshot=previous_snapshot.isoformat() if previous_snapshot else "",
            validation_result="PASS",
            new_publications=new_publications,
            changed_cells=changed,
            carry_forward_cells=carry,
            evidence_coverage=f"{full.coverage}/104",
            output_path=str(target / "full"),
        )
    except Exception as exc:  # noqa: BLE001 - converted to a user-safe result by design
        return UpdateResult(
            status="UPDATE_FAILED",
            success=False,
            message=f"Обновление не опубликовано: {type(exc).__name__}. Подробности доступны в Diagnostics.",
            snapshot_date=stamp,
            previous_snapshot=previous_snapshot.isoformat() if previous_snapshot else "",
            validation_result=validation_status,
            new_publications=new_publications,
            changed_cells=0,
            carry_forward_cells=0,
            evidence_coverage="",
            output_path=str(staging),
        )


def _validate_active_full_snapshot(
    root: Path,
    snapshot_date: date | None,
    *,
    new_publications: int = 0,
) -> UpdateResult:
    if snapshot_date is None:
        return _result(
            "LATEST_SNAPSHOT_MISSING",
            False,
            "Подтверждённый latest snapshot отсутствует.",
            date.today(),
            None,
        )
    report = validate_full_snapshot(snapshot_date, root=root, require_database=True, write_report=False)
    passed = report.get("status") == "PASS"
    return UpdateResult(
        status="NO_NEW_RESEARCH_SINCE_PREVIOUS_SNAPSHOT" if passed else "VALIDATION_FAILED",
        success=passed,
        message=(
            "NO_NEW_RESEARCH_SINCE_PREVIOUS_SNAPSHOT: новый research update не обнаружен; active scores сохранены без изменений."
            if passed
            else "Validation активного snapshot не пройдена; latest не изменён."
        ),
        snapshot_date=snapshot_date.isoformat(),
        previous_snapshot=_previous_snapshot_date(root),
        validation_result=str(report.get("status") or "FAIL"),
        new_publications=new_publications,
        changed_cells=0,
        carry_forward_cells=104 if passed else 0,
        evidence_coverage=("104/104" if passed else str(report.get("evidence_coverage") or "")),
        output_path=str(root / "outputs" / "snapshots" / snapshot_date.isoformat() / "full"),
    )


def _failed_candidate(
    snapshot_date: date,
    previous_snapshot: date | None,
    new_publications: int,
    path: Path,
    validation: dict[str, Any],
) -> UpdateResult:
    errors = validation.get("errors") or []
    reason = str(errors[0]) if errors else "Validation failed."
    return UpdateResult(
        status="VALIDATION_FAILED",
        success=False,
        message=f"Validation FAIL: {reason} Active latest не изменён.",
        snapshot_date=snapshot_date.isoformat(),
        previous_snapshot=previous_snapshot.isoformat() if previous_snapshot else "",
        validation_result="FAIL",
        new_publications=new_publications,
        changed_cells=0,
        carry_forward_cells=0,
        evidence_coverage=str(validation.get("evidence_coverage") or ""),
        output_path=str(path),
    )


def _result(
    status: str,
    success: bool,
    message: str,
    snapshot_date: date,
    previous_snapshot: date | None,
    *,
    new_publications: int = 0,
    validation_result: str = "NOT_RUN",
    output_path: str = "",
) -> UpdateResult:
    return UpdateResult(
        status=status,
        success=success,
        message=message,
        snapshot_date=snapshot_date.isoformat(),
        previous_snapshot=previous_snapshot.isoformat() if previous_snapshot else "",
        validation_result=validation_result,
        new_publications=new_publications,
        changed_cells=0,
        carry_forward_cells=0,
        evidence_coverage="",
        output_path=output_path,
    )


def _latest_snapshot_date(root: Path) -> date | None:
    path = root / "outputs" / "mae_full_latest_scores.csv"
    if not path.exists():
        return None
    with path.open(encoding="utf-8-sig", newline="") as handle:
        row = next(csv.DictReader(handle), None)
    return _parse_date((row or {}).get("snapshot_date"))


def _previous_snapshot_date(root: Path) -> str:
    path = root / "outputs" / "mae_full_latest_scores.csv"
    if not path.exists():
        return ""
    with path.open(encoding="utf-8-sig", newline="") as handle:
        row = next(csv.DictReader(handle), None)
    return str((row or {}).get("previous_snapshot_date") or "")


def _new_inbox_rows(root: Path) -> list[dict[str, str]]:
    inbox_path = root / "data" / "source_inbox.csv"
    rows, _ = _read_inbox(inbox_path)
    evidence_path = root / "outputs" / "mae_full_latest_evidence.csv"
    known: set[tuple[str, str, str]] = set()
    if evidence_path.exists():
        with evidence_path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                known.add((row.get("URL", "").strip(), row.get("cell_id", "").strip(), row.get("title", "").strip()))
    result = []
    for row in rows:
        if not any(str(value or "").strip() for value in row.values()):
            continue
        key = (row.get("URL", "").strip(), row.get("target_cell", "").strip(), row.get("title", "").strip())
        if key not in known:
            result.append(row)
    return result


def _read_inbox(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    if not path.exists():
        return [], list(INBOX_FIELDS)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader), list(reader.fieldnames or INBOX_FIELDS)


def _applicable_cell_ids(root: Path) -> set[str]:
    path = root / "outputs" / "mae_full_latest_scores.csv"
    if not path.exists():
        return set()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return {
            row["cell_id"]
            for row in csv.DictReader(handle)
            if str(row.get("applicable") or "").lower() == "true"
        }


def _dated_pilot_paths(target: Path, stamp: str) -> dict[str, Path]:
    return {
        "scores": target / f"mae_scores_{stamp}.csv",
        "evidence": target / f"mae_evidence_{stamp}.csv",
        "scenarios": target / f"mae_scenarios_{stamp}.csv",
        "report": target / f"mae_report_{stamp}.md",
        "workbook": target / f"mae_snapshot_{stamp}.xlsx",
    }


def _parse_date(value: Any) -> date:
    try:
        return date.fromisoformat(str(value or ""))
    except ValueError:
        return date.min
