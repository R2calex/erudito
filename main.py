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
    search_by_filter,
)
from core.distiller import compute_tier, build_llm_prompt, parse_llm_response, DISTILL_QUESTIONS
from core.distiller import DISTILL_LLM_MODEL as _LLM_MODEL, DISTILL_LLM_TIMEOUT, DISTILL_LLM_MAX_TOKENS, LITELLM_URL
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
MAX_CONCURRENT_DISTILL = int(os.getenv("MAX_CONCURRENT_DISTILL", "5"))
FEEDBACK_FILE = os.path.join(DATA_DIR, "feedback.jsonl")
FEEDBACK_THRESHOLD_PCT = int(os.getenv("FEEDBACK_THRESHOLD_PCT", "70"))
FEEDBACK_WINDOW_SIZE = int(os.getenv("FEEDBACK_WINDOW_SIZE", "20"))
INGESTED_DIR = os.path.join(DATA_DIR, "ingested")

# Node → DevOps Agent URL mapping for remote file discovery
NODE_DEVOPS_AGENTS = {
    "kubo": os.getenv("DEVOPS_AGENT_KUBO", "http://100.66.123.113:8091"),
    "sariatu": os.getenv("DEVOPS_AGENT_SARIATU", "http://100.76.110.104:8090"),
}
LOCAL_NODE = os.getenv("LOCAL_NODE", "hanzo")

# Global state
registry: Registry | None = None
_scan_task: asyncio.Task | None = None
_nlm_sync_task: asyncio.Task | None = None
_scan_semaphore: asyncio.Semaphore | None = None
_nlm_semaphore: asyncio.Semaphore | None = None
_distill_semaphore: asyncio.Semaphore | None = None
_coherence_cache: dict = {"score": None, "timestamp": None}
_nlm_call_stats: dict = {"success": 0, "total": 0}
_query_stats: dict = {"high": 0, "total": 0}
_last_scan_time: str | None = None
_auto_enriched_files: int = 0
_total_scanned_files: int = 0

# Error patterns that indicate NLM returned garbage instead of real answers
_NLM_ERROR_PATTERNS = ["RESOURCE_EXHAUSTED", "error code", "status': 'error", "Google rejected"]


def _is_nlm_error(answer: str) -> bool:
    """Check if an NLM answer is actually an error response, not real knowledge."""
    if not answer:
        return True
    answer_start = str(answer)[:200]
    return any(p in answer_start for p in _NLM_ERROR_PATTERNS)


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


async def _pull_remote_docs(project_name: str, entry: dict) -> bool:
    """Pull .md files from a remote node's devops-agent into data/ingested/{project}/."""
    node = entry.get("node", "")
    agent_url = NODE_DEVOPS_AGENTS.get(node)
    if not agent_url:
        logger.warning(f"No devops-agent URL for node '{node}' (project {project_name})")
        return False

    repo_path = entry.get("path", "")
    if not repo_path:
        return False

    try:
        import httpx
        async with httpx.AsyncClient(timeout=30) as client:
            # Step 1: List .md files on the remote node
            resp = await client.get(f"{agent_url}/files", params={"path": repo_path})
            resp.raise_for_status()
            file_list = resp.json().get("files", [])

            if not file_list:
                logger.info(f"No .md files found on {node} for {project_name}")
                return False

            # Step 2: Get content of all files
            file_paths = [f["path"] for f in file_list]
            resp2 = await client.post(f"{agent_url}/files/content", json={
                "path": repo_path,
                "files": file_paths,
            })
            resp2.raise_for_status()
            remote_files = resp2.json().get("files", [])

            # Step 3: Store in ingested dir
            ingest_dir = Path(INGESTED_DIR) / project_name
            ingest_dir.mkdir(parents=True, exist_ok=True)

            stored = 0
            for f in remote_files:
                safe_name = os.path.basename(f["path"])
                (ingest_dir / safe_name).write_text(f["content"], encoding="utf-8")
                stored += 1

            logger.info(f"Pulled {stored} files from {node}:{repo_path} for {project_name}")
            audit_log({"action": "remote_pull", "project": project_name, "node": node, "files": stored})
            return stored > 0

    except Exception as e:
        logger.warning(f"Remote pull failed for {project_name} from {node}: {e}")
        return False


async def _scan_project(project_name: str):
    """Scan a single project for changes. Handles both local and remote projects."""
    entry = registry.get(project_name)
    if not entry:
        return

    node = entry.get("node", LOCAL_NODE)
    is_remote = node != LOCAL_NODE and node in NODE_DEVOPS_AGENTS

    if is_remote:
        # Remote project: pull docs via devops-agent, then curate from ingested dir
        pulled = await _pull_remote_docs(project_name, entry)
        if not pulled:
            return

        ingest_dir = Path(INGESTED_DIR) / project_name
        delta_files = []
        for md_file in sorted(ingest_dir.glob("*.md")):
            delta_files.append({
                "path": md_file.name,
                "content": md_file.read_text(encoding="utf-8"),
                "action": "added",
            })

        if not delta_files:
            return

        logger.info(f"Remote delta for {project_name}: {len(delta_files)} files from {node}")
        audit_log({"action": "delta_detected", "project": project_name,
                   "files": len(delta_files), "source": f"remote:{node}"})

        # Sanitize
        for file_info in delta_files:
            if file_info["content"]:
                result = await sanitize_text(file_info["content"])
                file_info["content"] = result["sanitized"]

        # Curate
        try:
            curation_result = curate_project(project_name, str(ingest_dir), delta_files, CURATED_DIR)
            if curation_result.success and curation_result.curated_files > 0:
                proj = registry._data["projects"].get(project_name)
                if proj:
                    proj["curation_status"] = "curated"
                    proj["curated_at"] = _now_iso()
                    proj["curated_files"] = curation_result.curated_files
                    proj["doc_count"] = len(delta_files)
                    registry._persist()
                audit_log({"action": "curation_complete", "project": project_name,
                           "features": curation_result.features, "source": f"remote:{node}"})
                logger.info(f"Curated {project_name} (remote/{node}): {curation_result.curated_files} features")
        except Exception as e:
            logger.error(f"Curation failed for remote {project_name}: {e}")
        return

    # Local project: standard scan
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
            # Guardrail: never index NLM error responses as knowledge
            if _is_nlm_error(note.get("answer", "")):
                logger.warning(f"Skipping contaminated NLM response for {project_name}: {note.get('question', '')[:50]}")
                continue
            embedding = embed_text(f"{note['question']} {note['answer'][:200]}")
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


# --- Tier-based Distillation ---

def _fetch_existing_notes(project_name: str) -> list[dict]:
    """Fetch existing distilled notes from Qdrant nlm_notes collection."""
    try:
        results = search_by_filter(
            COLLECTION_NLM_NOTES,
            {"project": project_name, "canonical": True},
            limit=10,
        )
        return [
            {"question": r["payload"]["question"], "answer": r["payload"]["text"]}
            for r in results if "question" in r.get("payload", {})
        ]
    except Exception as e:
        logger.warning(f"Failed to fetch existing notes for {project_name}: {e}")
        return []


async def _distill_project(project_name: str, force_nlm: bool = False):
    """Dispatch distillation based on project tier."""
    entry = registry.get(project_name)
    if not entry:
        return
    curated_dir = os.path.join(CURATED_DIR, project_name)
    tier = entry.get("tier") or entry.get("computed_tier", 3)

    if force_nlm:
        if not NLM_ENABLED:
            raise ValueError("NLM is disabled (NLM_ENABLED=false)")
        if nlm.is_circuit_open():
            raise ValueError("NLM circuit breaker is tripped")
        try:
            await _distill_nlm(project_name, curated_dir)
        except Exception as e:
            logger.error(f"force_nlm failed for {project_name}: {e}")
            raise
        return

    if tier == 1:
        if not entry.get("nlm_baseline"):
            if NLM_ENABLED and not nlm.is_circuit_open():
                try:
                    await _distill_nlm(project_name, curated_dir)
                except Exception as e:
                    logger.warning(f"NLM unavailable for {project_name} baseline, falling back to LLM: {e}")
                    await _distill_llm(project_name, curated_dir, nlm_notes=None)
            else:
                logger.info(f"NLM not available for {project_name} baseline, using LLM")
                await _distill_llm(project_name, curated_dir, nlm_notes=None)
        else:
            existing_notes = _fetch_existing_notes(project_name)
            await _distill_llm(project_name, curated_dir, nlm_notes=existing_notes)
    elif tier == 2:
        await _distill_llm(project_name, curated_dir, nlm_notes=None)
    else:
        await _distill_direct(project_name, curated_dir)


async def _distill_nlm(project_name: str, curated_dir: str):
    """Distill via NotebookLM (Tier 1 initial or force_nlm)."""
    entry = registry.get(project_name)
    notebook_id = entry.get("notebook_id")

    if not notebook_id:
        notebook_id = await nlm.ensure_notebook(f"AI-Lab: {project_name}")
        if notebook_id:
            registry.update_fields(project_name, notebook_id=notebook_id)
            logger.info(f"Created notebook for {project_name}: {notebook_id}")
        else:
            registry.update_fields(
                project_name,
                nlm_consecutive_failures=entry.get("nlm_consecutive_failures", 0) + 1,
            )
            raise Exception(f"Failed to create notebook for {project_name}")

    _nlm_call_stats["total"] += 1
    result = await nlm.run_nlm_cycle(notebook_id, curated_dir, project_name=project_name)

    if result["success"]:
        _nlm_call_stats["success"] += 1
        indexed = 0
        for i, note in enumerate(result["notes"]):
            if _is_nlm_error(note.get("answer", "")):
                logger.warning(f"Skipping contaminated NLM response for {project_name}")
                continue
            embedding = embed_text(f"{note['question']} {note['answer'][:200]}")
            point_id = generate_point_id(f"nlm:{project_name}:{note['question'][:50]}", 0)
            is_canonical = note.get("question", "") in DISTILL_QUESTIONS
            upsert_points([{
                "id": point_id,
                "vector": embedding,
                "payload": {
                    "text": note["answer"],
                    "question": note["question"],
                    "source": "nlm",
                    "distill_source": "nlm",
                    "project": project_name,
                    "from_nlm": True,
                    "type": "qa",
                    "model": "notebooklm",
                    "canonical": is_canonical,
                    "chunk_index": i,
                },
            }], collection=COLLECTION_NLM_NOTES)
            indexed += 1

        registry.update_fields(
            project_name,
            nlm_baseline=True,
            last_distill=_now_iso(),
            last_nlm_distill=_now_iso(),
            nlm_consecutive_failures=0,
            status="synced",
        )
        audit_log({"action": "distill_nlm", "project": project_name, "notes": indexed})
    else:
        registry.update_fields(
            project_name,
            nlm_consecutive_failures=entry.get("nlm_consecutive_failures", 0) + 1,
        )
        raise Exception(f"NLM cycle failed for {project_name}")


async def _distill_llm(project_name: str, curated_dir: str, nlm_notes: list[dict] | None = None):
    """Distill via LLM (Tier 2, or Tier 1 with baseline)."""
    import litellm

    curated_path = Path(curated_dir)
    if not curated_path.exists():
        logger.warning(f"No curated dir for {project_name}: {curated_dir}")
        return

    curated_files = []
    for md_file in sorted(curated_path.glob("*.md")):
        curated_files.append({
            "name": md_file.name,
            "content": md_file.read_text(encoding="utf-8"),
        })

    if not curated_files:
        logger.warning(f"No curated files for {project_name}")
        return

    prompt = build_llm_prompt(project_name, curated_files, nlm_notes)

    try:
        response = await litellm.acompletion(
            model=_LLM_MODEL,
            messages=[
                {"role": "system", "content": prompt["system"]},
                {"role": "user", "content": prompt["user"]},
            ],
            api_base=LITELLM_URL,
            timeout=DISTILL_LLM_TIMEOUT,
            max_tokens=DISTILL_LLM_MAX_TOKENS,
        )
        raw_answer = response.choices[0].message.content
    except Exception as e:
        logger.error(f"LLM distill failed for {project_name}: {e}")
        audit_log({"action": "distill_llm_error", "project": project_name, "error": str(e)})
        return

    notes = parse_llm_response(raw_answer, project_name)
    if not notes:
        logger.warning(f"LLM response unparseable for {project_name}")
        audit_log({"action": "distill_llm_parse_error", "project": project_name})
        return

    for note in notes:
        embedding = embed_text(f"{note['question']} {note['text'][:200]}")
        point_id = generate_point_id(f"llm:{project_name}:{note['question'][:50]}", 0)
        upsert_points([{
            "id": point_id,
            "vector": embedding,
            "payload": note,
        }], collection=COLLECTION_NLM_NOTES)

    registry.update_fields(project_name, last_distill=_now_iso(), status="synced")
    audit_log({"action": "distill_llm", "project": project_name, "notes": len(notes),
               "model": _LLM_MODEL, "had_nlm_context": nlm_notes is not None})


async def _distill_direct(project_name: str, curated_dir: str):
    """Distill by directly embedding curated docs (Tier 3)."""
    curated_path = Path(curated_dir)
    if not curated_path.exists():
        logger.warning(f"No curated dir for {project_name}: {curated_dir}")
        return

    indexed = 0
    for i, md_file in enumerate(sorted(curated_path.glob("*.md"))):
        content = md_file.read_text(encoding="utf-8")
        feature_name = md_file.stem
        embedding = embed_text(content[:2000])
        point_id = generate_point_id(f"direct:{project_name}:{feature_name}", 0)
        upsert_points([{
            "id": point_id,
            "vector": embedding,
            "payload": {
                "text": content,
                "question": f"Project documentation: {feature_name}",
                "source": "direct",
                "distill_source": "direct",
                "project": project_name,
                "type": "curated_doc",
                "model": None,
                "canonical": True,
                "chunk_index": i,
            },
        }], collection=COLLECTION_NLM_NOTES)
        indexed += 1

    registry.update_fields(project_name, last_distill=_now_iso(), status="synced")
    audit_log({"action": "distill_direct", "project": project_name, "docs": indexed})


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

    nlm_circuit = nlm.circuit_status()

    return {
        "status": "ok" if qdrant_ok else "degraded",
        "version": "3.0.0",
        "qdrant": "up" if qdrant_ok else "down",
        "redis": "up" if redis_ok else "down",
        "projects": len(registry.list_all()) if registry else 0,
        "nlm_circuit": "open (rate limited)" if nlm_circuit["tripped"] else "closed (ok)",
    }


@app.post("/nlm/reset")
async def reset_nlm_circuit():
    """Manually reset the NLM circuit breaker after rate limit recovery."""
    nlm.reset_circuit()
    return {"status": "ok", "nlm_circuit": "closed"}


@app.get("/nlm/status")
async def nlm_status():
    """Get NLM circuit breaker status."""
    status = nlm.circuit_status()
    return {
        "tripped": status["tripped"],
        "tripped_at": str(status["tripped_at"]) if status["tripped_at"] else None,
        "reason": status["reason"],
        "cooldown_hours": nlm.NLM_COOLDOWN_HOURS,
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


# --- Ingest Endpoint (for remote nodes) ---


@app.post("/ingest/{project}")
async def ingest_endpoint(project: str, data: dict):
    """Ingest documentation files from remote nodes.

    Accepts: {"files": [{"path": "filename.md", "content": "...", "node": "kubo"}]}
    Stores files in data/ingested/{project}/ for the curator to process.
    Optionally triggers curation if auto_curate=true.
    """
    if not registry:
        raise HTTPException(503, "Registry not initialized")

    files = data.get("files", [])
    if not files:
        raise HTTPException(400, "No files provided")

    node = data.get("node", "unknown")
    auto_curate = data.get("auto_curate", False)

    # Store ingested files
    ingest_dir = Path(INGESTED_DIR) / project
    ingest_dir.mkdir(parents=True, exist_ok=True)

    stored = 0
    for f in files:
        filename = f.get("path") or f.get("filename")
        content = f.get("content", "")
        if not filename or not content:
            continue
        # Use basename only to avoid path traversal
        safe_name = os.path.basename(filename)
        (ingest_dir / safe_name).write_text(content, encoding="utf-8")
        stored += 1

    audit_log({
        "action": "ingest",
        "project": project,
        "node": node,
        "files": stored,
    })
    logger.info(f"Ingested {stored} files for {project} from {node}")

    # Register project if not exists
    entry = registry.get(project)
    if not entry:
        # Register with ingested path as the project path
        registry._data["projects"][project] = {
            **dict(__import__('core.registry', fromlist=['_DEFAULT_ENTRY'])._DEFAULT_ENTRY),
            "path": str(ingest_dir),
            "node": node,
        }
        registry._persist()
        logger.info(f"Auto-registered project '{project}' from node {node}")

    # Curate the ingested files directly (they're already .md, no need for scanner)
    if auto_curate and stored > 0:
        delta_files = []
        for md_file in sorted(ingest_dir.glob("*.md")):
            delta_files.append({
                "path": md_file.name,
                "content": md_file.read_text(encoding="utf-8"),
                "action": "added",
            })

        curation_result = curate_project(project, str(ingest_dir), delta_files, CURATED_DIR)
        if curation_result.success:
            proj = registry._data["projects"].get(project)
            if proj:
                proj["curation_status"] = "curated"
                proj["curated_at"] = _now_iso()
                proj["curated_files"] = curation_result.curated_files
                proj["doc_count"] = stored
                registry._persist()

        return {
            "status": "ingested_and_curated",
            "project": project,
            "node": node,
            "files_stored": stored,
            "curated_files": curation_result.curated_files,
            "features": curation_result.features,
        }

    return {
        "status": "ingested",
        "project": project,
        "node": node,
        "files_stored": stored,
        "message": f"Use POST /curate/{project} to process",
    }


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

        # Step 4: Index notes into Qdrant (skip error responses)
        indexed = 0
        for note in nlm_result["notes"]:
            if _is_nlm_error(note.get("answer", "")):
                logger.warning(f"Skipping contaminated NLM response for {project}: {note.get('question', '')[:50]}")
                continue
            embedding = embed_text(f"{note['question']} {note['answer'][:200]}")
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
    mode: str = Query(default="default", regex="^(default|dual)$"),
):
    _query_stats["total"] += 1
    result = await execute_query(
        query=q,
        project=project,
        top_k=top_k,
        nlm_client=nlm,
        registry=registry,
        mode=mode,
    )
    if result.get("confidence") == "high":
        _query_stats["high"] += 1
    return result
