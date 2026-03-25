"""Knowledge distillation: tier assignment, LLM prompt building, response parsing.

Canonical DISTILL_QUESTIONS are the single source of truth for all tiers.
"""
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger("erudito.distiller")

# Canonical questions — used by all distillation backends (NLM, LLM, Direct)
DISTILL_QUESTIONS = [
    "What is the current architecture and main components of this project?",
    "What recent changes have occurred and why?",
    "What external dependencies and integrations does this project have?",
    "What is the current operational status and any known issues?",
    "Provide a comprehensive summary of this project for someone unfamiliar with it.",
]

# Tier thresholds (feature count from curator output)
TIER1_THRESHOLD = int(os.getenv("TIER1_THRESHOLD", "5"))
TIER2_THRESHOLD = int(os.getenv("TIER2_THRESHOLD", "3"))

# LLM config
DISTILL_LLM_MODEL = os.getenv("DISTILL_LLM_MODEL", "openai/zen/minimax-m2.5")
DISTILL_LLM_TIMEOUT = int(os.getenv("DISTILL_LLM_TIMEOUT", "120"))
DISTILL_LLM_MAX_TOKENS = int(os.getenv("DISTILL_LLM_MAX_TOKENS", "4000"))
LITELLM_URL = os.getenv("LITELLM_URL", "http://localhost:4000")

# Only match top-level question numbers (1-5), not sub-lists within answers
_ANSWER_RE = re.compile(r"^[1-5]\.\s+", re.MULTILINE)


def compute_tier(project_name: str, curated_dir: str, registry_entry: dict) -> int:
    """Determine tier from feature count. Registry override wins.

    Each .md file in curated_dir is one feature group from the curator.
    """
    override = registry_entry.get("tier")
    if override is not None:
        return override

    curated_path = Path(curated_dir)
    if not curated_path.exists():
        logger.warning(f"Curated dir not found for {project_name}: {curated_dir}")
        return 3

    feature_count = len(list(curated_path.glob("*.md")))

    if feature_count >= TIER1_THRESHOLD:
        return 1
    elif feature_count >= TIER2_THRESHOLD:
        return 2
    else:
        return 3


def build_llm_prompt(
    project_name: str,
    curated_files: list[dict],
    nlm_notes: list[dict] | None,
) -> dict:
    """Build system + user prompts for LLM distillation.

    Args:
        project_name: Name of the project
        curated_files: List of {"name": str, "content": str}
        nlm_notes: Existing NLM notes for context (Tier 1 deltas) or None (Tier 2)

    Returns:
        {"system": str, "user": str}
    """
    system = (
        f"You are a knowledge distillation agent for the {project_name} project. "
        "Your task is to analyze the project documentation and produce structured knowledge notes. "
        "Answer each question based ONLY on the provided documentation. "
        "All output must be in English."
    )

    parts = []

    if nlm_notes:
        parts.append("## Existing Project Understanding (from prior deep analysis)\n")
        for note in nlm_notes:
            parts.append(f"Q: {note['question']}\nA: {note['answer']}\n")
        parts.append("\n## New/Updated Documentation\n")

    for f in curated_files:
        parts.append(f"### {f['name']}\n{f['content']}\n")

    parts.append(
        "\nAnswer each of the following questions. If the documentation doesn't "
        "contain enough information to answer, say \"Insufficient documentation.\"\n"
    )
    for i, q in enumerate(DISTILL_QUESTIONS, 1):
        parts.append(f"{i}. {q}")

    return {"system": system, "user": "\n".join(parts)}


def parse_llm_response(raw: str, project_name: str) -> list[dict]:
    """Parse numbered LLM response into note dicts matching canonical payload schema.

    Returns empty list if response is empty or unparseable.
    Rejects answers shorter than 20 characters.
    """
    if not raw or not raw.strip():
        return []

    # Split by numbered pattern (1. 2. 3. etc.)
    segments = _ANSWER_RE.split(raw.strip())
    # First segment is empty or preamble before "1."
    answers = [s.strip() for s in segments[1:] if s.strip()]

    if len(answers) != len(DISTILL_QUESTIONS):
        logger.warning(
            f"LLM response for {project_name}: expected {len(DISTILL_QUESTIONS)} "
            f"answers, got {len(answers)}"
        )
        return []

    notes = []
    for i, (question, answer) in enumerate(zip(DISTILL_QUESTIONS, answers)):
        if len(answer) < 20:
            logger.warning(f"Rejecting short answer for {project_name} Q{i+1}: {answer[:50]}")
            continue
        notes.append({
            "text": answer,
            "question": question,
            "source": "llm",
            "distill_source": "llm",
            "project": project_name,
            "type": "qa",
            "model": DISTILL_LLM_MODEL,
            "canonical": True,
            "chunk_index": i,
        })

    # If any answers were rejected, return empty (all-or-nothing for consistency)
    if len(notes) != len(DISTILL_QUESTIONS):
        return []

    return notes
