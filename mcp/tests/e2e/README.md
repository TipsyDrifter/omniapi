# End-to-end scripts (need real keys + network; not collected by pytest)

- `stdio_e2e.py` — spawns the server over stdio exactly like Claude Code (uv run omniapi-mcp), lists tools, calls two tools.
- `daemon_e2e.py` — against a running `omni serve`: REST health/status, two concurrent MCP sessions over streamable-http sharing one runtime, WebSocket events, calls ledger, costs.

Run with the project venv python from `mcp/`.
- `runs_e2e.py` — run_agent on claude+codex via MCP, poll get_run, resume on claude (needs SCRATCH env).
- `resume_e2e.py` — codex resume + gemini start/resume via MCP.
