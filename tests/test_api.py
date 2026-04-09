import pytest


@pytest.mark.integration
class TestHealthEndpoint:
    def test_health_returns_200(self):
        import httpx
        resp = httpx.get("http://localhost:8095/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data

    def test_health_contains_components(self):
        import httpx
        resp = httpx.get("http://localhost:8095/health")
        data = resp.json()
        assert "qdrant" in data
        assert "redis" in data


@pytest.mark.integration
class TestRegistryEndpoints:
    def test_list_registry(self):
        import httpx
        resp = httpx.get("http://localhost:8095/registry")
        assert resp.status_code == 200
        assert "projects" in resp.json()


@pytest.mark.integration
class TestCoherenceEndpoint:
    def test_post_coherence_score(self):
        import httpx
        resp = httpx.post("http://localhost:8095/metrics/coherence", json={"score": 85.0, "timestamp": "2026-03-18T01:00:00Z"})
        assert resp.status_code == 200
        metrics = httpx.get("http://localhost:8095/metrics").json()
        assert metrics["coherence_last_score"] == 85.0


@pytest.mark.integration
class TestMetricsEndpoint:
    def test_metrics_returns_coverage(self):
        import httpx
        resp = httpx.get("http://localhost:8095/metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert "coverage" in data
        assert "freshness_avg_minutes" in data

    def test_metrics_includes_curation(self):
        import httpx
        resp = httpx.get("http://localhost:8095/metrics")
        data = resp.json()
        assert "curation_pct" in data["coverage"]
        assert "curation_pending" in data["coverage"]


@pytest.mark.integration
class TestSearchEndpoint:
    def test_search_requires_query(self):
        import httpx
        resp = httpx.get("http://localhost:8095/search")
        assert resp.status_code == 422
