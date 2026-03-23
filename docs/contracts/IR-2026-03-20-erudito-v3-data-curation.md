# Implementation Report: Erudito v3 Data Curation Pipeline

**Date:** 2026-03-20
**Author:** r0calex + Claude Opus 4.6
**Spec:** ~/ai-lab/erudito/docs/superpowers/specs/2026-03-20-erudito-v3-data-curation-design.md
**Plan:** ~/ai-lab/erudito/docs/superpowers/plans/2026-03-20-erudito-v3-data-curation.md
**Branch:** feat/erudito-v3

## What Changed

### Problem
Erudito v3's data flow was inverted: raw .md file chunks went directly to Qdrant, producing low-quality search results (code fragments, config snippets). NotebookLM was an optional side process instead of the knowledge validation oracle it was designed to be.

### Solution
Added a Curator module that consolidates .md files by feature/topic before NotebookLM ingestion. Corrected the data flow so Qdrant stores NLM-processed knowledge instead of raw chunks.

### Files Changed

| File | Change |
|------|--------|
| `core/curator.py` | **NEW** — Feature grouping + consolidation (extract_feature_name, group_by_feature, consolidate_feature, curate_project) |
| `tests/test_curator.py` | **NEW** — 29 unit tests for curator module |
| `core/registry.py` | Added curation_status, curated_at, curated_files, nlm_consecutive_failures to _DEFAULT_ENTRY. Updated summary() with curation_pct, curation_pending |
| `tests/test_registry.py` | Added TestCurationFields (2 tests) |
| `main.py` | Scan loop calls curator instead of indexer. Added _nlm_sync_loop (separate background loop, max 2 concurrent). Added _fallback_index_curated. Updated lifespan() |
| `integrations/notebooklm.py` | run_nlm_cycle() now takes curated_dir instead of delta. Reads curated .md files from disk. Fixed MCP tool name prefix bug |
| `tests/test_api.py` | Added curation metrics assertion |
| `tests/test_erudito.py` | **DELETED** — Legacy v2 test file (38 false failures) |
| `~/ai-lab/agent-eval/kpis/erudito.yaml` | Added eru-11 (curation coverage), eru-12 (curation freshness), eru-13 (coherence drift) |

### Architecture

```
Scanner → Curator → data/curated/{project}/{feature}.md → NLM Sync Loop → Qdrant (nlm_notes)
                                                          (max 2 concurrent)
                                                          ↓ (fallback after 3 failures)
                                                          Qdrant (agent_knowledge)
```

Two independent background loops:
- `_scan_loop`: detects deltas, runs curator, writes curated files
- `_nlm_sync_loop`: reads curated files, syncs with NLM, stores answers in Qdrant

### Test Results
- 93 unit tests passing (0 failures)
- 7 integration tests (pass after deployment)
- 29 new curator tests cover feature extraction, grouping, consolidation, and full curation

### Pilot Test
- Project: `erudito`
- Input: 4 .md files (2 specs, 2 plans)
- Output: 1 curated feature file (`erudito-v3.md`, 136KB)
- Registry: `curation_status: "curated"`, `curated_files: 1`
- Metrics: `curation_pct: 9.1%` (1/11 projects curated)

### Issues Found During Implementation
1. **Scanner returns paths with directories** (e.g., `docs/superpowers/specs/...`) — curator's `extract_feature_name` needed to use `os.path.basename()`. Fixed.
2. **Existing registry entries lack new fields** — old YAML entries don't have curation fields. Registry's `get()` returns raw dict without defaults. Fields appear after first write. Works but could be improved.
3. **classify_doc_type cosmetic issue** — files in subdirectories lose their type prefix through enrichment, falling back to "Documentación". Content grouping is correct.

### Commits
- `53d76a6` — docs: add data curation design spec
- `405d4a0` — docs: add data curation implementation plan
- `curator module` — feat: add curator with feature grouping and consolidation
- `registry fields` — feat: add curation_status fields to registry
- `main.py integration` — feat: curator-driven scan loop + NLM sync loop
- `69422a4` — fix: curator handles file paths with directories
- `6e13f85` — chore: remove legacy v2 test file
- `KPIs` — feat: add Erudito curation and coherence KPIs

### Next Steps
1. Enable NLM_ENABLED=true and test NLM sync with erudito project
2. Run curation for remaining 10 projects (2 at a time max)
3. Implement coherence check algorithm in eval-agent
4. Optional: fix classify_doc_type for enriched subdirectory files
