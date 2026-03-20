"""NotebookLM MCP client via LiteLLM HTTP proxy.

All calls are best-effort with graceful degradation.
Protocol: JSON-RPC 2.0 over HTTP (Server-Sent Events response).
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
# Direct connection to NotebookLM MCP server (bypasses LiteLLM proxy due to Streamable HTTP bug)
NLM_MCP_URL = os.getenv("NLM_MCP_URL", "http://notebooklm-mcp:8765/mcp")
MCP_ENDPOINT = NLM_MCP_URL
NLM_TIMEOUT = 60.0  # seconds
NLM_SOURCE_LIMIT = 50

FIXED_QUESTIONS = [
    "What changes have occurred since the last session?",
    "Were there any design changes relative to the original plan?",
    "What new resources have been registered for this project?",
    "On which AI-Lab node is this project running?",
    "What is the current project status according to documentation?",
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
    """Parse MCP response, handling nested JSON in content[0].text."""
    if "error" in raw:
        logger.warning(f"NLM MCP error: {raw['error']}")
        return None
    try:
        content = raw["result"]["content"]
        if content and content[0]["type"] == "text":
            return json.loads(content[0]["text"])
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        logger.warning(f"Failed to parse NLM response: {e}")
    return None


def _parse_sse_response(text: str) -> dict | None:
    """Parse Server-Sent Events response from LiteLLM MCP proxy."""
    for line in text.splitlines():
        if line.startswith("data: "):
            try:
                return json.loads(line[6:])
            except json.JSONDecodeError:
                continue
    # Try as plain JSON (non-SSE response)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


# Session state for MCP Streamable HTTP
_session_id: str | None = None
_session_initialized: bool = False


async def _ensure_session(client: httpx.AsyncClient) -> bool:
    """Initialize MCP session if not already done. Returns True on success."""
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
        # Extract session ID from response headers
        _session_id = resp.headers.get("mcp-session-id")
        _session_initialized = True
        logger.info(f"NLM MCP session initialized (session_id={_session_id})")
        return True
    except Exception as e:
        logger.warning(f"NLM MCP session init failed: {e}")
        _session_initialized = False
        _session_id = None
        return False


async def _call_mcp(tool_name: str, arguments: dict) -> dict | None:
    """Call a NotebookLM MCP tool directly. Returns parsed result or None."""
    request = build_mcp_request(tool_name, arguments)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if _session_id:
        headers["Mcp-Session-Id"] = _session_id

    try:
        async with httpx.AsyncClient(timeout=NLM_TIMEOUT) as client:
            # Ensure session is initialized
            if not await _ensure_session(client):
                return None
            # Add session ID after init
            if _session_id:
                headers["Mcp-Session-Id"] = _session_id
            resp = await client.post(MCP_ENDPOINT, json=request, headers=headers)
            resp.raise_for_status()
            raw = _parse_sse_response(resp.text)
            if raw:
                return parse_mcp_response(raw)
    except Exception as e:
        logger.warning(f"NLM MCP call failed ({tool_name}): {e}")
        # Reset session on error so next call re-initializes
        global _session_initialized
        _session_initialized = False
    return None


async def create_notebook(title: str) -> str | None:
    """Create a new notebook. Returns notebook_id or None."""
    result = await _call_mcp("notebook_create", {"title": title})
    if result and "notebook_id" in result:
        return result["notebook_id"]
    return None


async def add_source(notebook_id: str, content: str, title: str = "") -> bool:
    """Add a text source to a notebook. Returns True on success."""
    result = await _call_mcp("source_add", {
        "notebook_id": notebook_id,
        "source": content,
    })
    return result is not None


async def query_notebook(notebook_id: str, query: str) -> str | None:
    """Query a notebook. Returns answer text or None."""
    result = await _call_mcp("notebook_query", {
        "notebook_id": notebook_id,
        "query": query,
    })
    if result:
        # Response may be string or dict with 'answer' key
        if isinstance(result, str):
            return result
        return result.get("answer") or result.get("response") or str(result)
    return None


async def create_note(notebook_id: str, note_text: str) -> bool:
    """Create a note in a notebook. Returns True on success."""
    result = await _call_mcp("note", {
        "notebook_id": notebook_id,
        "note": note_text,
    })
    return result is not None


async def get_notebook_info(notebook_id: str) -> dict | None:
    """Get notebook details including source count."""
    return await _call_mcp("notebook_get", {"notebook_id": notebook_id})


async def run_nlm_cycle(
    notebook_id: str,
    curated_dir: str,
    project_name: str = "",
    dynamic_question_generator=None,
) -> dict:
    """Run the full NotebookLM validation cycle for a project.

    Reads curated .md files from curated_dir, uploads as sources,
    asks fixed + dynamic questions, saves notes.

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

    info = await get_notebook_info(notebook_id)
    current_sources = 0
    if info:
        sources = info.get("sources", [])
        current_sources = len(sources) if isinstance(sources, list) else 0

    skip_source_add = False
    if current_sources + len(curated_files) > NLM_SOURCE_LIMIT:
        logger.warning(
            f"Notebook {notebook_id} would exceed {NLM_SOURCE_LIMIT} sources "
            f"({current_sources} + {len(curated_files)}). Skipping source_add."
        )
        skip_source_add = True

    if not skip_source_add:
        for cf in curated_files:
            content = cf.read_text(encoding="utf-8")
            ok = await add_source(notebook_id, content, cf.name)
            if ok:
                result["sources_uploaded"] += 1

    await asyncio.sleep(20)

    condensed = await query_notebook(
        notebook_id,
        "Condense all the information from the sources into a comprehensive summary."
    )
    if condensed:
        result["notes"].append({
            "type": "condensed",
            "question": "Condense all sources",
            "answer": condensed,
            "project": project_name,
        })

    for question in FIXED_QUESTIONS:
        answer = await query_notebook(notebook_id, question)
        if answer:
            result["notes"].append({
                "type": "qa",
                "question": question,
                "answer": answer,
                "project": project_name,
            })

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

    for note in result["notes"]:
        note_text = f"Q: {note['question']}\nA: {note['answer']}"
        saved = await create_note(notebook_id, note_text)
        if not saved:
            logger.warning(f"Failed to save note in notebook {notebook_id}")

    result["success"] = len(result["notes"]) > 0
    return result
