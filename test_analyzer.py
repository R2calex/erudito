"""Quick standalone test for the analyzer module.

Tests that need a live LLM are skipped unless ERUDITO_LLM_TESTS=1 is set.
"""
import sys
import os
import pytest

sys.path.insert(0, os.path.dirname(__file__))

from analyzer import analyze_chunk, batch_analyze, _llm_call

needs_llm = pytest.mark.skipif(
    os.environ.get("ERUDITO_LLM_TESTS") != "1",
    reason="Set ERUDITO_LLM_TESTS=1 to run live LLM tests",
)


@needs_llm
def test_llm_cascade():
    """Test 1: LLM cascade connectivity."""
    response, model = _llm_call("Say 'hello' in one word", max_tokens=10)
    assert response is not None, "All models in cascade failed"


@needs_llm
def test_analyze_chunk():
    """Test 2: Chunk analysis with real content."""
    text = """
    ## Keystone v3.0: Auto-Remediation
    When mesh-monitor detects a service DOWN, it automatically attempts to restart
    the container via Docker. If the restart fails, it sends a Telegram alert for
    manual intervention. A kill-switch per service prevents remediation of critical
    services like LiteLLM.
    """
    analysis, model = analyze_chunk(text, "SPEC-AUTO-REMEDIATION.md", "docs")
    assert analysis["relevance_score"] > 0.5, f"Expected >0.5, got {analysis['relevance_score']}"
    assert len(analysis["concepts"]) > 0, "Expected at least one concept"


def test_low_relevance():
    """Test 3: Low relevance chunk (imports only) — no LLM needed."""
    text = "import os\nimport sys\nimport json\nfrom typing import Dict, List"
    analysis, model = analyze_chunk(text, "main.py", "code")
    # Short text (<100 chars) gets auto-skipped with score 0.1
    assert analysis["relevance_score"] <= 0.3, f"Expected <=0.3, got {analysis['relevance_score']}"


@needs_llm
def test_batch_analyze():
    """Test 4: Batch analysis with mixed relevance."""
    chunks = [
        {"text": "import os\nimport sys", "offset": 0},
        {
            "text": (
                "The DevOps Agent runs as a FastAPI service on port 8090. "
                "It accepts task submissions via POST /tasks and executes them "
                "using LiteLLM reasoning models. Each task goes through a governance "
                "pre-flight check before Docker execution."
            ),
            "offset": 100,
        },
    ]
    results = batch_analyze(chunks, "SPEC-DEVOPS.md", "docs", min_relevance=0.3)
    skipped = [r for r in results if r["action"] == "skip"]
    indexed = [r for r in results if r["action"] == "index"]
    assert len(skipped) >= 1, "Expected at least 1 skipped chunk"
    assert len(indexed) >= 1, "Expected at least 1 indexed chunk"

