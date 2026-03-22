"""Data curation pipeline: feature grouping + consolidation.

Groups .md files by feature/topic, consolidates into standardized
documents with metadata for NotebookLM ingestion.
"""
import os
import re
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

import yaml

logger = logging.getLogger("erudito.curator")

# Known document type prefixes and their section titles + sort order
DOC_TYPES = {
    "SPEC": ("Diseño", 1),
    "PLAN": ("Plan", 2),
    "IR": ("Implementación", 3),
    "SOP": ("Operación", 4),
    "CONTRACT": ("Contrato", 5),
    "REPORT": ("Reporte", 6),
    "REVIEW": ("Revisión", 7),
}

_PREFIX_RE = re.compile(
    r"^(SPEC|IR|SOP|PLAN|CONTRACT|REPORT|REVIEW)-", re.IGNORECASE
)
_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}-|\d{8}-)")


def extract_feature_name(filename: str) -> str:
    """Extract normalized feature name from a filename (or path)."""
    # Use only the basename if a path is provided
    name = os.path.basename(filename)
    if name.endswith(".md"):
        name = name[:-3]
    name = _PREFIX_RE.sub("", name)
    name = _DATE_RE.sub("", name)
    name = name.lower().strip("-")
    return name if name else filename.lower().replace(".md", "")


MAX_FEATURES = int(os.getenv("MAX_CURATED_FEATURES", "45"))


def group_by_feature(filenames: list[str]) -> dict[str, list[str]]:
    """Group filenames by shared feature prefix, with directory fallback.

    Strategy:
    1. Group by prefix (keyword-based, handles SPEC-X, IR-X, SOP-X patterns)
    2. If too many orphans, fall back to directory-based grouping
    3. If still over MAX_FEATURES, merge smallest orphans into a misc group
    """
    if not filenames:
        return {}

    name_map = {f: extract_feature_name(f) for f in filenames}

    # Step 1: group by first token of extracted name
    first_token_groups: dict[str, list[str]] = {}
    for filename in filenames:
        name = name_map[filename]
        # Split on both hyphens and underscores for broader matching
        first_token = re.split(r"[-_]", name)[0]
        first_token_groups.setdefault(first_token, []).append(filename)

    # Step 2: determine feature key per first-token group
    groups: dict[str, list[str]] = {}
    for first_token, group_files in first_token_groups.items():
        if len(group_files) == 1:
            feature = first_token
        else:
            names = [name_map[f] for f in group_files]
            split_names = [re.split(r"[-_]", n) for n in names]
            min_len = min(len(parts) for parts in split_names)
            common_len = 0
            for i in range(min_len):
                if all(parts[i] == split_names[0][i] for parts in split_names):
                    common_len = i + 1
                else:
                    break
            feature = "-".join(split_names[0][:common_len]) if common_len > 0 else first_token
        groups.setdefault(feature, []).extend(group_files)

    # Step 3: Directory-based fallback for orphan files (groups with 1 file)
    if len(groups) > MAX_FEATURES:
        orphan_features = [k for k, v in groups.items() if len(v) == 1]
        if orphan_features:
            # Re-group orphans by parent directory
            dir_groups: dict[str, list[str]] = {}
            for feature in orphan_features:
                filename = groups[feature][0]
                parent = os.path.dirname(filename)
                dir_name = os.path.basename(parent) if parent else "root"
                dir_key = f"dir-{dir_name}" if dir_name != "root" else "misc"
                dir_groups.setdefault(dir_key, []).append(filename)

            # Remove orphans from groups, add directory groups
            for feature in orphan_features:
                del groups[feature]
            for dir_key, dir_files in dir_groups.items():
                groups.setdefault(dir_key, []).extend(dir_files)

            logger.info(f"Directory fallback: merged {len(orphan_features)} orphans into {len(dir_groups)} directory groups")

    # Step 4: If still over limit, merge smallest groups into "misc"
    if len(groups) > MAX_FEATURES:
        sorted_groups = sorted(groups.items(), key=lambda x: len(x[1]))
        misc_files = []
        while len(groups) > MAX_FEATURES - 1:  # -1 to leave room for misc
            feature, files = sorted_groups.pop(0)
            misc_files.extend(files)
            del groups[feature]
        if misc_files:
            groups["misc"] = misc_files
            logger.info(f"Merged {len(misc_files)} files into 'misc' group to stay under {MAX_FEATURES} features")

    return groups


class FileInfo(NamedTuple):
    """Metadata about a source file for consolidation."""
    filename: str
    content: str
    last_modified: str  # ISO date or "unknown"


def classify_doc_type(filename: str) -> tuple[str, int]:
    """Classify a file by its type prefix. Returns (section_title, sort_order)."""
    upper = os.path.basename(filename).upper()
    for prefix, (section, order) in DOC_TYPES.items():
        if upper.startswith(f"{prefix}-"):
            return section, order
    return "Documentación", 8


def consolidate_feature(
    project_name: str,
    feature: str,
    files: list[FileInfo],
) -> str:
    """Generate a consolidated markdown document for a feature."""
    now = datetime.now(timezone.utc).isoformat()
    dates = [f.last_modified for f in files if f.last_modified != "unknown"]
    last_modified = max(dates) if dates else "unknown"

    sections: list[tuple[int, str, str, str]] = []
    for f in files:
        section_title, order = classify_doc_type(f.filename)
        sections.append((order, section_title, f.filename, f.content))

    sections.sort(key=lambda x: (x[0], x[2]))

    source_files = [s[2] for s in sections]

    frontmatter = yaml.dump({
        "project": project_name,
        "feature": feature,
        "sources": len(files),
        "source_files": source_files,
        "curated_at": now,
        "last_source_modified": last_modified,
        "status": "curated",
    }, default_flow_style=False, sort_keys=False)

    parts = [f"---\n{frontmatter}---\n", f"# Feature: {feature}\n"]
    for _, section_title, filename, content in sections:
        parts.append(f"## {section_title}")
        parts.append(f"> Source: {filename}\n")
        parts.append(content.strip())
        parts.append("")

    return "\n".join(parts)


@dataclass
class CurationResult:
    """Result of curating a project's files."""
    success: bool
    curated_files: int = 0
    features: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def curate_project(
    project_name: str,
    project_path: str,
    delta_files: list[dict],
    output_base: str = "data/curated",
) -> CurationResult:
    """Curate a project's delta files into consolidated feature documents."""
    result = CurationResult(success=True)

    active_files = [
        f for f in delta_files
        if f.get("action") != "deleted" and f.get("content")
    ]

    if not active_files:
        return result

    filenames = [f["path"] for f in active_files]
    groups = group_by_feature(filenames)
    content_map = {f["path"]: f["content"] for f in active_files}

    output_dir = Path(output_base) / project_name
    output_dir.mkdir(parents=True, exist_ok=True)

    for feature, grouped_files in groups.items():
        try:
            file_infos = []
            for fname in grouped_files:
                content = content_map.get(fname, "")
                date_match = re.search(r"(\d{4}-\d{2}-\d{2})", fname)
                last_mod = date_match.group(1) if date_match else "unknown"
                file_infos.append(FileInfo(fname, content, last_mod))

            consolidated = consolidate_feature(project_name, feature, file_infos)
            out_file = output_dir / f"{feature}.md"
            out_file.write_text(consolidated, encoding="utf-8")
            result.curated_files += 1
            result.features.append(feature)
            logger.info(f"Curated {feature} ({len(grouped_files)} sources) -> {out_file}")
        except Exception as e:
            logger.error(f"Curation failed for feature {feature}: {e}")
            result.errors.append(f"{feature}: {e}")

    if result.errors:
        result.success = len(result.errors) < len(groups)

    return result
