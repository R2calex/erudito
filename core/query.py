"""Search + confidence routing with registry-first detection and multi-mode search.

Routes: registry queries → registry direct, dual mode → Qdrant + NLM live,
agent mode → concise structured context for LLM injection,
agentic mode → LLM-powered decompose + multi-retrieve + synthesize,
default → nlm_notes only.
"""
import json
import logging
import os
import re
import time
from typing import Optional

from core.indexer import embed_text, search, upsert_points, generate_point_id, COLLECTION_NLM_NOTES

logger = logging.getLogger("erudito.query")

CONFIDENCE_THRESHOLD = float(os.getenv("ERUDITO_CONFIDENCE_THRESHOLD", "0.75"))
NLM_SCORE_BOOST = 0.05

# Agentic search config
AGENTIC_MODEL = os.getenv("AGENTIC_LLM_MODEL", "openai/zen/minimax-m2.5-free")
AGENTIC_DECOMPOSE_TIMEOUT = int(os.getenv("AGENTIC_DECOMPOSE_TIMEOUT", "8"))
AGENTIC_SYNTH_TIMEOUT = int(os.getenv("AGENTIC_SYNTH_TIMEOUT", "12"))
LITELLM_URL = os.getenv("LITELLM_URL", "http://localhost:4000")
LITELLM_KEY = os.getenv("LITELLM_MASTER_KEY", "")

# Alias map: common names/keywords → canonical project name in registry.
# Checked when the query doesn't contain an exact project name.
PROJECT_ALIASES: dict[str, str] = {
    # infra-mcp / Keystone
    "keystone": "infra-mcp",
    "infrastructure mesh": "infra-mcp",
    "infra mcp": "infra-mcp",
    # sariatu
    "sariatu": "sariatu-ops",
    # erudito
    "erudito": "erudito",
    # mesh monitor
    "mesh monitor": "mesh-monitor",
    "mesh-monitor": "mesh-monitor",
    # agent eval
    "agent eval": "agent-eval",
    "agent-eval": "agent-eval",
    # devops agent
    "devops agent": "devops-agent",
    "devops-agent": "devops-agent",
    # event bus
    "event bus": "event-bus",
    "event-bus": "event-bus",
    # marker mcp
    "marker": "marker-mcp",
    "marker mcp": "marker-mcp",
    # notebooklm / nlm
    "notebooklm mcp": "notebooklm-mcp",
    # labforge
    "labforge": "labforge",
    # jasper
    "jasper": "jasper",
    # node reporter
    "node reporter": "node-reporter",
    "node-reporter": "node-reporter",
    # qdrant mcp
    "qdrant mcp": "qdrant-mcp",
    "qdrant-mcp": "qdrant-mcp",
    # n8n
    "n8n": "n8n-hanzo",
    # openclaw / kubo
    "openclaw": "openclaw-kubo",
    "kubo": "openclaw-kubo",
}


def identify_project(query: str, registry) -> str | None:
    """Identify project from query text using aliases and registry names.

    Priority: exact registry name in query > alias match > partial name match.
    """
    query_lower = query.lower()

    # 1. Exact registry name appears in query (original logic)
    for name in registry.list_all():
        if name.lower() in query_lower:
            return name

    # 2. Alias match (longest alias first to prefer specific matches)
    for alias in sorted(PROJECT_ALIASES, key=len, reverse=True):
        if alias in query_lower:
            candidate = PROJECT_ALIASES[alias]
            if registry.get(candidate):
                return candidate

    return None


# Registry intent keywords — if query matches, answer from registry not Qdrant
_REGISTRY_PATTERNS = [
    (re.compile(r"\b(where|path|location|ubicad|located)\b.*\b(project|proyecto)\b", re.I), "path"),
    (re.compile(r"\b(which|what)\s+node\b", re.I), "node"),
    (re.compile(r"\bnotebook.?id\b", re.I), "notebook_id"),
    (re.compile(r"\b(status|estado)\b.*\b(project|proyecto)\b", re.I), "status"),
    (re.compile(r"\b(how many|cuantos)\s+(docs|documents|archivos)\b", re.I), "doc_count"),
    (re.compile(r"\bcuration.?(status|estado)\b", re.I), "curation_status"),
    (re.compile(r"\b(list|listar)\s+(all\s+)?(projects|proyectos)\b", re.I), "_list_all"),
]


def detect_registry_intent(query: str) -> str | None:
    """Detect if query can be answered from registry. Returns field name or None."""
    for pattern, field in _REGISTRY_PATTERNS:
        if pattern.search(query):
            return field
    return None


def answer_from_registry(query: str, field: str, registry, project: str | None = None) -> dict | None:
    """Answer a query directly from registry data."""
    if field == "_list_all":
        projects = {}
        for name in registry.list_all():
            entry = registry.get(name)
            if entry:
                projects[name] = {
                    "path": entry.get("path"),
                    "node": entry.get("node"),
                    "status": entry.get("status"),
                    "curation_status": entry.get("curation_status"),
                }
        return {
            "answer": projects,
            "confidence": "high",
            "sources": [{"type": "registry", "project": "", "file": "registry.yaml", "score": 1.0}],
            "project_identified": None,
            "nlm_consulted": False,
            "suggestion": None,
            "source_type": "registry",
        }

    # Try to identify project from query
    if not project and registry:
        project = identify_project(query, registry)

    if not project:
        return None

    entry = registry.get(project)
    if not entry:
        return None

    value = entry.get(field)
    if field == "path":
        answer = f"Project '{project}' is located at {value} on node {entry.get('node', 'unknown')}"
    elif field == "node":
        answer = f"Project '{project}' runs on node {entry.get('node', 'unknown')} at path {entry.get('path', 'unknown')}"
    elif field == "notebook_id":
        answer = f"Project '{project}' has NotebookLM notebook ID: {value or 'none (not yet synced)'}"
    elif field == "status":
        answer = f"Project '{project}' status: {entry.get('status')}, curation: {entry.get('curation_status', 'unknown')}"
    elif field == "doc_count":
        answer = f"Project '{project}' has {entry.get('doc_count', 0)} documents, {entry.get('curated_files', 0)} curated features"
    elif field == "curation_status":
        answer = f"Project '{project}' curation status: {entry.get('curation_status', 'unknown')}, curated at: {entry.get('curated_at', 'never')}"
    else:
        answer = str(value)

    return {
        "answer": answer,
        "confidence": "high",
        "sources": [{"type": "registry", "project": project, "file": "registry.yaml", "score": 1.0}],
        "project_identified": project,
        "nlm_consulted": False,
        "suggestion": None,
        "source_type": "registry",
    }


def _get_distill_source(payload: dict) -> str:
    """Get distill_source from payload, with from_nlm fallback for old data."""
    ds = payload.get("distill_source")
    if ds:
        return ds
    if payload.get("from_nlm"):
        return "nlm"
    return ""


def classify_confidence(score: float) -> str:
    """Classify score into confidence level."""
    if score >= CONFIDENCE_THRESHOLD:
        return "high"
    elif score >= 0.4:
        return "medium"
    return "low"


def build_response(
    query: str,
    sources: list[dict],
    nlm_answer: str | None = None,
    nlm_consulted: bool = False,
    project_identified: str | None = None,
) -> dict:
    """Build a QueryResponse dict from search results."""
    if not sources and not nlm_answer:
        return {
            "answer": "I don't have information on this topic. Would you like me to investigate?",
            "confidence": "low",
            "sources": [],
            "project_identified": project_identified,
            "nlm_consulted": nlm_consulted,
            "suggestion": "investigate_web" if project_identified else None,
        }

    best_score = sources[0]["score"] if sources else 0.0
    confidence = classify_confidence(best_score)

    if nlm_answer:
        answer = nlm_answer
        confidence = "medium" if confidence == "low" else confidence
    else:
        top_texts = [s["payload"].get("text", "") for s in sources[:3]]
        answer = "\n\n".join(top_texts)

    formatted_sources = []
    for s in sources[:5]:
        payload = s.get("payload", {})
        ds = _get_distill_source(payload)
        formatted_sources.append({
            "type": "nlm_note" if ds in ("nlm", "llm") else ("curated_doc" if ds == "direct" else ("memory" if ds == "atomic" else "qdrant")),
            "project": payload.get("project", ""),
            "file": payload.get("source", ""),
            "score": round(s["score"], 4),
        })

    return {
        "answer": answer,
        "confidence": confidence,
        "sources": formatted_sources,
        "project_identified": project_identified,
        "nlm_consulted": nlm_consulted,
        "suggestion": None,
    }


async def execute_query(
    query: str,
    project: str | None = None,
    top_k: int = 5,
    nlm_client=None,
    registry=None,
    mode: str = "default",
    max_tokens: int = 800,
) -> dict:
    """Execute a query with confidence routing.

    Modes:
    - "default": registry-first, then nlm_notes search
    - "dual": returns both Qdrant answer and NLM live answer as array
    - "agent": concise structured context for LLM system prompt injection
    """
    # Identify project from query if not explicitly provided
    if not project and registry:
        project = identify_project(query, registry)

    # Route 1: Registry queries (exact data)
    if registry:
        intent = detect_registry_intent(query)
        if intent:
            result = answer_from_registry(query, intent, registry, project)
            if result:
                return result

    # Route 2: Agentic mode (LLM decompose → multi-retrieve → synthesize)
    if mode == "agentic":
        return await _execute_agentic(query, project, top_k, registry, max_tokens)

    # Route 3: Dual mode (Qdrant + NLM live)
    if mode == "dual":
        return await _execute_dual(query, project, top_k, nlm_client, registry)

    # Route 4: Agent mode (concise structured context for LLM injection)
    if mode == "agent":
        return _execute_agent(query, project, top_k, max_tokens)

    # Route 4: Default search (nlm_notes only)
    query_embedding = embed_text(query)
    nlm_results = search(query_embedding, COLLECTION_NLM_NOTES, top_k, project)

    for r in nlm_results:
        r["payload"].setdefault("distill_source", "nlm")
        r["payload"]["from_nlm"] = True  # backwards compat

    top_results = nlm_results[:top_k]
    best_score = top_results[0]["score"] if top_results else 0.0

    # High confidence → respond directly
    if best_score >= CONFIDENCE_THRESHOLD:
        return build_response(query, top_results, project_identified=project)

    # Low confidence → try NLM live if available
    notebook_id = None
    if project and registry:
        entry = registry.get(project)
        if entry:
            notebook_id = entry.get("notebook_id")

    if notebook_id and nlm_client:
        try:
            nlm_answer = await nlm_client.query_notebook(notebook_id, query)
            if nlm_answer:
                note_embedding = embed_text(f"{query} {nlm_answer[:200]}")
                point_id = generate_point_id(f"nlm_live:{project}:{query[:50]}", 0)
                upsert_points([{
                    "id": point_id,
                    "vector": note_embedding,
                    "payload": {
                        "text": nlm_answer,
                        "source": f"nlm_live:{query[:80]}",
                        "project": project,
                        "distill_source": "nlm",
                        "from_nlm": True,  # backwards compat
                        "chunk_index": 0,
                    },
                }], COLLECTION_NLM_NOTES)

                return build_response(
                    query, top_results,
                    nlm_answer=nlm_answer,
                    nlm_consulted=True,
                    project_identified=project,
                )
        except Exception as e:
            logger.warning(f"NLM query failed: {e}")

    return build_response(query, top_results, nlm_consulted=False, project_identified=project)


async def _execute_dual(
    query: str,
    project: str | None,
    top_k: int,
    nlm_client,
    registry,
) -> dict:
    """Dual-source mode: returns Qdrant answer + NLM live answer."""
    # Qdrant search
    query_embedding = embed_text(query)
    nlm_results = search(query_embedding, COLLECTION_NLM_NOTES, top_k, project)

    qdrant_answer = {
        "answer": "\n\n".join(r["payload"].get("text", "") for r in nlm_results[:3]) if nlm_results else "No results in Qdrant",
        "confidence": classify_confidence(nlm_results[0]["score"] if nlm_results else 0.0),
        "sources": [
            {
                "type": "nlm_note",
                "project": r["payload"].get("project", ""),
                "score": round(r["score"], 4),
            }
            for r in nlm_results[:5]
        ],
    }

    # NLM live query
    nlm_live_answer = {"answer": None, "notebook_id": None, "source": "notebooklm"}
    notebook_id = None
    if project and registry:
        entry = registry.get(project)
        if entry:
            notebook_id = entry.get("notebook_id")

    if notebook_id and nlm_client:
        try:
            answer = await nlm_client.query_notebook(notebook_id, query)
            nlm_live_answer = {
                "answer": answer,
                "notebook_id": notebook_id,
                "source": "notebooklm",
            }
        except Exception as e:
            nlm_live_answer["answer"] = f"NLM query failed: {e}"
    elif not notebook_id:
        nlm_live_answer["answer"] = "No notebook available for this project"

    return {
        "mode": "dual",
        "query": query,
        "project_identified": project,
        "qdrant": qdrant_answer,
        "nlm_live": nlm_live_answer,
    }


def _format_agent_context(sources: list[dict], max_tokens: int) -> str:
    """Format search results as concise markdown context for LLM injection.

    Rules per spec 4.5:
    - Start with ## Relevant Knowledge header
    - Each source becomes a ### {Title} subsection
    - Factual and concise — no preamble
    - Code examples preserved verbatim
    - Each subsection ends with Source: {url/file}
    - Respects max_tokens — truncate least-relevant sections first
    """
    if not sources:
        return "No relevant knowledge found for this query."

    # Approximate char budget: 1 token ≈ 4 chars (English average)
    char_budget = max_tokens * 4
    sections = []

    for s in sources:
        payload = s.get("payload", {})
        text = payload.get("text", "").strip()
        source_file = payload.get("source", "unknown")
        project = payload.get("project", "")

        # Derive a title from the source filename
        title = source_file.replace("_", " ").replace("-", " ")
        # Clean up common suffixes
        for suffix in (".md", ".txt", ".html", ".pdf"):
            title = title.removesuffix(suffix)
        # Capitalize words for readability
        title = title.title()

        # Build section
        section = f"### {title}\n{text}\nSource: {source_file}"
        if project:
            section = f"### {title} ({project})\n{text}\nSource: {source_file}"

        sections.append(section)

    # Assemble with header, truncating least-relevant (last) sections first
    header = "## Relevant Knowledge\n\n"
    result = header
    for section in sections:
        candidate = result + section + "\n\n"
        if len(candidate) > char_budget and result != header:
            break
        result = candidate

    return result.rstrip()


def _execute_agent(
    query: str,
    project: str | None,
    top_k: int,
    max_tokens: int,
) -> dict:
    """Agent mode: concise structured context for LLM system prompt injection.

    No NLM live consultation (too slow for agent context injection).
    Uses distilled notes from Qdrant only.
    """
    try:
        query_embedding = embed_text(query)
        results = search(query_embedding, COLLECTION_NLM_NOTES, top_k, project)
    except Exception as e:
        logger.error(f"Agent mode search failed: {e}")
        return {
            "mode": "agent",
            "query": query,
            "project": project,
            "context": "No relevant knowledge found for this query.",
            "sources": [],
            "confidence": "none",
            "token_estimate": 0,
        }

    if not results:
        return {
            "mode": "agent",
            "query": query,
            "project": project,
            "context": "No relevant knowledge found for this query.",
            "sources": [],
            "confidence": "none",
            "token_estimate": 0,
        }

    best_score = results[0]["score"]
    confidence = classify_confidence(best_score)

    context = _format_agent_context(results, max_tokens)

    formatted_sources = []
    for s in results[:top_k]:
        payload = s.get("payload", {})
        ds = _get_distill_source(payload)
        formatted_sources.append({
            "title": payload.get("source", "unknown").replace("_", " ").replace("-", " ").title(),
            "project": payload.get("project", ""),
            "type": "nlm_note" if ds in ("nlm", "llm") else ("curated_doc" if ds == "direct" else ("memory" if ds == "atomic" else "qdrant")),
            "score": round(s["score"], 4),
            "url": payload.get("url", payload.get("source", "")),
        })

    token_estimate = len(context) // 4

    return {
        "mode": "agent",
        "query": query,
        "project": project,
        "context": context,
        "sources": formatted_sources,
        "confidence": confidence,
        "token_estimate": token_estimate,
    }


# ---------------------------------------------------------------------------
# Agentic search mode
# ---------------------------------------------------------------------------

async def _llm_call(messages: list[dict], timeout: int, max_tokens: int = 2000) -> str:
    """Call LiteLLM proxy. Returns response text. Raises on failure."""
    import litellm
    response = await litellm.acompletion(
        model=AGENTIC_MODEL,
        messages=messages,
        api_base=LITELLM_URL,
        api_key=LITELLM_KEY,
        timeout=timeout,
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content


async def _decompose_query(
    query: str, project: str | None, registry,
) -> list[dict]:
    """LLM decomposes a complex query into 1-3 focused sub-queries.

    Returns list of {"query": str, "project_hint": str | None}.
    Falls back to single original query on any failure.
    """
    fallback = [{"query": query, "project_hint": project}]

    # Build project list for context
    project_list = []
    if registry:
        try:
            project_list = registry.list_all()
        except Exception:
            pass

    system = (
        "You are a search query decomposer for a knowledge base. "
        "Given a user question, break it into 1-3 focused sub-queries optimized for semantic search.\n\n"
        "Output ONLY a JSON array: [{\"query\": \"...\", \"project_hint\": \"...\"}]\n"
        "- project_hint is the project name to scope the search, or null for cross-project.\n"
        f"- Available projects: {', '.join(project_list) if project_list else 'unknown'}\n"
        "- If the question is simple, return it as one sub-query (keep the full question, don't reduce to a single keyword).\n"
        "- Each sub-query should be a full descriptive phrase (5+ words), not single keywords.\n"
        "- Do NOT wrap in markdown code blocks. Return raw JSON only."
    )

    try:
        raw = await _llm_call(
            [{"role": "system", "content": system}, {"role": "user", "content": query}],
            timeout=AGENTIC_DECOMPOSE_TIMEOUT,
            max_tokens=500,
        )
        # Strip markdown fences if model wraps them
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)

        parsed = json.loads(cleaned)
        if not isinstance(parsed, list) or not parsed:
            return fallback

        sub_queries = []
        for item in parsed[:3]:  # cap at 3
            q = item.get("query", "").strip()
            if not q:
                continue
            sub_queries.append({
                "query": q,
                "project_hint": item.get("project_hint"),
            })
        return sub_queries if sub_queries else fallback
    except Exception as e:
        logger.warning(f"Agentic decompose failed, using fallback: {e}")
        return fallback


def _multi_retrieve(sub_queries: list[dict], top_k: int = 5) -> list[dict]:
    """Execute vector search for each sub-query, merge and deduplicate results."""
    seen: dict[str, dict] = {}  # key: (source, project) → best result

    for sq in sub_queries:
        try:
            embedding = embed_text(sq["query"])
            results = search(embedding, COLLECTION_NLM_NOTES, top_k, sq.get("project_hint"))
            for r in results:
                payload = r.get("payload", {})
                key = f"{payload.get('source', '')}::{payload.get('project', '')}"
                if key not in seen or r["score"] > seen[key]["score"]:
                    seen[key] = r
        except Exception as e:
            logger.warning(f"Agentic retrieve failed for sub-query '{sq['query'][:50]}': {e}")

    # Sort by score descending, return more results for synthesis context
    merged = sorted(seen.values(), key=lambda x: x["score"], reverse=True)
    return merged[:top_k * 2]


async def _synthesize_answer(
    query: str, results: list[dict], max_tokens: int,
) -> str:
    """LLM synthesizes a final answer from search results with citations."""
    context = _format_agent_context(results, max_tokens)

    system = (
        "You are a knowledge assistant for an AI infrastructure lab. "
        "Synthesize a clear, concise answer using ONLY the provided context.\n\n"
        "Rules:\n"
        "- Cite sources using [Source: filename] format\n"
        "- If context is insufficient, say so explicitly\n"
        "- Be concise and factual\n"
        "- Preserve code examples if present\n"
        "- Answer in the same language as the question"
    )

    user_msg = f"Question: {query}\n\n{context}"

    return await _llm_call(
        [{"role": "system", "content": system}, {"role": "user", "content": user_msg}],
        timeout=AGENTIC_SYNTH_TIMEOUT,
        max_tokens=max_tokens,
    )


async def _execute_agentic(
    query: str,
    project: str | None,
    top_k: int,
    registry,
    max_tokens: int,
) -> dict:
    """Agentic search: decompose → multi-retrieve → synthesize."""
    start = time.monotonic()

    # Step 1: Decompose
    sub_queries = await _decompose_query(query, project, registry)
    # Propagate identified project to sub-queries without a hint
    if project:
        for sq in sub_queries:
            if not sq.get("project_hint"):
                sq["project_hint"] = project
    reasoning_steps = [sq["query"] for sq in sub_queries]

    # Step 2: Multi-retrieve
    results = _multi_retrieve(sub_queries, top_k)

    if not results:
        return {
            "mode": "agentic",
            "query": query,
            "answer": "No relevant knowledge found.",
            "reasoning_steps": reasoning_steps,
            "sources": [],
            "confidence": "none",
            "elapsed_ms": int((time.monotonic() - start) * 1000),
        }

    best_score = results[0]["score"]
    confidence = classify_confidence(best_score)

    # Step 3: Synthesize
    try:
        answer = await _synthesize_answer(query, results, max_tokens)
    except Exception as e:
        logger.warning(f"Agentic synthesis failed, returning raw context: {e}")
        answer = _format_agent_context(results, max_tokens)

    # Format sources (same shape as agent mode)
    formatted_sources = []
    for s in results[:top_k]:
        payload = s.get("payload", {})
        ds = _get_distill_source(payload)
        formatted_sources.append({
            "title": payload.get("source", "unknown").replace("_", " ").replace("-", " ").title(),
            "project": payload.get("project", ""),
            "type": "nlm_note" if ds in ("nlm", "llm") else ("curated_doc" if ds == "direct" else ("memory" if ds == "atomic" else "qdrant")),
            "score": round(s["score"], 4),
            "url": payload.get("url", payload.get("source", "")),
        })

    return {
        "mode": "agentic",
        "query": query,
        "answer": answer,
        "reasoning_steps": reasoning_steps,
        "sources": formatted_sources,
        "confidence": confidence,
        "elapsed_ms": int((time.monotonic() - start) * 1000),
    }
