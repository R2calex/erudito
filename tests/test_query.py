import pytest
from core.query import classify_confidence, build_response, NLM_SCORE_BOOST, detect_registry_intent


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
