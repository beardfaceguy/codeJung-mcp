# codejung-mcp — client configs

The same stdio server (`codejung_mcp.py`) works with any **local** MCP client
(one that spawns a subprocess). Web/hosted assistants (ChatGPT web, Grok web)
need a remote HTTP server instead — see "Web agents" below.

## Local clients (stdio)

| Client | Config file | Use |
|--------|-------------|-----|
| Claude Code | `~/.claude.json` | already registered: `claude mcp add codejung -s user -- python3 …/codejung_mcp.py` |
| Claude Desktop | Linux: `~/.config/Claude/claude_desktop_config.json` • macOS: `~/Library/Application Support/Claude/claude_desktop_config.json` | merge `mcp.json` |
| Cursor | `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (project) | merge `mcp.json` |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` | merge `mcp.json` |
| VS Code (Continue etc.) | client-specific; most accept the `mcpServers` shape | merge `mcp.json` |
| Codex CLI | `~/.codex/config.toml` | merge `codex-config.toml` |

`mcp.json` uses the near-universal `mcpServers` JSON shape; most clients accept
it verbatim. Merge the `codejung` entry into any existing config rather than
overwriting the file.

Each client's host machine needs: python3 ≥ 3.10 with `mcp` installed, and
passwordless SSH to the codeJung host (default `codejung`).

## Web agents (ChatGPT, Grok) — remote HTTP transport

These cannot spawn a local process; they connect only to a remote MCP server
over HTTPS. That requires running `codejung_mcp.py` with the streamable-HTTP
transport on a network-reachable host and adding authentication — which also
means deciding how to expose the codeJung host (LAN-only + tunnel, or public
+ TLS). Not enabled by default; ask to set this up when needed.
