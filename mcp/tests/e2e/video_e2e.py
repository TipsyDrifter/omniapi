"""Video (1.4-M3) end-to-end against OFFLINE sandbox daemons this script starts and kills itself:

    python tests/e2e/video_e2e.py [port]          (default 7881; run from mcp/ with the project venv)

No vendor is called and nothing is spent: the sandbox's stand-in vendor
(``video/sandbox.py``) keeps its jobs on disk and hands back a real, small MP4.
Every daemon runs with ``OMNIAPI_HOME`` in a fresh temporary folder,
``OMNIAPI_DEV=1 OMNIAPI_OFFLINE=1``, a 4-second stand-in video
(``OMNIAPI_FAKE_DELAY``), polling every 0.5 s (``OMNIAPI_VIDEO_POLL``) and
MCP tickets after 2 s (``OMNIAPI_JOB_SOFT_TIMEOUT``).

Walks:
  0. a v1.3.0-format database (built with v1.3.0's own store code) is upgraded in place, its rows unchanged
  1. the roster -> the estimate -> a video from the generate page's API -> its state while waiting ->
     done: one more work, the cost on the ledger, /file answers a Range request, the poster
  2. a video from a first frame is linked back to that image
  3. the daemon is killed (forcefully) in the middle of a video and started again: the same job is picked
     up and its file collected — and without ffmpeg on PATH the thumbnail says "no poster" (204)
  4. "stop waiting" -> collected in the background anyway (marked late)
  5. keep_collecting off -> "stop waiting" leaves it; "ask again" collects it
  6. waited too long (a 3-second limit) -> "ask again" waits on the same job, nothing is sent again
  7. MCP over a real connection: generate_video hands back a ticket, get_job_result fetches the video;
     a ticket survives a daemon kill; an estimate over the limit and a per-token model are refused with
     the reason; max_cost_usd lets the first one through
  8. Gemini Omni (Google, direct; 1.4-M5): listed and usable, its own request shape and price, a video
     made through the stand-in; with a (made-up) Google key set, OpenRouter's google/gemini-omni* and
     google/veo-* rows are no longer listed — Omni itself still is
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 7881
BASE = f"http://127.0.0.1:{PORT}"
MCP_DIR = Path(__file__).resolve().parents[2]
REPO = MCP_DIR.parent
WAN = "alibaba/wan-3.0"
GROK = "x-ai/grok-imagine-video"
KLING = "kwaivgi/kling-v3.0-std"
SEEDANCE = "bytedance/seedance-2.0-fast"
OMNI = "gemini-omni-1.1-flash"
OR_OMNI = "google/gemini-omni-1.1-flash"
#: made up, shaped like no real key; the sandbox never sends it anywhere
FAKE_GOOGLE_KEY = "sandbox-made-up-google-key-0000"


def check(label: str, ok: bool, detail: object = "") -> None:
    if not ok:
        print("FAIL " + label, detail)
        raise SystemExit(1)
    print("PASS " + label)


# ------------------------------------------------------------------ the daemon
class Daemon:
    def __init__(self, home: Path):
        self.home = home
        self.proc: subprocess.Popen | None = None

    def start(self, *, ffmpeg: bool = True, extra: dict | None = None) -> None:
        # OMNIAPI_SKIP_REPO_ENV=1: the sandbox never reads the checkout's mcp/.env (real keys would turn providers on
        # and change the model lists this script expects); an offline sandbox skips it by default, this says so
        env = {**os.environ, "OMNIAPI_HOME": str(self.home), "OMNIAPI_DEV": "1", "OMNIAPI_OFFLINE": "1", "OMNIAPI_SKIP_REPO_ENV": "1",
               "OMNIAPI_FAKE_DELAY": "4", "OMNIAPI_VIDEO_POLL": "0.5", "OMNIAPI_JOB_SOFT_TIMEOUT": "2",
               "PYTHONIOENCODING": "utf-8", **(extra or {})}
        if not ffmpeg:  # a computer without ffmpeg: no poster
            env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep)
                                          if p and not (Path(p) / "ffmpeg.exe").exists() and not (Path(p) / "ffmpeg").exists())
        log = open(self.home.parent / f"daemon-{PORT}.log", "ab")
        self.proc = subprocess.Popen([sys.executable, "-m", "omniapi_mcp.cli", "serve", "--foreground", "--port", str(PORT)],
                                     cwd=str(MCP_DIR), env=env, stdout=log, stderr=subprocess.STDOUT)
        t0 = time.time()
        while time.time() - t0 < 60:
            try:
                if httpx.get(BASE + "/api/health", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.3)
        raise SystemExit("FAIL the daemon did not start (see the log)")

    def kill(self) -> None:
        """Forced: no shutdown hook runs (TerminateProcess on Windows)."""
        assert self.proc is not None
        self.proc.kill()
        self.proc.wait(timeout=30)
        t0 = time.time()
        while time.time() - t0 < 10:
            try:
                httpx.get(BASE + "/api/health", timeout=0.5)
                time.sleep(0.2)
            except httpx.HTTPError:
                return

    def stop(self) -> None:
        """A normal stop (``omni stop --port``)."""
        subprocess.run([sys.executable, "-m", "omniapi_mcp.cli", "stop", "--port", str(PORT)], cwd=str(MCP_DIR),
                       env={**os.environ, "OMNIAPI_HOME": str(self.home)}, capture_output=True, timeout=60)
        if self.proc is not None:
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.kill()


async def until(http: httpx.AsyncClient, gid: str, statuses: tuple[str, ...], timeout: float = 40) -> dict:
    t0 = time.time()
    row: dict = {}
    while time.time() - t0 < timeout:
        row = (await http.get(f"/api/generations/{gid}")).json()
        if row.get("status") in statuses:
            return row
        await asyncio.sleep(0.25)
    raise SystemExit(f"FAIL {gid} never reached {statuses}: {row.get('status')} {row.get('error')}")


def png_bytes() -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (96, 64), (30, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


# ------------------------------------------------------------------ 0. a v1.3.0 database
async def build_v13_database(home: Path) -> None:
    """A database written by v1.3.0's own store code (``git show v1.3.0:…/store/db.py``)."""
    src = subprocess.run(["git", "show", "v1.3.0:mcp/omniapi_mcp/store/db.py"], cwd=str(REPO), capture_output=True,
                         check=True).stdout.decode("utf-8")
    src = src.replace("from ..catalog.paths import data_home", "data_home = None  # replaced: the test names the path")
    ns: dict = {"__name__": "v13_db"}
    exec(compile(src, "v13_db.py", "exec"), ns)

    async def fill() -> None:
        s = ns["Store"](home / "omniapi.db")
        await s.open()
        await s.add_artifact(artifact_id="v13work00000001", kind="image", tool="generate_image", model="gpt-image-2",
                             title="一點三的作品", file_path=str(home / "old.png"), mime="image/png", cost_usd=0.04, source="gui")
        await s.create_generation("v13gen0000000001", kind="image", tool="generate_image", model="gpt-image-2", title="一點三的工作",
                                  params={"prompt": "x"}, sources={}, estimate={"basis": "per_image", "usd": 0.04}, source="gui")
        await s.update_generation("v13gen0000000001", status="done", cost_usd=0.04, finished_at=time.time(), artifact_ids=["v13work00000001"])
        c = await s.create_conversation(kind="chat", title="一點三的聊天")
        await s.add_message(c["id"], role="user", content="你好")
        await s.close()

    await fill()
    con = sqlite3.connect(home / "omniapi.db")
    cols = {r[1] for r in con.execute("PRAGMA table_info(generations)")}
    con.close()
    check("the seeded database has v1.3.0's generations table (no remote columns)", "remote_id" not in cols, cols)


async def phase_upgrade() -> None:
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as http:
        old = (await http.get("/api/generations/v13gen0000000001")).json()
        check("a v1.3 generation reads back unchanged", (old["title"], old["status"], old["cost_usd"], old["artifact_ids"]) ==
              ("一點三的工作", "done", 0.04, ["v13work00000001"]), old)
        work = (await http.get("/api/artifacts/v13work00000001")).json()
        check("a v1.3 work reads back unchanged", work["title"] == "一點三的作品" and work["cost_usd"] == 0.04, work)
        chats = (await http.get("/api/chat")).json()
        check("a v1.3 chat is still there", any(c.get("title") == "一點三的聊天" for c in (chats if isinstance(chats, list) else chats.get("items", []))), chats)


# ------------------------------------------------------------------ 1-2. the generate page's API
async def phase_basic(http: httpx.AsyncClient) -> dict:
    st = (await http.get("/api/status")).json()
    check("the daemon is an offline dev sandbox", st["offline"] and st["dev"], st)
    models = (await http.get("/api/models", params={"modality": "video"})).json()["models"].get("video", [])
    ids = {m["id"] for m in models}
    check("the model list has the stand-in video roster", {WAN, GROK, KLING, SEEDANCE, "google/veo-3.1-lite"} <= ids, sorted(ids))
    check("…not the editors (left out of this version)", "black-forest-labs/flux-video-edit" not in ids)
    opts = (await http.get("/api/generate/options")).json()["kinds"]["video"]
    rows = {m["id"]: m for m in opts["models"]}
    check("the generate options carry each model's request shape", rows[KLING]["video_params"]["frames"] == ["first_frame", "last_frame"]
          and rows[GROK]["video_params"]["durations"][0] == 1, rows[KLING].get("video_params"))
    check("the default video model is Wan 3.0", opts["default_model"] == WAN, opts["default_model"])
    check("a closed model is listed but not usable, with why", rows["openai/sora-2-pro"]["unavailable"]["reason"] == "retired"
          and rows["openai/sora-2-pro"]["status"] == "retired")
    check("the announced shutdown is on the row", rows["google/veo-3.1-lite"]["shutdown"] == "2026-10-22")
    check("the limits are in the options", opts["limits"]["mcp_max_usd"] == 1.0 and opts["limits"]["max_wait_s"] == 1200, opts["limits"])
    check("the models left out are named with why", any(u["id"] == "black-forest-labs/flux-video-edit" and u["reason"] == "not_generation"
                                                        for u in opts["unlisted"]), opts["unlisted"])

    est = (await http.post("/api/generate/estimate", json={"kind": "video", "params": {"model": WAN, "resolution": "480p", "duration": 5}})).json()
    check("the estimate: zero in the sandbox, the listed price beside it", est["basis"] == "sandbox" and est["listed"]["usd"] == 0.25
          and est["listed"]["basis"] == "per_second" and est["listed"]["approx"] is True, est)
    est = (await http.post("/api/generate/estimate", json={"kind": "video", "params": {"model": SEEDANCE, "resolution": "480p"}})).json()
    check("a per-token model cannot be priced up front", est["listed"]["basis"] == "per_token" and est["listed"]["usd"] is None, est)
    est = (await http.post("/api/generate/estimate", json={"kind": "video", "params": {"model": WAN}})).json()
    check("no resolution chosen: a range", est["listed"]["basis"] == "range" and (est["listed"]["low"], est["listed"]["high"]) == (0.25, 1.0), est)

    before = (await http.get("/api/artifacts")).json()["counts"].get("video", 0)
    calls_before = len((await http.get("/api/calls", params={"tool": "generate_video", "limit": 200})).json())
    r = await http.post("/api/generations", json={"kind": "video", "params": {"prompt": "a paper boat drifts across a pond", "model": WAN,
                                                                               "resolution": "480p", "duration": 5, "aspect_ratio": "16:9"}})
    check("a video starts from the generate page's API", r.status_code == 200, r.text)
    gen = r.json()
    v = gen["video"]
    check("…its remote job id is stored at once", gen["status"] == "running" and v["remote_id"] and v["submitted_at"], gen)
    await asyncio.sleep(1.2)
    mid = (await http.get(f"/api/generations/{gen['id']}")).json()
    check("while waiting: the provider's state, when it was last asked, how long so far",
          mid["status"] == "running" and mid["video"]["remote_status"] in ("pending", "in_progress") and mid["video"]["polled_at"]
          and mid["video"]["waited_s"] >= 1 and mid["video"]["polls"] >= 1, mid["video"])
    listed = [g for g in (await http.get("/api/generations", params={"kind": "video"})).json() if g["id"] == gen["id"]]
    check("it is in the generation list with its waiting facts", listed and listed[0]["video"]["remote_id"] == v["remote_id"])
    done = await until(http, gen["id"], ("done",))
    work = done["artifacts"][0]
    check("done: a video work", work["kind"] == "video" and work["model"] == WAN and work["tool"] == "generate_video", work)
    check("…its size and length are the file's (asked 480p 16:9, the stand-in is 256x144)",
          (work["width"], work["height"], work["duration_s"]) == (256, 144, 2.0) and work["has_audio"] is True, work)
    check("…the cost on the job", done["cost_usd"] == 0.0 and done["video"]["charged"] == "yes", done)
    wall = (await http.get("/api/artifacts")).json()
    check("the wall has one more video", wall["counts"].get("video", 0) == before + 1 and wall["items"][0]["id"] == work["id"], wall["counts"])
    calls = (await http.get("/api/calls", params={"tool": "generate_video", "limit": 200})).json()
    mine = [c for c in calls if c["id"] == done["call_id"]]
    check("the ledger has the call with the real cost", len(calls) == calls_before + 1 and mine and mine[0]["status"] == "ok"
          and mine[0]["cost_usd"] == 0.0 and mine[0]["source"] == "gui", mine)
    rng = await http.get(work["file_url"], headers={"Range": "bytes=0-99"})
    check("/file answers a Range request (the timeline can be dragged)", rng.status_code == 206 and len(rng.content) == 100
          and rng.headers.get("content-range", "").startswith("bytes 0-99/") and rng.headers.get("accept-ranges") == "bytes",
          (rng.status_code, dict(rng.headers)))
    tail = await http.get(work["file_url"], headers={"Range": "bytes=1000-"})
    check("…from the middle too", tail.status_code == 206 and tail.headers["content-type"] == "video/mp4")
    th = await http.get(f"/api/artifacts/{work['id']}/thumb")
    if work["poster"]:
        check("with ffmpeg: a poster thumbnail", th.status_code == 200 and th.headers["content-type"] == "image/webp", th.status_code)
    else:
        check("without ffmpeg: 'no poster', not an error", th.status_code == 204 and th.headers.get("x-omniapi-poster") == "none")
    return work


async def phase_first_frame(http: httpx.AsyncClient) -> None:
    img = await http.post("/api/generations", json={"kind": "image", "params": {"prompt": "a lighthouse at dusk", "model": "gpt-image-2"}})
    check("an image to start from", img.status_code == 200, img.text)
    image = (await until(http, img.json()["id"], ("done",)))["artifacts"][0]
    est = (await http.post("/api/generate/estimate", json={"kind": "video", "params": {"model": GROK, "resolution": "480p", "duration": 1},
                                                           "sources": {"frames": {"first": {"artifact_id": image["id"]}}}})).json()
    check("a first frame adds its fee to the estimate ($0.05 + $0.002)", est["listed"]["usd"] == 0.052, est)
    bad = await http.post("/api/generations", json={"kind": "video", "params": {"prompt": "x", "model": GROK},
                                                     "sources": {"frames": {"first": {"artifact_id": image["id"]}, "last": {"artifact_id": image["id"]}}}})
    check("a last frame for a model that takes none is refused before anything is sent", bad.status_code == 400 and "not a last frame" in bad.text, bad.text)
    r = await http.post("/api/generations", json={"kind": "video", "params": {"prompt": "the light turns", "model": GROK, "resolution": "480p", "duration": 1},
                                                   "sources": {"frames": {"first": {"artifact_id": image["id"]}}}})
    check("a video from a first frame starts", r.status_code == 200 and r.json()["sources"]["frames"]["first"]["artifact_id"] == image["id"], r.text)
    done = await until(http, r.json()["id"], ("done",))
    video = done["artifacts"][0]
    check("…and is linked back to its source image", video["parent_id"] == image["id"], video)
    detail = (await http.get(f"/api/artifacts/{image['id']}")).json()
    check("…which lists the video among what was made from it", any(c["id"] == video["id"] for c in detail["children"]), detail["children"])
    up = await http.post("/api/uploads", params={"filename": "frame.png"}, content=png_bytes())
    check("an uploaded image works as a first frame too", up.status_code == 200, up.text)
    r = await http.post("/api/generations", json={"kind": "video", "params": {"prompt": "x", "model": KLING, "duration": 3},
                                                   "sources": {"frames": {"first": {"upload_id": up.json()["id"]}, "last": {"artifact_id": image["id"]}}}})
    check("…with a last frame, for a model that takes one", r.status_code == 200 and set(r.json()["sources"]["frames"]) == {"first", "last"}, r.text)
    await until(http, r.json()["id"], ("done",))
    music = await http.post("/api/generations", json={"kind": "music", "params": {"prompt": "x"}})
    song = (await until(http, music.json()["id"], ("done",)))["artifacts"][0]
    bad = await http.post("/api/generations", json={"kind": "video", "params": {"prompt": "x", "model": GROK}, "sources": {"frames": {"first": {"artifact_id": song["id"]}}}})
    check("a song is not a frame", bad.status_code == 400, bad.text)


# ------------------------------------------------------------------ 4-6. stop waiting, waited too long, ask again
async def phase_owner_actions(http: httpx.AsyncClient) -> None:
    r = await http.post("/api/generations", json={"kind": "video", "params": {"prompt": "stop waiting on me", "model": WAN, "resolution": "480p"}})
    gid = r.json()["id"]
    out = (await http.post(f"/api/generations/{gid}/stop-waiting")).json()
    check("'stop waiting': detached, still collected in the background", out["status"] == "detached" and out["video"]["detached_at"]
          and out["video"]["charged"] == "likely", out)
    done = await until(http, gid, ("done",))
    check("…collected later, marked as such, with its real cost", done["video"]["late"] is True and done["artifacts"] and done["cost_usd"] == 0.0, done)
    check("…and the work says so too", (await http.get(f"/api/artifacts/{done['artifacts'][0]['id']}")).json()["meta"]["late"] is True)

    p = await http.patch("/api/settings", json={"video": {"keep_collecting": False}})
    check("the setting 'keep collecting after stop waiting' can be switched off", p.status_code == 200 and p.json()["video"]["keep_collecting"]["value"] is False, p.text)
    r = await http.post("/api/generations", json={"kind": "video", "params": {"prompt": "leave me", "model": WAN, "resolution": "480p"}})
    gid = r.json()["id"]
    out = (await http.post(f"/api/generations/{gid}/cancel")).json()  # the page's old "stop" button means the same here
    check("with it off, stopping leaves the video uncollected", out["status"] == "abandoned" and out["video"]["can_recheck"], out)
    call = (await http.get(f"/api/calls/{out['call_id']}")).json()
    check("…and the ledger says the cost is unknown (sent, not collected)", call["status"] == "unsettled" and call["cost_usd"] is None, call)
    await asyncio.sleep(5)
    check("…nobody asked about it meanwhile", (await http.get(f"/api/generations/{gid}")).json()["status"] == "abandoned")
    again = (await http.post(f"/api/generations/{gid}/recheck")).json()
    check("'ask again' collects it after all", again["status"] == "done" and again["artifacts"], again)
    await http.patch("/api/settings", json={"video": {"keep_collecting": None}})

    p = await http.patch("/api/settings", json={"video": {"max_wait_minutes": 0.05}})
    check("the longest wait is a setting", p.status_code == 200 and p.json()["video"]["max_wait_minutes"]["value"] == 0.05, p.text)
    n_before = len((await http.get("/api/generations", params={"kind": "video", "limit": 200})).json())
    r = await http.post("/api/generations", json={"kind": "video", "params": {"prompt": "[never] a glacier creeps", "model": WAN, "resolution": "480p"}})
    gid, rid = r.json()["id"], r.json()["video"]["remote_id"]
    gave = await until(http, gid, ("gave_up",), timeout=20)
    check("waited too long: 'gave_up', the job id kept, can be asked again", gave["error_kind"] == "gave_up" and gave["video"]["remote_id"] == rid
          and gave["video"]["can_recheck"], gave)
    again = (await http.post(f"/api/generations/{gid}/recheck")).json()
    check("'ask again' waits on the same job (nothing sent again)", again["status"] == "running" and again["video"]["remote_id"] == rid
          and len((await http.get("/api/generations", params={"kind": "video", "limit": 200})).json()) == n_before + 1, again)
    await until(http, gid, ("gave_up",), timeout=20)
    r = await http.post(f"/api/generations/{gid}/stop-waiting")
    check("stop waiting on something no longer waited on changes nothing", r.status_code == 200 and r.json()["status"] == "gave_up")
    await http.patch("/api/settings", json={"video": {"max_wait_minutes": None}})

    r = await http.post("/api/generations", json={"kind": "video", "params": {"prompt": "[fail] x", "model": WAN, "resolution": "480p"}})
    bad = await until(http, r.json()["id"], ("error",))
    check("a failure at the provider says so, in its words", "Content policy violation" in bad["error"] and bad["video"]["charged"] == "unknown", bad)


# ------------------------------------------------------------------ 7. MCP
async def mcp_call(tool: str, args: dict) -> dict:
    async with streamablehttp_client(BASE + "/mcp") as (read, write, _):
        async with ClientSession(read, write) as s:
            await s.initialize()
            res = await s.call_tool(tool, args)
            return json.loads(res.content[0].text) if not res.isError else {"mcp_error": res.content[0].text}


async def phase_mcp_ticket(daemon: Daemon) -> None:
    listed = await mcp_call("list_available_models", {"modality": "video"})
    check("list_available_models shows the video models with their request shape",
          any(m["id"] == WAN and m["video_params"]["resolutions"] for m in listed["models"]["video"]))
    t = await mcp_call("generate_video", {"prompt": "waves on a pier", "model": WAN, "resolution": "480p", "duration": 5})
    check("generate_video hands back a ticket when the video takes longer than the call may", t.get("status") == "running"
          and t.get("task_id", "").startswith("video_"), t)
    r = await mcp_call("get_job_result", {"task_id": t["task_id"]})
    check("…which says it is still being made", r.get("status") == "running" and "restart" in r.get("message", ""), r)
    t0 = time.time()
    while r.get("status") == "running" and time.time() - t0 < 30:
        await asyncio.sleep(1)
        r = await mcp_call("get_job_result", {"task_id": t["task_id"]})
    check("get_job_result fetches the finished video", r.get("status") == "completed" and Path(r["file_path"]).is_file()
          and r["duration_s"] == 2.0 and r["has_audio"] is True and r["video_url"].startswith("file:"), r)
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as http:
        call = [c for c in (await http.get("/api/calls", params={"tool": "generate_video", "limit": 200})).json()
                if (c.get("result") or {}).get("task_id") == t["task_id"]]
        check("the MCP call's ledger row went from ticket to its real cost", call and call[0]["status"] == "ok" and call[0]["cost_usd"] == 0.0
              and call[0]["source"] == "mcp", call)

    # a ticket survives the daemon dying
    t = await mcp_call("generate_video", {"prompt": "rain on a window", "model": WAN, "resolution": "480p"})
    check("another ticket", t.get("status") == "running", t)
    daemon.kill()
    daemon.start()
    r = await mcp_call("get_job_result", {"task_id": t["task_id"]})
    t0 = time.time()
    while r.get("status") == "running" and time.time() - t0 < 40:
        await asyncio.sleep(1)
        r = await mcp_call("get_job_result", {"task_id": t["task_id"]})
    check("the same ticket fetches the video after the daemon was killed and started again", r.get("status") == "completed"
          and r.get("resumed_after_restart") is True and Path(r["file_path"]).is_file(), r)
    r2 = await mcp_call("get_job_result", {"task_id": r["generation_id"]})
    check("…and the bare generation id works as the ticket too", r2.get("status") == "completed" and r2["artifact_id"] == r["artifact_id"], r2)

    over = await mcp_call("generate_video", {"prompt": "x", "model": KLING, "resolution": "720p", "duration": 15, "generate_audio": True})
    check("over the per-video limit: not sent, with the estimate, the limit and how to allow it", over.get("status") == "refused"
          and over["reason"] == "over_limit" and over["limit_usd"] == 1.0 and abs(over["estimate_usd"] - 1.89) < 1e-6
          and "max_cost_usd=1.89" in over["message"], over)
    tok = await mcp_call("generate_video", {"prompt": "x", "model": SEEDANCE, "resolution": "480p"})
    check("a per-token model is not sent without an amount", tok.get("status") == "refused" and tok["reason"] == "price_unknown"
          and "max_cost_usd" in tok["message"], tok)
    ok = await mcp_call("generate_video", {"prompt": "x", "model": KLING, "resolution": "720p", "duration": 15, "generate_audio": True, "max_cost_usd": 2})
    check("with max_cost_usd the same video is sent", ok.get("status") in ("running", "completed") and ok.get("task_id", "").startswith("video_"), ok)
    bad = await mcp_call("generate_video", {"prompt": "x", "model": "openai/sora-2-pro"})
    check("a closed model is refused with the reason", "closed by its maker" in json.dumps(bad), bad)
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as http:
        await until(http, ok["generation_id"], ("done",))


async def phase_restart(daemon: Daemon) -> None:
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as http:
        r = await http.post("/api/generations", json={"kind": "video", "params": {"prompt": "a slow tide comes in", "model": WAN, "resolution": "480p"}})
        gid, rid = r.json()["id"], r.json()["video"]["remote_id"]
        await asyncio.sleep(1.5)
        mid = (await http.get(f"/api/generations/{gid}")).json()
        check("a video is half-way", mid["status"] == "running" and mid["video"]["polls"] >= 1, mid["video"])
    daemon.kill()
    check("the daemon is gone (killed, no shutdown hook)", True)
    daemon.start(ffmpeg=False)
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as http:
        row = (await http.get(f"/api/generations/{gid}")).json()
        check("after the restart the job is still waited on, not marked interrupted", row["status"] in ("running", "done") and row["video"]["remote_id"] == rid, row)
        done = await until(http, gid, ("done",))
        check("…it is collected: file, work, cost", done["artifacts"] and done["artifacts"][0]["exists"] and done["cost_usd"] == 0.0, done)
        check("…and it says it was picked up again after the restart", done["video"]["resumed"] and done["video"]["resumed"][0]["after_s"] >= 1, done["video"])
        work = done["artifacts"][0]
        check("without ffmpeg there is no poster", work["poster"] is False and work["thumb_url"] is None, work)
        th = await http.get(f"/api/artifacts/{work['id']}/thumb")
        check("…and the thumbnail says 'no poster' (204), not an error", th.status_code == 204 and th.headers.get("x-omniapi-poster") == "none", th.status_code)
        tools = (await http.get("/api/tools", params={"refresh": True})).json()
        ff = tools.get("ffmpeg") or next((t for t in (tools.get("tools") or []) if t.get("id") == "ffmpeg" or t.get("name") == "ffmpeg"), {})
        check("the outside-tools list says what ffmpeg is for, posters included", "封面" in json.dumps(ff, ensure_ascii=False), ff)
        backfill = (await http.post("/api/artifacts/backfill")).json()
        check("a backfill finds nothing new (every video is already a work)", backfill["added_total"] == 0, backfill)


# ------------------------------------------------------------------ 8. Gemini Omni direct
async def phase_omni(http: httpx.AsyncClient, *, google_key: bool) -> None:
    opts = (await http.get("/api/generate/options")).json()["kinds"]["video"]
    rows = {m["id"]: m for m in opts["models"]}
    omni = rows.get(OMNI) or {}
    check("Gemini Omni is listed, usable, from the google provider", omni.get("available") is True and omni.get("provider") == "google"
          and not omni.get("unavailable") and omni.get("implemented", True) is True, omni)
    vp = omni.get("video_params") or {}
    check("…with its own request shape", vp.get("durations") == list(range(3, 11)) and vp.get("resolutions") == ["360p", "720p", "1080p"]
          and vp.get("aspect_ratios") == ["16:9", "9:16"] and vp.get("audio") is True and vp.get("audio_fixed") is True, vp)
    if not google_key:
        check("without a Google key OpenRouter's Google rows are listed too", {OR_OMNI, "google/veo-3.1-lite"} <= set(rows), sorted(rows))
        return
    check("with the Google key set, OpenRouter's google/gemini-omni* and google/veo-* are not listed",
          not {OR_OMNI, "google/veo-3.1-lite"} & set(rows) and WAN in rows, sorted(rows))
    models = {m["id"] for m in (await http.get("/api/models", params={"modality": "video"})).json()["models"].get("video", [])}
    check("…nor on the models page's list", OMNI in models and not {OR_OMNI, "google/veo-3.1-lite"} & models, sorted(models))
    est = (await http.post("/api/generate/estimate", json={"kind": "video", "params": {"model": OMNI, "resolution": "720p", "duration": 5}})).json()
    check("the estimate: 720p is $0.10136 a second (5,792 tokens x $17.50 per 1M)", est["basis"] == "sandbox"
          and abs(est["listed"]["usd"] - 0.5068) < 1e-9 and est["listed"]["basis"] == "per_second", est)
    est = (await http.post("/api/generate/estimate", json={"kind": "video", "params": {"model": OMNI, "resolution": "1080p", "duration": 5}})).json()
    check("…1080p cannot be priced up front (Google states 720p's tokens only)", est["listed"]["basis"] == "per_token"
          and est["listed"]["usd"] is None, est)
    r = await http.post("/api/generations", json={"kind": "video", "params": {"prompt": "a lighthouse at dawn", "model": OMNI,
                                                                               "generate_audio": False}})
    check("sound cannot be switched off: refused before anything is sent", r.status_code == 400 and "always makes sound" in r.text, r.text)
    r = await http.post("/api/generations", json={"kind": "video", "params": {"prompt": "a lighthouse at dawn", "model": OMNI,
                                                                               "resolution": "720p", "duration": 3, "aspect_ratio": "9:16"}})
    check("an Omni video starts", r.status_code == 200 and r.json()["status"] == "running", r.text)
    done = await until(http, r.json()["id"], ("done",))
    work = done["artifacts"][0]
    check("…and lands on the wall as an Omni work", work["kind"] == "video" and work["model"] == OMNI, work)


async def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="omniapi-video-e2e-"))
    home = root / "home"
    home.mkdir()
    await build_v13_database(home)
    daemon = Daemon(home)
    daemon.start()
    try:
        await phase_upgrade()
        async with httpx.AsyncClient(base_url=BASE, timeout=30) as http:
            await phase_basic(http)
            await phase_first_frame(http)
        await phase_restart(daemon)
        daemon.kill()
        daemon.start()
        async with httpx.AsyncClient(base_url=BASE, timeout=30) as http:
            await phase_owner_actions(http)
        await phase_mcp_ticket(daemon)
        async with httpx.AsyncClient(base_url=BASE, timeout=30) as http:
            vids = (await http.get("/api/artifacts", params={"kind": "video", "limit": 100})).json()["items"]
            folder = {Path(v["file_path"]).parent.parent.name for v in vids}
            check("every video sits under videos/<date>/", folder == {"videos"}, folder)
            await phase_omni(http, google_key=False)
        daemon.stop()
        daemon.start(extra={"PROVIDERS__GEMINI__API_KEY": FAKE_GOOGLE_KEY, "PROVIDERS__GEMINI__ENABLED": "true"})
        async with httpx.AsyncClient(base_url=BASE, timeout=30) as http:
            await phase_omni(http, google_key=True)
    finally:
        daemon.stop()
    print(f"ALL PASS — home: {home}")
    shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
