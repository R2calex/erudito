import pytest
from integrations.sanitizer import sanitize_text, _local_poison_check, POISON_PATTERNS


class TestLocalPoisonCheck:
    def test_detects_api_key(self):
        text = 'api_key = "sk-proj-abc123xyz456def789"'
        assert _local_poison_check(text) is True

    def test_detects_postgres_uri(self):
        text = "postgresql://admin:secretpass@localhost/db"
        assert _local_poison_check(text) is True

    def test_detects_bearer_token(self):
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abc"
        assert _local_poison_check(text) is True

    def test_clean_text_passes(self):
        text = "This is a normal README about the project architecture."
        assert _local_poison_check(text) is False

    def test_detects_private_key(self):
        text = "-----BEGIN RSA PRIVATE KEY-----"
        assert _local_poison_check(text) is True


class TestSanitizeText:
    @pytest.mark.network
    @pytest.mark.asyncio
    async def test_sanitize_via_service(self):
        result = await sanitize_text("password=secret123456", sanitizer_url="http://localhost:8086")
        assert "secret123456" not in result["sanitized"]
        assert result["masked_count"] > 0

    @pytest.mark.asyncio
    async def test_fallback_on_unavailable(self):
        result = await sanitize_text(
            "Normal text without secrets",
            sanitizer_url="http://localhost:99999",
        )
        assert result["sanitized"] == "Normal text without secrets"
        assert result["masked_count"] == 0
        assert result["fallback"] is True
