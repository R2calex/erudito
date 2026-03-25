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

# Match each top-level answer start: captures the number (1-5)
# Strict: only matches at line start with optional markdown header prefix
# The prompt asks for plain "1. " format, but we also accept "## 1." and "**1.**"
_ANSWER_START_RE = re.compile(
    r"^(?:#{1,3} *)?(?:\*\*)?([1-5])\.(?:\*\*)?\s+", re.MULTILINE
)


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
        "contain enough information to answer, say \"Insufficient documentation.\"\n\n"
        "IMPORTANT: Use EXACTLY the format below. Start each answer with the number "
        "followed by a period (e.g., '1. '). Do NOT use markdown headers, bold, or "
        "sub-lists with numbers. Write each answer as a single paragraph.\n"
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

    # Find the FIRST occurrence of each number 1-5 in order
    # This handles LLMs that use sub-lists (e.g., "## 2." inside answer 1)
    text = raw.strip()
    matches = list(_ANSWER_START_RE.finditer(text))

    # Keep only the first match for each sequential number 1-5
    seen = set()
    ordered_matches = []
    for m in matches:
        num = int(m.group(1))
        expected_next = len(ordered_matches) + 1
        if num == expected_next and num not in seen:
            seen.add(num)
            ordered_matches.append(m)

    if len(ordered_matches) != len(DISTILL_QUESTIONS):
        logger.warning(
            f"LLM response for {project_name}: expected {len(DISTILL_QUESTIONS)} "
            f"sequential answers, found {len(ordered_matches)}"
        )
        return []

    # Extract text between matches
    answers = []
    for i, m in enumerate(ordered_matches):
        start = m.end()
        end = ordered_matches[i + 1].start() if i + 1 < len(ordered_matches) else len(text)
        answers.append(text[start:end].strip())

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
