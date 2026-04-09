# Skill: Curate Project for Erudito

> Use this skill to onboard or update a project in Erudito's knowledge base.
> Can be run by any agent — no LLM reasoning required, just follow the steps.

## Prerequisites

- Erudito running and healthy: `curl http://localhost:8095/health`
- Project registered in Erudito registry: `curl http://localhost:8095/registry/{project}`
- NotebookLM MCP accessible (NLM_ENABLED=true)

## Quick Mode (Single API Call)

For automated/unattended curation, use the endpoint:

```bash
curl -s -X POST http://localhost:8095/curate/{project_name} | python -m json.tool
```

This runs the full pipeline: scan → curate → NLM sync → Qdrant index.
Check `result.status == "validated"` for success.

## Supervised Mode (Step by Step)

Use this when onboarding a new project or debugging curation issues.

### Step 1: Verify project is registered

```bash
curl -s http://localhost:8095/registry/{project_name} | python -m json.tool
```

Check: `path` exists, `status` is not empty. Note `last_hash` and `curation_status`.

### Step 2: Trigger scan to detect changes

```bash
curl -s -X POST http://localhost:8095/scan/{project_name} | python -m json.tool
```

Wait 15-20 seconds, then check:

```bash
curl -s http://localhost:8095/registry/{project_name} | python -m json.tool
```

Verify: `curation_status` changed to `"curated"`, `curated_files > 0`.

### Step 3: Inspect curated files

```bash
# Inside container
docker exec erudito ls -la /app/data/curated/{project_name}/
docker exec erudito head -30 /app/data/curated/{project_name}/{feature}.md
```

Check:
- Files grouped by feature (not 1:1 with source files)
- YAML frontmatter with project, feature, sources, curated_at
- Sections ordered: Diseño → Plan → Implementación → Operación

### Step 4: Create or reuse NLM notebook

```python
# Run inside container
from integrations.notebooklm import ensure_notebook
notebook_id = await ensure_notebook("AI-Lab: {project_name}")
```

Check in NotebookLM UI: notebook exists with correct title. No duplicates.

### Step 5: Upload curated sources

```python
from integrations.notebooklm import sync_sources
from pathlib import Path

curated_files = sorted(Path("/app/data/curated/{project_name}").glob("*.md"))
stats = await sync_sources(notebook_id, curated_files)
print(stats)  # {added: N, deleted: 0, updated: 0, skipped: 0, total: N}
```

Check in NotebookLM UI: sources match curated files. No duplicates.

### Step 6: Ask project summary (English only)

```python
from integrations.notebooklm import query_notebook

answer = await query_notebook(notebook_id, "Explain what {project_name} is and what it consists of?")
print(answer)
```

Validate: answer is coherent, references the source material, in English.

### Step 7: Ask control questions (English only)

```python
QUESTIONS = [
    "What changes have occurred since the last session?",
    "Were there any design changes relative to the original plan?",
    "What new resources have been registered for this project?",
    "On which AI-Lab node is this project running?",
    "What is the current project status according to documentation?",
    # Add project-specific questions as needed:
    # "What is the architecture of {component}?",
    # "How does {feature} work?",
]

for q in QUESTIONS:
    answer = await query_notebook(notebook_id, q)
    print(f"Q: {q}\nA: {answer[:200]}\n")
```

Validate: answers reference actual project content, not generic responses.

### Step 8: Save notes in NLM

```python
from integrations.notebooklm import create_note, list_notes

# Summary note
await create_note(notebook_id, f"Q: {summary_question}\nA: {summary}", title="Erudito: Project Summary")

# QA notes
for q, a in qa_pairs:
    await create_note(notebook_id, f"Q: {q}\nA: {a}", title=f"Erudito: {q[:50]}")

# Verify
notes = await list_notes(notebook_id)
print(f"Notes in NLM: {len(notes)}")
```

Check in NotebookLM UI: notes panel shows all Erudito notes.

### Step 9: Index into Qdrant

```python
from core.indexer import ensure_collection, embed_text, upsert_points, generate_point_id, COLLECTION_NLM_NOTES

ensure_collection(COLLECTION_NLM_NOTES)

for note in all_notes:  # summary + QA
    embedding = embed_text(f"{note['question']} {note['answer']}")
    point_id = generate_point_id(f"nlm:{project_name}:{note['question'][:50]}", 0)
    upsert_points([{
        "id": point_id,
        "vector": embedding,
        "payload": {
            "text": note["answer"],
            "question": note["question"],
            "source": f"nlm:{project_name}",
            "project": project_name,
            "from_nlm": True,
            "type": note["type"],
            "chunk_index": 0,
        },
    }], COLLECTION_NLM_NOTES)
```

### Step 10: Validate search

```bash
curl -s "http://localhost:8095/search?q=What+is+{project_name}&top_k=3" | python -m json.tool
```

Check: results include `nlm_notes` entries from this project.

## Updating an Existing Project

When new files are added or existing files change:

1. The scan loop auto-detects the delta (git hash comparison)
2. Curator **fully regenerates** all curated files (not incremental)
3. `sync_sources` detects content changes:
   - New features → adds new sources to NLM
   - Changed features → deletes old source + adds updated one
   - Removed features → deletes source from NLM
4. Re-asks all questions, replaces all notes
5. Re-indexes into Qdrant

To trigger manually: `POST /curate/{project_name}`

## Language Policy

**All content in Erudito is English only.** Questions, answers, notes, and curated documents must be in English. The embedding model (nomic-embed-text) is language-sensitive — mixing languages degrades search quality. Consuming agents must translate to/from the user's language as needed.

## Troubleshooting

| Issue | Check |
|-------|-------|
| "No delta detected" | `last_hash` matches current HEAD. Clear it to force re-scan |
| "Curated dir not found" | Scan hasn't run yet. Trigger `/scan/{project}` first |
| NLM rate limit (RESOURCE_EXHAUSTED) | Wait 1-5 min, or switch NLM account |
| Notes not saving | Check NLM auth: `refresh_auth` tool |
| Search returns raw chunks | Project not yet validated. Run `/curate/{project}` |
