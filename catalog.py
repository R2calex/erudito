"""Product Catalog for KUBO AI-Lab — maintains an index of all projects and services.

Stores structured project metadata in a Qdrant collection `project_catalog`
with semantic search via Ollama embeddings.

Zero external dependencies (stdlib only + Qdrant/Ollama HTTP APIs).
"""

import json
import hashlib
import os
import urllib.request
from datetime import datetime, timezone

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text:latest")
CATALOG_COLLECTION = "project_catalog"
VECTOR_SIZE = 768


def ensure_collection():
    """Create project_catalog collection if it doesn't exist."""
    try:
        req = urllib.request.Request(f"{QDRANT_URL}/collections/{CATALOG_COLLECTION}")
        urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # Create collection
            data = json.dumps({
                "vectors": {"size": VECTOR_SIZE, "distance": "Cosine"}
            }).encode()
            req = urllib.request.Request(
                f"{QDRANT_URL}/collections/{CATALOG_COLLECTION}",
                data=data, method="PUT",
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=10)
        else:
            raise


def _embed(text):
    """Get embedding for a single text."""
    payload = json.dumps({"model": EMBED_MODEL, "input": [text]}).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/embed",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    resp = urllib.request.urlopen(req, timeout=30)
    data = json.loads(resp.read())
    return data["embeddings"][0]


def _project_id(name):
    """Generate deterministic 64-bit int ID from project name."""
    return int(hashlib.md5(name.encode()).hexdigest()[:16], 16)


def upsert_project(entry):
    """Add or update a project in the catalog.

    Args:
        entry: dict with at least 'name' key. Optional keys:
            description, type, stack, status, node_primary,
            port, github, tags, version.

    Returns:
        int: the deterministic point ID for this project.
    """
    ensure_collection()
    name = entry["name"]

    # Create searchable text for embedding
    search_text = (
        f"{name}: {entry.get('description', '')}. "
        f"Stack: {', '.join(entry.get('stack', []))}. "
        f"Tags: {', '.join(entry.get('tags', []))}"
    )
    embedding = _embed(search_text)

    payload = {
        **entry,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    point_id = _project_id(name)
    data = json.dumps({
        "points": [{"id": point_id, "vector": embedding, "payload": payload}]
    }).encode()
    req = urllib.request.Request(
        f"{QDRANT_URL}/collections/{CATALOG_COLLECTION}/points",
        data=data, method="PUT",
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=10)
    return point_id


def get_project(name):
    """Get a single project by name.

    Returns:
        dict or None: the project payload if found.
    """
    ensure_collection()
    point_id = _project_id(name)
    try:
        req = urllib.request.Request(
            f"{QDRANT_URL}/collections/{CATALOG_COLLECTION}/points/{point_id}"
        )
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read())
        return data.get("result", {}).get("payload")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def list_projects():
    """List all projects in the catalog.

    Returns:
        list[dict]: list of project payloads.
    """
    ensure_collection()
    data = json.dumps({"limit": 100, "with_payload": True}).encode()
    req = urllib.request.Request(
        f"{QDRANT_URL}/collections/{CATALOG_COLLECTION}/points/scroll",
        data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    resp = urllib.request.urlopen(req, timeout=10)
    result = json.loads(resp.read())
    return [p["payload"] for p in result["result"]["points"]]


def search_projects(query, top_k=5):
    """Search projects by natural language query.

    Args:
        query: natural language search string.
        top_k: max results to return.

    Returns:
        list[dict]: results with 'score' and project payload fields.
    """
    ensure_collection()
    embedding = _embed(query)
    data = json.dumps({
        "vector": embedding,
        "top": top_k,
        "with_payload": True,
    }).encode()
    req = urllib.request.Request(
        f"{QDRANT_URL}/collections/{CATALOG_COLLECTION}/points/search",
        data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    resp = urllib.request.urlopen(req, timeout=10)
    results = json.loads(resp.read())["result"]
    return [{"score": r["score"], **r["payload"]} for r in results]


def delete_project(name):
    """Remove a project from the catalog.

    Args:
        name: project name to delete.
    """
    point_id = _project_id(name)
    data = json.dumps({"points": [point_id]}).encode()
    req = urllib.request.Request(
        f"{QDRANT_URL}/collections/{CATALOG_COLLECTION}/points/delete",
        data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=10)


def count_projects():
    """Return the number of projects in the catalog."""
    ensure_collection()
    try:
        req = urllib.request.Request(
            f"{QDRANT_URL}/collections/{CATALOG_COLLECTION}"
        )
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read())
        return data.get("result", {}).get("points_count", 0)
    except Exception:
        return 0
