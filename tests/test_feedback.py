from __future__ import annotations

import asyncio
import os
import sys

import pytest


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CODEJUNG_SERVICE_API_TOKEN", "dummy")

import codejung_mcp as stdio  # noqa: E402
import codejung_mcp_http as http  # noqa: E402


def test_list_tools_advertises_incorrect_finding_feedback():
    for module in (stdio, http):
        tools = {tool.name: tool for tool in asyncio.run(module.mcp.list_tools())}
        schema = tools["report_incorrect_finding"].inputSchema
        assert {"finding_id", "explanation"} <= set(schema["properties"])


@pytest.mark.parametrize("module", [stdio, http])
def test_report_incorrect_finding_posts_pending_feedback(module, monkeypatch):
    calls = []

    def fake_api(method, path, body=None, headers=None):
        calls.append((method, path, body, headers))
        return {
            "feedbackId": "cjfdb_123",
            "findingId": "cjf_deadbeef",
            "state": "pending",
        }

    monkeypatch.setattr(module, "_api", fake_api)
    response = module.report_incorrect_finding(
        "cjf_deadbeef",
        "The preceding guard already handles this case.",
        "false_positive",
    )

    assert response["state"] == "pending"
    assert calls == [(
        "POST",
        "/v1/findings/cjf_deadbeef/feedback",
        {
            "explanation": "The preceding guard already handles this case.",
            "category": "false_positive",
        },
        {"Idempotency-Key": calls[0][3]["Idempotency-Key"]},
    )]
    assert calls[0][3]["Idempotency-Key"].startswith("mcp-")


@pytest.mark.parametrize("module", [stdio, http])
def test_report_incorrect_finding_rejects_invalid_inputs(module):
    with pytest.raises(ValueError, match="finding ID"):
        module.report_incorrect_finding("not-an-id", "Long enough explanation")
    with pytest.raises(ValueError, match="explanation"):
        module.report_incorrect_finding("cjf_deadbeef", "short")
    with pytest.raises(ValueError, match="category"):
        module.report_incorrect_finding(
            "cjf_deadbeef",
            "Long enough explanation",
            "made_up",
        )
