"""``omni`` — the OmniAPI command line.

    omni serve [--foreground] [--port 7788]   start the daemon (detached by default)
    omni status                               is it running? providers, store, uptime
    omni stop                                 stop the daemon
    omni autostart install|remove|status      run at Windows logon (Startup folder, no admin)
    omni mcp-config                           print / apply the Claude Code MCP entry
    omni run / runs / run-log                 dispatch and follow agent runs (M3)
    omni chat ["message"]                     chat with a text model; no message = interactive (M6)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

import typer

from . import __version__
from .catalog import data_home
from .daemon.app import DEFAULT_HOST, DEFAULT_PORT, clear_pid, read_pid

#: how long `omni serve` waits for the detached daemon to answer
SERVE_WAIT_SECONDS = 120

app = typer.Typer(help="OmniAPI — external-model dispatch center", no_args_is_help=True, add_completion=False)
autostart_app = typer.Typer(help="Windows logon autostart (Startup folder launcher; --task for schtasks)")
app.add_typer(autostart_app, name="autostart")

TASK_NAME = "OmniAPI Daemon"


def _log_dir() -> Path:
    d = data_home() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _env_file() -> Optional[Path]:
    """The server's .env: repo mcp/.env (dev) or ~/.omniapi/.env (installed)."""
    for cand in (Path(__file__).resolve().parents[1] / ".env", data_home() / ".env"):
        if cand.exists():
            return cand
    return None


def _health(host: str, port: int, timeout: float = 1.5) -> Optional[dict]:
    import httpx

    try:
        r = httpx.get(f"http://{host}:{port}/api/health", timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except Exception:
        return None
    return None


def _pid_alive(pid: int) -> bool:
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
            return str(pid) in out
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _python_for_daemon() -> str:
    """Prefer pythonw.exe (no console window) from the same venv on Windows."""
    exe = Path(sys.executable)
    if os.name == "nt":
        pyw = exe.with_name("pythonw.exe")
        if pyw.exists():
            return str(pyw)
    return str(exe)


@app.callback()
def _root() -> None:
    # pythonw.exe (used by the detached launcher / logon script) has no
    # console: sys.stdout/err are None and any echo would raise, leaving the
    # launcher process hanging. Route everything to the daemon log instead.
    if sys.stdout is None or sys.stderr is None:
        log_fh = open(_log_dir() / "daemon.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or log_fh
        sys.stderr = sys.stderr or log_fh


@app.command()
def version() -> None:
    typer.echo(f"omniapi {__version__}")


@app.command()
def serve(
    foreground: bool = typer.Option(False, "--foreground", "-f", help="Run in this console instead of detaching"),
    host: str = typer.Option(DEFAULT_HOST, help="Bind address (keep 127.0.0.1)"),
    port: int = typer.Option(DEFAULT_PORT, help="Port"),
    log_level: str = typer.Option("info", help="uvicorn log level"),
) -> None:
    """Start the daemon (MCP at /mcp, REST at /api, WebSocket at /ws)."""
    existing = _health(host, port)
    if existing:
        typer.echo(f"already running: pid {existing.get('pid')} v{existing.get('version')} on http://{host}:{port}")
        raise typer.Exit(0)

    if not foreground:
        log_path = _log_dir() / "daemon.log"
        cmd = [_python_for_daemon(), "-m", "omniapi_mcp.cli", "serve", "--foreground", "--host", host, "--port", str(port), "--log-level", log_level]
        creation = 0
        if os.name == "nt":
            creation = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        popen_kw: dict = dict(stdin=subprocess.DEVNULL, cwd=str(Path(__file__).resolve().parents[1]), close_fds=True)
        with open(log_path, "ab") as log:
            if os.name == "nt":
                # 呼叫端若在 Job Object 裡（Claude Code 的 shell、CI runner…），指令一結束整個 job 會被收掉，
                # daemon 跟著陪葬（2026-09-29 實證：log 無關機訊息、pid 檔沒清）。先試著脫離 job；
                # job 不允許 breakaway 時 CreateProcess 會回 access denied，退回原本的旗標。
                breakaway = 0x01000000  # CREATE_BREAKAWAY_FROM_JOB
                stamp = time.strftime("%Y-%m-%d %H:%M:%S")
                who = f"launcher pid={os.getpid()} ppid={os.getppid()} exe={Path(sys.executable).name}"
                try:
                    child = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, creationflags=creation | breakaway, **popen_kw)
                    how = "breakaway"
                except OSError as e:
                    child = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, creationflags=creation, **popen_kw)
                    how = f"no breakaway ({e.__class__.__name__}: winerror {getattr(e, 'winerror', '?')})"
                # 留一行給事後查：誰啟動的、有沒有脫離 job（daemon 無聲消失時，這是判斷是不是被 job 連坐的線索）
                log.write(f"{stamp} - omni.launcher - INFO - {who} started daemon pid={child.pid} [{how}]\n".encode("utf-8"))
                log.flush()
            else:
                subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True, **popen_kw)
        # 等它回應。全新安裝第一次啟動要 50～70 秒（第一次 import 很慢），所以用實際經過的時間算，
        # 不要用「重試次數」——Windows 連一個還沒開的埠，每次失敗就要一秒多，次數換算出來的秒數不準。
        t0 = time.monotonic()
        hinted = False
        while time.monotonic() - t0 < SERVE_WAIT_SECONDS:
            time.sleep(0.25)
            h = _health(host, port, timeout=1.0)
            if h:
                typer.echo(f"started: pid {h.get('pid')} v{h.get('version')} on http://{host}:{port}  (log: {log_path})")
                raise typer.Exit(0)
            if not hinted and time.monotonic() - t0 > 8:
                hinted = True
                typer.echo("starting… (the first start after an install can take about a minute)", err=True)
        typer.echo(
            f"daemon did not answer within {SERVE_WAIT_SECONDS}s — it may still be starting; check `omni status` and {log_path}",
            err=True,
        )
        raise typer.Exit(1)

    # foreground
    from .config.settings import Settings
    from .daemon.app import serve as _serve
    from .server import configure_logging

    env = _env_file()
    settings = Settings(_env_file=str(env)) if env else Settings()
    configure_logging(settings.server.log_level)
    _serve(settings, host=host, port=port, log_level=log_level)


@app.command()
def status(host: str = typer.Option(DEFAULT_HOST), port: int = typer.Option(DEFAULT_PORT), as_json: bool = typer.Option(False, "--json")) -> None:
    """Show whether the daemon is running and what it loaded."""
    import httpx

    h = _health(host, port)
    if not h:
        info = read_pid()
        if info and _pid_alive(int(info.get("pid", 0))):
            typer.echo(f"pid {info['pid']} is alive but http://{host}:{port} does not answer (starting?)")
        else:
            typer.echo(f"not running (http://{host}:{port})")
        raise typer.Exit(1)
    try:
        s = httpx.get(f"http://{host}:{port}/api/status", timeout=3).json()
    except Exception as e:
        typer.echo(f"running (pid {h.get('pid')}) but /api/status failed: {e}")
        raise typer.Exit(1)
    if as_json:
        typer.echo(json.dumps(s, ensure_ascii=False, indent=2))
        return
    typer.echo(f"OmniAPI v{s['version']}  pid {s['pid']}  http://{s['host']}:{s['port']}  up {s['uptime_s']:.0f}s")
    typer.echo(f"  MCP:        http://{s['host']}:{s['port']}/mcp")
    if not s["providers"]["configured"]:
        env = _env_file()
        where = str(env) if env else str(Path(__file__).resolve().parents[1] / ".env")
        typer.echo(f"  providers:  (none — no API key is configured; copy .env.example to {where}, fill in a key and set its ENABLED=true, then restart)")
    else:
        typer.echo(f"  providers:  {', '.join(s['providers']['configured'])}")
    typer.echo(f"  text:       {', '.join(s['providers']['text'])}   image: {', '.join(s['providers']['image'])}")
    disc = s.get("discovery") or {}
    typer.echo("  discovery:  " + (", ".join(f"{k}={v['models']}" for k, v in disc.items()) or "(not yet)"))
    st = s.get("store") or {}
    typer.echo(f"  store:      {st.get('calls', 0)} calls, {st.get('conversations', 0)} conversations, {st.get('runs', 0)} runs  ({st.get('path')})")
    typer.echo(f"  tiers:      {s.get('tiers')}")


def stop_target(info: Optional[dict], health: Optional[dict], port: int, pid_alive: Any = None) -> tuple[int, bool, str]:
    """Which process ``omni stop --port <port>`` should kill: ``(pid, clear_pid_file, note)``.

    The daemon answering on the requested port is the target. The pid file is
    only a fallback for a daemon that is up but not answering yet, and only
    when it was written for the same port — it lives in the data home, so with
    several daemons (a sandbox on another port with its own ``OMNIAPI_HOME``)
    the file in *this* home may describe a different daemon. Trusting it
    first meant ``omni stop --port 7799`` could kill the daemon on 7788.
    """
    alive = pid_alive or _pid_alive
    file_pid = int((info or {}).get("pid") or 0)
    file_port = int((info or {}).get("port") or 0)
    if health and health.get("pid"):
        pid = int(health["pid"])
        return pid, file_pid == pid, ""
    if not file_pid:
        return 0, False, "not running"
    if file_port != port:
        return 0, False, (
            f"nothing answers on port {port}; the pid file in this data home belongs to a daemon on port {file_port} "
            f"(pid {file_pid}) and was left alone. Use --port {file_port}, or set OMNIAPI_HOME to the other daemon's data home."
        )
    if not alive(file_pid):
        return 0, True, f"not running (removed a stale pid file for pid {file_pid})"
    return file_pid, True, ""


@app.command()
def stop(host: str = typer.Option(DEFAULT_HOST), port: int = typer.Option(DEFAULT_PORT)) -> None:
    """Stop the daemon listening on --port."""
    pid, clear, note = stop_target(read_pid(), _health(host, port), port)
    if not pid:
        if clear:
            clear_pid()
        typer.echo(note or "not running")
        raise typer.Exit(0)
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        os.kill(pid, 15)
    for _ in range(20):
        time.sleep(0.2)
        if not _pid_alive(pid):
            break
    if clear:
        clear_pid()
    typer.echo(f"stopped pid {pid}")


@app.command("mcp-config")
def mcp_config(apply: bool = typer.Option(False, "--apply", help="Write the entry into ~/.claude.json"), host: str = typer.Option(DEFAULT_HOST), port: int = typer.Option(DEFAULT_PORT)) -> None:
    """Show (or apply) the Claude Code MCP entry that points at the daemon."""
    entry = {"type": "http", "url": f"http://{host}:{port}/mcp"}
    typer.echo(json.dumps({"omniapi-mcp": entry}, indent=2))
    if not apply:
        return
    cfg_path = Path.home() / ".claude.json"
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    servers = data.setdefault("mcpServers", {})
    old = servers.get("omniapi-mcp")
    if old and old != entry:
        (data_home() / "claude-mcp-entry.backup.json").write_text(json.dumps(old, indent=2), encoding="utf-8")
    servers["omniapi-mcp"] = entry
    cfg_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    typer.echo(f"applied to {cfg_path} (previous entry backed up under {data_home()})")


# ---------------------------------------------------------------- agent runs
def _api(host: str, port: int) -> str:
    return f"http://{host}:{port}"


def _require_daemon(host: str, port: int) -> None:
    if not _health(host, port):
        typer.echo(f"daemon is not running on http://{host}:{port} — start it with `omni serve`", err=True)
        raise typer.Exit(1)


def _fmt_event(e: dict) -> Optional[str]:
    t, p = e.get("type"), e.get("payload") or {}
    if t == "text":
        return "💬 " + " ".join(str(p.get("text", "")).split())[:160]
    if t == "tool_call":
        from .harness.events import short_tool_summary

        return "🔧 " + short_tool_summary(p.get("name", "tool"), p.get("input"))
    if t == "tool_result":
        out = " ".join(str(p.get("output", "")).split())[:100]
        return ("❌ " if p.get("is_error") else "↩  ") + out
    if t == "session_start":
        return f"· session {str(p.get('session_id'))[:8]} ({p.get('model')})"
    if t == "error":
        return "‼ " + str(p.get("message"))[:200]
    if t == "result":
        return f"✅ done · turns={p.get('num_turns')} cost={p.get('cost_usd')}"
    return None


def _print_run_header(r: dict) -> None:
    typer.echo(f"{r['id']}  [{r['state']}]  {r.get('harness')}/{r.get('model')}  cwd={r.get('cwd')}  title={r.get('title')}")


@app.command()
def run(
    task: str = typer.Argument(..., help="The task brief (quote it)"),
    model: str = typer.Option("cheap", "--model", "-m", help="Tier (cheap/standard/strong) or model id"),
    cwd: Optional[str] = typer.Option(None, "--cwd", help="Working directory (default: current)"),
    title: Optional[str] = typer.Option(None, "--title", "-t"),
    harness: Optional[str] = typer.Option(None, help="claude | codex | gemini (default: by model)"),
    yolo: bool = typer.Option(False, "--yolo", help="Skip permissions / sandbox"),
    no_search: bool = typer.Option(False, "--no-search", help="Do not attach search MCP servers"),
    max_turns: Optional[int] = typer.Option(None, "--max-turns"),
    resume: Optional[str] = typer.Option(None, "--resume", help="Continue this run id with the task as follow-up"),
    api_billing: bool = typer.Option(False, "--api", help="Claude models: bill the Anthropic API key instead of the subscription login"),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Stream events until the run finishes"),
    host: str = typer.Option(DEFAULT_HOST),
    port: int = typer.Option(DEFAULT_PORT),
) -> None:
    """Dispatch a task to a headless agent (replaces `node dsk.js`)."""
    import httpx

    _require_daemon(host, port)
    body = {
        "prompt": task,
        "model": model,
        "cwd": os.path.abspath(cwd or os.getcwd()),
        "title": title,
        "harness": harness,
        "yolo": yolo,
        "search": not no_search,
        "max_turns": max_turns,
        "resume_run_id": resume,
        "auth": "api" if api_billing else None,
        "dispatcher": os.environ.get("OMNI_DISPATCHER") or ("cli" if not os.environ.get("CLAUDE_CODE_SESSION_ID") else f"claude-code:{os.environ['CLAUDE_CODE_SESSION_ID'][:8]}"),
    }
    r = httpx.post(_api(host, port) + "/api/runs", json={k: v for k, v in body.items() if v is not None}, timeout=30)
    if r.status_code != 200:
        typer.echo(f"error: {r.text}", err=True)
        raise typer.Exit(1)
    t = r.json()
    typer.echo(f"run {t['run_id']} started on {t['harness']} with {t['model']} (cwd {t.get('cwd')})")
    if not wait:
        typer.echo(f"follow with: omni run-log {t['run_id']} --follow")
        return
    _follow(host, port, t["run_id"])


def _follow(host: str, port: int, run_id: str, *, poll: float = 2.0) -> dict:
    import httpx

    after = 0
    while True:
        r = httpx.get(_api(host, port) + f"/api/runs/{run_id}", params={"after": after}, timeout=30).json()
        for e in r.get("events", []):
            after = max(after, e["id"])
            line = _fmt_event(e)
            if line:
                typer.echo("  " + line)
        if r["state"] in ("done", "error", "cancelled", "dead"):
            typer.echo("")
            typer.echo(f"== {r['state']}  turns={r.get('turns')}  cost={r.get('cost_usd')}  session={str(r.get('session_id'))[:8]}")
            if r.get("result"):
                typer.echo(r["result"])
            if r.get("error"):
                typer.echo(f"error: {r['error']}", err=True)
            return r
        time.sleep(poll)


@app.command("runs")
def runs_cmd(limit: int = typer.Option(15), state: Optional[str] = typer.Option(None), host: str = typer.Option(DEFAULT_HOST), port: int = typer.Option(DEFAULT_PORT)) -> None:
    """List recent agent runs (the board, in text)."""
    import httpx
    from datetime import datetime

    _require_daemon(host, port)
    rows = httpx.get(_api(host, port) + "/api/runs", params={"limit": limit, **({"state": state} if state else {})}, timeout=10).json()
    icon = {"running": "🔄", "starting": "🔄", "done": "✅", "error": "❌", "cancelled": "⏹", "dead": "💀"}
    for r in rows:
        started = datetime.fromtimestamp(r["started_at"]).strftime("%m-%d %H:%M")
        cost = f"${r['cost_usd']:.4f}" if r.get("cost_usd") is not None else "—"
        typer.echo(f"{icon.get(r['state'], '·')} {r['id']}  {started}  {r.get('harness'):6} {str(r.get('model'))[:22]:22} {cost:>9}  {r.get('turns') or 0:>3}t  {str(r.get('dispatcher') or '')[:18]:18} {r.get('title') or ''}")


@app.command("works")
def works_cmd(
    limit: int = typer.Option(20),
    kind: Optional[str] = typer.Option(None, help="image | speech | music | transcript | lyrics"),
    query: Optional[str] = typer.Option(None, "--query", "-q", help="Search prompts, titles and transcripts"),
    backfill: bool = typer.Option(False, "--backfill", help="Index files in the storage folder that are not in the works library yet"),
    dry_run: bool = typer.Option(False, "--dry-run", help="With --backfill: only count"),
    host: str = typer.Option(DEFAULT_HOST),
    port: int = typer.Option(DEFAULT_PORT),
) -> None:
    """List generated works (images, speech, music, transcripts)."""
    import httpx
    from datetime import datetime

    _require_daemon(host, port)
    _safe_console()
    if backfill:
        res = httpx.post(_api(host, port) + "/api/artifacts/backfill", params={"dry_run": dry_run}, timeout=300).json()
        verb = "would add" if res["dry_run"] else "added"
        typer.echo(f"scanned {res['scanned']} files in {res['base']}: {verb} {res['added_total']} {res['added'] or ''}, {res['already_indexed']} already indexed ({res['seconds']}s)")
        return
    params = {"limit": limit, **({"kind": kind} if kind else {}), **({"q": query} if query else {})}
    data = httpx.get(_api(host, port) + "/api/artifacts", params=params, timeout=10).json()
    icon = {"image": "🖼", "speech": "🗣", "music": "🎵", "transcript": "📝", "lyrics": "🎤"}
    for a in data["items"]:
        when = datetime.fromtimestamp(a["created_at"]).strftime("%m-%d %H:%M")
        cost = f"${a['cost_usd']:.4f}" if a.get("cost_usd") is not None else "—"
        label = " ".join(str(a.get("title") or a.get("prompt") or Path(a["file_path"]).name).split())[:60]
        typer.echo(f"{icon.get(a['kind'], '·')} {a['id']}  {when}  {str(a.get('model') or '?')[:22]:22} {cost:>9}  {str(a.get('source') or ''):8} {label}")
    typer.echo("  ".join(f"{k} {n}" for k, n in sorted(data["counts"].items())) or "no works yet")


@app.command("run-log")
def run_log(run_id: str, follow: bool = typer.Option(False, "--follow", "-f"), host: str = typer.Option(DEFAULT_HOST), port: int = typer.Option(DEFAULT_PORT)) -> None:
    """Show a run's events (and result); --follow tails a live run."""
    import httpx

    _require_daemon(host, port)
    if follow:
        _follow(host, port, run_id)
        return
    r = httpx.get(_api(host, port) + f"/api/runs/{run_id}", timeout=30)
    if r.status_code != 200:
        typer.echo(r.text, err=True)
        raise typer.Exit(1)
    row = r.json()
    _print_run_header(row)
    for e in row.get("events", []):
        line = _fmt_event(e)
        if line:
            typer.echo("  " + line)
    if row.get("result"):
        typer.echo("")
        typer.echo(row["result"])


# ---------------------------------------------------------------- chat (M6)
def _safe_console() -> None:
    """Piped / redirected output on Windows uses the ANSI code page (cp950…),
    which cannot encode every character a model writes; replace instead of
    crashing mid-reply. A real console is already UTF-8 (PEP 528)."""
    for stream in (sys.stdout, sys.stderr):
        enc = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
        if enc != "utf8" and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass


@app.command()
def chat(
    message: Optional[str] = typer.Argument(None, help="One message (quote it). Leave out for an interactive session."),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Tier (cheap/standard/strong) or model id. New chats default to cheap; --resume keeps the conversation's."),
    system: Optional[str] = typer.Option(None, "--system", "-s", help="System prompt"),
    resume: Optional[str] = typer.Option(None, "--resume", "-r", help="Continue this conversation id"),
    no_stream: bool = typer.Option(False, "--no-stream", help="Wait for the whole reply instead of printing it as it comes"),
    as_json: bool = typer.Option(False, "--json", help="Print only the final JSON (implies --no-stream)"),
    show_thinking: bool = typer.Option(False, "--show-thinking", help="Also print the model's reasoning (dimmed, to stderr)"),
    host: str = typer.Option(DEFAULT_HOST),
    port: int = typer.Option(DEFAULT_PORT),
) -> None:
    """Chat with any text model; the conversation also shows up in the GUI (/chat).

    The reply streams to stdout; the summary line (model, tokens, cost,
    conversation id, GUI link) goes to stderr. Interactive commands: /model,
    /system, /export [path], /id, /exit — Ctrl+C during a reply cancels it.
    """
    import httpx

    from .chat.cli_support import ApiError, ChatApi, ChatSession

    _safe_console()
    if resume is not None and not resume.strip():
        typer.echo("--resume needs a conversation id (it was empty)", err=True)
        raise typer.Exit(2)
    _require_daemon(host, port)
    if as_json:
        no_stream = True
    if as_json and not message:
        typer.echo("--json needs a message", err=True)
        raise typer.Exit(2)

    def out(s: str) -> None:
        typer.echo(s, nl=False)

    def err(s: str) -> None:
        typer.echo(s, nl=False, err=True)

    with httpx.Client(base_url=_api(host, port), timeout=30) as client:
        api = ChatApi(client)
        session = ChatSession(api, host=host, port=port, model=model, system=system, cid=resume, stream=not no_stream,
                              show_thinking=show_thinking, out=out, err=err, dim=lambda s: typer.style(s, dim=True), quiet=as_json)
        try:
            if resume:
                conv = api.get(resume)
                if system is not None:
                    api.update(resume, system_prompt=system)
                if model:
                    api.update(resume, model=model)
                if not message:
                    err(f"接續「{conv.get('title') or '未命名對話'}」：{conv.get('n_messages', 0)} 則訊息，模型 {model or conv.get('model')}\n")
            if message:
                summary = session.ask(message)
                if as_json:
                    enc = (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "")
                    typer.echo(json.dumps(summary, ensure_ascii=enc != "utf8", indent=2))
                if summary.get("state") == "error":
                    raise typer.Exit(1)
                if summary.get("state") == "cancelled":
                    raise typer.Exit(130)
                return
            err(f"OmniAPI 聊天 · 模型 {model or ('（沿用）' if resume else 'cheap')} · /help 看指令，/exit 離開\n")
            def read_line(prompt: str) -> str:
                if sys.stdout.isatty():
                    return input(prompt)  # the console's own line editing
                err(prompt)  # keep a redirected stdout to the replies only
                return input()

            session.repl(read_line, encoding=getattr(sys.stdin, "encoding", None))
            if session.cid:
                err(f"對話 {session.cid}  {session.url}\n")
        except ApiError as e:
            if e.status in (404, 405) and not session.cid and not resume:
                err(f"這個 daemon 還沒有聊天功能（/api/chat 回 {e.status}）——請重啟 daemon 換新版：omni stop，再 omni serve\n")
            else:
                err(f"錯誤（HTTP {e.status}）：{e.detail}\n")
            raise typer.Exit(1)
        except httpx.HTTPError as e:
            err(f"連不上 daemon：{e}\n")
            raise typer.Exit(1)


@app.command("import-dsk")
def import_dsk_cmd(runs_dir: Optional[str] = typer.Option(None, help="Defaults to ~/.dsk/runs"), force: bool = typer.Option(False, "--force", help="Re-import runs that already exist")) -> None:
    """Import legacy dsk runs (~/.dsk/runs) into the OmniAPI store as history."""
    import asyncio

    from .runs.import_dsk import import_dsk
    from .store.db import Store

    async def go():
        store = Store()
        await store.open()
        try:
            return await import_dsk(store, Path(runs_dir) if runs_dir else None, force=force)
        finally:
            await store.close()

    res = asyncio.run(go())
    typer.echo(f"imported {res['imported']}, skipped {res['skipped']} (already present) from {res['runs_dir']}")
    for e in res["errors"]:
        typer.echo("  ! " + e, err=True)


def _startup_dir() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _startup_script() -> Path:
    return _startup_dir() / "OmniAPI Daemon.vbs"


@autostart_app.command("install")
def autostart_install(
    port: int = typer.Option(DEFAULT_PORT),
    task: bool = typer.Option(False, "--task", help="Use a scheduled task (needs an elevated shell) instead of the Startup folder"),
) -> None:
    """Start the daemon at Windows logon.

    Default: a hidden launcher (.vbs) in the user's Startup folder — no admin
    rights needed. ``--task`` registers a schtasks ONLOGON task instead
    (Windows refuses that from a non-elevated shell: "access denied").
    """
    if os.name != "nt":
        typer.echo("autostart is Windows-only for now")
        raise typer.Exit(1)
    py = _python_for_daemon()
    if task:
        tr = f'"{py}" -m omniapi_mcp.cli serve --foreground --port {port}'
        r = subprocess.run(["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/TN", TASK_NAME, "/TR", tr, "/RL", "LIMITED"], capture_output=True, text=True)
        typer.echo((r.stdout or r.stderr).strip())
        raise typer.Exit(r.returncode)
    workdir = Path(__file__).resolve().parents[1]
    script = _startup_script()
    script.parent.mkdir(parents=True, exist_ok=True)
    # WScript.Shell.Run with window style 0 = hidden; False = do not wait.
    vbs = (
        'Set sh = CreateObject("WScript.Shell")\r\n'
        f'sh.CurrentDirectory = "{workdir}"\r\n'
        f'sh.Run """{py}"" -m omniapi_mcp.cli serve --port {port}", 0, False\r\n'
    )
    script.write_text(vbs, encoding="utf-8")
    typer.echo(f"installed {script}")
    typer.echo(f"  → {py} -m omniapi_mcp.cli serve --port {port}  (hidden, at logon)")


@autostart_app.command("remove")
def autostart_remove() -> None:
    removed = False
    script = _startup_script()
    if script.exists():
        script.unlink()
        typer.echo(f"removed {script}")
        removed = True
    r = subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME], capture_output=True, text=True)
    if r.returncode == 0:
        typer.echo(f"removed scheduled task '{TASK_NAME}'")
        removed = True
    if not removed:
        typer.echo("nothing installed")


@autostart_app.command("status")
def autostart_status() -> None:
    script = _startup_script()
    typer.echo(f"startup folder: {'installed' if script.exists() else 'not installed'}  ({script})")
    r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST"], capture_output=True, text=True)
    typer.echo(f"scheduled task: {'installed' if r.returncode == 0 else 'not installed'}")


if __name__ == "__main__":
    app()
