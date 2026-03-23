# SOP-ERUDITO: Erudito Operations

**Version:** 1.0.0
**Date:** 2026-03-13
**SPEC:** SPEC-ERUDITO.md

---

## 1. Start Erudito

```bash
cd ~/ai-lab/erudito
uvicorn main:app --host 0.0.0.0 --port 8095
```

**Prerequisites:** Qdrant on :6333, Ollama on :11434 with `nomic-embed-text:latest` pulled.

Verify it started:
```bash
curl -s http://localhost:8095/health | python3 -m json.tool
```

Expected: `"status": "ok"`, `"qdrant": "ok"`, `"ollama": "ok"`.

If status is `"degraded"`, check which dependency is unreachable and fix it before scanning.

## 2. Trigger a Manual Scan

### Full scan (all local sources)
```bash
curl -s -X POST http://localhost:8095/scan | python3 -m json.tool
```

### Single node scan
```bash
curl -s -X POST http://localhost:8095/scan/hanzo | python3 -m json.tool
```

### Interpret scan results
```json
{
  "status": "completed",
  "stats": {
    "scanned": 12,    // files processed
    "inserted": 8,    // new files indexed
    "updated": 3,     // existing files re-indexed
    "deleted": 1,     // removed files cleaned up
    "skipped": 0,     // files too small to chunk
    "poisoned": 2,    // files blocked by anti-poison
    "errors": []      // any errors during scan
  }
}
```

- `poisoned > 0` is normal — it means anti-poison is working. Check audit trail for which files were blocked.
- `errors` should be empty. If not, check the error messages for path issues or Ollama timeouts.

## 3. Add a New Scan Source

Edit `SCAN_SOURCES` in `~/ai-lab/erudito/main.py`:

```python
SCAN_SOURCES = [
    # ... existing sources ...
    {"path": os.path.expanduser("~/ai-lab/new-project"), "type": "code", "node": "hanzo"},
]
```

Requirements for a scan source:
- Must be a git repository (has `.git/` directory)
- Must be a local path (remote scanning not yet implemented)
- `type` should be `"docs"` or `"code"`
- `node` identifies which mesh node owns it

After adding, restart Erudito and trigger a scan.

## 4. Product Catalog Operations

### Seed the catalog with known projects
```bash
cd ~/ai-lab/erudito && python seed_catalog.py
```

### Add a single project programmatically
```python
from catalog import upsert_project

upsert_project({
    "name": "my-new-project",
    "description": "What it does in 1-2 sentences",
    "type": "infrastructure",       # infrastructure|mvp|library|experiment|documentation
    "stack": ["python", "fastapi"],
    "status": "active",             # active|stable|deprecated|experimental
    "node_primary": "hanzo",
    "port": 8099,
    "tags": ["relevant", "tags"],
})
```

### List all projects
```python
from catalog import list_projects
for p in list_projects():
    print(f"{p['name']:20s} — {p['description'][:60]}")
```

### Search projects by natural language
```python
from catalog import search_projects
results = search_projects("monitoring health check")
for r in results:
    print(f"{r['score']:.3f}  {r['name']:20s} — {r['description'][:50]}")
```

### Delete a project
```python
from catalog import delete_project
delete_project("old-project-name")
```

## 5. Check the Audit Trail

### Via API (if Erudito is running)
```bash
# Last 10 entries
curl -s "http://localhost:8095/audit?limit=10" | python3 -m json.tool

# Entries since a specific time
curl -s "http://localhost:8095/audit?since=2026-03-13T00:00:00Z" | python3 -m json.tool
```

### Direct file access
```bash
# Last 20 entries
tail -20 ~/ai-lab/erudito/data/audit.jsonl | python3 -m json.tool --json-lines

# Count entries by action type
cat ~/ai-lab/erudito/data/audit.jsonl | python3 -c "
import sys, json, collections
c = collections.Counter()
for line in sys.stdin:
    e = json.loads(line)
    c[e.get('action', 'unknown')] += 1
for action, count in c.most_common():
    print(f'  {action:20s} {count}')
"
```

### Common audit actions
| Action | Meaning |
|--------|---------|
| `scan_complete` | A scan finished successfully |
| `delete` | Chunks removed for a deleted file |
| `skip_poison` | File blocked by anti-poison |
| `embed_error` | Ollama embedding failed |
| `upsert_error` | Qdrant upsert failed |
| `staleness_cleanup` | Stale chunks pruned |

## 6. Verify Anti-Poison Is Working

### Quick check via audit trail
```bash
grep "skip_poison" ~/ai-lab/erudito/data/audit.jsonl | tail -5
```

If this returns entries, anti-poison is active and blocking files.

### Test with a known-bad file
```bash
# Create a test file with a fake credential
echo 'password = "test_fake_credential_12345"' > /tmp/poison_test.txt

# Verify the pattern matches
python3 -c "
import sys; sys.path.insert(0, '$HOME/ai-lab/erudito')
from main import _has_poison
print(_has_poison(open('/tmp/poison_test.txt').read()))
"
# Should print: True

rm /tmp/poison_test.txt
```

### Run unit tests for poison detection
```bash
cd ~/ai-lab/erudito && pytest tests/test_erudito.py::TestAntiPoison -v
```

## 7. Re-Index from Scratch

This is the equivalent of the old `index_docs.py --reset`.

### Step 1: Clear scan state
```bash
rm ~/ai-lab/erudito/data/scan_state.json
```

This forces the next scan to treat all files as new (full `git ls-files` instead of `git diff`).

### Step 2: Optionally clear the Qdrant collection
```bash
# WARNING: This deletes ALL chunks in agent_knowledge
curl -s -X DELETE "http://localhost:6333/collections/agent_knowledge"

# Recreate it (the scan will also auto-create if missing)
curl -s -X PUT "http://localhost:6333/collections/agent_knowledge" \
  -H "Content-Type: application/json" \
  -d '{"vectors": {"size": 768, "distance": "Cosine"}}'
```

### Step 3: Trigger a full scan
```bash
curl -s -X POST http://localhost:8095/scan | python3 -m json.tool
```

This will re-scan all files in all sources.

## 8. Clean Up Stale Chunks

Chunks that haven't been verified (re-scanned) in N days can be pruned:

```bash
# Delete chunks not seen in the last 30 days
curl -s -X DELETE "http://localhost:8095/staleness/cleanup?days=30" | python3 -m json.tool
```

Use a conservative value (30-60 days) to avoid deleting chunks from infrequently-changed files.

## 9. Troubleshoot Scan Failures

### Scan returns errors
Check the `errors` array in the scan response. Common causes:
- **"Directory not found"** — the scan source path doesn't exist. Check `SCAN_SOURCES` in `main.py`.
- **"Not a git repo"** — the directory has no `.git/`. Initialize with `git init` or remove from sources.
- **"Embed error"** — Ollama is down or the model isn't pulled. Fix: `ollama pull nomic-embed-text:latest`.
- **"Upsert error"** — Qdrant is down or the collection is missing.

### Health check shows "degraded"
```bash
# Check Qdrant
curl -s http://localhost:6333/collections/agent_knowledge | python3 -m json.tool

# Check Ollama
curl -s http://localhost:11434/api/tags | python3 -m json.tool
```

### Scan is very slow
- First scan after clearing state re-indexes everything — this is expected.
- Check if Ollama had a cold start: the first embedding call can take 10-30 seconds while the model loads.
- Large files produce many chunks. Check `CHUNK_SIZE` (800 chars) in `main.py`.

### Points not appearing in Qdrant
```bash
# Check collection point count
curl -s http://localhost:6333/collections/agent_knowledge | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f'Points: {d[\"result\"][\"points_count\"]}')
print(f'Status: {d[\"result\"][\"status\"]}')
"
```

If count is 0 after a scan, check the audit trail for `skip_poison` or `embed_error` entries.

## 10. Run Tests

```bash
cd ~/ai-lab/erudito

# Unit tests only (no services needed, always safe)
pytest tests/test_erudito.py -m "not network and not integration" -v

# With Qdrant + Ollama running
pytest tests/test_erudito.py --run-network -v

# Full suite (Erudito must be up on :8095)
pytest tests/test_erudito.py --run-network --run-integration -v
```
