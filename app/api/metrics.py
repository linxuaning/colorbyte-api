"""
Operational metrics endpoints used for growth/revenue stand-up reporting.
"""
from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.services.database import (
    get_exact_funnel_tuple_metrics,
    get_payment_initiation_metrics,
    get_payment_success_metrics,
    get_processing_complete_metrics,
)

router = APIRouter()


class ProcessingCompleteMetricsResponse(BaseModel):
    count: int
    by_mode: dict[str, int]
    window_hours: int
    generated_at: str


class PaymentInitiationMetricsResponse(BaseModel):
    count: int
    by_provider: dict[str, int]
    storage_backend: str
    window_hours: int
    generated_at: str


class PaymentSuccessMetricsResponse(BaseModel):
    count: int
    by_provider: dict[str, int]
    storage_backend: str
    window_hours: int
    generated_at: str


class ExactFunnelTupleMetricsResponse(BaseModel):
    landing_page: str
    cta_slot: str
    entry_variant: str
    checkout_source: str
    payment_initiations: int
    payment_by_provider: dict[str, int]
    processing_completions: int
    processing_by_mode: dict[str, int]
    storage_backend: str
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


@router.get(
    "/metrics/payment-initiations",
    response_model=PaymentInitiationMetricsResponse,
)
async def get_payment_initiations(
    hours: int = Query(default=24, ge=1, le=168),
):
    """Return server-side payment initiation count in trailing N hours (default 24h)."""
    return build_payment_initiation_metrics(hours=hours)


def build_payment_initiation_metrics(hours: int) -> PaymentInitiationMetricsResponse:
    data = get_payment_initiation_metrics(hours=hours)
    return PaymentInitiationMetricsResponse(**data)


@router.get(
    "/metrics/payment-successes",
    response_model=PaymentSuccessMetricsResponse,
)
async def get_payment_successes(
    hours: int = Query(default=24, ge=1, le=168),
):
    """Return server-side payment success count in trailing N hours (default 24h)."""
    return build_payment_success_metrics(hours=hours)


def build_payment_success_metrics(hours: int) -> PaymentSuccessMetricsResponse:
    data = get_payment_success_metrics(hours=hours)
    return PaymentSuccessMetricsResponse(**data)


@router.get(
    "/metrics/funnel-tuple",
    response_model=ExactFunnelTupleMetricsResponse,
)
async def get_funnel_tuple_metrics(
    landing_page: str = Query(..., min_length=1),
    cta_slot: str = Query(..., min_length=1),
    entry_variant: str = Query(..., min_length=1),
    checkout_source: str = Query(..., min_length=1),
    hours: int = Query(default=24, ge=1, le=168),
):
    """Return exact payment and processing counts for one funnel tuple."""
    return build_funnel_tuple_metrics(
        landing_page=landing_page,
        cta_slot=cta_slot,
        entry_variant=entry_variant,
        checkout_source=checkout_source,
        hours=hours,
    )


def build_funnel_tuple_metrics(
    landing_page: str,
    cta_slot: str,
    entry_variant: str,
    checkout_source: str,
    hours: int,
) -> ExactFunnelTupleMetricsResponse:
    data = get_exact_funnel_tuple_metrics(
        landing_page=landing_page,
        cta_slot=cta_slot,
        entry_variant=entry_variant,
        checkout_source=checkout_source,
        hours=hours,
    )
    return ExactFunnelTupleMetricsResponse(**data)
