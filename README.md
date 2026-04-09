# erudito

Knowledge orchestration agent with NotebookLM integration for the AI-Lab mesh.

## What it does

Erudito v3 is a FastAPI service that indexes, curates, and queries knowledge across all AI-Lab projects. It maintains a project registry (YAML + Qdrant dual-write), runs background scans to detect file changes, uses LLM-based analysis (via LiteLLM model cascade) to extract structured knowledge, and routes queries through a confidence-based system (local Qdrant embeddings vs NotebookLM for deep research). Includes a product catalog with semantic search powered by Ollama embeddings and Qdrant.

## Who uses it

- Claude Code agents (query knowledge via `/search` and `/registry`)
- mesh-monitor (checks Erudito health)
- infra-mcp / Keystone (project catalog registration)
- Operators (manual ingestion, curation, feedback)
- NotebookLM MCP (optional deep-research integration)

## How to run

```bash
# Start (includes sanitizer-hanzo sidecar)
cd ~/ai-lab/erudito
docker compose up -d

# Stop
docker compose down
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `QDRANT_URL` | `http://qdrant:6333` | Qdrant vector DB |
| `OLLAMA_URL` | `http://172.17.0.1:11434` | Ollama for embeddings |
| `LITELLM_URL` | `http://litellm-proxy-local:4000/v1` | LiteLLM proxy for LLM calls |
| `REDIS_URL` | -- | Optional Redis for caching |
| `SANITIZER_URL` | `http://sanitizer-hanzo:8086` | Text sanitizer sidecar |
| `SCAN_INTERVAL_MINUTES` | `15` | Background scan frequency |
| `NLM_ENABLED` | `true` | Enable NotebookLM integration |
| `ERUDITO_CONFIDENCE_THRESHOLD` | `0.75` | Confidence threshold for query routing |
| `DATA_DIR` | `/app/data` | Registry, audit logs, curated content |

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| GET | `/registry` | List all registered projects |
| GET | `/registry/{project}` | Get single project details |
| POST | `/registry` | Register a new project |
| POST | `/ingest/{project}` | Ingest documents for a project |
| DELETE | `/ingest/{project}` | Remove ingested data |
| POST | `/classify` | Classify a document |
| POST | `/scan` | Trigger full scan of all projects |
| POST | `/scan/{project}` | Scan a single project |
| POST | `/curate/{project}` | Curate a project's knowledge |
| GET | `/search` | Semantic search (modes: `default`, `dual`, `agent`, `agentic`) |
| POST | `/feedback` | Submit query quality feedback |
| GET | `/metrics` | Service metrics and scan stats |
| POST | `/metrics/coherence` | Compute knowledge coherence score |
| GET | `/audit` | View audit log |
| GET | `/nlm/status` | NotebookLM integration status |
| POST | `/nlm/reset` | Reset NLM state |

**Port:** 8095 (localhost and Tailscale IP 100.119.223.20)
**Container name:** `erudito`

## Search Modes

| Mode | Description | Latency | Use case |
|------|-------------|---------|----------|
| `default` | Qdrant vector search + NLM live fallback | 50-200ms | Quick lookups |
| `dual` | Qdrant + NLM live in parallel | 3-8s | Source comparison |
| `agent` | Qdrant only, markdown for LLM injection | 50-200ms | Agent system prompts |
| `agentic` | LLM decompose + multi-retrieve + synthesize | 6-30s | Complex cross-project questions |

```bash
# Agentic search example
curl 'http://localhost:8095/search?q=how+does+mesh+coordination+work&mode=agentic'
```

## Project Types

| Type | Registration | File types | Delta detection |
|------|-------------|------------|-----------------|
| `repo` (default) | Requires git | `.md` | `git diff` |
| `knowledge_base` | Plain folder | `.md`, `.txt`, `.yaml`, `.yml` | Filesystem hash |

```bash
# Register a knowledge base (no git needed)
curl -X POST http://localhost:8095/registry \
  -H 'Content-Type: application/json' \
  -d '{"name":"my-kb","path":"/app/knowledge/my-kb","type":"knowledge_base"}'
```
