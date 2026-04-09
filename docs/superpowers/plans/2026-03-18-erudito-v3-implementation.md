# Erudito v3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reengineer Erudito from a monolithic RAG agent into a modular knowledge orchestrator that uses NotebookLM as its consultation/validation tool while keeping Qdrant as the autonomous persistent knowledge base.

**Architecture:** Single FastAPI process with modular domain separation (`core/` for business logic, `integrations/` for external services). Dual-write registry (YAML + Redis) as the central map. Scanner detects git deltas, Indexer embeds to Qdrant, NotebookLM cycle validates and enriches. Query engine routes by confidence threshold (0.75).

**Tech Stack:** Python 3.12, FastAPI 0.115.0, Uvicorn 0.30.0, Qdrant (REST API), Redis (via redis-py), Ollama (nomic-embed-text), LiteLLM proxy, NotebookLM MCP (via LiteLLM), HTTPX for async HTTP.

**Spec:** `docs/superpowers/specs/2026-03-18-erudito-v3-design.md`

**V2 Codebase Reference:**
- `main.py` (934 lines) — monolith with scanning, indexing, search, catalog
- `analyzer.py` (262 lines) — LLM cascade for chunk analysis
- `catalog.py` (194 lines) — being removed, replaced by registry
- `tests/test_erudito.py` (525 lines, 56 tests) — good unit test base
- `data/scan_state.json` — current state to migrate

**Reusable v2 code:** `chunk_text`, `embed_texts`, `_qdrant_request`, `_has_poison`, `scan_repo`, `_find_git_root`, `_should_scan`, `audit_log`, LLM cascade from `analyzer.py`. These will be extracted and refactored into the new module structure.

---

## File Structure

```
erudito/
├── main.py                    # FastAPI app, lifespan, all endpoints, background scan task
├── core/
│   ├── __init__.py
│   ├── registry.py            # Triple map (YAML + Redis dual-write, version counter)
│   ├── scanner.py             # Delta detection (git hash diff, enrichment, sanitization)
│   ├── indexer.py             # Qdrant embedding pipeline (chunk, embed, upsert)
│   ├── query.py               # Search + confidence routing (0.75 threshold)
│   └── enricher.py            # Auto-enrich missing frontmatter metadata
├── integrations/
│   ├── __init__.py
│   ├── notebooklm.py          # NotebookLM MCP client (via LiteLLM HTTP)
│   ├── sanitizer.py           # HTTP client → sanitizer-hanzo
│   └── github.py              # Placeholder (future)
├── data/
│   ├── registry.yaml          # Map state (created at runtime)
│   └── audit.jsonl            # Operations log (created at runtime)
├── tests/
│   ├── conftest.py            # Fixtures, markers
│   ├── test_enricher.py       # Pure function tests
│   ├── test_registry.py       # YAML + Redis dual-write tests
│   ├── test_scanner.py        # Git delta detection tests
│   ├── test_indexer.py        # Chunking + embedding tests
│   ├── test_query.py          # Confidence routing tests
│   ├── test_sanitizer.py      # Sanitizer client tests
│   ├── test_notebooklm.py     # NotebookLM client tests
│   └── test_api.py            # Integration tests (endpoints)
├── migrate_v2.py              # One-shot migration: scan_state.json → registry.yaml
├── Dockerfile
├── docker-compose.yaml
└── requirements.txt
```

---

### Task 1: Project Scaffolding

**Files:**
- Create: `core/__init__.py`
- Create: `integrations/__init__.py`
- Create: `requirements.txt` (overwrite v2)
- Create: `tests/conftest.py` (overwrite v2)

- [ ] **Step 1: Create directory structure**

```bash
cd ~/ai-lab/erudito
mkdir -p core integrations
```

- [ ] **Step 2: Create core/__init__.py**

```python
# Erudito v3 — Core modules
```

- [ ] **Step 3: Create integrations/__init__.py**

```python
# Erudito v3 — External service integrations
```

- [ ] **Step 3b: Create integrations/github.py placeholder**

```python
"""GitHub repo sync integration (future).

Placeholder for backlog item: sync registry with repo state,
detect new repos, auto-register.
"""
# Not yet implemented
```

- [ ] **Step 4: Write requirements.txt**

```
fastapi==0.115.0
uvicorn==0.30.0
httpx>=0.27.0
pyyaml>=6.0.1
redis>=5.0.0
```

- [ ] **Step 5: Write tests/conftest.py**

```python
import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "network: requires Qdrant/Ollama")
    config.addinivalue_line("markers", "integration: requires running Erudito service")
    config.addinivalue_line("markers", "redis: requires Redis")
```

- [ ] **Step 6: Verify structure**

Run: `find ~/ai-lab/erudito/core ~/ai-lab/erudito/integrations ~/ai-lab/erudito/tests -type f | sort`

Expected: all __init__.py, github.py placeholder, and conftest.py files present.

- [ ] **Step 7: Commit**

```bash
git add core/__init__.py integrations/__init__.py integrations/github.py requirements.txt tests/conftest.py
git commit -m "chore(erudito-v3): scaffold project structure"
```

---

### Task 2: Enricher Module (Pure Functions, No Dependencies)

**Files:**
- Create: `core/enricher.py`
- Create: `tests/test_enricher.py`

The enricher is pure functions with zero external dependencies — ideal first module to build and test.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_enricher.py
import pytest
from core.enricher import parse_frontmatter, infer_type, enrich_content


class TestParseFrontmatter:
    def test_complete_frontmatter(self):
        content = "---\nproject: infra-mcp\ntype: spec\nlast_updated: 2026-03-17\nstatus: active\n---\n# Content"
        fm, body = parse_frontmatter(content)
        assert fm["project"] == "infra-mcp"
        assert fm["type"] == "spec"
        assert body == "# Content"

    def test_no_frontmatter(self):
        content = "# Just a heading\nSome text"
        fm, body = parse_frontmatter(content)
        assert fm == {}
        assert body == content

    def test_partial_frontmatter(self):
        content = "---\nproject: erudito\n---\n# Content"
        fm, body = parse_frontmatter(content)
        assert fm["project"] == "erudito"
        assert "type" not in fm

    def test_empty_content(self):
        fm, body = parse_frontmatter("")
        assert fm == {}
        assert body == ""


class TestInferType:
    def test_readme(self):
        assert infer_type("README.md") == "readme"

    def test_spec(self):
        assert infer_type("2026-03-18-erudito-v3-design-spec.md") == "spec"

    def test_sop(self):
        assert infer_type("SOP.md") == "sop"
        assert infer_type("deployment-sop.md") == "sop"

    def test_ir(self):
        assert infer_type("IR-v2-migration.md") == "ir"
        assert infer_type("implementation-report.md") == "ir"

    def test_backlog(self):
        assert infer_type("BACKLOG.md") == "backlog"

    def test_design(self):
        assert infer_type("system-design.md") == "design"

    def test_generic_fallback(self):
        assert infer_type("notes.md") == "doc"
        assert infer_type("TODO.md") == "doc"


class TestEnrichContent:
    def test_already_complete(self):
        content = "---\nproject: x\ntype: spec\nlast_updated: 2026-03-17\nstatus: active\n---\n# X"
        result, enriched = enrich_content(content, "x.md", "my-project", "2026-03-17")
        assert enriched is False
        assert "auto_enriched" not in result or "auto_enriched: false" in result

    def test_no_frontmatter_adds_all(self):
        content = "# My Doc\nSome content"
        result, enriched = enrich_content(content, "README.md", "infra-mcp", "2026-03-18")
        assert enriched is True
        assert "project: infra-mcp" in result
        assert "type: readme" in result
        assert "auto_enriched: true" in result

    def test_partial_frontmatter_fills_missing(self):
        content = "---\nproject: erudito\n---\n# Content"
        result, enriched = enrich_content(content, "spec-v3.md", "erudito", "2026-03-18")
        assert enriched is True
        assert "type: spec" in result
        assert "project: erudito" in result
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/ai-lab/erudito && python -m pytest tests/test_enricher.py -v`
Expected: FAIL — ImportError (module doesn't exist yet)

- [ ] **Step 3: Implement core/enricher.py**

```python
"""Auto-enrich missing frontmatter metadata for .md files.

Enrichment happens in memory — original files are never modified.
"""
import re
from typing import Optional

# Required frontmatter fields
REQUIRED_FIELDS = {"project", "type", "last_updated", "status"}

# Type inference rules (no LLM required)
_TYPE_RULES = [
    (re.compile(r"README", re.IGNORECASE), "readme"),
    (re.compile(r"spec", re.IGNORECASE), "spec"),
    (re.compile(r"\bsop\b", re.IGNORECASE), "sop"),
    (re.compile(r"\bir\b|implementation.report", re.IGNORECASE), "ir"),
    (re.compile(r"backlog", re.IGNORECASE), "backlog"),
    (re.compile(r"design", re.IGNORECASE), "design"),
]


def parse_frontmatter(content: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from markdown content.

    Returns (frontmatter_dict, body_without_frontmatter).
    If no frontmatter found, returns ({}, original_content).
    """
    if not content or not content.startswith("---"):
        return {}, content

    end = content.find("---", 3)
    if end == -1:
        return {}, content

    raw = content[3:end].strip()
    body = content[end + 3:].lstrip("\n")
    fm = {}
    for line in raw.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            fm[key.strip()] = val.strip()
    return fm, body


def infer_type(filename: str) -> str:
    """Infer document type from filename using pattern rules."""
    for pattern, doc_type in _TYPE_RULES:
        if pattern.search(filename):
            return doc_type
    return "doc"


def enrich_content(
    content: str,
    filename: str,
    project_name: str,
    git_date: str,
    status: str = "active",
) -> tuple[str, bool]:
    """Enrich markdown content with frontmatter if missing or incomplete.

    Returns (enriched_content, was_enriched).
    Enrichment is in-memory only — never writes to disk.
    """
    fm, body = parse_frontmatter(content)

    missing = REQUIRED_FIELDS - set(fm.keys())
    if not missing:
        return content, False

    # Fill missing fields
    if "project" not in fm:
        fm["project"] = project_name
    if "type" not in fm:
        fm["type"] = infer_type(filename)
    if "last_updated" not in fm:
        fm["last_updated"] = git_date
    if "status" not in fm:
        fm["status"] = status
    fm["auto_enriched"] = "true"

    # Rebuild content with frontmatter
    lines = ["---"]
    for key in ["project", "type", "last_updated", "status", "auto_enriched"]:
        if key in fm:
            lines.append(f"{key}: {fm[key]}")
    # Preserve any extra fields from original frontmatter
    for key, val in fm.items():
        if key not in {"project", "type", "last_updated", "status", "auto_enriched"}:
            lines.append(f"{key}: {val}")
    lines.append("---")
    lines.append(body)

    return "\n".join(lines), True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/ai-lab/erudito && python -m pytest tests/test_enricher.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add core/enricher.py tests/test_enricher.py
git commit -m "feat(erudito-v3): add enricher module with frontmatter auto-enrichment"
```

---

### Task 3: Sanitizer Client

**Files:**
- Create: `integrations/sanitizer.py`
- Create: `tests/test_sanitizer.py`

HTTP client for sanitizer-hanzo. Falls back to local regex patterns if unavailable.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sanitizer.py
import pytest
from integrations.sanitizer import sanitize_text, _local_poison_check, POISON_PATTERNS


class TestLocalPoisonCheck:
    def test_detects_api_key(self):
        text = 'api_key = "sk-proj-abc123xyz456def789"'
        assert _local_poison_check(text) is True

    def test_detects_postgres_uri(self):
        text = "postgresql://admin:secretpass@localhost/db"
        assert _local_poison_check(text) is True

    def test_detects_bearer_token(self):
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abc"
        assert _local_poison_check(text) is True

    def test_clean_text_passes(self):
        text = "This is a normal README about the project architecture."
        assert _local_poison_check(text) is False

    def test_detects_private_key(self):
        text = "-----BEGIN RSA PRIVATE KEY-----"
        assert _local_poison_check(text) is True


class TestSanitizeText:
    @pytest.mark.network
    async def test_sanitize_via_service(self):
        """Requires sanitizer-hanzo running on localhost:8086."""
        result = await sanitize_text("password=secret123456", sanitizer_url="http://localhost:8086")
        assert "secret123456" not in result["sanitized"]
        assert result["masked_count"] > 0

    async def test_fallback_on_unavailable(self):
        """When sanitizer is unreachable, falls back to local check."""
        result = await sanitize_text(
            "Normal text without secrets",
            sanitizer_url="http://localhost:99999",
        )
        assert result["sanitized"] == "Normal text without secrets"
        assert result["masked_count"] == 0
        assert result["fallback"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/ai-lab/erudito && python -m pytest tests/test_sanitizer.py -v -m "not network" -k "not test_sanitize_via_service"`
Expected: FAIL — ImportError

- [ ] **Step 3: Implement integrations/sanitizer.py**

```python
"""HTTP client for sanitizer-hanzo with local regex fallback.

Calls sanitizer-hanzo POST /sanitize for text masking.
If the service is unavailable, falls back to local regex patterns
(same as v2's _has_poison but returns masked text instead of bool).
"""
import os
import re
import logging
import httpx

logger = logging.getLogger("erudito.sanitizer")

SANITIZER_URL = os.getenv("SANITIZER_URL", "http://localhost:8086")

# Local fallback patterns (subset of kubo-sanitizer's 16 patterns)
POISON_PATTERNS = [
    re.compile(r"password\s*[:=]\s*['\"]?[^\s'\"]{8,}", re.IGNORECASE),
    re.compile(r"api[_-]?key\s*[:=]\s*['\"]?[a-zA-Z0-9_-]{20,}", re.IGNORECASE),
    re.compile(r"token\s*[:=]\s*['\"]?[a-zA-Z0-9._-]{20,}", re.IGNORECASE),
    re.compile(r"postgresql://[^:]+:[^@]+@"),
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),
    re.compile(r"Bearer\s+[a-zA-Z0-9._-]{20,}"),
    re.compile(r"BEGIN\s+(RSA|DSA|EC|OPENSSH)\s+PRIVATE\s+KEY"),
    re.compile(r"ghp_[a-zA-Z0-9]{36}"),
]


def _local_poison_check(text: str) -> bool:
    """Check if text contains secrets using local regex patterns."""
    return any(p.search(text) for p in POISON_PATTERNS)


def _local_mask(text: str) -> tuple[str, int]:
    """Mask secrets locally as fallback. Returns (masked_text, count)."""
    count = 0
    result = text
    for p in POISON_PATTERNS:
        matches = p.findall(result)
        if matches:
            count += len(matches)
            result = p.sub("[MASKED]", result)
    return result, count


async def sanitize_text(
    text: str,
    sanitizer_url: str | None = None,
    context: str | None = None,
) -> dict:
    """Sanitize text via sanitizer-hanzo, with local fallback.

    Returns dict with keys: sanitized, masked_count, patterns_matched, fallback.
    """
    url = sanitizer_url or SANITIZER_URL
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{url}/sanitize",
                json={"text": text, "context": context},
            )
            resp.raise_for_status()
            data = resp.json()
            return {
                "sanitized": data["sanitized"],
                "masked_count": data.get("masked_count", 0),
                "patterns_matched": data.get("patterns_matched", []),
                "fallback": False,
            }
    except Exception as e:
        logger.warning(f"Sanitizer unavailable ({e}), using local fallback")
        masked, count = _local_mask(text)
        return {
            "sanitized": masked,
            "masked_count": count,
            "patterns_matched": [],
            "fallback": True,
        }
```

- [ ] **Step 4: Run unit tests to verify they pass**

Run: `cd ~/ai-lab/erudito && python -m pytest tests/test_sanitizer.py -v -k "TestLocalPoisonCheck or test_fallback"`
Expected: All PASS (local tests, no network needed)

- [ ] **Step 5: Commit**

```bash
git add integrations/sanitizer.py tests/test_sanitizer.py
git commit -m "feat(erudito-v3): add sanitizer client with local fallback"
```

---

### Task 4: Registry Module (YAML + Redis Dual-Write)

**Files:**
- Create: `core/registry.py`
- Create: `tests/test_registry.py`

This is the central data structure — test thoroughly.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/ai-lab/erudito && python -m pytest tests/test_registry.py -v`
Expected: FAIL — ImportError

- [ ] **Step 3: Implement core/registry.py**

```python
"""Triple map registry: project ↔ notebook ↔ repo.

Dual-write to YAML (human-readable, git-versionable) and Redis (crash recovery).
Version counter for conflict resolution on startup.
"""
import os
import logging
import threading
import yaml
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("erudito.registry")

_DEFAULT_ENTRY = {
    "path": None,
    "node": None,
    "repo": None,
    "notebook_id": None,
    "last_hash": None,
    "last_sync": None,
    "status": "pending",
    "coverage": 0.0,
    "auto_enriched": False,
    "doc_count": 0,
    "last_nlm_session": None,
    "nlm_source_count": 0,
}


class Registry:
    """Project registry with YAML + Redis dual-write."""

    def __init__(self, yaml_path: str = "data/registry.yaml", redis_url: str | None = None):
        self.yaml_path = yaml_path
        self._redis = None
        self._redis_url = redis_url
        self._data: dict = {"version": 0, "projects": {}}
        self._lock = threading.Lock()
        self._load()

    @property
    def version(self) -> int:
        return self._data["version"]

    def _load(self):
        """Load and reconcile YAML + Redis state."""
        yaml_data = self._load_yaml()
        redis_data = self._load_redis()

        if yaml_data and not redis_data:
            self._data = yaml_data
            self._save_redis()
        elif redis_data and not yaml_data:
            self._data = redis_data
            self._save_yaml()
        elif yaml_data and redis_data:
            # Highest version wins
            if redis_data.get("version", 0) > yaml_data.get("version", 0):
                self._data = redis_data
                self._save_yaml()
            else:
                self._data = yaml_data
                self._save_redis()
        # else: both empty, use defaults

    def _load_yaml(self) -> dict | None:
        try:
            with open(self.yaml_path, "r") as f:
                data = yaml.safe_load(f)
                if data and "projects" in data:
                    return data
        except (FileNotFoundError, yaml.YAMLError):
            pass
        return None

    def _save_yaml(self):
        Path(self.yaml_path).parent.mkdir(parents=True, exist_ok=True)
        with open(self.yaml_path, "w") as f:
            yaml.dump(self._data, f, default_flow_style=False, sort_keys=False)

    def _load_redis(self) -> dict | None:
        r = self._get_redis()
        if not r:
            return None
        try:
            import json
            raw = r.get("erudito:registry")
            if raw:
                return json.loads(raw)
        except Exception as e:
            logger.warning(f"Redis load failed: {e}")
        return None

    def _save_redis(self):
        r = self._get_redis()
        if not r:
            return
        try:
            import json
            r.set("erudito:registry", json.dumps(self._data, default=str))
        except Exception as e:
            logger.warning(f"Redis save failed: {e}")

    def _get_redis(self):
        if self._redis_url is None:
            return None
        if self._redis is None:
            try:
                import redis as redis_lib
                self._redis = redis_lib.from_url(self._redis_url)
                self._redis.ping()
            except Exception as e:
                logger.warning(f"Redis connection failed: {e}")
                self._redis = None
        return self._redis

    def _persist(self):
        """Dual-write: YAML first, then Redis. Increment version. Thread-safe."""
        with self._lock:
            self._data["version"] += 1
            self._save_yaml()
            self._save_redis()

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def register(self, name: str, path: str, node: str, repo: str):
        """Register a new project. Raises ValueError on duplicate name or path."""
        projects = self._data["projects"]
        if name in projects:
            raise ValueError(f"Project '{name}' already registered")
        for existing_name, entry in projects.items():
            if entry["path"] == path:
                raise ValueError(f"Path '{path}' already registered under '{existing_name}'")
        entry = dict(_DEFAULT_ENTRY)
        entry["path"] = path
        entry["node"] = node
        entry["repo"] = repo
        projects[name] = entry
        self._persist()

    def get(self, name: str) -> dict | None:
        entry = self._data["projects"].get(name)
        if entry:
            return dict(entry)
        return None

    def update_sync(
        self,
        name: str,
        new_hash: str,
        notebook_id: str | None = None,
        doc_count: int | None = None,
    ):
        project = self._data["projects"].get(name)
        if not project:
            raise KeyError(f"Project '{name}' not found")
        project["last_hash"] = new_hash
        project["last_sync"] = self._now()
        project["status"] = "synced"
        if notebook_id is not None:
            project["notebook_id"] = notebook_id
        if doc_count is not None:
            project["doc_count"] = doc_count
        self._persist()

    def mark_validated(self, name: str, coverage: float = 0.0):
        project = self._data["projects"].get(name)
        if not project:
            raise KeyError(f"Project '{name}' not found")
        project["status"] = "validated"
        project["coverage"] = coverage
        project["last_nlm_session"] = self._now()
        self._persist()

    def mark_stale(self, name: str):
        project = self._data["projects"].get(name)
        if not project:
            raise KeyError(f"Project '{name}' not found")
        project["status"] = "stale"
        self._persist()

    def list_all(self) -> list[str]:
        return list(self._data["projects"].keys())

    def list_by_status(self, status: str) -> list[str]:
        return [
            name for name, entry in self._data["projects"].items()
            if entry["status"] == status
        ]

    def summary(self) -> dict:
        projects = self._data["projects"]
        total = len(projects)
        by_status = {}
        for entry in projects.values():
            s = entry["status"]
            by_status[s] = by_status.get(s, 0) + 1
        validated = by_status.get("validated", 0)
        synced = by_status.get("synced", 0)
        return {
            "total": total,
            "validated": validated,
            "synced": synced,
            "stale": by_status.get("stale", 0),
            "pending": by_status.get("pending", 0),
            "coverage_pct": round(validated / total * 100, 1) if total else 0.0,
            "coverage_partial_pct": round((validated + synced) / total * 100, 1) if total else 0.0,
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/ai-lab/erudito && python -m pytest tests/test_registry.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add core/registry.py tests/test_registry.py
git commit -m "feat(erudito-v3): add registry module with YAML + Redis dual-write"
```

---

### Task 5: Indexer Module (Qdrant Embedding Pipeline)

**Files:**
- Create: `core/indexer.py`
- Create: `tests/test_indexer.py`

Extracts and refactors v2's chunking, embedding, and Qdrant operations from `main.py:245-385`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_indexer.py
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
        # Verify overlap
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/ai-lab/erudito && python -m pytest tests/test_indexer.py -v`
Expected: FAIL — ImportError

- [ ] **Step 3: Implement core/indexer.py**

```python
"""Qdrant embedding pipeline: chunk, embed, upsert.

Extracts and refactors v2's chunking (main.py:245-263), embedding (main.py:286-354),
and Qdrant operations (main.py:271-344) into a clean module.
"""
import hashlib
import json
import logging
import os
import struct
from typing import Optional
from urllib.request import Request, urlopen
from urllib.error import URLError

logger = logging.getLogger("erudito.indexer")

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text:latest")

CHUNK_SIZE = 800
CHUNK_OVERLAP = 150
COLLECTION_KNOWLEDGE = "agent_knowledge"
COLLECTION_NLM_NOTES = "nlm_notes"


def generate_point_id(source: str, chunk_index: int) -> int:
    """Generate deterministic 64-bit positive integer ID from source + chunk index."""
    h = hashlib.md5(f"{source}::{chunk_index}".encode()).digest()
    return struct.unpack("Q", h[:8])[0] & 0x7FFFFFFFFFFFFFFF


def chunk_text(text: str, filename: str) -> list[dict]:
    """Split text into overlapping chunks with metadata.

    Returns list of {text, source, offset, chunk_index}.
    Filters chunks shorter than 50 chars.
    """
    if not text:
        return []
    chunks = []
    start = 0
    idx = 0
    while start < len(text):
        end = start + CHUNK_SIZE
        chunk = text[start:end]
        if len(chunk) >= 50:
            chunks.append({
                "text": chunk,
                "source": filename,
                "offset": start,
                "chunk_index": idx,
            })
            idx += 1
        start += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


def _qdrant_request(path: str, data: dict | None = None, method: str = "POST") -> dict:
    """Low-level Qdrant REST API client."""
    url = f"{QDRANT_URL}{path}"
    body = json.dumps(data).encode() if data else None
    req = Request(url, data=body, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception as e:
        logger.error(f"Qdrant request failed: {path} — {e}")
        raise


def embed_texts(texts: list[str], batch_size: int = 32) -> list[list[float]]:
    """Embed texts via Ollama in batches."""
    all_embeddings = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        for text in batch:
            data = json.dumps({"model": EMBED_MODEL, "prompt": text}).encode()
            req = Request(f"{OLLAMA_URL}/api/embeddings", data=data, method="POST")
            req.add_header("Content-Type", "application/json")
            with urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
                all_embeddings.append(result["embedding"])
    return all_embeddings


def embed_text(text: str) -> list[float]:
    """Embed a single text."""
    return embed_texts([text])[0]


def ensure_collection(name: str, vector_size: int = 768):
    """Create Qdrant collection if it doesn't exist."""
    try:
        _qdrant_request(f"/collections/{name}", method="GET")
    except Exception:
        _qdrant_request(f"/collections/{name}", data={
            "vectors": {"size": vector_size, "distance": "Cosine"},
        }, method="PUT")
        logger.info(f"Created Qdrant collection: {name}")


def upsert_points(points: list[dict], collection: str = COLLECTION_KNOWLEDGE):
    """Batch upsert points to Qdrant (100 per batch)."""
    for i in range(0, len(points), 100):
        batch = points[i:i + 100]
        _qdrant_request(f"/collections/{collection}/points", data={"points": batch}, method="PUT")


def delete_by_source(source_file: str, collection: str = COLLECTION_KNOWLEDGE):
    """Delete all points matching a source file from Qdrant."""
    _qdrant_request(f"/collections/{collection}/points/delete", data={
        "filter": {"must": [{"key": "source", "match": {"value": source_file}}]}
    })


def search(query_embedding: list[float], collection: str = COLLECTION_KNOWLEDGE,
           top_k: int = 5, project_filter: str | None = None) -> list[dict]:
    """Semantic search in Qdrant. Returns list of {score, payload}."""
    body: dict = {
        "vector": query_embedding,
        "limit": top_k,
        "with_payload": True,
    }
    if project_filter:
        body["filter"] = {"must": [{"key": "project", "match": {"value": project_filter}}]}
    result = _qdrant_request(f"/collections/{collection}/points/search", data=body)
    return [{"score": r["score"], "payload": r.get("payload", {})} for r in result.get("result", [])]


def index_delta(delta: dict) -> int:
    """Index a Delta object into Qdrant. Returns number of points upserted.

    Delta structure: {project, old_hash, new_hash, files: [{path, action, content, auto_enriched}]}
    """
    project = delta["project"]
    total = 0

    for file_info in delta["files"]:
        if file_info["action"] == "deleted":
            delete_by_source(file_info["path"])
            continue

        chunks = chunk_text(file_info["content"], file_info["path"])
        if not chunks:
            continue

        texts = [c["text"] for c in chunks]
        embeddings = embed_texts(texts)

        points = []
        for chunk, embedding in zip(chunks, embeddings):
            point_id = generate_point_id(chunk["source"], chunk["chunk_index"])
            points.append({
                "id": point_id,
                "vector": embedding,
                "payload": {
                    "text": chunk["text"],
                    "source": chunk["source"],
                    "project": project,
                    "chunk_index": chunk["chunk_index"],
                    "offset": chunk["offset"],
                    "auto_enriched": file_info.get("auto_enriched", False),
                },
            })
        upsert_points(points)
        total += len(points)

    return total
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/ai-lab/erudito && python -m pytest tests/test_indexer.py -v`
Expected: All PASS (unit tests only — chunking and point ID)

- [ ] **Step 5: Commit**

```bash
git add core/indexer.py tests/test_indexer.py
git commit -m "feat(erudito-v3): add indexer module with chunking and Qdrant pipeline"
```

---

### Task 6: Scanner Module (Delta Detection)

**Files:**
- Create: `core/scanner.py`
- Create: `tests/test_scanner.py`

Extracts and refactors v2's git scanning from `main.py:157-237`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_scanner.py
import os
import subprocess
import tempfile
import pytest
from core.scanner import find_git_root, should_scan_file, compute_delta


class TestShouldScanFile:
    def test_md_files_allowed(self):
        assert should_scan_file("README.md") is True

    def test_py_files_excluded(self):
        assert should_scan_file("main.py") is False

    def test_env_excluded(self):
        assert should_scan_file(".env") is False

    def test_credentials_excluded(self):
        assert should_scan_file("credentials.json") is False

    def test_excluded_dir(self):
        assert should_scan_file("node_modules/README.md") is False
        assert should_scan_file(".git/config") is False


class TestFindGitRoot:
    def test_finds_root(self, tmp_path):
        # Create a git repo
        subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
        subdir = tmp_path / "sub" / "deep"
        subdir.mkdir(parents=True)
        root = find_git_root(str(subdir))
        assert root == str(tmp_path)

    def test_returns_none_for_non_git(self, tmp_path):
        root = find_git_root(str(tmp_path))
        assert root is None


class TestComputeDelta:
    def test_no_changes(self, tmp_path):
        # Create git repo with a commit
        subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@test.com"], capture_output=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "test"], capture_output=True)
        readme = tmp_path / "README.md"
        readme.write_text("# Test")
        subprocess.run(["git", "-C", str(tmp_path), "add", "."], capture_output=True)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "init"], capture_output=True)

        head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()

        delta = compute_delta("test-project", str(tmp_path), head)
        assert delta is None  # No changes
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/ai-lab/erudito && python -m pytest tests/test_scanner.py -v`
Expected: FAIL — ImportError

- [ ] **Step 3: Implement core/scanner.py**

```python
"""Delta detection via git hash comparison.

Scans project repos for .md file changes since the last known commit.
Produces Delta objects consumed by Indexer and NotebookLM cycle.
"""
import logging
import os
import subprocess
from datetime import datetime, timezone
from typing import Optional

from core.enricher import enrich_content

logger = logging.getLogger("erudito.scanner")

# v3 scans only .md files
SCAN_EXTENSIONS = {".md"}
EXCLUDED_FILES = {".env", "credentials.json", "auth-profiles.json", "join_token.txt"}
EXCLUDED_DIRS = {"node_modules", ".git", "__pycache__", ".pytest_cache", "venv", ".venv", "data", ".superpowers"}


def should_scan_file(filepath: str) -> bool:
    """Check if a file should be scanned based on extension and exclusion rules."""
    basename = os.path.basename(filepath)
    if basename in EXCLUDED_FILES:
        return False
    parts = filepath.replace("\\", "/").split("/")
    if any(d in EXCLUDED_DIRS for d in parts):
        return False
    _, ext = os.path.splitext(filepath)
    return ext in SCAN_EXTENSIONS


def find_git_root(path: str) -> str | None:
    """Find the git root directory for a given path."""
    try:
        result = subprocess.run(
            ["git", "-C", path, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _git_head(repo_path: str) -> str | None:
    """Get HEAD commit hash for a repo."""
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _git_date(repo_path: str) -> str:
    """Get the date of the last commit."""
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "log", "-1", "--format=%ci"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()[:10]  # YYYY-MM-DD
    except Exception:
        pass
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def compute_delta(
    project_name: str,
    repo_path: str,
    last_hash: str | None,
) -> dict | None:
    """Compute what .md files changed since last_hash.

    Returns a Delta dict or None if no changes.
    Does NOT sanitize — caller must sanitize before passing to consumers.
    """
    head = _git_head(repo_path)
    if not head:
        logger.warning(f"Cannot get HEAD for {project_name} at {repo_path}")
        return None

    if head == last_hash:
        return None  # No changes

    git_date = _git_date(repo_path)

    if last_hash:
        # Incremental: diff since last known commit
        try:
            result = subprocess.run(
                ["git", "-C", repo_path, "diff", "--name-status", f"{last_hash}..{head}", "--", "*.md"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                logger.warning(f"git diff failed for {project_name}: {result.stderr}")
                return None
        except Exception as e:
            logger.warning(f"git diff error for {project_name}: {e}")
            return None

        files = []
        for line in result.stdout.strip().splitlines():
            if not line:
                continue
            parts = line.split("\t", 1)
            if len(parts) != 2:
                continue
            status, filepath = parts
            if not should_scan_file(filepath):
                continue

            if status.startswith("D"):
                files.append({"path": filepath, "action": "deleted", "content": "", "auto_enriched": False})
            else:
                action = "added" if status.startswith("A") else "modified"
                full_path = os.path.join(repo_path, filepath)
                try:
                    with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                    enriched_content, was_enriched = enrich_content(content, filepath, project_name, git_date)
                    files.append({
                        "path": filepath,
                        "action": action,
                        "content": enriched_content,
                        "auto_enriched": was_enriched,
                    })
                except FileNotFoundError:
                    logger.warning(f"File not found (possibly deleted): {full_path}")
    else:
        # Full scan: index all .md files
        files = []
        for root, dirs, filenames in os.walk(repo_path):
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
            for fname in filenames:
                rel_path = os.path.relpath(os.path.join(root, fname), repo_path)
                if not should_scan_file(rel_path):
                    continue
                full_path = os.path.join(root, fname)
                try:
                    with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                    enriched_content, was_enriched = enrich_content(content, fname, project_name, git_date)
                    files.append({
                        "path": rel_path,
                        "action": "added",
                        "content": enriched_content,
                        "auto_enriched": was_enriched,
                    })
                except Exception as e:
                    logger.warning(f"Error reading {full_path}: {e}")

    if not files:
        return None

    return {
        "project": project_name,
        "old_hash": last_hash,
        "new_hash": head,
        "files": files,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/ai-lab/erudito && python -m pytest tests/test_scanner.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add core/scanner.py tests/test_scanner.py
git commit -m "feat(erudito-v3): add scanner module with git delta detection"
```

---

### Task 7: NotebookLM Integration Client

**Files:**
- Create: `integrations/notebooklm.py`
- Create: `tests/test_notebooklm.py`

MCP client for NotebookLM via LiteLLM HTTP. All calls are best-effort with graceful degradation.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_notebooklm.py
import pytest
from integrations.notebooklm import (
    build_mcp_request,
    parse_mcp_response,
    FIXED_QUESTIONS,
)


class TestMCPProtocol:
    def test_build_request(self):
        req = build_mcp_request("notebook_list", {"max_results": 10})
        assert req["jsonrpc"] == "2.0"
        assert req["method"] == "tools/call"
        assert req["params"]["name"] == "notebooklm_mcp-notebook_list"
        assert req["params"]["arguments"]["max_results"] == 10

    def test_parse_success_response(self):
        raw = {
            "result": {
                "content": [{"type": "text", "text": '{"notebooks": []}'}]
            }
        }
        parsed = parse_mcp_response(raw)
        assert parsed == {"notebooks": []}

    def test_parse_error_response(self):
        raw = {"error": {"message": "auth failed"}}
        parsed = parse_mcp_response(raw)
        assert parsed is None


class TestFixedQuestions:
    def test_has_five_questions(self):
        assert len(FIXED_QUESTIONS) == 5

    def test_questions_are_strings(self):
        for q in FIXED_QUESTIONS:
            assert isinstance(q, str)
            assert len(q) > 10
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/ai-lab/erudito && python -m pytest tests/test_notebooklm.py -v`
Expected: FAIL — ImportError

- [ ] **Step 3: Implement integrations/notebooklm.py**

```python
"""NotebookLM MCP client via LiteLLM HTTP proxy.

All calls are best-effort with graceful degradation.
Protocol: JSON-RPC 2.0 over HTTP (Server-Sent Events response).
"""
import json
import logging
import os
import asyncio
from typing import Optional

import httpx

logger = logging.getLogger("erudito.notebooklm")

LITELLM_URL = os.getenv("LITELLM_URL", "http://localhost:4000")
LITELLM_API_KEY = os.getenv("LITELLM_API_KEY", "")
MCP_ENDPOINT = f"{LITELLM_URL}/mcp/notebooklm_mcp"
NLM_TIMEOUT = 60.0  # seconds
NLM_SOURCE_LIMIT = 50

FIXED_QUESTIONS = [
    "What changes have occurred since the last session?",
    "Were there any design changes relative to the original plan?",
    "What new resources have been registered for this project?",
    "On which AI-Lab node is this project running?",
    "What is the current project status according to documentation?",
]

_REQUEST_ID = 0


def build_mcp_request(tool_name: str, arguments: dict) -> dict:
    """Build a JSON-RPC 2.0 MCP request."""
    global _REQUEST_ID
    _REQUEST_ID += 1
    return {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "id": _REQUEST_ID,
        "params": {
            "name": f"notebooklm_mcp-{tool_name}",
            "arguments": arguments,
        },
    }


def parse_mcp_response(raw: dict) -> dict | None:
    """Parse MCP response, handling nested JSON in content[0].text."""
    if "error" in raw:
        logger.warning(f"NLM MCP error: {raw['error']}")
        return None
    try:
        content = raw["result"]["content"]
        if content and content[0]["type"] == "text":
            return json.loads(content[0]["text"])
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        logger.warning(f"Failed to parse NLM response: {e}")
    return None


def _parse_sse_response(text: str) -> dict | None:
    """Parse Server-Sent Events response from LiteLLM MCP proxy."""
    for line in text.splitlines():
        if line.startswith("data: "):
            try:
                return json.loads(line[6:])
            except json.JSONDecodeError:
                continue
    # Try as plain JSON (non-SSE response)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


async def _call_mcp(tool_name: str, arguments: dict) -> dict | None:
    """Call a NotebookLM MCP tool via LiteLLM. Returns parsed result or None."""
    request = build_mcp_request(tool_name, arguments)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if LITELLM_API_KEY:
        headers["Authorization"] = f"Bearer {LITELLM_API_KEY}"

    try:
        async with httpx.AsyncClient(timeout=NLM_TIMEOUT) as client:
            resp = await client.post(MCP_ENDPOINT, json=request, headers=headers)
            resp.raise_for_status()
            raw = _parse_sse_response(resp.text)
            if raw:
                return parse_mcp_response(raw)
    except Exception as e:
        logger.warning(f"NLM MCP call failed ({tool_name}): {e}")
    return None


async def create_notebook(title: str) -> str | None:
    """Create a new notebook. Returns notebook_id or None."""
    result = await _call_mcp("notebook_create", {"title": title})
    if result and "notebook_id" in result:
        return result["notebook_id"]
    return None


async def add_source(notebook_id: str, content: str, title: str = "") -> bool:
    """Add a text source to a notebook. Returns True on success."""
    result = await _call_mcp("source_add", {
        "notebook_id": notebook_id,
        "source": content,
    })
    return result is not None


async def query_notebook(notebook_id: str, query: str) -> str | None:
    """Query a notebook. Returns answer text or None."""
    result = await _call_mcp("notebook_query", {
        "notebook_id": notebook_id,
        "query": query,
    })
    if result:
        # Response may be string or dict with 'answer' key
        if isinstance(result, str):
            return result
        return result.get("answer") or result.get("response") or str(result)
    return None


async def create_note(notebook_id: str, note_text: str) -> bool:
    """Create a note in a notebook. Returns True on success."""
    result = await _call_mcp("note", {
        "notebook_id": notebook_id,
        "note": note_text,
    })
    return result is not None


async def get_notebook_info(notebook_id: str) -> dict | None:
    """Get notebook details including source count."""
    return await _call_mcp("notebook_get", {"notebook_id": notebook_id})


async def run_nlm_cycle(
    notebook_id: str,
    delta: dict,
    dynamic_question_generator=None,
) -> dict:
    """Run the full NotebookLM validation cycle for a project.

    1. Check source limits
    2. Add sources
    3. Condense
    4. Ask questions (5 fixed + up to 5 dynamic)
    5. Create notes
    6. Return notes for Qdrant storage

    Returns: {success: bool, notes: list[dict], source_count: int}
    """
    result = {"success": False, "notes": [], "source_count": 0}

    # 1. Check source count
    info = await get_notebook_info(notebook_id)
    current_sources = 0
    if info:
        sources = info.get("sources", [])
        current_sources = len(sources) if isinstance(sources, list) else 0

    new_files = [f for f in delta["files"] if f["action"] != "deleted"]
    skip_source_add = False
    if current_sources + len(new_files) > NLM_SOURCE_LIMIT:
        logger.warning(
            f"Notebook {notebook_id} would exceed {NLM_SOURCE_LIMIT} sources "
            f"({current_sources} + {len(new_files)}). Skipping source_add, continuing with questions."
        )
        skip_source_add = True

    # 2. Add sources (skip if over limit)
    if not skip_source_add:
        for file_info in new_files:
            ok = await add_source(notebook_id, file_info["content"], file_info["path"])
            if ok:
                current_sources += 1
    result["source_count"] = current_sources

    # 3. Wait for processing
    await asyncio.sleep(20)

    # 4. Condense
    condensed = await query_notebook(notebook_id, "Condense all the information from the sources into a comprehensive summary.")
    if condensed:
        result["notes"].append({
            "type": "condensed",
            "question": "Condense all sources",
            "answer": condensed,
            "project": delta["project"],
        })

    # 5. Ask questions
    questions = list(FIXED_QUESTIONS)
    if dynamic_question_generator:
        dynamic = dynamic_question_generator(delta)
        questions.extend(dynamic[:5])

    for question in questions:
        answer = await query_notebook(notebook_id, question)
        if answer:
            result["notes"].append({
                "type": "qa",
                "question": question,
                "answer": answer,
                "project": delta["project"],
            })

    # 6. Create notes in notebook
    for note in result["notes"]:
        note_text = f"Q: {note['question']}\nA: {note['answer']}"
        saved = await create_note(notebook_id, note_text)
        if not saved:
            logger.warning(f"Failed to save note in notebook {notebook_id}")

    result["success"] = len(result["notes"]) > 0
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/ai-lab/erudito && python -m pytest tests/test_notebooklm.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add integrations/notebooklm.py tests/test_notebooklm.py
git commit -m "feat(erudito-v3): add NotebookLM MCP client with full cycle"
```

---

### Task 8: Query Engine (Confidence Routing)

**Files:**
- Create: `core/query.py`
- Create: `tests/test_query.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_query.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/ai-lab/erudito && python -m pytest tests/test_query.py -v`
Expected: FAIL — ImportError

- [ ] **Step 3: Implement core/query.py**

```python
"""Search + confidence routing with NotebookLM escalation.

Searches both agent_knowledge and nlm_notes collections.
Routes to NotebookLM when confidence is below threshold (0.75).
"""
import logging
import os
from typing import Optional

from core.indexer import embed_text, search, upsert_points, generate_point_id, COLLECTION_KNOWLEDGE, COLLECTION_NLM_NOTES

logger = logging.getLogger("erudito.query")

CONFIDENCE_THRESHOLD = float(os.getenv("ERUDITO_CONFIDENCE_THRESHOLD", "0.75"))
NLM_SCORE_BOOST = 0.05


def classify_confidence(score: float) -> str:
    """Classify score into confidence level."""
    if score >= CONFIDENCE_THRESHOLD:
        return "high"
    elif score >= 0.4:
        return "medium"
    return "low"


def build_response(
    query: str,
    sources: list[dict],
    nlm_answer: str | None = None,
    nlm_consulted: bool = False,
    project_identified: str | None = None,
) -> dict:
    """Build a QueryResponse dict from search results."""
    if not sources and not nlm_answer:
        return {
            "answer": "I don't have information on this topic. Would you like me to investigate?",
            "confidence": "low",
            "sources": [],
            "project_identified": project_identified,
            "nlm_consulted": nlm_consulted,
            "suggestion": "investigate_web" if project_identified else None,
        }

    best_score = sources[0]["score"] if sources else 0.0
    confidence = classify_confidence(best_score)

    if nlm_answer:
        answer = nlm_answer
        confidence = "medium" if confidence == "low" else confidence
    else:
        # Compose answer from top sources
        top_texts = [s["payload"].get("text", "") for s in sources[:3]]
        answer = "\n\n".join(top_texts)

    formatted_sources = []
    for s in sources[:5]:
        payload = s.get("payload", {})
        formatted_sources.append({
            "type": "nlm_note" if payload.get("from_nlm") else "qdrant",
            "project": payload.get("project", ""),
            "file": payload.get("source", ""),
            "score": round(s["score"], 4),
        })

    return {
        "answer": answer,
        "confidence": confidence,
        "sources": formatted_sources,
        "project_identified": project_identified,
        "nlm_consulted": nlm_consulted,
        "suggestion": None,
    }


async def execute_query(
    query: str,
    project: str | None = None,
    top_k: int = 5,
    nlm_client=None,
    registry=None,
) -> dict:
    """Execute a query with confidence routing.

    1. Search Qdrant (both collections)
    2. If high confidence → respond directly
    3. If low confidence + notebook available → consult NotebookLM
    4. If nothing → "I don't know"
    """
    query_embedding = embed_text(query)

    # Search both collections
    knowledge_results = search(query_embedding, COLLECTION_KNOWLEDGE, top_k, project)
    nlm_results = search(query_embedding, COLLECTION_NLM_NOTES, top_k, project)

    # Boost nlm_notes scores and mark them
    for r in nlm_results:
        r["score"] = min(r["score"] + NLM_SCORE_BOOST, 1.0)
        r["payload"]["from_nlm"] = True

    # Combine and sort
    all_results = knowledge_results + nlm_results
    all_results.sort(key=lambda x: x["score"], reverse=True)
    top_results = all_results[:top_k]

    best_score = top_results[0]["score"] if top_results else 0.0

    # High confidence → respond directly
    if best_score >= CONFIDENCE_THRESHOLD:
        return build_response(query, top_results, project_identified=project)

    # Low confidence → try NotebookLM if available
    notebook_id = None
    if project and registry:
        entry = registry.get(project)
        if entry:
            notebook_id = entry.get("notebook_id")

    if notebook_id and nlm_client:
        try:
            nlm_answer = await nlm_client.query_notebook(notebook_id, query)
            if nlm_answer:
                # Save to Qdrant for future queries
                note_embedding = embed_text(nlm_answer)
                point_id = generate_point_id(f"nlm_live:{project}:{query[:50]}", 0)
                upsert_points([{
                    "id": point_id,
                    "vector": note_embedding,
                    "payload": {
                        "text": nlm_answer,
                        "source": f"nlm_live:{query[:80]}",
                        "project": project,
                        "from_nlm": True,
                        "chunk_index": 0,
                    },
                }], COLLECTION_NLM_NOTES)

                return build_response(
                    query, top_results,
                    nlm_answer=nlm_answer,
                    nlm_consulted=True,
                    project_identified=project,
                )
        except Exception as e:
            logger.warning(f"NLM query failed: {e}")

    # Respond with what we have
    return build_response(query, top_results, nlm_consulted=False, project_identified=project)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/ai-lab/erudito && python -m pytest tests/test_query.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add core/query.py tests/test_query.py
git commit -m "feat(erudito-v3): add query engine with confidence routing"
```

---

### Task 9: Main Application (FastAPI + Endpoints + Lifespan)

**Files:**
- Create: `main.py` (overwrite v2)
- Create: `tests/test_api.py`

This wires everything together: endpoints, background scan loop, lifespan.

- [ ] **Step 1: Write the failing API tests**

```python
# tests/test_api.py
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
class TestMetricsEndpoint:
    def test_metrics_returns_coverage(self):
        import httpx
        resp = httpx.get("http://localhost:8095/metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert "coverage" in data
        assert "freshness_avg_minutes" in data


@pytest.mark.integration
class TestCoherenceEndpoint:
    def test_post_coherence_score(self):
        import httpx
        resp = httpx.post("http://localhost:8095/metrics/coherence", json={"score": 85.0, "timestamp": "2026-03-18T01:00:00Z"})
        assert resp.status_code == 200
        # Verify it shows up in /metrics
        metrics = httpx.get("http://localhost:8095/metrics").json()
        assert metrics["coherence_last_score"] == 85.0


@pytest.mark.integration
class TestSearchEndpoint:
    def test_search_requires_query(self):
        import httpx
        resp = httpx.get("http://localhost:8095/search")
        assert resp.status_code == 422  # Missing required param
```

- [ ] **Step 2: Backup v2 main.py**

```bash
cp ~/ai-lab/erudito/main.py ~/ai-lab/erudito/main_v2_backup.py
```

- [ ] **Step 3: Implement main.py**

```python
"""Erudito v3 — Knowledge Orchestration Agent with NotebookLM Integration.

FastAPI application with modular domain separation.
Background scan loop, confidence-based query routing, dual-write registry.
"""
import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import JSONResponse

from core.registry import Registry
from core.scanner import compute_delta
from core.indexer import (
    index_delta, ensure_collection, embed_text, search,
    COLLECTION_KNOWLEDGE, COLLECTION_NLM_NOTES,
)
from core.query import execute_query
from integrations.sanitizer import sanitize_text
from integrations import notebooklm as nlm

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("erudito")

# Configuration
DATA_DIR = os.getenv("DATA_DIR", "data")
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL_MINUTES", "15")) * 60
REGISTRY_YAML = os.path.join(DATA_DIR, "registry.yaml")
AUDIT_FILE = os.path.join(DATA_DIR, "audit.jsonl")
REDIS_URL = os.getenv("REDIS_URL", None)

# Global state
registry: Registry | None = None
_scan_task: asyncio.Task | None = None
_coherence_cache: dict = {"score": None, "timestamp": None}
_nlm_call_stats: dict = {"success": 0, "total": 0}
_query_stats: dict = {"high": 0, "total": 0}
_last_scan_time: str | None = None
_auto_enriched_files: int = 0
_total_scanned_files: int = 0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def audit_log(entry: dict):
    """Append to JSONL audit file."""
    Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
    entry["timestamp"] = _now_iso()
    with open(AUDIT_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


async def _scan_loop():
    """Background scan loop running every SCAN_INTERVAL seconds."""
    while True:
        try:
            await _run_scan_all()
        except Exception as e:
            logger.error(f"Scan loop error: {e}")
            audit_log({"action": "scan_error", "error": str(e)})
        await asyncio.sleep(SCAN_INTERVAL)


async def _run_scan_all():
    """Scan all projects for deltas and process them."""
    global _last_scan_time
    if not registry:
        return
    for project_name in registry.list_all():
        await _scan_project(project_name)
    _last_scan_time = _now_iso()


async def _scan_project(project_name: str):
    """Scan a single project for changes."""
    entry = registry.get(project_name)
    if not entry:
        return

    repo_path = os.path.expanduser(entry["path"])
    if not os.path.isdir(repo_path):
        logger.warning(f"Path not found for {project_name}: {repo_path}")
        return

    delta = compute_delta(project_name, repo_path, entry.get("last_hash"))
    if not delta:
        return  # No changes

    logger.info(f"Delta detected for {project_name}: {len(delta['files'])} files changed")
    audit_log({"action": "delta_detected", "project": project_name, "files": len(delta["files"])})

    # Sanitize all files in the delta
    for file_info in delta["files"]:
        if file_info["content"]:
            result = await sanitize_text(file_info["content"])
            file_info["content"] = result["sanitized"]
            if result["masked_count"] > 0:
                logger.info(f"Sanitized {result['masked_count']} secrets in {file_info['path']}")

    # Track auto-enrichment for metadata compliance metric
    global _auto_enriched_files, _total_scanned_files
    for f in delta["files"]:
        if f["action"] != "deleted":
            _total_scanned_files += 1
            if f.get("auto_enriched"):
                _auto_enriched_files += 1

    # Run Indexer and NLM cycle in parallel
    indexer_task = asyncio.create_task(_run_indexer(project_name, delta))
    nlm_task = asyncio.create_task(_run_nlm_cycle(project_name, delta))

    await asyncio.gather(indexer_task, nlm_task, return_exceptions=True)


async def _run_indexer(project_name: str, delta: dict):
    """Run the indexer pipeline."""
    try:
        count = index_delta(delta)
        doc_count = len([f for f in delta["files"] if f["action"] != "deleted"])
        registry.update_sync(project_name, delta["new_hash"], doc_count=doc_count)
        logger.info(f"Indexed {count} points for {project_name}")
        audit_log({"action": "index_complete", "project": project_name, "points": count})
    except Exception as e:
        logger.error(f"Indexer failed for {project_name}: {e}")
        audit_log({"action": "index_error", "project": project_name, "error": str(e)})


async def _run_nlm_cycle(project_name: str, delta: dict):
    """Run the NotebookLM validation cycle."""
    entry = registry.get(project_name)
    if not entry:
        return

    notebook_id = entry.get("notebook_id")

    # Create notebook if needed
    if not notebook_id:
        notebook_id = await nlm.create_notebook(f"AI-Lab: {project_name}")
        if notebook_id:
            # Only update notebook_id, don't touch hash/status (indexer handles that)
            entry = registry.get(project_name)
            if entry:
                registry._data["projects"][project_name]["notebook_id"] = notebook_id
                registry._persist()
            logger.info(f"Created notebook for {project_name}: {notebook_id}")
        else:
            _nlm_call_stats["total"] += 1
            logger.warning(f"Failed to create notebook for {project_name}")
            return

    _nlm_call_stats["total"] += 1
    result = await nlm.run_nlm_cycle(notebook_id, delta)

    if result["success"]:
        _nlm_call_stats["success"] += 1
        # Store notes in Qdrant nlm_notes collection
        for note in result["notes"]:
            embedding = embed_text(f"{note['question']} {note['answer']}")
            from core.indexer import upsert_points, generate_point_id
            point_id = generate_point_id(f"nlm:{project_name}:{note['question'][:50]}", 0)
            upsert_points([{
                "id": point_id,
                "vector": embedding,
                "payload": {
                    "text": note["answer"],
                    "question": note["question"],
                    "source": f"nlm:{project_name}",
                    "project": project_name,
                    "from_nlm": True,
                    "type": note["type"],
                    "chunk_index": 0,
                },
            }], COLLECTION_NLM_NOTES)

        # Update nlm_source_count and mark validated
        entry = registry.get(project_name)
        if entry:
            registry._data["projects"][project_name]["nlm_source_count"] = result["source_count"]
        registry.mark_validated(project_name, coverage=1.0)
        audit_log({"action": "nlm_validated", "project": project_name, "notes": len(result["notes"])})
    else:
        logger.warning(f"NLM cycle failed for {project_name}")
        audit_log({"action": "nlm_failed", "project": project_name})


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: init services and start scan loop."""
    global registry, _scan_task

    Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
    registry = Registry(yaml_path=REGISTRY_YAML, redis_url=REDIS_URL)

    # Ensure Qdrant collections exist
    try:
        ensure_collection(COLLECTION_KNOWLEDGE)
        ensure_collection(COLLECTION_NLM_NOTES)
    except Exception as e:
        logger.warning(f"Qdrant init warning: {e}")

    # Start background scan
    _scan_task = asyncio.create_task(_scan_loop())
    logger.info(f"Erudito v3 started. Scan interval: {SCAN_INTERVAL}s")

    yield

    # Cleanup
    if _scan_task:
        _scan_task.cancel()


app = FastAPI(title="Erudito v3", version="3.0.0", lifespan=lifespan)


# --- System Endpoints ---

@app.get("/health")
async def health():
    qdrant_ok = False
    try:
        from core.indexer import _qdrant_request
        r = _qdrant_request("/collections", method="GET")
        qdrant_ok = True
    except Exception:
        pass

    redis_ok = False
    if registry and registry._get_redis():
        try:
            registry._get_redis().ping()
            redis_ok = True
        except Exception:
            pass

    return {
        "status": "ok" if qdrant_ok else "degraded",
        "version": "3.0.0",
        "qdrant": "up" if qdrant_ok else "down",
        "redis": "up" if redis_ok else "down",
        "projects": len(registry.list_all()) if registry else 0,
    }


@app.get("/metrics")
async def metrics():
    if not registry:
        raise HTTPException(503, "Registry not initialized")
    summary = registry.summary()

    # Freshness calculation
    import statistics
    freshness_values = []
    for name in registry.list_all():
        entry = registry.get(name)
        if entry and entry.get("last_sync"):
            try:
                sync_time = datetime.fromisoformat(entry["last_sync"])
                age_min = (datetime.now(timezone.utc) - sync_time).total_seconds() / 60
                freshness_values.append(age_min)
            except Exception:
                pass

    # Metadata compliance (tracked at file level during scans)
    total_docs = _total_scanned_files
    auto_enriched_count = _auto_enriched_files

    nlm_health = round(
        _nlm_call_stats["success"] / _nlm_call_stats["total"] * 100, 1
    ) if _nlm_call_stats["total"] > 0 else 100.0

    query_hit = round(
        _query_stats["high"] / _query_stats["total"] * 100, 1
    ) if _query_stats["total"] > 0 else 0.0

    return {
        "coverage": summary,
        "freshness_avg_minutes": round(statistics.mean(freshness_values), 1) if freshness_values else 0,
        "metadata_compliance_pct": round(
            (1 - auto_enriched_count / total_docs) * 100, 1
        ) if total_docs > 0 else 100.0,
        "nlm_health_24h_pct": nlm_health,
        "query_hit_rate_pct": query_hit,
        "coherence_last_score": _coherence_cache.get("score"),
        "coherence_last_run": _coherence_cache.get("timestamp"),
        "last_scan": _last_scan_time,
    }


@app.post("/metrics/coherence")
async def update_coherence(data: dict):
    """Endpoint for eval-agent to report coherence score."""
    _coherence_cache["score"] = data.get("score")
    _coherence_cache["timestamp"] = data.get("timestamp", _now_iso())
    return {"status": "ok"}


@app.get("/audit")
async def audit(limit: int = Query(default=50, ge=1, le=500)):
    try:
        with open(AUDIT_FILE, "r") as f:
            lines = f.readlines()
        entries = [json.loads(line) for line in lines[-limit:]]
        entries.reverse()
        return {"entries": entries}
    except FileNotFoundError:
        return {"entries": []}


# --- Registry Endpoints ---

@app.get("/registry")
async def list_registry():
    if not registry:
        raise HTTPException(503, "Registry not initialized")
    projects = {}
    for name in registry.list_all():
        projects[name] = registry.get(name)
    return {"projects": projects}


@app.get("/registry/{project}")
async def get_registry_project(project: str):
    if not registry:
        raise HTTPException(503, "Registry not initialized")
    entry = registry.get(project)
    if not entry:
        raise HTTPException(404, f"Project '{project}' not found")
    return entry


@app.post("/registry")
async def register_project(data: dict):
    if not registry:
        raise HTTPException(503, "Registry not initialized")
    name = data.get("name")
    path = data.get("path")
    node = data.get("node", "hanzo")
    repo = data.get("repo", "")

    if not name or not path:
        raise HTTPException(400, "name and path are required")

    expanded = os.path.expanduser(path)
    if not os.path.isdir(expanded):
        raise HTTPException(400, f"Path does not exist: {path}")

    from core.scanner import find_git_root
    if not find_git_root(expanded):
        raise HTTPException(400, f"Path is not a git repository: {path}")

    try:
        registry.register(name, path, node, repo)
    except ValueError as e:
        raise HTTPException(400, str(e))

    return {"status": "registered", "project": name}


# --- Scan Endpoints ---

@app.post("/scan")
async def scan_all():
    asyncio.create_task(_run_scan_all())
    return {"status": "scan_started", "projects": registry.list_all() if registry else []}


@app.post("/scan/{project}")
async def scan_project(project: str):
    if not registry:
        raise HTTPException(503, "Registry not initialized")
    if not registry.get(project):
        raise HTTPException(404, f"Project '{project}' not found")
    asyncio.create_task(_scan_project(project))
    return {"status": "scan_started", "project": project}


# --- Search Endpoint ---

@app.get("/search")
async def search_endpoint(
    q: str = Query(..., min_length=1),
    project: str | None = Query(default=None),
    top_k: int = Query(default=5, ge=1, le=50),
):
    _query_stats["total"] += 1
    result = await execute_query(
        query=q,
        project=project,
        top_k=top_k,
        nlm_client=nlm,
        registry=registry,
    )
    if result["confidence"] == "high":
        _query_stats["high"] += 1
    return result
```

- [ ] **Step 4: Verify the app starts**

Run: `cd ~/ai-lab/erudito && python -c "from main import app; print('App loaded successfully')"`
Expected: "App loaded successfully"

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_api.py
git commit -m "feat(erudito-v3): add main application with all endpoints and scan loop"
```

---

### Task 10: Migration Script

**Files:**
- Create: `migrate_v2.py`

One-shot script to convert v2 state to v3 registry.

- [ ] **Step 1: Implement migrate_v2.py**

```python
"""One-shot migration: Erudito v2 scan_state.json → v3 registry.yaml.

Usage: python migrate_v2.py [--dry-run]

Reads data/scan_state.json, converts to registry.yaml format.
Shows preview diff and requires confirmation before writing.
"""
import json
import os
import sys
import yaml

# Alias map for ambiguous paths (basename collision or non-obvious names)
ALIAS_MAP = {
    "profiles": "jasper-profiles",
    "claude_contracts": "claude-contracts",
    "opencode_contracts": "opencode-contracts",
}

# Node mapping
NODE_MAP = {
    "ai-lab": "hanzo",
    "desarrollos_openclaw": "hanzo",
}

# Repo mapping (known repos)
REPO_MAP = {
    "infra-mcp": "r0calex/infra-mcp",
    "devops-agent": "r0calex/devops-agent",
    "mesh-monitor": "r0calex/mesh-monitor",
    "erudito": "r0calex/erudito",
    "event-bus": "r0calex/event-bus",
    "node-reporter": "r0calex/node-reporter",
    "qdrant-mcp": "r0calex/qdrant-mcp",
}


def derive_name(path: str) -> str:
    """Derive project name from path, using alias map for ambiguous cases."""
    basename = os.path.basename(path.rstrip("/"))
    return ALIAS_MAP.get(basename, basename)


def derive_node(path: str) -> str:
    """Derive node from path."""
    for key, node in NODE_MAP.items():
        if key in path:
            return node
    return "hanzo"


def migrate(dry_run: bool = False):
    state_file = "data/scan_state.json"
    output_file = "data/registry.yaml"

    if not os.path.exists(state_file):
        print(f"Error: {state_file} not found")
        sys.exit(1)

    with open(state_file, "r") as f:
        v2_state = json.load(f)

    registry_data = {"version": 1, "projects": {}}

    print("=== Migration Preview ===\n")
    for path, state in v2_state.items():
        name = derive_name(path)
        node = derive_node(path)
        repo = REPO_MAP.get(name, "")

        entry = {
            "path": path,
            "node": node,
            "repo": repo,
            "notebook_id": None,
            "last_hash": state.get("last_commit"),
            "last_sync": state.get("last_scan"),
            "status": "synced",
            "coverage": 0.0,
            "auto_enriched": False,
            "doc_count": 0,
            "last_nlm_session": None,
            "nlm_source_count": 0,
        }
        registry_data["projects"][name] = entry
        print(f"  {path}")
        print(f"    → name: {name}, node: {node}, repo: {repo}")
        print(f"    → last_hash: {state.get('last_commit', 'N/A')[:8]}...")
        print()

    print(f"Total projects: {len(registry_data['projects'])}")
    print(f"Output: {output_file}")
    print()

    if dry_run:
        print("--- Dry run, not writing ---")
        print(yaml.dump(registry_data, default_flow_style=False))
        return

    confirm = input("Write registry.yaml? [y/N] ")
    if confirm.lower() != "y":
        print("Aborted.")
        return

    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    with open(output_file, "w") as f:
        yaml.dump(registry_data, f, default_flow_style=False, sort_keys=False)
    print(f"Written to {output_file}")


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    migrate(dry_run=dry)
```

- [ ] **Step 2: Test with dry-run**

Run: `cd ~/ai-lab/erudito && python migrate_v2.py --dry-run`
Expected: Preview showing all 10 v2 sources mapped to project names

- [ ] **Step 3: Commit**

```bash
git add migrate_v2.py
git commit -m "feat(erudito-v3): add v2 to v3 migration script"
```

---

### Task 11: Docker Deployment

**Files:**
- Create: `Dockerfile`
- Create: `docker-compose.yaml`

- [ ] **Step 1: Write Dockerfile**

```dockerfile
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8095

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8095/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8095"]
```

- [ ] **Step 2: Write docker-compose.yaml**

```yaml
services:
  erudito:
    build: .
    container_name: erudito
    ports:
      - "100.119.223.20:8095:8095"
      - "127.0.0.1:8095:8095"
    environment:
      - QDRANT_URL=http://qdrant:6333
      - OLLAMA_URL=http://host.docker.internal:11434
      - LITELLM_URL=http://litellm-proxy-local:4000/v1
      - REDIS_URL=redis://litellm-redis-local:6379
      - SANITIZER_URL=http://sanitizer-hanzo:8086
      - ERUDITO_CONFIDENCE_THRESHOLD=0.75
      - SCAN_INTERVAL_MINUTES=15
      - DATA_DIR=/app/data
    volumes:
      - ./data:/app/data
      - ~/ai-lab:/mnt/ai-lab:ro
      - ~/desarrollos_openclaw:/mnt/dev:ro
    networks:
      - litellm-local_litellm-network
    restart: unless-stopped
    depends_on:
      - sanitizer-hanzo

  sanitizer-hanzo:
    image: kubo-sanitizer:local
    container_name: sanitizer-hanzo
    ports:
      - "100.119.223.20:8086:8086"
      - "127.0.0.1:8086:8086"
    networks:
      - litellm-local_litellm-network
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8086/health"]
      interval: 30s
      timeout: 10s
      retries: 3

networks:
  litellm-local_litellm-network:
    external: true
```

- [ ] **Step 3: Verify docker-compose config is valid**

Run: `cd ~/ai-lab/erudito && docker compose config --quiet`
Expected: No errors

- [ ] **Step 4: Commit**

```bash
git add Dockerfile docker-compose.yaml
git commit -m "feat(erudito-v3): add Docker deployment (Erudito + sanitizer-hanzo)"
```

---

### Task 12: Update eval-agent KPIs

**Files:**
- Modify: `~/ai-lab/agent-eval/kpis/erudito.yaml`

- [ ] **Step 1: Read current KPI file**

Read: `~/ai-lab/agent-eval/kpis/erudito.yaml`

- [ ] **Step 2: Update KPI definitions for v3**

Update the file to include new v3 endpoints and probes:
- Metric: `/health` returns v3 format with `qdrant`, `redis`, `projects` fields
- Metric: `/metrics` returns coverage object
- Metric: `/registry` returns projects list
- Probe: `/search?q=<question>` — compare answer against known ground truth

- [ ] **Step 3: Commit**

```bash
git add ~/ai-lab/agent-eval/kpis/erudito.yaml
git commit -m "feat(eval-agent): update Erudito KPIs for v3 endpoints"
```

---

### Task 13: Integration Test and Deploy

- [ ] **Step 1: Run migration (dry-run first)**

```bash
cd ~/ai-lab/erudito && python migrate_v2.py --dry-run
```

Verify output looks correct. Then run for real:

```bash
python migrate_v2.py
```

- [ ] **Step 2: Run all unit tests**

```bash
cd ~/ai-lab/erudito && python -m pytest tests/ -v -m "not network and not integration and not redis"
```

Expected: All PASS

- [ ] **Step 3: Start Erudito locally (non-Docker) for smoke test**

```bash
cd ~/ai-lab/erudito && uvicorn main:app --host 0.0.0.0 --port 8095
```

- [ ] **Step 4: Run smoke tests**

```bash
curl -s http://localhost:8095/health | python -m json.tool
curl -s http://localhost:8095/registry | python -m json.tool
curl -s http://localhost:8095/metrics | python -m json.tool
curl -s "http://localhost:8095/search?q=what+is+infra-mcp" | python -m json.tool
```

- [ ] **Step 5: Run integration tests**

```bash
cd ~/ai-lab/erudito && python -m pytest tests/test_api.py -v -m integration
```

- [ ] **Step 6: Docker build and deploy**

```bash
cd ~/ai-lab/erudito && docker compose build && docker compose up -d
```

- [ ] **Step 7: Verify Docker deployment**

```bash
docker ps | grep -E "erudito|sanitizer"
curl -s http://100.119.223.20:8095/health | python -m json.tool
curl -s http://100.119.223.20:8086/health | python -m json.tool
```

- [ ] **Step 8: Trigger first scan**

```bash
curl -s -X POST http://localhost:8095/scan | python -m json.tool
```

- [ ] **Step 9: Final commit**

```bash
git add -A
git commit -m "feat(erudito-v3): complete v3 deployment with all modules"
```
