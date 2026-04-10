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

    # AI Provider:
    # - "huggingface": legacy Spaces/Gradio flow
    # - "hf_inference": Hugging Face HTTP inference API
    # - "replicate": Replicate API
    # - "nero": Nero AI task API
    # - "mock": local no-op provider
    ai_provider: Literal["huggingface", "hf_inference", "replicate", "nero", "mock"] = "huggingface"

    # Replicate AI (only needed when ai_provider=replicate)
    replicate_api_token: str = ""

    # Nero AI task API (only needed when ai_provider=nero)
    nero_api_key: str = ""

    # Hugging Face Inference API (needed when ai_provider=hf_inference)
    hf_token: str = ""
    # Comma-separated model fallback order. Keep overrideable because
    # serverless image models can change availability without code changes.
    hf_inference_models: str = (
        "stabilityai/stable-diffusion-x4-upscaler,"
        "caidas/swin2SR-classical-sr-x2-64,"
        "caidas/swin2SR-lightweight-x2-64"
    )

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

    # PayPal
    paypal_client_id: str = ""  # PayPal REST API Client ID
    paypal_client_secret: str = ""  # PayPal REST API Secret
    paypal_mode: Literal["sandbox", "live"] = "live"  # PayPal environment mode
    paypal_webhook_id: str = ""  # PayPal Webhook ID for signature verification
    paypal_price_usd: float = 4.99  # One-time Pro Lifetime price

    # Dodo Payments
    dodo_payments_api_key: str = ""  # Dodo Payments API key
    dodo_payments_webhook_key: str = ""  # Dodo webhook signing key
    dodo_payments_environment: Literal["test_mode", "live_mode"] = "live_mode"
    dodo_payments_product_id: str = ""  # Product ID used for one-time checkout
    dodo_payments_price_usd: float = 4.99  # One-time Pro Lifetime price
    dodo_payments_currency: str = "USD"

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
