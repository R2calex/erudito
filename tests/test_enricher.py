import pytest
from core.enricher import parse_frontmatter, infer_type, enrich_content


class TestParseFrontmatter:
    def test_complete_frontmatter(self):
        content = "---\nproject: infra-mcp\ntype: spec\nlast_updated: 2026-03-17\nstatus: active\n---\n# Content"
        fm, body = parse_frontmatter(content)
        assert fm["project"] == "infra-mcp"
        assert fm["type"] == "spec"
        assert body == "# Content"

    def test_no_frontmatter(self):
        content = "# Just a heading\nSome text"
        fm, body = parse_frontmatter(content)
        assert fm == {}
        assert body == content

    def test_partial_frontmatter(self):
        content = "---\nproject: erudito\n---\n# Content"
        fm, body = parse_frontmatter(content)
        assert fm["project"] == "erudito"
        assert "type" not in fm

    def test_empty_content(self):
        fm, body = parse_frontmatter("")
        assert fm == {}
        assert body == ""


class TestInferType:
    def test_readme(self):
        assert infer_type("README.md") == "readme"

    def test_spec(self):
        assert infer_type("2026-03-18-erudito-v3-design-spec.md") == "spec"

    def test_sop(self):
        assert infer_type("SOP.md") == "sop"
        assert infer_type("deployment-sop.md") == "sop"

    def test_ir(self):
        assert infer_type("IR-v2-migration.md") == "ir"
        assert infer_type("implementation-report.md") == "ir"

    def test_backlog(self):
        assert infer_type("BACKLOG.md") == "backlog"

    def test_design(self):
        assert infer_type("system-design.md") == "design"

    def test_generic_fallback(self):
        assert infer_type("notes.md") == "doc"
        assert infer_type("TODO.md") == "doc"


class TestEnrichContent:
    def test_already_complete(self):
        content = "---\nproject: x\ntype: spec\nlast_updated: 2026-03-17\nstatus: active\n---\n# X"
        result, enriched = enrich_content(content, "x.md", "my-project", "2026-03-17")
        assert enriched is False

    def test_no_frontmatter_adds_all(self):
        content = "# My Doc\nSome content"
        result, enriched = enrich_content(content, "README.md", "infra-mcp", "2026-03-18")
        assert enriched is True
        assert "project: infra-mcp" in result
        assert "type: readme" in result
        assert "auto_enriched: true" in result

    def test_partial_frontmatter_fills_missing(self):
        content = "---\nproject: erudito\n---\n# Content"
        result, enriched = enrich_content(content, "spec-v3.md", "erudito", "2026-03-18")
        assert enriched is True
        assert "type: spec" in result
        assert "project: erudito" in result
