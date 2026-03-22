import pytest
from core.classifier import classify_file, classify_inbox, move_classified, ClassificationResult
from pathlib import Path


class TestClassifyFile:
    def test_keystone_spec(self):
        assert classify_file("SPEC-KEYSTONE-ARCHITECTURE.md") == "infra-mcp"

    def test_keystone_ir_with_date(self):
        assert classify_file("IR-2026-03-11-keystone-activation.md") == "infra-mcp"

    def test_erudito(self):
        assert classify_file("SOP-ERUDITO.md") == "erudito"

    def test_erudito_v2(self):
        assert classify_file("SPEC-ERUDITO-V2-INTELLIGENT-RAG.md") == "erudito"

    def test_mesh_monitor(self):
        assert classify_file("SPEC-MESH-MONITOR.md") == "mesh-monitor"

    def test_jasper_milestone(self):
        assert classify_file("IR-2026-03-15-jasper-m2-backend-chat.md") == "jasper"

    def test_jasper_plan(self):
        assert classify_file("PLAN-M3-JIMBO-AGENT-FACTORY.md") == "jasper"

    def test_litellm(self):
        assert classify_file("SOP-LITELLM-MODEL-SYNC.md") == "litellm"

    def test_event_bus(self):
        assert classify_file("SPEC-EVENT-BUS.md") == "event-bus"

    def test_mesh_channel_goes_to_event_bus(self):
        assert classify_file("SPEC-MESH-CHANNEL.md") == "event-bus"

    def test_marker_mcp(self):
        assert classify_file("SPEC-MARKER-MCP.md") == "marker-mcp"

    def test_n8n(self):
        assert classify_file("SOP-N8N-INTEGRATION.md") == "n8n"

    def test_kubo(self):
        assert classify_file("SOP-KUBO-MODEL-MANAGEMENT.md") == "kubo"

    def test_sariatu(self):
        assert classify_file("IR-2026-03-12-sariatu-integration.md") == "sariatu"

    def test_auto_remediation_goes_to_mesh_monitor(self):
        assert classify_file("SOP-AUTO-REMEDIATION.md") == "mesh-monitor"

    def test_placement_engine_goes_to_infra_mcp(self):
        assert classify_file("SPEC-PLACEMENT-ENGINE.md") == "infra-mcp"

    def test_agent_bootstrap_goes_to_devops(self):
        assert classify_file("SPEC-AGENT-BOOTSTRAP-PROTOCOL.md") == "devops-agent"

    def test_unclassified_generic(self):
        assert classify_file("CONTRACT-001.md") is None

    def test_unclassified_claude(self):
        assert classify_file("CLAUDE.md") is None

    def test_guardrail_goes_to_litellm(self):
        assert classify_file("SPEC-litellm-guardrails-session-hygiene.md") == "litellm"


class TestClassifyInbox:
    def test_classify_real_files(self, tmp_path):
        # Create some test files
        for name in ["SPEC-ERUDITO.md", "SOP-KEYSTONE.md", "CONTRACT-001.md"]:
            (tmp_path / name).write_text("test content")

        result = classify_inbox(str(tmp_path))
        assert "erudito" in result.classified
        assert "infra-mcp" in result.classified
        assert "CONTRACT-001.md" in result.unclassified

    def test_empty_inbox(self, tmp_path):
        result = classify_inbox(str(tmp_path))
        assert result.classified == {}
        assert result.unclassified == []

    def test_nonexistent_inbox(self):
        result = classify_inbox("/nonexistent/path")
        assert len(result.errors) > 0


class TestMoveClassified:
    def test_moves_files(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        (inbox / "SPEC-ERUDITO.md").write_text("erudito content")

        project_dir = tmp_path / "erudito"
        project_dir.mkdir()

        classification = ClassificationResult()
        classification.classified = {"erudito": ["SPEC-ERUDITO.md"]}

        result = move_classified(
            str(inbox),
            classification,
            {"erudito": str(project_dir)},
        )

        assert result.moved == 1
        assert (project_dir / "docs" / "contracts" / "SPEC-ERUDITO.md").exists()
        assert not (inbox / "SPEC-ERUDITO.md").exists()

    def test_dry_run(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        (inbox / "SPEC-ERUDITO.md").write_text("content")

        classification = ClassificationResult()
        classification.classified = {"erudito": ["SPEC-ERUDITO.md"]}

        result = move_classified(
            str(inbox),
            classification,
            {"erudito": str(tmp_path / "erudito")},
            dry_run=True,
        )

        assert result.moved == 1
        assert (inbox / "SPEC-ERUDITO.md").exists()  # Not moved in dry run

    def test_unregistered_project(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        (inbox / "SPEC-ERUDITO.md").write_text("content")

        classification = ClassificationResult()
        classification.classified = {"erudito": ["SPEC-ERUDITO.md"]}

        result = move_classified(
            str(inbox),
            classification,
            {},  # No projects registered
        )

        assert result.moved == 0
        assert len(result.errors) > 0
