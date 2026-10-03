"""verify_install.py — walk a freshly installed OmniAPI the way a new user would.

Run it against an OFFLINE sandbox daemon started from the install you want to
check (nothing here calls a vendor or costs money):

    cd mcp
    OMNIAPI_HOME=<empty dir> OMNIAPI_DEV=1 OMNIAPI_OFFLINE=1 uv run omni serve --port 7801
    uv run python ../scripts/verify_install.py --port 7801

What it checks, in the order a new user meets them:

1. the daemon answers and says it is an offline development sandbox
2. the web GUI is served at ``/`` and its deep links (``/make``, ``/works``) load
3. an MCP client can connect to ``http://127.0.0.1:<port>/mcp`` — **without**
   the trailing slash, exactly as the README and ``omni mcp-config`` write it —
   and sees the tools
4. a chat round with the echo model
5. a generation from the GUI's door lands on the works wall and its file is served

Exit code 0 when everything passes; the first failure stops the run.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

import httpx

EXPECTED_TOOLS = 19


def check(label: str, ok: bool, detail: object = "") -> None:
    if not ok:
        print(f"FAIL {label}", detail)
        raise SystemExit(1)
    print(f"PASS {label}")


async def main(base: str) -> None:
    async with httpx.AsyncClient(base_url=base, timeout=60) as http:
        st = (await http.get("/api/status")).json()
        check("the daemon answers", bool(st.get("version")), st)
        check("it is an offline development sandbox (nothing here can spend money)", st.get("offline") and st.get("dev"),
              "start it with OMNIAPI_DEV=1 OMNIAPI_OFFLINE=1")

        for path in ("/", "/make", "/works", "/chat"):
            r = await http.get(path)
            check(f"the GUI is served at {path}", r.status_code == 200 and "<div id=\"root\"" in r.text,
                  f"{r.status_code} — was the GUI built (npm run build --prefix gui)?")

        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(base + "/mcp") as (read, write, _):  # no trailing slash, on purpose
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                check(f"an MCP client connects to {base}/mcp and sees the tools", len(tools) >= EXPECTED_TOOLS,
                      f"{len(tools)} tools: {[t.name for t in tools]}")

        conv = (await http.post("/api/chat", json={"model": "echo-fast"})).json()
        turn = await http.post(f"/api/chat/{conv['id']}/messages", params={"wait": True}, json={"text": "hello"})
        check("a chat round with the echo model", turn.status_code == 200 and turn.json().get("state") == "done", turn.text[:300])

        opts = (await http.get("/api/generate/options")).json()
        check("the generate page has models to offer", all(k["models"] for k in opts["kinds"].values()), {k: len(v["models"]) for k, v in opts["kinds"].items()})
        gen = (await http.post("/api/generations", json={"kind": "image", "params": {"prompt": "install check"}})).json()
        t0 = time.time()
        while gen.get("status") == "running" and time.time() - t0 < 60:
            await asyncio.sleep(0.3)
            gen = (await http.get(f"/api/generations/{gen['id']}")).json()
        check("a generation from the GUI's door finishes", gen.get("status") == "done" and gen.get("artifacts"), gen)
        work = gen["artifacts"][0]
        wall = (await http.get("/api/artifacts", params={"limit": 5})).json()
        check("it is on the works wall", any(a["id"] == work["id"] for a in wall["items"]))
        f = await http.get(work["file_url"])
        check("and its file is served", f.status_code == 200 and f.content[:4] == b"\x89PNG", f.status_code)
    print("ALL PASS")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7801)
    args = ap.parse_args()
    try:
        asyncio.run(main(f"http://{args.host}:{args.port}"))
    except httpx.ConnectError:
        print(f"FAIL nothing is listening on http://{args.host}:{args.port} — start the sandbox daemon first")
        sys.exit(1)
