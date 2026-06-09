"""
File-backed task store.
Tasks are persisted to tasks/{task_id}.json so they survive Render OOM restarts.
In-memory dict is the hot path; disk is the source of truth on startup.
"""
import json
import uuid
import time
import logging
from enum import Enum
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

logger = logging.getLogger("artimagehub.task_store")

TASK_DIR = Path("tasks")
TASK_DIR.mkdir(exist_ok=True)


class TaskStatus(str, Enum):
    PENDING = "pending"
    UPLOADING = "uploading"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Task:
    id: str
    file_id: str
    upload_path: str
    status: TaskStatus = TaskStatus.PENDING
    progress: int = 0
    stage: str = "Queued"
    result_path: Optional[str] = None
    error: Optional[str] = None
    provider_used: Optional[str] = None
    provider_backend: Optional[str] = None
    colorize: bool = False
    email: str = ""
    feature_key: str = "restoration"
    landing_page: Optional[str] = None
    cta_slot: Optional[str] = None
    entry_variant: Optional[str] = None
    checkout_source: Optional[str] = None
    created_at: float = field(default_factory=time.time)


# In-memory hot cache
_tasks: dict[str, Task] = {}


def _task_path(task_id: str) -> Path:
    return TASK_DIR / f"{task_id}.json"


def _save_task(task: Task) -> None:
    try:
        data = asdict(task)
        task_json = json.dumps(data)
        _task_path(task.id).write_text(task_json)
        try:
            from app.services.database import upsert_persistent_task

            upsert_persistent_task(task.id, task_json)
        except Exception as exc:
            logger.warning("task_store: failed to persist task %s to database: %s", task.id, exc)
    except Exception as exc:
        logger.warning("task_store: failed to persist task %s: %s", task.id, exc)


def _hydrate_files_from_persistent(task_id: str, row: dict) -> None:
    """Restore image files from persistent storage onto the current local disk."""
    try:
        upload_bytes = row.get("upload_bytes")
        if upload_bytes:
            data = json.loads(row["task_json"])
            upload_path = data.get("upload_path")
            if upload_path:
                path = Path(upload_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                if not path.exists():
                    path.write_bytes(bytes(upload_bytes))

        result_bytes = row.get("result_bytes")
        if result_bytes:
            data = json.loads(row["task_json"])
            result_path = data.get("result_path")
            if result_path:
                path = Path(result_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                if not path.exists():
                    path.write_bytes(bytes(result_bytes))
    except Exception as exc:
        logger.warning("task_store: failed to hydrate task %s files: %s", task_id, exc)


def _load_task_from_disk(task_id: str) -> Optional[Task]:
    path = _task_path(task_id)
    if not path.exists():
        try:
            from app.services.database import get_persistent_task

            row = get_persistent_task(task_id)
            if row and row.get("task_json"):
                TASK_DIR.mkdir(exist_ok=True)
                path.write_text(row["task_json"])
                _hydrate_files_from_persistent(task_id, row)
            else:
                return None
        except Exception as exc:
            logger.warning("task_store: failed to load task %s from database: %s", task_id, exc)
            return None
    try:
        data = json.loads(path.read_text())
        data["status"] = TaskStatus(data["status"])
        data.setdefault("provider_used", None)
        data.setdefault("provider_backend", None)
        data.setdefault("feature_key", "restoration")
        return Task(**data)
    except Exception as exc:
        logger.warning("task_store: failed to load task %s from disk: %s", task_id, exc)
        return None


def _boot_load() -> None:
    """Load all persisted tasks into memory on startup."""
    loaded = 0
    for p in TASK_DIR.glob("*.json"):
        task_id = p.stem
        task = _load_task_from_disk(task_id)
        if task is not None:
            _tasks[task_id] = task
            loaded += 1
    if loaded:
        logger.info("task_store: restored %d tasks from disk", loaded)


# Run on import
_boot_load()


def create_task(
    file_id: str,
    upload_path: str,
    colorize: bool = False,
    email: str = "",
    feature_key: str = "restoration",
    landing_page: str | None = None,
    cta_slot: str | None = None,
    entry_variant: str | None = None,
    checkout_source: str | None = None,
) -> Task:
    task_id = uuid.uuid4().hex[:12]
    task = Task(
        id=task_id,
        file_id=file_id,
        upload_path=upload_path,
        colorize=colorize,
        email=email,
        feature_key=feature_key,
        landing_page=landing_page,
        cta_slot=cta_slot,
        entry_variant=entry_variant,
        checkout_source=checkout_source,
    )
    _tasks[task_id] = task
    _save_task(task)
    return task


def get_task(task_id: str) -> Task | None:
    task = _tasks.get(task_id)
    if task is not None:
        return task
    # Hot cache miss — try disk (handles cold restarts)
    task = _load_task_from_disk(task_id)
    if task is not None:
        _tasks[task_id] = task
    return task


def update_task(
    task_id: str,
    status: TaskStatus | None = None,
    progress: int | None = None,
    stage: str | None = None,
    result_path: str | None = None,
    error: str | None = None,
    provider_used: str | None = None,
    provider_backend: str | None = None,
) -> Task | None:
    task = get_task(task_id)
    if task is None:
        return None
    if status is not None:
        task.status = status
    if progress is not None:
        task.progress = progress
    if stage is not None:
        task.stage = stage
    if result_path is not None:
        task.result_path = result_path
    if error is not None:
        task.error = error
    if provider_used is not None:
        task.provider_used = provider_used
    if provider_backend is not None:
        task.provider_backend = provider_backend
    _save_task(task)
    return task


def list_tasks() -> list[Task]:
    return list(_tasks.values())
