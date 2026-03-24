"""Tests for the tier-based distillation dispatcher."""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock


@pytest.fixture
def mock_registry():
    reg = MagicMock()
    return reg


class TestDistillProject:
    @pytest.mark.asyncio
    async def test_tier3_calls_distill_direct(self, mock_registry):
        with patch("main.registry", mock_registry), \
             patch("main._distill_direct", new_callable=AsyncMock) as mock_direct, \
             patch("main._distill_llm", new_callable=AsyncMock) as mock_llm, \
             patch("main._distill_nlm", new_callable=AsyncMock) as mock_nlm:
            mock_registry.get.return_value = {"computed_tier": 3, "tier": None}
            from main import _distill_project
            await _distill_project("simple-project")
            mock_direct.assert_called_once()
            mock_llm.assert_not_called()
            mock_nlm.assert_not_called()

    @pytest.mark.asyncio
    async def test_tier2_calls_distill_llm_no_notes(self, mock_registry):
        with patch("main.registry", mock_registry), \
             patch("main._distill_llm", new_callable=AsyncMock) as mock_llm, \
             patch("main._distill_nlm", new_callable=AsyncMock) as mock_nlm, \
             patch("main._distill_direct", new_callable=AsyncMock):
            mock_registry.get.return_value = {"computed_tier": 2, "tier": None}
            from main import _distill_project
            await _distill_project("medium-project")
            mock_llm.assert_called_once()

    @pytest.mark.asyncio
    async def test_tier1_no_baseline_calls_nlm(self, mock_registry):
        with patch("main.registry", mock_registry), \
             patch("main.NLM_ENABLED", True), \
             patch("main.nlm") as mock_nlm_module, \
             patch("main._distill_nlm", new_callable=AsyncMock) as mock_distill_nlm, \
             patch("main._distill_llm", new_callable=AsyncMock) as mock_llm:
            mock_nlm_module.is_circuit_open.return_value = False
            mock_registry.get.return_value = {"computed_tier": 1, "tier": None, "nlm_baseline": False}
            from main import _distill_project
            await _distill_project("complex-project")
            mock_distill_nlm.assert_called_once()
            mock_llm.assert_not_called()

    @pytest.mark.asyncio
    async def test_tier1_with_baseline_calls_llm_with_notes(self, mock_registry):
        with patch("main.registry", mock_registry), \
             patch("main._distill_nlm", new_callable=AsyncMock) as mock_nlm, \
             patch("main._distill_llm", new_callable=AsyncMock) as mock_llm, \
             patch("main._fetch_existing_notes", return_value=[{"question": "Q", "answer": "A"}]):
            mock_registry.get.return_value = {"computed_tier": 1, "tier": None, "nlm_baseline": True}
            from main import _distill_project
            await _distill_project("complex-project")
            mock_llm.assert_called_once()
            mock_nlm.assert_not_called()

    @pytest.mark.asyncio
    async def test_force_nlm_overrides_tier(self, mock_registry):
        with patch("main.registry", mock_registry), \
             patch("main.NLM_ENABLED", True), \
             patch("main.nlm") as mock_nlm_module, \
             patch("main._distill_nlm", new_callable=AsyncMock) as mock_nlm, \
             patch("main._distill_llm", new_callable=AsyncMock) as mock_llm, \
             patch("main._distill_direct", new_callable=AsyncMock) as mock_direct:
            mock_nlm_module.is_circuit_open.return_value = False
            mock_registry.get.return_value = {"computed_tier": 3, "tier": None}
            from main import _distill_project
            await _distill_project("simple-project", force_nlm=True)
            mock_nlm.assert_called_once()
            mock_llm.assert_not_called()
            mock_direct.assert_not_called()

    @pytest.mark.asyncio
    async def test_tier1_nlm_failure_falls_back_to_llm(self, mock_registry):
        with patch("main.registry", mock_registry), \
             patch("main.NLM_ENABLED", True), \
             patch("main.nlm") as mock_nlm_module, \
             patch("main._distill_nlm", new_callable=AsyncMock, side_effect=Exception("NLM down")), \
             patch("main._distill_llm", new_callable=AsyncMock) as mock_llm:
            mock_nlm_module.is_circuit_open.return_value = False
            mock_registry.get.return_value = {"computed_tier": 1, "tier": None, "nlm_baseline": False}
            from main import _distill_project
            await _distill_project("complex-project")
            mock_llm.assert_called_once()
