"""Through the daemon's MCP door: run_agent (claude/deepseek + codex), poll
get_run, then a follow-up via resume_run_id. Also exercises REST /api/runs."""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

BASE = "http://127.0.0.1:7788"
SCRATCH = Path(os.environ["SCRATCH"])
WHICH = sys.argv[1:] or ["claude", "codex"]
MODELS = {"claude": "cheap", "codex": "gpt-5.4-mini", "gemini": "gemini-3.8-flash"}


def j(r):
    return json.loads(r.content[0].text)


async def poll(session, run_id, label, timeout=300):
    after = 0
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = j(await session.call_tool("get_run", {"run_id": run_id, "after_event": after}))
        for e in r.get("events", []):
            after = max(after, e["id"])
            p = e["payload"]
            if e["type"] == "tool_call":
                print(f"   [{label}] tool {p.get('name')} {str(p.get('input'))[:60]}")
            elif e["type"] == "text":
                print(f"   [{label}] text {' '.join(p.get('text','').split())[:90]}")
            elif e["type"] in ("error",):
                print(f"   [{label}] {e['type']}: {str(p)[:120]}")
        if r["state"] in ("done", "error", "cancelled", "dead"):
            return r
        await asyncio.sleep(2)
    return r


async def main():
    async with streamablehttp_client(BASE + "/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = [t.name for t in (await session.list_tools()).tools]
            print("TOOLS", len(names), [n for n in names if "run" in n])
            tickets = {}
            for h in WHICH:
                cwd = SCRATCH / f"mcp-{h}"
                cwd.mkdir(exist_ok=True)
                t = j(await session.call_tool("run_agent", {
                    "task": f"在目前工作目錄建立 notes-{h}.md，內容是三行關於 {h} harness 的中文筆記，然後一句話回報。",
                    "model": MODELS[h], "cwd": str(cwd), "title": f"mcp e2e {h}", "search": False, "max_turns": 8,
                    "dispatcher": "e2e_runs.py",
                }))
                print("TICKET", h, {k: t.get(k) for k in ("run_id", "harness", "model", "error")})
                if "run_id" in t:
                    tickets[h] = t["run_id"]
            results = await asyncio.gather(*(poll(session, rid, h) for h, rid in tickets.items()))
            for h, r in zip(tickets, results):
                print(f"RESULT {h}: state={r['state']} turns={r.get('turns')} cost={r.get('cost_usd')} session={str(r.get('session_id'))[:8]} file={ (SCRATCH / f'mcp-{h}' / f'notes-{h}.md').exists() }")
                print("   ", " ".join((r.get("result") or r.get("error") or "").split())[:160])

            # follow-up on the claude run
            if "claude" in tickets:
                t2 = j(await session.call_tool("run_agent", {"task": "把剛才那個 notes 檔再加第四行：『續接測試成功』，然後回報。", "resume_run_id": tickets["claude"], "search": False, "max_turns": 6}))
                print("RESUME TICKET", {k: t2.get(k) for k in ("run_id", "harness", "model", "error")})
                if "run_id" in t2:
                    r2 = await poll(session, t2["run_id"], "resume")
                    content = (SCRATCH / "mcp-claude" / "notes-claude.md").read_text(encoding="utf-8", errors="replace")
                    print(f"RESUME RESULT: state={r2['state']} turns={r2.get('turns')} cost={r2.get('cost_usd')} file_has_4th_line={'續接測試成功' in content}")

            lr = j(await session.call_tool("list_runs", {"limit": 5}))
            print("LIST_RUNS", [(x["id"][-3:], x["state"], x["harness"], x["cost_usd"]) for x in lr["runs"]])
    rest = httpx.get(BASE + "/api/runs?limit=3", timeout=5).json()
    print("REST /api/runs", [(x["id"][-3:], x["state"]) for x in rest])


asyncio.run(main())
