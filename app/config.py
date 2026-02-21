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

    # LemonSqueezy (legacy, will be replaced by BMC)
    lemonsqueezy_api_key: str = ""
    lemonsqueezy_store_id: str = ""
    lemonsqueezy_variant_id: str = ""  # Variant ID for $9.9/month subscription
    lemonsqueezy_webhook_secret: str = ""
    trial_days: int = 7

    # Buy Me a Coffee
    bmc_api_token: str = ""  # Bearer token for BMC API (if needed)
    bmc_webhook_secret: str = ""  # Secret for webhook signature verification
    bmc_page_url: str = ""  # User's BMC page URL (e.g., https://buymeacoffee.com/username)

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
