# Erudito v3 — Backlog

## Critical (next session)

- [ ] **Registry thread safety**: Replace all 13 direct `registry._data["projects"]` mutations in main.py with `registry.update_fields()`. The method exists but isn't used yet. Concurrent scan/NLM tasks can corrupt state. (review #2)
- [ ] **Run tier migration**: Execute `python scripts/migrate-tiers.py` to assign tiers and migrate Qdrant payloads. Then run `./scripts/cleanup-and-recurate.sh` which now respects tiers (only Tier 1 hits NLM).

## Important (this week)

- [ ] **API authentication**: Add API key header check for write endpoints (/registry, /ingest, /classify, /nlm/reset, /scan, /curate). Tailscale provides network-level auth but process-level auth is missing. (review #3)
- [ ] **Deprecation fix**: Replace `asyncio.get_event_loop()` with `asyncio.get_running_loop()` in `_fallback_index_curated`. (review #4)
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
