"""Comprehensive tests for Erudito — Knowledge Orchestration Agent.

Tests are organized into:
- Unit tests (no network, always runnable)
- Network tests (require Qdrant + Ollama, marked @pytest.mark.network)
- Integration tests (require Erudito service running, marked @pytest.mark.integration)

Run unit tests only:
    pytest tests/test_erudito.py -m "not network and not integration"

Run with live services:
    pytest tests/test_erudito.py -m "network"

Run integration (Erudito must be up on :8095):
    pytest tests/test_erudito.py -m "integration"

Run everything:
    pytest tests/test_erudito.py
"""

import hashlib
import json
import os
import sys
import urllib.request

import pytest

# Add parent dir to path so we can import erudito modules
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

ERUDITO_URL = os.getenv("ERUDITO_URL", "http://localhost:8095")


# ---------------------------------------------------------------------------
# HTTP helpers for integration tests
# ---------------------------------------------------------------------------

def erudito_get(path):
    req = urllib.request.Request(f"{ERUDITO_URL}{path}")
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read())


def erudito_post(path, data=None):
    payload = json.dumps(data).encode() if data else None
    req = urllib.request.Request(
        f"{ERUDITO_URL}{path}",
        data=payload,
        headers={"Content-Type": "application/json"} if payload else {},
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=120)  # scans can take time
    return json.loads(resp.read())


def erudito_delete(path, params=None):
    url = f"{ERUDITO_URL}{path}"
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(url, method="DELETE")
    resp = urllib.request.urlopen(req, timeout=30)
    return json.loads(resp.read())


# ===========================================================================
# UNIT TESTS — no network required
# ===========================================================================


class TestAntiPoison:
    """Test anti-RAG-poison pattern detection."""

    def test_password_detected(self):
        from main import _has_poison
        assert _has_poison("password = 'mysecret123'") is True

    def test_password_colon_detected(self):
        from main import _has_poison
        assert _has_poison('password: "longpassword123"') is True

    def test_api_key_detected(self):
        from main import _has_poison
        assert _has_poison("sk-abc123def456ghi789jkl012") is True

    def test_bearer_token_detected(self):
        from main import _has_poison
        assert _has_poison("Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abc123") is True

    def test_postgres_uri_detected(self):
        from main import _has_poison
        assert _has_poison("postgresql://admin:secret@localhost/db") is True

    def test_private_key_detected(self):
        from main import _has_poison
        assert _has_poison("-----BEGIN RSA PRIVATE KEY-----") is True

    def test_normal_text_passes(self):
        from main import _has_poison
        assert _has_poison("This is normal documentation text") is False

    def test_keyword_mention_passes(self):
        from main import _has_poison
        assert _has_poison("The API key should be stored securely") is False

    def test_short_password_value_passes(self):
        from main import _has_poison
        # Password pattern requires 8+ chars in value
        assert _has_poison("password = 'short'") is False

    def test_empty_string_passes(self):
        from main import _has_poison
        assert _has_poison("") is False


class TestShouldScan:
    """Test file inclusion/exclusion rules."""

    def test_md_included(self):
        from main import _should_scan
        assert _should_scan("docs/SPEC-SOMETHING.md") is True

    def test_py_included(self):
        from main import _should_scan
        assert _should_scan("src/main.py") is True

    def test_yaml_included(self):
        from main import _should_scan
        assert _should_scan("config/docker-compose.yml") is True

    def test_env_excluded(self):
        from main import _should_scan
        assert _should_scan(".env") is False

    def test_credentials_excluded(self):
        from main import _should_scan
        assert _should_scan("secrets/credentials.json") is False

    def test_node_modules_excluded(self):
        from main import _should_scan
        assert _should_scan("node_modules/package/index.js") is False

    def test_git_dir_excluded(self):
        from main import _should_scan
        assert _should_scan(".git/config") is False

    def test_pycache_excluded(self):
        from main import _should_scan
        assert _should_scan("__pycache__/module.cpython-311.pyc") is False

    def test_binary_excluded(self):
        from main import _should_scan
        assert _should_scan("image.png") is False

    def test_data_dir_excluded(self):
        from main import _should_scan
        assert _should_scan("data/scan_state.json") is False


class TestChunking:
    """Test text chunking logic."""

    def test_basic_chunking(self):
        from main import chunk_text, CHUNK_SIZE
        text = "A" * 2000
        chunks = chunk_text(text, "test.md")
        assert len(chunks) > 1
        for c in chunks:
            assert len(c["text"]) <= CHUNK_SIZE

    def test_empty_text_no_chunks(self):
        from main import chunk_text
        chunks = chunk_text("", "empty.md")
        assert chunks == []

    def test_short_text_skipped(self):
        from main import chunk_text
        # Text < 50 chars should be skipped
        chunks = chunk_text("too short", "tiny.md")
        assert chunks == []

    def test_chunk_metadata(self):
        from main import chunk_text
        text = "X" * 1000
        chunks = chunk_text(text, "meta.md")
        assert len(chunks) >= 1
        assert chunks[0]["source"] == "meta.md"
        assert chunks[0]["chunk_index"] == 0
        assert "offset" in chunks[0]

    def test_chunk_overlap(self):
        from main import chunk_text, CHUNK_SIZE, CHUNK_OVERLAP
        text = "A" * (CHUNK_SIZE * 3)
        chunks = chunk_text(text, "overlap.md")
        if len(chunks) >= 2:
            # Second chunk should start before where first chunk ends
            step = CHUNK_SIZE - CHUNK_OVERLAP
            assert chunks[1]["offset"] == step


class TestPointId:
    """Test deterministic ID generation."""

    def test_deterministic(self):
        from main import _generate_point_id
        id1 = _generate_point_id("test.md", 0)
        id2 = _generate_point_id("test.md", 0)
        assert id1 == id2

    def test_different_source(self):
        from main import _generate_point_id
        id1 = _generate_point_id("a.md", 0)
        id2 = _generate_point_id("b.md", 0)
        assert id1 != id2

    def test_different_chunk_index(self):
        from main import _generate_point_id
        id1 = _generate_point_id("test.md", 0)
        id2 = _generate_point_id("test.md", 1)
        assert id1 != id2

    def test_returns_int(self):
        from main import _generate_point_id
        pid = _generate_point_id("test.md", 42)
        assert isinstance(pid, int)


class TestCatalogProjectId:
    """Test catalog project ID generation."""

    def test_deterministic(self):
        from catalog import _project_id
        id1 = _project_id("infra-mcp")
        id2 = _project_id("infra-mcp")
        assert id1 == id2

    def test_different_names(self):
        from catalog import _project_id
        id1 = _project_id("infra-mcp")
        id2 = _project_id("devops-agent")
        assert id1 != id2

    def test_returns_int(self):
        from catalog import _project_id
        assert isinstance(_project_id("test"), int)


class TestScanState:
    """Test scan state persistence."""

    def test_load_missing_state(self, tmp_path, monkeypatch):
        from main import load_scan_state
        monkeypatch.setattr("main.STATE_FILE", str(tmp_path / "nonexistent.json"))
        state = load_scan_state()
        assert state == {}

    def test_save_and_load(self, tmp_path, monkeypatch):
        import main
        state_file = str(tmp_path / "state.json")
        monkeypatch.setattr("main.STATE_FILE", state_file)
        monkeypatch.setattr("main.DATA_DIR", str(tmp_path))

        test_state = {"/some/path": {"last_commit": "abc123", "last_scan": "2026-03-13T00:00:00Z"}}
        main.save_scan_state(test_state)
        loaded = main.load_scan_state()
        assert loaded == test_state

    def test_load_corrupt_state(self, tmp_path, monkeypatch):
        from main import load_scan_state
        state_file = tmp_path / "corrupt.json"
        state_file.write_text("not json{{{")
        monkeypatch.setattr("main.STATE_FILE", str(state_file))
        state = load_scan_state()
        assert state == {}


class TestAuditLog:
    """Test audit logging."""

    def test_audit_creates_file(self, tmp_path, monkeypatch):
        import main
        audit_file = str(tmp_path / "audit.jsonl")
        monkeypatch.setattr("main.AUDIT_FILE", audit_file)
        monkeypatch.setattr("main.DATA_DIR", str(tmp_path))

        main.audit_log({"action": "test", "detail": "unit test"})

        with open(audit_file) as f:
            lines = f.readlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["action"] == "test"
        assert "timestamp" in entry

    def test_audit_appends(self, tmp_path, monkeypatch):
        import main
        audit_file = str(tmp_path / "audit.jsonl")
        monkeypatch.setattr("main.AUDIT_FILE", audit_file)
        monkeypatch.setattr("main.DATA_DIR", str(tmp_path))

        main.audit_log({"action": "first"})
        main.audit_log({"action": "second"})

        with open(audit_file) as f:
            lines = f.readlines()
        assert len(lines) == 2


class TestAnalyzerParsing:
    """Test LLM response parsing (no network needed)."""

    def test_parse_clean_json(self):
        from analyzer import _parse_json_response
        result = _parse_json_response('{"key": "value"}')
        assert result == {"key": "value"}

    def test_parse_markdown_wrapped(self):
        from analyzer import _parse_json_response
        result = _parse_json_response('```json\n{"key": "value"}\n```')
        assert result == {"key": "value"}

    def test_parse_invalid_json(self):
        from analyzer import _parse_json_response
        with pytest.raises(json.JSONDecodeError):
            _parse_json_response("not json at all")

    def test_analyze_short_chunk_skipped(self):
        from analyzer import analyze_chunk
        analysis, model = analyze_chunk("short", "test.py", "code")
        assert analysis["relevance_score"] == 0.1
        assert model == "skipped"


class TestSeedCatalogData:
    """Validate seed data structure without network."""

    def test_all_projects_have_required_fields(self):
        from seed_catalog import PROJECTS
        for p in PROJECTS:
            assert "name" in p, f"Missing name in project"
            assert "description" in p, f"Missing description in {p.get('name')}"
            assert "type" in p, f"Missing type in {p['name']}"
            assert "stack" in p, f"Missing stack in {p['name']}"
            assert "status" in p, f"Missing status in {p['name']}"
            assert "tags" in p, f"Missing tags in {p['name']}"
            assert isinstance(p["stack"], list), f"stack should be list in {p['name']}"
            assert isinstance(p["tags"], list), f"tags should be list in {p['name']}"

    def test_unique_project_names(self):
        from seed_catalog import PROJECTS
        names = [p["name"] for p in PROJECTS]
        assert len(names) == len(set(names)), "Duplicate project names in seed data"

    def test_project_count(self):
        from seed_catalog import PROJECTS
        assert len(PROJECTS) >= 8, "Expected at least 8 seed projects"


# ===========================================================================
# NETWORK TESTS — require Qdrant + Ollama running
# ===========================================================================


@pytest.mark.network
class TestCatalogCRUD:
    """Test catalog CRUD operations against live Qdrant + Ollama."""

    TEST_PROJECT = {
        "name": "_test_project_erudito",
        "description": "Temporary test project for pytest — should be cleaned up",
        "type": "test",
        "stack": ["python", "pytest"],
        "status": "experimental",
        "tags": ["test", "temporary"],
    }

    def test_upsert_project(self):
        from catalog import upsert_project
        pid = upsert_project(self.TEST_PROJECT)
        assert isinstance(pid, int)

    def test_list_contains_project(self):
        from catalog import upsert_project, list_projects
        upsert_project(self.TEST_PROJECT)
        projects = list_projects()
        names = [p["name"] for p in projects]
        assert "_test_project_erudito" in names

    def test_get_project(self):
        from catalog import upsert_project, get_project
        upsert_project(self.TEST_PROJECT)
        p = get_project("_test_project_erudito")
        assert p is not None
        assert p["name"] == "_test_project_erudito"
        assert p["type"] == "test"

    def test_search_projects(self):
        from catalog import upsert_project, search_projects
        upsert_project(self.TEST_PROJECT)
        results = search_projects("temporary test project pytest")
        assert len(results) > 0
        # The test project should be in results (possibly not #1)
        names = [r["name"] for r in results]
        assert "_test_project_erudito" in names

    def test_update_project(self):
        from catalog import upsert_project, get_project
        upsert_project(self.TEST_PROJECT)
        updated = {**self.TEST_PROJECT, "version": "99.0.0"}
        upsert_project(updated)
        p = get_project("_test_project_erudito")
        assert p["version"] == "99.0.0"

    def test_delete_project(self):
        from catalog import upsert_project, delete_project, get_project
        upsert_project(self.TEST_PROJECT)
        delete_project("_test_project_erudito")
        p = get_project("_test_project_erudito")
        # After delete, get_project should return None (or the point is gone)
        # Qdrant may return 404 which get_project handles
        assert p is None

    def test_count_projects(self):
        from catalog import count_projects
        count = count_projects()
        assert isinstance(count, int)
        assert count >= 0


@pytest.mark.network
class TestEmbedding:
    """Test Ollama embedding connectivity."""

    def test_embed_text(self):
        from catalog import _embed
        vec = _embed("test embedding for erudito")
        assert isinstance(vec, list)
        assert len(vec) == 768  # nomic-embed-text dimension

    def test_embed_different_texts(self):
        from catalog import _embed
        v1 = _embed("python fastapi infrastructure")
        v2 = _embed("cooking recipes for dinner")
        # Vectors should be different
        assert v1 != v2


@pytest.mark.network
class TestEruditoEmbedBatch:
    """Test batch embedding from main module."""

    def test_embed_texts_batch(self):
        from main import embed_texts
        texts = ["first text for embedding", "second different text"]
        embeddings = embed_texts(texts)
        assert len(embeddings) == 2
        assert len(embeddings[0]) == 768


# ===========================================================================
# INTEGRATION TESTS — require Erudito service running on :8095
# ===========================================================================


@pytest.mark.integration
class TestEruditoHealth:
    def test_health(self):
        r = erudito_get("/health")
        assert r["status"] in ("ok", "degraded")
        assert r["agent"] == "erudito"
        assert "version" in r

    def test_status(self):
        r = erudito_get("/status")
        assert "scan_sources" in r
        assert r["scan_sources"] > 0
        assert "sources" in r


@pytest.mark.integration
class TestEruditoScan:
    def test_scan_triggers(self):
        r = erudito_post("/scan")
        assert r["status"] == "completed"
        assert "stats" in r
        stats = r["stats"]
        assert "scanned" in stats
        assert "inserted" in stats

    def test_scan_single_node(self):
        r = erudito_post("/scan/hanzo")
        assert r["status"] == "completed"
        assert r["node"] == "hanzo"

    def test_scan_unknown_node(self):
        try:
            erudito_post("/scan/nonexistent")
            assert False, "Should have raised"
        except urllib.error.HTTPError as e:
            assert e.code == 404


@pytest.mark.integration
class TestEruditoAudit:
    def test_audit_trail(self):
        r = erudito_get("/audit?limit=10")
        assert "entries" in r
        assert isinstance(r["entries"], list)

    def test_audit_has_timestamps(self):
        r = erudito_get("/audit?limit=5")
        for entry in r["entries"]:
            assert "timestamp" in entry
            assert "action" in entry


@pytest.mark.integration
class TestEruditoStaleness:
    def test_staleness_cleanup(self):
        r = erudito_delete("/staleness/cleanup", {"days": "9999"})
        assert r["status"] == "completed"
        assert "deleted" in r
        # With a very long horizon, nothing should be stale
        assert r["deleted"] == 0
