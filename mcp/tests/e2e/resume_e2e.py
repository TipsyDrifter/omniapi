"""Codex resume + Gemini via the daemon MCP door."""

import asyncio, json, os, time
from pathlib import Path
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

BASE = "http://127.0.0.1:7788"
SCRATCH = Path(os.environ["SCRATCH"])


def j(r):
    return json.loads(r.content[0].text)


async def wait_done(session, run_id, label):
    t0 = time.time()
    while time.time() - t0 < 300:
        r = j(await session.call_tool("get_run", {"run_id": run_id, "include_events": False}))
        if r["state"] in ("done", "error", "cancelled", "dead"):
            return r
        await asyncio.sleep(2)
    return r


async def main():
    async with streamablehttp_client(BASE + "/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            # Codex resume: continue the earlier codex run (notes-codex.md)
            runs = j(await session.call_tool("list_runs", {"limit": 30}))["runs"]
            codex_parent = next((r for r in runs if r["harness"] == "codex" and r["state"] == "done"), None)
            print("codex parent:", codex_parent and codex_parent["id"])
            if codex_parent:
                t = j(await session.call_tool("run_agent", {"task": "把目前目錄的 notes-codex.md 再加第四行：『Codex 續接測試成功』，然後一句話回報。", "resume_run_id": codex_parent["id"], "max_turns": 6}))
                print("codex resume ticket:", {k: t.get(k) for k in ("run_id", "harness", "model", "error")})
                if "run_id" in t:
                    r = await wait_done(session, t["run_id"], "codex-resume")
                    content = (SCRATCH / "mcp-codex" / "notes-codex.md").read_text(encoding="utf-8", errors="replace")
                    print(f"codex resume: state={r['state']} turns={r.get('turns')} session={str(r.get('session_id'))[:8]} 4th_line={'Codex 續接測試成功' in content} err={str(r.get('error'))[:150]}")
            # Gemini via MCP
            cwd = SCRATCH / "mcp-gemini"; cwd.mkdir(exist_ok=True)
            t = j(await session.call_tool("run_agent", {"task": "在目前目錄建立 gemini-note.txt，內容一行 'gemini via mcp'，然後一句話回報。", "model": "gemini-3.8-flash", "cwd": str(cwd), "search": False, "max_turns": 6, "title": "gemini via mcp"}))
            print("gemini ticket:", {k: t.get(k) for k in ("run_id", "harness", "model", "error")})
            if "run_id" in t:
                r = await wait_done(session, t["run_id"], "gemini")
                print(f"gemini: state={r['state']} turns={r.get('turns')} cost={r.get('cost_usd')} file={(cwd / 'gemini-note.txt').exists()} err={str(r.get('error'))[:150]}")
                # gemini resume attempt (UUID) — verifies D20 open item
                t2 = j(await session.call_tool("run_agent", {"task": "把 gemini-note.txt 再加一行 'resumed'，一句話回報。", "resume_run_id": t["run_id"], "max_turns": 6}))
                print("gemini resume ticket:", {k: t2.get(k) for k in ("run_id", "error")})
                if "run_id" in t2:
                    r2 = await wait_done(session, t2["run_id"], "gemini-resume")
                    content = (cwd / "gemini-note.txt").read_text(encoding="utf-8", errors="replace")
                    print(f"gemini resume: state={r2['state']} resumed_line={'resumed' in content} err={str(r2.get('error'))[:200]}")


asyncio.run(main())
