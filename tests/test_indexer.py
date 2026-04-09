import pytest
from core.indexer import chunk_text, generate_point_id


class TestChunking:
    def test_small_text_single_chunk(self):
        chunks = chunk_text("Short text", "test.md")
        assert len(chunks) == 1
        assert chunks[0]["text"] == "Short text"
        assert chunks[0]["source"] == "test.md"
        assert chunks[0]["chunk_index"] == 0

    def test_long_text_multiple_chunks(self):
        text = "A" * 1600
        chunks = chunk_text(text, "long.md")
        assert len(chunks) >= 2
        end_of_first = chunks[0]["text"]
        start_of_second = chunks[1]["text"]
        assert end_of_first[-150:] == start_of_second[:150]

    def test_filters_tiny_chunks(self):
        text = "A" * 800 + "\n" + "B" * 10
        chunks = chunk_text(text, "test.md")
        for c in chunks:
            assert len(c["text"]) >= 50

    def test_empty_text(self):
        assert chunk_text("", "empty.md") == []

    def test_chunk_metadata(self):
        text = "Content " * 200
        chunks = chunk_text(text, "file.md")
        for i, c in enumerate(chunks):
            assert c["chunk_index"] == i
            assert c["source"] == "file.md"
            assert "offset" in c


class TestPointId:
    def test_deterministic(self):
        id1 = generate_point_id("source.md", 0)
        id2 = generate_point_id("source.md", 0)
        assert id1 == id2

    def test_different_inputs(self):
        id1 = generate_point_id("a.md", 0)
        id2 = generate_point_id("b.md", 0)
        assert id1 != id2

    def test_positive_integer(self):
        pid = generate_point_id("test.md", 5)
        assert isinstance(pid, int)
        assert pid > 0


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
