# SPEC: Erudito v2 — Intelligent RAG Pipeline

**Project:** Erudito (AI-Lab Knowledge Orchestration)
**Status:** Ready for implementation
**Author:** Claude + R0calex
**Date:** 2026-03-16
**Codebase:** `~/ai-lab/erudito/`
**Service:** localhost:8095, systemd timer `erudito-scan.timer`

---

## 1. Problem Statement

Erudito claims to be an "LLM-curated RAG pipeline" but the audit (2026-03-16) reveals it's a naive chunker — it splits text, embeds it, and stores it in Qdrant without understanding, deduplicating, or quality-filtering anything. The analyzer module (`analyzer.py`, 263 LOC) exists with full LLM extraction capabilities but is **dead code** — never called during scans. The `SIMILARITY_THRESHOLD = 0.92` constant is defined but never used.

**Impact:** Jimbo and Jasper query Erudito for knowledge but get raw, duplicated, unranked text chunks. A search for "claude code configuration" returns overlapping fragments from 5 files instead of one consolidated, enriched answer.

---

## 2. Objective

Wire the existing analyzer into the scan pipeline, add a search endpoint, enable deduplication, and add observability — using code that **already exists** in the repo. This is an integration task, not a rewrite.

**One-liner:** Connect the building blocks that are already built but not wired together.

---

## 3. Current Architecture (As-Is)

```
Scan timer (15 min)
  → run_scan()
    → git diff (incremental)
    → chunk text (800 chars, 150 overlap)
    → embed via Ollama (nomic-embed-text, 768 dims)
    → upsert to Qdrant (agent_knowledge collection)
    → NO analysis, NO dedup, NO quality filter
```

**Files:**
- `main.py` (~650 LOC) — Scanner, API, chunking, embedding, Qdrant upsert
- `analyzer.py` (~263 LOC) — LLM extraction: summary, concepts, tags, relevance_score, doc_type. **NEVER CALLED.**
- `catalog.py` (~200 LOC) — Product catalog CRUD. Working, separate concern.

**Key constants (main.py):**
```python
CHUNK_SIZE = 800
CHUNK_OVERLAP = 150
MIN_CHUNK_LEN = 50
SIMILARITY_THRESHOLD = 0.92  # DEFINED BUT NEVER USED
EMBED_MODEL = "nomic-embed-text:latest"
EMBED_DIM = 768
OLLAMA_URL = "http://localhost:11434"
```

**Qdrant collection:** `agent_knowledge`
- 1,598 points currently
- Payload: `{source, node, type, chunk_index, offset, text, last_verified}`
- Missing: `summary, concepts, tags, relevance_score, doc_type, dependencies`

---

## 4. Target Architecture (To-Be)

```
Scan timer (15 min)
  → run_scan()
    → git diff (incremental)
    → chunk text (800 chars, 150 overlap)
    → embed via Ollama
    → [NEW] analyzer.batch_analyze(chunks) → enrich with LLM
    → [NEW] filter by relevance_score >= 0.3
    → [NEW] dedup check (cosine > 0.92 against existing points)
    → upsert to Qdrant (enriched payload)

API:
  → [NEW] GET /search?q=...&top_k=5   — semantic search on agent_knowledge
  → [NEW] GET /metrics                  — quality/coverage stats
  → [EXISTING] GET /health, /status, /audit, /catalog/*
```

---

## 5. Implementation Tasks

### Task 1: Add `GET /search` endpoint

**File:** `main.py`
**Priority:** HIGHEST — without this, no one can search agent_knowledge via HTTP

Add a search endpoint that:
1. Receives query string `q` and optional `top_k` (default 5)
2. Embeds the query via Ollama (same model as indexing)
3. Searches Qdrant `agent_knowledge` collection with cosine similarity
4. Returns results with score, text, source, and enrichment metadata if available

```python
@app.get("/search")
async def search_knowledge(q: str, top_k: int = 5, min_score: float = 0.3):
    """Semantic search over agent_knowledge collection."""
    if not q or len(q.strip()) < 3:
        raise HTTPException(status_code=400, detail="Query must be at least 3 characters")

    # Embed query
    embedding = embed_text(q.strip())  # reuse existing embed_text function

    # Search Qdrant
    results = qdrant.search(
        collection_name="agent_knowledge",
        query_vector=embedding,
        limit=top_k,
        score_threshold=min_score,
    )

    return {
        "query": q,
        "results": [
            {
                "score": round(hit.score, 4),
                "text": hit.payload.get("text", ""),
                "source": hit.payload.get("source", ""),
                "node": hit.payload.get("node", ""),
                "type": hit.payload.get("type", ""),
                # Enrichment fields (empty if not yet analyzed)
                "summary": hit.payload.get("summary", ""),
                "concepts": hit.payload.get("concepts", []),
                "tags": hit.payload.get("tags", []),
                "relevance_score": hit.payload.get("relevance_score"),
                "doc_type": hit.payload.get("doc_type", ""),
            }
            for hit in results
        ],
        "total": len(results),
    }
```

**Verification:**
```bash
curl -s "http://localhost:8095/search?q=claude+code+configuration&top_k=3" | python3 -m json.tool
# Should return results with score > 0.3
```

---

### Task 2: Wire analyzer into scan loop

**File:** `main.py`
**Depends on:** Read `analyzer.py` first to understand its API

The analyzer already exists at `analyzer.py` and has:
```python
class DocumentAnalyzer:
    async def analyze_chunk(self, text: str, source: str, doc_type: str) -> dict
    async def batch_analyze(self, chunks: list, source: str, doc_type: str) -> list
```

Each analyzed chunk returns:
```python
{
    "summary": "Brief summary of the chunk",
    "concepts": ["concept1", "concept2"],
    "tags": ["tag1", "tag2"],
    "relevance_score": 0.85,  # 0.0-1.0
    "doc_type": "spec|sop|ir|code|config|docs",
    "dependencies": ["dep1"],
}
```

**Integration point:** In `run_scan()`, after chunking and before upserting, call the analyzer:

```python
# After: chunks = chunk_text(content)
# Before: upsert to Qdrant

# Feature flag — enable/disable analyzer
ANALYZER_ENABLED = os.getenv("ERUDITO_ANALYZER_ENABLED", "true").lower() == "true"

if ANALYZER_ENABLED and chunks:
    try:
        analyzer = DocumentAnalyzer()  # or reuse a module-level instance
        enriched = await analyzer.batch_analyze(chunks, source=full_source, doc_type=scan_source.get("type", "docs"))

        # Merge enrichment into chunk payloads
        for i, chunk_data in enumerate(chunks_to_upsert):
            if i < len(enriched) and enriched[i]:
                chunk_data["summary"] = enriched[i].get("summary", "")
                chunk_data["concepts"] = enriched[i].get("concepts", [])
                chunk_data["tags"] = enriched[i].get("tags", [])
                chunk_data["relevance_score"] = enriched[i].get("relevance_score", 0.5)
                chunk_data["doc_type"] = enriched[i].get("doc_type", "")
                chunk_data["dependencies"] = enriched[i].get("dependencies", [])

        # Filter by relevance
        min_relevance = float(os.getenv("ERUDITO_MIN_RELEVANCE", "0.3"))
        chunks_to_upsert = [c for c in chunks_to_upsert if c.get("relevance_score", 1.0) >= min_relevance]

    except Exception as exc:
        logger.warning("Analyzer failed, proceeding without enrichment: %s", exc)
        # Fallback: upsert raw chunks without enrichment (current behavior)
```

**Important:** The analyzer uses an LLM cascade (local first → free remote). Read `analyzer.py` to understand the model cascade and verify it works:
```bash
# Test analyzer standalone
cd ~/ai-lab/erudito
python -c "
import asyncio
from analyzer import DocumentAnalyzer
async def test():
    a = DocumentAnalyzer()
    r = await a.analyze_chunk('This is a SPEC document defining the architecture of the mesh-channel service.', 'test.md', 'spec')
    print(r)
asyncio.run(test())
"
```

**Environment variables:**
- `ERUDITO_ANALYZER_ENABLED=true` — feature flag (default: true)
- `ERUDITO_MIN_RELEVANCE=0.3` — minimum relevance score to index (default: 0.3)

---

### Task 3: Enable deduplication

**File:** `main.py`

Before upserting a chunk, check if a very similar chunk already exists in Qdrant. If cosine similarity > 0.92, skip the upsert (or update metadata only).

```python
DEDUP_ENABLED = os.getenv("ERUDITO_DEDUP_ENABLED", "true").lower() == "true"

async def is_duplicate(embedding: list, threshold: float = 0.92) -> bool:
    """Check if a very similar chunk already exists."""
    if not DEDUP_ENABLED:
        return False
    try:
        results = qdrant.search(
            collection_name="agent_knowledge",
            query_vector=embedding,
            limit=1,
            score_threshold=threshold,
        )
        return len(results) > 0
    except Exception:
        return False  # On error, allow the upsert
```

**Integration:** In the upsert loop, before calling `qdrant.upsert()`:
```python
embedding = embed_text(chunk_text)
if await is_duplicate(embedding, SIMILARITY_THRESHOLD):
    stats["skipped_dedup"] += 1
    continue
# else: proceed with upsert
```

**Note:** This adds one Qdrant search per chunk. For incremental scans (few files changed), this is fine. For full re-index, it may be slow. Add `ERUDITO_DEDUP_ENABLED=false` env var to skip during bulk operations.

**Audit logging:** Log dedup skips:
```python
audit_log("skip_dedup", source=full_source, chunk_index=i, similarity=results[0].score)
```

---

### Task 4: Add `GET /metrics` endpoint

**File:** `main.py`

Observability endpoint that answers: "Is Erudito actually doing its job?"

```python
@app.get("/metrics")
async def get_metrics():
    """Return quality and coverage metrics for the knowledge base."""
    collection_info = qdrant.get_collection("agent_knowledge")
    total_points = collection_info.points_count

    # Sample points to compute stats (scroll 200 random points)
    points, _ = qdrant.scroll(
        collection_name="agent_knowledge",
        limit=200,
        with_payload=True,
        with_vectors=False,
    )

    enriched = sum(1 for p in points if p.payload.get("relevance_score") is not None)
    high_quality = sum(1 for p in points if (p.payload.get("relevance_score") or 0) > 0.7)
    has_concepts = sum(1 for p in points if p.payload.get("concepts"))
    has_summary = sum(1 for p in points if p.payload.get("summary"))

    # Source distribution
    sources = {}
    for p in points:
        src = p.payload.get("source", "unknown")
        # Extract repo/directory from source path
        parts = src.split("/")
        repo = parts[0] if parts else "unknown"
        sources[repo] = sources.get(repo, 0) + 1

    sample_size = len(points)

    return {
        "total_points": total_points,
        "sample_size": sample_size,
        "enrichment": {
            "enriched_pct": round(enriched / sample_size * 100, 1) if sample_size else 0,
            "high_quality_pct": round(high_quality / sample_size * 100, 1) if sample_size else 0,
            "has_concepts_pct": round(has_concepts / sample_size * 100, 1) if sample_size else 0,
            "has_summary_pct": round(has_summary / sample_size * 100, 1) if sample_size else 0,
        },
        "sources": dict(sorted(sources.items(), key=lambda x: -x[1])),
        "collection": {
            "vectors_count": collection_info.vectors_count,
            "segments_count": getattr(collection_info, 'segments_count', None),
        },
    }
```

**Verification:**
```bash
curl -s http://localhost:8095/metrics | python3 -m json.tool
# Before wiring analyzer: enriched_pct = 0
# After wiring analyzer: enriched_pct should climb with each scan
```

---

### Task 5: Re-index existing content with enrichment

After Tasks 1-4 are deployed, trigger a full re-index to enrich the existing 1,598 chunks:

```bash
# Option A: Reset and re-scan everything
curl -s -X POST http://localhost:8095/scan  # Triggers incremental (won't re-process unchanged files)

# Option B: Force full re-index by clearing last_commit tracking
# In the Erudito status, reset each source's last_commit to force re-scan
# Then trigger scan
```

**Better approach:** Add a `POST /reindex` endpoint that:
1. Clears `last_commit` for all sources (forces full re-scan)
2. Triggers `run_scan()` with analyzer enabled
3. Returns stats on completion

```python
@app.post("/reindex")
async def force_reindex():
    """Force full re-index of all sources with LLM enrichment."""
    # Clear scan state to force full rescan
    for source in SCAN_SOURCES:
        source["last_commit"] = None

    stats = await run_scan()
    return {"status": "completed", "stats": stats}
```

**Warning:** Full re-index with analyzer will make LLM calls for every chunk (~1,600 chunks). With batch processing and local model, this may take 30-60 minutes. Use the feature flag to control:
```bash
ERUDITO_ANALYZER_ENABLED=true curl -X POST http://localhost:8095/reindex
```

---

### Task 6: Update audit logging for new features

Extend audit entries with new actions:

```python
# New audit actions:
"analyze_success"   — chunk was analyzed by LLM
"analyze_fail"      — LLM analysis failed for chunk
"skip_relevance"    — chunk filtered by min_relevance threshold
"skip_dedup"        — chunk skipped due to similarity > 0.92
"reindex_start"     — full reindex triggered
"reindex_complete"  — full reindex finished
```

---

## 6. Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `ERUDITO_ANALYZER_ENABLED` | `true` | Enable LLM enrichment during scan |
| `ERUDITO_DEDUP_ENABLED` | `true` | Enable similarity-based dedup |
| `ERUDITO_MIN_RELEVANCE` | `0.3` | Minimum relevance score to index |
| `ERUDITO_SIMILARITY_THRESHOLD` | `0.92` | Cosine threshold for dedup |

---

## 7. Files to Modify

```
~/ai-lab/erudito/main.py          # Tasks 1-6: search, analyzer wiring, dedup, metrics, reindex
~/ai-lab/erudito/analyzer.py       # READ ONLY — verify API, test standalone, no changes needed
~/ai-lab/erudito/.env              # Add feature flag env vars if needed
```

**Do NOT modify:** `catalog.py` (working, separate concern)

---

## 8. Testing Protocol

### After Task 1 (search endpoint):
```bash
# Should return results
curl -s "http://localhost:8095/search?q=jasper+agent+architecture" | python3 -c "
import sys,json; r=json.load(sys.stdin)
print(f'Results: {r[\"total\"]}')
for h in r['results'][:3]: print(f'  [{h[\"score\"]}] {h[\"source\"]}: {h[\"text\"][:80]}...')
"
```

### After Task 2 (analyzer wiring):
```bash
# Trigger scan with a known changed file
echo "# Test" >> ~/desarrollos_openclaw/claude_contracts/knowledge/platform-claude-code.md
cd ~/desarrollos_openclaw/claude_contracts && git add -A && git commit -m "test: trigger erudito rescan"
curl -s -X POST http://localhost:8095/scan | python3 -m json.tool
# Check: stats.inserted > 0 or stats.updated > 0

# Verify enrichment
curl -s "http://localhost:8095/search?q=claude+code" | python3 -c "
import sys,json; r=json.load(sys.stdin)
for h in r['results'][:2]:
    print(f'Score: {h[\"score\"]}')
    print(f'Summary: {h.get(\"summary\", \"NONE\")}')
    print(f'Concepts: {h.get(\"concepts\", \"NONE\")}')
    print(f'Relevance: {h.get(\"relevance_score\", \"NONE\")}')
    print()
"
# Summary and concepts should be populated (not empty/NONE)
```

### After Task 3 (deduplication):
```bash
curl -s http://localhost:8095/metrics | python3 -c "
import sys,json; m=json.load(sys.stdin)
print(f'Total: {m[\"total_points\"]}')
print(f'Enriched: {m[\"enrichment\"][\"enriched_pct\"]}%')
print(f'High quality: {m[\"enrichment\"][\"high_quality_pct\"]}%')
"
```

### After Task 5 (full reindex):
```bash
# Before
curl -s http://localhost:8095/metrics | python3 -c "import sys,json; print(json.load(sys.stdin)['enrichment'])"
# enriched_pct should be ~0

# Trigger
curl -s -X POST http://localhost:8095/reindex
# Wait 30-60 min

# After
curl -s http://localhost:8095/metrics | python3 -c "import sys,json; print(json.load(sys.stdin)['enrichment'])"
# enriched_pct should be >80%
```

---

## 9. Acceptance Criteria

| # | Criterion | How to verify |
|---|-----------|---------------|
| 1 | `GET /search` returns relevant results | Query "jasper agent" returns Jasper-related chunks with score > 0.5 |
| 2 | Analyzer enriches new chunks | After scan, new chunks have `summary`, `concepts`, `relevance_score` populated |
| 3 | Low-relevance chunks are filtered | Chunks with relevance < 0.3 are not stored |
| 4 | Duplicate chunks are skipped | Scanning same content twice doesn't create duplicate points |
| 5 | `GET /metrics` shows enrichment stats | `enriched_pct` > 0 after analyzer runs |
| 6 | Feature flags work | `ERUDITO_ANALYZER_ENABLED=false` disables LLM calls |
| 7 | Existing functionality unbroken | `/health`, `/catalog`, `/scan`, `/audit` still work |
| 8 | Audit logs new actions | `analyze_success`, `skip_dedup`, `skip_relevance` appear in audit |

---

## 10. Deployment

```bash
# 1. Apply changes
cd ~/ai-lab/erudito
# ... edit main.py ...

# 2. Restart service
systemctl --user restart erudito.service

# 3. Verify health
curl -s http://localhost:8095/health | python3 -m json.tool

# 4. Test search
curl -s "http://localhost:8095/search?q=test" | python3 -m json.tool

# 5. Trigger scan to test analyzer
curl -s -X POST http://localhost:8095/scan | python3 -m json.tool

# 6. Check metrics
curl -s http://localhost:8095/metrics | python3 -m json.tool

# 7. If all good, trigger full reindex (long-running)
curl -s -X POST http://localhost:8095/reindex
```

---

## 11. Estimated Effort

| Task | Effort | Notes |
|------|--------|-------|
| Task 1: Search endpoint | 15 min | Reuses existing embed_text + Qdrant search |
| Task 2: Analyzer wiring | 30 min | Read analyzer.py, test standalone, wire into scan loop |
| Task 3: Deduplication | 45 min | Add pre-upsert similarity check |
| Task 4: Metrics endpoint | 20 min | Scroll + aggregate |
| Task 5: Reindex endpoint + run | 15 min code + 30-60 min runtime | Long-running due to LLM calls |
| Task 6: Audit logging | 10 min | Add new action types |
| **Total** | **~2.5 hours code + 1 hour reindex** | |

---

## 12. Non-Interactive Execution Notes

This spec is designed to be executable in a **non-interactive Claude Code session**. Key points:

- All changes are in **one file** (`main.py`) — no cross-repo coordination needed
- The analyzer module (`analyzer.py`) should be **read but not modified**
- Test after each task before proceeding to the next
- If the analyzer's LLM cascade fails (no local model, no remote key), set `ERUDITO_ANALYZER_ENABLED=false` and proceed with Tasks 1, 3, 4 only
- Restart the service after changes: `systemctl --user restart erudito.service`
- The full reindex (Task 5) is long-running — trigger it and verify later, don't wait
- Commit after each task with message format: `feat(erudito): <description>`
- Documentation: create `IR-2026-03-16-erudito-v2-intelligent-rag.md` in `~/desarrollos_openclaw/claude_contracts/`
