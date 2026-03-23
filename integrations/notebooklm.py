"""NotebookLM MCP client for Erudito v3.

All calls are best-effort with graceful degradation.
Protocol: JSON-RPC 2.0 over HTTP (Server-Sent Events response).

Idempotency: checks for existing notebooks/sources/notes before creating.
"""
import json
import logging
import os
import asyncio
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger("erudito.notebooklm")

LITELLM_URL = os.getenv("LITELLM_URL", "http://localhost:4000")
LITELLM_API_KEY = os.getenv("LITELLM_API_KEY", "")
NLM_MCP_URL = os.getenv("NLM_MCP_URL", "http://notebooklm-mcp:8765/mcp")
MCP_ENDPOINT = NLM_MCP_URL
NLM_TIMEOUT = 120.0  # seconds (increased for source_add with wait=True)
NLM_SOURCE_LIMIT = 50
NLM_COOLDOWN_HOURS = int(os.getenv("NLM_COOLDOWN_HOURS", "6"))

# Circuit breaker: stop calling NLM after rate limit until cooldown expires
_circuit_breaker = {
    "tripped": False,
    "tripped_at": None,
    "reason": None,
}

FIXED_QUESTIONS = [
    "What changes have occurred since the last session?",
    "Were there any design changes relative to the original plan?",
    "What new resources have been registered for this project?",
    "On which AI-Lab node is this project running?",
    "What is the current project status according to documentation?",
    "How should other agents and nodes in the AI-Lab mesh use this project?",
    "What are the most common operational issues and how to troubleshoot them?",
    "Where is this project located (path, node, repository)?",
]

_REQUEST_ID = 0


def build_mcp_request(tool_name: str, arguments: dict) -> dict:
    """Build a JSON-RPC 2.0 MCP request."""
    global _REQUEST_ID
    _REQUEST_ID += 1
    return {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "id": _REQUEST_ID,
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }


def parse_mcp_response(raw: dict) -> dict | None:
    """Parse MCP response, handling nested JSON or plain text in content[0].text."""
    if "error" in raw:
        logger.warning(f"NLM MCP error: {raw['error']}")
        return None
    try:
        result = raw.get("result", {})
        if result.get("isError"):
            logger.warning(f"NLM tool error: {result.get('content', [{}])[0].get('text', 'unknown')}")
            return None
        content = result["content"]
        if content and content[0]["type"] == "text":
            text = content[0]["text"]
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"text": text}
    except (KeyError, IndexError) as e:
        logger.warning(f"Failed to parse NLM response: {e}")
    return None


def _parse_sse_response(text: str) -> dict | None:
    """Parse Server-Sent Events response from MCP server."""
    for line in text.splitlines():
        if line.startswith("data: "):
            try:
                return json.loads(line[6:])
            except json.JSONDecodeError:
                continue
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


# Session state for MCP Streamable HTTP
_session_id: str | None = None
_session_initialized: bool = False


async def _ensure_session(client: httpx.AsyncClient) -> bool:
    """Initialize MCP session if not already done."""
    global _session_id, _session_initialized
    if _session_initialized and _session_id:
        return True

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    init_request = {
        "jsonrpc": "2.0",
        "method": "initialize",
        "id": 0,
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "erudito", "version": "3.0"},
        },
    }
    try:
        resp = await client.post(MCP_ENDPOINT, json=init_request, headers=headers)
        resp.raise_for_status()
        _session_id = resp.headers.get("mcp-session-id")
        _session_initialized = True
        logger.info(f"NLM MCP session initialized (session_id={_session_id})")
        return True
    except Exception as e:
        logger.warning(f"NLM MCP session init failed: {e}")
        _session_initialized = False
        _session_id = None
        return False


def is_circuit_open() -> bool:
    """Check if circuit breaker is tripped. Auto-resets after cooldown."""
    if not _circuit_breaker["tripped"]:
        return False
    from datetime import datetime, timezone
    tripped_at = _circuit_breaker["tripped_at"]
    if tripped_at:
        elapsed = (datetime.now(timezone.utc) - tripped_at).total_seconds() / 3600
        if elapsed >= NLM_COOLDOWN_HOURS:
            _circuit_breaker["tripped"] = False
            _circuit_breaker["tripped_at"] = None
            _circuit_breaker["reason"] = None
            logger.info(f"NLM circuit breaker reset after {elapsed:.1f}h cooldown")
            return False
    return True


def trip_circuit(reason: str):
    """Trip the circuit breaker — stops all NLM calls until cooldown."""
    from datetime import datetime, timezone
    _circuit_breaker["tripped"] = True
    _circuit_breaker["tripped_at"] = datetime.now(timezone.utc)
    _circuit_breaker["reason"] = reason
    logger.warning(f"NLM circuit breaker TRIPPED: {reason}. Cooldown: {NLM_COOLDOWN_HOURS}h")


def reset_circuit():
    """Manually reset the circuit breaker."""
    _circuit_breaker["tripped"] = False
    _circuit_breaker["tripped_at"] = None
    _circuit_breaker["reason"] = None
    logger.info("NLM circuit breaker manually reset")


def circuit_status() -> dict:
    """Get circuit breaker status."""
    return dict(_circuit_breaker)


async def _call_mcp(tool_name: str, arguments: dict) -> dict | None:
    """Call a NotebookLM MCP tool directly. Returns parsed result or None."""
    # Circuit breaker: don't call NLM if rate limited
    if is_circuit_open():
        logger.debug(f"NLM circuit open, skipping {tool_name}")
        return None

    request = build_mcp_request(tool_name, arguments)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if _session_id:
        headers["Mcp-Session-Id"] = _session_id

    try:
        async with httpx.AsyncClient(timeout=NLM_TIMEOUT) as client:
            if not await _ensure_session(client):
                return None
            if _session_id:
                headers["Mcp-Session-Id"] = _session_id
            resp = await client.post(MCP_ENDPOINT, json=request, headers=headers)
            resp.raise_for_status()
            raw = _parse_sse_response(resp.text)
            if raw:
                result = parse_mcp_response(raw)
                # Check if NLM returned a rate limit error
                if result and isinstance(result, dict):
                    text = result.get("text") or result.get("error") or ""
                    if "RESOURCE_EXHAUSTED" in str(text):
                        trip_circuit("RESOURCE_EXHAUSTED from Google")
                        return None
                return result
    except Exception as e:
        logger.warning(f"NLM MCP call failed ({tool_name}): {e}")
        global _session_initialized
        _session_initialized = False
    return None


# --- Notebook Operations ---

async def list_notebooks() -> list[dict]:
    """List all notebooks. Returns list of {id, title, source_count, ...}."""
    result = await _call_mcp("notebook_list", {"max_results": 100})
    if result and "notebooks" in result:
        return result["notebooks"]
    return []


async def find_notebook(title: str) -> str | None:
    """Find an existing notebook by exact title. Returns notebook_id or None.

    If multiple notebooks match, returns the most recent one and logs a warning.
    """
    notebooks = await list_notebooks()
    matches = [nb for nb in notebooks if nb.get("title") == title]
    if not matches:
        return None
    if len(matches) > 1:
        logger.warning(f"Found {len(matches)} notebooks with title '{title}', using first match")
    return matches[0].get("id")


async def ensure_notebook(title: str) -> str | None:
    """Get or create a notebook by title. Returns notebook_id or None.

    Checks for existing notebook first to avoid duplicates.
    """
    existing = await find_notebook(title)
    if existing:
        logger.info(f"Reusing existing notebook '{title}': {existing}")
        return existing

    result = await _call_mcp("notebook_create", {"title": title})
    if result and "notebook_id" in result:
        logger.info(f"Created new notebook '{title}': {result['notebook_id']}")
        return result["notebook_id"]
    return None


async def delete_notebook(notebook_id: str) -> bool:
    """Delete a notebook permanently."""
    result = await _call_mcp("notebook_delete", {
        "notebook_id": notebook_id,
        "confirm": True,
    })
    return result is not None


async def get_notebook_info(notebook_id: str) -> dict | None:
    """Get notebook details including sources."""
    return await _call_mcp("notebook_get", {"notebook_id": notebook_id})


# --- Source Operations ---

async def list_sources(notebook_id: str) -> list[dict]:
    """List all sources in a notebook. Returns list of {id, title, type, ...}."""
    result = await _call_mcp("source_list_drive", {"notebook_id": notebook_id})
    if result and "sources" in result:
        return result["sources"]
    # Fallback: try notebook_get which also returns sources
    info = await get_notebook_info(notebook_id)
    if info and "sources" in info:
        return info["sources"] if isinstance(info["sources"], list) else []
    return []


async def delete_source(source_id: str) -> bool:
    """Delete a source permanently."""
    result = await _call_mcp("source_delete", {
        "source_id": source_id,
        "confirm": True,
    })
    return result is not None


async def add_source(notebook_id: str, content: str, title: str = "") -> bool:
    """Add a text source to a notebook. Returns True on success."""
    args = {
        "notebook_id": notebook_id,
        "source_type": "text",
        "text": content,
        "wait": True,
        "wait_timeout": 120.0,
    }
    if title:
        args["title"] = title
    result = await _call_mcp("source_add", args)
    return result is not None


async def get_source_content(source_id: str) -> str | None:
    """Get raw text content of a source."""
    result = await _call_mcp("source_get_content", {"source_id": source_id})
    if result:
        return result.get("content") or result.get("text") or None
    return None


def _content_hash(text: str) -> str:
    """Quick hash for content comparison."""
    import hashlib
    return hashlib.md5(text.encode()).hexdigest()[:16]


async def sync_sources(notebook_id: str, curated_files: list[Path]) -> dict:
    """Sync curated files to notebook sources. Idempotent with content change detection.

    - Lists existing sources, matches by title (filename)
    - Deletes sources whose curated file no longer exists (feature removed)
    - Detects content changes: if title matches but content differs, replaces source
    - Adds new sources that don't exist yet (new feature)
    - Skips sources where title matches and content is unchanged

    Returns: {added: int, deleted: int, updated: int, skipped: int, total: int}
    """
    stats = {"added": 0, "deleted": 0, "updated": 0, "skipped": 0, "total": 0}

    existing_sources = await list_sources(notebook_id)
    existing_by_title = {}
    for src in existing_sources:
        title = src.get("title") or src.get("name") or ""
        existing_by_title[title] = src

    desired_titles = {cf.name for cf in curated_files}

    # Delete sources whose curated file no longer exists
    for title, src in existing_by_title.items():
        if title and title not in desired_titles:
            src_id = src.get("id") or src.get("source_id")
            if src_id:
                ok = await delete_source(src_id)
                if ok:
                    stats["deleted"] += 1
                    logger.info(f"Deleted stale source '{title}' from notebook")

    # Process each curated file
    for cf in curated_files:
        new_content = cf.read_text(encoding="utf-8")

        if cf.name in existing_by_title:
            # Source exists — check if content changed
            src = existing_by_title[cf.name]
            src_id = src.get("id") or src.get("source_id")

            # Try to get existing content for comparison
            content_changed = False
            if src_id:
                old_content = await get_source_content(src_id)
                if old_content:
                    content_changed = _content_hash(old_content) != _content_hash(new_content)
                else:
                    # Can't read old content, assume changed to be safe
                    content_changed = True

            if content_changed:
                # Delete old + add new (replace)
                if src_id:
                    await delete_source(src_id)
                current_count = len(existing_sources) - stats["deleted"] - stats["updated"] + stats["added"]
                if current_count >= NLM_SOURCE_LIMIT:
                    logger.warning(f"Reached source limit ({NLM_SOURCE_LIMIT}), cannot update '{cf.name}'")
                    continue
                ok = await add_source(notebook_id, new_content, cf.name)
                if ok:
                    stats["updated"] += 1
                    logger.info(f"Updated source '{cf.name}' (content changed)")
            else:
                stats["skipped"] += 1
                logger.debug(f"Source '{cf.name}' unchanged, skipping")
        else:
            # New source
            current_count = len(existing_sources) - stats["deleted"] + stats["added"]
            if current_count >= NLM_SOURCE_LIMIT:
                logger.warning(f"Reached source limit ({NLM_SOURCE_LIMIT}), cannot add '{cf.name}'")
                break
            ok = await add_source(notebook_id, new_content, cf.name)
            if ok:
                stats["added"] += 1
                logger.info(f"Added new source '{cf.name}'")

    stats["total"] = len(existing_sources) - stats["deleted"] + stats["added"] + stats["updated"]
    return stats


# --- Note Operations ---

async def list_notes(notebook_id: str) -> list[dict]:
    """List all notes in a notebook."""
    result = await _call_mcp("note", {
        "notebook_id": notebook_id,
        "action": "list",
    })
    if result and "notes" in result:
        return result["notes"]
    return []


async def create_note(notebook_id: str, note_text: str, title: str = "") -> bool:
    """Create a note in a notebook. Returns True on success."""
    args = {
        "notebook_id": notebook_id,
        "action": "create",
        "content": note_text,
    }
    if title:
        args["title"] = title
    result = await _call_mcp("note", args)
    return result is not None


async def delete_note(notebook_id: str, note_id: str) -> bool:
    """Delete a note."""
    result = await _call_mcp("note", {
        "notebook_id": notebook_id,
        "action": "delete",
        "note_id": note_id,
        "confirm": True,
    })
    return result is not None


async def query_notebook(notebook_id: str, query: str) -> str | None:
    """Query a notebook. Returns answer text or None."""
    result = await _call_mcp("notebook_query", {
        "notebook_id": notebook_id,
        "query": query,
    })
    if result:
        if isinstance(result, str):
            return result
        return result.get("answer") or result.get("response") or result.get("text") or str(result)
    return None


# --- NLM Cycle ---

async def run_nlm_cycle(
    notebook_id: str,
    curated_dir: str,
    project_name: str = "",
    dynamic_question_generator=None,
) -> dict:
    """Run the full NotebookLM validation cycle for a project.

    Idempotent: syncs sources (adds new, skips existing), asks questions,
    saves notes (clears old notes first to avoid duplicates).

    Returns: {success: bool, notes: list[dict], sources_uploaded: int}
    """
    result = {"success": False, "notes": [], "sources_uploaded": 0}

    curated_path = Path(curated_dir)
    if not curated_path.is_dir():
        logger.warning(f"Curated dir not found: {curated_dir}")
        return result

    curated_files = sorted(curated_path.glob("*.md"))
    if not curated_files:
        logger.warning(f"No curated files in {curated_dir}")
        return result

    # 1. Sync sources (idempotent: adds new, deletes stale, skips existing)
    sync_stats = await sync_sources(notebook_id, curated_files)
    result["sources_uploaded"] = sync_stats["added"]
    logger.info(
        f"Source sync for {project_name}: "
        f"added={sync_stats['added']}, deleted={sync_stats['deleted']}, "
        f"skipped={sync_stats['skipped']}, total={sync_stats['total']}"
    )

    result["sync_stats"] = sync_stats

    # 2. Wait for NLM to process new/updated sources
    if sync_stats["added"] > 0 or sync_stats.get("updated", 0) > 0:
        await asyncio.sleep(20)

    # 3. Project summary question
    summary_q = f"Explain what {project_name} is and what it consists of?" if project_name else "Explain what this project is and what it consists of?"
    summary = await query_notebook(notebook_id, summary_q)
    if summary:
        result["notes"].append({
            "type": "summary",
            "question": summary_q,
            "answer": summary,
            "project": project_name,
        })

    # 4. Ask fixed questions
    for question in FIXED_QUESTIONS:
        answer = await query_notebook(notebook_id, question)
        if answer:
            result["notes"].append({
                "type": "qa",
                "question": question,
                "answer": answer,
                "project": project_name,
            })

    # 5. Ask dynamic questions
    if dynamic_question_generator:
        dynamic = dynamic_question_generator(curated_dir)
        for question in dynamic[:5]:
            answer = await query_notebook(notebook_id, question)
            if answer:
                result["notes"].append({
                    "type": "qa_dynamic",
                    "question": question,
                    "answer": answer,
                    "project": project_name,
                })

    # 6. Save notes in notebook (clear old erudito notes first to avoid duplicates)
    existing_notes = await list_notes(notebook_id)
    for existing in existing_notes:
        content = existing.get("content") or existing.get("text") or ""
        # Only delete notes that were created by Erudito (start with "Q: ")
        if content.startswith("Q: "):
            note_id = existing.get("id") or existing.get("note_id")
            if note_id:
                await delete_note(notebook_id, note_id)

    notes_saved = 0
    for note in result["notes"]:
        note_text = f"Q: {note['question']}\nA: {note['answer']}"
        title = f"Erudito: {note['question'][:50]}"
        saved = await create_note(notebook_id, note_text, title=title)
        if saved:
            notes_saved += 1
        else:
            logger.warning(f"Failed to save note in notebook {notebook_id}")

    logger.info(f"Notes for {project_name}: {notes_saved}/{len(result['notes'])} saved to NLM")

    # 7. Verify notes were saved
    verify_notes = await list_notes(notebook_id)
    erudito_notes = [n for n in verify_notes if (n.get("content") or n.get("text") or "").startswith("Q: ")]
    if len(erudito_notes) < len(result["notes"]):
        logger.warning(
            f"Note verification: expected {len(result['notes'])} notes, "
            f"found {len(erudito_notes)} in NLM for {project_name}"
        )

    result["success"] = len(result["notes"]) > 0
    return result
