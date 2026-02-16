"""
Task status API endpoint.
Frontend polls this to track processing progress.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from app.services.task_store import get_task

router = APIRouter()


class TaskResponse(BaseModel):
    task_id: str
    status: str
    progress: int
    stage: Optional[str] = None
    error: Optional[str] = None


@router.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_task_status(task_id: str):
    """Get the current status of a processing task."""
    task = get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    return TaskResponse(
        task_id=task.id,
        status=task.status.value,
        progress=task.progress,
        stage=task.stage,
        error=task.error,
    )
