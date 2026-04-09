import pytest
from core.query import (
    classify_confidence,
    build_response,
    NLM_SCORE_BOOST,
    detect_registry_intent,
    _format_agent_context,
    _execute_agent,
)


class TestConfidenceClassification:
    def test_high_confidence(self):
        assert classify_confidence(0.85) == "high"

    def test_medium_confidence(self):
        assert classify_confidence(0.50) == "medium"

    def test_low_confidence(self):
        assert classify_confidence(0.20) == "low"

    def test_threshold_exact(self):
        assert classify_confidence(0.75) == "high"

    def test_below_threshold(self):
        assert classify_confidence(0.74) == "medium"


class TestNLMScoreBoost:
    def test_boost_value(self):
        assert NLM_SCORE_BOOST == 0.05


class TestBuildResponse:
    def test_high_confidence_response(self):
        sources = [{"score": 0.85, "payload": {"text": "answer", "source": "file.md", "project": "p"}}]
        resp = build_response("What is X?", sources, nlm_consulted=False)
        assert resp["confidence"] == "high"
        assert resp["nlm_consulted"] is False
        assert resp["suggestion"] is None

    def test_no_results_response(self):
        resp = build_response("Random question", [], nlm_consulted=False)
        assert resp["confidence"] == "low"
        assert "don't have" in resp["answer"].lower() or "no information" in resp["answer"].lower()


class TestRegistryIntent:
    def test_path_query(self):
        assert detect_registry_intent("Where is the erudito project located?") == "path"

    def test_node_query(self):
        assert detect_registry_intent("Which node runs infra-mcp?") == "node"

    def test_notebook_id_query(self):
        assert detect_registry_intent("What is the notebook_id for erudito?") == "notebook_id"

    def test_status_query(self):
        assert detect_registry_intent("What is the status of project jasper?") == "status"

    def test_list_all(self):
        assert detect_registry_intent("List all projects") == "_list_all"

    def test_no_intent(self):
        assert detect_registry_intent("How does the curator work?") is None

    def test_curation_status(self):
        assert detect_registry_intent("What is the curation_status of erudito?") == "curation_status"


def test_get_distill_source_backwards_compat():
    from core.query import _get_distill_source
    # New format
    assert _get_distill_source({"distill_source": "nlm"}) == "nlm"
    assert _get_distill_source({"distill_source": "llm"}) == "llm"
    assert _get_distill_source({"distill_source": "direct"}) == "direct"
    # Old format (backwards compat)
    assert _get_distill_source({"from_nlm": True}) == "nlm"
    # No source info
    assert _get_distill_source({}) == ""
    assert _get_distill_source({"from_nlm": False}) == ""


def _make_source(text, source="doc.md", project="labforge", score=0.85, distill_source="nlm"):
    """Helper to build a mock Qdrant search result."""
    return {
        "score": score,
        "payload": {
            "text": text,
            "source": source,
            "project": project,
            "distill_source": distill_source,
            "chunk_index": 0,
        },
    }


class TestFormatAgentContext:
    def test_empty_sources(self):
        result = _format_agent_context([], 800)
        assert result == "No relevant knowledge found for this query."

    def test_single_source(self):
        sources = [_make_source("BGP uses TCP port 179.", "bgp_guide.md", "labforge")]
        result = _format_agent_context(sources, 800)
        assert result.startswith("## Relevant Knowledge")
        assert "### Bgp Guide (labforge)" in result
        assert "BGP uses TCP port 179." in result
        assert "Source: bgp_guide.md" in result

    def test_multiple_sources(self):
        sources = [
            _make_source("BGP config info", "bgp_guide.md", "labforge", 0.9),
            _make_source("OSPF config info", "ospf_guide.md", "labforge", 0.8),
        ]
        result = _format_agent_context(sources, 800)
        assert "### Bgp Guide" in result
        assert "### Ospf Guide" in result

    def test_max_tokens_truncation(self):
        # Create sources that exceed budget when combined
        long_text = "x" * 500
        sources = [
            _make_source(long_text, f"doc_{i}.md", "p", 0.9 - i * 0.1)
            for i in range(5)
        ]
        result = _format_agent_context(sources, 200)  # ~800 chars budget
        # Should have header + at most 1 section (each section is ~530 chars)
        assert result.startswith("## Relevant Knowledge")
        assert result.count("###") <= 2  # truncated lower-relevance sections

    def test_code_preserved_verbatim(self):
        code_text = "```yaml\nrouter bgp 65000:\n  neighbor 10.0.0.1\n```"
        sources = [_make_source(code_text, "config.md")]
        result = _format_agent_context(sources, 800)
        assert "router bgp 65000:" in result
        assert "neighbor 10.0.0.1" in result

    def test_no_project_in_title(self):
        sources = [_make_source("text", "doc.md", "")]
        result = _format_agent_context(sources, 800)
        assert "### Doc" in result
        assert "()" not in result  # no empty parens

    def test_strips_file_extensions(self):
        for ext in [".md", ".txt", ".html", ".pdf"]:
            sources = [_make_source("text", f"my_doc{ext}", "p")]
            result = _format_agent_context(sources, 800)
            assert ext not in result.split("Source:")[0]


class TestExecuteAgent:
    def test_no_results(self, monkeypatch):
        monkeypatch.setattr("core.query.embed_text", lambda q: [0.0] * 768)
        monkeypatch.setattr("core.query.search", lambda *a, **kw: [])

        result = _execute_agent("test query", "labforge", 5, 800)
        assert result["mode"] == "agent"
        assert result["confidence"] == "none"
        assert result["token_estimate"] == 0
        assert result["sources"] == []
        assert "No relevant knowledge" in result["context"]

    def test_with_results(self, monkeypatch):
        mock_results = [
            _make_source("BGP uses TCP port 179.", "bgp_guide.md", "labforge", 0.85, "nlm"),
            _make_source("OSPF uses protocol 89.", "ospf_guide.md", "labforge", 0.72, "llm"),
        ]
        monkeypatch.setattr("core.query.embed_text", lambda q: [0.0] * 768)
        monkeypatch.setattr("core.query.search", lambda *a, **kw: mock_results)

        result = _execute_agent("routing protocols", "labforge", 5, 800)
        assert result["mode"] == "agent"
        assert result["query"] == "routing protocols"
        assert result["project"] == "labforge"
        assert result["confidence"] == "high"
        assert "## Relevant Knowledge" in result["context"]
        assert "BGP uses TCP port 179." in result["context"]
        assert result["token_estimate"] > 0
        assert len(result["sources"]) == 2
        assert result["sources"][0]["score"] == 0.85
        assert result["sources"][0]["type"] == "nlm_note"

    def test_qdrant_failure(self, monkeypatch):
        monkeypatch.setattr("core.query.embed_text", lambda q: (_ for _ in ()).throw(Exception("Qdrant down")))

        result = _execute_agent("test", "labforge", 5, 800)
        assert result["mode"] == "agent"
        assert result["confidence"] == "none"
        assert result["token_estimate"] == 0

    def test_source_url_fallback(self, monkeypatch):
        sources = [_make_source("text", "bgp.md", "labforge")]
        sources[0]["payload"]["url"] = "https://example.com/bgp"
        monkeypatch.setattr("core.query.embed_text", lambda q: [0.0] * 768)
        monkeypatch.setattr("core.query.search", lambda *a, **kw: sources)

        result = _execute_agent("bgp", "labforge", 5, 800)
        assert result["sources"][0]["url"] == "https://example.com/bgp"

    def test_source_without_url_uses_filename(self, monkeypatch):
        sources = [_make_source("text", "bgp.md", "labforge")]
        monkeypatch.setattr("core.query.embed_text", lambda q: [0.0] * 768)
        monkeypatch.setattr("core.query.search", lambda *a, **kw: sources)

        result = _execute_agent("bgp", "labforge", 5, 800)
        assert result["sources"][0]["url"] == "bgp.md"


@pytest.mark.integration
class TestSearchAgentModeEndpoint:
    def test_agent_mode_returns_context(self):
        import httpx
        resp = httpx.get("http://localhost:8095/search", params={
            "q": "what is erudito",
            "mode": "agent",
            "max_tokens": 500,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["mode"] == "agent"
        assert "context" in data
        assert "sources" in data
        assert "token_estimate" in data
        assert "confidence" in data

    def test_agent_mode_with_project_filter(self):
        import httpx
        resp = httpx.get("http://localhost:8095/search", params={
            "q": "architecture",
            "project": "erudito",
            "mode": "agent",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["mode"] == "agent"
        assert data["project"] == "erudito"

    def test_agent_mode_invalid_max_tokens(self):
        import httpx
        resp = httpx.get("http://localhost:8095/search", params={
            "q": "test",
            "mode": "agent",
            "max_tokens": 50,  # below minimum of 100
        })
        assert resp.status_code == 422
