# Implementation Report: Erudito v3 Curation Pipeline — Full Implementation

**Date:** 2026-03-22
**Author:** r0calex + Claude Opus 4.6
**Branch:** feat/erudito-v3
**Session:** Marathon session — design, implementation, testing, deployment, validation

## Summary

Complete implementation of the Erudito v3 data curation pipeline, transforming Erudito from a raw-chunk RAG into a curated knowledge orchestrator validated by NotebookLM.

## What Was Built

### Core Pipeline
1. **Curator module** (`core/curator.py`): Groups .md files by feature using frequency-based prefix detection with directory fallback. Consolidates into standardized documents with YAML frontmatter.
2. **Classifier module** (`core/classifier.py`): Routes documentation from inbox (claude_contracts) to project repos via keyword matching. 76 files redistributed across 9 projects.
3. **NLM sync loop**: Separate background loop (max 2 concurrent) syncs curated files to NotebookLM, asks questions, stores answers in Qdrant.
4. **Query engine rewrite** (`core/query.py`): Registry-first routing for exact data, dual-source mode (Qdrant + NLM live), nlm_notes-only search (raw chunks purged).

### Endpoints Added
- `POST /curate/{project}` — Full pipeline in one call
- `POST /ingest/{project}` — Accept files from remote nodes
- `POST /classify` — Sort inbox files to projects
- `GET /search?mode=dual` — Dual-source response (Qdrant + NLM)
- `GET /nlm/status` — Circuit breaker status
- `POST /nlm/reset` — Manual circuit breaker reset

### Safety Features
- NLM circuit breaker: Stops calls after RESOURCE_EXHAUSTED, auto-resets after 6h
- Error contamination guardrail: Never indexes NLM error responses as knowledge
- Idempotent NLM operations: ensure_notebook, sync_sources (content-aware), note deduplication
- Content change detection via hash comparison for source updates

### Remote Node Support
- DevOps agents on Kubo/Sariatu extended with /files and /files/content endpoints
- Erudito scan loop pulls docs from satellites via HTTP
- push-docs.sh script for manual push from satellite nodes

### Validation Results
- 13 projects curated and validated via NLM
- Search scores: 0.83 — 0.92 (up from 0.50 — 0.75)
- 128 unit tests passing
- Pipeline tested end-to-end with manual NLM verification

## Key Decisions
- NotebookLM is oracle/consultant, not the RAG. Erudito continues from Qdrant if NLM is down.
- All knowledge in English only (embedding model is language-sensitive).
- One notebook per project in NLM. No cross-notebook summary (deferred).
- agent_knowledge collection purged. nlm_notes is sole source of truth.
- Registry-first routing for exact data queries (path, node, status, notebook_id).

## Known Issues (in BACKLOG.md)
- 13 direct registry mutations need thread-safe refactor (Critical)
- 12 of 13 projects need re-curate (NLM rate limit exhausted both accounts)
- Spanish section titles in curator (should be English)
- No API authentication on write endpoints

## Files Changed
| File | Lines | Change |
|------|-------|--------|
| main.py | ~950 | Major rewrite: curator integration, NLM sync loop, /curate, /ingest, /classify, circuit breaker |
| core/curator.py | ~200 | NEW: feature grouping + consolidation |
| core/classifier.py | ~170 | NEW: inbox file routing |
| core/query.py | ~200 | Rewritten: registry-first, dual-source, nlm_notes-only |
| core/registry.py | ~230 | Added curation fields, update_fields(), register_remote() |
| integrations/notebooklm.py | ~450 | Major rewrite: idempotent ops, circuit breaker, curated_dir |
| tests/ | 128 tests | test_curator(29), test_classifier(26), test_query(15), + updates |
| docker-compose.yaml | | NLM_ENABLED, node config, Redis secret to .env |
| docs/BACKLOG.md | | NEW: prioritized issue tracker |
| scripts/ | | NEW: push-docs.sh, cleanup-and-recurate.sh |
