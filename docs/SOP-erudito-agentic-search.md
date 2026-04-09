# SOP: Erudito Agentic Search & Knowledge Base Registration

**Last updated:** 2026-04-02
**Service:** Erudito (port 8095)

---

## 1. Search Modes Reference

Erudito exposes 4 search modes via `GET /search?q={query}&mode={mode}`:

| Mode | Description | Latency | Best for |
|---|---|---|---|
| `default` | Qdrant vector search + NLM live fallback | 50-200ms | Quick lookups, single-project questions |
| `dual` | Qdrant + NLM live in parallel, returns both | 3-8s | Comparing sources, debugging knowledge gaps |
| `agent` | Qdrant only, returns structured markdown for LLM injection | 50-200ms | Other agents needing context for their system prompts |
| `agentic` | LLM decomposes query, multi-hop retrieval, synthesized answer | 6-30s | Complex questions spanning multiple projects, technical deep-dives |

### When to use `mode=agentic`

Use agentic mode when:
- The question involves **multiple projects** (e.g., "how do Keystone and Sariatu coordinate?")
- The question requires **reasoning** not just retrieval (e.g., "what are the deployment best practices across the mesh?")
- You need a **ready-to-use answer** with citations, not raw chunks

Do NOT use agentic mode when:
- You need sub-200ms response (use `default` or `agent`)
- You're feeding context into another LLM prompt (use `agent` — it's pre-formatted)
- Cost is a concern (each agentic query costs 2 LLM calls)

### Example agentic query

```bash
curl 'http://localhost:8095/search?q=how+does+the+infrastructure+mesh+work+and+what+agents+are+deployed+on+each+node&mode=agentic'
```

Response includes:
- `answer` — synthesized text with `[Source: filename]` citations
- `reasoning_steps` — the sub-queries the LLM generated
- `sources` — ranked list with scores
- `elapsed_ms` — total processing time

---

## 2. Knowledge Base Registration

### What is a knowledge base?

A `knowledge_base` is a non-git directory containing technical knowledge files (`.md`, `.txt`, `.yaml`, `.yml`). Unlike repos, knowledge bases don't need git history — they're scanned via filesystem hash comparison.

### How to register

```bash
# 1. Create the knowledge directory on the host
mkdir -p ~/ai-lab/knowledge/my-topic

# 2. Add content files (.md, .txt, .yaml, .yml)
echo "# My Knowledge\n\nContent here..." > ~/ai-lab/knowledge/my-topic/overview.md

# 3. Ensure the directory is mounted in the Erudito container
# Add to docker run: -v /home/r0calex/ai-lab/knowledge:/app/knowledge:ro

# 4. Register via API
curl -X POST http://localhost:8095/registry \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "my-topic",
    "path": "/app/knowledge/my-topic",
    "type": "knowledge_base",
    "node": "hanzo"
  }'

# 5. Trigger initial scan
curl -X POST http://localhost:8095/scan/my-topic
```

### Key differences from repo registration

| Aspect | `type: repo` | `type: knowledge_base` |
|---|---|---|
| Git required | Yes | No |
| File types scanned | `.md` only | `.md`, `.txt`, `.yaml`, `.yml` |
| Delta detection | `git diff` | Filesystem hash comparison |
| Registration validation | Checks git root | Checks directory exists |

### Adding content after registration

Just add files to the mounted directory. Erudito's background scan (every 15 min) will detect changes via filesystem hash. To scan immediately: `POST /scan/{project}`.

---

## 3. Troubleshooting

### Agentic mode returns raw context instead of synthesized answer
- **Cause:** LLM call failed (timeout, rate limit, auth error)
- **Check:** `docker logs erudito-v3 | grep "synthesis failed"`
- **Fix:** Verify `LITELLM_MASTER_KEY` is set and LiteLLM proxy is healthy

### Agentic mode always returns 1 reasoning step
- **Cause:** Decomposition LLM call failing, falling back to original query
- **Check:** `docker logs erudito-v3 | grep "decompose failed"`
- **Fix:** Same as above — LLM connectivity issue

### Knowledge base not appearing in search results
- **Cause:** Not scanned yet or mount missing
- **Check:** `curl http://localhost:8095/registry/my-topic` — verify `status` and `doc_count`
- **Fix:** Trigger `POST /scan/my-topic`, check container has the volume mount

### Rate limit errors on free models
- **Cause:** Free zen models have aggressive rate limits
- **Fix:** Switch to paid model: set `AGENTIC_LLM_MODEL=openai/zen/minimax-m2.5`
