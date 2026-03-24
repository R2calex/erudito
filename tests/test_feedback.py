"""Tests for consumer feedback endpoint and metrics."""
import json
import os
import tempfile
import pytest
from unittest.mock import patch


class TestFeedbackStats:
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
