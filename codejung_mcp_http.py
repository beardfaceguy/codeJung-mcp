#!/usr/bin/env python3
"""Remote (streamable-HTTP) codeJung MCP server — runs ON the codeJung host.

Unlike codejung_mcp.py (stdio + SSH-to-loopback, for client machines), this
variant runs on the Pi next to the codeJung API and talks to it directly over
localhost. It is meant to sit behind Caddy (TLS + bearer auth) so remote / web
MCP clients (Cursor via URL, ChatGPT/Grok connectors, etc.) can use codeJung
over the internet.

Only PR-based tools are exposed (remote clients have no local directory to
review, so review_dir is intentionally omitted).

Config (env vars):
  CODEJUNG_API                base URL of the local API   (default http://127.0.0.1:8080)
  CODEJUNG_ENV_PATH           path to codejung.env         (default ~/codeJung/deploy/codejung.env)
  CODEJUNG_SERVICE_API_TOKEN  API token (overrides reading it from codejung.env)
  CODEJUNG_MCP_HTTP_HOST      bind host                    (default 127.0.0.1 — front with Caddy)
  CODEJUNG_MCP_HTTP_PORT      bind port                    (default 8765)
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

import anyio
from mcp.server.fastmcp import Context, FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

API = os.environ.get("CODEJUNG_API", "http://127.0.0.1:8080")
HOST = os.environ.get("CODEJUNG_MCP_HTTP_HOST", "127.0.0.1")
PORT = int(os.environ.get("CODEJUNG_MCP_HTTP_PORT", "8765"))

_PR_URL_RE = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+/pull/\d+$")
_JOB_ID_RE = re.compile(r"^cj_[0-9a-f]+$")
_POLL_INTERVAL = 15  # seconds between job polls (module-level so tests can shrink it)


def _load_token() -> str:
    tok = os.environ.get("CODEJUNG_SERVICE_API_TOKEN")
    if tok:
        return tok
    env_path = os.environ.get(
        "CODEJUNG_ENV_PATH", os.path.expanduser("~/codeJung/deploy/codejung.env"))
    with open(env_path) as fh:
        for line in fh:
            if line.startswith("CODEJUNG_SERVICE_API_TOKEN="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError("CODEJUNG_SERVICE_API_TOKEN not found (env or codejung.env)")


TOKEN = _load_token()


def _api(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        API + path, data=data, method=method,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise RuntimeError(f"codeJung API {method} {path} -> {exc.code}: {detail}") from None


def _backend_health() -> dict:
    """Probe the codeJung REST API's unauthenticated readiness endpoint."""
    try:
        with urllib.request.urlopen(API + "/v1/health", timeout=5) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:  # network error, non-200, bad JSON — all "not ready"
        return {"status": "unreachable", "error": str(exc)[:200]}


def _submit(pr_url: str, post: bool = True) -> str:
    if not _PR_URL_RE.match(pr_url):
        raise ValueError(f"invalid GitHub PR URL: {pr_url!r}")
    payload: dict = {"source": {"type": "github_pr", "prUrl": pr_url}}
    if not post:
        payload["reviewConfig"] = {"post": False}
    return _api("POST", "/v1/jobs", payload)["jobId"]


def _poll(job_id: str) -> dict:
    if not _JOB_ID_RE.match(job_id):
        raise ValueError(f"invalid job id: {job_id!r}")
    return _api("GET", f"/v1/jobs/{job_id}")


def _result(job_id: str) -> dict:
    if not _JOB_ID_RE.match(job_id):
        raise ValueError(f"invalid job id: {job_id!r}")
    return _api("GET", f"/v1/jobs/{job_id}/result")


mcp = FastMCP("codejung", host=HOST, port=PORT)


@mcp.tool()
def submit_review(pr_url: str, post_comments: bool = True) -> dict:
    """Submit a GitHub PR for codeJung review and return immediately.

    The review runs a multi-model pipeline that typically takes ~3-5 minutes
    (sometimes longer). Tell the user it's running and will take a few minutes,
    then poll get_review.
    post_comments=False reviews without posting inline comments to the PR —
    findings come back via get_review only. Default True.
    Returns {"jobId": "cj_...", "status": "queued"} — poll with get_review, which
    reports a live `phase` (current pipeline step) while the review runs.
    """
    return {"jobId": _submit(pr_url, post=post_comments), "status": "queued"}


@mcp.tool()
def get_review(job_id: str) -> dict:
    """Get a review job's status, plus its result once it has succeeded.

    A "running" status is normal — reviews take ~3-5 minutes. Tell the user it's
    still in progress and check again in a minute or two.

    While running, the response carries a `phase` field naming the current
    pipeline step (e.g. "chunking", "pass2", "reconcile", "posting") — surface it
    so the user sees which step the review is on. Once "succeeded", the response
    adds `summaryMarkdown` + `findings`.
    """
    job = _poll(job_id)
    status = job.get("status", "unknown")
    resp = {"jobId": job_id, "status": status}
    if job.get("phase"):
        resp["phase"] = job["phase"]  # current pipeline step, e.g. "pass2", "reconcile"
    if status == "succeeded":
        r = _result(job_id)
        resp["summaryMarkdown"] = r.get("summaryMarkdown", "")
        resp["findings"] = r.get("findings", [])
    return resp


async def _await_job(ctx: Context, job_id: str, wait_secs: int, label: str) -> dict | None:
    """Poll a job to completion, emitting an MCP progress notification each cycle
    so the client sees a heartbeat and does not time out mid-review. Returns the
    terminal response, or None if wait_secs elapses while it is still running.

    report_progress no-ops unless the client sent a progressToken, so this is
    safe for clients that don't support progress. Blocking API calls run in a
    worker thread so the event loop stays free to flush notifications.
    """
    start = time.monotonic()
    polls = 0
    while True:
        job = await anyio.to_thread.run_sync(_poll, job_id)
        status = job.get("status", "unknown")
        polls += 1
        elapsed = int(time.monotonic() - start)
        phase = job.get("phase")
        detail = f" · {phase}" if phase else ""
        await ctx.report_progress(
            min(elapsed, wait_secs), wait_secs,
            f"{label}: {status}{detail} ({elapsed}s elapsed, poll {polls})")
        if status in ("succeeded", "failed", "timed_out"):
            resp = {"jobId": job_id, "status": status}
            if status == "succeeded":
                r = await anyio.to_thread.run_sync(_result, job_id)
                resp["summaryMarkdown"] = r.get("summaryMarkdown", "")
                resp["findings"] = r.get("findings", [])
            else:
                resp["error"] = job.get("error")
            return resp
        if time.monotonic() - start >= wait_secs:
            return None
        await anyio.sleep(_POLL_INTERVAL)


@mcp.tool()
async def review_pr(pr_url: str, ctx: Context, wait_secs: int = 240,
                    post_comments: bool = True) -> dict:
    """Submit a GitHub PR and wait up to wait_secs for the review to finish.

    TIMING: a review typically takes ~3-5 minutes (sometimes longer). While it
    waits, this streams an MCP progress notification every ~15s (a heartbeat)
    naming the current pipeline step (e.g. "running · reconcile"), so
    progress-aware clients won't time out mid-review. Still, if it returns status
    "running", that's expected: poll get_review with the jobId — its response
    carries the same `phase` field for clients that don't consume progress.
    If it completes in that window, returns the review markdown + findings.
    Never blocks indefinitely. Set wait_secs=0 to return right after submit.
    post_comments=False reviews without posting inline comments to the PR —
    findings come back here only. Default True.
    """
    job_id = await anyio.to_thread.run_sync(lambda: _submit(pr_url, post=post_comments))
    resp = await _await_job(ctx, job_id, wait_secs, pr_url)
    if resp is not None:
        return resp
    return {"jobId": job_id, "status": "running",
            "hint": (f"still running after {wait_secs}s — normal, not a hang; reviews "
                     f"take ~3-5 min. Tell the user it's in progress, then call "
                     f"get_review('{job_id}') again shortly.")}


@mcp.tool()
def health() -> dict:
    """Check that codeJung is up and ready to accept reviews.

    Use this to verify the service before submitting a review. Returns
    {"mcp": "ok", "backend": {...}} — "backend" is the REST API's own health
    (status "ok" when ready, "unreachable" when the API is down).
    """
    return {"mcp": "ok", "backend": _backend_health()}


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_request: Request) -> JSONResponse:
    """Unauthenticated liveness/readiness probe for the MCP process itself.

    A 200 here means this MCP process is serving. "backend" reports the REST
    API's readiness; the status code goes 503 when the backend is not ready so
    an uptime monitor / reverse proxy can see a degraded state.
    """
    backend = _backend_health()
    ready = backend.get("status") == "ok"
    return JSONResponse(
        {"status": "ok" if ready else "degraded", "mcp": "ok", "backend": backend},
        status_code=200 if ready else 503,
    )


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
