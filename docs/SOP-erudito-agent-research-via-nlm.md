# SOP: How Any AI-Lab Agent Uses Erudito + NotebookLM for Research

**Last Updated:** 2026-03-26
**Audience:** Any AI-Lab agent (Claude Code, devops-agent, LabForge, etc.)
**Prerequisite:** Erudito running on Hanzo (`http://hanzo:8095`)

---

## Overview

NotebookLM (NLM) is only accessible through Erudito — it runs as an internal Docker service with rate limits managed by Erudito's circuit breaker. No agent should call NLM directly.

Erudito exposes a simple HTTP API that any agent can use. The full pipeline is:

```
Register → Ingest → Scan → Curate → Distill (NLM/LLM) → Search
```

Most agents only need **Ingest + Curate + Search**.

---

## Step-by-Step

### Step 1: Register the project (one-time)

```bash
curl -X POST "http://hanzo:8095/registry" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-project",
    "path": "/app/data/ingested/my-project",
    "node": "sariatu"
  }'
```

- If the project is on Hanzo's filesystem (git repo), use the real path
- If the project is remote (Sariatu, Kubo), use `/app/data/ingested/{project}` — files will be sent via ingest

**Check if already registered:**
```bash
curl -s "http://hanzo:8095/registry/my-project"
```

### Step 2: Send your documents (remote nodes only)

For projects NOT on Hanzo's filesystem, push your .md files:

```bash
curl -X POST "http://hanzo:8095/ingest/my-project" \
  -H "Content-Type: application/json" \
  -d '{
    "files": {
      "docs/spec/SPEC-example.md": "# My SPEC\n\nContent...",
      "docs/architecture.md": "# Architecture\n\n...",
      "README.md": "# Project Overview\n\n..."
    },
    "node": "sariatu"
  }'
```

**Tips:**
- Keys = relative file paths (directories preserved)
- Values = file content as strings (markdown)
- Max 200 files, max 1MB per file
- Idempotent — re-sending overwrites existing files

### Step 3: Run the full pipeline (scan + curate + distill)

```bash
curl -X POST "http://hanzo:8095/curate/my-project"
```

This runs synchronously and does everything:
1. **Scan** — detects changed files (git diff for local repos, filesystem hash for ingested)
2. **Curate** — consolidates files by feature/topic into structured documents
3. **Tier assignment** — decides how to distill:
   - **Tier 1** (≥5 features): Full NotebookLM analysis
   - **Tier 2** (3-4 features): LLM distillation via LiteLLM
   - **Tier 3** (<3 features): Direct embedding into Qdrant
4. **Distill** — generates Q&A notes and embeds them into Qdrant

**To force NotebookLM regardless of tier:**
```bash
curl -X POST "http://hanzo:8095/curate/my-project?force_nlm=true"
```

**Response example:**
```json
{
  "project": "my-project",
  "steps": {
    "scan": {"files": 12, "status": "delta_detected"},
    "curate": {"status": "ok", "curated_files": 4, "features": ["auth", "api", "deploy", "config"]},
    "distill": {"status": "ok", "backend": "nlm"}
  },
  "tier": 1,
  "distill_backend": "nlm",
  "status": "validated"
}
```

### Step 4: Search for knowledge

```bash
# For agent system prompt injection (concise, structured):
curl -s "http://hanzo:8095/search?q=how+to+configure+BGP&project=my-project&mode=agent&max_tokens=800"

# For human-readable answers:
curl -s "http://hanzo:8095/search?q=how+to+configure+BGP&project=my-project&mode=default"
```

**Agent mode response:**
```json
{
  "mode": "agent",
  "context": "## Relevant Knowledge\n\n### BGP Configuration\n...",
  "sources": [...],
  "confidence": "high",
  "token_estimate": 650
}
```

**Low-confidence queries automatically consult NLM live** (default mode only):
- If Qdrant score < 0.75 AND the project has a NotebookLM notebook
- The answer gets cached as a new note in Qdrant for future queries
- This is transparent — the agent just sees a better answer

### Step 5: Update knowledge (ongoing)

When your project changes, repeat steps 2-3:

```bash
# Re-send updated files
curl -X POST "http://hanzo:8095/ingest/my-project" \
  -d '{"files": {"docs/new-feature.md": "..."}, "node": "sariatu"}'

# Re-run pipeline (only processes changes)
curl -X POST "http://hanzo:8095/curate/my-project"
```

---

## Quick Reference — Endpoints

| Action | Method | Endpoint | Notes |
|---|---|---|---|
| Register project | POST | `/registry` | One-time setup |
| Check registration | GET | `/registry/{project}` | Verify status |
| Send files | POST | `/ingest/{project}` | Remote nodes only |
| Remove files | DELETE | `/ingest/{project}?prefix=...` | Optional prefix filter |
| Full pipeline | POST | `/curate/{project}` | scan+curate+distill |
| Scan only | POST | `/scan/{project}` | Async, no distill |
| Search (agent) | GET | `/search?mode=agent&q=...` | For LLM injection |
| Search (human) | GET | `/search?mode=default&q=...` | Narrative answers |
| NLM status | GET | `/nlm/status` | Circuit breaker check |
| NLM reset | POST | `/nlm/reset` | After rate limit recovery |

## Python Integration Pattern

```python
import httpx

ERUDITO = "http://hanzo:8095"

async def push_and_index(project: str, files: dict, node: str):
    """Push files and run full pipeline."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Ingest
        resp = await client.post(f"{ERUDITO}/ingest/{project}", json={"files": files, "node": node})
        resp.raise_for_status()

        # Full pipeline (may take 30-60s for NLM tier)
        resp = await client.post(f"{ERUDITO}/curate/{project}", timeout=120.0)
        return resp.json()

async def search(query: str, project: str = None, max_tokens: int = 800) -> str | None:
    """Get agent-ready context from Erudito."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        params = {"q": query, "mode": "agent", "max_tokens": max_tokens}
        if project:
            params["project"] = project
        resp = await client.get(f"{ERUDITO}/search", params=params)
        resp.raise_for_status()
        data = resp.json()
        if data.get("confidence") == "none":
            return None
        return data.get("context")
```

## Troubleshooting

| Problem | Check | Fix |
|---|---|---|
| `files_stored: 0` | Old Erudito version | Redeploy: `docker compose up -d --build` |
| Scan returns `no_changes` | Old scanner (git-only) | Redeploy — now supports ingested dirs |
| `curate` → `distill: skipped` | No curated files | Check `curate` step output for errors |
| NLM circuit tripped | `GET /nlm/status` | Wait for cooldown or `POST /nlm/reset` |
| `confidence: none` | Qdrant empty or down | Check `GET /health` → qdrant field |
| Slow distill (>60s) | NLM processing | Normal for tier 1 with many features |
