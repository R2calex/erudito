import pytest
from core.query import classify_confidence, build_response, NLM_SCORE_BOOST


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
