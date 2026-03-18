import pytest
from integrations.notebooklm import (
    build_mcp_request,
    parse_mcp_response,
    FIXED_QUESTIONS,
)


class TestMCPProtocol:
    def test_build_request(self):
        req = build_mcp_request("notebook_list", {"max_results": 10})
        assert req["jsonrpc"] == "2.0"
        assert req["method"] == "tools/call"
        assert req["params"]["name"] == "notebooklm_mcp-notebook_list"
        assert req["params"]["arguments"]["max_results"] == 10

    def test_parse_success_response(self):
        raw = {
            "result": {
                "content": [{"type": "text", "text": '{"notebooks": []}'}]
            }
        }
        parsed = parse_mcp_response(raw)
        assert parsed == {"notebooks": []}

    def test_parse_error_response(self):
        raw = {"error": {"message": "auth failed"}}
        parsed = parse_mcp_response(raw)
        assert parsed is None


class TestFixedQuestions:
    def test_has_five_questions(self):
        assert len(FIXED_QUESTIONS) == 5

    def test_questions_are_strings(self):
        for q in FIXED_QUESTIONS:
            assert isinstance(q, str)
            assert len(q) > 10
