# tests/test_registry.py
import os
import tempfile
import pytest
import yaml
from core.registry import Registry


@pytest.fixture
def tmp_registry(tmp_path):
    """Create a Registry with temp YAML path and no Redis."""
    yaml_path = tmp_path / "registry.yaml"
    return Registry(yaml_path=str(yaml_path), redis_url=None)


class TestRegistryOperations:
    def test_register_project(self, tmp_registry):
        tmp_registry.register("infra-mcp", path="~/ai-lab/infra-mcp", node="hanzo", repo="r0calex/infra-mcp")
        project = tmp_registry.get("infra-mcp")
        assert project["path"] == "~/ai-lab/infra-mcp"
        assert project["node"] == "hanzo"
        assert project["status"] == "pending"
        assert project["notebook_id"] is None

    def test_register_duplicate_name_raises(self, tmp_registry):
        tmp_registry.register("x", path="/a", node="hanzo", repo="r/x")
        with pytest.raises(ValueError, match="already registered"):
            tmp_registry.register("x", path="/b", node="hanzo", repo="r/x2")

    def test_register_duplicate_path_raises(self, tmp_registry):
        tmp_registry.register("a", path="/same", node="hanzo", repo="r/a")
        with pytest.raises(ValueError, match="already registered"):
            tmp_registry.register("b", path="/same", node="hanzo", repo="r/b")

    def test_update_sync(self, tmp_registry):
        tmp_registry.register("p", path="/p", node="hanzo", repo="r/p")
        tmp_registry.update_sync("p", new_hash="abc123", notebook_id="nb-1", doc_count=5)
        project = tmp_registry.get("p")
        assert project["status"] == "synced"
        assert project["last_hash"] == "abc123"
        assert project["notebook_id"] == "nb-1"
        assert project["doc_count"] == 5

    def test_mark_validated(self, tmp_registry):
        tmp_registry.register("p", path="/p", node="hanzo", repo="r/p")
        tmp_registry.update_sync("p", new_hash="abc")
        tmp_registry.mark_validated("p", coverage=0.85)
        project = tmp_registry.get("p")
        assert project["status"] == "validated"
        assert project["coverage"] == 0.85

    def test_mark_stale(self, tmp_registry):
        tmp_registry.register("p", path="/p", node="hanzo", repo="r/p")
        tmp_registry.update_sync("p", new_hash="abc")
        tmp_registry.mark_validated("p", coverage=0.9)
        tmp_registry.mark_stale("p")
        assert tmp_registry.get("p")["status"] == "stale"

    def test_list_by_status(self, tmp_registry):
        tmp_registry.register("a", path="/a", node="hanzo", repo="r/a")
        tmp_registry.register("b", path="/b", node="hanzo", repo="r/b")
        tmp_registry.update_sync("a", new_hash="x")
        pending = tmp_registry.list_by_status("pending")
        synced = tmp_registry.list_by_status("synced")
        assert len(pending) == 1
        assert pending[0] == "b"
        assert len(synced) == 1
        assert synced[0] == "a"

    def test_summary(self, tmp_registry):
        tmp_registry.register("a", path="/a", node="hanzo", repo="r/a")
        tmp_registry.register("b", path="/b", node="hanzo", repo="r/b")
        tmp_registry.update_sync("a", new_hash="x")
        tmp_registry.mark_validated("a", coverage=0.8)
        s = tmp_registry.summary()
        assert s["total"] == 2
        assert s["validated"] == 1
        assert s["pending"] == 1
        assert s["coverage_pct"] == 50.0

    def test_get_nonexistent_returns_none(self, tmp_registry):
        assert tmp_registry.get("nope") is None


class TestRegistryPersistence:
    def test_yaml_roundtrip(self, tmp_registry):
        tmp_registry.register("p", path="/p", node="hanzo", repo="r/p")
        # Load fresh instance from same file
        fresh = Registry(yaml_path=tmp_registry.yaml_path, redis_url=None)
        assert fresh.get("p")["path"] == "/p"

    def test_version_increments(self, tmp_registry):
        tmp_registry.register("a", path="/a", node="hanzo", repo="r/a")
        v1 = tmp_registry.version
        tmp_registry.register("b", path="/b", node="hanzo", repo="r/b")
        assert tmp_registry.version == v1 + 1

    def test_list_all(self, tmp_registry):
        tmp_registry.register("a", path="/a", node="hanzo", repo="r/a")
        tmp_registry.register("b", path="/b", node="hanzo", repo="r/b")
        all_projects = tmp_registry.list_all()
        assert set(all_projects) == {"a", "b"}
