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

from mcp.server.fastmcp import FastMCP

API = os.environ.get("CODEJUNG_API", "http://127.0.0.1:8080")
HOST = os.environ.get("CODEJUNG_MCP_HTTP_HOST", "127.0.0.1")
PORT = int(os.environ.get("CODEJUNG_MCP_HTTP_PORT", "8765"))

_PR_URL_RE = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+/pull/\d+$")
_JOB_ID_RE = re.compile(r"^cj_[0-9a-f]+$")


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

    post_comments=False reviews without posting inline comments to the PR —
    findings come back via get_review only. Default True.
    Returns {"jobId": "cj_...", "status": "queued"} — poll with get_review.
    """
    return {"jobId": _submit(pr_url, post=post_comments), "status": "queued"}


@mcp.tool()
def get_review(job_id: str) -> dict:
    """Get a review job's status, plus its result once it has succeeded."""
    status = _poll(job_id).get("status", "unknown")
    resp = {"jobId": job_id, "status": status}
    if status == "succeeded":
        r = _result(job_id)
        resp["summaryMarkdown"] = r.get("summaryMarkdown", "")
        resp["findings"] = r.get("findings", [])
    return resp


@mcp.tool()
def review_pr(pr_url: str, wait_secs: int = 240, post_comments: bool = True) -> dict:
    """Submit a GitHub PR and wait up to wait_secs for the review to finish.

    If it completes in that window, returns the review markdown + findings; if
    not, returns status "running" with a jobId to poll via get_review. Never
    blocks indefinitely. Set wait_secs=0 to return right after submit.
    post_comments=False reviews without posting inline comments to the PR —
    findings come back here only. Default True.
    """
    job_id = _submit(pr_url, post=post_comments)
    start = time.monotonic()
    while True:
        job = _poll(job_id)
        status = job.get("status", "unknown")
        if status in ("succeeded", "failed", "timed_out"):
            resp = {"jobId": job_id, "status": status}
            if status == "succeeded":
                r = _result(job_id)
                resp["summaryMarkdown"] = r.get("summaryMarkdown", "")
                resp["findings"] = r.get("findings", [])
            else:
                resp["error"] = job.get("error")
            return resp
        if time.monotonic() - start >= wait_secs:
            return {"jobId": job_id, "status": "running",
                    "hint": f"still running; call get_review('{job_id}') to fetch the result"}
        time.sleep(15)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
