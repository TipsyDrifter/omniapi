# End-to-end scripts (need real keys + network; not collected by pytest)

- `stdio_e2e.py` — spawns the server over stdio exactly like Claude Code (uv run omniapi-mcp), lists tools, calls two tools.
- `daemon_e2e.py` — against a running `omni serve`: REST health/status, two concurrent MCP sessions over streamable-http sharing one runtime, WebSocket events, calls ledger, costs.

Run with the project venv python from `mcp/`.

## Sandboxes never read the repo's `mcp/.env`

The offline scripts (`video`, `generate`, `openrouter_images`, `artifacts`, `chat`, `settings`) expect a sandbox
whose providers and model lists come from nothing but the sandbox itself. The service normally reads
`mcp/.env` of the checkout it runs from (and `.env` in its working directory); a real key in there would
turn providers on and change the model lists. So:

- an **offline sandbox** (`OMNIAPI_OFFLINE=1`) leaves the checkout's `mcp/.env` and the working directory's
  `.env` unread **by default**; the `.env` inside its own `OMNIAPI_HOME` is still read;
- `OMNIAPI_SKIP_REPO_ENV=1` asks for the same on any service (`video_e2e.py` sets it explicitly);
  `OMNIAPI_SKIP_REPO_ENV=0` reads the files even in an offline sandbox;
- `GET /api/status` says which way it went (`service.repo_env_skipped`, `service.env_file`).

Start sandboxes with a fresh empty `OMNIAPI_HOME`, `OMNIAPI_DEV=1 OMNIAPI_OFFLINE=1` and a port that is not 7788.
- `runs_e2e.py` — run_agent on claude+codex via MCP, poll get_run, resume on claude (needs SCRATCH env).
- `resume_e2e.py` — codex resume + gemini start/resume via MCP.
- `settings_m4_e2e.py` — offline sandbox, no keys: shadowed .env key, connection status (survives a restart: two phases), `/api/tools`, Claude Code's MCP entry in a stand-in file (`OMNIAPI_CLAUDE_CONFIG`); setup in its docstring.
