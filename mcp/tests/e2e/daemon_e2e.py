"""Daemon end-to-end: REST health/status, MCP over streamable-http (two
sessions sharing one runtime), WebSocket events, and the calls ledger."""

import asyncio
import json
import time

import httpx
import websockets
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

BASE = "http://127.0.0.1:7788"


async def wait_health():
    for _ in range(60):
        try:
            r = httpx.get(BASE + "/api/health", timeout=1)
            if r.status_code == 200 and r.json().get("status") == "ok":
                return r.json()
        except Exception:
            pass
        await asyncio.sleep(0.5)
    raise SystemExit("daemon never became healthy")


async def ws_collect(stop: asyncio.Event, out: list):
    async with websockets.connect(BASE.replace("http", "ws") + "/ws") as ws:
        while not stop.is_set():
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=1)
                out.append(json.loads(msg))
            except asyncio.TimeoutError:
                continue


async def mcp_session(label: str, model: str):
    async with streamablehttp_client(BASE + "/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            r = await session.call_tool("complete_text", {"prompt": "Reply with exactly: PONG", "model": model, "max_completion_tokens": 32})
            data = json.loads(r.content[0].text)
            print(f"[{label}] tools={len(tools.tools)} -> {data.get('provider')}/{data.get('model')}: {data.get('text','').strip()!r} cost={data.get('cost_usd')}")
            return data


async def main():
    h = await wait_health()
    print("HEALTH", h)
    st = httpx.get(BASE + "/api/status", timeout=5).json()
    print("STATUS pid", st["pid"], "mode", st["mode"], "providers", st["providers"]["configured"], "store", st["store"])

    stop = asyncio.Event()
    events: list = []
    ws_task = asyncio.create_task(ws_collect(stop, events))
    await asyncio.sleep(0.5)

    # two concurrent MCP sessions — must share the same runtime/pid
    await asyncio.gather(mcp_session("session-A", "cheap"), mcp_session("session-B", "gpt-5.4-mini"))

    await asyncio.sleep(0.5)
    stop.set()
    await ws_task
    kinds = [e["type"] for e in events]
    print("WS events:", kinds)

    calls = httpx.get(BASE + "/api/calls?limit=5", timeout=5).json()
    print("CALLS", [(c["tool"], c["status"], c["model"], c["duration_ms"], c["cost_usd"]) for c in calls])
    st2 = httpx.get(BASE + "/api/status", timeout=5).json()
    print("STATUS after: pid", st2["pid"], "calls in store", st2["store"]["calls"], "discovery", {k: v["models"] for k, v in st2["discovery"].items()})
    models = httpx.get(BASE + "/api/models?modality=text", timeout=5).json()
    print("MODELS text count", models["counts"])
    costs = httpx.get(BASE + "/api/costs?days=1", timeout=5).json()
    print("COSTS", costs["total"])


asyncio.run(main())
