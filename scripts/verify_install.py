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
6. v1.2 chat: a message with an image (the echo model says it got one); one
   with a text file and a PDF made on the spot (the reply records both); "draw…"
   brings a proposal, accepting it puts a chat-sourced work on the wall;
   regenerating a reply gives version 2; deleting the conversation

Exit code 0 when everything passes; the first failure stops the run.
"""

from __future__ import annotations

import argparse
import asyncio
import io
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

        await chat_v12(http)
    print("ALL PASS")


async def chat_v12(http: httpx.AsyncClient) -> None:
    """v1.2 chat: attachments, a generation proposed in the chat, a second version, delete."""
    up = lambda name, data: http.post("/api/uploads", params={"filename": name, "purpose": "chat"}, content=data)  # noqa: E731

    # an image in a chat
    img = (await up("install-check.png", png())).json()
    conv = (await http.post("/api/chat", json={"model": "echo-fast"})).json()
    cid = conv["id"]
    t = (await http.post(f"/api/chat/{cid}/messages", params={"wait": True},
                         json={"text": "what is in this picture", "attachments": [{"upload_id": img["id"]}]})).json()
    check("a chat message with an image: the echo model says it got 1", t.get("state") == "done" and "附了 **1** 張圖" in t["message"]["content"],
          str(t)[:300])
    first_reply = t["message"]

    # a text file and a PDF, made right here
    txt = (await up("notes.txt", "install check notes\nline two\n".encode("utf-8"))).json()
    doc = (await up("brief.pdf", pdf(["Install check brief", "Page two"]))).json()
    check("the PDF upload says how many pages it has", doc.get("kind") == "file" and (doc.get("info") or {}).get("pages") == 2, doc.get("info"))
    t = (await http.post(f"/api/chat/{cid}/messages", params={"wait": True},
                         json={"text": "what do these say", "attachments": [{"upload_id": txt["id"]}, {"upload_id": doc["id"]}]})).json()
    sent = {e["name"]: e["mode"] for e in (t.get("message") or {}).get("meta", {}).get("files") or []}
    check("a chat message with a text file and a PDF: the reply records both", t.get("state") == "done"
          and set(sent) == {"notes.txt", "brief.pdf"} and all(sent.values()), sent)

    # "draw…" → a proposal; accept → the work lands on the wall as a chat work
    gens_before = {g["id"] for g in (await http.get("/api/generations", params={"limit": 200})).json()}
    t = (await http.post(f"/api/chat/{cid}/messages", params={"wait": True}, json={"text": "幫我畫一隻戴耳機的貓"})).json()
    props = (t.get("message") or {}).get("proposals") or []
    check("asking for a picture brings a proposal", len(props) == 1 and props[0]["state"] == "pending" and props[0]["kind"] == "image", props)
    gens_now = {g["id"] for g in (await http.get("/api/generations", params={"limit": 200})).json()}
    check("and nothing is generated before it is accepted", gens_now == gens_before)
    acc = await http.post(f"/api/chat/{cid}/proposals/{props[0]['id']}/accept", json={})
    check("accepting it starts the generation", acc.status_code == 200 and acc.json().get("state") == "generating", acc.text[:300])
    done = await proposal_settled(http, cid, props[0]["id"])
    check("the proposal ends with a work", done.get("state") == "done" and (done.get("artifact") or {}).get("id"), done)
    wall = (await http.get("/api/artifacts", params={"source": "chat", "limit": 50})).json()
    row = next((a for a in wall["items"] if a["id"] == done["artifact"]["id"]), None)
    check("the work is on the wall, its source the chat", row is not None and row.get("source") == "chat", (row or {}).get("source"))

    # regenerate the first reply: version 2 of 2
    r2 = (await http.post(f"/api/chat/{cid}/messages/{first_reply['id']}/regenerate", params={"wait": True}, json={})).json()
    got = (await http.get(f"/api/chat/{cid}")).json()
    shown = next((m for m in got["messages"] if m["id"] == (r2.get("message") or {}).get("id")), None)
    check("regenerating a reply gives version 2 of 2", r2.get("state") == "done" and shown is not None
          and shown["versions"]["count"] == 2 and shown["versions"]["index"] == 2, (shown or {}).get("versions"))

    # delete the conversation
    gone = (await http.delete(f"/api/chat/{cid}")).json()
    check("deleting the conversation", gone.get("deleted") is True and (await http.get(f"/api/chat/{cid}")).status_code == 404, gone)
    check("and it leaves its uploads and its work", (await http.get(img["file_url"])).status_code == 200
          and (await http.get(done["artifact"]["file_url"])).status_code == 200)


async def proposal_settled(http: httpx.AsyncClient, cid: str, pid: str, seconds: float = 60) -> dict:
    deadline = time.time() + seconds
    while True:
        conv = (await http.get(f"/api/chat/{cid}")).json()
        found = [p for m in conv["messages"] for p in m.get("proposals") or [] if p["id"] == pid]
        if found and found[0]["state"] not in ("pending", "generating"):
            return found[0]
        if time.time() > deadline:
            return found[0] if found else {}
        await asyncio.sleep(0.3)


def png() -> bytes:
    from PIL import Image  # Pillow is a dependency of the daemon

    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (40, 90, 200)).save(buf, format="PNG")
    return buf.getvalue()


def pdf(pages: list[str]) -> bytes:
    """A small valid PDF, one line of text per page."""
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            f"<< /Type /Pages /Kids [{' '.join(f'{4 + 2 * i} 0 R' for i in range(len(pages)))}] /Count {len(pages)} >>".encode(),
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    for i, text in enumerate(pages):
        content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>".encode())
        objs.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
    out, offsets = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for k, body in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(f"{k} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets))
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


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
