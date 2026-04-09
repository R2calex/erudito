"""File classifier: routes documentation from inbox to project repos.

Reads files from an inbox directory (e.g., claude_contracts),
classifies each to a project, and moves it to {project_path}/docs/contracts/.
"""
import os
import re
import shutil
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("erudito.classifier")

# Keyword → project mapping.
# Order matters: more specific patterns first.
# Keywords are matched against the normalized filename (lowercase, prefix/date stripped).
KEYWORD_MAP = [
    # Erudito
    (r"erudito", "erudito"),
    # Infra-MCP / Keystone
    (r"keystone", "infra-mcp"),
    (r"infra-consolidation", "infra-mcp"),
    (r"placement-engine", "infra-mcp"),
    (r"node-awareness", "infra-mcp"),
    (r"node-bootstrap", "infra-mcp"),
    (r"remote-deploy", "infra-mcp"),
    (r"port-conflict", "infra-mcp"),
    # Mesh Monitor
    (r"mesh-monitor", "mesh-monitor"),
    (r"auto-remediation", "mesh-monitor"),
    # Event Bus / Mesh Channel
    (r"event-bus", "event-bus"),
    (r"mesh-channel", "event-bus"),
    # DevOps Agent
    (r"devops-agent", "devops-agent"),
    (r"agent-bootstrap", "devops-agent"),
    # Marker MCP
    (r"marker-mcp", "marker-mcp"),
    # Qdrant MCP
    (r"qdrant-mcp", "qdrant-mcp"),
    # Jasper (all milestones + related)
    (r"jasper", "jasper"),
    (r"jimbo", "jasper"),
    # Agent Eval
    (r"agent-eval", "agent-eval"),
    # LiteLLM
    (r"litellm", "litellm"),
    (r"guardrail", "litellm"),
    # N8N
    (r"n8n", "n8n"),
    # Kubo
    (r"kubo", "kubo"),
    # Sariatu
    (r"sariatu", "sariatu"),
    # OpenClaw
    (r"openclaw", "openclaw"),
    # MCP general
    (r"mcp-install", "infra-mcp"),
]

# Prefixes to strip for keyword extraction
_PREFIX_RE = re.compile(
    r"^(SPEC|IR|SOP|PLAN|CONTRACT|REPORT|REVIEW|REPORT-SPEC)-", re.IGNORECASE
)
_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}-|\d{8}-)")


def _normalize_filename(filename: str) -> str:
    """Strip prefix, date, extension and normalize for keyword matching."""
    name = os.path.basename(filename)
    if name.endswith(".md"):
        name = name[:-3]
    name = _PREFIX_RE.sub("", name)
    name = _DATE_RE.sub("", name)
    return name.lower().strip("-_")


def classify_file(filename: str) -> str | None:
    """Classify a file to a project using keyword matching.

    Returns project name or None if unclassified.
    """
    normalized = _normalize_filename(filename)
    if not normalized:
        return None

    for pattern, project in KEYWORD_MAP:
        if re.search(pattern, normalized):
            return project

    return None


@dataclass
class ClassificationResult:
    """Result of classifying inbox files."""
    classified: dict = field(default_factory=dict)   # project → [files]
    unclassified: list = field(default_factory=list)  # files that couldn't be classified
    moved: int = 0
    errors: list = field(default_factory=list)


def classify_inbox(inbox_path: str) -> ClassificationResult:
    """Classify all .md files in the inbox directory.

    Returns a ClassificationResult with files grouped by project.
    Does NOT move files — use move_classified() for that.
    """
    result = ClassificationResult()
    inbox = Path(inbox_path)

    if not inbox.is_dir():
        result.errors.append(f"Inbox not found: {inbox_path}")
        return result

    for md_file in sorted(inbox.glob("*.md")):
        project = classify_file(md_file.name)
        if project:
            result.classified.setdefault(project, []).append(md_file.name)
        else:
            result.unclassified.append(md_file.name)

    return result


def move_classified(
    inbox_path: str,
    classification: ClassificationResult,
    project_paths: dict[str, str],
    dest_subdir: str = "docs/contracts",
    dry_run: bool = False,
) -> ClassificationResult:
    """Move classified files from inbox to their project directories.

    Args:
        inbox_path: Source directory (e.g., claude_contracts)
        classification: Result from classify_inbox()
        project_paths: Mapping of project name → project root path
        dest_subdir: Subdirectory within project to place files
        dry_run: If True, don't actually move files

    Returns: Updated ClassificationResult with move count.
    """
    inbox = Path(inbox_path)

    for project, files in classification.classified.items():
        project_path = project_paths.get(project)
        if not project_path:
            logger.warning(f"Project '{project}' not in registry, skipping {len(files)} files")
            classification.errors.append(f"Project '{project}' not registered: {files}")
            continue

        dest_dir = Path(os.path.expanduser(project_path)) / dest_subdir
        if not dry_run:
            dest_dir.mkdir(parents=True, exist_ok=True)

        for filename in files:
            src = inbox / filename
            dst = dest_dir / filename

            if not src.exists():
                continue

            if dst.exists():
                logger.debug(f"File already exists at destination: {dst}")
                # Still remove from inbox to avoid re-processing
                if not dry_run:
                    src.unlink()
                    classification.moved += 1
                continue

            if dry_run:
                logger.info(f"[DRY RUN] {filename} → {project} ({dest_dir})")
                classification.moved += 1
            else:
                try:
                    shutil.move(str(src), str(dst))
                    classification.moved += 1
                    logger.info(f"Moved {filename} → {project} ({dest_dir})")
                except Exception as e:
                    logger.error(f"Failed to move {filename}: {e}")
                    classification.errors.append(f"Move failed: {filename} → {e}")

    return classification
