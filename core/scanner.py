"""Delta detection via git hash comparison.

Scans project repos for .md file changes since the last known commit.
Produces Delta objects consumed by Indexer and NotebookLM cycle.
"""
import logging
import os
import subprocess
from datetime import datetime, timezone
from typing import Optional

from core.enricher import enrich_content

logger = logging.getLogger("erudito.scanner")

# v3 scans only .md files
SCAN_EXTENSIONS = {".md"}
EXCLUDED_FILES = {".env", "credentials.json", "auth-profiles.json", "join_token.txt"}
EXCLUDED_DIRS = {"node_modules", ".git", "__pycache__", ".pytest_cache", "venv", ".venv", "data", ".superpowers"}


def should_scan_file(filepath: str) -> bool:
    """Check if a file should be scanned based on extension and exclusion rules."""
    basename = os.path.basename(filepath)
    if basename in EXCLUDED_FILES:
        return False
    parts = filepath.replace("\\", "/").split("/")
    if any(d in EXCLUDED_DIRS for d in parts):
        return False
    _, ext = os.path.splitext(filepath)
    return ext in SCAN_EXTENSIONS


def find_git_root(path: str) -> str | None:
    """Find the git root directory for a given path."""
    try:
        result = subprocess.run(
            ["git", "-C", path, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _git_head(repo_path: str) -> str | None:
    """Get HEAD commit hash for a repo."""
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _git_date(repo_path: str) -> str:
    """Get the date of the last commit."""
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "log", "-1", "--format=%ci"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()[:10]  # YYYY-MM-DD
    except Exception:
        pass
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def compute_delta(
    project_name: str,
    repo_path: str,
    last_hash: str | None,
) -> dict | None:
    """Compute what .md files changed since last_hash.

    Returns a Delta dict or None if no changes.
    Does NOT sanitize — caller must sanitize before passing to consumers.
    """
    head = _git_head(repo_path)
    if not head:
        logger.warning(f"Cannot get HEAD for {project_name} at {repo_path}")
        return None

    if head == last_hash:
        return None  # No changes

    git_date = _git_date(repo_path)

    if last_hash:
        # Incremental: diff since last known commit
        try:
            result = subprocess.run(
                ["git", "-C", repo_path, "diff", "--name-status", f"{last_hash}..{head}", "--", "*.md"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                logger.warning(f"git diff failed for {project_name}: {result.stderr}")
                return None
        except Exception as e:
            logger.warning(f"git diff error for {project_name}: {e}")
            return None

        files = []
        for line in result.stdout.strip().splitlines():
            if not line:
                continue
            parts = line.split("\t", 1)
            if len(parts) != 2:
                continue
            status, filepath = parts
            if not should_scan_file(filepath):
                continue

            if status.startswith("D"):
                files.append({"path": filepath, "action": "deleted", "content": "", "auto_enriched": False})
            else:
                action = "added" if status.startswith("A") else "modified"
                full_path = os.path.join(repo_path, filepath)
                try:
                    with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                    enriched_content, was_enriched = enrich_content(content, filepath, project_name, git_date)
                    files.append({
                        "path": filepath,
                        "action": action,
                        "content": enriched_content,
                        "auto_enriched": was_enriched,
                    })
                except FileNotFoundError:
                    logger.warning(f"File not found (possibly deleted): {full_path}")
    else:
        # Full scan: index all .md files
        files = []
        for root, dirs, filenames in os.walk(repo_path):
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
            for fname in filenames:
                rel_path = os.path.relpath(os.path.join(root, fname), repo_path)
                if not should_scan_file(rel_path):
                    continue
                full_path = os.path.join(root, fname)
                try:
                    with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                    enriched_content, was_enriched = enrich_content(content, fname, project_name, git_date)
                    files.append({
                        "path": rel_path,
                        "action": "added",
                        "content": enriched_content,
                        "auto_enriched": was_enriched,
                    })
                except Exception as e:
                    logger.warning(f"Error reading {full_path}: {e}")

    if not files:
        return None

    return {
        "project": project_name,
        "old_hash": last_hash,
        "new_hash": head,
        "files": files,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
