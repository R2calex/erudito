"""Auto-enrich missing frontmatter metadata for .md files.

Enrichment happens in memory — original files are never modified.
"""
import re
from typing import Optional

REQUIRED_FIELDS = {"project", "type", "last_updated", "status"}

_TYPE_RULES = [
    (re.compile(r"README", re.IGNORECASE), "readme"),
    (re.compile(r"spec", re.IGNORECASE), "spec"),
    (re.compile(r"\bsop\b", re.IGNORECASE), "sop"),
    (re.compile(r"\bir\b|implementation.report", re.IGNORECASE), "ir"),
    (re.compile(r"backlog", re.IGNORECASE), "backlog"),
    (re.compile(r"design", re.IGNORECASE), "design"),
]


def parse_frontmatter(content: str) -> tuple[dict, str]:
    if not content or not content.startswith("---"):
        return {}, content
    end = content.find("---", 3)
    if end == -1:
        return {}, content
    raw = content[3:end].strip()
    body = content[end + 3:].lstrip("\n")
    fm = {}
    for line in raw.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            fm[key.strip()] = val.strip()
    return fm, body


def infer_type(filename: str) -> str:
    for pattern, doc_type in _TYPE_RULES:
        if pattern.search(filename):
            return doc_type
    return "doc"


def enrich_content(
    content: str,
    filename: str,
    project_name: str,
    git_date: str,
    status: str = "active",
) -> tuple[str, bool]:
    fm, body = parse_frontmatter(content)
    missing = REQUIRED_FIELDS - set(fm.keys())
    if not missing:
        return content, False
    if "project" not in fm:
        fm["project"] = project_name
    if "type" not in fm:
        fm["type"] = infer_type(filename)
    if "last_updated" not in fm:
        fm["last_updated"] = git_date
    if "status" not in fm:
        fm["status"] = status
    fm["auto_enriched"] = "true"
    lines = ["---"]
    for key in ["project", "type", "last_updated", "status", "auto_enriched"]:
        if key in fm:
            lines.append(f"{key}: {fm[key]}")
    for key, val in fm.items():
        if key not in {"project", "type", "last_updated", "status", "auto_enriched"}:
            lines.append(f"{key}: {val}")
    lines.append("---")
    lines.append(body)
    return "\n".join(lines), True
