from app.config import get_settings
from app.services.ai_service import AIService, HuggingFaceProvider, PhotoFixProvider


def test_photofix_provider_does_not_silently_fallback_to_huggingface(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("AI_PROVIDER", "photofix")
    monkeypatch.setenv("PHOTOFIX_API_URL", "https://example.com/api/restore")

    service = AIService()

    assert isinstance(service._provider, PhotoFixProvider)
    assert service._fallback_provider is None
    assert not isinstance(service._fallback_provider, HuggingFaceProvider)
