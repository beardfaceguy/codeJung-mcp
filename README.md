# codeJung-mcp

An [MCP](https://modelcontextprotocol.io) server that exposes the self-hosted
[codeJung](https://github.com/beardfaceguy/codeJung) code-review service as tools
any MCP client can call — Claude Code, Claude Desktop, Cursor, Codex CLI,
Windsurf, and web/URL-based clients.

It's a thin client over codeJung's stable `/v1` HTTP API. There are **two ways
to connect** (see [Installation](#installation)):

- **Remote (recommended):** point your client at the hosted URL — nothing to
  install, works from anywhere.
- **Local:** run the stdio server (`codejung_mcp.py`) from a clone of this repo.

## Tools

| Tool | Purpose |
|------|---------|
| `review_pr(pr_url, wait_secs=300, post_comments=True)` | Submit a GitHub PR, wait up to `wait_secs`, return review markdown + findings. |
| `submit_review(pr_url, post_comments=True)` | Submit a PR and return `jobId` immediately (for long reviews). |
| `get_review(job_id)` | Status, plus result once the job has succeeded. |
| `review_dir(path, wait_secs=300)` | Review a **local directory** (full-file scan, no PR needed). *Local stdio + SSH mode only.* |

The selected LLM in your client is irrelevant — the client (not the model)
drives the server, so it works with any tool-calling model (GPT, Claude, Gemini,
Grok, …). MCP tools run in the client's **Agent mode**.

### Review-only (don't post to the PR)

`review_pr` / `submit_review` take `post_comments` (default `True`). Pass
`post_comments=False` to review a PR **without posting inline comments** — the
findings come back to you (`findings` array + `summaryMarkdown`) and the PR is
left untouched. Good for "review-then-decide" agent workflows.

### Expect a few minutes

A review runs a multi-model pipeline and typically takes **~3–5 minutes**
(sometimes longer under load). That's normal, not a hang. Tell the user a review
is in progress; a `"running"` status just means "check back shortly." Model
calls are individually deadline-capped and retried, so a review can't hang
indefinitely — it just isn't instant.

### Blocking vs non-blocking

`review_pr` / `review_dir` wait *at most* `wait_secs`, emitting progress to
stderr, then return `{"status":"running","jobId":...}` if not finished — they
never block indefinitely. Fetch a still-running result later with
`get_review(job_id)`. Set `wait_secs=0` to return right after submit. For clients
with short tool-call timeouts, prefer `submit_review` → `get_review`.

## Installation

Pick **one** of the two options below. You'll need the codeJung **bearer token**
either way — ask the maintainer (it's the `CODEJUNG_SERVICE_API_TOKEN`).

### Option A — Remote URL (recommended; nothing to install)

The service is hosted at **`https://codejung.wint3rmute.com/mcp`** — always on,
TLS, bearer-token gated. No clone, no Python, no SSH; works from any network.

**Cursor** — merge into `~/.cursor/mcp.json` (under `mcpServers`):

```json
{
  "mcpServers": {
    "codejung": {
      "url": "https://codejung.wint3rmute.com/mcp",
      "headers": { "Authorization": "Bearer <TOKEN>" }
    }
  }
}
```

**Claude Code** — one command:

```bash
claude mcp add --transport http codejung https://codejung.wint3rmute.com/mcp \
  --header "Authorization: Bearer <TOKEN>"
```

**Claude Desktop / Windsurf / other URL-capable clients** — add the same
`url` + `headers` entry to that client's MCP config file.

Then restart the client, switch to **Agent mode**, and try:
*"review https://github.com/owner/repo/pull/123 with codejung"*.

> Web clients (ChatGPT/Grok) can point at the same URL, but their MCP client and
> auth support vary by product/plan (some expect OAuth rather than a static
> bearer header). The endpoint is a standards-compliant streamable-HTTP MCP
> server; whether a given product accepts it depends on that product.

### Option B — Local stdio server (from a clone)

Run the stdio server yourself. Needed for `review_dir` (local-directory reviews),
or if you'd rather not use the hosted URL.

1. **Clone + install deps:**
   ```bash
   git clone git@github.com:beardfaceguy/codeJung-mcp.git
   cd codeJung-mcp
   pip install -r requirements.txt        # installs `mcp`; needs python3 ≥ 3.10
   ```
2. **Choose how it reaches codeJung** (via env vars — see [Configuration](#configuration)):
   - **Remote API over HTTPS** (works anywhere):
     `CODEJUNG_API_URL=https://codejung.wint3rmute.com` + `CODEJUNG_API_TOKEN=<token>`
   - **SSH-to-loopback** (on the home LAN; the token is read on the host and
     never leaves it): `CODEJUNG_SSH_HOST=codejung` + passwordless SSH to that host.
3. **Register with your client**, e.g. Claude Code (remote-API mode):
   ```bash
   claude mcp add codejung -s user \
     -e CODEJUNG_API_URL=https://codejung.wint3rmute.com \
     -e CODEJUNG_API_TOKEN=<token> \
     -- python3 /ABS/PATH/codeJung-mcp/codejung_mcp.py
   ```
   - **Cursor / Claude Desktop / Windsurf:** merge `client-configs/mcp.json`
     (the `command`/`args`/`env` form), editing the absolute path + env.
   - **Codex CLI:** merge `client-configs/codex-config.toml` into `~/.codex/config.toml`.

> `review_dir` requires **SSH mode** (it rsyncs the target dir to the host); it
> is unavailable in remote-URL mode and returns a clear error there.

## Configuration (env vars)

| Var | Default | Meaning |
|-----|---------|---------|
| `CODEJUNG_API_URL` | *(unset)* | If set, the stdio server calls the public REST API over HTTPS (remote mode). |
| `CODEJUNG_API_TOKEN` | *(unset)* | Bearer token for remote mode (required when `CODEJUNG_API_URL` is set). |
| `CODEJUNG_SSH_HOST` | `codejung` | SSH host running the service (SSH mode, when `CODEJUNG_API_URL` is unset). |
| `CODEJUNG_ENV_PATH` | `~/codeJung/deploy/codejung.env` | Path to `codejung.env` on the host (SSH mode; holds the token). |
| `CODEJUNG_REVIEW_STAGING` | `~/cj-review-staging` | Host staging dir for `review_dir` (SSH mode). |

## Verifying (Option B)

```bash
pip install -r requirements.txt
# tools register?
python3 -c "import asyncio, codejung_mcp as m; print([t.name for t in asyncio.run(m.mcp.list_tools())])"
# end-to-end (remote mode): set the env first, then submit a real PR
CODEJUNG_API_URL=https://codejung.wint3rmute.com CODEJUNG_API_TOKEN=<token> \
  python3 -c "import codejung_mcp as m; print(m.review_pr('https://github.com/OWNER/REPO/pull/N', post_comments=False)['status'])"
```

For Option A, just confirm the endpoint is reachable:
```bash
curl https://codejung.wint3rmute.com/v1/health     # -> {"status":"ok"}
```

## Security notes

- The bearer token gates all `/v1/jobs*` endpoints (constant-time check) and the
  `/mcp` endpoint. Keep it secret; don't paste it in shared channels.
- In SSH mode the token is read on the host and never transits to the client.
- PR URLs and job IDs are strictly validated before interpolation into any
  remote command (prevents shell injection).
- `review_dir` copies the target dir to the host (excluding `.git`,
  `node_modules`, virtualenvs, build output) and removes the staged copy after.

## How it's hosted (maintainer reference)

codeJung is served permanently at `https://codejung.wint3rmute.com` via the
router's nginx reverse proxy (a vhost on the shared `:443`, alongside other
services), terminating a Let's Encrypt cert and proxying to the Pi's Caddy, which
routes `/` → codeJung REST API and `/mcp` → the streamable-HTTP MCP server
(`codejung_mcp_http.py`).

Host-side pieces:
- Remote MCP server: `codejung_mcp_http.py` runs on the host (venv
  `~/.codejung-mcp-venv`), systemd unit `codejung-mcp-http.service`, binds
  `127.0.0.1:8765`.
- Caddy `/mcp` route: bearer-gated via `CJ_MCP_TOKEN`, rewrites upstream Host to
  `127.0.0.1:8765` (the MCP SDK's DNS-rebinding guard only trusts localhost).

Redeploy `codejung_mcp_http.py` after editing:
```bash
scp codejung_mcp_http.py codejung:~/codeJung-mcp-http.py
ssh codejung 'sudo systemctl restart codejung-mcp-http'
```
