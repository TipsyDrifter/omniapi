"""Generate page end-to-end against an OFFLINE sandbox daemon (no vendor call):

    OMNIAPI_HOME=<empty dir> OMNIAPI_DEV=1 OMNIAPI_OFFLINE=1 OMNIAPI_FAKE_DELAY=2 omni serve --port 7802
    python tests/e2e/generate_e2e.py [port]

Starts generations the way the GUI does (REST), and checks that each one
runs in the background, announces itself on the WebSocket, lands in the
works index, and stays readable afterwards — plus sources by id, estimates,
cancelling and the requests that must be refused.
"""

import asyncio
import io
import json
import sys
import time
import wave

import httpx
import websockets

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 7802
BASE = f"http://127.0.0.1:{PORT}"


def check(label: str, ok: bool, detail: object = "") -> None:
    if not ok:
        print("FAIL " + label, detail)
        raise SystemExit(1)
    print("PASS " + label)


def wav_bytes(seconds: float = 1.0, rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


async def ws_collect(stop: asyncio.Event, out: list) -> None:
    async with websockets.connect(BASE.replace("http", "ws") + "/ws") as ws:
        while not stop.is_set():
            try:
                out.append(json.loads(await asyncio.wait_for(ws.recv(), timeout=0.5)))
            except asyncio.TimeoutError:
                continue


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE, timeout=60) as http:
        run_started = time.time()
        st = (await http.get("/api/status")).json()
        check("the daemon is an offline dev sandbox", st["offline"] and st["dev"], st)

        opts = (await http.get("/api/generate/options")).json()
        check("options list the five kinds", set(opts["kinds"]) == {"image", "speech", "music", "transcript", "video"}, list(opts["kinds"]))
        # 1.4-M3: a closed video model (Sora) stays on the list unusable (1.4-M5: Gemini Omni is connected, direct)
        closed = {(k, m["id"]) for k, v in opts["kinds"].items() for m in v["models"]
                  if k == "video" and (m.get("unavailable") or {}).get("reason") in ("retired", "not_implemented")}
        check("every model is usable in the sandbox (but a closed video model)",
              all(m["available"] for k, v in opts["kinds"].items() for m in v["models"] if (k, m["id"]) not in closed)
              and closed == {("video", "openai/sora-2-pro")}, closed)
        check("speech options carry voices for three providers", set(opts["kinds"]["speech"]["voices"]) == {"openai", "google", "elevenlabs"})
        check("openai lists 13 voices", len(opts["kinds"]["speech"]["voices"]["openai"]["voices"]) == 13)

        est = (await http.post("/api/generate/estimate", json={"kind": "image", "params": {"prompt": "x", "model": "gpt-image-2"}})).json()
        check("the sandbox estimate is zero and says why", est == {"basis": "sandbox", "usd": 0.0}, est)

        stop, events = asyncio.Event(), []
        ws_task = asyncio.create_task(ws_collect(stop, events))
        await asyncio.sleep(0.5)

        async def start(body: dict) -> dict:
            r = await http.post("/api/generations", json=body)
            check(f"start {body['kind']} accepted", r.status_code == 200, r.text)
            return r.json()

        async def finished(gid: str, timeout: float = 60) -> dict:
            t0 = time.time()
            while time.time() - t0 < timeout:
                row = (await http.get(f"/api/generations/{gid}")).json()
                if row["status"] != "running":
                    return row
                await asyncio.sleep(0.3)
            check(f"generation {gid} finished in time", False)
            return {}

        # ---- image, then an edit of it by id
        t0 = time.time()
        img = await start({"kind": "image", "params": {"prompt": "a lighthouse at dusk\nsecond line", "model": "gpt-image-2", "n": 2}})
        check("start returns at once with a running job", img["status"] == "running" and time.time() - t0 < 2, img)
        check("the job is titled from the prompt's first line", img["title"] == "a lighthouse at dusk", img["title"])
        listed = (await http.get("/api/generations", params={"status": "running"})).json()
        check("the running job is listed", any(g["id"] == img["id"] for g in listed))
        img = await finished(img["id"])
        check("the image job finished with two works", img["status"] == "done" and len(img["artifacts"]) == 2, img)
        check("its works came through the gui door", all(a["source"] == "gui" for a in img["artifacts"]))
        first = img["artifacts"][0]
        check("the work's file is served", (await http.get(first["file_url"])).status_code == 200)

        edit = await start({"kind": "image", "params": {"prompt": "make it night"}, "sources": {"images": [{"artifact_id": first["id"]}]}})
        check("an image job with a source is an edit", edit["tool"] == "edit_image", edit["tool"])
        check("the request shows the source by name and url, not by path",
              edit["sources"]["images"][0]["thumb_url"] and "file_path" not in json.dumps(edit), edit["sources"])
        edit = await finished(edit["id"])
        check("the edit finished", edit["status"] == "done" and len(edit["artifacts"]) == 1, edit)
        check("the edit points back at the image it started from", edit["artifacts"][0]["parent_id"] == first["id"], edit["artifacts"][0])

        # ---- speech, then a transcript of it; and a transcript of an upload
        speech = await finished((await start({"kind": "speech", "params": {"text": "早安，這是示範語音。", "voice": "alloy", "model": "gpt-4o-mini-tts"}}))["id"])
        check("the speech job finished", speech["status"] == "done" and speech["artifacts"][0]["kind"] == "speech", speech)
        tr = await finished((await start({"kind": "transcript", "params": {"model": "gpt-transcribe"}, "sources": {"audio": {"artifact_id": speech["artifacts"][0]["id"]}}}))["id"])
        check("the transcript job finished with text", tr["status"] == "done" and tr["artifacts"][0]["kind"] == "transcript" and tr["artifacts"][0]["text"], tr)
        check("the transcript points back at its audio", tr["artifacts"][0]["parent_id"] == speech["artifacts"][0]["id"])
        up = (await http.post("/api/uploads", params={"filename": "會議錄音.wav"}, content=wav_bytes())).json()
        tr2 = await start({"kind": "transcript", "params": {}, "sources": {"audio": {"upload_id": up["id"]}}})
        check("a transcript of an upload is titled with the upload's name", tr2["title"] == "會議錄音.wav", tr2["title"])
        check("the upload transcript finished", (await finished(tr2["id"]))["status"] == "done")

        # ---- music: leave, come back, cancel
        music = await start({"kind": "music", "params": {"prompt": "[Verse 1]\nRain on the window", "model": "V6", "title": ""}})
        check("lyric tags are skipped for the title", music["title"] == "Rain on the window", music["title"])
        check("the status counts the live job", (await http.get("/api/status")).json()["live_generations"] >= 1)
        music = await finished(music["id"])
        check("the music job finished", music["status"] == "done" and music["artifacts"][0]["kind"] == "music", music)
        # a Suno job makes two songs: both land as works, the second hangs under the first
        songs = music["artifacts"]
        check("a Suno job keeps both songs", len(songs) == 2 and all(a["kind"] == "music" for a in songs), [a.get("file_path") for a in songs])
        check("the second song is linked to the first", songs[1]["parent_id"] == songs[0]["id"], songs[1].get("parent_id"))
        detail = (await http.get(f"/api/artifacts/{songs[0]['id']}")).json()
        check("the first song lists the second as made with it", [c["id"] for c in detail.get("children") or []] == [songs[1]["id"]], detail.get("children"))
        wall = (await http.get("/api/artifacts", params={"kind": "music", "limit": 10})).json()
        wall_ids = {a["id"] for a in wall.get("items") or wall.get("artifacts") or []}
        check("both songs are on the works wall", {a["id"] for a in songs} <= wall_ids, sorted(wall_ids))

        doomed = await start({"kind": "music", "params": {"prompt": "to be cancelled"}})
        cancelled = (await http.post(f"/api/generations/{doomed['id']}/cancel")).json()
        check("a cancelled job says so", cancelled["status"] == "cancelled" and not cancelled["artifacts"], cancelled)
        await asyncio.sleep(5)  # longer than the stand-in's delay: the work must not land afterwards
        check("and stays cancelled", (await http.get(f"/api/generations/{doomed['id']}")).json()["status"] == "cancelled")

        # ---- refusals
        async def refused(label: str, body: dict, code: int) -> None:
            r = await http.post("/api/generations", json=body)
            check(label, r.status_code == code, f"{r.status_code} {r.text[:200]}")

        await refused("a path argument is refused", {"kind": "image", "params": {"prompt": "x", "image_path": "C:/Windows/win.ini"}}, 400)
        await refused("an unknown kind is refused", {"kind": "hologram", "params": {"prompt": "x"}}, 400)
        await refused("an empty prompt is refused before a job exists", {"kind": "image", "params": {"prompt": ""}}, 400)
        await refused("a transcript without audio is refused", {"kind": "transcript", "params": {}}, 400)
        await refused("an image cannot be the audio source", {"kind": "transcript", "params": {}, "sources": {"audio": {"artifact_id": first["id"]}}}, 400)
        await refused("an unknown source is 404", {"kind": "image", "params": {"prompt": "x"}, "sources": {"images": [{"artifact_id": "nope"}]}}, 404)
        await refused("speech takes no source", {"kind": "speech", "params": {"text": "x"}, "sources": {"audio": {"upload_id": up["id"]}}}, 400)

        await asyncio.sleep(0.5)
        stop.set()
        await ws_task
        started = [e for e in events if e.get("type") == "generation.started"]
        done = [e for e in events if e.get("type") == "generation.finished"]
        check("every job announced its start and its end", len(started) == 7 and len(done) == 7, (len(started), len(done)))
        check("the finished event carries the works", any(e["generation"]["artifacts"] for e in done))

        calls = (await http.get("/api/calls", params={"limit": 20})).json()
        gui_calls = [c for c in calls if c.get("source") == "gui" and c["ts"] >= run_started]
        check("the ledger recorded the gui calls as finished, never as tickets",
              len(gui_calls) == 7 and all(c["status"] in ("ok", "error") for c in gui_calls),
              [(c["tool"], c["status"]) for c in gui_calls])
    print("ALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
