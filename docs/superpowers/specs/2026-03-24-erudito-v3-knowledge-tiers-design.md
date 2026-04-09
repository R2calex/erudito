# Erudito v3 — Knowledge Tier System Design

**Date:** 2026-03-24
**Status:** Approved (brainstorming complete)
**Author:** r0calex + Claude Opus 4.6
**Depends on:** docs/superpowers/specs/2026-03-20-erudito-v3-data-curation-design.md

## Overview

The current Erudito v3 pipeline routes ALL projects through NotebookLM for knowledge distillation. NLM has restrictive rate limits (even with enterprise accounts), and simple projects (1-3 docs, single feature) consume the same NLM quota as architecturally complex ones. This wastes NLM budget on projects that don't benefit from deep comprehension.

This design introduces a **3-tier knowledge distillation system** that allocates NLM budget to projects that actually need it, uses a cheap local LLM for medium-complexity projects, and indexes curated docs directly for simple ones.

### Problem Statement

1. NLM rate limits exhausted — 12 of 13 projects still queued for re-curation
2. Simple projects (mesh-monitor, marker-mcp) don't benefit from NLM's deep analysis
3. No fallback distillation path when NLM is unavailable
4. NLM re-syncs on every delta, even trivial ones — no "cord-cutting" mechanism
5. No consumer feedback — no way to detect knowledge drift from real usage

### Design Principles

- **Tier is a pipeline config, not a code branch.** The pipeline stays: scan → curate → distill → Qdrant. Only the distill backend changes per tier.
- **Uniform data shape.** All tiers produce the same payload format in `nlm_notes`. `query.py` doesn't branch on tier.
- **NLM builds foundations, not increments.** NLM does the initial deep analysis. Subsequent deltas are handled by cheaper LLMs that leverage the NLM baseline as context.
- **Feedback from consumers, not self-assessment.** Drift detection comes from agents reporting query usefulness, not internal coherence checks.
- **Operator controls NLM re-engagement.** The distill loop never auto-triggers NLM after the baseline is established. Only `?force_nlm=true` does.

---

## Architecture

### Tier Definitions

| Tier | Backend | Condition | Example Projects |
|------|---------|-----------|-----------------|
| **1 — NLM** | NLM initial distillation + minimax for deltas | >= 5 unique features | jasper, erudito, infra-mcp, keystone |
| **2 — LLM** | minimax-m2.5 via LiteLLM (free) | 3-4 unique features | devops-agent, event-bus, opencode-contracts |
| **3 — Direct** | No LLM, embed curated docs directly | 1-2 unique features | mesh-monitor, marker-mcp, qdrant-mcp |

### Corrected Data Flow

```
1. Scanner → detects git hash deltas (unchanged)
2. Curator → groups by feature, consolidates (unchanged)
3. Tier Assignment → compute_tier() from feature count, registry override wins
4. Distill Step (NEW — replaces _nlm_sync_project):
   ├── Tier 1 + no baseline  → _distill_nlm(curated_dir)
   ├── Tier 1 + has baseline  → _distill_llm(curated_dir, nlm_notes=existing_notes)
   ├── Tier 2                 → _distill_llm(curated_dir, nlm_notes=None)
   └── Tier 3                 → _distill_direct(curated_dir)
5. Qdrant ← all tiers write to nlm_notes collection (same payload shape)
6. Consumer Feedback ← agents report usefulness via POST /feedback
```

### Background Loop Change

`_nlm_sync_loop` → renamed to `_distill_loop`. Same timing (5-min offset from scan), same interval.

**Concurrency:**
- NLM calls: `MAX_CONCURRENT_NLM=2` (unchanged, hard limit)
- LLM + Direct calls: `MAX_CONCURRENT_DISTILL` env var, default `5` (new, separate semaphore)
- The distill loop uses the appropriate semaphore based on the operation being performed

**Thread safety:** All tier-related registry updates MUST use `registry.update_fields()`, not direct dict mutation. The distill loop runs concurrently with the scan loop — this is the same issue flagged in the backlog (registry thread safety). This spec does not fix the 13 existing direct mutations but must not add new ones.

### What Changes vs Current Implementation

| Module | Change |
|--------|--------|
| `main.py` | `_nlm_sync_loop` → `_distill_loop`. `_nlm_sync_project` → `_distill_project` (dispatcher). New `_distill_llm`, `_distill_direct` functions. `POST /curate/{project}` gains `force_nlm` query param. New `POST /feedback` endpoint. Tier assignment after curation. |
| `core/registry.py` | New fields: `tier`, `computed_tier`, `nlm_baseline`, `last_distill`, `last_nlm_distill` |
| `core/distiller.py` | **New module.** `compute_tier()`, LLM prompt construction, canonical `DISTILL_QUESTIONS` list, `_fetch_existing_notes()`, payload schema |
| `integrations/notebooklm.py` | `FIXED_QUESTIONS` replaced by import from `core/distiller.DISTILL_QUESTIONS`. Keeps 8 existing questions for NLM (superset), but only the canonical 5 are used for comparison/indexing. |
| `core/query.py` | Minor: replace `from_nlm` boolean check with `distill_source` field check (`"nlm"`, `"llm"`, `"direct"`) |
| `core/curator.py` | No changes |
| `data/feedback.jsonl` | **New file.** Append-only consumer feedback log |

---

## Component Design

### 1. Canonical Questions & Payload Schema

**Single source of truth:** `core/distiller.py` defines `DISTILL_QUESTIONS` — the canonical 5 questions used across all tiers:

```python
# core/distiller.py
DISTILL_QUESTIONS = [
    "What is the current architecture and main components of this project?",
    "What recent changes have occurred and why?",
    "What external dependencies and integrations does this project have?",
    "What is the current operational status and any known issues?",
    "Provide a comprehensive summary of this project for someone unfamiliar with it.",
]
```

`integrations/notebooklm.py` keeps its 8 `FIXED_QUESTIONS` for NLM queries (NLM benefits from the extra questions like node location, troubleshooting). But only the 5 canonical questions are indexed to `nlm_notes` — the extra 3 NLM-specific answers are stored as supplementary notes with `"canonical": False`.

**Canonical payload schema** (all tiers produce this shape):

```python
{
    "text": str,               # The answer text
    "question": str,           # The question asked (from DISTILL_QUESTIONS)
    "source": str,             # "nlm" | "llm" | "direct" (renamed from from_nlm boolean)
    "distill_source": str,     # Same as source — explicit field for query.py routing
    "project": str,            # Project name
    "type": str,               # "qa" for NLM/LLM, "curated_doc" for direct
    "model": str | None,       # "notebooklm" | "minimax-m2.5" | None (direct)
    "canonical": bool,         # True for DISTILL_QUESTIONS answers, False for NLM extras
    "chunk_index": int,        # Point ID generation compatibility
}
```

**Migration note:** Existing `from_nlm: True` payloads in Qdrant must be migrated to `distill_source: "nlm"`. The migration script handles this.

### 2. `_fetch_existing_notes`

Retrieves existing NLM/LLM notes for a project from Qdrant. Used by `_distill_llm` to provide context for Tier 1 delta processing.

```python
def _fetch_existing_notes(project_name: str) -> list[dict]:
    """Fetch existing distilled notes from Qdrant nlm_notes collection.

    Returns list of {"question": str, "answer": str} dicts.
    Only returns canonical notes (canonical: True).
    Returns empty list if Qdrant is unreachable.
    """
    try:
        results = search(
            collection=COLLECTION_NLM_NOTES,
            query_filter={"project": project_name, "canonical": True},
            limit=10,
        )
        return [{"question": r.payload["question"], "answer": r.payload["text"]}
                for r in results if "question" in r.payload]
    except Exception as e:
        logger.warning(f"Failed to fetch existing notes for {project_name}: {e}")
        return []
```

### 3. Tier Auto-Assignment

Runs **after curation** because feature count comes from curator output. Each curated `.md` file in the output directory represents one feature group (the curator writes one file per feature).

```python
def compute_tier(project_name: str, curated_dir: str, registry_entry: dict) -> int:
    """Determine tier from feature count. Registry override wins.

    One curated .md file = one feature group (from curator's group_by_feature).
    """
    override = registry_entry.get("tier")
    if override is not None:
        return override

    # Each .md in curated_dir is one feature group from the curator
    feature_count = len(list(Path(curated_dir).glob("*.md")))

    if feature_count >= 5:
        return 1
    elif feature_count >= 3:
        return 2
    else:
        return 3
```

**Registry fields:**

```python
# New fields in _DEFAULT_ENTRY:
"tier": None,              # None = auto-assign, 1/2/3 = manual override
"computed_tier": 3,         # Last auto-computed tier (for observability)
"nlm_baseline": False,      # True after first successful NLM distillation
"last_distill": None,       # ISO timestamp of last distillation (any backend)
"last_nlm_distill": None,   # ISO timestamp of last NLM distillation specifically
```

Effective tier = `tier if tier is not None else computed_tier`.

### 4. Distill Dispatcher (`_distill_project`)

Replaces `_nlm_sync_project` in main.py:

```python
async def _distill_project(project_name: str, force_nlm: bool = False):
    entry = registry.get(project_name)
    curated_dir = os.path.join(CURATED_DIR, project_name)
    tier = entry.get("tier") or entry.get("computed_tier", 3)

    if force_nlm:
        # Operator override: always use NLM regardless of tier/baseline
        await _distill_nlm(project_name, curated_dir)
        return

    if tier == 1:
        if not entry.get("nlm_baseline"):
            # First time: attempt NLM, fall back to LLM if unavailable
            try:
                await _distill_nlm(project_name, curated_dir)
            except (NLMUnavailableError, CircuitBreakerOpen):
                logger.warning(f"NLM unavailable for {project_name} baseline, falling back to LLM")
                await _distill_llm(project_name, curated_dir, nlm_notes=None)
                # nlm_baseline stays False — will retry NLM next cycle
        else:
            existing_notes = _fetch_existing_notes(project_name)
            await _distill_llm(project_name, curated_dir, nlm_notes=existing_notes)
    elif tier == 2:
        await _distill_llm(project_name, curated_dir, nlm_notes=None)
    else:
        await _distill_direct(project_name, curated_dir)
```

### 5. `/curate/{project}` Endpoint Refactor

The current endpoint runs NLM unconditionally. After tiers, it calls the dispatcher:

```python
@app.post("/curate/{project}")
async def curate_endpoint(project: str, force_nlm: bool = Query(False)):
    # ... existing scan + curate logic unchanged ...

    # Tier assignment (after curation)
    computed = compute_tier(project, curated_dir, entry)
    registry.update_fields(project, computed_tier=computed)

    # Distill (replaces direct nlm.run_nlm_cycle call)
    await _distill_project(project, force_nlm=force_nlm)

    # ... existing response logic ...
```

`force_nlm=true` requires `NLM_ENABLED=true` and circuit breaker open. Returns 503 otherwise.

### 7. `_distill_nlm` (Existing NLM Flow)

Wraps current `_nlm_sync_project` logic:
- Calls `nlm.run_nlm_cycle(notebook_id, curated_dir, project_name=project_name)`
- On success: indexes notes to `nlm_notes`, sets `nlm_baseline: True`, updates `last_nlm_distill`
- On failure: circuit breaker handles it. Falls back to `_distill_llm` with no NLM context.
- Payload includes `"source": "nlm"` in metadata

### 8. `_distill_llm` (New — LLM Distillation)

Uses LiteLLM to call minimax-m2.5 (free via LiteLLM gateway on Kubo).

**Prompt construction:**

```
System: You are a knowledge distillation agent for the {project_name} project.
Your task is to analyze the project documentation and produce structured knowledge notes.
Answer each question based ONLY on the provided documentation.
All output must be in English.

{if nlm_notes:}
## Existing Project Understanding (from prior deep analysis)
{for note in nlm_notes:}
Q: {note.question}
A: {note.answer}
{endfor}

## New/Updated Documentation
{endif}

{for file in curated_files:}
### {file.name}
{file.content}
{endfor}

Answer each of the following questions. If the documentation doesn't contain enough
information to answer, say "Insufficient documentation."

1. What is the current architecture and main components of this project?
2. What recent changes have occurred and why?
3. What external dependencies and integrations does this project have?
4. What is the current operational status and any known issues?
5. Provide a comprehensive summary of this project for someone unfamiliar with it.
```

**Response parsing:** Each answer is extracted and indexed as a separate note in `nlm_notes` with:
- Same payload shape as NLM notes
- `"source": "llm"` instead of `"source": "nlm"`
- `"model": "minimax-m2.5"` for traceability

**LiteLLM call:**
```python
import litellm

DISTILL_LLM_MODEL = os.getenv("DISTILL_LLM_MODEL", "minimax-m2.5")
DISTILL_LLM_TIMEOUT = int(os.getenv("DISTILL_LLM_TIMEOUT", "120"))
DISTILL_LLM_MAX_TOKENS = int(os.getenv("DISTILL_LLM_MAX_TOKENS", "4000"))
LITELLM_URL = os.getenv("LITELLM_URL", "http://localhost:4000")

response = await litellm.acompletion(
    model=DISTILL_LLM_MODEL,
    messages=[{"role": "system", "content": system_prompt},
              {"role": "user", "content": questions_prompt}],
    api_base=LITELLM_URL,
    timeout=DISTILL_LLM_TIMEOUT,
    max_tokens=DISTILL_LLM_MAX_TOKENS,
)
```

**LLM error handling:**
- Timeout (120s default): log warning, skip project, retry next cycle
- Empty/malformed response: validate that response contains numbered answers matching DISTILL_QUESTIONS. If parsing fails, discard and log `distill_llm_parse_error`.
- LiteLLM connection error: log, skip, retry next cycle. Do NOT use `_is_nlm_error` patterns (those are NLM-specific). Instead, validate that each parsed answer has `len(answer) > 20` (reject trivially short responses).

### 9. `_distill_direct` (New — Direct Indexing)

Simplest path. No LLM call.

- Reads each curated `.md` file from `curated_dir`
- Embeds: `embed_text(file_content[:2000])` (nomic-embed-text handles ~8192 tokens; 2000 chars is safe and captures meaningful content)
- Indexes to `nlm_notes` with canonical payload schema:
  - `"text"`: full file content
  - `"question"`: `"Project documentation: {feature_name}"`
  - `"distill_source"`: `"direct"`
  - `"source"`: `"direct"`
  - `"type"`: `"curated_doc"`
  - `"project"`: project name
  - `"model"`: `None`
  - `"canonical"`: `True`
  - `"chunk_index"`: sequential per feature

### 10. Cord-Cutting & NLM Re-engagement

**Tier 1 lifecycle:**

```
Registration → curate → Tier 1 assigned (>=5 features)
                              │
                   ┌──────────▼──────────┐
                   │  nlm_baseline: false │
                   │  → _distill_nlm()    │
                   └──────────┬──────────┘
                              │ success
                   ┌──────────▼──────────┐
                   │  nlm_baseline: true  │
                   │  subsequent deltas:  │
                   │  → _distill_llm()    │
                   │    (with NLM notes)  │
                   └──────────┬──────────┘
                              │ POST /curate/{project}?force_nlm=true
                   ┌──────────▼──────────┐
                   │  Full NLM re-sync    │
                   │  replaces all notes  │
                   │  nlm_baseline stays  │
                   └──────────────────────┘
```

**The distill loop NEVER auto-triggers NLM after baseline.** Only `?force_nlm=true` does. This is the "cord-cutting" mechanism.

**`?force_nlm=true` behavior:**
- Bypasses tier check — always uses NLM
- Replaces existing notes for the project (deletes old, indexes new)
- Updates `last_nlm_distill`
- Requires `NLM_ENABLED=true` and circuit breaker open

### 11. Consumer Feedback Loop

**Endpoint:** `POST /feedback`

```json
{
  "project": "jasper",
  "query": "what is jasper's memory architecture?",
  "useful": true,
  "coherent": true,
  "logical": true
}
```

Three booleans. No scoring scales, no free text. Dead simple for consuming agents to report.

**Storage:** Append-only JSONL at `data/feedback.jsonl`. Each entry timestamped automatically.

```json
{"timestamp": "2026-03-24T10:00:00Z", "project": "jasper", "query": "...", "useful": true, "coherent": true, "logical": true}
```

**Metrics integration:** `GET /metrics` adds per-project feedback stats:

```json
{
  "feedback": {
    "jasper": {"total": 42, "useful_pct": 95.2, "coherent_pct": 97.6, "logical_pct": 92.8, "needs_review": false},
    "mesh-monitor": {"total": 5, "useful_pct": 60.0, "coherent_pct": 80.0, "logical_pct": 60.0, "needs_review": true}
  }
}
```

**Alert threshold:** If any project drops below `FEEDBACK_THRESHOLD_PCT` (env var, default `70`) on any metric over the last `FEEDBACK_WINDOW_SIZE` (env var, default `20`) queries → `"needs_review": true`. This signals the operator to investigate or trigger `?force_nlm=true`.

**No automation:** Feedback informs the operator. It does NOT auto-trigger NLM re-sync or tier changes.

---

## Retroactive Application

### Cron Queue Cleanup

The daily 5:00 AM cron (`cleanup-and-recurate.sh`) currently queues all 12 pending projects for NLM sync. After implementing tiers:

1. Run `compute_tier()` for all 13 registered projects based on existing curated files
2. Only Tier 1 projects remain in the NLM queue
3. Tier 2 projects get distilled via minimax immediately (no NLM wait)
4. Tier 3 projects get direct-indexed immediately

**Expected result:** The NLM queue shrinks from 12 projects to ~3-4 (jasper, erudito, infra-mcp, and potentially keystone/labforge). The remaining 8-9 projects are processed instantly via LLM or direct indexing.

### Migration Script

`scripts/migrate-tiers.py` — one-time script:
1. **Backup:** Copy `data/registry.yaml` to `data/registry.yaml.pre-tiers` before any changes
2. For each registered project, compute tier from existing `data/curated/{project}/` feature count
3. Update registry with `computed_tier` fields (no manual `tier` overrides — let auto-assign work first)
4. For Tier 2/3 projects: run distillation immediately
5. For Tier 1 projects that already have NLM notes: set `nlm_baseline: True`
6. Migrate existing Qdrant `nlm_notes` payloads: add `distill_source: "nlm"` field to entries with `from_nlm: True`
7. Update cron script to respect tiers

**Rollback:** Restore `data/registry.yaml.pre-tiers` and restart. Qdrant payload migration is additive (adds field, doesn't remove `from_nlm`), so no Qdrant rollback needed.

---

## KPIs

### Existing KPIs (unchanged)

| ID | Name | Status |
|----|------|--------|
| eru-01 through eru-10 | (all existing) | unchanged |
| eru-11 | Curation coverage | unchanged |
| eru-12 | Curation freshness | unchanged |

### Modified KPIs

| ID | Name | Change |
|----|------|--------|
| eru-13 | Coherence drift | Replaced by consumer feedback metrics (eru-16, eru-17). Self-assessment coherence check deferred — remains in backlog as nice-to-have for eval-agent. |

### New KPIs

| ID | Name | Type | Assertion | Severity |
|----|------|------|-----------|----------|
| eru-14 | Distillation coverage | metric | All curated projects have `last_distill` set | high |
| eru-15 | NLM budget efficiency | metric | Only Tier 1 projects consume NLM quota | medium |
| eru-16 | Consumer satisfaction | metric | Per-project useful_pct >= 70% over last 20 queries | high |
| eru-17 | Feedback volume | metric | At least 1 feedback entry per project per week | low |

---

## Testing Strategy

### New Tests

| File | Category | Test Cases |
|------|----------|------------|
| `tests/test_distiller.py` | Tier computation | Feature count thresholds (1-2→T3, 3-4→T2, 5+→T1), manual override, empty curated dir |
| `tests/test_distiller.py` | LLM distillation | Prompt construction with/without NLM notes, response parsing, error handling, payload shape matches NLM |
| `tests/test_distiller.py` | Direct distillation | Curated doc embedding, payload shape, feature extraction |
| `tests/test_main_tiers.py` | Dispatcher | Tier routing (T1 no baseline→NLM, T1 baseline→LLM, T2→LLM, T3→direct), force_nlm override |
| `tests/test_feedback.py` | Feedback endpoint | POST validation, JSONL append, metrics aggregation, needs_review threshold |

### Existing Tests (must not break)

All 128 existing tests must continue passing. The tier system is additive — existing NLM flow is wrapped, not replaced.

---

## Prerequisites

- **Fix curator Spanish section titles** (backlog item): `curator.py` DOC_TYPES uses Spanish titles ("Diseno", "Implementacion"). These curated docs are fed directly to the LLM prompt. Fix to English ("Design", "Implementation") before or alongside tier implementation.

---

## Error Handling

| Scenario | Behavior |
|----------|----------|
| NLM unavailable for Tier 1 baseline | Falls back to `_distill_llm` with no NLM context. Sets `nlm_baseline: False`. Retries NLM next cycle. |
| LiteLLM/minimax unavailable | Project stays with `last_distill` unchanged. Retry next cycle. Log error. |
| LLM returns garbage | Apply same `_is_nlm_error` pattern check. Discard, don't index. |
| Tier changes (project gains features) | `computed_tier` updates automatically after next curation. If tier goes from 3→1, NLM baseline process starts. |
| force_nlm with circuit breaker tripped | Return 503 with message. Don't queue. |

---

## File Structure (additions)

```
erudito/
├── core/
│   ├── distiller.py           # NEW — compute_tier, LLM prompt, FIXED_QUESTIONS
│   └── (existing modules unchanged)
├── data/
│   ├── feedback.jsonl          # NEW — consumer feedback log
│   └── (existing files)
├── scripts/
│   ├── migrate-tiers.py        # NEW — one-time tier migration
│   └── (existing scripts)
├── tests/
│   ├── test_distiller.py       # NEW — tier computation + LLM distillation
│   ├── test_feedback.py        # NEW — feedback endpoint + metrics
│   └── test_main_tiers.py      # NEW — dispatcher routing tests
└── (existing files)
```
