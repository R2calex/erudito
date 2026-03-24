# Implementation Report: Erudito v3 Knowledge Tier System

**Date:** 2026-03-24
**Author:** r0calex + Claude Opus 4.6
**Branch:** feat/erudito-v3
**Spec:** docs/superpowers/specs/2026-03-24-erudito-v3-knowledge-tiers-design.md

## Summary

Added a 3-tier knowledge distillation system to optimize NLM budget. Complex projects (>=5 features) use NLM for initial deep analysis then LLM for deltas. Medium projects (3-4 features) use LLM only. Simple projects (1-2 features) embed curated docs directly. Consumer feedback endpoint enables drift detection from real usage.

## What Was Built

### Core Module: `core/distiller.py` (NEW)
- `DISTILL_QUESTIONS` — 5 canonical questions, single source of truth for all tiers
- `compute_tier()` — auto-assigns tier from curator feature count, registry override wins
- `build_llm_prompt()` — constructs system+user prompts, optionally includes NLM notes as context
- `parse_llm_response()` — parses numbered LLM responses into canonical payload schema

### Distillation Functions (main.py)
- `_distill_project()` — dispatcher routes based on tier and `nlm_baseline` status
- `_distill_nlm()` — wraps NLM flow, sets `nlm_baseline=True` on success, uses `registry.update_fields()`
- `_distill_llm()` — calls minimax-m2.5 via LiteLLM, parses response, indexes to Qdrant
- `_distill_direct()` — embeds curated docs directly (content[:2000]), no LLM call
- `_fetch_existing_notes()` — retrieves canonical NLM notes from Qdrant for Tier 1 delta context

### Background Loop
- `_distill_loop` replaces `_nlm_sync_loop` — runs always (not gated by NLM_ENABLED)
- Dual semaphores: `MAX_CONCURRENT_NLM=2` for NLM calls, `MAX_CONCURRENT_DISTILL=5` for LLM/Direct
- Candidate selection: only projects where `curated_at > last_distill`

### Endpoints
- `POST /curate/{project}?force_nlm=true` — tier-aware, force_nlm bypasses tier check (validates NLM enabled + circuit breaker)
- `POST /feedback` — consumer feedback (3 booleans: useful, coherent, logical), JSONL storage
- `GET /metrics` — adds tier distribution, nlm_baseline_count, per-project feedback stats with `needs_review` flag

### Data Model Changes
- Registry: `tier`, `computed_tier`, `nlm_baseline`, `last_distill`, `last_nlm_distill`
- Qdrant payloads: `distill_source` field replaces `from_nlm` boolean (backwards compat via `_get_distill_source()`)
- Canonical payload schema: text, question, source, distill_source, project, type, model, canonical, chunk_index

### Prerequisite Fix
- Curator DOC_TYPES: Spanish section titles changed to English (Design, Implementation, Operations, etc.)

### Operations
- `scripts/migrate-tiers.py` — one-time migration with backup, dry-run, Qdrant payload migration
- `scripts/cleanup-and-recurate.sh` — updated to only send Tier 1 projects to NLM

## Key Decisions

- **Tier is a pipeline config, not a code branch.** The pipeline stays scan → curate → distill → Qdrant. Only the distill backend changes.
- **Uniform data shape across tiers.** All tiers produce the same payload format in `nlm_notes`. `query.py` doesn't need tier-specific branching.
- **NLM cord-cutting is manual.** The distill loop never auto-triggers NLM after baseline. Only `?force_nlm=true` does.
- **Consumer feedback over self-assessment.** Drift detection comes from agents reporting query usefulness, not internal coherence checks.
- **All-or-nothing LLM parsing.** If any of the 5 answers is too short or malformed, the entire response is rejected.

## Tier Distribution (from dry-run migration)

| Tier | Count | Projects |
|------|-------|----------|
| 1 (NLM) | 4 | jasper, jasper-profiles, infra-mcp, erudito |
| 2 (LLM) | 3 | opencode-contracts, event-bus, kubo-contracts |
| 3 (Direct) | 9 | claude-contracts, devops-agent, mesh-monitor, + 6 more |

**NLM queue reduced from 12 to 4 projects.**

## Test Coverage

| File | Tests | What |
|------|-------|------|
| test_distiller.py | 17 | Tier thresholds, prompt building, response parsing |
| test_main_tiers.py | 8 | Dispatcher routing, force_nlm, error paths |
| test_feedback.py | 4 | Feedback stats, needs_review threshold |
| test_curator.py | +1 | English DOC_TYPES validation |
| test_registry.py | +1 | Tier fields in _DEFAULT_ENTRY |
| test_query.py | +1 | _get_distill_source backwards compat |
| test_indexer.py | +1 | search_by_filter |

**Total: 168 tests passing (128 original + 40 new)**

## Files Changed

| File | Lines | Change |
|------|-------|--------|
| core/distiller.py | ~140 | NEW: tier logic, prompts, parsing |
| main.py | ~+200/-150 | Distill functions, loop, endpoints, /curate refactor |
| core/registry.py | +5 fields | tier, computed_tier, nlm_baseline, last_distill, last_nlm_distill |
| core/query.py | ~15 | _get_distill_source, distill_source checks |
| core/indexer.py | ~25 | search_by_filter function |
| core/curator.py | ~10 | DOC_TYPES Spanish → English |
| integrations/notebooklm.py | +2 | Import DISTILL_QUESTIONS |
| requirements.txt | +1 | litellm>=1.40.0 |
| scripts/migrate-tiers.py | ~105 | NEW: migration script |
| scripts/cleanup-and-recurate.sh | ~40 | Tier-aware cron |
| tests/ | ~330 | 7 new/modified test files |

## Known Issues (in BACKLOG.md)

- Registry thread safety: 13 pre-existing direct mutations need update_fields() (Critical, pre-existing)
- LLM response validation: add _is_nlm_error check to _distill_llm (Important, new)
- Integration test coverage: _distill_llm, _distill_direct, _distill_nlm need unit tests (Nice to have)
