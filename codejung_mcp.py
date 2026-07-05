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

mcp = FastMCP("codejung")


def _remote(curl_cmd: str) -> str:
    """Run a curl against the loopback API on the host; token stays on the host."""
    token_expr = f"TOKEN=$(grep ^CODEJUNG_SERVICE_API_TOKEN {ENV_PATH} | cut -d= -f2)"
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
def review_pr(pr_url: str, timeout_mins: int = 30) -> dict:
    """Submit a GitHub PR, wait for the review to finish, and return findings.

    This is the one-shot workhorse: it submits, polls until the review completes
    (or times out), and returns the review markdown + structured findings.

    Args:
        pr_url: Full GitHub PR URL, e.g. https://github.com/owner/repo/pull/123
        timeout_mins: How long to wait before giving up (default 30).
    Returns:
        {"jobId","status","summaryMarkdown","findings"} on success, or
        {"jobId","status","error"} if it failed/timed out.
    """
    job_id = _submit(pr_url)
    deadline = time.monotonic() + timeout_mins * 60
    while time.monotonic() < deadline:
        job = _poll(job_id)
        status = job.get("status", "unknown")
        if status in ("succeeded", "failed", "timed_out"):
            if status == "succeeded":
                r = _result(job_id)
                return {
                    "jobId": job_id, "status": status,
                    "summaryMarkdown": r.get("summaryMarkdown", ""),
                    "findings": r.get("findings", []),
                }
            return {"jobId": job_id, "status": status, "error": job.get("error")}
        time.sleep(15)
    return {"jobId": job_id, "status": "timed_out",
            "error": f"still running after {timeout_mins} min; poll get_review('{job_id}') later"}


@mcp.tool()
def review_dir(path: str, timeout_mins: int = 30) -> dict:
    """Review a local directory of code with codeJung (full-file scan, no PR needed).

    Stages the directory to the codeJung host, runs a full-file review of every
    source file in it, and returns findings. Use this for code that is not (yet)
    a GitHub PR — e.g. a working tree or a standalone script directory.

    Args:
        path: Local directory path on this machine to review.
        timeout_mins: How long to wait before giving up (default 30).
    Returns:
        {"jobId","status","summaryMarkdown","findings"} on success, or
        {"jobId"/None,"status","error"} on failure/timeout.
    """
    container_path, host_sub = _stage_dir(path)
    try:
        job_id = _submit_local_dir(container_path)
        deadline = time.monotonic() + timeout_mins * 60
        while time.monotonic() < deadline:
            job = _poll(job_id)
            status = job.get("status", "unknown")
            if status in ("succeeded", "failed", "timed_out"):
                if status == "succeeded":
                    r = _result(job_id)
                    return {
                        "jobId": job_id, "status": status,
                        "summaryMarkdown": r.get("summaryMarkdown", ""),
                        "findings": r.get("findings", []),
                    }
                return {"jobId": job_id, "status": status, "error": job.get("error")}
            time.sleep(15)
        return {"jobId": job_id, "status": "timed_out",
                "error": f"still running after {timeout_mins} min"}
    finally:
        _unstage(host_sub)


if __name__ == "__main__":
    mcp.run()
