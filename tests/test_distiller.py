import os
import tempfile
import pytest
from pathlib import Path


class TestComputeTier:
    def _make_curated_dir(self, tmp_path, count):
        d = tmp_path / "curated" / "test-project"
        d.mkdir(parents=True)
        for i in range(count):
            (d / f"feature-{i}.md").write_text(f"# Feature {i}")
        return str(d)

    def test_tier3_one_feature(self, tmp_path):
        from core.distiller import compute_tier
        d = self._make_curated_dir(tmp_path, 1)
        assert compute_tier("test", d, {}) == 3

    def test_tier3_two_features(self, tmp_path):
        from core.distiller import compute_tier
        d = self._make_curated_dir(tmp_path, 2)
        assert compute_tier("test", d, {}) == 3

    def test_tier2_three_features(self, tmp_path):
        from core.distiller import compute_tier
        d = self._make_curated_dir(tmp_path, 3)
        assert compute_tier("test", d, {}) == 2

    def test_tier2_four_features(self, tmp_path):
        from core.distiller import compute_tier
        d = self._make_curated_dir(tmp_path, 4)
        assert compute_tier("test", d, {}) == 2

    def test_tier1_five_features(self, tmp_path):
        from core.distiller import compute_tier
        d = self._make_curated_dir(tmp_path, 5)
        assert compute_tier("test", d, {}) == 1

    def test_tier1_many_features(self, tmp_path):
        from core.distiller import compute_tier
        d = self._make_curated_dir(tmp_path, 12)
        assert compute_tier("test", d, {}) == 1

    def test_manual_override_wins(self, tmp_path):
        from core.distiller import compute_tier
        d = self._make_curated_dir(tmp_path, 12)  # Would be tier 1
        assert compute_tier("test", d, {"tier": 3}) == 3

    def test_override_none_uses_auto(self, tmp_path):
        from core.distiller import compute_tier
        d = self._make_curated_dir(tmp_path, 5)
        assert compute_tier("test", d, {"tier": None}) == 1

    def test_empty_curated_dir(self, tmp_path):
        from core.distiller import compute_tier
        d = self._make_curated_dir(tmp_path, 0)
        assert compute_tier("test", d, {}) == 3

    def test_nonexistent_dir(self, tmp_path):
        from core.distiller import compute_tier
        assert compute_tier("test", str(tmp_path / "nope"), {}) == 3


class TestBuildLlmPrompt:
    def test_prompt_without_nlm_notes(self):
        from core.distiller import build_llm_prompt, DISTILL_QUESTIONS
        curated_files = [
            {"name": "auth.md", "content": "# Auth\nHandles login."},
        ]
        prompt = build_llm_prompt("myproject", curated_files, nlm_notes=None)
        assert "myproject" in prompt["system"]
        assert "English" in prompt["system"]
        assert "Auth" in prompt["user"]
        assert "Existing Project Understanding" not in prompt["user"]
        for q in DISTILL_QUESTIONS:
            assert q in prompt["user"]

    def test_prompt_with_nlm_notes(self):
        from core.distiller import build_llm_prompt
        curated_files = [
            {"name": "core.md", "content": "# Core module"},
        ]
        nlm_notes = [
            {"question": "What is the architecture?", "answer": "Microservices with Redis."},
        ]
        prompt = build_llm_prompt("myproject", curated_files, nlm_notes=nlm_notes)
        assert "Existing Project Understanding" in prompt["user"]
        assert "Microservices with Redis" in prompt["user"]
        assert "Core module" in prompt["user"]

    def test_prompt_english_only(self):
        from core.distiller import build_llm_prompt
        prompt = build_llm_prompt("test", [{"name": "f.md", "content": "data"}], None)
        assert "English" in prompt["system"]


class TestParseLlmResponse:
    def test_parse_numbered_answers(self):
        from core.distiller import parse_llm_response, DISTILL_QUESTIONS
        raw = """1. The architecture uses FastAPI with Qdrant for vector storage.

2. Recent changes include adding a curation pipeline.

3. Dependencies: Qdrant, Redis, Ollama for embeddings.

4. Status is operational with 128 tests passing.

5. This project is a knowledge orchestrator that manages documentation."""
        notes = parse_llm_response(raw, "testproject")
        assert len(notes) == 5
        for note in notes:
            assert note["project"] == "testproject"
            assert note["distill_source"] == "llm"
            assert note["canonical"] is True
            assert len(note["text"]) > 20

    def test_parse_empty_response(self):
        from core.distiller import parse_llm_response
        notes = parse_llm_response("", "test")
        assert notes == []

    def test_parse_malformed_response(self):
        from core.distiller import parse_llm_response
        notes = parse_llm_response("This is not numbered at all.", "test")
        assert notes == []

    def test_parse_rejects_short_answers(self):
        from core.distiller import parse_llm_response
        raw = """1. Yes.

2. No changes.

3. None.

4. OK.

5. It works."""
        notes = parse_llm_response(raw, "test")
        assert len(notes) == 0  # All too short (<20 chars)
