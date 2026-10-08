"""Video jobs (1.4-M3) against the sandbox's stand-in vendor, in-process:
the remote id is stored before any waiting, a restart picks the job up again,
"stop waiting" keeps collecting (or not), "waited too long" keeps the id and
"ask again" uses it, failures say what happened, the file's own facts are
indexed, the ledger gets the real cost, a 1.3 database upgrades in place,
and the MCP per-video limit."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio

from omniapi_mcp.bus import EventBus
from omniapi_mcp.catalog import catalog
from omniapi_mcp.catalog.discovery import DiscoveryResult
from omniapi_mcp.catalog.openrouter_videos import sandbox_listing
from omniapi_mcp.config.settings import VideoSettings
from omniapi_mcp.store.db import Store
from omniapi_mcp.video import jobs as J
from omniapi_mcp.video.jobs import VideoError, VideoJobs
from omniapi_mcp.video.media import probe, read_mp4

WAN = "alibaba/wan-3.0"
GROK = "x-ai/grok-imagine-video"
KLING = "kwaivgi/kling-v3.0-std"


@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIAPI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OMNIAPI_DEV", "1")
    monkeypatch.setenv("OMNIAPI_OFFLINE", "1")
    monkeypatch.setenv("OMNIAPI_VIDEO_POLL", "0.05")
    monkeypatch.setenv("OMNIAPI_FAKE_DELAY", "0.3")
    monkeypatch.setattr(catalog, "_entries", dict(catalog._entries))  # the roster below is this test's only
    catalog._merge_videos(DiscoveryResult(provider="openrouter-videos", models=sandbox_listing(), fetched_at=time.time()))
    store = Store(tmp_path / "omniapi.db")
    await store.open()
    settings = SimpleNamespace(video=VideoSettings(), storage=SimpleNamespace(base_path=str(tmp_path / "works")),
                               providers=SimpleNamespace(), defaults=SimpleNamespace(video=None), images=SimpleNamespace(default_model=None))
    bus = EventBus()
    made: list[VideoJobs] = []

    def new_jobs() -> VideoJobs:
        j = VideoJobs(store, bus, lambda: settings)
        made.append(j)
        return j

    yield SimpleNamespace(store=store, settings=settings, bus=bus, new=new_jobs, tmp=tmp_path)
    for j in made:
        await j.close()
    await store.close()


async def _until(store: Store, gid: str, statuses: tuple[str, ...], timeout: float = 10.0) -> dict:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        row = await store.generation(gid)
        if row["status"] in statuses:
            return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"{gid} never reached {statuses}: {row['status']}")


# ------------------------------------------------------------------ the happy path
@pytest.mark.asyncio
async def test_the_remote_id_is_stored_before_any_waiting_then_the_video_is_collected(env):
    jobs = env.new()
    row = await jobs.start({"prompt": "a paper boat on a pond", "model": WAN, "resolution": "480p"}, source="gui")
    stored = await env.store.generation(row["id"])
    # the moment start() returns, the vendor's job id and the send time are in the database
    assert stored["status"] == "running" and stored["remote_id"].startswith("sbx-") and stored["submitted_at"]
    assert stored["provider"] == "sandbox" and row["video"]["charged"] == "likely" and row["video"]["can_stop_waiting"]
    assert row["estimate"]["basis"] == "sandbox" and row["estimate"]["listed"]["usd"] == pytest.approx(0.25)
    assert row["params"]["duration"] == 5  # the default length is sent (and kept), so the bill and the estimate agree
    call = await env.store.call(stored["call_id"])
    assert call["status"] == "ticket" and call["tool"] == "generate_video"

    done = await _until(env.store, row["id"], ("done",))
    assert done["cost_usd"] == 0.0 and done["remote_status"] == "completed"
    work = (await env.store.artifacts_by_ids(done["artifact_ids"]))[0]
    assert work["kind"] == "video" and work["tool"] == "generate_video" and work["model"] == WAN
    assert (work["width"], work["height"], work["duration_s"]) == (256, 144, 2.0)  # from the file, not the request
    assert work["meta"]["has_audio"] is True and work["meta"]["requested"]["resolution"] == "480p"
    assert "\\videos\\" in work["file_path"] or "/videos/" in work["file_path"]
    assert read_mp4(work["file_path"])["has_audio"] is True
    call = await env.store.call(stored["call_id"])
    assert call["status"] == "ok" and call["cost_usd"] == 0.0
    pub = await jobs.public(row["id"])
    assert pub["artifacts"][0]["poster"] in (True, False) and "lease_owner" not in pub and "remote" not in pub


@pytest.mark.asyncio
async def test_a_first_frame_links_the_video_to_its_source_image(env):
    from PIL import Image

    img = env.tmp / "works" / "images" / "img_20261005120000_a.png"
    img.parent.mkdir(parents=True)
    Image.new("RGB", (64, 64), (200, 100, 0)).save(img)
    src = await env.store.add_artifact(kind="image", tool="generate_image", file_path=str(img.resolve()), mime="image/png")
    jobs = env.new()
    row = await jobs.start({"prompt": "it starts to rain", "model": GROK, "duration": 1, "resolution": "480p", "first_frame": src["id"]}, source="mcp")
    assert row["sources"]["frames"]["first"]["artifact_id"] == src["id"]
    assert row["estimate"]["listed"]["usd"] == pytest.approx(0.052)  # $0.05 for the second + $0.002 for the frame
    done = await _until(env.store, row["id"], ("done",))
    work = (await env.store.artifacts_by_ids(done["artifact_ids"]))[0]
    assert work["parent_id"] == src["id"] and work["meta"]["frames"]["first"]["artifact_id"] == src["id"]
    # a local path works as well (the MCP way), and a model without a last frame refuses one before anything is sent
    row2 = await jobs.start({"prompt": "x", "model": GROK, "duration": 1, "first_frame": str(img)}, source="mcp")
    assert row2["sources"]["frames"]["first"]["artifact_id"] == src["id"]
    with pytest.raises(VideoError, match="not a last frame"):
        await jobs.start({"prompt": "x", "model": GROK, "first_frame": src["id"], "last_frame": src["id"]})


@pytest.mark.asyncio
async def test_a_request_the_listing_rules_out_is_refused_before_a_job_exists(env):
    jobs = env.new()
    n = len(await env.store.generations(kind="video"))
    with pytest.raises(VideoError, match="Supported durations: 1-15"):
        await jobs.start({"prompt": "x", "model": GROK, "duration": 30})
    with pytest.raises(VideoError, match="closed by its maker since 2026-09-24") as e:
        jobs.check({"prompt": "x", "model": "openai/sora-2-pro"}, first=False, last=False)
    assert e.value.status == 410
    with pytest.raises(VideoError, match="not a text- or image-to-video model"):
        jobs.check({"prompt": "x", "model": "black-forest-labs/flux-video-edit"}, first=False, last=False)
    with pytest.raises(VideoError, match="without sound"):
        jobs.check({"prompt": "x", "model": "runway/gen-4.5", "generate_audio": True}, first=False, last=False)
    with pytest.raises(VideoError, match="not on OpenRouter's video roster"):
        jobs.check({"prompt": "x", "model": "no-such/model"}, first=False, last=False)
    _e, _req, warnings = jobs.check({"prompt": "x", "model": "google/veo-3.1-lite", "seed": 3}, first=False, last=False)
    assert any("2026-10-22" in w for w in warnings)
    assert len(await env.store.generations(kind="video")) == n


# ------------------------------------------------------------------ restarts
@pytest.mark.asyncio
async def test_a_restart_while_waiting_picks_the_same_job_up_again(env, monkeypatch):
    monkeypatch.setenv("OMNIAPI_FAKE_DELAY", "1.0")
    first = env.new()
    row = await first.start({"prompt": "slow tide", "model": WAN}, source="gui")
    await asyncio.sleep(0.2)
    await first.close()  # the service stops: nothing is marked; the row stays waiting
    stopped = await env.store.generation(row["id"])
    assert stopped["status"] == "running" and stopped["lease_owner"] is None
    # the startup sweep of the next process leaves a video with a remote id alone
    await env.store.interrupt_running_generations()
    assert (await env.store.generation(row["id"]))["status"] == "running"
    monkeypatch.setattr(J, "OWNER", "next-process")
    second = env.new()
    assert await second.resume() == [row["id"]]
    done = await _until(env.store, row["id"], ("done",))
    pub = await second.public(row["id"])
    assert done["artifact_ids"] and pub["video"]["resumed"] and pub["video"]["resumed"][0]["after_s"] >= 0.1


@pytest.mark.asyncio
async def test_a_job_another_live_process_watches_is_left_to_it(env, monkeypatch):
    monkeypatch.setenv("OMNIAPI_FAKE_DELAY", "30")
    jobs = env.new()
    row = await jobs.start({"prompt": "x", "model": WAN}, source="gui")
    await jobs.close()
    await env.store.claim_generation(row["id"], "someone-else", time.time() + 60)
    monkeypatch.setattr(J, "OWNER", "third")
    assert await env.new().resume() == []
    # its holder died (the lease ran out): taken over
    await env.store.db.execute("UPDATE generations SET lease_until=? WHERE id=?", (time.time() - 1, row["id"]))
    await env.store.db.commit()
    assert await env.new().resume() == [row["id"]]


@pytest.mark.asyncio
async def test_a_video_that_died_before_its_id_came_back_is_lost(env):
    await env.store.create_generation("v" * 16, kind="video", tool="generate_video", model=WAN, title="t", params={}, sources={},
                                      estimate={}, source="gui")
    await env.store.db.execute("UPDATE generations SET created_at=? WHERE id=?", (time.time() - 600, "v" * 16))
    await env.store.db.commit()
    await env.store.interrupt_running_generations()
    row = await env.store.generation("v" * 16)
    assert (row["status"], row["error_kind"]) == ("interrupted", "lost") and "billed" in row["error"]


# ------------------------------------------------------------------ stop waiting / waited too long / ask again
@pytest.mark.asyncio
async def test_stop_waiting_keeps_collecting_in_the_background(env, monkeypatch):
    monkeypatch.setenv("OMNIAPI_FAKE_DELAY", "0.8")
    jobs = env.new()
    row = await jobs.start({"prompt": "x", "model": WAN}, source="gui")
    out = await jobs.stop_waiting(row["id"])
    assert out["status"] == "detached" and out["video"]["detached_at"] and not out["video"]["can_stop_waiting"]
    done = await _until(env.store, row["id"], ("done",))
    pub = await jobs.public(row["id"])
    assert pub["video"]["late"] is True and done["cost_usd"] == 0.0
    work = (await env.store.artifacts_by_ids(done["artifact_ids"]))[0]
    assert work["meta"]["late"] is True


@pytest.mark.asyncio
async def test_stop_waiting_without_collecting_says_the_cost_is_unknown_and_ask_again_collects(env, monkeypatch):
    monkeypatch.setenv("OMNIAPI_FAKE_DELAY", "0.5")
    env.settings.video = VideoSettings(keep_collecting=False)
    jobs = env.new()
    row = await jobs.start({"prompt": "x", "model": WAN}, source="gui")
    out = await jobs.stop_waiting(row["id"])
    assert out["status"] == "abandoned" and out["video"]["can_recheck"] and "billed" in out["error"]
    call = await env.store.call(out["call_id"])
    assert call["status"] == "unsettled" and call["cost_usd"] is None and "not collected" in call["result"]["note"]
    await asyncio.sleep(0.8)
    assert (await env.store.generation(row["id"]))["status"] == "abandoned"  # nobody asked meanwhile
    again = await jobs.recheck(row["id"])
    assert again["status"] == "done" and again["artifacts"]


@pytest.mark.asyncio
async def test_waited_too_long_keeps_the_id_and_ask_again_does_not_resend(env):
    env.settings.video = VideoSettings(max_wait_minutes=0.05)  # 3 s
    jobs = env.new()
    row = await jobs.start({"prompt": "[never] a glacier", "model": WAN}, source="gui")
    gave = await _until(env.store, row["id"], ("gave_up",), timeout=8)
    assert gave["error_kind"] == "gave_up" and gave["remote_id"] == row["video"]["remote_id"]
    pub = await jobs.public(row["id"])
    assert pub["video"]["can_recheck"] and pub["video"]["polls"] >= 3
    again = await jobs.recheck(row["id"])
    assert again["status"] == "running" and again["video"]["remote_id"] == row["video"]["remote_id"]
    assert len(await env.store.generations(kind="video")) == 1  # nothing sent twice
    with pytest.raises(VideoError, match="only a video we stopped waiting on"):
        await jobs.recheck(row["id"])


# ------------------------------------------------------------------ failures
@pytest.mark.asyncio
async def test_a_failure_at_the_vendor_and_a_job_it_forgot(env):
    jobs = env.new()
    row = await jobs.start({"prompt": "[fail] x", "model": WAN}, source="gui")
    bad = await _until(env.store, row["id"], ("error",))
    assert "Content policy violation" in bad["error"] and bad["error_kind"] == "rejected"
    assert (await env.store.call(bad["call_id"]))["status"] == "error"
    row = await jobs.start({"prompt": "[gone] x", "model": WAN}, source="gui")
    gone = await _until(env.store, row["id"], ("error",))
    assert gone["error_kind"] == "lost" and "no longer knows" in gone["error"]


@pytest.mark.asyncio
async def test_a_refusal_at_sending_is_recorded_with_the_vendors_words(env, monkeypatch):
    from omniapi_mcp.capabilities.video import VideoProviderError
    from omniapi_mcp.video.sandbox import SandboxVideoProvider

    said = "Resolution 4K is not supported for this model. Supported resolutions: 480p, 720p"

    async def refuse(self, request):
        raise VideoProviderError(said, provider_name="sandbox", error_kind="invalid", said=said, charged="no", status=400)

    monkeypatch.setattr(SandboxVideoProvider, "submit", refuse)
    jobs = env.new()
    row = await jobs.start({"prompt": "x", "model": WAN}, source="gui")
    assert row["status"] == "error" and row["error"] == said and row["video"]["charged"] == "no"
    assert (await env.store.call(row["call_id"]))["status"] == "error"


# ------------------------------------------------------------------ the file, the database, the MCP limit
def test_the_file_is_read_without_ffmpeg():
    from pathlib import Path

    here = Path(__file__).resolve().parents[1]
    cover = here / "fixtures" / "video" / "cover_track.mp4"
    facts = read_mp4(cover)
    # the cover picture (96x96 MJPEG, a second "video" track) is not the video
    assert (facts["width"], facts["height"], facts["duration_s"], facts["has_audio"]) == (256, 144, 2.0, True)
    silent = here.parent / "omniapi_mcp" / "video" / "sandbox" / "sample_v.mp4"
    assert read_mp4(silent)["has_audio"] is False and read_mp4(silent)["height"] == 256
    real = here.parents[1] / "prototypes" / "openrouter-media-probe" / "video-wan-3.0-480p-5s.mp4"
    if real.is_file():  # the real Wan 3.0 file of 2026-10-05: 854x480, 30 fps, with sound
        f = read_mp4(real)
        assert (f["width"], f["height"], f["fps"], f["has_audio"]) == (854, 480, 30.0, True) and abs(f["duration_s"] - 5.04) < 0.01
    junk = here / "fixtures" / "video" / "not_a_video.mp4"
    junk.write_bytes(b"not an mp4 at all")
    try:
        assert read_mp4(junk) is None and probe(junk)["source"] in ("none", "ffprobe")
    finally:
        junk.unlink()


V13_GENERATIONS = """
CREATE TABLE generations (
  id TEXT PRIMARY KEY, created_at REAL NOT NULL, finished_at REAL, kind TEXT NOT NULL, tool TEXT NOT NULL, model TEXT,
  title TEXT, params_json TEXT, sources_json TEXT, status TEXT NOT NULL, error TEXT, error_kind TEXT, estimate_json TEXT,
  cost_usd REAL, call_id TEXT, artifact_ids_json TEXT, source TEXT, meta_json TEXT
);
INSERT INTO generations (id, created_at, finished_at, kind, tool, model, title, params_json, sources_json, status, cost_usd, artifact_ids_json, source)
VALUES ('old1', 1759000000, 1759000010, 'image', 'generate_image', 'gpt-image-2', '舊的一張', '{"prompt": "x"}', '{}', 'done', 0.04, '["a1"]', 'gui');
PRAGMA user_version=1;
"""


@pytest.mark.asyncio
async def test_a_13_database_upgrades_in_place_and_keeps_its_rows(tmp_path):
    path = tmp_path / "omniapi.db"
    con = sqlite3.connect(path)
    con.executescript(V13_GENERATIONS)
    con.commit()
    con.close()
    s = Store(path)
    await s.open()
    try:
        cur = await s.db.execute("PRAGMA table_info(generations)")
        cols = {r[1] for r in await cur.fetchall()}
        assert {"provider", "remote_id", "submitted_at", "remote_status", "polled_at", "lease_owner", "lease_until", "remote_json"} <= cols
        old = await s.generation("old1")
        assert (old["title"], old["status"], old["cost_usd"], old["artifact_ids"], old["remote_id"]) == ("舊的一張", "done", 0.04, ["a1"], None)
    finally:
        await s.close()
    s2 = Store(path)  # opening again must not add the columns twice
    await s2.open()
    await s2.close()


def test_the_mcp_limit():
    from omniapi_mcp.server import _video_limit

    req = SimpleNamespace(model=WAN, duration=5, resolution="480p")
    assert _video_limit({"basis": "per_second", "usd": 0.25}, None, 1.0, req) is None
    over = _video_limit({"basis": "per_second", "usd": 2.4}, None, 1.0, req)
    assert over["status"] == "refused" and over["reason"] == "over_limit" and over["limit_usd"] == 1.0
    assert "max_cost_usd=2.4 " in over["message"] and "$2.4" in over["message"]
    assert _video_limit({"basis": "per_second", "usd": 2.4}, 3.0, 1.0, req) is None
    assert "max_cost_usd=0.53 " in _video_limit({"basis": "per_second", "usd": 0.521}, None, 0.5, req)["message"]
    assert _video_limit({"basis": "range", "usd": None, "low": 0.5, "high": 1.2}, None, 1.0, req)["reason"] == "over_limit"
    tok = _video_limit({"basis": "per_token", "usd": None}, None, 1.0, req)
    assert tok["reason"] == "price_unknown" and "per token" in tok["message"] and "max_cost_usd" in tok["message"]
    assert _video_limit({"basis": "per_token", "usd": None}, 0.5, 1.0, req) is None  # the caller named an amount
    assert _video_limit({"basis": "per_second", "usd": 50.0}, None, None, req) is None  # no limit set
    sandbox = {"basis": "sandbox", "usd": 0.0, "listed": {"basis": "per_second", "usd": 1.6}}
    assert _video_limit(sandbox, None, 1.0, req)["reason"] == "over_limit"  # the sandbox checks what it would cost
