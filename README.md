# codeJung-mcp

An [MCP](https://modelcontextprotocol.io) server that exposes the self-hosted
[codeJung](https://github.com/beardfaceguy/codeJung) code-review service as tools
any MCP client can call — Claude Code, Claude Desktop, Cursor, Codex CLI,
Windsurf, and others.

It is a thin client: it depends only on codeJung's stable `/v1` HTTP API, not on
any codeJung internals. The service binds to loopback on its host, so every call
is executed on that host over SSH — the service token is read there and never
leaves the machine.

## Target API

Speaks codeJung's **`/v1`** job API (`POST /v1/jobs`, `GET /v1/jobs/{id}`,
`GET /v1/jobs/{id}/result`). If codeJung ever breaks `/v1`, bump this client to
match. The quickest health check is the smoke test in [Verifying](#verifying).

## Tools

| Tool | Purpose |
|------|---------|
| `review_pr(pr_url, wait_secs=300)` | Submit a GitHub PR, wait up to `wait_secs`, return review markdown + findings. |
| `submit_review(pr_url)` | Submit a PR and return `jobId` immediately (for long reviews). |
| `get_review(job_id)` | Status, plus result once the job has succeeded. |
| `review_dir(path, wait_secs=300)` | Review a **local directory** (full-file scan, no PR needed) — stages it to the host and reviews every source file. |

### Blocking vs non-blocking

`review_pr` / `review_dir` wait *at most* `wait_secs` for the review, emitting
progress to stderr, then return `{"status":"running","jobId":...}` if it hasn't
finished — they never block indefinitely. Fetch a still-running result later with
`get_review(job_id)`. Set `wait_secs=0` to return as soon as the job is
submitted. For clients with short tool-call timeouts, prefer the non-blocking
pattern: `submit_review` → `get_review`.

## Requirements

- python3 ≥ 3.10, `pip install -r requirements.txt` (installs `mcp`)
- openssh client + passwordless SSH to the codeJung host
- `rsync` on both machines (only needed for `review_dir`)
- codeJung service running on the host (`~/codeJung/deploy`, api + worker)

## Configuration (env vars)

| Var | Default | Meaning |
|-----|---------|---------|
| `CODEJUNG_SSH_HOST` | `codejung` | SSH host running the service |
| `CODEJUNG_ENV_PATH` | `~/codeJung/deploy/codejung.env` | path to `codejung.env` on the host (holds the service token) |
| `CODEJUNG_REVIEW_STAGING` | `~/cj-review-staging` | host dir bind-mounted into the worker at `/review-staging` (used by `review_dir`) |

> `review_dir` requires the worker container to bind-mount the staging dir. In
> `deploy/docker-compose.yml` the worker must have:
> `- ${CODEJUNG_REVIEW_STAGING:-/home/you/cj-review-staging}:/review-staging:ro`

## Install per client

The same stdio server works with any local MCP client. Ready-made config
snippets are in [`client-configs/`](client-configs/).

### Claude Code

```bash
claude mcp add codejung -s user -- python3 /path/to/codeJung-mcp/codejung_mcp.py
```

### Cursor / Claude Desktop / Windsurf

Merge the `codejung` entry from `client-configs/mcp.json` into the client's MCP
config (paths listed in `client-configs/README.md`). Edit the absolute path to
match where you cloned this repo. MCP tools run in **Agent mode**, and the
selected model is irrelevant — the client (not the model) drives the server, so
it works with any tool-calling model (GPT, Claude, Gemini, Grok, …).

### Codex CLI

Merge `client-configs/codex-config.toml` into `~/.codex/config.toml`.

## Verifying

```bash
pip install -r requirements.txt
# smoke test — should print the tool names:
python3 -c "import asyncio, codejung_mcp as m; \
print([t.name for t in asyncio.run(m.mcp.list_tools())])"
# end-to-end — the host's models must be reachable:
python3 -c "import codejung_mcp as m; print(m.review_pr('https://github.com/OWNER/REPO/pull/N')['status'])"
```

## Usage in a session

> "review https://github.com/owner/repo/pull/123 with codejung"
> "use codejung to review the directory ./my-service"

## Security notes

- The service token is read on the host and never transits to the client.
- PR URLs and job IDs are strictly validated before being interpolated into the
  remote command, to prevent shell injection.
- `review_dir` copies the target directory to the host (excluding `.git`,
  `node_modules`, virtualenvs, build output) and removes the staged copy when the
  review finishes.
