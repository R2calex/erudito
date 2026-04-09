import pytest
from core.curator import extract_feature_name


def test_doc_types_are_english():
    """All curator section titles must be in English per policy."""
    from core.curator import DOC_TYPES
    for prefix, (title, _order) in DOC_TYPES.items():
        assert title.isascii(), f"{prefix} title '{title}' contains non-ASCII"
    expected = {
        "SPEC": "Design", "PLAN": "Plan", "IR": "Implementation",
        "SOP": "Operations", "CONTRACT": "Contract",
        "REPORT": "Report", "REVIEW": "Review",
    }
    for prefix, expected_title in expected.items():
        assert DOC_TYPES[prefix][0] == expected_title, f"{prefix} should be '{expected_title}'"


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
        files = ["SPEC-KEYSTONE-ARCHITECTURE.md", "CLAUDE.md"]
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
        assert group_by_feature([]) == {}

    def test_single_file(self):
        groups = group_by_feature(["README.md"])
        assert "readme" in groups
        assert len(groups["readme"]) == 1


from core.curator import classify_doc_type, consolidate_feature, FileInfo


class TestClassifyDocType:
    def test_spec(self):
        section, order = classify_doc_type("SPEC-KEYSTONE.md")
        assert section == "Design"
        assert order == 1

    def test_ir(self):
        section, order = classify_doc_type("IR-2026-03-11-keystone.md")
        assert section == "Implementation"
        assert order == 3

    def test_sop(self):
        section, order = classify_doc_type("SOP-KEYSTONE.md")
        assert section == "Operations"
        assert order == 4

    def test_unknown(self):
        section, order = classify_doc_type("CLAUDE.md")
        assert section == "Documentation"
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
        assert "## Design" in result
        assert "## Operations" in result
        assert "Content here." in result
        assert "Ops content." in result

    def test_section_ordering(self):
        files = [
            FileInfo("SOP-KEYSTONE.md", "ops", "2026-03-11"),
            FileInfo("SPEC-KEYSTONE.md", "spec", "2026-03-10"),
            FileInfo("IR-2026-03-11-keystone.md", "impl", "2026-03-11"),
        ]
        result = consolidate_feature("test-project", "keystone", files)
        spec_pos = result.index("## Design")
        impl_pos = result.index("## Implementation")
        ops_pos = result.index("## Operations")
        assert spec_pos < impl_pos < ops_pos

    def test_frontmatter_present(self):
        files = [FileInfo("SPEC-KEYSTONE.md", "content", "2026-03-10")]
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
