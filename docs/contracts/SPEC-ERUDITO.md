# SPEC-ERUDITO: Knowledge Orchestration Agent

**Version:** 1.0.0
**Date:** 2026-03-13
**Status:** Active
**Owner:** Hanzo (Claude Code)

---

## Objective

Erudito is the intelligent knowledge orchestration layer for the KUBO AI-Lab mesh. It replaces the previous `index_docs.py` raw indexer with an LLM-curated RAG pipeline that understands what it indexes, detects staleness, prevents credential leaks, and maintains a structured product catalog.

## Context / Problem Statement

The existing `index_docs.py` (in `~/ai-lab/litellm-local/`) is a batch indexer that:

- Re-indexes everything on every run (no incremental support)
- Has no deduplication — the same concept appears multiple times
- Has no anti-poison filtering — credentials can leak into Qdrant
- Does not extract structured knowledge — chunks are raw text with no metadata enrichment
- Cannot track file deletions — stale chunks persist forever
- Has no product catalog — "what projects do we have?" requires manual inventory

Erudito solves all of these problems.

## Architecture

```
                    +-----------+
                    |  FastAPI  |  :8095
                    |  /health  |
                    |  /scan    |
                    |  /audit   |
                    |  /catalog |
                    +-----+-----+
                          |
          +---------------+---------------+
          |               |               |
    +-----v-----+  +-----v-----+  +------v------+
    |  Scanner   |  |  Analyzer |  |  Catalog    |
    |  (git)     |  |  (LLM)   |  |  Manager    |
    +-----+------+  +-----+----+  +------+------+
          |               |               |
    +-----v------+  +-----v----+  +------v------+
    |  Git repos |  |  LiteLLM |  |  Qdrant     |
    |  (local +  |  |  Proxy   |  |  project_   |
    |   remote)  |  |  :4001   |  |  catalog    |
    +------------+  +----------+  +-------------+
          |
    +-----v------+
    |  Qdrant    |
    |  agent_    |
    |  knowledge |
    +------------+
```

### Components

1. **Scanner** (`main.py`) — Git-aware file scanner. Uses `git diff` for incremental changes since last known commit. Tracks scan state in `data/scan_state.json`.

2. **Analyzer** (`analyzer.py`) — LLM-powered knowledge extraction. Uses a model cascade (local first, then free remote) to extract summaries, concepts, tags, relevance scores, and document types from each chunk.

3. **Catalog Manager** (`catalog.py`) — Structured project index stored in Qdrant `project_catalog` collection. Supports CRUD and semantic search.

4. **Anti-RAG-Poison** — Regex-based credential detection that blocks files containing passwords, API keys, bearer tokens, connection strings, and private keys from entering Qdrant.

5. **Audit Trail** — JSONL-based logging of every insert, update, delete, skip, and error.

## Scan Sources

### Local (Hanzo)
| Path | Type |
|------|------|
| `~/desarrollos_openclaw/claude_contracts` | docs |
| `~/desarrollos_openclaw/opencode_contracts` | docs |
| `~/ai-lab/infra-mcp` | code |
| `~/ai-lab/devops-agent` | code |
| `~/ai-lab/mesh-monitor` | code |
| `~/ai-lab/event-bus` | code |
| `~/ai-lab/node-reporter` | code |
| `~/ai-lab/qdrant-mcp` | code |
| `~/ai-lab/erudito` | code |

### Remote (future)
| Node | Path | Access |
|------|------|--------|
| kubo | `~/ai-lab/` | SSH via Tailscale |
| sariatu | `~/ai-lab/` | SSH via ProxyJump |

## Anti-RAG-Poison

### Excluded Files
`.env`, `credentials.json`, `auth-profiles.json`, `join_token.txt`, `package-lock.json`

### Excluded Directories
`node_modules`, `.git`, `__pycache__`, `.pytest_cache`, `venv`, `.venv`, `data`

### Poison Patterns (regex)
| Pattern | Catches |
|---------|---------|
| `password\s*[:=]\s*['\"]?[^\s'\"]{8,}` | Hardcoded passwords |
| `api[_-]?key\s*[:=]\s*['\"]?[a-zA-Z0-9_-]{20,}` | API key assignments |
| `token\s*[:=]\s*['\"]?[a-zA-Z0-9._-]{20,}` | Token assignments |
| `postgresql://[^:]+:[^@]+@` | Postgres connection strings |
| `sk-[a-zA-Z0-9]{20,}` | OpenAI-style API keys |
| `Bearer\s+[a-zA-Z0-9._-]{20,}` | Bearer tokens |
| `BEGIN\s+(RSA\|DSA\|EC\|OPENSSH)\s+PRIVATE\s+KEY` | Private keys |

Files matching any pattern are skipped entirely and logged to the audit trail as `skip_poison`.

## LLM Cost Model

| Tier | Model | Cost | Usage |
|------|-------|------|-------|
| 1 (local) | `lmstudio/qwen/qwen3.5-9b` | $0 | Primary — runs on laptop GPU |
| 2 (free remote) | `zen/nemotron-3-super-free` | $0 | Fallback when laptop offline |
| 3 (free remote) | `zen/mimo-v2-flash-free` | $0 | Second fallback |
| 4 (cheap remote) | `zen/minimax-m2.5` | ~$0.15/1M tokens | Last resort |

**Estimated monthly cost:** $0-2 depending on scan frequency and local model availability.

## Embedding

- **Model:** `nomic-embed-text:latest` via Ollama
- **Dimensions:** 768
- **Distance:** Cosine
- **Cost:** $0 (local)

## Qdrant Collections

### `agent_knowledge` (curated RAG)
- Managed by Scanner + Analyzer
- Payload: `source`, `node`, `type`, `chunk_index`, `offset`, `text`, `last_verified`
- Enriched (when analyzer is used): `summary`, `concepts`, `doc_type`, `tags`, `dependencies`, `relevance_score`

### `project_catalog` (structured index)
- Managed by Catalog Manager
- Payload: `name`, `description`, `type`, `stack`, `status`, `node_primary`, `port`, `github`, `tags`, `version`, `updated_at`
- Searchable by natural language ("what monitoring tools do we have?")

## Risks and Challenges

1. **LLM extraction quality varies by model.** Mitigation: cascade with validation; if JSON parsing fails, use raw text with default metadata.

2. **First scan is slow (re-indexes everything).** Mitigation: subsequent scans are incremental via `git diff`. State persisted in `data/scan_state.json`.

3. **Remote node scanning via SSH adds latency.** Mitigation: scan remote nodes less frequently; local nodes are the priority.

4. **Ollama cold start after idle.** Mitigation: first embedding call may timeout; retry logic in batch processing.

5. **Anti-poison regex may have false positives.** Mitigation: all skips are logged to audit trail with `action: skip_poison` for manual review.

6. **Qdrant collection may accumulate stale chunks.** Mitigation: `last_verified` timestamp on every chunk; `/staleness/cleanup` endpoint prunes old entries.

## Acceptance Criteria

1. Health endpoint (`GET /health`) returns status OK when Qdrant and Ollama are reachable.
2. Scan (`POST /scan`) completes and returns stats with scanned/inserted/deleted counts.
3. Anti-poison blocks all credential patterns (password, API key, bearer token, connection string, private key).
4. Incremental scan only processes files changed since last commit.
5. Deleted files result in their chunks being removed from Qdrant.
6. Product catalog CRUD works (upsert, list, search, delete).
7. Catalog search returns semantically relevant results.
8. Audit trail records all operations with timestamps.
9. Scan completes in <5 minutes for incremental changes.
10. Monthly LLM cost stays under $2.

## Minimum Tests

| # | Test | Type |
|---|------|------|
| 1 | Health endpoint returns OK | integration |
| 2 | Scan triggers and returns stats | integration |
| 3 | Anti-poison blocks credential patterns | unit |
| 4 | File inclusion/exclusion rules work | unit |
| 5 | Chunking produces correct metadata | unit |
| 6 | Deterministic point IDs | unit |
| 7 | Scan state persistence (save/load) | unit |
| 8 | Audit trail creates entries | unit |
| 9 | Catalog CRUD (upsert/get/list/delete) | network |
| 10 | Catalog search returns relevant results | network |
| 11 | Embedding returns 768-dim vectors | network |
| 12 | LLM response parsing handles markdown wrapping | unit |
| 13 | Seed data has valid structure | unit |
| 14 | Staleness cleanup endpoint works | integration |
