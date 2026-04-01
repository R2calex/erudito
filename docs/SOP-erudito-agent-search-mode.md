# SOP: Erudito Agent Search Mode

**Last Updated:** 2026-03-26
**Project:** Erudito
**Component:** `/search?mode=agent`

---

## Overview

The `mode=agent` search returns concise structured context for LLM system prompt injection. Any AI-Lab agent can use it.

## Usage

### Basic Query

```bash
curl -s "http://hanzo:8095/search?q=how+to+configure+BGP&mode=agent"
```

### With Project Filter and Token Limit

```bash
curl -s "http://hanzo:8095/search?q=BGP+config&mode=agent&project=labforge&max_tokens=500&top_k=3"
```

### Parameters

| Param | Required | Default | Range | Description |
|---|---|---|---|---|
| `q` | Yes | — | min 1 char | Search query |
| `mode` | No | `default` | `default`/`dual`/`agent` | Search mode |
| `project` | No | — | — | Filter to project |
| `top_k` | No | 5 | 1-50 | Max source chunks |
| `max_tokens` | No | 800 | 100-4000 | Approx token budget for context |

### Response Fields

| Field | Type | Description |
|---|---|---|
| `mode` | string | Always `"agent"` |
| `query` | string | Echo of input query |
| `project` | string/null | Echo of project filter |
| `context` | string | Markdown context for LLM injection |
| `sources` | array | Source metadata with scores and URLs |
| `confidence` | string | `high`/`medium`/`low`/`none` |
| `token_estimate` | int | Approximate token count of context |

## Integration Pattern (for consuming agents)

```python
import httpx

ERUDITO_URL = "http://hanzo:8095"

async def get_agent_context(query: str, project: str = None, max_tokens: int = 800) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            params = {"q": query, "mode": "agent", "max_tokens": max_tokens, "top_k": 5}
            if project:
                params["project"] = project
            resp = await client.get(f"{ERUDITO_URL}/search", params=params)
            resp.raise_for_status()
            data = resp.json()
            if data.get("confidence") == "none":
                return None
            return data.get("context")
    except Exception:
        return None  # graceful degradation
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| 422 on `mode=agent` | Old Erudito version running | Redeploy container: `docker compose up -d --build` |
| `confidence: "none"` always | Qdrant down or empty collection | Check `curl http://hanzo:6333/collections/nlm_notes` |
| Empty context | No indexed data for project | Run `POST /scan/{project}` to trigger indexing |
| Slow responses (>2s) | Ollama embedding latency | Check Ollama status: `curl http://hanzo:11434/api/tags` |
| Token estimate too high | max_tokens too generous | Lower `max_tokens` param (default 800 is good for most uses) |

## Monitoring

- Query stats available at `GET /metrics` → `query_stats.total`, `query_stats.high`
- Agent mode queries increment the same counters as default mode
- Qdrant health: `GET /health` → `qdrant` field
