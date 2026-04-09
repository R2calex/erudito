# Erudito v3 — Data Curation Pipeline Design

**Date:** 2026-03-20
**Status:** Approved (brainstorming complete)
**Author:** r0calex + Claude Opus 4.6
**Depends on:** docs/superpowers/specs/2026-03-18-erudito-v3-design.md

## Overview

The current Erudito v3 implementation has an inverted data flow: raw .md file chunks go directly to Qdrant, with NotebookLM as an optional side process. This produces low-quality search results (raw code fragments, config snippets) and defeats the purpose of having NLM as a knowledge validation tool.

This design introduces a **Curator module** that consolidates and homogenizes files before they reach NotebookLM, and corrects the data flow so that Qdrant stores NLM-processed knowledge (not raw chunks).

### Problem Statement

1. Search returns raw chunks with no semantic understanding
2. Files go to NLM without format standardization or metadata
3. No curation tracking — no way to know what's been processed vs pending
4. Coherence score is hardcoded (85.0), not actually measured
5. NLM's 50-source limit requires file consolidation

### Design Principles

- **NLM is an oracle, not the RAG.** Erudito asks NLM specific questions and stores the answers. NLM generates ground truth; Qdrant serves it. If NLM is down, Erudito continues from Qdrant.
- **NLM does not auto-summarize.** You must ask it questions — it does not generate knowledge unprompted.
- **NLM does not cross notebooks.** Cross-project queries are Erudito's responsibility via Qdrant.
- **Curated files are inspectable.** They live on disk at `data/curated/` for debugging and auditing.
- **Rollout is incremental.** One project at a time as pilot, then expand 2 at a time max.

---

## Architecture

### Corrected Data Flow

```
1. Scanner → detects git hash deltas, lists changed .md files
2. Curator → groups by feature/topic, consolidates with standardized metadata
           → writes to data/curated/{project}/{feature-slug}.md
           → marks project as "curated" in registry (curation_status field)
3. NLM Sync Loop (_nlm_sync_loop, separate from _scan_loop):
   - Runs every SCAN_INTERVAL (same as scan loop)
   - Queries registry for projects with curation_status == "curated" that need NLM sync
   - Processes max 2 projects concurrently (asyncio.Semaphore(2))
   - For each project:
     a. Uploads curated files as sources to project's notebook
     b. Asks specific questions (fixed + dynamic)
     c. Asks NLM to save answers in its internal notes
     d. Erudito stores those answers in Qdrant (nlm_notes collection)
     e. Updates registry: status → "synced", curation_status stays "curated"
4. Coherence Check (periodic, triggered by /metrics/coherence endpoint or eval-agent):
   - Re-asks the same fixed questions to Qdrant (RAG)
   - Uses LLM (via LiteLLM) to synthesize Qdrant search results into an answer
   - Compares synthesized answer vs NLM ground truth (embedding similarity)
   - Significant divergence = data contamination alert
```

### Two Background Loops

```
_scan_loop (existing, every SCAN_INTERVAL):
  for each project in registry:
    scanner.compute_delta() → curator.curate() → mark curation_status="curated"

_nlm_sync_loop (NEW, every SCAN_INTERVAL, offset by 5min):
  for each project where curation_status=="curated" and needs NLM sync:
    (max 2 concurrent via asyncio.Semaphore(2))
    notebooklm.run_nlm_cycle(curated_files) → index answers to Qdrant
```

The scan semaphore (`MAX_CONCURRENT_SCANS=3`) controls scan concurrency. The NLM semaphore (`MAX_CONCURRENT_NLM=2`) controls NLM concurrency independently. They do not interact.

### What Changes vs Current Implementation

| Module | Change |
|--------|--------|
| `main.py` | `_scan_project()` replaces `_run_indexer()` call with `curator.curate_project()`. `_run_nlm_cycle()` is removed from `_scan_project()` and moved to `_nlm_sync_loop()` (new background loop). Adds `_nlm_sync_loop()` function and starts it in lifespan |
| `core/curator.py` | **New module.** Groups files by feature, consolidates, writes to `data/curated/` |
| `core/indexer.py` | `index_delta()` no longer called during scan. `upsert_points()` retained for storing NLM answers. See Fallback section for when `index_delta()` is used |
| `core/scanner.py` | No changes — continues detecting deltas |
| `integrations/notebooklm.py` | `run_nlm_cycle()` signature changes: receives `curated_dir: str` (path to `data/curated/{project}/`) instead of `delta: dict`. Reads curated .md files from that directory. Adds logic for requesting NLM to save notes |
| `core/registry.py` | Adds `curation_status`, `curated_at`, `curated_files` fields to `_DEFAULT_ENTRY` (defaults: `"uncurated"`, `null`, `0`). No new status values for the main `status` field — it keeps existing values: `pending`, `synced`, `validated`, `stale` |

### Fallback: `agent_knowledge` Collection

The `agent_knowledge` Qdrant collection is retained as fallback. When NLM is unavailable (detected by `_nlm_sync_loop` failing 3 consecutive times for a project), Erudito automatically indexes the **curated documents** (not raw files) into `agent_knowledge` using `index_delta()`. This ensures search always has answers, even in degraded mode.

Fallback trigger: `nlm_consecutive_failures >= 3` per project (tracked in registry).

When NLM becomes available again and successfully syncs, the `agent_knowledge` entries for that project are replaced with NLM-validated knowledge.

---

## Component Design

### 1. Curator Module (`core/curator.py`)

**Responsibility:** Take .md files detected by scanner, group by feature/topic, generate consolidated documents with standardized metadata.

**Feature Grouping Algorithm:**

Files follow the naming convention: `{TYPE}-{topic}.md` or `{TYPE}-{date}-{topic}.md`

```
SPEC-KEYSTONE-ARCHITECTURE.md     → feature: "keystone"
IR-2026-03-11-keystone-activation.md → feature: "keystone"
SOP-KEYSTONE-OPERATIONS.md        → feature: "keystone"
```

Extraction steps (function `extract_feature_name(filename) -> str`):
1. Strip file extension (`.md`)
2. Strip known type prefix via regex: `^(SPEC|IR|SOP|PLAN|CONTRACT|REPORT|REVIEW)-`
3. Strip date pattern via regex: `^(\d{4}-\d{2}-\d{2}-|\d{8}-)`
4. Normalize to lowercase, strip leading/trailing hyphens
5. Extract root feature: split by `-`, take segments while they form a known feature group. Specifically:
   - Build a frequency map of first-N-segments across all files in the project
   - The longest prefix shared by 2+ files is the feature name
   - Example: `mesh-monitor-health-fix` and `mesh-monitor` → feature = `mesh-monitor`
   - Example: `keystone-architecture` and `keystone-operations` → feature = `keystone`
   - Single-occurrence prefixes: the full normalized name is the feature (1:1 curated doc)
6. Orphan files (no shared feature with any other file) become their own 1:1 curated document

**Edge case examples:**

| Filename | After strip | Feature |
|----------|------------|---------|
| `SPEC-KEYSTONE-ARCHITECTURE.md` | `keystone-architecture` | `keystone` |
| `IR-2026-03-15-mesh-monitor-health-fix.md` | `mesh-monitor-health-fix` | `mesh-monitor` |
| `SOP-LITELLM-MODEL-SYNC.md` | `litellm-model-sync` | `litellm` (if `litellm-prompt-management` also exists) |
| `CLAUDE.md` | `claude` | `claude` (orphan, 1:1) |

**Consolidated Document Format:**

```yaml
---
project: claude-contracts
feature: keystone
sources: 4
source_files:
  - SPEC-KEYSTONE-ARCHITECTURE.md
  - IR-2026-03-11-keystone-activation.md
  - IR-2026-03-12-keystone-v2.3-reconciliation.md
  - SOP-KEYSTONE-OPERATIONS.md
curated_at: 2026-03-20T10:00:00Z
last_source_modified: 2026-03-12
status: curated
---

# Feature: Keystone

## Diseño
> Source: SPEC-KEYSTONE-ARCHITECTURE.md (2026-03-11)

[sanitized content of SPEC]

## Implementación
> Source: IR-2026-03-11-keystone-activation.md

[sanitized content of IR]

## Implementación
> Source: IR-2026-03-12-keystone-v2.3-reconciliation.md

[sanitized content of IR]

## Operación
> Source: SOP-KEYSTONE-OPERATIONS.md

[sanitized content of SOP]
```

**Section ordering within consolidated doc:** Design → Plan → Implementation (chronological) → Operation. This gives NLM the temporal narrative of the feature.

**Type-to-section mapping:**

| File prefix | Section title | Sort order |
|-------------|--------------|------------|
| SPEC- | Diseño | 1 |
| PLAN- | Plan | 2 |
| IR- | Implementación | 3 (sorted by date) |
| SOP- | Operación | 4 |
| CONTRACT- | Contrato | 5 |
| REPORT- | Reporte | 6 |
| REVIEW- | Revisión | 7 |
| (other) | Documentación | 8 |

**Key functions:**

- `curate_project(project_name, project_path, delta_files) -> CurationResult` — main entry point
- `group_by_feature(files) -> dict[str, list[FileInfo]]` — feature extraction and grouping
- `consolidate_feature(feature, files) -> str` — generates the consolidated markdown
- `extract_feature_name(filename) -> str` — strips type prefix, date, normalizes

**Output:** Files written to `data/curated/{project}/{feature-slug}.md`

**Dimensioning:** `claude-contracts` (114 files) → ~35 unique topics → ~35 curated files. Within NLM's 50-source limit.

### 2. Registry Changes (`core/registry.py`)

**Status field (unchanged):** Keeps existing lifecycle from parent spec:

```
pending → synced → validated
   ↑         ↓
   └── stale ←┘  (new delta detected)
```

- `pending`: Registered, never scanned
- `synced`: Scanner ran, files indexed (via NLM or fallback)
- `validated`: NLM ground truth stored, coherence verified
- `stale`: New delta detected since last sync

**New `curation_status` field (independent from `status`):**

```
uncurated → curated → stale (new delta) → curated (re-curated)
              ↓
           error (curation failed)
```

**New fields added to `_DEFAULT_ENTRY` in `registry.py`:**

```python
# Add to _DEFAULT_ENTRY dict:
"curation_status": "uncurated",   # uncurated | curated | stale | error
"curated_at": None,               # ISO timestamp of last curation
"curated_files": 0,               # number of consolidated docs in data/curated/{project}/
"nlm_consecutive_failures": 0,    # for fallback trigger (>= 3 → use agent_knowledge)
```

### 3. NLM Cycle Changes (`integrations/notebooklm.py`)

**Current signature:** `run_nlm_cycle(notebook_id, delta) -> dict`
**New signature:** `run_nlm_cycle(notebook_id, curated_dir: str) -> dict`

Where `curated_dir` is the path to `data/curated/{project}/`. The function reads all `.md` files in that directory and uploads them as sources.

**NLM interaction protocol:**
1. List curated `.md` files in `curated_dir`
2. Upload each curated doc as a source to the project notebook (via `add_source()`)
3. Ask fixed questions (already defined in code):
   - What changes have occurred since the last session?
   - Were there any design changes relative to the original plan?
   - What new resources have been registered for this project?
   - On which AI-Lab node is this project running?
   - What is the current project status according to documentation?
4. Ask dynamic questions (generated from curated file metadata — e.g., "What is the status of feature X?" for each feature found in the curated docs)
5. Request NLM to save answers in its internal notes
6. Return answers as `dict` with `success`, `notes` (list of `{question, answer}`), `sources_uploaded`

**Concurrency:** Max 2 projects syncing with NLM simultaneously (hard limit via `asyncio.Semaphore(2)` in `_nlm_sync_loop`).

### 4. Coherence Check

**Purpose:** Detect RAG data contamination by comparing Qdrant answers against NLM ground truth.

**Ownership:** The coherence check is **triggered externally** (by eval-agent via `/metrics/coherence` endpoint, or manually). Erudito computes and caches the score but does not run it on a loop.

**Algorithm:**
1. For each project with `status == "synced"` or `"validated"`:
   a. Retrieve NLM ground truth answers from Qdrant `nlm_notes` (filter: `from_nlm: True`, `project: {name}`)
   b. For each fixed question that has a ground truth answer:
      - Run the same question through Qdrant search (`agent_knowledge` + `nlm_notes`)
      - Use LLM (via LiteLLM, local model preferred per cost policy) to synthesize search results into a single answer text
      - Compute embedding similarity between synthesized answer and NLM ground truth
   c. Average similarity across all questions = project coherence score
2. Global coherence score = average across all projects
3. If any project score < 0.8 → flag that project as potentially contaminated

**Why LLM synthesis is needed:** Qdrant search returns ranked document chunks, not a direct answer. To compare against NLM's answer (which is natural language), we need to synthesize the chunks into a comparable answer first.

This replaces the current hardcoded `coherence_last_score: 85.0`.

### 5. Curated File Lifecycle

**Regeneration policy:** Curated files are **fully regenerated** (not incrementally updated) whenever the scanner detects a delta for the project. This is simpler and avoids stale section problems.

**Cleanup:** When a curated file's source files are all deleted from the repo (detected during scan as files that no longer exist), the corresponding curated file is deleted from `data/curated/{project}/`. The audit log records this as `curation_cleanup`.

**Disk management:** Curated files are text-only markdown. For the largest project (claude-contracts, 114 files → ~35 curated docs), estimated size is < 5MB. No special disk management needed.

---

## KPIs

### Existing KPIs (unchanged)

| ID | Name | Status |
|----|------|--------|
| eru-01 | Health responds with v3 format | PASS |
| eru-02 | Qdrant connected | PASS |
| eru-03 | Registry has projects | PASS |
| eru-05 | Search returns results | PASS |
| eru-09 | NLM health above 50% | PASS |

### Modified KPIs

| ID | Name | Change |
|----|------|--------|
| eru-04 | Coverage above 50% | Remains, measures synced/validated projects |
| eru-10 | Scan freshness under 30min | Remains, needs working scan loop |

### New KPIs

| ID | Name | Type | Assertion | Severity |
|----|------|------|-----------|----------|
| eru-11 | Curation coverage | metric | `$.coverage.curation_pct >= 90` | high |
| eru-12 | Curation freshness | metric | `$.coverage.curation_pending == 0` | medium |
| eru-13 | Coherence drift | probe | `$.coherence_last_score >= 0.8` (real, not hardcoded) | high |

**eru-13 serves as the control metric:** If NLM answers and Qdrant answers diverge, the RAG data has been contaminated. This is the primary quality signal.

**Metrics endpoint changes:** `/metrics` response adds curation fields:

```json
{
  "coverage": {
    "total": 11,
    "validated": 0,
    "synced": 4,
    "stale": 0,
    "pending": 7,
    "coverage_pct": 0.0,
    "coverage_partial_pct": 36.4,
    "curation_pct": 72.7,
    "curation_pending": 3
  },
  "coherence_last_score": 0.85,
  ...
}
```

`curation_pct` = projects with `curation_status == "curated"` / total projects * 100.
`curation_pending` = projects with `curation_status` in (`"uncurated"`, `"stale"`).

---

## Rollout Plan

### Pilot Project: `erudito`

Small project (2 .md files in last delta), well-understood codebase.

**Phases:**

1. Implement `core/curator.py` with unit tests
2. Run curation for `erudito` only → inspect `data/curated/erudito/`
3. Connect with NLM for that project only → verify source upload, questions, notes
4. Verify answers reach Qdrant `nlm_notes`
5. Run manual coherence check
6. If all OK → enable next project (max 2 at a time)
7. Monitor KPIs eru-11, eru-12, eru-13

### Error Handling

| Scenario | Behavior |
|----------|----------|
| Curation fails | Project stays in `curation_error`, not sent to NLM, logged in audit |
| NLM unavailable | Project stays in `needs_nlm_sync`, retry next cycle. Erudito serves from `agent_knowledge` fallback |
| NLM returns empty/garbage | Discarded, not indexed, logged as `nlm_quality_error` |
| NLM rate limited | Backoff, retry next cycle. Max 2 concurrent enforced |
| Source exceeds 50-file NLM limit | Curator should consolidate more aggressively (broader grouping). Log warning |

---

## Testing Strategy

### `tests/test_curator.py` — Unit tests (no network)

| Category | Test cases |
|----------|-----------|
| Feature extraction | Strip SPEC/IR/SOP prefix, strip dates, normalize case, edge cases (no prefix, unknown prefix, date-only names) |
| Feature grouping | Multiple files same feature, orphan files get 1:1, frequency-based prefix detection |
| Consolidation | Section ordering (SPEC before IR before SOP), chronological IR sorting, frontmatter generation, source attribution |
| Full curation | `curate_project()` with mock file system, output file creation in `data/curated/`, CurationResult fields |
| Edge cases | Empty delta, single file, all same type, file with no content |

### `tests/test_notebooklm.py` — Updates

Add tests for new `run_nlm_cycle(notebook_id, curated_dir)` signature: mock file reads from curated directory, verify sources uploaded match curated files.

### Integration test (pilot)

Manual: run `curator.curate_project("erudito", ...)`, inspect output in `data/curated/erudito/`, then trigger NLM sync and verify `nlm_notes` in Qdrant.

---

## File Structure (additions)

```
erudito/
├── core/
│   ├── curator.py              # NEW — feature grouping + consolidation
│   └── (existing modules unchanged)
├── data/
│   ├── curated/                # NEW — curated output directory
│   │   └── {project}/
│   │       └── {feature}.md
│   └── (existing files)
├── tests/
│   └── test_curator.py         # NEW — curator unit tests
└── (existing files)
```
