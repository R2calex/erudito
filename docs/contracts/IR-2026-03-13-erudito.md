# IR-2026-03-13: Erudito — Knowledge Orchestration Agent

**Date:** 2026-03-13
**Type:** Implementation Report
**Status:** Completed
**SPEC:** SPEC-ERUDITO.md
**SOP:** SOP-ERUDITO.md

---

## Summary

Built the Product Catalog, comprehensive test suite, and documentation for Erudito, the Knowledge Orchestration Agent that replaces the raw `index_docs.py` indexer with intelligent, LLM-curated RAG.

## Files Created

### Erudito Core (~/ai-lab/erudito/)

| File | Purpose |
|------|---------|
| `catalog.py` | Product Catalog Manager — Qdrant `project_catalog` collection CRUD + semantic search |
| `seed_catalog.py` | Seeds catalog with 8 known AI-Lab projects |
| `tests/__init__.py` | Test package init |
| `tests/conftest.py` | Pytest configuration — custom markers and CLI flags |
| `tests/test_erudito.py` | 40+ tests across unit, network, and integration tiers |

### Documentation (~/desarrollos_openclaw/claude_contracts/)

| File | Purpose |
|------|---------|
| `SPEC-ERUDITO.md` | Full specification — architecture, anti-poison, LLM cost model, acceptance criteria |
| `IR-2026-03-13-erudito.md` | This implementation report |
| `SOP-ERUDITO.md` | Operational procedures — scan, catalog, audit, troubleshooting |

### Pre-existing (built by parallel agent)

| File | Purpose |
|------|---------|
| `main.py` | FastAPI service — scanner, chunker, Qdrant operations, audit, staleness cleanup |
| `analyzer.py` | LLM-powered knowledge extraction with model cascade |
| `test_analyzer.py` | Standalone analyzer tests |
| `requirements.txt` | Dependencies (fastapi, uvicorn) |

## Design Decisions

### 1. Catalog uses Qdrant (not a flat file)
The product catalog stores structured project metadata as Qdrant points with embeddings. This enables natural language search ("what monitoring tools do we have?") alongside exact listing. The `project_catalog` collection is separate from `agent_knowledge` to keep concerns isolated.

### 2. Deterministic point IDs
Both catalog and scanner use MD5-based deterministic IDs. For catalog: `md5(project_name)[:16]` as int. For chunks: `md5(source:chunk_index)[:16]` as int. This ensures upserts are idempotent — re-indexing the same file overwrites the same points.

### 3. Three-tier test architecture
- **Unit tests** (no network): anti-poison, chunking, ID generation, state persistence, audit logging, seed data validation. Always runnable.
- **Network tests** (Qdrant + Ollama): catalog CRUD, embedding, search. Require `--run-network` flag.
- **Integration tests** (Erudito on :8095): health, scan, audit, staleness. Require `--run-integration` flag.

### 4. Seed data is static and manual
The 8 seed projects are hardcoded in `seed_catalog.py` rather than auto-discovered. This is intentional — the catalog should contain curated, human-verified entries. Auto-discovery from the scanner can be added later as an enhancement.

### 5. stdlib-only HTTP
All Qdrant and Ollama interactions use `urllib.request` — no `requests` or `httpx` dependency. This keeps the footprint minimal and aligns with the existing codebase pattern.

## Test Coverage

### Unit Tests (always runnable)
- `TestAntiPoison` — 10 tests covering all poison patterns + safe text
- `TestShouldScan` — 10 tests for file inclusion/exclusion rules
- `TestChunking` — 5 tests for text splitting logic
- `TestPointId` — 4 tests for deterministic ID generation
- `TestCatalogProjectId` — 3 tests for catalog ID generation
- `TestScanState` — 3 tests for state persistence
- `TestAuditLog` — 2 tests for audit file creation/append
- `TestAnalyzerParsing` — 4 tests for JSON response parsing
- `TestSeedCatalogData` — 3 tests for seed data validation

### Network Tests (require Qdrant + Ollama)
- `TestCatalogCRUD` — 7 tests: upsert, list, get, search, update, delete, count
- `TestEmbedding` — 2 tests: single embed, different texts produce different vectors
- `TestEruditoEmbedBatch` — 1 test: batch embedding from main module

### Integration Tests (require Erudito service)
- `TestEruditoHealth` — 2 tests: health check, status endpoint
- `TestEruditoScan` — 3 tests: full scan, node scan, unknown node 404
- `TestEruditoAudit` — 2 tests: audit listing, timestamp validation
- `TestEruditoStaleness` — 1 test: cleanup with long horizon

**Total: 44 tests** across unit/network/integration.

## How to Run

```bash
# Unit tests only (no services needed)
cd ~/ai-lab/erudito && pytest tests/test_erudito.py -m "not network and not integration" -v

# With Qdrant + Ollama running
cd ~/ai-lab/erudito && pytest tests/test_erudito.py --run-network -v

# With Erudito service running
cd ~/ai-lab/erudito && pytest tests/test_erudito.py --run-network --run-integration -v

# Seed the catalog
cd ~/ai-lab/erudito && python seed_catalog.py
```

## Known Limitations

1. Catalog API endpoints are not yet wired into `main.py` — the FastAPI app currently has scan/audit/health/staleness routes but no `/catalog` routes. The catalog module works standalone and via `seed_catalog.py`. API integration is a natural next step.

2. The analyzer is not yet called during scans — `main.py` chunks and embeds raw text without LLM enrichment. Integrating `analyzer.batch_analyze()` into the scan loop is a future enhancement.

3. Remote node scanning (kubo, sariatu via SSH) is defined in `REMOTE_SOURCES` but not yet implemented in the scan loop.
