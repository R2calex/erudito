import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from main import _validate_ingest_path, _parse_ingest_files


class TestValidateIngestPath:
    def test_simple_filename(self):
        assert _validate_ingest_path("test.md") == "test.md"

    def test_nested_path(self):
        assert _validate_ingest_path("docs/spec/SPEC.md") == "docs/spec/SPEC.md"

    def test_rejects_absolute_path(self):
        assert _validate_ingest_path("/etc/passwd") is None

    def test_rejects_path_traversal(self):
        assert _validate_ingest_path("../etc/passwd") is None
        assert _validate_ingest_path("docs/../../etc/passwd") is None

    def test_rejects_empty(self):
        assert _validate_ingest_path("") is None
        assert _validate_ingest_path(None) is None

    def test_normalizes_path(self):
        assert _validate_ingest_path("docs//spec///file.md") == "docs/spec/file.md"

    def test_rejects_dotdot_in_middle(self):
        assert _validate_ingest_path("a/../../../etc/passwd") is None


class TestParseIngestFiles:
    def test_dict_format(self):
        data = {"files": {"a.md": "content A", "b.md": "content B"}}
        result = _parse_ingest_files(data)
        assert len(result) == 2
        assert ("a.md", "content A") in result

    def test_list_format(self):
        data = {"files": [{"path": "a.md", "content": "A"}, {"path": "b.md", "content": "B"}]}
        result = _parse_ingest_files(data)
        assert len(result) == 2

    def test_list_format_filename_key(self):
        data = {"files": [{"filename": "a.md", "content": "A"}]}
        result = _parse_ingest_files(data)
        assert len(result) == 1
        assert result[0][0] == "a.md"

    def test_list_format_name_key(self):
        data = {"files": [{"name": "a.md", "content": "A"}]}
        result = _parse_ingest_files(data)
        assert len(result) == 1

    def test_empty_files(self):
        assert _parse_ingest_files({"files": {}}) == []
        assert _parse_ingest_files({"files": []}) == []
        assert _parse_ingest_files({}) == []

    def test_dict_skips_non_string_values(self):
        data = {"files": {"a.md": "ok", "b.md": 123}}
        result = _parse_ingest_files(data)
        assert len(result) == 1

    def test_list_skips_incomplete_entries(self):
        data = {"files": [{"path": "a.md"}, {"content": "no path"}]}
        result = _parse_ingest_files(data)
        assert len(result) == 0


@pytest.mark.integration
class TestIngestEndpoint:
    def test_ingest_dict_format(self):
        import httpx
        resp = httpx.post("http://localhost:8095/ingest/test-ingest", json={
            "files": {"test.md": "# Test\nHello world"},
            "node": "hanzo-test",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["files_stored"] == 1
        assert data["status"] == "ingested"

    def test_ingest_nested_paths(self):
        import httpx
        resp = httpx.post("http://localhost:8095/ingest/test-ingest", json={
            "files": {
                "docs/spec/SPEC.md": "# SPEC",
                "docs/ir/IR.md": "# IR",
                "README.md": "# README",
            },
            "node": "hanzo-test",
        })
        assert resp.status_code == 200
        assert resp.json()["files_stored"] == 3

    def test_ingest_rejects_path_traversal(self):
        import httpx
        resp = httpx.post("http://localhost:8095/ingest/test-ingest", json={
            "files": {"../etc/passwd": "evil", "good.md": "# OK"},
            "node": "attacker",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["files_stored"] == 1
        assert "../etc/passwd" in data.get("rejected", [])

    def test_ingest_empty_files(self):
        import httpx
        resp = httpx.post("http://localhost:8095/ingest/test-ingest", json={
            "files": {},
            "node": "hanzo-test",
        })
        assert resp.status_code == 400

    def test_ingest_list_format(self):
        import httpx
        resp = httpx.post("http://localhost:8095/ingest/test-ingest", json={
            "files": [{"path": "list-test.md", "content": "# List format"}],
            "node": "hanzo-test",
        })
        assert resp.status_code == 200
        assert resp.json()["files_stored"] == 1


@pytest.mark.integration
class TestDeleteIngestEndpoint:
    def _setup_files(self):
        import httpx
        httpx.post("http://localhost:8095/ingest/test-delete", json={
            "files": {
                "a.md": "# A",
                "sub/b.md": "# B",
                "sub/c.md": "# C",
                "other/d.md": "# D",
            },
            "node": "hanzo-test",
        })

    def test_delete_with_prefix(self):
        import httpx
        self._setup_files()
        resp = httpx.delete("http://localhost:8095/ingest/test-delete", params={"prefix": "sub/"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["files_removed"] == 2
        assert data["status"] == "deleted"

    def test_delete_all(self):
        import httpx
        self._setup_files()
        resp = httpx.delete("http://localhost:8095/ingest/test-delete")
        assert resp.status_code == 200
        assert resp.json()["files_removed"] >= 1

    def test_delete_nonexistent_project(self):
        import httpx
        resp = httpx.delete("http://localhost:8095/ingest/nonexistent-project-xyz")
        assert resp.status_code == 200
        assert resp.json()["files_removed"] == 0

    def test_delete_rejects_path_traversal(self):
        import httpx
        resp = httpx.delete("http://localhost:8095/ingest/test-delete", params={"prefix": "../etc"})
        assert resp.status_code == 400
