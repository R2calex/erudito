"""
Erudito v1.0 — Knowledge Orchestration Agent for the KUBO AI-Lab mesh.

Replaces index_docs.py with an intelligent curator that:
- Scans git repos for changes (incremental via git diff)
- Chunks and embeds documents
- Curates Qdrant agent_knowledge collection (insert/update/delete)
- Anti-RAG-poison filtering (regex-based)
- Audit logging

Run: uvicorn main:app --host 0.0.0.0 --port 8095
"""

import hashlib
import json
import os
import re
import subprocess
import urllib.request
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCAN_SOURCES = [
    {"path": os.path.expanduser("~/desarrollos_openclaw/claude_contracts"), "type": "docs", "node": "hanzo"},
    {"path": os.path.expanduser("~/desarrollos_openclaw/opencode_contracts"), "type": "docs", "node": "hanzo"},
    {"path": os.path.expanduser("~/ai-lab/infra-mcp"), "type": "code", "node": "hanzo"},
    {"path": os.path.expanduser("~/ai-lab/devops-agent"), "type": "code", "node": "hanzo"},
    {"path": os.path.expanduser("~/ai-lab/mesh-monitor"), "type": "code", "node": "hanzo"},
    {"path": os.path.expanduser("~/ai-lab/event-bus"), "type": "code", "node": "hanzo"},
    {"path": os.path.expanduser("~/ai-lab/node-reporter"), "type": "code", "node": "hanzo"},
    {"path": os.path.expanduser("~/ai-lab/qdrant-mcp"), "type": "code", "node": "hanzo"},
    {"path": os.path.expanduser("~/ai-lab/erudito"), "type": "code", "node": "hanzo"},
]

REMOTE_SOURCES = [
    {"path": "~/ai-lab/", "type": "code", "node": "kubo", "ssh": "r0calex@100.66.123.113"},
    {"path": "~/ai-lab/", "type": "code", "node": "sariatu", "ssh": "sariatu"},
]

SCAN_EXTENSIONS = {".md", ".py", ".yml", ".yaml", ".json", ".sh", ".toml", ".cfg"}
EXCLUDED_FILES = {".env", "credentials.json", "auth-profiles.json", "join_token.txt", "package-lock.json"}
EXCLUDED_DIRS = {"node_modules", ".git", "__pycache__", ".pytest_cache", "venv", ".venv", "data"}

POISON_PATTERNS = [
    re.compile(r"password\s*[:=]\s*['\"]?[^\s'\"]{8,}", re.IGNORECASE),
    re.compile(r"api[_-]?key\s*[:=]\s*['\"]?[a-zA-Z0-9_-]{20,}", re.IGNORECASE),
    re.compile(r"token\s*[:=]\s*['\"]?[a-zA-Z0-9._-]{20,}", re.IGNORECASE),
    re.compile(r"postgresql://[^:]+:[^@]+@"),
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),
    re.compile(r"Bearer\s+[a-zA-Z0-9._-]{20,}"),
    re.compile(r"BEGIN\s+(RSA|DSA|EC|OPENSSH)\s+PRIVATE\s+KEY"),
]

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text:latest")
COLLECTION = "agent_knowledge"
SIMILARITY_THRESHOLD = 0.92

CHUNK_SIZE = 800  # chars
CHUNK_OVERLAP = 150

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
STATE_FILE = os.path.join(DATA_DIR, "scan_state.json")
AUDIT_FILE = os.path.join(DATA_DIR, "audit.jsonl")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + "Z"


def _should_scan(filepath: str) -> bool:
    """Return True if the file should be scanned based on extension/exclusion rules."""
    basename = os.path.basename(filepath)
    if basename in EXCLUDED_FILES:
        return False
    _, ext = os.path.splitext(basename)
    if ext not in SCAN_EXTENSIONS:
        return False
    parts = filepath.replace("\\", "/").split("/")
    for part in parts:
        if part in EXCLUDED_DIRS:
            return False
    return True


def _has_poison(content: str) -> bool:
    """Check if content matches any anti-RAG-poison pattern."""
    for pat in POISON_PATTERNS:
        if pat.search(content):
            return True
    return False


def _generate_point_id(source: str, chunk_index: int) -> int:
    """Generate a deterministic 64-bit int ID from source + chunk_index."""
    h = hashlib.md5(f"{source}:{chunk_index}".encode()).hexdigest()
    return int(h[:16], 16)


# ---------------------------------------------------------------------------
# Scan state & audit
# ---------------------------------------------------------------------------


def load_scan_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_scan_state(state: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def audit_log(entry: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    entry["timestamp"] = _now_iso()
    with open(AUDIT_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# Git scanner
# ---------------------------------------------------------------------------


def _find_git_root(path: str) -> str | None:
    """Find the git root for a path (may be a parent directory)."""
    result = subprocess.run(
        ["git", "-C", path, "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def scan_repo(source: dict) -> tuple[list[str], list[str], str]:
    """Scan a git repo for changes since last scan.

    The scan path may be a subdirectory of a git repo. In that case,
    git commands run from the git root, and file paths are filtered
    to only include files under the scan path.

    Returns (changed_files, deleted_files, head_commit).
    Files are returned as paths relative to source["path"].
    """
    path = source["path"]
    git_root = _find_git_root(path)
    if not git_root:
        raise RuntimeError(f"Not a git repo: {path}")

    # If scan path is a subdir of git root, compute the prefix
    abs_path = os.path.abspath(path)
    abs_root = os.path.abspath(git_root)
    if abs_path == abs_root:
        subdir_prefix = ""
    else:
        subdir_prefix = os.path.relpath(abs_path, abs_root)
        if not subdir_prefix.endswith("/"):
            subdir_prefix += "/"

    state = load_scan_state()
    last_commit = state.get(path, {}).get("last_commit", "")

    if last_commit:
        # Check if last_commit still exists in the repo
        check = subprocess.run(
            ["git", "-C", git_root, "cat-file", "-t", last_commit],
            capture_output=True, text=True, timeout=5,
        )
        if check.returncode != 0:
            last_commit = ""

    if last_commit:
        cmd = ["git", "-C", git_root, "diff", "--name-only", last_commit, "HEAD"]
        if subdir_prefix:
            cmd.extend(["--", subdir_prefix])
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        changed_files = [f for f in result.stdout.strip().split("\n") if f]

        cmd_del = ["git", "-C", git_root, "diff", "--name-only", "--diff-filter=D", last_commit, "HEAD"]
        if subdir_prefix:
            cmd_del.extend(["--", subdir_prefix])
        result_deleted = subprocess.run(cmd_del, capture_output=True, text=True, timeout=10)
        deleted_files = [f for f in result_deleted.stdout.strip().split("\n") if f]
    else:
        cmd = ["git", "-C", git_root, "ls-files"]
        if subdir_prefix:
            cmd.append(subdir_prefix)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        changed_files = [f for f in result.stdout.strip().split("\n") if f]
        deleted_files = []

    # Strip subdir prefix so paths are relative to scan path
    if subdir_prefix:
        changed_files = [f[len(subdir_prefix):] for f in changed_files if f.startswith(subdir_prefix)]
        deleted_files = [f[len(subdir_prefix):] for f in deleted_files if f.startswith(subdir_prefix)]

    changed_files = [f for f in changed_files if _should_scan(f)]

    head = subprocess.run(
        ["git", "-C", git_root, "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=5,
    ).stdout.strip()

    return changed_files, deleted_files, head


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


def chunk_text(text: str, filename: str) -> list[dict]:
    """Split text into overlapping chunks with metadata."""
    chunks = []
    step = CHUNK_SIZE - CHUNK_OVERLAP
    if step <= 0:
        step = CHUNK_SIZE
    idx = 0
    for i in range(0, len(text), step):
        chunk = text[i : i + CHUNK_SIZE]
        if len(chunk.strip()) < 50:
            continue
        chunks.append({
            "text": chunk,
            "source": filename,
            "offset": i,
            "chunk_index": idx,
        })
        idx += 1
    return chunks


# ---------------------------------------------------------------------------
# Qdrant operations (raw REST API)
# ---------------------------------------------------------------------------


def _qdrant_request(path: str, data: dict | None = None, method: str | None = None, timeout: int = 15):
    """Send a request to Qdrant REST API."""
    url = f"{QDRANT_URL}{path}"
    if data is not None:
        payload = json.dumps(data).encode()
        req = urllib.request.Request(
            url, data=payload, method=method or "POST",
            headers={"Content-Type": "application/json"},
        )
    else:
        req = urllib.request.Request(url, method=method or "GET")
    resp = urllib.request.urlopen(req, timeout=timeout)
    return json.loads(resp.read())


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Get embeddings from Ollama. Process in batches to avoid timeouts."""
    BATCH_SIZE = 32
    all_embeddings = []
    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i : i + BATCH_SIZE]
        payload = json.dumps({"model": EMBED_MODEL, "input": batch}).encode()
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/embed",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=120)
        data = json.loads(resp.read())
        all_embeddings.extend(data["embeddings"])
    return all_embeddings


def upsert_points(points: list[dict]):
    """Upsert a batch of points to Qdrant."""
    if not points:
        return
    # Batch in groups of 100
    BATCH = 100
    for i in range(0, len(points), BATCH):
        batch = points[i : i + BATCH]
        _qdrant_request(
            f"/collections/{COLLECTION}/points",
            data={"points": batch},
            method="PUT",
            timeout=30,
        )


def delete_by_source(source_file: str) -> int:
    """Delete all chunks from a specific source file."""
    filter_payload = {
        "filter": {"must": [{"key": "source", "match": {"value": source_file}}]},
        "limit": 500,
        "with_payload": False,
    }
    try:
        result = _qdrant_request(
            f"/collections/{COLLECTION}/points/scroll",
            data=filter_payload,
            method="POST",
        )
    except Exception:
        return 0

    points = result.get("result", {}).get("points", [])
    if points:
        ids = [p["id"] for p in points]
        _qdrant_request(
            f"/collections/{COLLECTION}/points/delete",
            data={"points": ids},
            method="POST",
        )
    return len(points)


# ---------------------------------------------------------------------------
# Main scan orchestrator
# ---------------------------------------------------------------------------


async def run_scan(sources: list[dict] | None = None) -> dict:
    """Run a full scan cycle."""
    if sources is None:
        sources = SCAN_SOURCES

    state = load_scan_state()
    stats = {"scanned": 0, "inserted": 0, "updated": 0, "deleted": 0, "skipped": 0, "poisoned": 0, "errors": []}

    for source in sources:
        path = source["path"]
        if not os.path.isdir(path):
            stats["errors"].append(f"Directory not found: {path}")
            continue

        # Check if it's inside a git repo
        if not _find_git_root(path):
            stats["errors"].append(f"Not a git repo: {path}")
            continue

        try:
            changed, deleted, head = scan_repo(source)
        except Exception as e:
            stats["errors"].append(f"Scan error {path}: {e}")
            continue

        # Handle deleted files
        for f in deleted:
            full_source = os.path.join(path, f)
            count = delete_by_source(full_source)
            stats["deleted"] += count
            audit_log({"action": "delete", "source": full_source, "chunks_deleted": count})

        # Process changed files
        for f in changed:
            full_path = os.path.join(path, f)
            if not os.path.isfile(full_path):
                continue

            try:
                with open(full_path, encoding="utf-8", errors="ignore") as fh:
                    content = fh.read()
            except Exception:
                continue

            # Anti-poison check
            if _has_poison(content):
                stats["poisoned"] += 1
                audit_log({"action": "skip_poison", "source": full_path})
                continue

            # Chunk the file
            chunks = chunk_text(content, full_path)
            if not chunks:
                stats["skipped"] += 1
                continue

            # Embed all chunks in batch
            texts = [c["text"] for c in chunks]
            try:
                embeddings = embed_texts(texts)
            except Exception as e:
                stats["errors"].append(f"Embed error {full_path}: {e}")
                audit_log({"action": "embed_error", "source": full_path, "error": str(e)})
                continue

            # Delete old chunks for this file (clean slate)
            old_deleted = delete_by_source(full_path)
            if old_deleted > 0:
                stats["updated"] += 1
            else:
                stats["inserted"] += 1

            # Build points for batch upsert
            batch_points = []
            for chunk, emb in zip(chunks, embeddings):
                point_id = _generate_point_id(full_path, chunk["chunk_index"])
                payload = {
                    "source": full_path,
                    "node": source.get("node", "unknown"),
                    "type": source.get("type", "unknown"),
                    "chunk_index": chunk["chunk_index"],
                    "offset": chunk["offset"],
                    "text": chunk["text"],
                    "last_verified": _now_iso(),
                }
                batch_points.append({"id": point_id, "vector": emb, "payload": payload})

            try:
                upsert_points(batch_points)
            except Exception as e:
                stats["errors"].append(f"Upsert error {full_path}: {e}")
                audit_log({"action": "upsert_error", "source": full_path, "error": str(e)})
                continue

            stats["scanned"] += 1

        # Update scan state
        state[path] = {"last_commit": head, "last_scan": _now_iso()}

    save_scan_state(state)
    audit_log({"action": "scan_complete", "stats": {k: v for k, v in stats.items() if k != "errors"}})
    return stats


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Erudito",
    version="1.0.0",
    description="Knowledge Orchestration Agent — KUBO AI-Lab",
)


@app.get("/health")
async def health():
    """Health check."""
    # Quick check: can we reach Qdrant and Ollama?
    qdrant_ok = False
    ollama_ok = False
    try:
        req = urllib.request.Request(f"{QDRANT_URL}/collections/{COLLECTION}")
        resp = urllib.request.urlopen(req, timeout=3)
        info = json.loads(resp.read())
        qdrant_ok = info.get("result", {}).get("status") == "green"
        points_count = info.get("result", {}).get("points_count", 0)
    except Exception:
        points_count = -1

    try:
        req = urllib.request.Request(f"{OLLAMA_URL}/api/tags")
        resp = urllib.request.urlopen(req, timeout=3)
        models = json.loads(resp.read())
        ollama_ok = any(m["name"] == EMBED_MODEL for m in models.get("models", []))
    except Exception:
        pass

    return {
        "status": "ok" if (qdrant_ok and ollama_ok) else "degraded",
        "version": "1.0.0",
        "agent": "erudito",
        "qdrant": "ok" if qdrant_ok else "unreachable",
        "ollama": "ok" if ollama_ok else "unreachable",
        "collection": COLLECTION,
        "points_count": points_count,
        "embed_model": EMBED_MODEL,
    }


@app.post("/scan")
async def trigger_scan():
    """Trigger a full scan of all local sources."""
    stats = await run_scan()
    return {"status": "completed", "stats": stats}


@app.post("/scan/{node}")
async def trigger_scan_node(node: str):
    """Trigger a scan for a specific node."""
    sources = [s for s in SCAN_SOURCES if s.get("node") == node]
    if not sources:
        raise HTTPException(404, f"No sources for node: {node}")
    stats = await run_scan(sources)
    return {"status": "completed", "node": node, "stats": stats}


@app.get("/status")
async def status():
    """Get scan status and last scan times."""
    state = load_scan_state()
    sources_info = []
    for s in SCAN_SOURCES:
        p = s["path"]
        info = state.get(p, {})
        sources_info.append({
            "path": p,
            "node": s.get("node"),
            "type": s.get("type"),
            "last_commit": info.get("last_commit", "never"),
            "last_scan": info.get("last_scan", "never"),
            "exists": os.path.isdir(p),
        })
    return {
        "scan_sources": len(SCAN_SOURCES),
        "sources": sources_info,
        "collection": COLLECTION,
    }


@app.get("/audit")
async def audit(since: str | None = None, limit: int = 50):
    """Get recent audit log entries."""
    entries = []
    try:
        with open(AUDIT_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if since and entry.get("timestamp", "") < since:
                    continue
                entries.append(entry)
    except FileNotFoundError:
        pass
    return {"entries": entries[-limit:], "total": len(entries)}


@app.delete("/staleness/cleanup")
async def cleanup_stale(days: int = 30):
    """Delete chunks not verified in N days."""
    # Scroll all points, check last_verified, delete stale ones
    cutoff = datetime.now(timezone.utc)
    from datetime import timedelta
    cutoff = cutoff - timedelta(days=days)
    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%S") + "Z"

    deleted = 0
    offset = None
    while True:
        scroll_payload = {
            "limit": 100,
            "with_payload": ["last_verified"],
        }
        if offset is not None:
            scroll_payload["offset"] = offset

        try:
            result = _qdrant_request(
                f"/collections/{COLLECTION}/points/scroll",
                data=scroll_payload,
                method="POST",
            )
        except Exception:
            break

        points = result.get("result", {}).get("points", [])
        next_offset = result.get("result", {}).get("next_page_offset")

        stale_ids = []
        for p in points:
            lv = p.get("payload", {}).get("last_verified", "")
            if lv and lv < cutoff_str:
                stale_ids.append(p["id"])

        if stale_ids:
            try:
                _qdrant_request(
                    f"/collections/{COLLECTION}/points/delete",
                    data={"points": stale_ids},
                    method="POST",
                )
                deleted += len(stale_ids)
            except Exception:
                pass

        if next_offset is None or not points:
            break
        offset = next_offset

    audit_log({"action": "staleness_cleanup", "days": days, "deleted": deleted})
    return {"status": "completed", "deleted": deleted, "cutoff": cutoff_str}


# ---------------------------------------------------------------------------
# Product Catalog routes
# ---------------------------------------------------------------------------

from catalog import (
    list_projects, get_project, upsert_project,
    delete_project, search_projects, count_projects,
)


@app.get("/catalog")
async def catalog_list():
    """List all projects in the catalog."""
    projects = list_projects()
    return {"projects": projects, "count": len(projects)}


@app.get("/catalog/search")
async def catalog_search(q: str, top_k: int = 5):
    """Search projects by natural language query."""
    results = search_projects(q, top_k)
    return {"query": q, "results": results}


@app.get("/catalog/{name}")
async def catalog_get(name: str):
    """Get a project by name."""
    project = get_project(name)
    if not project:
        raise HTTPException(404, f"Project not found: {name}")
    return project


@app.post("/catalog")
async def catalog_upsert(entry: dict):
    """Add or update a project in the catalog."""
    if "name" not in entry:
        raise HTTPException(400, "Missing 'name' field")
    point_id = upsert_project(entry)
    return {"status": "ok", "name": entry["name"], "point_id": point_id}


@app.delete("/catalog/{name}")
async def catalog_delete(name: str):
    """Delete a project from the catalog."""
    delete_project(name)
    return {"status": "deleted", "name": name}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8095)
