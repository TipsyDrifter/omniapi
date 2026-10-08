"""Works library end-to-end against an OFFLINE sandbox daemon (no vendor call):

    OMNIAPI_HOME=<empty dir> OMNIAPI_DEV=1 OMNIAPI_OFFLINE=1 omni serve --port 7802
    python tests/e2e/artifacts_e2e.py [port]

Generates through MCP (the stand-in generators answer), then checks that
every work reached the index, the WebSocket announced it, and the file,
thumbnail and upload endpoints serve what was written.
"""

import asyncio
import json
import sys

import httpx
import websockets
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 7802
BASE = f"http://127.0.0.1:{PORT}"


def check(label: str, ok: bool, detail: object = "") -> None:
    if not ok:
        print("FAIL " + label, detail)
        raise SystemExit(1)
    print("PASS " + label)


async def ws_collect(stop: asyncio.Event, out: list) -> None:
    async with websockets.connect(BASE.replace("http", "ws") + "/ws") as ws:
        while not stop.is_set():
            try:
                out.append(json.loads(await asyncio.wait_for(ws.recv(), timeout=0.5)))
            except asyncio.TimeoutError:
                continue


async def main() -> None:
    st = httpx.get(BASE + "/api/status", timeout=10).json()
    check("the daemon is an offline dev sandbox", st["offline"] and st["dev"], st)
    before = httpx.get(BASE + "/api/artifacts", timeout=10).json()["counts"]

    stop, events = asyncio.Event(), []
    ws_task = asyncio.create_task(ws_collect(stop, events))
    await asyncio.sleep(0.5)

    async with streamablehttp_client(BASE + "/mcp") as (read, write, _):
        async with ClientSession(read, write) as s:
            await s.initialize()

            async def call(tool: str, args: dict) -> dict:
                r = await s.call_tool(tool, args)
                return json.loads(r.content[0].text)

            img = await call("generate_image", {"prompt": "a lighthouse at dusk", "n": 2})
            check("generate_image answered by the stand-in", img.get("metadata", {}).get("provider") == "echo", img)
            speech = await call("generate_speech", {"text": "早安，這是示範語音。"})
            music = await call("generate_music", {"prompt": "lofi piano", "title": "E2E 示範曲"})
            text = await call("transcribe_audio", {"audio_path": speech["audio_path"]})
            check("speech, music and transcript came back", bool(speech.get("audio_path") and music.get("audio_path") and text.get("text")))
            suno = await call("generate_music", {"prompt": "lofi piano", "model": "V6"})
            tracks = suno.get("tracks") or []
            check("a Suno job answers with both songs", suno.get("track_count") == 2 and len(tracks) == 2
                  and tracks[0]["audio_path"] == suno["audio_path"] and tracks[1]["audio_path"] != suno["audio_path"], suno)
            refused = await call("music_lyrics", {"action": "generate", "prompt": "x"})
            check("tools without a stand-in are still refused offline", refused.get("status") == "refused", refused)

            listing = httpx.get(BASE + "/api/artifacts", timeout=10).json()
            first_image = next(a for a in listing["items"] if a["kind"] == "image")
            edit = await call("edit_image", {"prompt": "make it night", "image_path": first_image["file_path"]})
            check("edit_image answered by the stand-in", edit.get("operation") == "edit", edit)

    await asyncio.sleep(0.5)
    stop.set()
    await ws_task

    listing = httpx.get(BASE + "/api/artifacts", timeout=10).json()
    delta = {k: listing["counts"].get(k, 0) - before.get(k, 0) for k in ("image", "speech", "music", "transcript")}
    check("every work reached the index", delta == {"image": 3, "speech": 1, "music": 3, "transcript": 1}, delta)
    created = [e for e in events if e.get("type") == "artifact.created"]
    check("the WebSocket announced each one", len(created) == 8, [e.get("type") for e in events])
    rows = {a["file_path"]: a for a in listing["items"] if a["kind"] == "music"}
    one, two = (rows.get(str(t["audio_path"])) for t in tracks)
    check("both Suno songs are works, the second under the first", bool(one and two) and two["parent_id"] == one["id"]
          and one["call_id"] == two["call_id"] and two["meta"]["track"] == 2, (one, two))

    by_kind: dict = {}
    for a in listing["items"]:
        by_kind.setdefault(a["kind"], a)
    child = by_kind["image"]
    check("the edit is linked to its source", child["tool"] == "edit_image" and child["parent_id"] == first_image["id"], child)
    check("works carry the call that made them", all(a.get("call_id") and a["source"] == "mcp" for a in listing["items"][:6]))

    f = httpx.get(BASE + child["file_url"], timeout=10)
    check("image file is served", f.status_code == 200 and f.headers["content-type"] == "image/png" and f.content[:4] == b"\x89PNG", f.status_code)
    t = httpx.get(BASE + child["thumb_url"], params={"w": 240}, timeout=20)
    check("thumbnail is served", t.status_code == 200 and t.headers["content-type"] == "image/webp" and len(t.content) < len(f.content), t.status_code)
    m = httpx.get(BASE + by_kind["music"]["file_url"], headers={"Range": "bytes=0-99"}, timeout=10)
    check("audio supports range requests (seeking in the player)", m.status_code == 206 and len(m.content) == 100, (m.status_code, len(m.content)))
    d = httpx.get(BASE + by_kind["music"]["file_url"], params={"download": True}, timeout=10)
    check("download sets an attachment header", "attachment" in d.headers.get("content-disposition", ""), dict(d.headers))
    tr = httpx.get(f"{BASE}/api/artifacts/{by_kind['transcript']['id']}", timeout=10).json()
    check("the transcript is kept in full, in the index and on disk", "示範逐字稿" in tr["text"] and tr["exists"], tr)

    up = httpx.post(BASE + "/api/uploads", params={"filename": "參考圖.png"}, content=f.content, timeout=20)
    check("upload accepted", up.status_code == 200 and up.json()["kind"] == "image", up.text)
    bad = httpx.post(BASE + "/api/uploads", params={"filename": "x.exe"}, content=b"MZ", timeout=20)
    check("upload refuses other file types", bad.status_code == 415, bad.status_code)

    hid = httpx.patch(f"{BASE}/api/artifacts/{child['id']}", json={"hidden": True}, timeout=10).json()
    after = httpx.get(BASE + "/api/artifacts", timeout=10).json()
    still = httpx.get(BASE + child["file_url"], timeout=10).status_code
    check("hiding takes it off the list and keeps the file", hid["hidden"] and child["id"] not in [a["id"] for a in after["items"]] and still == 200)
    httpx.patch(f"{BASE}/api/artifacts/{child['id']}", json={"hidden": False}, timeout=10)

    calls = httpx.get(BASE + "/api/calls", params={"limit": 10}, timeout=10).json()
    check("the calls ledger recorded the generations", {"generate_image", "generate_speech", "generate_music", "transcribe_audio", "edit_image"} <= {c["tool"] for c in calls})
    print("ALL PASS — storage:", st["data_home"])


asyncio.run(main())
