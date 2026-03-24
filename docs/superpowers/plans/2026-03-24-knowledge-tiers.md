# Knowledge Tier System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 3-tier knowledge distillation (NLM/LLM/Direct) to Erudito v3 so NLM budget is reserved for complex projects, while medium and simple projects get distilled via cheap LLM or direct indexing.

**Architecture:** The existing pipeline (scan → curate → NLM sync → Qdrant) is extended with a polymorphic distill step that replaces the NLM-only sync. Tier is auto-assigned from curator feature count. Consumer feedback endpoint enables drift detection from real usage.

**Tech Stack:** Python 3.11, FastAPI, Qdrant, LiteLLM (minimax-m2.5), asyncio, pytest

**Spec:** `docs/superpowers/specs/2026-03-24-erudito-v3-knowledge-tiers-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `core/distiller.py` | Create | `DISTILL_QUESTIONS`, `compute_tier()`, `build_llm_prompt()`, `parse_llm_response()`, payload schema |
| `core/curator.py` | Modify (lines 19-27) | Fix Spanish DOC_TYPES → English |
| `core/registry.py` | Modify (lines 16-33) | Add tier fields to `_DEFAULT_ENTRY` |
| `core/query.py` | Modify (line 144) | Replace `from_nlm` with `distill_source` |
| `core/indexer.py` | Modify (lines 118-129) | Add filter-based search for `_fetch_existing_notes` |
| `main.py` | Modify (lines 41-42, 308-416, 450-483, 539-581, 817-958) | Distill loop, dispatcher, feedback endpoint, /curate refactor |
| `integrations/notebooklm.py` | Modify (line 34) | Import `DISTILL_QUESTIONS` from distiller |
| `tests/test_distiller.py` | Create | Tier computation, LLM prompt, response parsing, direct indexing |
| `tests/test_feedback.py` | Create | Feedback endpoint, JSONL storage, metrics aggregation |
| `tests/test_main_tiers.py` | Create | Dispatcher routing, force_nlm, distill loop |
| `scripts/migrate-tiers.py` | Create | One-time tier assignment + Qdrant payload migration |
| `scripts/cleanup-and-recurate.sh` | Modify | Respect tiers, only NLM-sync Tier 1 projects |

---

### Task 1: Fix Spanish DOC_TYPES in curator.py (Prerequisite)

**Files:**
- Modify: `core/curator.py:19-27`
- Test: `tests/test_curator.py`

- [ ] **Step 1: Write test for English section titles**

```python
# Add to tests/test_curator.py
def test_doc_types_are_english():
    """All curator section titles must be in English per policy."""
    from core.curator import DOC_TYPES
    for prefix, (title, _order) in DOC_TYPES.items():
        assert title.isascii(), f"{prefix} title '{title}' contains non-ASCII"
    expected = {
        "SPEC": "Design", "PLAN": "Plan", "IR": "Implementation",
        "SOP": "Operations", "CONTRACT": "Contract",
        "REPORT": "Report", "REVIEW": "Review",
    }
    for prefix, expected_title in expected.items():
        assert DOC_TYPES[prefix][0] == expected_title, f"{prefix} should be '{expected_title}'"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/r0calex/ai-lab/erudito && python -m pytest tests/test_curator.py::test_doc_types_are_english -v`
Expected: FAIL — titles are Spanish

- [ ] **Step 3: Fix DOC_TYPES to English**

In `core/curator.py:19-27`, replace:

```python
DOC_TYPES = {
    "SPEC": ("Design", 1),
    "PLAN": ("Plan", 2),
    "IR": ("Implementation", 3),
    "SOP": ("Operations", 4),
    "CONTRACT": ("Contract", 5),
    "REPORT": ("Report", 6),
    "REVIEW": ("Review", 7),
}
```

- [ ] **Step 4: Run all curator tests**

Run: `cd /home/r0calex/ai-lab/erudito && python -m pytest tests/test_curator.py -v`
Expected: ALL PASS (existing tests use prefixes, not title strings)

- [ ] **Step 5: Commit**

```bash
git add core/curator.py tests/test_curator.py
git commit -m "fix(curator): change Spanish section titles to English per english-only policy"
```

---

### Task 2: Create `core/distiller.py` — Questions, Tier Logic, Prompt Builder

**Files:**
- Create: `core/distiller.py`
- Create: `tests/test_distiller.py`

- [ ] **Step 1: Write tests for `compute_tier()`**

```python
# tests/test_distiller.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/r0calex/ai-lab/erudito && python -m pytest tests/test_distiller.py::TestComputeTier -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement `core/distiller.py` — tier logic + questions**

```python
"""Knowledge distillation: tier assignment, LLM prompt building, response parsing.

Canonical DISTILL_QUESTIONS are the single source of truth for all tiers.
"""
import logging
import os
from pathlib import Path

logger = logging.getLogger("erudito.distiller")

# Canonical questions — used by all distillation backends (NLM, LLM, Direct)
DISTILL_QUESTIONS = [
    "What is the current architecture and main components of this project?",
    "What recent changes have occurred and why?",
    "What external dependencies and integrations does this project have?",
    "What is the current operational status and any known issues?",
    "Provide a comprehensive summary of this project for someone unfamiliar with it.",
]

# Tier thresholds (feature count from curator output)
TIER1_THRESHOLD = int(os.getenv("TIER1_THRESHOLD", "5"))
TIER2_THRESHOLD = int(os.getenv("TIER2_THRESHOLD", "3"))


def compute_tier(project_name: str, curated_dir: str, registry_entry: dict) -> int:
    """Determine tier from feature count. Registry override wins.

    Each .md file in curated_dir is one feature group from the curator.
    """
    override = registry_entry.get("tier")
    if override is not None:
        return override

    curated_path = Path(curated_dir)
    if not curated_path.exists():
        logger.warning(f"Curated dir not found for {project_name}: {curated_dir}")
        return 3

    feature_count = len(list(curated_path.glob("*.md")))

    if feature_count >= TIER1_THRESHOLD:
        return 1
    elif feature_count >= TIER2_THRESHOLD:
        return 2
    else:
        return 3
```

- [ ] **Step 4: Run tier tests to verify they pass**

Run: `cd /home/r0calex/ai-lab/erudito && python -m pytest tests/test_distiller.py::TestComputeTier -v`
Expected: ALL PASS

- [ ] **Step 5: Write tests for `build_llm_prompt()`**

```python
# Add to tests/test_distiller.py
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
```

- [ ] **Step 6: Write tests for `parse_llm_response()`**

```python
# Add to tests/test_distiller.py
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
```

- [ ] **Step 7: Implement `build_llm_prompt()` and `parse_llm_response()`**

Add to `core/distiller.py`:

```python
import re

# LLM config
DISTILL_LLM_MODEL = os.getenv("DISTILL_LLM_MODEL", "minimax-m2.5")
DISTILL_LLM_TIMEOUT = int(os.getenv("DISTILL_LLM_TIMEOUT", "120"))
DISTILL_LLM_MAX_TOKENS = int(os.getenv("DISTILL_LLM_MAX_TOKENS", "4000"))
LITELLM_URL = os.getenv("LITELLM_URL", "http://localhost:4000")

_ANSWER_RE = re.compile(r"^\d+\.\s*", re.MULTILINE)


def build_llm_prompt(
    project_name: str,
    curated_files: list[dict],
    nlm_notes: list[dict] | None,
) -> dict:
    """Build system + user prompts for LLM distillation.

    Args:
        project_name: Name of the project
        curated_files: List of {"name": str, "content": str}
        nlm_notes: Existing NLM notes for context (Tier 1 deltas) or None (Tier 2)

    Returns:
        {"system": str, "user": str}
    """
    system = (
        f"You are a knowledge distillation agent for the {project_name} project. "
        "Your task is to analyze the project documentation and produce structured knowledge notes. "
        "Answer each question based ONLY on the provided documentation. "
        "All output must be in English."
    )

    parts = []

    if nlm_notes:
        parts.append("## Existing Project Understanding (from prior deep analysis)\n")
        for note in nlm_notes:
            parts.append(f"Q: {note['question']}\nA: {note['answer']}\n")
        parts.append("\n## New/Updated Documentation\n")

    for f in curated_files:
        parts.append(f"### {f['name']}\n{f['content']}\n")

    parts.append(
        "\nAnswer each of the following questions. If the documentation doesn't "
        "contain enough information to answer, say \"Insufficient documentation.\"\n"
    )
    for i, q in enumerate(DISTILL_QUESTIONS, 1):
        parts.append(f"{i}. {q}")

    return {"system": system, "user": "\n".join(parts)}


def parse_llm_response(raw: str, project_name: str) -> list[dict]:
    """Parse numbered LLM response into note dicts matching canonical payload schema.

    Returns empty list if response is empty or unparseable.
    Rejects answers shorter than 20 characters.
    """
    if not raw or not raw.strip():
        return []

    # Split by numbered pattern (1. 2. 3. etc.)
    segments = _ANSWER_RE.split(raw.strip())
    # First segment is empty or preamble before "1."
    answers = [s.strip() for s in segments[1:] if s.strip()]

    if len(answers) != len(DISTILL_QUESTIONS):
        logger.warning(
            f"LLM response for {project_name}: expected {len(DISTILL_QUESTIONS)} "
            f"answers, got {len(answers)}"
        )
        return []

    notes = []
    for i, (question, answer) in enumerate(zip(DISTILL_QUESTIONS, answers)):
        if len(answer) < 20:
            logger.warning(f"Rejecting short answer for {project_name} Q{i+1}: {answer[:50]}")
            continue
        notes.append({
            "text": answer,
            "question": question,
            "source": "llm",
            "distill_source": "llm",
            "project": project_name,
            "type": "qa",
            "model": DISTILL_LLM_MODEL,
            "canonical": True,
            "chunk_index": i,
        })

    # If any answers were rejected, return empty (all-or-nothing for consistency)
    if len(notes) != len(DISTILL_QUESTIONS):
        return []

    return notes
```

- [ ] **Step 8: Run all distiller tests**

Run: `cd /home/r0calex/ai-lab/erudito && python -m pytest tests/test_distiller.py -v`
Expected: ALL PASS

- [ ] **Step 9: Add litellm to requirements.txt**

Add `litellm>=1.40.0` to `requirements.txt`. It is needed for `_distill_llm` LiteLLM calls.

- [ ] **Step 10: Commit**

```bash
git add core/distiller.py tests/test_distiller.py requirements.txt
git commit -m "feat(distiller): add core module — tier computation, LLM prompt builder, response parser"
```

---

### Task 3: Add Registry Tier Fields

**Files:**
- Modify: `core/registry.py:16-33`
- Test: `tests/test_registry.py`

- [ ] **Step 1: Write test for new default fields**

```python
# Add to tests/test_registry.py
def test_default_entry_has_tier_fields(tmp_path):
    from core.registry import Registry
    r = Registry(yaml_path=str(tmp_path / "reg.yaml"))
    r.register("test-project", path="/tmp/test")
    entry = r.get("test-project")
    assert entry["tier"] is None
    assert entry["computed_tier"] == 3
    assert entry["nlm_baseline"] is False
    assert entry["last_distill"] is None
    assert entry["last_nlm_distill"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/r0calex/ai-lab/erudito && python -m pytest tests/test_registry.py::test_default_entry_has_tier_fields -v`
Expected: FAIL — KeyError on tier fields

- [ ] **Step 3: Add fields to `_DEFAULT_ENTRY`**

In `core/registry.py`, add to `_DEFAULT_ENTRY` dict after line 32 (`"nlm_consecutive_failures": 0,`):

```python
    "tier": None,               # None = auto-assign, 1/2/3 = manual override
    "computed_tier": 3,          # Last auto-computed tier
    "nlm_baseline": False,       # True after first successful NLM distillation
    "last_distill": None,        # ISO timestamp of last distillation (any backend)
    "last_nlm_distill": None,    # ISO timestamp of last NLM distillation
```

- [ ] **Step 4: Run all registry tests**

Run: `cd /home/r0calex/ai-lab/erudito && python -m pytest tests/test_registry.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add core/registry.py tests/test_registry.py
git commit -m "feat(registry): add tier, computed_tier, nlm_baseline, last_distill fields"
```

---

### Task 4: Update `core/query.py` — Replace `from_nlm` with `distill_source`

**Files:**
- Modify: `core/query.py:144,191,220`
- Test: `tests/test_query.py`

- [ ] **Step 1: Write test for distill_source detection**

```python
# Add to tests/test_query.py
def test_source_type_uses_distill_source():
    """query.py should detect source type from distill_source field, not from_nlm."""
    # This test validates the payload format contract
    nlm_payload = {"distill_source": "nlm", "text": "answer"}
    llm_payload = {"distill_source": "llm", "text": "answer"}
    direct_payload = {"distill_source": "direct", "text": "answer"}

    def classify(payload):
        ds = payload.get("distill_source", "")
        if ds in ("nlm", "llm"):
            return "nlm_note"
        elif ds == "direct":
            return "curated_doc"
        return "qdrant"

    assert classify(nlm_payload) == "nlm_note"
    assert classify(llm_payload) == "nlm_note"
    assert classify(direct_payload) == "curated_doc"
    # Backwards compat: old payloads without distill_source
    assert classify({"from_nlm": True, "text": "old"}) == "qdrant"  # No distill_source = old format
```

- [ ] **Step 2: Run test to verify it passes (this is a contract test, not impl test)**

Run: `cd /home/r0calex/ai-lab/erudito && python -m pytest tests/test_query.py::test_source_type_uses_distill_source -v`
Expected: PASS (tests the contract function, not query.py yet)

- [ ] **Step 3: Read `core/query.py` fully and update `from_nlm` references**

Read `core/query.py` lines 130-230 to find all `from_nlm` references. Replace:
- Line 144: `"type": "nlm_note" if payload.get("from_nlm") else "qdrant"` → `"type": "nlm_note" if payload.get("distill_source") in ("nlm", "llm") else ("curated_doc" if payload.get("distill_source") == "direct" else "qdrant")`
- Line 191: `r["payload"]["from_nlm"] = True` → `r["payload"]["distill_source"] = "nlm"`
- Line 220: where `from_nlm: True` is set for NLM live answers → `"distill_source": "nlm"`

Keep backwards compatibility: check `distill_source` first, fall back to `from_nlm` for old Qdrant data until migration runs. Specifically:

```python
# Backwards-compat helper
def _get_distill_source(payload: dict) -> str:
    """Get distill_source from payload, with from_nlm fallback for old data."""
    ds = payload.get("distill_source")
    if ds:
        return ds
    if payload.get("from_nlm"):
        return "nlm"
    return ""
```

Use `_get_distill_source(payload)` wherever `distill_source` is checked.

- [ ] **Step 4: Run all query tests**

Run: `cd /home/r0calex/ai-lab/erudito && python -m pytest tests/test_query.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add core/query.py tests/test_query.py
git commit -m "feat(query): replace from_nlm boolean with distill_source field for tier support"
```

---

### Task 5: Add `search_by_filter` to `core/indexer.py`

**Files:**
- Modify: `core/indexer.py` (after line 129)
- Test: `tests/test_indexer.py`

- [ ] **Step 1: Write test for filter-based search**

```python
# Add to tests/test_indexer.py
def test_search_by_filter_returns_matching_points(monkeypatch):
    """search_by_filter should query Qdrant with scroll and filter conditions."""
    from core import indexer
    captured = {}

    def mock_qdrant(path, data=None, method="POST"):
        captured["path"] = path
        captured["data"] = data
        return {"result": {"points": [
            {"id": 1, "payload": {"question": "Q1", "text": "A1", "project": "test"}},
        ]}, "status": "ok"}

    monkeypatch.setattr("core.indexer._qdrant_request", mock_qdrant)
    results = indexer.search_by_filter("nlm_notes", {"project": "test", "canonical": True}, limit=10)
    assert len(results) == 1
    assert results[0]["payload"]["question"] == "Q1"
    assert "scroll" in captured["path"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/r0calex/ai-lab/erudito && python -m pytest tests/test_indexer.py::test_search_by_filter_returns_matching_points -v`
Expected: FAIL — function not found

- [ ] **Step 3: Implement `search_by_filter`**

Add to `core/indexer.py` after the existing `search()` function:

```python
def search_by_filter(
    collection: str,
    filters: dict,
    limit: int = 10,
) -> list[dict]:
    """Scroll Qdrant points matching exact filter conditions (no vector search).

    Args:
        collection: Qdrant collection name
        filters: Dict of field_name → value for exact match
        limit: Max points to return

    Returns:
        List of point dicts with "id" and "payload"
    """
    must_conditions = [
        {"key": k, "match": {"value": v}} for k, v in filters.items()
    ]
    body = {
        "filter": {"must": must_conditions},
        "limit": limit,
        "with_payload": True,
    }
    try:
        result = _qdrant_request(
            f"/collections/{collection}/points/scroll",
            data=body,
        )
        return result.get("result", {}).get("points", [])
    except Exception as e:
        logger.warning(f"Qdrant scroll error: {e}")
        return []
```

- [ ] **Step 4: Run all indexer tests**

Run: `cd /home/r0calex/ai-lab/erudito && python -m pytest tests/test_indexer.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add core/indexer.py tests/test_indexer.py
git commit -m "feat(indexer): add search_by_filter for non-vector Qdrant queries"
```

---

### Task 6: Implement Distill Functions in `main.py`

**Files:**
- Modify: `main.py` (lines 41-42 env vars, lines 308-416 NLM sync → distill)
- Create: `tests/test_main_tiers.py`

- [ ] **Step 1: Write dispatcher routing tests**

```python
# tests/test_main_tiers.py
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
            # nlm_notes should be None for tier 2
            call_kwargs = mock_llm.call_args
            assert call_kwargs[1].get("nlm_notes") is None or call_kwargs[0][2] is None

    @pytest.mark.asyncio
    async def test_tier1_no_baseline_calls_nlm(self, mock_registry):
        with patch("main.registry", mock_registry), \
             patch("main._distill_nlm", new_callable=AsyncMock) as mock_nlm, \
             patch("main._distill_llm", new_callable=AsyncMock) as mock_llm:
            mock_registry.get.return_value = {"computed_tier": 1, "tier": None, "nlm_baseline": False}
            from main import _distill_project
            await _distill_project("complex-project")
            mock_nlm.assert_called_once()
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
             patch("main._distill_nlm", new_callable=AsyncMock) as mock_nlm, \
             patch("main._distill_llm", new_callable=AsyncMock) as mock_llm, \
             patch("main._distill_direct", new_callable=AsyncMock) as mock_direct:
            mock_registry.get.return_value = {"computed_tier": 3, "tier": None}
            from main import _distill_project
            await _distill_project("simple-project", force_nlm=True)
            mock_nlm.assert_called_once()
            mock_llm.assert_not_called()
            mock_direct.assert_not_called()

    @pytest.mark.asyncio
    async def test_tier1_nlm_failure_falls_back_to_llm(self, mock_registry):
        with patch("main.registry", mock_registry), \
             patch("main._distill_nlm", new_callable=AsyncMock, side_effect=Exception("NLM down")), \
             patch("main._distill_llm", new_callable=AsyncMock) as mock_llm:
            mock_registry.get.return_value = {"computed_tier": 1, "tier": None, "nlm_baseline": False}
            from main import _distill_project
            await _distill_project("complex-project")
            mock_llm.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/r0calex/ai-lab/erudito && python -m pytest tests/test_main_tiers.py -v`
Expected: FAIL — functions not found

- [ ] **Step 3: Update `integrations/notebooklm.py` to reference canonical questions**

At the top of `notebooklm.py`, after the existing `FIXED_QUESTIONS` definition, add:

```python
from core.distiller import DISTILL_QUESTIONS
# FIXED_QUESTIONS remains for NLM (superset of 8 questions).
# DISTILL_QUESTIONS (5) is the canonical set used for indexing.
```

No functional change to `run_nlm_cycle` — it keeps using `FIXED_QUESTIONS`. The canonical check happens at indexing time in `_distill_nlm`.

- [ ] **Step 4: Add new env vars and imports to `main.py`**

After line 42 (`NLM_ENABLED = ...`), add:

```python
MAX_CONCURRENT_DISTILL = int(os.getenv("MAX_CONCURRENT_DISTILL", "5"))
DISTILL_LLM_MODEL = os.getenv("DISTILL_LLM_MODEL", "minimax-m2.5")
FEEDBACK_FILE = os.path.join(DATA_DIR, "feedback.jsonl")
FEEDBACK_THRESHOLD_PCT = int(os.getenv("FEEDBACK_THRESHOLD_PCT", "70"))
FEEDBACK_WINDOW_SIZE = int(os.getenv("FEEDBACK_WINDOW_SIZE", "20"))
```

Add imports:
```python
from core.distiller import compute_tier, build_llm_prompt, parse_llm_response, DISTILL_QUESTIONS, DISTILL_LLM_MODEL as _LLM_MODEL, DISTILL_LLM_TIMEOUT, DISTILL_LLM_MAX_TOKENS, LITELLM_URL
from core.indexer import search_by_filter
```

Add to global state:
```python
_distill_semaphore: asyncio.Semaphore | None = None
```

- [ ] **Step 4: Implement `_fetch_existing_notes()`**

Add to `main.py`:

```python
def _fetch_existing_notes(project_name: str) -> list[dict]:
    """Fetch existing distilled notes from Qdrant nlm_notes collection."""
    try:
        results = search_by_filter(
            COLLECTION_NLM_NOTES,
            {"project": project_name, "canonical": True},
            limit=10,
        )
        return [
            {"question": r["payload"]["question"], "answer": r["payload"]["text"]}
            for r in results if "question" in r.get("payload", {})
        ]
    except Exception as e:
        logger.warning(f"Failed to fetch existing notes for {project_name}: {e}")
        return []
```

- [ ] **Step 5: Implement `_distill_project()` dispatcher**

Add to `main.py`:

```python
async def _distill_project(project_name: str, force_nlm: bool = False):
    """Dispatch distillation based on project tier."""
    entry = registry.get(project_name)
    if not entry:
        return
    curated_dir = os.path.join(CURATED_DIR, project_name)
    tier = entry.get("tier") or entry.get("computed_tier", 3)

    if force_nlm:
        if not NLM_ENABLED:
            raise ValueError("NLM is disabled (NLM_ENABLED=false)")
        if nlm.is_circuit_open():
            raise ValueError("NLM circuit breaker is tripped")
        try:
            await _distill_nlm(project_name, curated_dir)
        except Exception as e:
            logger.error(f"force_nlm failed for {project_name}: {e}")
            raise
        return

    if tier == 1:
        if not entry.get("nlm_baseline"):
            if NLM_ENABLED and not nlm.is_circuit_open():
                try:
                    await _distill_nlm(project_name, curated_dir)
                except Exception as e:
                    logger.warning(f"NLM unavailable for {project_name} baseline, falling back to LLM: {e}")
                    await _distill_llm(project_name, curated_dir, nlm_notes=None)
            else:
                logger.info(f"NLM not available for {project_name} baseline, using LLM")
                await _distill_llm(project_name, curated_dir, nlm_notes=None)
        else:
            existing_notes = _fetch_existing_notes(project_name)
            await _distill_llm(project_name, curated_dir, nlm_notes=existing_notes)
    elif tier == 2:
        await _distill_llm(project_name, curated_dir, nlm_notes=None)
    else:
        await _distill_direct(project_name, curated_dir)
```

- [ ] **Step 6: Implement `_distill_nlm()`**

Wrap existing `_nlm_sync_project` logic:

```python
async def _distill_nlm(project_name: str, curated_dir: str):
    """Distill via NotebookLM (Tier 1 initial or force_nlm)."""
    entry = registry.get(project_name)
    notebook_id = entry.get("notebook_id")

    if not notebook_id:
        notebook_id = await nlm.ensure_notebook(f"AI-Lab: {project_name}")
        if notebook_id:
            registry.update_fields(project_name, notebook_id=notebook_id)
            logger.info(f"Created notebook for {project_name}: {notebook_id}")
        else:
            registry.update_fields(
                project_name,
                nlm_consecutive_failures=entry.get("nlm_consecutive_failures", 0) + 1,
            )
            raise Exception(f"Failed to create notebook for {project_name}")

    _nlm_call_stats["total"] += 1
    result = await nlm.run_nlm_cycle(notebook_id, curated_dir, project_name=project_name)

    if result["success"]:
        _nlm_call_stats["success"] += 1
        indexed = 0
        for i, note in enumerate(result["notes"]):
            if _is_nlm_error(note.get("answer", "")):
                logger.warning(f"Skipping contaminated NLM response for {project_name}")
                continue
            embedding = embed_text(f"{note['question']} {note['answer'][:200]}")
            point_id = generate_point_id(f"nlm:{project_name}:{note['question'][:50]}", 0)
            is_canonical = note.get("question", "") in DISTILL_QUESTIONS
            upsert_points([{
                "id": point_id,
                "vector": embedding,
                "payload": {
                    "text": note["answer"],
                    "question": note["question"],
                    "source": "nlm",
                    "distill_source": "nlm",
                    "project": project_name,
                    "from_nlm": True,  # Backwards compat
                    "type": "qa",
                    "model": "notebooklm",
                    "canonical": is_canonical,
                    "chunk_index": i,
                },
            }], collection=COLLECTION_NLM_NOTES)
            indexed += 1

        registry.update_fields(
            project_name,
            nlm_baseline=True,
            last_distill=_now_iso(),
            last_nlm_distill=_now_iso(),
            nlm_consecutive_failures=0,
            status="synced",
        )
        audit_log({"action": "distill_nlm", "project": project_name, "notes": indexed})
    else:
        registry.update_fields(
            project_name,
            nlm_consecutive_failures=entry.get("nlm_consecutive_failures", 0) + 1,
        )
        raise Exception(f"NLM cycle failed for {project_name}")
```

- [ ] **Step 7: Implement `_distill_llm()`**

```python
async def _distill_llm(project_name: str, curated_dir: str, nlm_notes: list[dict] | None = None):
    """Distill via LLM (Tier 2, or Tier 1 with baseline)."""
    import litellm

    curated_path = Path(curated_dir)
    if not curated_path.exists():
        logger.warning(f"No curated dir for {project_name}: {curated_dir}")
        return

    curated_files = []
    for md_file in sorted(curated_path.glob("*.md")):
        curated_files.append({
            "name": md_file.name,
            "content": md_file.read_text(encoding="utf-8"),
        })

    if not curated_files:
        logger.warning(f"No curated files for {project_name}")
        return

    prompt = build_llm_prompt(project_name, curated_files, nlm_notes)

    try:
        response = await litellm.acompletion(
            model=_LLM_MODEL,
            messages=[
                {"role": "system", "content": prompt["system"]},
                {"role": "user", "content": prompt["user"]},
            ],
            api_base=LITELLM_URL,
            timeout=DISTILL_LLM_TIMEOUT,
            max_tokens=DISTILL_LLM_MAX_TOKENS,
        )
        raw_answer = response.choices[0].message.content
    except Exception as e:
        logger.error(f"LLM distill failed for {project_name}: {e}")
        audit_log({"action": "distill_llm_error", "project": project_name, "error": str(e)})
        return

    notes = parse_llm_response(raw_answer, project_name)
    if not notes:
        logger.warning(f"LLM response unparseable for {project_name}")
        audit_log({"action": "distill_llm_parse_error", "project": project_name})
        return

    for note in notes:
        embedding = embed_text(f"{note['question']} {note['text'][:200]}")
        point_id = generate_point_id(f"llm:{project_name}:{note['question'][:50]}", 0)
        upsert_points([{
            "id": point_id,
            "vector": embedding,
            "payload": note,
        }], collection=COLLECTION_NLM_NOTES)

    registry.update_fields(project_name, last_distill=_now_iso(), status="synced")
    audit_log({"action": "distill_llm", "project": project_name, "notes": len(notes),
               "model": _LLM_MODEL, "had_nlm_context": nlm_notes is not None})
```

- [ ] **Step 8: Implement `_distill_direct()`**

```python
async def _distill_direct(project_name: str, curated_dir: str):
    """Distill by directly embedding curated docs (Tier 3)."""
    curated_path = Path(curated_dir)
    if not curated_path.exists():
        logger.warning(f"No curated dir for {project_name}: {curated_dir}")
        return

    indexed = 0
    for i, md_file in enumerate(sorted(curated_path.glob("*.md"))):
        content = md_file.read_text(encoding="utf-8")
        feature_name = md_file.stem
        embedding = embed_text(content[:2000])
        point_id = generate_point_id(f"direct:{project_name}:{feature_name}", 0)
        upsert_points([{
            "id": point_id,
            "vector": embedding,
            "payload": {
                "text": content,
                "question": f"Project documentation: {feature_name}",
                "source": "direct",
                "distill_source": "direct",
                "project": project_name,
                "type": "curated_doc",
                "model": None,
                "canonical": True,
                "chunk_index": i,
            },
        }], collection=COLLECTION_NLM_NOTES)
        indexed += 1

    registry.update_fields(project_name, last_distill=_now_iso(), status="synced")
    audit_log({"action": "distill_direct", "project": project_name, "docs": indexed})
```

- [ ] **Step 9: Run dispatcher tests**

Run: `cd /home/r0calex/ai-lab/erudito && python -m pytest tests/test_main_tiers.py -v`
Expected: ALL PASS

- [ ] **Step 10: Commit**

```bash
git add main.py tests/test_main_tiers.py
git commit -m "feat(distill): implement _distill_project dispatcher with NLM/LLM/Direct backends"
```

---

### Task 7: Replace `_nlm_sync_loop` with `_distill_loop`

**Files:**
- Modify: `main.py` (lines 308-344 sync loop, lines 450-483 lifespan)

- [ ] **Step 1: Replace `_nlm_sync_loop` and `_run_nlm_sync_all`**

Replace lines 308-344 with:

```python
async def _distill_loop():
    """Background distillation loop. Runs offset from scan loop."""
    global _distill_semaphore
    await asyncio.sleep(300)  # 5-minute offset from scan loop
    while True:
        try:
            await _run_distill_all()
        except Exception as e:
            logger.error(f"Distill loop error: {e}")
            audit_log({"action": "distill_loop_error", "error": str(e)})
        await asyncio.sleep(SCAN_INTERVAL)


async def _run_distill_all():
    """Distill all curated projects, respecting tiers and concurrency."""
    global _nlm_semaphore, _distill_semaphore
    if not registry:
        return
    if _nlm_semaphore is None:
        _nlm_semaphore = asyncio.Semaphore(MAX_CONCURRENT_NLM)
    if _distill_semaphore is None:
        _distill_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DISTILL)

    candidates = []
    for name in registry.list_all():
        entry = registry.get(name)
        if not entry or entry.get("curation_status") != "curated":
            continue
        # Skip if already distilled after last curation
        last_distill = entry.get("last_distill")
        curated_at = entry.get("curated_at")
        if last_distill and curated_at and last_distill >= curated_at:
            continue
        candidates.append(name)

    if not candidates:
        return

    logger.info(f"Distill loop: {len(candidates)} projects to process")

    async def _bounded_distill(name):
        entry = registry.get(name)
        tier = entry.get("tier") or entry.get("computed_tier", 3)
        needs_nlm = (tier == 1 and not entry.get("nlm_baseline"))

        if needs_nlm and NLM_ENABLED:
            async with _nlm_semaphore:
                await _distill_project(name)
        else:
            async with _distill_semaphore:
                await _distill_project(name)

    tasks = [_bounded_distill(name) for name in candidates]
    await asyncio.gather(*tasks, return_exceptions=True)
```

- [ ] **Step 2: Update lifespan to start `_distill_loop`**

In the `lifespan()` function (around line 469-471), replace:

```python
# Old:
if NLM_ENABLED:
    _nlm_sync_task = asyncio.create_task(_nlm_sync_loop())
```

With:

```python
# New: distill loop runs always (Tier 2/3 don't need NLM)
_nlm_sync_task = asyncio.create_task(_distill_loop())
```

- [ ] **Step 3: Remove old `_nlm_sync_project` function**

Delete lines 347-416 (the old `_nlm_sync_project`). This is fully replaced by `_distill_nlm`.

- [ ] **Step 4: Run all existing tests to verify nothing broke**

Run: `cd /home/r0calex/ai-lab/erudito && python -m pytest tests/ -v`
Expected: ALL PASS (128 existing + new tests)

- [ ] **Step 5: Commit**

```bash
git add main.py
git commit -m "feat(distill): replace _nlm_sync_loop with tier-aware _distill_loop"
```

---

### Task 8: Refactor `/curate/{project}` Endpoint

**Files:**
- Modify: `main.py` (lines 817-958)

- [ ] **Step 1: Read current `/curate/{project}` endpoint fully**

Read `main.py:817-958` to understand the current flow before modifying.

- [ ] **Step 2: Add `force_nlm` query param and tier assignment**

Modify the endpoint signature:
```python
@app.post("/curate/{project}")
async def curate_endpoint(project: str, force_nlm: bool = Query(False)):
```

After the curation step (around where `curate_project()` returns), add tier computation:

```python
    # Compute and store tier
    computed = compute_tier(project, os.path.join(CURATED_DIR, project), entry)
    registry.update_fields(project, computed_tier=computed)
```

Replace the existing NLM sync block (lines ~887-927) with:

```python
    # Distill based on tier (or force_nlm)
    if force_nlm and not NLM_ENABLED:
        return JSONResponse(status_code=503, content={"error": "NLM is disabled"})

    await _distill_project(project, force_nlm=force_nlm)
    tier = entry.get("tier") or computed
```

Update the response to include tier info:
```python
    result["tier"] = tier
    result["distill_backend"] = "nlm" if (force_nlm or (tier == 1 and not entry.get("nlm_baseline"))) else ("llm" if tier <= 2 else "direct")
```

- [ ] **Step 3: Run all tests**

Run: `cd /home/r0calex/ai-lab/erudito && python -m pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add main.py
git commit -m "feat(curate): add force_nlm param and tier-based distillation to /curate endpoint"
```

---

### Task 9: Add `POST /feedback` Endpoint and Metrics Integration

**Files:**
- Modify: `main.py` (after last endpoint, and in /metrics)
- Create: `tests/test_feedback.py`

- [ ] **Step 1: Write feedback tests**

```python
# tests/test_feedback.py
"""Tests for consumer feedback endpoint and metrics."""
import json
import os
import tempfile
import pytest
from unittest.mock import patch


class TestFeedbackParsing:
    def test_compute_feedback_stats_empty(self):
        from main import _compute_feedback_stats
        stats = _compute_feedback_stats([])
        assert stats == {}

    def test_compute_feedback_stats_single_project(self):
        from main import _compute_feedback_stats
        entries = [
            {"project": "test", "useful": True, "coherent": True, "logical": True},
            {"project": "test", "useful": True, "coherent": False, "logical": True},
            {"project": "test", "useful": False, "coherent": True, "logical": False},
        ]
        stats = _compute_feedback_stats(entries)
        assert stats["test"]["total"] == 3
        assert abs(stats["test"]["useful_pct"] - 66.7) < 0.1
        assert abs(stats["test"]["coherent_pct"] - 66.7) < 0.1
        assert abs(stats["test"]["logical_pct"] - 66.7) < 0.1

    def test_needs_review_below_threshold(self):
        from main import _compute_feedback_stats
        # 20 entries, 13 useful (65%), below 70% threshold
        entries = [{"project": "p", "useful": i < 13, "coherent": True, "logical": True}
                   for i in range(20)]
        stats = _compute_feedback_stats(entries, threshold=70, window=20)
        assert stats["p"]["needs_review"] is True

    def test_needs_review_above_threshold(self):
        from main import _compute_feedback_stats
        entries = [{"project": "p", "useful": True, "coherent": True, "logical": True}
                   for _ in range(20)]
        stats = _compute_feedback_stats(entries, threshold=70, window=20)
        assert stats["p"]["needs_review"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/r0calex/ai-lab/erudito && python -m pytest tests/test_feedback.py -v`
Expected: FAIL — function not found

- [ ] **Step 3: Implement `_compute_feedback_stats` and `POST /feedback`**

Add to `main.py`:

```python
from pydantic import BaseModel

class FeedbackRequest(BaseModel):
    project: str
    query: str
    useful: bool
    coherent: bool
    logical: bool


def _load_feedback(limit: int = 0) -> list[dict]:
    """Load feedback entries from JSONL file."""
    if not os.path.exists(FEEDBACK_FILE):
        return []
    entries = []
    with open(FEEDBACK_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    if limit > 0:
        return entries[-limit:]
    return entries


def _compute_feedback_stats(
    entries: list[dict],
    threshold: int = FEEDBACK_THRESHOLD_PCT,
    window: int = FEEDBACK_WINDOW_SIZE,
) -> dict:
    """Compute per-project feedback stats from entries."""
    from collections import defaultdict
    by_project = defaultdict(list)
    for e in entries:
        by_project[e.get("project", "unknown")].append(e)

    stats = {}
    for project, items in by_project.items():
        recent = items[-window:]
        total = len(recent)
        if total == 0:
            continue
        useful_pct = round(sum(1 for i in recent if i.get("useful")) / total * 100, 1)
        coherent_pct = round(sum(1 for i in recent if i.get("coherent")) / total * 100, 1)
        logical_pct = round(sum(1 for i in recent if i.get("logical")) / total * 100, 1)
        needs_review = useful_pct < threshold or coherent_pct < threshold or logical_pct < threshold
        stats[project] = {
            "total": total,
            "useful_pct": useful_pct,
            "coherent_pct": coherent_pct,
            "logical_pct": logical_pct,
            "needs_review": needs_review,
        }
    return stats


@app.post("/feedback")
async def feedback_endpoint(req: FeedbackRequest):
    """Record consumer feedback for a query result."""
    entry = {
        "timestamp": _now_iso(),
        "project": req.project,
        "query": req.query,
        "useful": req.useful,
        "coherent": req.coherent,
        "logical": req.logical,
    }
    with open(FEEDBACK_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return {"status": "recorded"}
```

- [ ] **Step 4: Add feedback stats to `/metrics` endpoint**

In the `/metrics` endpoint (around line 539-581), add to the response dict:

```python
    feedback_entries = _load_feedback()
    feedback_stats = _compute_feedback_stats(feedback_entries)
```

Add `"feedback": feedback_stats` to the returned JSON.

- [ ] **Step 5: Run all feedback tests**

Run: `cd /home/r0calex/ai-lab/erudito && python -m pytest tests/test_feedback.py -v`
Expected: ALL PASS

- [ ] **Step 6: Run all tests**

Run: `cd /home/r0calex/ai-lab/erudito && python -m pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
git add main.py tests/test_feedback.py
git commit -m "feat(feedback): add POST /feedback endpoint and per-project drift metrics"
```

---

### Task 10: Add Tier Info to `/metrics` Endpoint

**Files:**
- Modify: `main.py` (lines 539-581, /metrics endpoint)

- [ ] **Step 1: Add tier distribution to metrics response**

In the `/metrics` endpoint, add:

```python
    tier_distribution = {1: 0, 2: 0, 3: 0}
    for name in registry.list_all():
        entry = registry.get(name)
        if entry:
            tier = entry.get("tier") or entry.get("computed_tier", 3)
            tier_distribution[tier] = tier_distribution.get(tier, 0) + 1
```

Add to response:
```python
    "tiers": {
        "distribution": tier_distribution,
        "nlm_baseline_count": sum(1 for n in registry.list_all() if (registry.get(n) or {}).get("nlm_baseline")),
    },
```

- [ ] **Step 2: Run all tests**

Run: `cd /home/r0calex/ai-lab/erudito && python -m pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "feat(metrics): add tier distribution and NLM baseline count"
```

---

### Task 11: Integrate Tier Assignment into Scan Loop

**Files:**
- Modify: `main.py` (in `_scan_project`, after curation step)

- [ ] **Step 1: Add tier computation after curation in `_scan_project`**

In `_scan_project()` (around line 210 for local, line 208 for remote), after the `curate_project()` call succeeds, add:

```python
            # Compute tier from curated output
            computed = compute_tier(project_name, os.path.join(CURATED_DIR, project_name), entry)
            registry.update_fields(project_name, computed_tier=computed)
            logger.info(f"Tier for {project_name}: {computed} ({curation_result.curated_files} features)")
```

- [ ] **Step 2: Run all tests**

Run: `cd /home/r0calex/ai-lab/erudito && python -m pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "feat(scan): compute and store tier after curation in scan loop"
```

---

### Task 12: Migration Script + Cron Update

**Files:**
- Create: `scripts/migrate-tiers.py`
- Modify: `scripts/cleanup-and-recurate.sh`

- [ ] **Step 1: Create migration script**

```python
#!/usr/bin/env python3
"""One-time migration: compute tiers for all existing projects and distill Tier 2/3.

Usage: python scripts/migrate-tiers.py [--dry-run]
"""
import asyncio
import os
import sys
import shutil
import yaml
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.distiller import compute_tier
from core.registry import Registry

DATA_DIR = os.getenv("DATA_DIR", "data")
REGISTRY_YAML = os.path.join(DATA_DIR, "registry.yaml")
CURATED_DIR = os.path.join(DATA_DIR, "curated")


def main():
    dry_run = "--dry-run" in sys.argv

    # Step 1: Backup
    backup_path = f"{REGISTRY_YAML}.pre-tiers"
    if not os.path.exists(backup_path):
        shutil.copy2(REGISTRY_YAML, backup_path)
        print(f"Backup: {backup_path}")
    else:
        print(f"Backup already exists: {backup_path}")

    # Step 2: Load registry
    registry = Registry(yaml_path=REGISTRY_YAML)
    projects = registry.list_all()
    print(f"Found {len(projects)} projects")

    tier_counts = {1: [], 2: [], 3: []}

    for name in projects:
        entry = registry.get(name)
        if not entry:
            continue

        curated_dir = os.path.join(CURATED_DIR, name)
        tier = compute_tier(name, curated_dir, entry)
        feature_count = len(list(Path(curated_dir).glob("*.md"))) if Path(curated_dir).exists() else 0

        # Check if project already has NLM notes (nlm_baseline)
        has_nlm = entry.get("status") in ("synced", "validated") and entry.get("nlm_source_count", 0) > 0

        tier_counts[tier].append(name)
        print(f"  {name}: tier={tier} features={feature_count} nlm_baseline={has_nlm}")

        if not dry_run:
            registry.update_fields(
                name,
                computed_tier=tier,
                nlm_baseline=has_nlm,
            )

    print(f"\nTier distribution:")
    print(f"  Tier 1 (NLM):    {len(tier_counts[1])} — {', '.join(tier_counts[1])}")
    print(f"  Tier 2 (LLM):    {len(tier_counts[2])} — {', '.join(tier_counts[2])}")
    print(f"  Tier 3 (Direct): {len(tier_counts[3])} — {', '.join(tier_counts[3])}")

    # Step 3: Migrate Qdrant payloads (add distill_source to existing nlm_notes)
    if not dry_run:
        print("\n=== Migrating Qdrant nlm_notes payloads ===")
        try:
            from core.indexer import _qdrant_request, COLLECTION_NLM_NOTES
            # Scroll all points in nlm_notes
            scroll_body = {"limit": 100, "with_payload": True}
            result = _qdrant_request(
                f"/collections/{COLLECTION_NLM_NOTES}/points/scroll",
                data=scroll_body,
            )
            points = result.get("result", {}).get("points", [])
            migrated = 0
            for point in points:
                payload = point.get("payload", {})
                if payload.get("from_nlm") and not payload.get("distill_source"):
                    # Add distill_source field (additive, keeps from_nlm)
                    _qdrant_request(
                        f"/collections/{COLLECTION_NLM_NOTES}/points/payload",
                        data={
                            "points": [point["id"]],
                            "payload": {"distill_source": "nlm", "canonical": True},
                        },
                        method="POST",
                    )
                    migrated += 1
            print(f"  Migrated {migrated} points (added distill_source='nlm')")
        except Exception as e:
            print(f"  WARNING: Qdrant migration failed: {e}")
            print(f"  Old payloads will still work via from_nlm fallback in query.py")

    if dry_run:
        print("\n[DRY RUN] No changes made.")
    else:
        print(f"\nRegistry updated. Tier 2/3 projects ready for distill loop.")
        print(f"Run: curl -X POST http://localhost:8095/curate/{{project}} for each Tier 2/3 project.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Update cron script to respect tiers**

Modify `scripts/cleanup-and-recurate.sh` step 4 (lines 52-86). Replace the project list with a tier-aware approach:

Add after the NLM connectivity test (around line 49):

```bash
# Step 3.5: Get tier-1 projects only (these need NLM)
echo "=== Step 3.5: Identifying Tier 1 (NLM) projects ==="
TIER1_PROJECTS=$(python3 -c "
import sys, os, yaml
sys.path.insert(0, '.')
from core.registry import Registry
r = Registry(yaml_path='data/registry.yaml')
for name in r.list_all():
    e = r.get(name)
    if e and (e.get('tier') or e.get('computed_tier', 3)) == 1:
        print(name)
")
echo "Tier 1 projects: $TIER1_PROJECTS"
```

Then modify the re-curate loop to only process `$TIER1_PROJECTS` with NLM, and add a separate pass for Tier 2/3:

```bash
# Step 4b: Distill Tier 2/3 projects (no NLM needed)
echo "=== Step 4b: Distilling Tier 2 and 3 projects ==="
for project in $(python3 -c "
import sys, os, yaml
sys.path.insert(0, '.')
from core.registry import Registry
r = Registry(yaml_path='data/registry.yaml')
for name in r.list_all():
    e = r.get(name)
    if e and (e.get('tier') or e.get('computed_tier', 3)) >= 2:
        print(name)
"); do
    echo "Distilling (LLM/Direct): $project"
    curl -s -X POST "http://localhost:8095/curate/$project" > /dev/null 2>&1
done
```

- [ ] **Step 3: Make migration script executable**

```bash
chmod +x scripts/migrate-tiers.py
```

- [ ] **Step 4: Commit**

```bash
git add scripts/migrate-tiers.py scripts/cleanup-and-recurate.sh
git commit -m "ops(tiers): add migration script and update cron to respect tier assignments"
```

---

### Task 13: Final Integration Test + Full Test Suite

**Files:**
- All modified files

- [ ] **Step 1: Run full test suite**

Run: `cd /home/r0calex/ai-lab/erudito && python -m pytest tests/ -v --tb=short`
Expected: ALL PASS (128 existing + ~30 new tests)

- [ ] **Step 2: Run migration script in dry-run mode**

Run: `cd /home/r0calex/ai-lab/erudito && python scripts/migrate-tiers.py --dry-run`
Expected: Shows tier assignment for all 13 projects without making changes

- [ ] **Step 3: Verify no import errors**

Run: `cd /home/r0calex/ai-lab/erudito && python -c "from core.distiller import compute_tier, build_llm_prompt, parse_llm_response, DISTILL_QUESTIONS; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Final commit if any fixes needed**

```bash
git add -A
git commit -m "fix: integration fixes for knowledge tier system"
```

---

## Summary

| Task | What | New Tests |
|------|------|-----------|
| 1 | Fix Spanish DOC_TYPES | 1 |
| 2 | Create `core/distiller.py` | ~15 |
| 3 | Registry tier fields | 1 |
| 4 | Replace `from_nlm` in query.py | 1 |
| 5 | Add `search_by_filter` to indexer | 1 |
| 6 | Implement distill functions in main.py | 6 |
| 7 | Replace sync loop with distill loop | 0 (uses existing) |
| 8 | Refactor `/curate/{project}` | 0 (manual verification) |
| 9 | Feedback endpoint + metrics | 4 |
| 10 | Tier info in /metrics | 0 |
| 11 | Tier in scan loop | 0 |
| 12 | Migration script + cron | 0 (manual) |
| 13 | Integration test | 0 |

**Total new tests: ~29**
**Estimated commits: 13**
