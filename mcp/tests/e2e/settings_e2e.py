"""Settings without a restart, the request guard and a clean shutdown (1.3-M2),
against an OFFLINE sandbox daemon (no vendor call):

    OMNIAPI_HOME=<empty dir> OMNIAPI_DEV=1 OMNIAPI_OFFLINE=1 omni serve --port 7824
    python -m tests.e2e.settings_e2e [port]          (from mcp/; ends by shutting the sandbox down)

Walks the milestone's acceptance: a key set over the API makes that provider
usable at once (same pid, no restart); a tier remapped takes effect at once;
the defaults steer new chats; keys never come back in full; pages from other
sites are refused; ``POST /api/shutdown`` ends the process cleanly.
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import websockets

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 7824
BASE = f"http://127.0.0.1:{PORT}"
KEY = "sk-e2e-fake-deepseek-key-0000-WXYZ"  # never valid anywhere; the sandbox sends nothing


def check(label: str, ok: bool, detail: object = "") -> None:
    if not ok:
        print("FAIL " + label, detail)
        raise SystemExit(1)
    print("PASS " + label)


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


async def ws_collect(stop: asyncio.Event, out: list) -> None:
    async with websockets.connect(BASE.replace("http", "ws") + "/ws", additional_headers={"Origin": "http://localhost:5178"}) as ws:
        while not stop.is_set():
            try:
                out.append(json.loads(await asyncio.wait_for(ws.recv(), timeout=0.5)))
            except asyncio.TimeoutError:
                continue


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE, timeout=60) as http:
        st = (await http.get("/api/status")).json()
        check("the daemon is an offline dev sandbox", st["offline"] and st["dev"], st)
        pid = st["pid"]
        data_home = Path(st["data_home"])
        check("a fresh data home has no settings.json", not (data_home / "settings.json").exists())

        # ---------------------------------------------------------- before
        s0 = (await http.get("/api/settings")).json()
        check("deepseek starts without a key", s0["providers"]["deepseek"]["key"]["set"] is False and not s0["providers"]["deepseek"]["configured"], s0["providers"]["deepseek"])
        m0 = (await http.get("/api/models")).json()
        check("the model list says deepseek is not usable", m0["providers"]["deepseek"]["configured"] is False)
        check("status does not list deepseek", "deepseek" not in st["providers"]["configured"] and "deepseek" not in st["providers"]["text"])

        events: list = []
        stop = asyncio.Event()
        collector = asyncio.create_task(ws_collect(stop, events))
        await asyncio.sleep(0.5)

        # ---------------------------------------------------------- a key, no restart
        r = await http.patch("/api/settings", json={"providers": {"deepseek": {"api_key": KEY}}})
        check("PATCH a key answers 200", r.status_code == 200, r.text)
        check("the answer never carries the key", KEY not in r.text and KEY[:-4] not in r.text)
        body = r.json()
        check("the answer shows the last four and the source", body["providers"]["deepseek"]["key"] == {"set": True, "last4": "WXYZ", "source": "settings", "shadowed": None}, body["providers"]["deepseek"])
        st1 = (await http.get("/api/status")).json()
        check("same process (no restart)", st1["pid"] == pid and st1["uptime_s"] >= st["uptime_s"])
        check("status now lists deepseek as configured and as a text provider", "deepseek" in st1["providers"]["configured"] and "deepseek" in st1["providers"]["text"], st1["providers"])
        m1 = (await http.get("/api/models")).json()
        check("the model list: deepseek went from unusable to usable", m1["providers"]["deepseek"]["configured"] is True)
        g = await http.get("/api/settings")
        check("GET /api/settings never carries the key", KEY not in g.text)
        saved = json.loads((data_home / "settings.json").read_text(encoding="utf-8"))
        check("the key is kept in <data home>/settings.json", saved["providers"]["deepseek"]["api_key"] == KEY)

        # ---------------------------------------------------------- tiers and defaults
        r = await http.patch("/api/settings", json={"tiers": {"cheap": "gpt-5.4-mini"}, "defaults": {"chat": "echo"}})
        check("PATCH a tier and a default", r.status_code == 200 and sorted(r.json()["changed"]) == ["defaults.chat", "tiers.cheap"], r.text)
        m2 = (await http.get("/api/models")).json()
        check("cheap now resolves to the new model (model list)", m2["tiers"]["cheap"] == "gpt-5.4-mini", m2["tiers"])
        st2 = (await http.get("/api/status")).json()
        check("cheap now resolves to the new model (status)", st2["tiers"]["cheap"] == "gpt-5.4-mini" and st2["pid"] == pid)
        conv = (await http.post("/api/chat", json={"message": "hello from settings e2e"})).json()
        check("a new chat without a model takes the chat default", conv.get("model") == "echo", conv)
        reply = None
        for _ in range(100):
            c = (await http.get(f"/api/chat/{conv['id']}")).json()
            if c.get("live") is None and any(m.get("role") == "assistant" for m in c.get("messages", [])):
                reply = [m for m in c["messages"] if m.get("role") == "assistant"][-1]
                break
            await asyncio.sleep(0.1)
        check("…and it answers", reply is not None and reply.get("model", "echo").startswith("echo"), reply)

        # ---------------------------------------------------------- test a key
        t = (await http.post("/api/settings/test-key", json={"provider": "deepseek"})).json()
        check("test-key in the sandbox is simulated (nothing sent)", t["ok"] is True and t["simulated"] is True and KEY not in json.dumps(t), t)

        # ---------------------------------------------------------- events
        await asyncio.sleep(0.5)
        stop.set()
        await collector
        changed = [e for e in events if e.get("type") == "settings.changed"]
        check("the page hears settings.changed (twice)", len(changed) == 2, [e.get("type") for e in events])
        check("…without the key", KEY not in json.dumps(events))

        # ---------------------------------------------------------- the guard
        r = await http.post("/api/settings/test-key", json={"provider": "deepseek"}, headers={"Origin": "https://evil.example"})
        check("a foreign Origin is refused (403)", r.status_code == 403, r.status_code)
        r = await http.patch("/api/settings", json={"providers": {"deepseek": {"api_key": None}}}, headers={"Origin": "http://evil.example"})
        check("a foreign page cannot change the settings", r.status_code == 403)
        r = await http.post("/api/shutdown", headers={"Host": f"evil.example:{PORT}"})
        check("a rebound Host is refused (403)", r.status_code == 403)
        r = await http.patch("/api/settings", json={}, headers={"Origin": "http://localhost:5178"})
        check("the Vite dev server's origin passes", r.status_code == 200)
        refused = False
        try:
            async with websockets.connect(BASE.replace("http", "ws") + "/ws", additional_headers={"Origin": "https://evil.example"}) as ws:
                await asyncio.wait_for(ws.recv(), timeout=3)
        except Exception:
            refused = True
        check("a WebSocket from a foreign origin is refused", refused)
        r = await http.post("/mcp", json={}, headers={"Origin": "https://evil.example"})
        check("/mcp is left as it was (not the guard's 403)", "only local pages" not in r.text, r.status_code)

        # ---------------------------------------------------------- back, then shut down
        r = await http.patch("/api/settings", json={"providers": {"deepseek": {"api_key": None}}})
        check("null removes the key again", r.json()["providers"]["deepseek"]["configured"] is False)
        check("…and deepseek is unusable again, no restart", (await http.get("/api/models")).json()["providers"]["deepseek"]["configured"] is False)

        r = await http.post("/api/shutdown")
        check("POST /api/shutdown accepted", r.status_code == 200 and r.json()["pid"] == pid, r.text)
    t0 = time.monotonic()
    while time.monotonic() - t0 < 20 and pid_alive(pid):
        await asyncio.sleep(0.3)
    check(f"the process exited by itself ({time.monotonic() - t0:.1f}s)", not pid_alive(pid))
    check("its pid file is gone (clean lifespan exit)", not (data_home / "omniapi.pid").exists())
    log = data_home / "logs" / "daemon.log"
    if log.exists():
        text = log.read_text(encoding="utf-8", errors="replace")
        check("daemon.log records the clean shutdown", "Runtime shutdown complete" in text)
        check("daemon.log never records the key", KEY not in text)
    print("ALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
