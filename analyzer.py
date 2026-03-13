"""Erudito LLM Analyzer — knowledge extraction from text chunks.

Uses LiteLLM proxy with model cascade: local first, then free remote.
Zero external dependencies (stdlib only).
"""

import json
import os
import urllib.request
from typing import Dict, List, Optional, Tuple

LITELLM_URL = os.getenv("LITELLM_URL", "http://localhost:4001/v1")
LITELLM_KEY = os.getenv("LITELLM_API_KEY", "")

# Model cascade: try local first, then free remote
# Models tagged as "thinking" use reasoning_content and need higher token budgets.
MODEL_CASCADE = [
    {"id": "lmstudio/qwen/qwen3.5-9b", "thinking": True},
    {"id": "zen/nemotron-3-super-free", "thinking": False},
    {"id": "zen/mimo-v2-flash-free",    "thinking": False},
    {"id": "zen/minimax-m2.5",          "thinking": False},
]

# Thinking models spend tokens on internal reasoning before producing content.
# We multiply the requested max_tokens by this factor for thinking models.
_THINKING_TOKEN_MULTIPLIER = 6

# --- Prompts ---

EXTRACTION_SYSTEM = """You are a knowledge curator for the KUBO AI-Lab infrastructure.
Your job is to analyze text chunks and extract structured knowledge.

Respond ONLY with valid JSON, no markdown, no explanation."""

EXTRACTION_PROMPT = """Analyze this text chunk from file "{source}" (type: {doc_type}):

---
{text}
---

Extract structured knowledge. Return JSON:
{{
  "summary": "1-2 sentence summary of what this chunk teaches",
  "concepts": ["list", "of", "key", "concepts"],
  "type": "spec|sop|incident_report|code_pattern|config|architecture|general",
  "relevance_score": 0.0-1.0,
  "tags": ["relevant", "tags"],
  "dependencies": ["things this depends on or relates to"]
}}

Rules:
- relevance_score: 0.0 = boilerplate/noise, 1.0 = critical knowledge
- Be concise in summary
- Extract 2-5 concepts max
- If this is just a file header, import list, or formatting, set relevance_score < 0.3"""

CATALOG_SYSTEM = EXTRACTION_SYSTEM

CATALOG_PROMPT = """Analyze this project directory and generate a catalog entry.

Files found:
{files_content}

Return JSON:
{{
  "name": "project name",
  "description": "1-2 sentence description",
  "type": "mvp|library|infrastructure|experiment|documentation",
  "stack": ["tech1", "tech2"],
  "status": "active|stable|deprecated|experimental",
  "tags": ["relevant", "tags"]
}}"""

# --- LLM Client ---


def _llm_call(
    prompt: str,
    system_prompt: str = "",
    max_tokens: int = 500,
    temperature: float = 0.1,
) -> Tuple[Optional[str], Optional[str]]:
    """Call LLM via LiteLLM proxy with cascade fallback.

    Returns (response_text, model_used) or (None, None) if all fail.
    """
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    for model_info in MODEL_CASCADE:
        model_id = model_info["id"]
        is_thinking = model_info["thinking"]
        effective_max = max_tokens * _THINKING_TOKEN_MULTIPLIER if is_thinking else max_tokens
        timeout = 60 if is_thinking else 30

        try:
            payload = json.dumps({
                "model": model_id,
                "messages": messages,
                "max_tokens": effective_max,
                "temperature": temperature,
            }).encode()

            headers = {"Content-Type": "application/json"}
            if LITELLM_KEY:
                headers["Authorization"] = f"Bearer {LITELLM_KEY}"

            req = urllib.request.Request(
                f"{LITELLM_URL}/chat/completions",
                data=payload,
                headers=headers,
            )
            resp = urllib.request.urlopen(req, timeout=timeout)
            data = json.loads(resp.read())
            content = (data["choices"][0]["message"].get("content") or "").strip()

            # Thinking models may exhaust tokens on reasoning, producing empty content
            if not content:
                continue

            return content, model_id
        except Exception:
            continue  # Try next model in cascade

    return None, None  # All models failed


# --- Analysis Functions ---


def _parse_json_response(response: str) -> Optional[dict]:
    """Parse JSON from LLM response, handling markdown wrapping."""
    clean = response.strip()
    if clean.startswith("```"):
        clean = clean.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(clean)


def analyze_chunk(
    text: str, source: str, doc_type: str = "docs"
) -> Tuple[dict, Optional[str]]:
    """Analyze a single chunk via LLM. Returns (analysis_dict, model_used)."""
    default = {
        "relevance_score": 0.5,
        "summary": "",
        "concepts": [],
        "type": "general",
        "tags": [],
        "dependencies": [],
    }

    # Skip very short chunks
    if len(text.strip()) < 100:
        return {
            **default,
            "relevance_score": 0.1,
            "summary": "Too short",
        }, "skipped"

    prompt = EXTRACTION_PROMPT.format(
        source=os.path.basename(source),
        doc_type=doc_type,
        text=text[:2000],  # Cap input to control costs
    )

    response, model = _llm_call(prompt, EXTRACTION_SYSTEM, max_tokens=300)

    if response is None:
        return {**default, "summary": "LLM unavailable"}, None

    try:
        analysis = _parse_json_response(response)

        # Validate and normalize
        analysis["relevance_score"] = max(
            0.0, min(1.0, float(analysis.get("relevance_score", 0.5)))
        )
        analysis.setdefault("summary", "")
        analysis.setdefault("concepts", [])
        analysis.setdefault("type", "general")
        analysis.setdefault("tags", [])
        analysis.setdefault("dependencies", [])

        return analysis, model
    except (json.JSONDecodeError, KeyError, ValueError, AttributeError):
        return {
            **default,
            "summary": response[:200] if response else "",
        }, model


def batch_analyze(
    chunks: List[dict],
    source: str,
    doc_type: str = "docs",
    min_relevance: float = 0.3,
) -> List[dict]:
    """Analyze multiple chunks, filter by relevance.

    Each chunk dict must have a 'text' key.
    Returns list of {chunk, analysis, action, model}.
    """
    results = []
    for chunk in chunks:
        analysis, model = analyze_chunk(chunk["text"], source, doc_type)

        if analysis["relevance_score"] < min_relevance:
            results.append({
                "chunk": chunk,
                "analysis": analysis,
                "action": "skip",
                "model": model,
            })
        else:
            enriched = {
                **chunk,
                "summary": analysis["summary"],
                "concepts": analysis["concepts"],
                "doc_type": analysis["type"],
                "tags": analysis["tags"],
                "dependencies": analysis["dependencies"],
                "relevance_score": analysis["relevance_score"],
            }
            results.append({
                "chunk": enriched,
                "analysis": analysis,
                "action": "index",
                "model": model,
            })

    return results


def analyze_for_catalog(
    source_path: str, file_list: List[str]
) -> Optional[dict]:
    """Analyze a project directory to generate catalog entry."""
    context_parts = []
    for f in file_list[:10]:  # Cap at 10 files
        full = os.path.join(source_path, f)
        if os.path.isfile(full):
            try:
                with open(full, encoding="utf-8", errors="ignore") as fh:
                    content = fh.read()[:1000]
                    context_parts.append(f"=== {f} ===\n{content}")
            except Exception:
                continue

    if not context_parts:
        return None

    prompt = CATALOG_PROMPT.format(files_content="\n".join(context_parts))

    response, model = _llm_call(prompt, CATALOG_SYSTEM, max_tokens=200)
    if response:
        try:
            return _parse_json_response(response)
        except (json.JSONDecodeError, ValueError):
            pass
    return None
