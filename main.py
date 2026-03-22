"""Erudito v3 — Knowledge Orchestration Agent with NotebookLM Integration.

FastAPI application with modular domain separation.
Background scan loop, confidence-based query routing, dual-write registry.
"""
import asyncio
import json
import logging
import os
import yaml
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import JSONResponse

from core.curator import curate_project
from core.registry import Registry
from core.scanner import compute_delta
from core.indexer import (
    index_delta, ensure_collection, embed_text, search,
    upsert_points, generate_point_id,
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
CURATED_DIR = os.path.join(DATA_DIR, "curated")
MAX_CONCURRENT_NLM = int(os.getenv("MAX_CONCURRENT_NLM", "2"))
NLM_ENABLED = os.getenv("NLM_ENABLED", "false").lower() == "true"

# Global state
registry: Registry | None = None
_scan_task: asyncio.Task | None = None
_nlm_sync_task: asyncio.Task | None = None
_scan_semaphore: asyncio.Semaphore | None = None
_nlm_semaphore: asyncio.Semaphore | None = None
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

    # Run Curator (replaces direct indexing)
    try:
        curation_result = curate_project(
            project_name, repo_path, delta["files"], CURATED_DIR
        )
        if curation_result.success and curation_result.curated_files > 0:
            project = registry._data["projects"].get(project_name)
            if project:
                project["curation_status"] = "curated"
                project["curated_at"] = _now_iso()
                project["curated_files"] = curation_result.curated_files
                registry._persist()
            registry.update_sync(project_name, delta["new_hash"],
                                 doc_count=len([f for f in delta["files"] if f["action"] != "deleted"]))
            audit_log({
                "action": "curation_complete",
                "project": project_name,
                "features": curation_result.features,
                "curated_files": curation_result.curated_files,
            })
            logger.info(f"Curated {project_name}: {curation_result.curated_files} features")
        elif curation_result.errors:
            project = registry._data["projects"].get(project_name)
            if project:
                project["curation_status"] = "error"
                registry._persist()
            audit_log({
                "action": "curation_error",
                "project": project_name,
                "errors": curation_result.errors,
            })
    except Exception as e:
        logger.error(f"Curation failed for {project_name}: {e}")
        audit_log({"action": "curation_error", "project": project_name, "error": str(e)})

    # Cleanup curated files for features with all sources deleted
    deleted_files = [f["path"] for f in delta["files"] if f["action"] == "deleted"]
    if deleted_files:
        curated_project_dir = Path(CURATED_DIR) / project_name
        if curated_project_dir.is_dir():
            for curated_file in curated_project_dir.glob("*.md"):
                try:
                    content = curated_file.read_text(encoding="utf-8")
                    if content.startswith("---"):
                        fm_end = content.index("---", 3)
                        fm = yaml.safe_load(content[3:fm_end])
                        sources = fm.get("source_files", [])
                        if sources and all(s in deleted_files for s in sources):
                            curated_file.unlink()
                            audit_log({"action": "curation_cleanup", "project": project_name, "file": curated_file.name})
                            logger.info(f"Cleaned up curated file: {curated_file.name}")
                except Exception as e:
                    logger.warning(f"Cleanup check failed for {curated_file}: {e}")


async def _nlm_sync_loop():
    """Background NLM sync loop. Runs offset from scan loop."""
    await asyncio.sleep(300)  # 5-minute offset from scan loop
    while True:
        try:
            await _run_nlm_sync_all()
        except Exception as e:
            logger.error(f"NLM sync loop error: {e}")
            audit_log({"action": "nlm_sync_error", "error": str(e)})
        await asyncio.sleep(SCAN_INTERVAL)


async def _run_nlm_sync_all():
    """Sync all curated projects with NLM, max 2 concurrent."""
    global _nlm_semaphore
    if not registry or not NLM_ENABLED:
        return
    if _nlm_semaphore is None:
        _nlm_semaphore = asyncio.Semaphore(MAX_CONCURRENT_NLM)

    candidates = []
    for name in registry.list_all():
        entry = registry.get(name)
        if entry and entry.get("curation_status") == "curated":
            candidates.append(name)

    if not candidates:
        return

    logger.info(f"NLM sync: {len(candidates)} projects to process")

    async def _bounded_sync(name):
        async with _nlm_semaphore:
            await _nlm_sync_project(name)

    tasks = [_bounded_sync(name) for name in candidates]
    await asyncio.gather(*tasks, return_exceptions=True)


async def _nlm_sync_project(project_name: str):
    """Sync a single project with NLM using curated files."""
    entry = registry.get(project_name)
    if not entry:
        return

    notebook_id = entry.get("notebook_id")

    if not notebook_id:
        notebook_id = await nlm.ensure_notebook(f"AI-Lab: {project_name}")
        if notebook_id:
            registry._data["projects"][project_name]["notebook_id"] = notebook_id
            registry._persist()
            logger.info(f"Created notebook for {project_name}: {notebook_id}")
        else:
            project = registry._data["projects"].get(project_name)
            if project:
                project["nlm_consecutive_failures"] = project.get("nlm_consecutive_failures", 0) + 1
                registry._persist()
            _nlm_call_stats["total"] += 1
            logger.warning(f"Failed to create notebook for {project_name}")
            return

    curated_dir = os.path.join(CURATED_DIR, project_name)
    _nlm_call_stats["total"] += 1
    result = await nlm.run_nlm_cycle(notebook_id, curated_dir, project_name=project_name)

    if result["success"]:
        _nlm_call_stats["success"] += 1
        for note in result["notes"]:
            embedding = embed_text(f"{note['question']} {note['answer']}")
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

        project = registry._data["projects"].get(project_name)
        if project:
            project["nlm_source_count"] = result["sources_uploaded"]
            project["nlm_consecutive_failures"] = 0
            registry._persist()
        registry.mark_validated(project_name, coverage=1.0)
        audit_log({"action": "nlm_validated", "project": project_name, "notes": len(result["notes"])})
    else:
        project = registry._data["projects"].get(project_name)
        if project:
            failures = project.get("nlm_consecutive_failures", 0) + 1
            project["nlm_consecutive_failures"] = failures
            registry._persist()

            if failures >= 3:
                logger.warning(f"NLM failed {failures}x for {project_name}, falling back to direct indexing")
                await _fallback_index_curated(project_name)

        logger.warning(f"NLM cycle failed for {project_name}")
        audit_log({"action": "nlm_failed", "project": project_name})


async def _fallback_index_curated(project_name: str):
    """Fallback: index curated docs directly to agent_knowledge when NLM is unavailable."""
    curated_dir = os.path.join(CURATED_DIR, project_name)
    curated_path = Path(curated_dir)
    if not curated_path.is_dir():
        return

    delta_files = []
    for md_file in sorted(curated_path.glob("*.md")):
        delta_files.append({
            "path": str(md_file),
            "content": md_file.read_text(encoding="utf-8"),
            "action": "modified",
        })

    if delta_files:
        try:
            loop = asyncio.get_event_loop()
            delta = {
                "project": project_name,
                "old_hash": None,
                "new_hash": "fallback",
                "files": delta_files,
            }
            count = await loop.run_in_executor(None, index_delta, delta)
            logger.info(f"Fallback indexed {count} points for {project_name}")
            audit_log({"action": "fallback_index", "project": project_name, "points": count})
        except Exception as e:
            logger.error(f"Fallback indexing failed for {project_name}: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: init services and start background loops."""
    global registry, _scan_task, _nlm_sync_task, _scan_semaphore, _nlm_semaphore

    Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
    Path(CURATED_DIR).mkdir(parents=True, exist_ok=True)
    registry = Registry(yaml_path=REGISTRY_YAML, redis_url=REDIS_URL)

    # Ensure Qdrant collections exist
    try:
        ensure_collection(COLLECTION_KNOWLEDGE)
        ensure_collection(COLLECTION_NLM_NOTES)
    except Exception as e:
        logger.warning(f"Qdrant init warning: {e}")

    # Init concurrency control and start background loops
    _scan_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SCANS)
    _nlm_semaphore = asyncio.Semaphore(MAX_CONCURRENT_NLM)
    _scan_task = asyncio.create_task(_scan_loop())
    if NLM_ENABLED:
        _nlm_sync_task = asyncio.create_task(_nlm_sync_loop())
        logger.info(f"NLM sync enabled. Max concurrent: {MAX_CONCURRENT_NLM}")
    logger.info(f"Max concurrent scans: {MAX_CONCURRENT_SCANS}")
    logger.info(f"Erudito v3 started. Scan interval: {SCAN_INTERVAL}s")

    yield

    # Cleanup
    if _scan_task:
        _scan_task.cancel()
    if _nlm_sync_task:
        _nlm_sync_task.cancel()


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


# --- Classify Endpoint ---

@app.post("/classify")
async def classify_endpoint(dry_run: bool = Query(default=True)):
    """Classify inbox files and move them to their project directories.

    Reads from claude_contracts, classifies by keyword, moves to {project}/docs/contracts/.
    Default: dry_run=True (preview only). Set dry_run=false to actually move files.
    """
    if not registry:
        raise HTTPException(503, "Registry not initialized")

    from core.classifier import classify_inbox, move_classified

    inbox_path = "/home/r0calex/desarrollos_openclaw/claude_contracts"

    classification = classify_inbox(inbox_path)

    # Build project_paths from registry
    project_paths = {}
    for name in registry.list_all():
        entry = registry.get(name)
        if entry and entry.get("path"):
            project_paths[name] = entry["path"]

    # Move (or preview)
    result = move_classified(inbox_path, classification, project_paths, dry_run=dry_run)

    audit_log({
        "action": "classify_dry_run" if dry_run else "classify_move",
        "classified": {k: len(v) for k, v in result.classified.items()},
        "unclassified": len(result.unclassified),
        "moved": result.moved,
    })

    return {
        "dry_run": dry_run,
        "classified": {k: v for k, v in result.classified.items()},
        "unclassified": result.unclassified,
        "moved": result.moved,
        "errors": result.errors,
        "summary": {
            "total_files": sum(len(v) for v in result.classified.values()) + len(result.unclassified),
            "classified": sum(len(v) for v in result.classified.values()),
            "unclassified": len(result.unclassified),
        },
    }


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


# --- Curate Endpoint ---

@app.post("/curate/{project}")
async def curate_endpoint(project: str):
    """Full curation pipeline: scan → curate → NLM sync → Qdrant index.

    Runs synchronously. Returns detailed results of each step.
    """
    if not registry:
        raise HTTPException(503, "Registry not initialized")
    entry = registry.get(project)
    if not entry:
        raise HTTPException(404, f"Project '{project}' not found")

    result = {
        "project": project,
        "steps": {},
    }

    repo_path = os.path.expanduser(entry["path"])
    if not os.path.isdir(repo_path):
        raise HTTPException(400, f"Path not found: {repo_path}")

    # Step 1: Scan for delta
    delta = compute_delta(project, repo_path, entry.get("last_hash"))
    if delta:
        # Sanitize
        for file_info in delta["files"]:
            if file_info["content"]:
                san = await sanitize_text(file_info["content"])
                file_info["content"] = san["sanitized"]

        result["steps"]["scan"] = {"files": len(delta["files"]), "status": "delta_detected"}
    else:
        result["steps"]["scan"] = {"files": 0, "status": "no_changes"}

    # Step 2: Curate (always run even without delta — uses existing files)
    if delta:
        curation_result = curate_project(project, repo_path, delta["files"], CURATED_DIR)
        proj = registry._data["projects"].get(project)
        if proj and curation_result.success:
            proj["curation_status"] = "curated"
            proj["curated_at"] = _now_iso()
            proj["curated_files"] = curation_result.curated_files
            registry._persist()
            if delta:
                registry.update_sync(project, delta["new_hash"],
                                     doc_count=len([f for f in delta["files"] if f["action"] != "deleted"]))
        result["steps"]["curate"] = {
            "status": "ok" if curation_result.success else "error",
            "curated_files": curation_result.curated_files,
            "features": curation_result.features,
            "errors": curation_result.errors,
        }
        audit_log({"action": "curation_complete", "project": project,
                   "features": curation_result.features, "curated_files": curation_result.curated_files})
    else:
        # Check if curated files already exist
        curated_dir = Path(CURATED_DIR) / project
        existing = list(curated_dir.glob("*.md")) if curated_dir.is_dir() else []
        result["steps"]["curate"] = {
            "status": "skipped_no_delta",
            "existing_curated_files": len(existing),
        }

    # Step 3: NLM sync
    curated_dir = os.path.join(CURATED_DIR, project)
    curated_path = Path(curated_dir)
    if not curated_path.is_dir() or not list(curated_path.glob("*.md")):
        result["steps"]["nlm"] = {"status": "skipped_no_curated_files"}
        return result

    # Ensure notebook
    notebook_id = entry.get("notebook_id")
    if not notebook_id:
        notebook_id = await nlm.ensure_notebook(f"AI-Lab: {project}")
        if notebook_id:
            registry._data["projects"][project]["notebook_id"] = notebook_id
            registry._persist()

    if not notebook_id:
        result["steps"]["nlm"] = {"status": "error", "message": "Failed to create/find notebook"}
        return result

    # Run NLM cycle
    _nlm_call_stats["total"] += 1
    nlm_result = await nlm.run_nlm_cycle(notebook_id, curated_dir, project_name=project)

    if nlm_result["success"]:
        _nlm_call_stats["success"] += 1

        # Step 4: Index notes into Qdrant
        indexed = 0
        for note in nlm_result["notes"]:
            embedding = embed_text(f"{note['question']} {note['answer']}")
            point_id = generate_point_id(f"nlm:{project}:{note['question'][:50]}", 0)
            upsert_points([{
                "id": point_id,
                "vector": embedding,
                "payload": {
                    "text": note["answer"],
                    "question": note["question"],
                    "source": f"nlm:{project}",
                    "project": project,
                    "from_nlm": True,
                    "type": note["type"],
                    "chunk_index": 0,
                },
            }], COLLECTION_NLM_NOTES)
            indexed += 1

        # Update registry
        proj = registry._data["projects"].get(project)
        if proj:
            proj["nlm_source_count"] = nlm_result.get("sources_uploaded", 0)
            proj["nlm_consecutive_failures"] = 0
            proj["status"] = "validated"
            proj["coverage"] = 1.0
            proj["last_nlm_session"] = _now_iso()
            registry._persist()

        result["steps"]["nlm"] = {
            "status": "ok",
            "notebook_id": notebook_id,
            "sources_uploaded": nlm_result.get("sources_uploaded", 0),
            "sync_stats": nlm_result.get("sync_stats", {}),
            "notes_generated": len(nlm_result["notes"]),
        }
        result["steps"]["qdrant"] = {"status": "ok", "indexed": indexed}

        audit_log({"action": "nlm_validated", "project": project, "notes": len(nlm_result["notes"])})
    else:
        proj = registry._data["projects"].get(project)
        if proj:
            proj["nlm_consecutive_failures"] = proj.get("nlm_consecutive_failures", 0) + 1
            registry._persist()
        result["steps"]["nlm"] = {"status": "error", "message": "NLM cycle failed"}
        audit_log({"action": "nlm_failed", "project": project})

    result["status"] = "validated" if nlm_result["success"] else "partial"
    return result


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
