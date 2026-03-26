# Erudito v3 — Backlog

## Critical (next session)

- [ ] **Registry thread safety**: Replace all 13 direct `registry._data["projects"]` mutations in main.py with `registry.update_fields()`. The method exists but isn't used yet. Concurrent scan/NLM tasks can corrupt state. (review #2)
- [x] **Run tier migration**: ~~Execute `python scripts/migrate-tiers.py`~~ — Done. Tiers assigned, Qdrant migrated, legacy cleanup complete.
- [x] **Legacy cleanup**: ~~agent_knowledge collection, orphan points, dead code~~ — Done (commit 0a6cf6d).

## Manual Review Backlog (weekend)

Projects and items that could not be fully categorized/distilled automatically.
Audit date: 2026-03-26.

### Projects never curated (no knowledge extracted)

| Project | Docs | Issue | Action needed |
|---------|------|-------|---------------|
| `claude-contracts` | 130 | `curation_status=uncurated`, tier 3. Never curated despite having 130 docs. Likely too many files for auto-curation or path mismatch. | Review docs, curate manually or adjust scanner config |
| `node-reporter` | 0 | `status=pending`, 0 docs. Registered but never scanned — path may not exist or be empty. | Verify path exists, remove from registry if abandoned |

### Projects with only `direct` distillation (minimal knowledge)

These were distilled by raw doc embedding, not LLM or NLM. Quality is low — single generic note per project.

| Project | Notes | Source | Issue |
|---------|-------|--------|-------|
| `agent-eval` | 1 | direct | Tier 3, only 1 generic note. 3 docs available but not LLM-distilled. |
| `devops-agent` | 1 | direct | Tier 3, only 1 note ("agent-bootstrap-protocol"). 3 docs available. |
| `marker-mcp` | 1 | direct | Tier 3, only 1 note. 3 docs available. |
| `mesh-monitor` | 2 | direct | Tier 3, 2 generic notes. 6 docs available. |
| `openclaw-kubo` | 2 | direct | Tier 3, 2 notes. 2 docs available. |
| `qdrant-mcp` | 1 | direct | Tier 3, 1 note. 1 doc available. |
| `sariatu-docs` | 1 | direct | Tier 3, 1 note. 1 doc available. |

### Tier 1 projects blocked on NLM

These are high-priority projects that should have NLM-quality distillation but NLM circuit breaker is tripped.

| Project | NLM failures | Has LLM fallback? | Notes |
|---------|-------------|-------------------|-------|
| `erudito` | 30 | Yes (5 LLM notes) | NLM never succeeded. LLM fallback working. |
| `infra-mcp` | 29 | Yes (5 LLM notes) | NLM never succeeded. LLM fallback working. |
| `jasper` | 0 | Yes (5 LLM + 9 NLM) | Only project with NLM notes. Circuit OK. |
| `jasper-profiles` | 28 | Yes (5 LLM notes) | NLM never succeeded. LLM fallback working. |

### Recurring LLM distillation errors

Jasper had ~20+ consecutive `distill_llm_error` entries (2026-03-25) with `zen/minimax-m2.5` returning `Cannot read properties of undefined (reading 'prompt_tokens')`. This was fixed by the LiteLLM model name correction but may recur if the upstream model has issues.

### Summary — what to review this weekend

1. **`claude-contracts`**: 130 uncurated docs — biggest gap. Decide if worth curating or splitting.
2. **`node-reporter`**: Ghost project — verify or remove.
3. **7 tier-3 projects with `direct` notes**: Run `POST /curate/{project}` then `POST /scan/{project}` to trigger LLM distillation for each.
4. **NLM circuit breaker**: 14/16 projects have 28-30 NLM failures. Investigate NLM connectivity or accept LLM-only distillation as sufficient.

## Important (this week)

- [ ] **API authentication**: Add API key header check for write endpoints (/registry, /ingest, /classify, /nlm/reset, /scan, /curate). Tailscale provides network-level auth but process-level auth is missing. (review #3)
- [x] **Deprecation fix**: ~~Replace `asyncio.get_event_loop()` with `asyncio.get_running_loop()` in `_fallback_index_curated`~~ — Moot: `_fallback_index_curated` removed in legacy cleanup.
- [ ] **NLM session lock**: Add `asyncio.Lock` to protect `_session_id` / `_session_initialized` in notebooklm.py. (review #5)
- [ ] **Content hash**: Use full sha256 instead of truncated MD5 in `_content_hash`. (review #6)
- [ ] **Dynamic import cleanup**: Replace `__import__('core.registry')` in /ingest with proper import. (review #7)
- [ ] **sync_sources total count**: Fix double-counting of updated sources in total. (review #8)
- [ ] **Inbox path env var**: Make classify inbox path configurable via `CLASSIFY_INBOX` env var. (review #9)
- [x] **Curator section titles**: ~~Change Spanish titles to English~~ — Done in knowledge tiers implementation.
- [ ] **Deploy devops-agents on Kubo/Sariatu**: Endpoints deployed but projects need proper registration with remote paths. See memory: project_remote_nodes_pending.md

## Nice to Have (backlog)

- [ ] **Coherence check algorithm**: Implement in eval-agent (LLM synthesis + embedding comparison). Spec exists.
- [ ] **Technical docs pipeline**: Second Qdrant use case — index external library docs for offline agent access.
- [ ] **`NLM_SCORE_BOOST` dead code**: Remove or implement in query engine. (review #12)
- [ ] **SSE multi-line parsing**: Handle streaming responses with multiple data lines. (review #13)
- [ ] **Test coverage**: Add unit tests for execute_query, answer_from_registry, circuit breaker, _pull_remote_docs, _distill_llm, _distill_direct, _distill_nlm. (review #18)
- [x] **LLM response validation**: ~~Add `_is_nlm_error` check to `_distill_llm`~~ — Done, guards against error responses from LLM gateway.
- [ ] **Rate limiter on /search**: Prevent Ollama/NLM overload from burst requests. (review #16)
- [ ] **push-docs.sh filename escaping**: Escape `$BASENAME` through json.dumps. (review #17)
- [ ] **Register endpoint for remote projects**: Allow POST /registry without path/git validation when node != hanzo. (review #15)
- [ ] **Dockerfile safe.directory**: Restrict to specific mounted paths instead of '*'. (review #19)
- [ ] **NLM concurrency on HTTP path**: `/curate?force_nlm=true` bypasses the NLM semaphore since it doesn't go through `_bounded_distill`. Add semaphore acquisition in `_distill_project` for NLM calls. (review #21)
- [ ] **Stale nlm_consecutive_failures read**: `_distill_nlm` reads `entry` at top, then increments counter from stale snapshot. Use atomic increment in `update_fields` or re-read before write. (review #22)
