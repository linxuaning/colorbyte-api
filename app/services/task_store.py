"""
In-memory task store for MVP.
Tracks photo processing tasks and their status.
"""
import uuid
import time
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional


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
    colorize: bool = False
    created_at: float = field(default_factory=time.time)


# In-memory store
_tasks: dict[str, Task] = {}


def create_task(file_id: str, upload_path: str, colorize: bool = False) -> Task:
    task_id = uuid.uuid4().hex[:12]
    task = Task(
        id=task_id,
        file_id=file_id,
        upload_path=upload_path,
        colorize=colorize,
    )
    _tasks[task_id] = task
    return task


def get_task(task_id: str) -> Task | None:
    return _tasks.get(task_id)


def update_task(
    task_id: str,
    status: TaskStatus | None = None,
    progress: int | None = None,
    stage: str | None = None,
    result_path: str | None = None,
    error: str | None = None,
) -> Task | None:
    task = _tasks.get(task_id)
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
    return task


def list_tasks() -> list[Task]:
    return list(_tasks.values())
