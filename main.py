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
MAX_CONCURRENT_SCANS = int(os.getenv("MAX_CONCURRENT_SCANS", "3"))

# Global state
registry: Registry | None = None
_scan_task: asyncio.Task | None = None
_scan_semaphore: asyncio.Semaphore | None = None
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
    """Scan all projects with max concurrency limit."""
    global _last_scan_time, _scan_semaphore
    if not registry:
        return
    if _scan_semaphore is None:
        _scan_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SCANS)

    async def _bounded_scan(name):
        async with _scan_semaphore:
            await _scan_project(name)

    tasks = [_bounded_scan(name) for name in registry.list_all()]
    await asyncio.gather(*tasks, return_exceptions=True)
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
    """Run the indexer pipeline in a thread pool to avoid blocking the event loop."""
    try:
        loop = asyncio.get_event_loop()
        count = await loop.run_in_executor(None, index_delta, delta)
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
    global registry, _scan_task, _scan_semaphore

    Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
    registry = Registry(yaml_path=REGISTRY_YAML, redis_url=REDIS_URL)

    # Ensure Qdrant collections exist
    try:
        ensure_collection(COLLECTION_KNOWLEDGE)
        ensure_collection(COLLECTION_NLM_NOTES)
    except Exception as e:
        logger.warning(f"Qdrant init warning: {e}")

    # Init concurrency control and start background scan
    _scan_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SCANS)
    _scan_task = asyncio.create_task(_scan_loop())
    logger.info(f"Max concurrent scans: {MAX_CONCURRENT_SCANS}")
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
