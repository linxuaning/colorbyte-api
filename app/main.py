"""
ArtImageHub Backend - FastAPI Application
AI-powered photo restoration service
"""
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import upload, tasks, download, payment, metrics, admin
from app.services.database import get_database_backend, get_payment_metrics_storage_backend, init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

app = FastAPI(
    title="ArtImageHub API",
    description="AI-powered photo restoration service",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "https://artimagehub.com",
        "https://www.artimagehub.com",
    ],
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize database
init_db()

# Include routers
app.include_router(upload.router, prefix="/api", tags=["upload"])
app.include_router(tasks.router, prefix="/api", tags=["tasks"])
app.include_router(download.router, prefix="/api", tags=["download"])
app.include_router(payment.router, prefix="/api", tags=["payment"])
app.include_router(metrics.router, prefix="/api", tags=["metrics"])
app.include_router(admin.router, prefix="/api", tags=["admin"])


@app.get("/")
async def root():
    return {"message": "ArtImageHub API", "version": "0.1.0"}


@app.get("/version")
async def version():
    """Return the git commit currently running. Set RENDER_GIT_COMMIT in render.yaml."""
    import os
    return {
        "git_commit": os.environ.get("RENDER_GIT_COMMIT", "unknown"),
        "git_branch": os.environ.get("RENDER_GIT_BRANCH", "unknown"),
        "service": os.environ.get("RENDER_SERVICE_NAME", "unknown"),
    }


@app.get("/health")
async def health_check():
    from app.config import get_settings, get_effective_ai_provider

    settings = get_settings()
    effective_provider = get_effective_ai_provider(settings)
    provider_source = "config"
    if settings.ai_provider == "huggingface" and settings.replicate_api_token:
        provider_source = "replicate_token_auto"

    return {
        "status": "healthy",
        "ai_provider": effective_provider,
        "configured_ai_provider": settings.ai_provider,
        "provider_source": provider_source,
        "replicate_token_configured": bool(settings.replicate_api_token),
        "database_url_configured": bool(settings.database_url or settings.metrics_database_url),
        "database_backend": get_database_backend(),
        "metrics_database_configured": bool(settings.metrics_database_url),
        "payment_metrics_backend": get_payment_metrics_storage_backend(),
    }
