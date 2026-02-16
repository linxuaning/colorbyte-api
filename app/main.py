"""
ArtImageHub Backend - FastAPI Application
AI-powered photo restoration service
"""
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import upload, tasks, download

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

# Include routers
app.include_router(upload.router, prefix="/api", tags=["upload"])
app.include_router(tasks.router, prefix="/api", tags=["tasks"])
app.include_router(download.router, prefix="/api", tags=["download"])


@app.get("/")
async def root():
    return {"message": "ArtImageHub API", "version": "0.1.0"}


@app.get("/health")
async def health_check():
    from app.config import get_settings
    return {"status": "healthy", "ai_provider": get_settings().ai_provider}
