# Erudito v3 — Knowledge Orchestration Agent with NotebookLM Integration

**Date:** 2026-03-18
**Status:** Approved (brainstorming complete)
**Author:** r0calex + Claude Opus 4.6

## Overview

Erudito v3 is a reengineering of the AI-Lab knowledge orchestration agent. The core change: NotebookLM becomes Erudito's primary consultation and validation tool, while Qdrant remains the autonomous persistent knowledge base. Erudito orchestrates both — it is never dependent on NotebookLM being available.

### Problem Statement

Erudito v2 maintains a custom RAG pipeline (chunking, embedding, LLM enrichment, deduplication) that is complex and produces knowledge without external validation. NotebookLM MCP (31 tools, already deployed) offers superior document comprehension that we are not leveraging.

### Design Principles

- **NotebookLM is a consultant, not the brain.** If it's down (auth expired, rate limit, subscription), Erudito continues operating from Qdrant.
- **Event-driven, not polling.** Only process what changed (git hash deltas). No changes = no work.
- **No alert spam.** Log everything internally, emit mesh alerts only when agents are consuming them (future).
- **Honesty over hallucination.** "I don't know" is a valid response. Offer next steps, don't fabricate.
- **Auto-enrich, don't block.** Missing metadata gets inferred and labeled, not rejected.

## Architecture

### Approach: Modular by Domain (single process)

One FastAPI process with clear module separation. Not a monolith (v2's problem), not microservices (overkill for current scale).

```
erudito/
├── main.py                # FastAPI app, lifespan, endpoints
├── core/
│   ├── registry.py        # Triple map (YAML + Redis dual-write)
│   ├── scanner.py         # Delta detection (git hash diff)
│   ├── indexer.py         # Qdrant embedding pipeline
│   ├── query.py           # Search + confidence routing (threshold 0.75)
│   └── enricher.py        # Auto-enrich missing metadata
├── integrations/
│   ├── notebooklm.py      # NotebookLM MCP client (via LiteLLM)
│   ├── sanitizer.py       # HTTP client → sanitizer-hanzo
│   └── github.py          # Repo sync (future, placeholder)
├── data/
│   ├── registry.yaml      # Map state (+ backup in Redis)
│   └── audit.jsonl        # Operations log
├── tests/
│   ├── test_registry.py
│   ├── test_scanner.py
│   ├── test_indexer.py
│   ├── test_query.py
│   ├── test_notebooklm.py
│   └── test_sanitizer.py
├── Dockerfile
├── docker-compose.yaml
└── requirements.txt
```

### External Services (on Hanzo)

| Service | Port | Purpose | Dependency Level |
|---------|------|---------|-----------------|
| Qdrant | 6333 | Vector DB (persistent knowledge) | Required |
| Redis | 6379 | Registry backup + cache | Required |
| Ollama | 11434 | Embeddings (nomic-embed-text) | Required |
| LiteLLM | 4000 | LLM gateway + NLM MCP proxy | Required |
| sanitizer-hanzo | 8086 | Text masking (secrets redaction) | Required |
| NotebookLM MCP | 8765 (via LiteLLM) | Document comprehension | Best-effort |

### Deployment

Docker Compose with two services: Erudito (:8095) and sanitizer-hanzo (:8086), both on the existing `litellm-local_litellm-network`. Repos mounted read-only — Erudito never modifies source repos. Auto-enrich generates frontmatter in memory only.

Sanitizer-hanzo is the same image as kubo-sanitizer (16 masking patterns, categorized labels like `[MASKED_AUTH]`, `[AWS_KEY_MASKED]`). It runs as its own container, independent of Erudito. Sariatu also uses it via Tailscale.

---

## Component Design

### 1. Registry (core/registry.py)

The central map connecting every project to its NotebookLM notebook and GitHub repo.

**Storage:** Dual-write to YAML (human-readable, git-versionable) and Redis (crash recovery). Each write increments a `version` counter stored in both YAML and Redis. On startup reconciliation: if YAML exists and Redis is empty → populate Redis. If YAML is missing/corrupt and Redis has data → regenerate YAML. If both exist → compare `version` numbers, highest version wins (handles crash-between-writes where Redis may be ahead of YAML). Every write goes to YAML first, then Redis — if a crash occurs between the two, the next startup detects the version mismatch and reconciles. This means at most one operation can be lost, and only in a narrow crash window.

**Entry structure:**

```yaml
projects:
  infra-mcp:
    path: ~/ai-lab/infra-mcp
    node: hanzo
    repo: r0calex/infra-mcp
    notebook_id: null              # Filled on notebook creation
    last_hash: e2c8bfc...
    last_sync: 2026-03-17T14:00:00Z
    status: validated              # pending | synced | validated | stale
    coverage: 0.85
    auto_enriched: false
    doc_count: 12
    last_nlm_session: 2026-03-17T10:00:00Z
```

**Status lifecycle:**
- `pending` → Project registered, no indexing yet
- `synced` → Files in Qdrant, but not validated by NotebookLM
- `validated` → Full cycle complete: Qdrant + NotebookLM + questions answered + notes created
- `stale` → New commits detected that haven't been processed (transitions back from validated)

**Operations:** register, get, update_sync, mark_validated, mark_stale, list_by_status, summary.

**`last_nlm_session` semantics:** Updated only on successful NLM cycle completion, not on attempt. This makes it a reliable staleness indicator — if the timestamp is old, NLM hasn't successfully validated in a while.

**`doc_count` update:** Updated by the Indexer after each successful upsert cycle for a project. Reflects the current count of .md files tracked in Qdrant for that project.

### 2. Scanner (core/scanner.py)

Detects what changed via git hash comparison and produces Delta objects.

**Cycle (every 15 minutes):**

1. `registry.list_all()` → all projects
2. For each: `git rev-parse HEAD` → current hash
3. Compare with `registry.last_hash`
4. If equal → skip (no changes, no work, no noise)
5. If different → `git diff --name-only last_hash..HEAD -- '*.md'`
6. Read changed file contents
7. Validate metadata (frontmatter present?) — if missing, auto-enrich + label
8. Sanitize via sanitizer-hanzo (`POST /sanitize`)
9. Return Delta object

**Delta structure:**

```yaml
Delta:
  project: infra-mcp
  old_hash: a1b2c3d...
  new_hash: e4f5g6h...
  files:
    - path: README.md
      action: modified        # modified | added | deleted
      content: "..."          # sanitized, with metadata
      auto_enriched: true
  timestamp: 2026-03-17T14:30:00Z
```

**Scan scheduling:** The 15-minute scan cycle runs as an asyncio background task within the FastAPI lifespan (not external cron). Configurable via `SCAN_INTERVAL_MINUTES` env var. Manual trigger available via `POST /scan`.

**Design decisions:**
- Only .md files for now — 90% of value is in docs. Expandable later.
- Sanitization happens here, once — all downstream consumers receive clean text.
- Auto-enrich happens here — Indexer and NotebookLM always receive docs with metadata.
- Remote node scanning (Sariatu via SSH) is deferred to backlog. v3 scans only local repos mounted as volumes.

### 3. Indexer (core/indexer.py)

Fast, local embedding pipeline. Always available.

**Flow (on receiving Delta):**

1. Chunk each file (800 chars, 150 overlap)
2. Embed via Ollama (nomic-embed-text)
3. Upsert in Qdrant (collection: `agent_knowledge`)
4. If action=deleted → remove from collection
5. `registry.update_sync(project, new_hash)` → status becomes `synced`

**Estimated time:** ~2-5 seconds per file.
**If it fails:** Retry 3x, then log error. Does not block NotebookLM cycle.

### 4. NotebookLM Cycle (integrations/notebooklm.py)

Intelligent, external, best-effort. Runs in parallel with Indexer.

**Flow (on receiving Delta):**

1. Check source count for the notebook. NotebookLM limits: 50 sources/notebook, ~500K tokens total. If adding new sources would exceed the limit, consolidate older sources (merge multiple small .md into a single combined source) or skip with a warning log. A `nlm_source_count` field in the registry tracks current count per notebook.
2. `source_add` each changed file to the project's notebook
3. Wait for processing (~15-30s)
4. `notebook_query`: "Condense all sources"
5. Generate 5-10 pertinent questions:
   - **Fixed questions (always):**
     1. What changes have occurred since the last session?
     2. Were there any design changes relative to the original plan?
     3. What new resources have been registered for this project?
     4. On which AI-Lab node is this project running?
     5. What is the current project status according to documentation?
   - **Dynamic questions (5, LLM-generated):** Based on Delta content — what specifically changed.
6. `notebook_query` with each question
7. `note()` — save answers as notes in the notebook. Validate by calling `source_list_drive` or re-reading the notebook to confirm the note count increased. Timeout after 10s — if validation fails, log warning but continue (note may still be saved asynchronously by NLM).
8. Store same notes in Qdrant (collection: `nlm_notes`)
9. `registry.mark_validated(project)` → status becomes `validated`

**Estimated time:** ~2-5 minutes per project.
**If it fails:** Log, status stays `synced` (not `validated`). Retries on next scan cycle.

**Two Qdrant collections:**
- `agent_knowledge` — raw doc embeddings (from Indexer)
- `nlm_notes` — validated answers from NotebookLM (higher confidence, ground truth for KPIs)

### 5. Query Engine (core/query.py)

Routes questions through Qdrant and optionally NotebookLM based on confidence.

**Algorithm:**

1. **Identify project** from the question. If not identifiable → cross-project search (Qdrant only).
2. **Search Qdrant** — both `agent_knowledge` and `nlm_notes`. Results from `nlm_notes` get a +0.05 score boost before combining (they are validated ground truth with higher inherent confidence than raw chunks). Combined results sorted by adjusted score.
3. **Best score >= 0.75** → Respond directly. Confidence: high.
4. **Best score < 0.75 + project has notebook_id** → Escalate to `notebook_query`.
   - NLM responds well → Respond with NLM info. Save as note in Qdrant (enriches for next time).
   - NLM doesn't know either → "I don't have information on this. Want me to investigate?"
   - NLM unavailable → Respond with Qdrant results + low confidence disclaimer.
5. **Best score < 0.75 + no notebook_id** → "I don't have sufficient information."
6. **Cross-project** → Qdrant only (NotebookLM cannot cross notebooks).

**Confidence threshold:** 0.75 (configurable via `ERUDITO_CONFIDENCE_THRESHOLD`).

**Response structure:**

```json
{
  "answer": "...",
  "confidence": "high | medium | low",
  "sources": [
    {"type": "qdrant | nlm_note | notebooklm_live", "project": "...", "file": "...", "score": 0.85}
  ],
  "project_identified": "infra-mcp | null",
  "nlm_consulted": true,
  "suggestion": null
}
```

### 6. Enricher (core/enricher.py)

Auto-generates missing frontmatter metadata when scanner detects files without it.

**Frontmatter standard:**

```markdown
---
project: infra-mcp
type: spec | sop | ir | readme | backlog | design
last_updated: 2026-03-17
status: active | draft | deprecated
auto_enriched: true
session_id: <optional, session where doc was created>
---
```

**Behavior:**
- If frontmatter is completely missing → generate from filename, path, git date, and project context. Label `auto_enriched: true`.
- If frontmatter exists but incomplete → fill missing fields only. Label `auto_enriched: true`.
- If frontmatter is complete → pass through unchanged. `auto_enriched: false`.
- Enrichment happens in memory — original files are never modified.

**Type inference rules (no LLM required):**
- Filename contains "spec" or "SPEC" → type: spec
- Filename contains "sop" or "SOP" → type: sop
- Filename contains "ir" or "IR" or "implementation-report" → type: ir
- Filename is "README.md" → type: readme
- Filename contains "backlog" or "BACKLOG" → type: backlog
- Filename contains "design" → type: design
- Otherwise → type: doc (generic fallback, always valid)

**Enforcement hook:** A pre-commit or CI hook should validate frontmatter on .md files before they enter the repo. This is the first line of defense. Erudito's auto-enrich is the safety net.

---

## KPIs and Metrics

### Core KPIs

**Coverage:** `validated_projects / total_projects`. Also reports partial coverage (synced + validated). Target: 100% validated.

**Coherence:** Executed by eval-agent, not Erudito. Process:
1. Take questions from the latest scan cycle (stored in `nlm_notes`)
2. Ask the same questions to Erudito via `/search` (Qdrant only)
3. LLM judge compares responses
4. If Erudito's answer is coherent with NotebookLM's validated answer → pass
5. Coherence = pass / total_probes. Acceptable threshold: >= 70%.

### Secondary KPIs

| KPI | What it measures | Target |
|-----|-----------------|--------|
| Freshness | Avg time between commit and sync | < 30 min |
| Metadata compliance | % of files with complete (non-auto-enriched) frontmatter | > 80% |
| NLM Health | % of successful NotebookLM calls in 24h | > 90% (below 50% = auth likely expired) |
| Query Hit Rate | % of queries answered with high confidence (>= 0.75) | > 75% |

### Metrics Endpoint

`GET /metrics` returns:

```json
{
  "coverage": {
    "total_projects": 8,
    "validated": 5, "synced": 2, "stale": 1, "pending": 0,
    "coverage_pct": 62.5,
    "coverage_partial_pct": 87.5
  },
  "freshness_avg_minutes": 22,
  "metadata_compliance_pct": 78.0,
  "nlm_health_24h_pct": 95.0,
  "query_hit_rate_pct": 82.0,
  "coherence_last_score": 80.0,
  "coherence_last_run": "2026-03-18T01:00:00Z",
  "last_scan": "2026-03-18T02:15:00Z"
}
```

`coherence_last_score` and `coherence_last_run` are populated by eval-agent writing back to Erudito via a `POST /metrics/coherence` endpoint (accepts `{score, timestamp}`). This keeps coherence visible in the standard `/metrics` response without Erudito needing to compute it.

This format should become the standard for all AI-Lab agents' `/metrics` endpoints (DevOps, Keystone, future agents) to enable a unified dashboard in the future.

---

## API Endpoints

### System
| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Status, Qdrant, Ollama, NLM, Redis connectivity |
| GET | `/metrics` | All KPIs (coverage, freshness, compliance, NLM health, hit rate) |
| GET | `/audit` | Recent operations log |

### Registry
| Method | Path | Description |
|--------|------|-------------|
| GET | `/registry` | List all projects and their status |
| GET | `/registry/{project}` | Detail for one project |
| POST | `/registry` | Register new project (name, path, node, repo). Validates: path exists, is a git repo, not already registered under another name. Returns 400 with details if validation fails. |

### Scanning
| Method | Path | Description |
|--------|------|-------------|
| POST | `/scan` | Force scan of all projects |
| POST | `/scan/{project}` | Force scan of specific project |

### Queries
| Method | Path | Description |
|--------|------|-------------|
| GET | `/search?q=...&project=...&top_k=5` | Semantic search with confidence routing |
| GET | `/search?q=...&top_k=10` | Cross-project search (Qdrant only, no project filter) |

All existing v2 endpoints (`/health`, `/search`, `/scan`) remain backward compatible. eval-agent does not break during migration.

---

## Migration from v2

1. **Qdrant `agent_knowledge`:** Kept as-is. Existing embeddings remain valid. New collection `nlm_notes` created.
2. **Qdrant `project_catalog`:** The v2 catalog collection and `/catalog/*` endpoints are removed. The registry replaces the catalog's purpose (project listing and metadata). Existing catalog data in Qdrant can be dropped after migration.
3. **scan_state.json → registry.yaml:** One-shot migration script converts last_hash per source to the new registry format. Name derivation rule: use the basename of the source path (e.g., `/home/r0calex/ai-lab/infra-mcp` → `infra-mcp`). For ambiguous paths (e.g., `~/desarrollos_openclaw/proyectos/jasper_3.0/agents/profiles`), the migration script includes an alias map that must be reviewed manually before execution. The script outputs a preview diff and requires confirmation.
4. **Create notebooks:** First scan cycle post-deploy creates NotebookLM notebooks for each registered project.
5. **Endpoints:** Backward compatible for /health, /search, /scan. Catalog endpoints (`/catalog/*`) are removed — replaced by `/registry`. New endpoints added (/registry, /metrics).
6. **eval-agent KPIs:** Update `~/ai-lab/agent-eval/kpis/erudito.yaml` with new endpoints and probes.

---

## Backlog (Future)

- **Mesh-overview notebook:** A virtual project entry in the registry that aggregates the main README/SPEC from every project into a single NotebookLM notebook. Enables cross-project queries via NLM. Evaluate after v3 stabilizes.
- **AI-Lab Dashboard:** Unified frontend consuming `/metrics` from all agents. Each agent redesign should follow the same metrics format to be plug-and-play.
- **GitHub integration:** `integrations/github.py` — sync registry with repo state, detect new repos, auto-register.
- **Expand file types:** Support .yaml, .py beyond .md once the pipeline is proven.
- **Remote node scanning:** Scan Sariatu repos via SSH (Tailscale, key-based auth). Deferred from v3 scope — v3 scans only locally mounted volumes.

---

## Non-Functional Requirements

- **Graceful degradation:** NotebookLM down → Erudito continues from Qdrant. Sanitizer down → fallback to local regex patterns. Redis down → YAML is source of truth.
- **No mesh alerts (for now):** All events logged to audit.jsonl. Mesh alerting enabled when agents are actively consuming alerts.
- **Read-only repo access:** Erudito mounts repos as read-only volumes. Auto-enrich metadata stays in memory.
- **Security:** All text passes through sanitizer-hanzo before leaving local infrastructure (NotebookLM upload, Qdrant indexing).
