#!/usr/bin/env python3
"""MCP server for the self-hosted codeJung review service.

Exposes codeJung's PR-review API as MCP tools. The service binds to loopback on
its host (the `codejung` Raspberry Pi), so every call is executed on the host
via SSH — the service token is read there and never leaves that machine, exactly
mirroring cj-review-remote.sh.

Config (env vars):
  CODEJUNG_SSH_HOST   SSH host running the service   (default: codejung)
  CODEJUNG_ENV_PATH   path to codejung.env on host   (default: ~/codeJung/deploy/codejung.env)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid

from mcp.server.fastmcp import FastMCP

SSH_HOST = os.environ.get("CODEJUNG_SSH_HOST", "codejung")
ENV_PATH = os.environ.get("CODEJUNG_ENV_PATH", "~/codeJung/deploy/codejung.env")
# Host dir bind-mounted into the worker container at /review-staging. review_dir
# stages a copy of the target directory here so the containerized worker can see
# it. Must match the bind mount in deploy/docker-compose.yml.
STAGING_HOST_DIR = os.environ.get("CODEJUNG_REVIEW_STAGING", "~/cj-review-staging")
STAGING_CONTAINER_DIR = "/review-staging"

# Strict input validation: these values are interpolated into a remote shell
# command, so they must not contain shell metacharacters. The regexes below
# permit only the exact shapes we expect, rejecting everything else.
_PR_URL_RE = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+/pull/\d+$")
_JOB_ID_RE = re.compile(r"^cj_[0-9a-f]+$")


def _safe_config_path(value: str, name: str) -> str:
    """Operator-supplied paths are interpolated into a remote shell command (and
    must stay unquoted so a leading ~ expands). Allow-list path-safe characters
    only — this rejects globs ([ ]), comments (#), quotes, whitespace, and every
    command/redirection metacharacter, so interpolation cannot inject commands."""
    if not re.fullmatch(r"[A-Za-z0-9._/~-]+", value):
        raise ValueError(f"{name} has non-path characters: {value!r}")
    return value


ENV_PATH = _safe_config_path(ENV_PATH, "CODEJUNG_ENV_PATH")
STAGING_HOST_DIR = _safe_config_path(STAGING_HOST_DIR, "CODEJUNG_REVIEW_STAGING")

mcp = FastMCP("codejung")


def _remote(curl_cmd: str) -> str:
    """Run a curl against the loopback API on the host; token stays on the host."""
    # cut -f2- (not -f2): tokens may legitimately contain '=' characters.
    token_expr = f"TOKEN=$(grep ^CODEJUNG_SERVICE_API_TOKEN {ENV_PATH} | cut -d= -f2-)"
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", SSH_HOST,
         f"{token_expr}; {curl_cmd}"],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ssh/curl to {SSH_HOST} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def _submit(pr_url: str) -> str:
    if not _PR_URL_RE.match(pr_url):
        raise ValueError(f"invalid GitHub PR URL: {pr_url!r}")
    body = json.dumps({"source": {"type": "github_pr", "prUrl": pr_url}})
    out = _remote(
        "curl -s -X POST http://127.0.0.1:8080/v1/jobs "
        '-H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" '
        f"-d '{body}'"
    )
    return json.loads(out)["jobId"]


def _submit_local_dir(container_path: str) -> str:
    body = json.dumps({"source": {"type": "local_dir", "localDir": container_path}})
    out = _remote(
        "curl -s -X POST http://127.0.0.1:8080/v1/jobs "
        '-H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" '
        f"-d '{body}'"
    )
    return json.loads(out)["jobId"]


def _stage_dir(local_path: str) -> tuple[str, str]:
    """rsync a local directory into the Pi's staging area (bind-mounted into the
    worker). Returns (container_path, host_staging_subdir) for submit + cleanup."""
    local_path = os.path.abspath(os.path.expanduser(local_path))
    if not os.path.isdir(local_path):
        raise ValueError(f"not a directory: {local_path!r}")
    stage_id = uuid.uuid4().hex[:12]
    host_sub = f"{STAGING_HOST_DIR.rstrip('/')}/{stage_id}"
    # ensure the staging subdir exists on the host
    subprocess.run(["ssh", "-o", "BatchMode=yes", SSH_HOST, f"mkdir -p {host_sub}"],
                   check=True, capture_output=True, text=True, timeout=30)
    # copy contents (trailing slash) — skip VCS/build/venv noise the scanner ignores anyway
    proc = subprocess.run(
        ["rsync", "-az", "--delete",
         "--exclude=.git", "--exclude=__pycache__", "--exclude=node_modules",
         "--exclude=.venv", "--exclude=venv", "--exclude=dist", "--exclude=build",
         f"{local_path}/", f"{SSH_HOST}:{host_sub}/"],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        _unstage(host_sub)
        raise RuntimeError(f"rsync to {SSH_HOST} failed: {proc.stderr.strip()}")
    return f"{STAGING_CONTAINER_DIR}/{stage_id}", host_sub


def _unstage(host_sub: str) -> None:
    subprocess.run(["ssh", "-o", "BatchMode=yes", SSH_HOST, f"rm -rf {host_sub}"],
                   capture_output=True, text=True, timeout=30)


def _sweep_stale_staging(max_age_min: int = 360) -> None:
    """Best-effort removal of staging dirs orphaned by timed-out jobs. The age
    threshold is deliberately generous (default 6h) so an in-flight review is
    never swept out from under a running job. Failures are ignored.

    Only dirs whose names match our own stage-id shape (12 hex chars) are
    removed, so even a misconfigured STAGING_HOST_DIR cannot delete unrelated
    directories."""
    hex12 = "[0-9a-f]" * 12  # matches uuid4().hex[:12] created by _stage_dir
    try:
        subprocess.run(
            ["ssh", "-o", "BatchMode=yes", SSH_HOST,
             f"find {STAGING_HOST_DIR} -mindepth 1 -maxdepth 1 -type d "
             f"-name '{hex12}' -mmin +{max_age_min} -exec rm -rf {{}} + 2>/dev/null || true"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        pass


def _poll(job_id: str) -> dict:
    if not _JOB_ID_RE.match(job_id):
        raise ValueError(f"invalid job id: {job_id!r}")
    out = _remote(
        f"curl -s http://127.0.0.1:8080/v1/jobs/{job_id} "
        '-H "Authorization: Bearer $TOKEN"'
    )
    return json.loads(out)


def _result(job_id: str) -> dict:
    if not _JOB_ID_RE.match(job_id):
        raise ValueError(f"invalid job id: {job_id!r}")
    out = _remote(
        f"curl -s http://127.0.0.1:8080/v1/jobs/{job_id}/result "
        '-H "Authorization: Bearer $TOKEN"'
    )
    return json.loads(out)


def _terminal_response(job_id: str, status: str, job: dict) -> dict:
    """Build the tool response for a job that has reached a terminal state."""
    resp = {"jobId": job_id, "status": status}
    if status == "succeeded":
        r = _result(job_id)
        resp["summaryMarkdown"] = r.get("summaryMarkdown", "")
        resp["findings"] = r.get("findings", [])
    else:
        resp["error"] = job.get("error")
    return resp


def _wait_for_job(job_id: str, wait_secs: int, *, label: str = "") -> dict | None:
    """Poll a job for up to wait_secs, emitting progress to stderr each cycle so
    the caller can see it is alive (never a silent multi-minute hang). Returns
    the terminal response once the job finishes, or None if wait_secs elapses
    while the job is still running (caller then hands back a resumable jobId)."""
    start = time.monotonic()
    polls = 0
    while True:
        job = _poll(job_id)
        status = job.get("status", "unknown")
        polls += 1
        print(f"[codejung] {label or job_id}: {status} "
              f"({int(time.monotonic() - start)}s, poll {polls})",
              file=sys.stderr, flush=True)
        if status in ("succeeded", "failed", "timed_out"):
            return _terminal_response(job_id, status, job)
        if time.monotonic() - start >= wait_secs:
            return None
        time.sleep(15)


def _running_response(job_id: str, wait_secs: int) -> dict:
    return {"jobId": job_id, "status": "running",
            "hint": (f"still running after {wait_secs}s; "
                     f"call get_review('{job_id}') to fetch the result when done")}


@mcp.tool()
def submit_review(pr_url: str) -> dict:
    """Submit a GitHub PR for codeJung review and return immediately.

    Args:
        pr_url: Full GitHub PR URL, e.g. https://github.com/owner/repo/pull/123
    Returns:
        {"jobId": "cj_...", "status": "queued"} — poll with get_review.
    """
    return {"jobId": _submit(pr_url), "status": "queued"}


@mcp.tool()
def get_review(job_id: str) -> dict:
    """Get the status of a review job, plus its result if it has finished.

    Args:
        job_id: The jobId returned by submit_review / review_pr.
    Returns:
        {"status": ..., "summaryMarkdown": ..., "findings": [...]} — the last
        two are present only once status is "succeeded".
    """
    status = _poll(job_id).get("status", "unknown")
    resp = {"jobId": job_id, "status": status}
    if status == "succeeded":
        r = _result(job_id)
        resp["summaryMarkdown"] = r.get("summaryMarkdown", "")
        resp["findings"] = r.get("findings", [])
    return resp


@mcp.tool()
def review_pr(pr_url: str, wait_secs: int = 300) -> dict:
    """Submit a GitHub PR for review and wait a bounded time for the result.

    Submits the job, then waits up to wait_secs (emitting progress to stderr).
    If the review finishes in that window, returns the markdown + findings. If
    not, returns immediately with status "running" and a jobId to poll via
    get_review — it never blocks indefinitely. Set wait_secs=0 to return as soon
    as the job is submitted (equivalent to submit_review). For guaranteed
    non-blocking use in clients with short tool-call timeouts, prefer
    submit_review + get_review.

    Args:
        pr_url: Full GitHub PR URL, e.g. https://github.com/owner/repo/pull/123
        wait_secs: Max seconds to wait inline for completion (default 300).
    Returns:
        succeeded → {"jobId","status","summaryMarkdown","findings"};
        failed/timed_out → {"jobId","status","error"};
        still running → {"jobId","status":"running","hint": ...}.
    """
    job_id = _submit(pr_url)
    resp = _wait_for_job(job_id, wait_secs, label=pr_url)
    return resp if resp is not None else _running_response(job_id, wait_secs)


@mcp.tool()
def review_dir(path: str, wait_secs: int = 300) -> dict:
    """Review a local directory of code with codeJung (full-file scan, no PR needed).

    Stages the directory to the codeJung host, runs a full-file review of every
    source file in it, and waits up to wait_secs (emitting progress to stderr).
    If the review finishes in that window, returns the markdown + findings; if
    not, returns status "running" with a jobId to poll via get_review — it never
    blocks indefinitely. Use this for code that is not (yet) a GitHub PR.

    Args:
        path: Local directory path on this machine to review.
        wait_secs: Max seconds to wait inline for completion (default 300).
    Returns:
        succeeded → {"jobId","status","summaryMarkdown","findings"};
        failed/timed_out → {"jobId","status","error"};
        still running → {"jobId","status":"running","hint": ...}.
    """
    _sweep_stale_staging()  # reap staging dirs orphaned by earlier timed-out jobs
    container_path, host_sub = _stage_dir(path)

    # Submit first, in its own guard, so job_id is unambiguously bound.
    try:
        job_id = _submit_local_dir(container_path)
    except Exception:
        _unstage(host_sub)
        raise

    # Cleanup is deliberately NOT in a `finally`: the worker reads the staged
    # files *during* the review, so unstaging while the job is still running
    # (the wait-elapsed path) would pull the rug out from under it. We unstage
    # only once the job is terminal, or on an error path.
    try:
        resp = _wait_for_job(job_id, wait_secs, label=path)
    except Exception:
        _unstage(host_sub)
        raise

    if resp is not None:
        _unstage(host_sub)  # job is terminal — staged files no longer needed
        return resp
    # Still running: leave the staged files in place so the job (and a later
    # get_review) can complete. The copy is reaped by _sweep_stale_staging().
    return _running_response(job_id, wait_secs)


if __name__ == "__main__":
    mcp.run()
