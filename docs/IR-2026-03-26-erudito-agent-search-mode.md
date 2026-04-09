# IR: Erudito `mode=agent` Search Mode

**Date:** 2026-03-26
**Author:** r0calex + Claude Opus 4.6
**Project:** Erudito (Knowledge Orchestration Agent)
**Branch:** feat/erudito-v3
**Related Spec:** SPEC-rag-knowledge-manager.md (Section 4 / Phase 1-E)

---

## What Changed

Added a new search mode (`mode=agent`) to Erudito's `/search` endpoint. This mode returns concise, structured markdown context optimized for injection into LLM system prompts — usable by any AI-Lab agent, not just LabForge.

### Files Modified

| File | Change |
|---|---|
| `core/query.py` | Added `_format_agent_context()`, `_execute_agent()`, `max_tokens` param to `execute_query()` |
| `main.py` | Updated `/search` endpoint: added `agent` to mode regex, added `max_tokens` query param (100-4000, default 800) |
| `tests/test_query.py` | Added `TestFormatAgentContext` (7 tests), `TestExecuteAgent` (5 tests), `TestSearchAgentModeEndpoint` (3 integration tests) |

### New Endpoint Behavior

```
GET /search?q={query}&mode=agent&project={project}&top_k={n}&max_tokens={limit}
```

Response:
```json
{
  "mode": "agent",
  "query": "...",
  "project": "...",
  "context": "## Relevant Knowledge\n\n### Title (project)\n...\nSource: file.md",
  "sources": [{"title": "...", "project": "...", "type": "nlm_note", "score": 0.85, "url": "..."}],
  "confidence": "high|medium|low|none",
  "token_estimate": 650
}
```

## Why

LabForge (and future AI-Lab agents) need vendor-specific knowledge injected into their system prompts. The existing `default` mode returns verbose narrative answers with inline references — unsuitable for LLM context injection. The `agent` mode provides:

- Concise, facts-first markdown sections
- Token budget enforcement via `max_tokens`
- Token estimate for upstream budget management
- No NLM live consultation (latency constraint)
- Circuit breaker: returns `confidence: "none"` on Qdrant failure

## Design Decisions

1. **No NLM live** — Agent mode is latency-sensitive (5s timeout on consumer side). Uses distilled notes from Qdrant only.
2. **Truncation strategy** — Least-relevant sections (lowest score) are dropped first. The first section always fits.
3. **Token estimation** — `len(context) / 4` (English average). Approximate but sufficient for budget management.
4. **Backward compatible** — `default` and `dual` modes unchanged. `agent` is purely additive.

## Verification

- 28 unit tests pass (12 new for agent mode)
- Integration tests require container redeploy (live instance still runs old code)
