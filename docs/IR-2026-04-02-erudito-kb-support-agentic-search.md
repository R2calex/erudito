# IR: Knowledge Base Support + Agentic Search Mode

**Date:** 2026-04-02
**Author:** r0calex + Claude Opus 4.6
**Project:** Erudito (Knowledge Orchestration Agent)
**Related Spec:** SPEC-rag-knowledge-manager.md
**Type:** Feature addition (two features in one session)

---

## Context

Erudito v3 only registered git repositories, limiting its scope to AI-Lab project documentation. Technical knowledge (Docker patterns, deployment guides, reference material) that lives in plain folders couldn't be indexed. Additionally, search was purely embedding-based (cosine similarity), which works for "find the doc about X" but fails for complex questions requiring reasoning across multiple projects.

Two features were implemented to address this:

1. **Knowledge Base support** — register non-git directories as `type: "knowledge_base"`
2. **Agentic search** — LLM-powered decompose + multi-retrieve + synthesize pipeline

---

## What Changed

### Feature 1: Knowledge Base Support

| File | Change |
|---|---|
| `core/registry.py` | Added `"type": "repo"` to `_DEFAULT_ENTRY`, added `type` param to `register()` |
| `core/scanner.py` | Added `KB_EXTENSIONS = {".md", ".txt", ".yaml", ".yml"}`, optional `extensions` param to `should_scan_file()` and `compute_fs_delta()` |
| `main.py` | `POST /registry` accepts `type` param, skips git validation for `knowledge_base`. New `_process_fs_project()` helper extracts shared logic for fs-based scanning (used by both ingested and knowledge_base routes). New Route 0 in `_scan_project()` for knowledge bases. |

**Registration:**
```bash
POST /registry
{
  "name": "docker-patterns",
  "path": "/app/knowledge/docker-patterns",
  "type": "knowledge_base",
  "node": "hanzo"
}
```

**Scanning:** Knowledge bases use `compute_fs_delta()` (filesystem hash comparison) with expanded extensions (`.md`, `.txt`, `.yaml`, `.yml`). Same curation and indexing pipeline as git repos.

**Docker mount required:** The knowledge directory must be mounted into the container:
```
-v /home/r0calex/ai-lab/knowledge:/app/knowledge:ro
```

### Feature 2: Agentic Search Mode

| File | Change |
|---|---|
| `core/query.py` | Added config constants (`AGENTIC_MODEL`, `AGENTIC_DECOMPOSE_TIMEOUT`, etc.), `_llm_call()`, `_decompose_query()`, `_multi_retrieve()`, `_synthesize_answer()`, `_execute_agentic()` |
| `main.py` | Updated mode regex to include `agentic` |

**Flow:**
1. **Decompose** — LLM breaks complex query into 1-3 sub-queries with project hints
2. **Multi-retrieve** — Vector search for each sub-query, deduplicate by (source, project), keep highest scores
3. **Synthesize** — LLM produces coherent answer with `[Source: filename]` citations

**Graceful degradation at every step:**
- Decompose fails → single original query (vector search still works)
- Synthesis fails → raw formatted context (same as `mode=agent`)
- Both fail → still returns Qdrant results

**Endpoint:**
```
GET /search?q={query}&mode=agentic&project={project}&top_k=5&max_tokens=800
```

**Response:**
```json
{
  "mode": "agentic",
  "query": "original question",
  "answer": "synthesized answer with [Source: ...] citations",
  "reasoning_steps": ["sub-query 1", "sub-query 2"],
  "sources": [{"title", "project", "type", "score", "url"}],
  "confidence": "high|medium|low|none",
  "elapsed_ms": 12345
}
```

---

## Environment Variables Added

| Variable | Default | Purpose |
|---|---|---|
| `AGENTIC_LLM_MODEL` | `openai/zen/minimax-m2.5-free` | LLM model for agentic reasoning (currently using `openai/zen/minimax-m2.5` due to rate limits on free tier) |
| `AGENTIC_DECOMPOSE_TIMEOUT` | `8` | Timeout (seconds) for query decomposition |
| `AGENTIC_SYNTH_TIMEOUT` | `12` | Timeout (seconds) for answer synthesis |
| `LITELLM_MASTER_KEY` | (empty) | Required for LiteLLM proxy authentication |

---

## Design Decisions

1. **No NLM live in agentic mode** — too slow (3-8s per call), same rationale as `mode=agent`
2. **No caching of synthesized answers** — each query is contextual; underlying Qdrant notes are already cached
3. **Project hint propagation** — if `identify_project()` resolves a project before decomposition, that project is applied as default hint to sub-queries that don't specify one
4. **Free models preferred** — defaults to free tier, but `zen/minimax-m2.5` (paid) needed due to rate limits on free models

---

## Verification

| Test | Result |
|---|---|
| Register knowledge_base (non-git folder) | PASS — `docker-patterns` registered and scanned |
| Knowledge base scan picks up `.md` files | PASS — 1 file detected, curated as Tier 3 |
| Agentic simple query ("what is Keystone") | PASS — 1 step, ~10s, correct answer with citations |
| Agentic complex query (cross-project) | PASS — 2 steps decomposition, synthesized answer with node table |
| Graceful degradation (LLM unavailable) | PASS — falls back to vector search results |

---

## Performance

| Mode | Typical latency | LLM calls |
|---|---|---|
| `default` | 50-200ms | 0 |
| `agent` | 50-200ms | 0 |
| `dual` | 3-8s | 0 (but NLM live) |
| `agentic` (simple) | 6-12s | 2 (decompose + synthesize) |
| `agentic` (complex) | 15-30s | 2 (decompose + synthesize) |
