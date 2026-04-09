# Erudito v3 Data Curation Pipeline — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Curator module that consolidates .md files by feature/topic before NotebookLM ingestion, and correct the data flow so Qdrant stores NLM-processed knowledge instead of raw chunks.

**Architecture:** New `core/curator.py` groups files by feature using frequency-based prefix detection, writes consolidated docs to `data/curated/{project}/`. Scan loop calls curator instead of indexer. Separate `_nlm_sync_loop` processes curated projects through NLM (max 2 concurrent). Registry gains `curation_status` field independent from `status`.

**Tech Stack:** Python 3.12, FastAPI, PyYAML, regex for prefix extraction, existing Qdrant/Redis/Ollama stack.

**Spec:** `docs/superpowers/specs/2026-03-20-erudito-v3-data-curation-design.md`

**Codebase Reference:**
- `core/registry.py` (212 lines) — `_DEFAULT_ENTRY` at line 16, `summary()` at line 194
- `main.py` (418 lines) — `_scan_project()` at line 92, `_run_indexer()` at line 135, `_run_nlm_cycle()` at line 149, `lifespan()` at line 206, `/metrics` at line 266
- `integrations/notebooklm.py` (275 lines) — `run_nlm_cycle()` at line 195
- `core/indexer.py` (171 lines) — `index_delta()` at line 132

---

## File Structure

```
erudito/
├── core/
│   ├── curator.py              # NEW — feature grouping + consolidation
│   ├── registry.py             # MODIFY — add curation fields to _DEFAULT_ENTRY + summary()
│   └── (scanner.py, indexer.py, query.py, enricher.py — unchanged)
├── integrations/
│   └── notebooklm.py           # MODIFY — run_nlm_cycle() reads from curated_dir
├── main.py                     # MODIFY — scan loop calls curator, add _nlm_sync_loop
├── data/
│   └── curated/                # NEW — runtime output directory
├── tests/
│   ├── test_curator.py         # NEW — curator unit tests
│   ├── test_registry.py        # MODIFY — add tests for new curation fields
│   ├── test_notebooklm.py      # MODIFY — test new run_nlm_cycle signature
│   └── test_api.py             # MODIFY — test updated /metrics response
└── docs/
```

---

### Task 1: Curator Module — Feature Extraction (TDD)

**Files:**
- Create: `core/curator.py`
- Create: `tests/test_curator.py`

- [ ] **Step 1: Write failing tests for `extract_feature_name()`**

```python
# tests/test_curator.py
import pytest
from core.curator import extract_feature_name


class TestExtractFeatureName:
    def test_spec_prefix(self):
        assert extract_feature_name("SPEC-KEYSTONE-ARCHITECTURE.md") == "keystone-architecture"

    def test_ir_prefix_with_date(self):
        assert extract_feature_name("IR-2026-03-11-keystone-activation.md") == "keystone-activation"

    def test_sop_prefix(self):
        assert extract_feature_name("SOP-KEYSTONE-OPERATIONS.md") == "keystone-operations"

    def test_plan_prefix(self):
        assert extract_feature_name("PLAN-M1-MESH-CHANNEL.md") == "m1-mesh-channel"

    def test_no_prefix(self):
        assert extract_feature_name("CLAUDE.md") == "claude"

    def test_date_only_prefix(self):
        assert extract_feature_name("IR-03042026-001.md") == "001"

    def test_contract_prefix_with_date(self):
        # 022242026 is 9 digits (not 8), so date regex won't strip it
        assert extract_feature_name("CONTRACT-022242026-001.md") == "022242026-001"

    def test_contract_8digit_date(self):
        assert extract_feature_name("CONTRACT-03042026-001.md") == "001"

    def test_normalize_case(self):
        assert extract_feature_name("SPEC-ERUDITO-V2-INTELLIGENT-RAG.md") == "erudito-v2-intelligent-rag"

    def test_review_prefix(self):
        assert extract_feature_name("REVIEW-SPEC-JASPER-MVP-DEMO.md") == "spec-jasper-mvp-demo"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_curator.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.curator'`

- [ ] **Step 3: Implement `extract_feature_name()`**

```python
# core/curator.py
"""Data curation pipeline: feature grouping + consolidation.

Groups .md files by feature/topic, consolidates into standardized
documents with metadata for NotebookLM ingestion.
"""
import os
import re
import logging
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
    """Extract normalized feature name from a filename.

    Steps:
    1. Strip .md extension
    2. Strip known type prefix (SPEC-, IR-, SOP-, etc.)
    3. Strip date pattern (YYYY-MM-DD- or MMDDYYYY-)
    4. Normalize to lowercase, strip leading/trailing hyphens
    """
    name = filename
    if name.endswith(".md"):
        name = name[:-3]

    name = _PREFIX_RE.sub("", name)
    name = _DATE_RE.sub("", name)
    name = name.lower().strip("-")

    return name if name else filename.lower().replace(".md", "")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_curator.py::TestExtractFeatureName -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add core/curator.py tests/test_curator.py
git commit -m "feat(erudito-v3): add curator extract_feature_name with TDD"
```

---

### Task 2: Curator Module — Feature Grouping (TDD)

**Files:**
- Modify: `core/curator.py`
- Modify: `tests/test_curator.py`

- [ ] **Step 1: Write failing tests for `group_by_feature()`**

```python
# Append to tests/test_curator.py
from core.curator import group_by_feature


class TestGroupByFeature:
    def test_groups_same_feature(self):
        files = [
            "SPEC-KEYSTONE-ARCHITECTURE.md",
            "IR-2026-03-11-keystone-activation.md",
            "SOP-KEYSTONE-OPERATIONS.md",
        ]
        groups = group_by_feature(files)
        assert "keystone" in groups
        assert len(groups["keystone"]) == 3

    def test_orphan_stays_alone(self):
        files = [
            "SPEC-KEYSTONE-ARCHITECTURE.md",
            "CLAUDE.md",
        ]
        groups = group_by_feature(files)
        assert "keystone" in groups
        assert "claude" in groups
        assert len(groups["claude"]) == 1

    def test_multi_feature_separation(self):
        files = [
            "SPEC-KEYSTONE-ARCHITECTURE.md",
            "IR-2026-03-11-keystone-activation.md",
            "SPEC-MESH-MONITOR.md",
            "SOP-MESH-MONITOR.md",
        ]
        groups = group_by_feature(files)
        assert "keystone" in groups
        assert "mesh-monitor" in groups
        assert len(groups["keystone"]) == 2
        assert len(groups["mesh-monitor"]) == 2

    def test_litellm_variants_group_together(self):
        files = [
            "SOP-LITELLM-MODEL-SYNC.md",
            "SOP-LITELLM-PROMPT-MANAGEMENT.md",
            "SPEC-LITELLM-MODEL-SYNC.md",
        ]
        groups = group_by_feature(files)
        assert "litellm" in groups
        assert len(groups["litellm"]) == 3

    def test_empty_list(self):
        groups = group_by_feature([])
        assert groups == {}

    def test_single_file(self):
        groups = group_by_feature(["README.md"])
        assert "readme" in groups
        assert len(groups["readme"]) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_curator.py::TestGroupByFeature -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement `group_by_feature()`**

```python
# Add to core/curator.py

def group_by_feature(filenames: list[str]) -> dict[str, list[str]]:
    """Group filenames by shared feature prefix using frequency detection.

    Algorithm:
    1. Extract feature name for each file
    2. Build frequency map of prefixes (1-segment, 2-segment, etc.)
    3. For each file, find the longest prefix shared with 2+ files
    4. Single-occurrence prefixes keep full name (orphan)
    """
    if not filenames:
        return {}

    # Step 1: Extract names
    name_map = {f: extract_feature_name(f) for f in filenames}

    # Step 2: Build prefix frequency map
    all_names = list(name_map.values())
    prefix_freq: dict[str, int] = {}
    for name in all_names:
        parts = name.split("-")
        for length in range(1, len(parts) + 1):
            prefix = "-".join(parts[:length])
            prefix_freq[prefix] = prefix_freq.get(prefix, 0) + 1

    # Step 3: For each file, find longest shared prefix (freq >= 2)
    groups: dict[str, list[str]] = {}
    for filename in filenames:
        name = name_map[filename]
        parts = name.split("-")

        # Try longest prefix first, stop at first with freq >= 2
        feature = name  # default: full name (orphan)
        for length in range(len(parts), 0, -1):
            prefix = "-".join(parts[:length])
            if prefix_freq.get(prefix, 0) >= 2:
                feature = prefix
                break

        groups.setdefault(feature, []).append(filename)

    return groups
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_curator.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add core/curator.py tests/test_curator.py
git commit -m "feat(erudito-v3): add curator group_by_feature with frequency detection"
```

---

### Task 3: Curator Module — File Consolidation (TDD)

**Files:**
- Modify: `core/curator.py`
- Modify: `tests/test_curator.py`

- [ ] **Step 1: Write failing tests for `classify_doc_type()` and `consolidate_feature()`**

```python
# Append to tests/test_curator.py
from core.curator import classify_doc_type, consolidate_feature, FileInfo


class TestClassifyDocType:
    def test_spec(self):
        section, order = classify_doc_type("SPEC-KEYSTONE.md")
        assert section == "Diseño"
        assert order == 1

    def test_ir(self):
        section, order = classify_doc_type("IR-2026-03-11-keystone.md")
        assert section == "Implementación"
        assert order == 3

    def test_sop(self):
        section, order = classify_doc_type("SOP-KEYSTONE.md")
        assert section == "Operación"
        assert order == 4

    def test_unknown(self):
        section, order = classify_doc_type("CLAUDE.md")
        assert section == "Documentación"
        assert order == 8

    def test_plan(self):
        section, order = classify_doc_type("PLAN-M1-MESH.md")
        assert section == "Plan"
        assert order == 2


class TestConsolidateFeature:
    def test_basic_consolidation(self):
        files = [
            FileInfo("SPEC-KEYSTONE.md", "# Keystone Spec\nContent here.", "2026-03-10"),
            FileInfo("SOP-KEYSTONE.md", "# Keystone Ops\nOps content.", "2026-03-11"),
        ]
        result = consolidate_feature("test-project", "keystone", files)
        assert "# Feature: keystone" in result
        assert "## Diseño" in result
        assert "## Operación" in result
        assert "Content here." in result
        assert "Ops content." in result

    def test_section_ordering(self):
        files = [
            FileInfo("SOP-KEYSTONE.md", "ops", "2026-03-11"),
            FileInfo("SPEC-KEYSTONE.md", "spec", "2026-03-10"),
            FileInfo("IR-2026-03-11-keystone.md", "impl", "2026-03-11"),
        ]
        result = consolidate_feature("test-project", "keystone", files)
        spec_pos = result.index("## Diseño")
        impl_pos = result.index("## Implementación")
        ops_pos = result.index("## Operación")
        assert spec_pos < impl_pos < ops_pos

    def test_frontmatter_present(self):
        files = [
            FileInfo("SPEC-KEYSTONE.md", "content", "2026-03-10"),
        ]
        result = consolidate_feature("test-project", "keystone", files)
        assert "project: test-project" in result
        assert "feature: keystone" in result
        assert "sources: 1" in result

    def test_ir_sorted_by_date(self):
        files = [
            FileInfo("IR-2026-03-15-keystone-fix.md", "fix", "2026-03-15"),
            FileInfo("IR-2026-03-11-keystone-init.md", "init", "2026-03-11"),
        ]
        result = consolidate_feature("proj", "keystone", files)
        init_pos = result.index("IR-2026-03-11-keystone-init.md")
        fix_pos = result.index("IR-2026-03-15-keystone-fix.md")
        assert init_pos < fix_pos
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_curator.py::TestClassifyDocType tests/test_curator.py::TestConsolidateFeature -v`
Expected: FAIL

- [ ] **Step 3: Implement `FileInfo`, `classify_doc_type()`, and `consolidate_feature()`**

```python
# Add to core/curator.py

class FileInfo(NamedTuple):
    """Metadata about a source file for consolidation."""
    filename: str
    content: str
    last_modified: str  # ISO date or "unknown"


def classify_doc_type(filename: str) -> tuple[str, int]:
    """Classify a file by its type prefix. Returns (section_title, sort_order)."""
    upper = filename.upper()
    for prefix, (section, order) in DOC_TYPES.items():
        if upper.startswith(f"{prefix}-"):
            return section, order
    return "Documentación", 8


def consolidate_feature(
    project_name: str,
    feature: str,
    files: list[FileInfo],
) -> str:
    """Generate a consolidated markdown document for a feature.

    Sections ordered: Diseño → Plan → Implementación (chronological) → Operación → etc.
    """
    now = datetime.now(timezone.utc).isoformat()
    source_files = [f.filename for f in files]
    dates = [f.last_modified for f in files if f.last_modified != "unknown"]
    last_modified = max(dates) if dates else "unknown"

    # Frontmatter
    frontmatter = yaml.dump({
        "project": project_name,
        "feature": feature,
        "sources": len(files),
        "source_files": source_files,
        "curated_at": now,
        "last_source_modified": last_modified,
        "status": "curated",
    }, default_flow_style=False, sort_keys=False)

    # Group files by doc type and sort
    sections: list[tuple[int, str, str, str]] = []  # (order, section_title, filename, content)
    for f in files:
        section_title, order = classify_doc_type(f.filename)
        sections.append((order, section_title, f.filename, f.content))

    # Sort by (order, filename) — filename sort ensures chronological for IR- dates
    sections.sort(key=lambda x: (x[0], x[2]))

    # Build document
    parts = [f"---\n{frontmatter}---\n", f"# Feature: {feature}\n"]
    for _, section_title, filename, content in sections:
        parts.append(f"## {section_title}")
        parts.append(f"> Source: {filename}\n")
        parts.append(content.strip())
        parts.append("")  # blank line

    return "\n".join(parts)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_curator.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add core/curator.py tests/test_curator.py
git commit -m "feat(erudito-v3): add curator consolidate_feature with doc type ordering"
```

---

### Task 4: Curator Module — Full `curate_project()` (TDD)

**Files:**
- Modify: `core/curator.py`
- Modify: `tests/test_curator.py`

- [ ] **Step 1: Write failing tests for `curate_project()`**

```python
# Append to tests/test_curator.py
import tempfile
from core.curator import curate_project, CurationResult


class TestCurateProject:
    def test_curate_creates_output_files(self, tmp_path):
        output_dir = tmp_path / "curated"
        delta_files = [
            {"path": "SPEC-AUTH.md", "content": "# Auth spec", "action": "modified"},
            {"path": "SOP-AUTH.md", "content": "# Auth ops", "action": "modified"},
        ]
        result = curate_project("my-project", str(tmp_path), delta_files, str(output_dir))
        assert result.success
        assert result.curated_files > 0
        assert (output_dir / "my-project" / "auth.md").exists()

    def test_curate_empty_delta(self, tmp_path):
        output_dir = tmp_path / "curated"
        result = curate_project("proj", str(tmp_path), [], str(output_dir))
        assert result.success
        assert result.curated_files == 0

    def test_curate_skips_deleted(self, tmp_path):
        output_dir = tmp_path / "curated"
        delta_files = [
            {"path": "SPEC-AUTH.md", "content": "# Auth spec", "action": "modified"},
            {"path": "OLD-FILE.md", "content": None, "action": "deleted"},
        ]
        result = curate_project("proj", str(tmp_path), delta_files, str(output_dir))
        assert result.success
        assert result.curated_files == 1

    def test_curate_result_has_features(self, tmp_path):
        output_dir = tmp_path / "curated"
        delta_files = [
            {"path": "SPEC-AUTH.md", "content": "# Auth spec", "action": "modified"},
            {"path": "IR-2026-03-20-auth-fix.md", "content": "# Fix", "action": "modified"},
        ]
        result = curate_project("proj", str(tmp_path), delta_files, str(output_dir))
        assert "auth" in result.features
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_curator.py::TestCurateProject -v`
Expected: FAIL

- [ ] **Step 3: Implement `CurationResult` and `curate_project()`**

```python
# Add to core/curator.py
from dataclasses import dataclass, field


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
    """Curate a project's delta files into consolidated feature documents.

    Args:
        project_name: Registry project name
        project_path: Path to the project repo
        delta_files: List of {path, content, action} from scanner
        output_base: Base directory for curated output

    Returns:
        CurationResult with success status and stats
    """
    result = CurationResult(success=True)

    # Filter out deleted files and files with no content
    active_files = [
        f for f in delta_files
        if f.get("action") != "deleted" and f.get("content")
    ]

    if not active_files:
        return result

    # Group by feature
    filenames = [f["path"] for f in active_files]
    groups = group_by_feature(filenames)

    # Build FileInfo objects
    content_map = {f["path"]: f["content"] for f in active_files}

    # Create output directory
    output_dir = Path(output_base) / project_name
    output_dir.mkdir(parents=True, exist_ok=True)

    for feature, grouped_files in groups.items():
        try:
            file_infos = []
            for fname in grouped_files:
                content = content_map.get(fname, "")
                # Try to extract date from filename for sorting
                date_match = re.search(r"(\d{4}-\d{2}-\d{2})", fname)
                last_mod = date_match.group(1) if date_match else "unknown"
                file_infos.append(FileInfo(fname, content, last_mod))

            consolidated = consolidate_feature(project_name, feature, file_infos)
            out_file = output_dir / f"{feature}.md"
            out_file.write_text(consolidated, encoding="utf-8")
            result.curated_files += 1
            result.features.append(feature)
            logger.info(f"Curated {feature} ({len(grouped_files)} sources) → {out_file}")
        except Exception as e:
            logger.error(f"Curation failed for feature {feature}: {e}")
            result.errors.append(f"{feature}: {e}")

    if result.errors:
        result.success = len(result.errors) < len(groups)  # partial success

    return result
```

- [ ] **Step 4: Run all curator tests**

Run: `python -m pytest tests/test_curator.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add core/curator.py tests/test_curator.py
git commit -m "feat(erudito-v3): add curator curate_project with full pipeline"
```

---

### Task 5: Registry — Add Curation Fields

**Files:**
- Modify: `core/registry.py:16-29` (`_DEFAULT_ENTRY`)
- Modify: `core/registry.py:194-211` (`summary()`)
- Modify: `tests/test_registry.py`

- [ ] **Step 1: Write failing tests for new curation fields**

```python
# Append to tests/test_registry.py

class TestCurationFields:
    def test_default_entry_has_curation_fields(self, tmp_registry):
        tmp_registry.register("test", "/tmp/test", "hanzo", "")
        entry = tmp_registry.get("test")
        assert entry["curation_status"] == "uncurated"
        assert entry["curated_at"] is None
        assert entry["curated_files"] == 0
        assert entry["nlm_consecutive_failures"] == 0

    def test_summary_includes_curation(self, tmp_registry):
        tmp_registry.register("a", "/tmp/a", "hanzo", "")
        summary = tmp_registry.summary()
        assert "curation_pct" in summary
        assert "curation_pending" in summary
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_registry.py::TestCurationFields -v`
Expected: FAIL — KeyError on new fields

- [ ] **Step 3: Add curation fields to `_DEFAULT_ENTRY`**

In `core/registry.py`, add to `_DEFAULT_ENTRY` (after line 28):

```python
    "nlm_source_count": 0,
    "curation_status": "uncurated",
    "curated_at": None,
    "curated_files": 0,
    "nlm_consecutive_failures": 0,
```

- [ ] **Step 4: Update `summary()` to include curation metrics**

Replace `summary()` method in `core/registry.py`:

```python
    def summary(self) -> dict:
        projects = self._data["projects"]
        total = len(projects)
        by_status = {}
        curation_curated = 0
        curation_pending = 0
        for entry in projects.values():
            s = entry["status"]
            by_status[s] = by_status.get(s, 0) + 1
            cs = entry.get("curation_status", "uncurated")
            if cs == "curated":
                curation_curated += 1
            elif cs in ("uncurated", "stale"):
                curation_pending += 1
        validated = by_status.get("validated", 0)
        synced = by_status.get("synced", 0)
        return {
            "total": total,
            "validated": validated,
            "synced": synced,
            "stale": by_status.get("stale", 0),
            "pending": by_status.get("pending", 0),
            "coverage_pct": round(validated / total * 100, 1) if total else 0.0,
            "coverage_partial_pct": round((validated + synced) / total * 100, 1) if total else 0.0,
            "curation_pct": round(curation_curated / total * 100, 1) if total else 0.0,
            "curation_pending": curation_pending,
        }
```

- [ ] **Step 5: Run all registry tests**

Run: `python -m pytest tests/test_registry.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add core/registry.py tests/test_registry.py
git commit -m "feat(erudito-v3): add curation_status fields to registry"
```

---

### Task 6: Modify Scan Loop — Curator Instead of Indexer

**Files:**
- Modify: `main.py:92-146` (`_scan_project`, `_run_indexer`)

- [ ] **Step 1: Add curator import and config to `main.py`**

At the top imports (after line 18), add:

```python
from core.curator import curate_project
```

Add config constant (after line 38):

```python
CURATED_DIR = os.path.join(DATA_DIR, "curated")
MAX_CONCURRENT_NLM = int(os.getenv("MAX_CONCURRENT_NLM", "2"))
```

Add to global state section (after line 42):

```python
_nlm_sync_task: asyncio.Task | None = None
_nlm_semaphore: asyncio.Semaphore | None = None
```

- [ ] **Step 2: Replace `_run_indexer` call with `curator.curate_project` in `_scan_project()`**

Replace lines 126-132 of `main.py` (the indexer + nlm block) with:

```python
    # Run Curator (replaces direct indexing)
    try:
        curation_result = curate_project(
            project_name, repo_path, delta["files"], CURATED_DIR
        )
        if curation_result.success and curation_result.curated_files > 0:
            # Update registry with curation status
            project = registry._data["projects"].get(project_name)
            if project:
                project["curation_status"] = "curated"
                project["curated_at"] = _now_iso()
                project["curated_files"] = curation_result.curated_files
                registry._persist()
            registry.update_sync(project_name, delta["new_hash"],
                                 doc_count=len([f for f in delta["files"] if f["action"] != "deleted"]))
            audit_log({
                "action": "curation_complete",
                "project": project_name,
                "features": curation_result.features,
                "curated_files": curation_result.curated_files,
            })
            logger.info(f"Curated {project_name}: {curation_result.curated_files} features")
        elif curation_result.errors:
            project = registry._data["projects"].get(project_name)
            if project:
                project["curation_status"] = "error"
                registry._persist()
            audit_log({
                "action": "curation_error",
                "project": project_name,
                "errors": curation_result.errors,
            })
    except Exception as e:
        logger.error(f"Curation failed for {project_name}: {e}")
        audit_log({"action": "curation_error", "project": project_name, "error": str(e)})
```

- [ ] **Step 3: Add curated file cleanup for deleted sources**

In `_scan_project()`, after curation, add cleanup for curated files whose source files have all been deleted:

```python
    # Cleanup curated files for features with all sources deleted
    deleted_files = [f["path"] for f in delta["files"] if f["action"] == "deleted"]
    if deleted_files and curation_result.success:
        curated_project_dir = Path(CURATED_DIR) / project_name
        if curated_project_dir.is_dir():
            for curated_file in curated_project_dir.glob("*.md"):
                try:
                    content = curated_file.read_text(encoding="utf-8")
                    # Check if all source_files in frontmatter are in deleted list
                    if content.startswith("---"):
                        fm_end = content.index("---", 3)
                        fm = yaml.safe_load(content[3:fm_end])
                        sources = fm.get("source_files", [])
                        if sources and all(s in deleted_files for s in sources):
                            curated_file.unlink()
                            audit_log({"action": "curation_cleanup", "project": project_name, "file": curated_file.name})
                            logger.info(f"Cleaned up curated file: {curated_file.name}")
                except Exception as e:
                    logger.warning(f"Cleanup check failed for {curated_file}: {e}")
```

Add `import yaml` to main.py imports if not already present.

- [ ] **Step 4: Run all tests to verify no regressions**

Run: `python -m pytest tests/ -v --tb=short`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add main.py
git commit -m "feat(erudito-v3): scan loop calls curator instead of indexer, with cleanup"
```

---

### Task 7: Add NLM Sync Loop

**Files:**
- Modify: `main.py` — add `_nlm_sync_loop()`, `_nlm_sync_project()`, update `lifespan()`
- Modify: `integrations/notebooklm.py:195-274` — change `run_nlm_cycle()` signature

- [ ] **Step 1: Update `run_nlm_cycle()` in `notebooklm.py` to accept `curated_dir`**

Replace `run_nlm_cycle()` (lines 195-274) with:

```python
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

    # 1. Read curated files
    curated_path = Path(curated_dir)
    if not curated_path.is_dir():
        logger.warning(f"Curated dir not found: {curated_dir}")
        return result

    curated_files = sorted(curated_path.glob("*.md"))
    if not curated_files:
        logger.warning(f"No curated files in {curated_dir}")
        return result

    # 2. Check source count
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

    # 3. Upload curated docs as sources
    if not skip_source_add:
        for cf in curated_files:
            content = cf.read_text(encoding="utf-8")
            ok = await add_source(notebook_id, content, cf.name)
            if ok:
                result["sources_uploaded"] += 1

    # 4. Wait for processing
    await asyncio.sleep(20)

    # 5. Condense
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

    # 6. Ask fixed questions
    for question in FIXED_QUESTIONS:
        answer = await query_notebook(notebook_id, question)
        if answer:
            result["notes"].append({
                "type": "qa",
                "question": question,
                "answer": answer,
                "project": project_name,
            })

    # 7. Ask dynamic questions from curated features
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

    # 8. Save notes in notebook
    for note in result["notes"]:
        note_text = f"Q: {note['question']}\nA: {note['answer']}"
        saved = await create_note(notebook_id, note_text)
        if not saved:
            logger.warning(f"Failed to save note in notebook {notebook_id}")

    result["success"] = len(result["notes"]) > 0
    return result
```

Add `from pathlib import Path` to the imports at top of `notebooklm.py`.

- [ ] **Step 2: Add `_nlm_sync_loop()` and `_nlm_sync_project()` to `main.py`**

Add after `_run_nlm_cycle()` function (or replace it), before `lifespan()`:

```python
_nlm_sync_task: asyncio.Task | None = None
_nlm_semaphore: asyncio.Semaphore | None = None


async def _nlm_sync_loop():
    """Background NLM sync loop. Runs offset from scan loop."""
    await asyncio.sleep(300)  # 5-minute offset from scan loop
    while True:
        try:
            await _run_nlm_sync_all()
        except Exception as e:
            logger.error(f"NLM sync loop error: {e}")
            audit_log({"action": "nlm_sync_error", "error": str(e)})
        await asyncio.sleep(SCAN_INTERVAL)


async def _run_nlm_sync_all():
    """Sync all curated projects with NLM, max 2 concurrent."""
    global _nlm_semaphore
    if not registry or not NLM_ENABLED:
        return
    if _nlm_semaphore is None:
        _nlm_semaphore = asyncio.Semaphore(MAX_CONCURRENT_NLM)

    # Find projects that need NLM sync
    candidates = []
    for name in registry.list_all():
        entry = registry.get(name)
        if entry and entry.get("curation_status") == "curated":
            candidates.append(name)

    if not candidates:
        return

    logger.info(f"NLM sync: {len(candidates)} projects to process")

    async def _bounded_sync(name):
        async with _nlm_semaphore:
            await _nlm_sync_project(name)

    tasks = [_bounded_sync(name) for name in candidates]
    await asyncio.gather(*tasks, return_exceptions=True)


async def _nlm_sync_project(project_name: str):
    """Sync a single project with NLM using curated files."""
    entry = registry.get(project_name)
    if not entry:
        return

    notebook_id = entry.get("notebook_id")

    # Create notebook if needed
    if not notebook_id:
        notebook_id = await nlm.create_notebook(f"AI-Lab: {project_name}")
        if notebook_id:
            registry._data["projects"][project_name]["notebook_id"] = notebook_id
            registry._persist()
            logger.info(f"Created notebook for {project_name}: {notebook_id}")
        else:
            # Track consecutive failures
            project = registry._data["projects"].get(project_name)
            if project:
                project["nlm_consecutive_failures"] = project.get("nlm_consecutive_failures", 0) + 1
                registry._persist()
            _nlm_call_stats["total"] += 1
            logger.warning(f"Failed to create notebook for {project_name}")
            return

    curated_dir = os.path.join(CURATED_DIR, project_name)
    _nlm_call_stats["total"] += 1
    result = await nlm.run_nlm_cycle(notebook_id, curated_dir, project_name=project_name)

    if result["success"]:
        _nlm_call_stats["success"] += 1
        # Store notes in Qdrant nlm_notes collection
        for note in result["notes"]:
            embedding = embed_text(f"{note['question']} {note['answer']}")
            point_id = generate_point_id(f"nlm:{project_name}:{note['question'][:50]}", 0)
            upsert_points([{
                "id": point_id,
                "vector": embedding,
                "payload": {
                    "text": note["answer"],
                    "question": note["question"],
                    "source": f"nlm:{project_name}",
                    "project": project_name,
                    "from_nlm": True,
                    "type": note["type"],
                    "chunk_index": 0,
                },
            }], COLLECTION_NLM_NOTES)

        # Reset failure counter and mark validated
        project = registry._data["projects"].get(project_name)
        if project:
            project["nlm_source_count"] = result["sources_uploaded"]
            project["nlm_consecutive_failures"] = 0
            registry._persist()
        registry.mark_validated(project_name, coverage=1.0)
        audit_log({"action": "nlm_validated", "project": project_name, "notes": len(result["notes"])})
    else:
        # Track failure
        project = registry._data["projects"].get(project_name)
        if project:
            failures = project.get("nlm_consecutive_failures", 0) + 1
            project["nlm_consecutive_failures"] = failures
            registry._persist()

            # Fallback: index curated docs directly if NLM fails 3+ times
            if failures >= 3:
                logger.warning(f"NLM failed {failures}x for {project_name}, falling back to direct indexing")
                await _fallback_index_curated(project_name)

        logger.warning(f"NLM cycle failed for {project_name}")
        audit_log({"action": "nlm_failed", "project": project_name})


async def _fallback_index_curated(project_name: str):
    """Fallback: index curated docs directly to agent_knowledge when NLM is unavailable."""
    curated_dir = os.path.join(CURATED_DIR, project_name)
    curated_path = Path(curated_dir)
    if not curated_path.is_dir():
        return

    delta_files = []
    for md_file in sorted(curated_path.glob("*.md")):
        delta_files.append({
            "path": str(md_file),
            "content": md_file.read_text(encoding="utf-8"),
            "action": "modified",
        })

    if delta_files:
        try:
            loop = asyncio.get_event_loop()
            delta = {
                "project": project_name,
                "old_hash": None,
                "new_hash": "fallback",
                "files": delta_files,
            }
            count = await loop.run_in_executor(None, index_delta, delta)
            logger.info(f"Fallback indexed {count} points for {project_name}")
            audit_log({"action": "fallback_index", "project": project_name, "points": count})
        except Exception as e:
            logger.error(f"Fallback indexing failed for {project_name}: {e}")
```

- [ ] **Step 3: Update `lifespan()` to start NLM sync loop**

Replace `lifespan()` in `main.py`:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: init services and start background loops."""
    global registry, _scan_task, _nlm_sync_task, _scan_semaphore, _nlm_semaphore

    Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
    Path(CURATED_DIR).mkdir(parents=True, exist_ok=True)
    registry = Registry(yaml_path=REGISTRY_YAML, redis_url=REDIS_URL)

    # Ensure Qdrant collections exist
    try:
        ensure_collection(COLLECTION_KNOWLEDGE)
        ensure_collection(COLLECTION_NLM_NOTES)
    except Exception as e:
        logger.warning(f"Qdrant init warning: {e}")

    # Init concurrency control and start background loops
    _scan_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SCANS)
    _nlm_semaphore = asyncio.Semaphore(MAX_CONCURRENT_NLM)
    _scan_task = asyncio.create_task(_scan_loop())
    if NLM_ENABLED:
        _nlm_sync_task = asyncio.create_task(_nlm_sync_loop())
        logger.info(f"NLM sync enabled. Max concurrent: {MAX_CONCURRENT_NLM}")
    logger.info(f"Max concurrent scans: {MAX_CONCURRENT_SCANS}")
    logger.info(f"Erudito v3 started. Scan interval: {SCAN_INTERVAL}s")

    yield

    # Cleanup
    if _scan_task:
        _scan_task.cancel()
    if _nlm_sync_task:
        _nlm_sync_task.cancel()
```

- [ ] **Step 4: Remove old `_run_indexer()` and `_run_nlm_cycle()` functions**

Delete `_run_indexer()` (lines 135-146) and `_run_nlm_cycle()` (lines 149-203) from `main.py`. They are replaced by the curator + NLM sync loop.

- [ ] **Step 5: Update test_notebooklm.py — no code change needed**

The existing tests test `build_mcp_request` and `parse_mcp_response` which don't change. The `run_nlm_cycle` signature change is tested via integration, not unit tests.

- [ ] **Step 6: Run all tests**

Run: `python -m pytest tests/ -v --tb=short`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add main.py integrations/notebooklm.py
git commit -m "feat(erudito-v3): add NLM sync loop, curator-driven scan, fallback indexing"
```

---

### Task 8: Update /metrics Endpoint

**Files:**
- Modify: `main.py` (`metrics()` endpoint)
- Modify: `tests/test_api.py`

- [ ] **Step 1: Update `/metrics` endpoint to include curation metrics**

The `summary()` method now returns `curation_pct` and `curation_pending` (from Task 5), so `/metrics` already includes them via `"coverage": summary`. No additional code needed for the coverage section.

Verify the coherence score is no longer hardcoded — it already comes from `_coherence_cache` which is updated via `/metrics/coherence` endpoint. This is correct per spec (eval-agent triggers coherence checks externally).

- [ ] **Step 2: Update test_api.py to check curation fields**

```python
# In tests/test_api.py, update test_metrics to check new fields
# Find the existing test_metrics test and add assertions:
#   assert "curation_pct" in data["coverage"]
#   assert "curation_pending" in data["coverage"]
```

- [ ] **Step 3: Run tests**

Run: `python -m pytest tests/test_api.py -v`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add main.py tests/test_api.py
git commit -m "feat(erudito-v3): metrics endpoint includes curation coverage"
```

---

### Task 9: Update KPIs in eval-agent

**Files:**
- Modify: `~/ai-lab/agent-eval/kpis/erudito.yaml`

- [ ] **Step 1: Read current KPI file**

Read: `~/ai-lab/agent-eval/kpis/erudito.yaml`

- [ ] **Step 2: Add new KPI entries**

Append after eru-10:

```yaml
  - id: eru-11
    name: "Curation coverage above 90%"
    type: metric
    check: http_json
    url: /metrics
    assertion: "$.coverage.curation_pct >= 90"
    severity: high
    interval: 30m

  - id: eru-12
    name: "No curation pending"
    type: metric
    check: http_json
    url: /metrics
    assertion: "$.coverage.curation_pending == 0"
    severity: medium
    interval: 30m

  - id: eru-13
    name: "Coherence drift below threshold"
    type: metric
    check: http_json
    url: /metrics
    assertion: "$.coherence_last_score >= 0.8 or $.coherence_last_score == null"
    severity: high
    interval: 1h
```

- [ ] **Step 3: Commit**

```bash
git add ~/ai-lab/agent-eval/kpis/erudito.yaml
git commit -m "feat(eval-agent): add Erudito curation and coherence KPIs"
```

---

### Task 10: Pilot Test with `erudito` Project

**Files:** No code changes — validation only.

- [ ] **Step 1: Rebuild and redeploy Docker container**

```bash
cd ~/ai-lab/erudito && docker compose build && docker compose up -d
```

- [ ] **Step 2: Wait for container to be healthy**

```bash
docker ps --filter "name=erudito" --format "table {{.Names}}\t{{.Status}}"
curl -s http://localhost:8095/health | python -m json.tool
```

Expected: Container healthy, version 3.0.0

- [ ] **Step 3: Trigger scan for `erudito` project only**

```bash
curl -s -X POST http://localhost:8095/scan/erudito | python -m json.tool
```

- [ ] **Step 4: Wait 30 seconds then inspect curated output**

```bash
ls -la data/curated/erudito/
cat data/curated/erudito/*.md | head -50
```

Expected: Curated .md files with frontmatter and consolidated sections

- [ ] **Step 5: Check registry for curation status**

```bash
curl -s http://localhost:8095/registry/erudito | python -m json.tool
```

Expected: `curation_status: "curated"`, `curated_files > 0`

- [ ] **Step 6: Check metrics for curation coverage**

```bash
curl -s http://localhost:8095/metrics | python -m json.tool
```

Expected: `curation_pct > 0`, `curation_pending` decreased

- [ ] **Step 7: Check audit log for curation entries**

```bash
curl -s http://localhost:8095/audit?limit=5 | python -m json.tool
```

Expected: `curation_complete` entry for `erudito`

- [ ] **Step 8: Commit updated plan checkboxes**

```bash
git add docs/superpowers/plans/2026-03-20-erudito-v3-data-curation.md
git commit -m "docs(erudito-v3): mark pilot test complete in plan"
```

---

### Out of scope (deferred)

**Coherence check algorithm implementation:** Per the spec, the coherence check is "triggered externally by eval-agent." The actual coherence computation (LLM synthesis of Qdrant results, embedding similarity comparison vs NLM ground truth) will be implemented in the eval-agent codebase, not in Erudito. Erudito exposes the existing `/metrics/coherence` POST endpoint for eval-agent to report scores. This is a separate plan for the eval-agent project.
