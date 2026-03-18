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
        if len(chunk) >= 50 or start == 0:
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
