"""Import legacy dsk (DeepSeek worker) runs into the OmniAPI store as
read-only history (decision D11).

Source layout: ``~/.dsk/runs/<runId>/`` with ``status.json`` (state, model,
cwd, usage, activityLog, dispatcher…), ``events.jsonl`` (Claude Code
stream-json) and ``result.md`` (final report). The events are translated with
the same mapping the Claude harness uses, so the board renders old and new
runs identically.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from ..store.db import Store

logger = logging.getLogger(__name__)

_MODEL_ALIAS = {"deepseek-v4-flash": "deepseek-flash", "deepseek-v4-pro": "deepseek-v4-pro"}
# dsk-table.js estCost: off-peak per-1M prices, peak (Taipei weekday 09-12, 14-18) x2
_FLASH_PRICE = {"inMiss": 0.22, "inHit": 0.007, "out": 0.66}


def _epoch(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _is_peak(ts: Optional[float]) -> bool:
    if ts is None:
        return False
    # UTC weekday 01-04 / 06-10 == Taipei 09-12 / 14-18
    t = time.gmtime(ts)
    if t.tm_wday >= 5:
        return False
    return 1 <= t.tm_hour < 4 or 6 <= t.tm_hour < 10


def estimate_cost(model: str, usage: dict[str, Any], started_at: Optional[float]) -> Optional[float]:
    if "flash" not in (model or ""):
        return None
    mult = 2 if _is_peak(started_at) else 1
    usd = (
        (usage.get("inMiss") or 0) * _FLASH_PRICE["inMiss"]
        + (usage.get("inHit") or 0) * _FLASH_PRICE["inHit"]
        + (usage.get("out") or 0) * _FLASH_PRICE["out"]
    ) * mult / 1e6
    return round(usd, 6)


def translate_stream_event(ev: dict[str, Any], tool_names: dict[str, str]) -> list[tuple[str, dict[str, Any]]]:
    """Claude Code stream-json line → list of (type, payload) unified events."""
    t = ev.get("type")
    out: list[tuple[str, dict[str, Any]]] = []
    if t == "system" and ev.get("subtype") == "init":
        out.append(("session_start", {"session_id": ev.get("session_id"), "model": ev.get("model"), "tools": ev.get("tools", []), "mcp_servers": [s.get("name") for s in ev.get("mcp_servers", []) if isinstance(s, dict)]}))
    elif t == "system":
        # thinking_tokens / task_progress / … : thousands per run, pure noise
        pass
    elif t == "assistant":
        for block in (ev.get("message") or {}).get("content") or []:
            bt = block.get("type")
            if bt == "text" and block.get("text"):
                out.append(("text", {"text": block["text"]}))
            elif bt == "thinking" and block.get("thinking"):
                out.append(("thinking", {"text": block["thinking"]}))
            elif bt == "tool_use":
                tool_names[block.get("id", "")] = block.get("name", "")
                out.append(("tool_call", {"id": block.get("id"), "name": block.get("name"), "input": block.get("input")}))
    elif t == "user":
        content = (ev.get("message") or {}).get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    c = block.get("content")
                    if isinstance(c, list):
                        c = "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in c)
                    text = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
                    if len(text) > 4000:
                        text = text[:4000] + f"…[+{len(text) - 4000} chars]"
                    out.append(("tool_result", {"id": block.get("tool_use_id"), "name": tool_names.get(block.get("tool_use_id", "")), "output": text, "is_error": bool(block.get("is_error"))}))
    elif t == "result":
        out.append(("result", {"text": ev.get("result", ""), "usage": ev.get("usage"), "cost_usd": None, "num_turns": ev.get("num_turns"), "duration_ms": ev.get("duration_ms"), "session_id": ev.get("session_id"), "is_error": bool(ev.get("is_error")), "subtype": ev.get("subtype")}))
    return out


async def import_dsk(store: Store, runs_dir: Path | None = None, *, force: bool = False) -> dict[str, Any]:
    runs_dir = runs_dir or (Path.home() / ".dsk" / "runs")
    if not runs_dir.exists():
        return {"imported": 0, "skipped": 0, "errors": [], "runs_dir": str(runs_dir), "note": "runs dir not found"}
    imported, skipped, errors = 0, 0, []
    for d in sorted(p for p in runs_dir.iterdir() if p.is_dir()):
        status_file = d / "status.json"
        if not status_file.exists():
            continue
        try:
            st = json.loads(status_file.read_text(encoding="utf-8"))
        except Exception as e:
            errors.append(f"{d.name}: bad status.json ({e})")
            continue
        run_id = st.get("runId") or d.name
        if await store.run(run_id) and not force:
            skipped += 1
            continue
        model = _MODEL_ALIAS.get(st.get("model") or "", st.get("model") or "deepseek-flash")
        started = _epoch(st.get("startedAt")) or d.stat().st_mtime
        ended = _epoch(st.get("endedAt"))
        state = {"done": "done", "error": "error", "running": "dead", "starting": "dead"}.get(st.get("state"), "dead")
        usage = st.get("usage") or {}
        result_md = (d / "result.md").read_text(encoding="utf-8", errors="replace") if (d / "result.md").exists() else None
        dispatcher = st.get("dispatcherTitle") or st.get("dispatcher")
        if st.get("dispatcherTitle") and st.get("dispatcher"):
            dispatcher = f"{st['dispatcherTitle']}（{st['dispatcher']}）"
        try:
            if force and await store.run(run_id):
                await store.db.execute("DELETE FROM events WHERE run_id=?", (run_id,))
                await store.db.execute("DELETE FROM runs WHERE id=?", (run_id,))
                await store.db.commit()
            await store.create_run(
                run_id,
                title=st.get("title") or (st.get("prompt") or "")[:40],
                prompt=st.get("prompt"),
                harness="claude",
                model=model,
                cwd=st.get("cwd"),
                state=state,
                started_at=started,
                ended_at=ended,
                turns=st.get("turns") or 0,
                context_tokens=st.get("context"),
                cost_usd=estimate_cost(model, usage, started),
                pid=st.get("pid"),
                session_id=st.get("sessionId"),
                dispatcher=dispatcher,
                result=result_md,
                error=st.get("error"),
                meta={"source": "dsk", "endpoint": "deepseek", "usage": usage, "activityLog": st.get("activityLog"), "lastActivity": st.get("lastActivity"), "durationMs": st.get("durationMs")},
            )
            events_file = d / "events.jsonl"
            n_events = 0
            if events_file.exists():
                tool_names: dict[str, str] = {}
                ts = started
                with events_file.open(encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            ev = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        for etype, payload in translate_stream_event(ev, tool_names):
                            ts += 0.001  # keep ordering monotonic
                            await store.add_event(run_id, etype, payload, ts=ts)
                            n_events += 1
            imported += 1
            logger.info("imported dsk run %s (%d events)", run_id, n_events)
        except Exception as e:
            errors.append(f"{run_id}: {type(e).__name__}: {e}")
    return {"imported": imported, "skipped": skipped, "errors": errors, "runs_dir": str(runs_dir)}
