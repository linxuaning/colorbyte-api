"""
Application configuration
"""
from pydantic_settings import BaseSettings
from functools import lru_cache
from typing import Literal


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # App
    app_name: str = "ArtImageHub API"
    debug: bool = False

    # AI Provider: "huggingface" (free, dev) or "replicate" (paid, prod)
    ai_provider: Literal["huggingface", "replicate", "mock"] = "huggingface"

    # Replicate AI (only needed when ai_provider=replicate)
    replicate_api_token: str = ""

    # Storage (Cloudflare R2 - future)
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket_name: str = "artimagehub"

    # Stripe
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_id: str = ""  # Recurring price ID for $9.9/month subscription
    trial_days: int = 7

    # Database
    database_path: str = "data/artimagehub.db"

    # CORS
    frontend_url: str = "http://localhost:3000"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
