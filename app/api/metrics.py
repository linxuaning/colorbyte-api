"""
Operational metrics endpoints used for growth/revenue stand-up reporting.
"""
from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.services.database import get_processing_complete_metrics

router = APIRouter()


class ProcessingCompleteMetricsResponse(BaseModel):
    count: int
    by_mode: dict[str, int]
    window_hours: int
    generated_at: str


@router.get(
    "/metrics/processing-complete",
    response_model=ProcessingCompleteMetricsResponse,
)
async def get_processing_complete(
    hours: int = Query(default=24, ge=1, le=168),
):
    """Return completed processing count in trailing N hours (default 24h)."""
    return build_processing_complete_metrics(hours=hours)


def build_processing_complete_metrics(hours: int) -> ProcessingCompleteMetricsResponse:
    data = get_processing_complete_metrics(hours=hours)
    return ProcessingCompleteMetricsResponse(**data)
