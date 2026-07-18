"""Tier-1 progress-heartbeat tests for the codeJung MCP servers.

Both review tools poll a job to completion while emitting an MCP progress
notification each cycle, so progress-aware clients don't time out mid-review.
These tests drive the async tool functions directly with a fake Context and
monkeypatched API calls (no network, no SSH), shrinking the poll interval to 0.
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("CODEJUNG_SERVICE_API_TOKEN", "dummy")  # let the HTTP module import

import codejung_mcp as stdio  # noqa: E402
import codejung_mcp_http as http  # noqa: E402


class FakeCtx:
    """Records report_progress calls the way a progress-aware client would receive."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def report_progress(self, progress, total=None, message=None) -> None:
        self.calls.append((progress, total, message))


@pytest.fixture(autouse=True)
def _fast_polls(monkeypatch):
    monkeypatch.setattr(stdio, "_POLL_INTERVAL", 0)
    monkeypatch.setattr(http, "_POLL_INTERVAL", 0)


def _running_then(*, terminal: str, running_cycles: int):
    seq = iter([{"status": "running"}] * running_cycles + [{"status": terminal}])
    return lambda job_id: next(seq)


# ---- HTTP server ---------------------------------------------------------

def test_http_review_pr_emits_progress_then_succeeds(monkeypatch):
    monkeypatch.setattr(http, "_submit", lambda pr_url, post=True: "cj_abc")
    monkeypatch.setattr(http, "_poll", _running_then(terminal="succeeded", running_cycles=2))
    monkeypatch.setattr(http, "_result",
                        lambda job_id: {"summaryMarkdown": "S", "findings": [{"id": 1}]})
    ctx = FakeCtx()
    resp = asyncio.run(http.review_pr("https://github.com/o/r/pull/1", ctx, wait_secs=100))

    assert resp["status"] == "succeeded"
    assert resp["findings"] == [{"id": 1}]
    assert resp["summaryMarkdown"] == "S"
    # one heartbeat per poll: 2 running + 1 terminal
    assert len(ctx.calls) == 3
    # progress is monotonic and bounded by total
    progresses = [c[0] for c in ctx.calls]
    assert progresses == sorted(progresses)
    assert all(c[1] == 100 for c in ctx.calls)


def test_http_review_pr_times_out_to_running_hint(monkeypatch):
    monkeypatch.setattr(http, "_submit", lambda pr_url, post=True: "cj_x")
    monkeypatch.setattr(http, "_poll", lambda job_id: {"status": "running"})
    ctx = FakeCtx()
    resp = asyncio.run(http.review_pr("https://github.com/o/r/pull/1", ctx, wait_secs=0))

    assert resp["status"] == "running"
    assert resp["jobId"] == "cj_x"
    assert "hint" in resp
    assert len(ctx.calls) >= 1  # heartbeat still emitted before returning


def test_http_review_pr_failed_carries_error(monkeypatch):
    monkeypatch.setattr(http, "_submit", lambda pr_url, post=True: "cj_f")
    monkeypatch.setattr(http, "_poll", lambda job_id: {"status": "failed", "error": {"code": "boom"}})
    ctx = FakeCtx()
    resp = asyncio.run(http.review_pr("https://github.com/o/r/pull/1", ctx, wait_secs=100))

    assert resp["status"] == "failed"
    assert resp["error"] == {"code": "boom"}


def test_http_get_review_surfaces_phase(monkeypatch):
    monkeypatch.setattr(http, "_poll", lambda job_id: {"status": "running", "phase": "reconcile"})
    resp = http.get_review("cj_x")
    assert resp["status"] == "running"
    assert resp["phase"] == "reconcile"


def test_http_heartbeat_message_includes_phase(monkeypatch):
    seq = iter([{"status": "running", "phase": "pass1"},
                {"status": "succeeded", "phase": "posting"}])
    monkeypatch.setattr(http, "_submit", lambda pr_url, post=True: "cj_p")
    monkeypatch.setattr(http, "_poll", lambda job_id: next(seq))
    monkeypatch.setattr(http, "_result", lambda job_id: {"summaryMarkdown": "", "findings": []})
    ctx = FakeCtx()
    asyncio.run(http.review_pr("https://github.com/o/r/pull/1", ctx, wait_secs=100))
    messages = [c[2] for c in ctx.calls]
    assert any("pass1" in m for m in messages)


# ---- stdio server --------------------------------------------------------

def test_stdio_review_pr_emits_progress_then_succeeds(monkeypatch):
    monkeypatch.setattr(stdio, "_submit", lambda pr_url, post=True: "cj_s")
    monkeypatch.setattr(stdio, "_poll", _running_then(terminal="succeeded", running_cycles=1))
    monkeypatch.setattr(stdio, "_result",
                        lambda job_id: {"summaryMarkdown": "M", "findings": [{"id": 2}]})
    ctx = FakeCtx()
    resp = asyncio.run(stdio.review_pr("https://github.com/o/r/pull/9", ctx, wait_secs=100))

    assert resp["status"] == "succeeded"
    assert resp["findings"] == [{"id": 2}]
    assert len(ctx.calls) == 2  # 1 running + 1 terminal


def test_stdio_get_review_surfaces_phase(monkeypatch):
    monkeypatch.setattr(stdio, "_poll", lambda job_id: {"status": "running", "phase": "pass3"})
    resp = stdio.get_review("cj_y")
    assert resp["status"] == "running"
    assert resp["phase"] == "pass3"


def test_stdio_review_dir_stages_waits_and_unstages(monkeypatch):
    events: list[str] = []
    monkeypatch.setattr(stdio, "REMOTE_URL", "")  # SSH mode
    monkeypatch.setattr(stdio, "_sweep_stale_staging", lambda: events.append("sweep"))
    monkeypatch.setattr(stdio, "_stage_dir", lambda path: ("/review-staging/abc", "~/stage/abc"))
    monkeypatch.setattr(stdio, "_submit_local_dir", lambda cp: "cj_d")
    monkeypatch.setattr(stdio, "_poll", _running_then(terminal="succeeded", running_cycles=1))
    monkeypatch.setattr(stdio, "_result", lambda job_id: {"summaryMarkdown": "", "findings": []})
    monkeypatch.setattr(stdio, "_unstage", lambda host_sub: events.append(f"unstage:{host_sub}"))
    ctx = FakeCtx()
    resp = asyncio.run(stdio.review_dir("/tmp/x", ctx, wait_secs=100))

    assert resp["status"] == "succeeded"
    assert events == ["sweep", "unstage:~/stage/abc"]  # terminal job -> staged copy cleaned up
    assert len(ctx.calls) == 2


def test_stdio_review_dir_running_leaves_staging(monkeypatch):
    events: list[str] = []
    monkeypatch.setattr(stdio, "REMOTE_URL", "")
    monkeypatch.setattr(stdio, "_sweep_stale_staging", lambda: None)
    monkeypatch.setattr(stdio, "_stage_dir", lambda path: ("/review-staging/abc", "~/stage/abc"))
    monkeypatch.setattr(stdio, "_submit_local_dir", lambda cp: "cj_r")
    monkeypatch.setattr(stdio, "_poll", lambda job_id: {"status": "running"})
    monkeypatch.setattr(stdio, "_unstage", lambda host_sub: events.append("unstage"))
    ctx = FakeCtx()
    resp = asyncio.run(stdio.review_dir("/tmp/x", ctx, wait_secs=0))

    assert resp["status"] == "running"
    assert events == []  # still running -> staged files left in place for the worker


# ---- schema regression guard --------------------------------------------

def test_ctx_not_exposed_in_tool_schema():
    for mod in (http, stdio):
        tools = {t.name: t for t in asyncio.run(mod.mcp.list_tools())}
        props = tools["review_pr"].inputSchema.get("properties", {})
        assert "ctx" not in props
        assert "pr_url" in props
