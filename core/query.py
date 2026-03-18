"""Search + confidence routing with NotebookLM escalation.

Searches both agent_knowledge and nlm_notes collections.
Routes to NotebookLM when confidence is below threshold (0.75).
"""
import logging
import os
from typing import Optional

from core.indexer import embed_text, search, upsert_points, generate_point_id, COLLECTION_KNOWLEDGE, COLLECTION_NLM_NOTES

logger = logging.getLogger("erudito.query")

CONFIDENCE_THRESHOLD = float(os.getenv("ERUDITO_CONFIDENCE_THRESHOLD", "0.75"))
NLM_SCORE_BOOST = 0.05


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
        # Compose answer from top sources
        top_texts = [s["payload"].get("text", "") for s in sources[:3]]
        answer = "\n\n".join(top_texts)

    formatted_sources = []
    for s in sources[:5]:
        payload = s.get("payload", {})
        formatted_sources.append({
            "type": "nlm_note" if payload.get("from_nlm") else "qdrant",
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
) -> dict:
    """Execute a query with confidence routing.

    1. Search Qdrant (both collections)
    2. If high confidence → respond directly
    3. If low confidence + notebook available → consult NotebookLM
    4. If nothing → "I don't know"
    """
    query_embedding = embed_text(query)

    # Search both collections
    knowledge_results = search(query_embedding, COLLECTION_KNOWLEDGE, top_k, project)
    nlm_results = search(query_embedding, COLLECTION_NLM_NOTES, top_k, project)

    # Boost nlm_notes scores and mark them
    for r in nlm_results:
        r["score"] = min(r["score"] + NLM_SCORE_BOOST, 1.0)
        r["payload"]["from_nlm"] = True

    # Combine and sort
    all_results = knowledge_results + nlm_results
    all_results.sort(key=lambda x: x["score"], reverse=True)
    top_results = all_results[:top_k]

    best_score = top_results[0]["score"] if top_results else 0.0

    # High confidence → respond directly
    if best_score >= CONFIDENCE_THRESHOLD:
        return build_response(query, top_results, project_identified=project)

    # Low confidence → try NotebookLM if available
    notebook_id = None
    if project and registry:
        entry = registry.get(project)
        if entry:
            notebook_id = entry.get("notebook_id")

    if notebook_id and nlm_client:
        try:
            nlm_answer = await nlm_client.query_notebook(notebook_id, query)
            if nlm_answer:
                # Save to Qdrant for future queries
                note_embedding = embed_text(nlm_answer)
                point_id = generate_point_id(f"nlm_live:{project}:{query[:50]}", 0)
                upsert_points([{
                    "id": point_id,
                    "vector": note_embedding,
                    "payload": {
                        "text": nlm_answer,
                        "source": f"nlm_live:{query[:80]}",
                        "project": project,
                        "from_nlm": True,
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

    # Respond with what we have
    return build_response(query, top_results, nlm_consulted=False, project_identified=project)
