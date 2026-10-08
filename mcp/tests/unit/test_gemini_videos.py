"""Gemini Omni direct (1.4-M5): the Interactions request shape, asking and
collecting, the paid-tier refusal in plain words, the fallback when Google
will not run it in the background, the cost from the usage, the routing in
the job runner, and the "not listed twice" rule. No request leaves: every
answer comes from an ``httpx.MockTransport``; the keys are made up."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from omniapi_mcp.capabilities.video import VideoProviderError, VideoRequest
from omniapi_mcp.catalog import catalog
from omniapi_mcp.catalog.catalog import ModelCatalog
from omniapi_mcp.catalog.discovery import DiscoveryResult
from omniapi_mcp.catalog.openrouter_videos import check_request, direct_video_twin, estimate, sandbox_listing
from omniapi_mcp.config.settings import VideoSettings
from omniapi_mcp.providers import gemini_videos as G
from omniapi_mcp.providers.gemini_videos import GeminiVideoProvider, api_base, cost_from_usage, usage_cost
from omniapi_mcp.store.db import Store
from omniapi_mcp.video.jobs import VideoError, VideoJobs
from omniapi_mcp.video.sandbox import SAMPLE_WITH_SOUND

OMNI = "gemini-omni-1.1-flash"
FAKE_KEY = "not-a-real-google-key-0000"
BASE = "https://gl.test/v1beta"
MP4 = SAMPLE_WITH_SOUND.read_bytes()
B64 = base64.b64encode(MP4).decode()
PRICING = {"video_output": 17.5, "text_output": 9, "media_input": 1.5}
PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()


def _done(iid: str = "v1_abc", *, usage: dict | None = None, video: dict | None = None) -> dict:
    content = [video if video is not None else {"type": "video", "mime_type": "video/mp4", "data": B64}]
    out = {"id": iid, "status": "completed", "model": OMNI, "object": "interaction",
           "steps": [{"type": "user_input", "content": [{"type": "text", "text": "x"}]},
                     {"type": "thought", "content": [{"type": "thought", "text": "planning"}]},
                     {"type": "model_output", "content": content}]}
    if usage is not None:
        out["usage"] = usage
    return out


USAGE = {"total_input_tokens": 12, "total_output_tokens": 28960, "total_thought_tokens": 100,
         "output_tokens_by_modality": [{"modality": "video", "tokens": 28960}]}


def _provider(handler, tmp_path: Path, **kw) -> GeminiVideoProvider:
    return GeminiVideoProvider(FAKE_KEY, BASE + "/", transport=httpx.MockTransport(handler), cache_dir=tmp_path / "cache",
                               pricing=PRICING, **kw)


# ------------------------------------------------------------------ the request
def test_the_body_follows_the_interactions_shape():
    plain = GeminiVideoProvider.body(VideoRequest(model=OMNI, prompt="a marble run", duration=5, resolution="720p", aspect_ratio="9:16"))
    assert plain == {"model": OMNI, "input": "a marble run", "store": True, "background": True,
                     "response_format": {"type": "video", "aspect_ratio": "9:16", "resolution": "720p", "duration": "5s"}}
    framed = GeminiVideoProvider.body(VideoRequest(model=OMNI, prompt="dusk falls", first_frame=PNG, last_frame=PNG))
    assert [b["type"] for b in framed["input"]] == ["image", "image", "text"]
    assert framed["input"][0]["mime_type"] == "image/png" and framed["input"][0]["data"] == PNG.split(",", 1)[1]
    assert framed["generation_config"] == {"video_config": {"task": "image_to_video"}}
    waiting = GeminiVideoProvider.body(VideoRequest(model=OMNI, prompt="x", resolution="1080p"), background=False)
    assert "background" not in waiting and waiting["response_format"]["delivery"] == "uri" and waiting["store"] is True


def test_the_api_root_is_found_from_the_settings_base_url():
    assert api_base(None) == "https://generativelanguage.googleapis.com/v1beta"
    assert api_base("https://generativelanguage.googleapis.com/v1beta/") == "https://generativelanguage.googleapis.com/v1beta"
    assert api_base("https://generativelanguage.googleapis.com/v1beta/openai/") == "https://generativelanguage.googleapis.com/v1beta"


def test_the_cost_of_the_first_real_omni_video():
    """2026-10-06, the owner's key: 3 s of 720p. Google's usage has no cost of its own; the formula gives the
    figure the owner checked ($0.309401: 17,376 video tokens, 390 other output + 197 thinking at the text rate)."""
    real = json.loads((Path(__file__).parents[1] / "fixtures" / "video" / "gemini_omni_usage_2026-10-06.json").read_text(encoding="utf-8"))
    pricing = catalog.get(OMNI, modality="video").pricing
    assert "cost" not in real["usage"]
    assert cost_from_usage(real["usage"], pricing) == pytest.approx(0.309401, abs=1e-6)
    assert usage_cost(real["usage"], pricing) == pytest.approx(0.309401, abs=1e-6)
    assert usage_cost({**real["usage"], "cost": 0.25}, pricing) == 0.25  # a cost in the usage is taken as it is


def test_the_cost_comes_from_the_usage():
    # 5 s of 720p = 28,960 video tokens at $17.50 per 1M; thinking at the text rate; input at $1.50
    usd = cost_from_usage(USAGE, PRICING)
    assert usd == pytest.approx((28960 * 17.5 + 100 * 9 + 12 * 1.5) / 1e6)
    assert cost_from_usage({"total_output_tokens": 5792}, PRICING) == pytest.approx(0.10136)  # no breakdown: all video
    assert cost_from_usage({}, PRICING) is None and cost_from_usage(USAGE, None) is None


# ------------------------------------------------------------------ send, ask, collect
@pytest.mark.asyncio
async def test_submit_status_and_download(tmp_path):
    seen: list[httpx.Request] = []
    polls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.method == "POST":
            return httpx.Response(200, json={"id": "v1_abc", "status": "in_progress", "model": OMNI, "object": "interaction"})
        polls["n"] += 1
        if polls["n"] == 1:
            return httpx.Response(200, json={"id": "v1_abc", "status": "in_progress", "model": OMNI})
        return httpx.Response(200, json=_done(usage=USAGE))

    p = _provider(handler, tmp_path, background=True)
    sub = await p.submit(VideoRequest(model=OMNI, prompt="a marble run", duration=5, resolution="720p"))
    assert (sub.remote_id, sub.status) == ("v1_abc", "pending")
    post = seen[0]
    assert str(post.url) == BASE + "/interactions" and post.headers["x-goog-api-key"] == FAKE_KEY
    assert FAKE_KEY not in str(post.url) and json.loads(post.content)["background"] is True
    first = await p.status("v1_abc")
    assert first.status == "in_progress" and not first.done and first.cost_usd is None
    done = await p.status("v1_abc")
    assert done.status == "completed" and done.outputs == 1 and done.cost_usd == pytest.approx(cost_from_usage(USAGE, PRICING))
    assert "<" in json.dumps(done.raw) and B64 not in json.dumps(done.raw)  # the job row never keeps the base64
    asked = len(seen)
    dest = tmp_path / "out" / "v.mp4"
    assert await p.download("v1_abc", dest) == len(MP4) and dest.read_bytes() == MP4
    assert len(seen) == asked  # the bytes came with the answer: no second request
    await p.close()


@pytest.mark.asyncio
async def test_a_restart_after_completion_still_has_the_file_and_its_cost(tmp_path):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_done(usage=USAGE))

    p = _provider(handler, tmp_path)
    await p.status("v1_abc")
    await p.close()
    calls = {"n": 0}

    def silent(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500)

    again = _provider(silent, tmp_path)  # a new process: the cache on disk answers
    state = await again.status("v1_abc")
    assert state.status == "completed" and state.cost_usd == pytest.approx(cost_from_usage(USAGE, PRICING)) and calls["n"] == 0
    await again.close()


@pytest.mark.asyncio
async def test_a_completed_answer_without_a_video_is_a_failure_in_the_models_words(tmp_path):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_done(video={"type": "text", "text": "I can't make that video."}))

    p = _provider(handler, tmp_path)
    state = await p.status("v1_x")
    assert state.status == "failed" and "I can't make that video" in (state.error or "")
    await p.close()


@pytest.mark.asyncio
async def test_failed_and_gone_jobs(tmp_path):
    answers = {"v1_f": httpx.Response(200, json={"id": "v1_f", "status": "failed", "errors": [{"code": "x", "message": "blocked by safety filters"}]}),
               "v1_gone": httpx.Response(404, json={"error": {"code": 404, "message": "Interaction not found", "status": "NOT_FOUND"}}),
               "v1_busy": httpx.Response(503, json={"error": {"code": 503, "message": "overloaded", "status": "UNAVAILABLE"}})}

    def handler(req: httpx.Request) -> httpx.Response:
        return answers[req.url.path.rsplit("/", 1)[-1]]

    p = _provider(handler, tmp_path)
    failed = await p.status("v1_f")
    assert failed.status == "failed" and "safety" in failed.error
    with pytest.raises(VideoProviderError) as gone:
        await p.status("v1_gone")
    assert gone.value.transient is False and gone.value.error_kind == "lost"
    with pytest.raises(VideoProviderError) as busy:
        await p.status("v1_busy")
    assert busy.value.transient is True
    await p.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body", [
    (429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                     "message": "Quota exceeded for metric: generate_content_free_tier_requests, limit: 0, model: gemini-omni-1.1-flash"}}),
    (400, {"error": {"code": 400, "status": "FAILED_PRECONDITION",
                     "message": "This model is only available on the paid tier. Please enable billing on your project."}}),
])
async def test_no_paid_tier_says_so_in_plain_words(tmp_path, status, body):
    p = _provider(lambda req: httpx.Response(status, json=body), tmp_path)
    with pytest.raises(VideoProviderError) as err:
        await p.submit(VideoRequest(model=OMNI, prompt="x"))
    e = err.value
    assert str(e.said).startswith("Gemini 影片要開付費層") and e.error_kind == "quota" and e.charged == "no"
    assert body["error"]["message"] in e.said  # Google's own words come along
    await p.close()


@pytest.mark.asyncio
async def test_other_refusals(tmp_path):
    cases = [(400, "API key not valid. Please pass a valid API key.", "INVALID_ARGUMENT", "auth"),
             (400, "The prompt was blocked by safety filters.", "INVALID_ARGUMENT", "rejected"),
             (400, "response_format.resolution: unsupported value", "INVALID_ARGUMENT", "invalid"),
             (500, "internal", "INTERNAL", "unavailable")]
    for status, message, word, kind in cases:
        p = _provider(lambda req, s=status, m=message, w=word: httpx.Response(s, json={"error": {"code": s, "message": m, "status": w}}), tmp_path)
        with pytest.raises(VideoProviderError) as err:
            await p.submit(VideoRequest(model=OMNI, prompt="x"))
        assert err.value.error_kind == kind and message in err.value.said
        assert err.value.charged == ("no" if status < 500 else "unknown")
        await p.close()


@pytest.mark.asyncio
async def test_background_refused_falls_back_to_waiting(tmp_path):
    bodies: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(500)  # never asked: the cache answers
        body = json.loads(req.content)
        bodies.append(body)
        if body.get("background"):
            return httpx.Response(400, json={"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                                       "message": "background is not supported for this model"}})
        return httpx.Response(200, json=_done("v1_sync", usage=USAGE))

    p = _provider(handler, tmp_path, background=True)
    sub = await p.submit(VideoRequest(model=OMNI, prompt="x", duration=3))
    assert [b.get("background") for b in bodies] == [True, None]
    assert sub.remote_id == "v1_sync" and sub.status == "completed" and sub.raw["waited_in_request"] is True
    state = await p.status("v1_sync")
    assert state.status == "completed" and state.cost_usd == pytest.approx(cost_from_usage(USAGE, PRICING))
    dest = tmp_path / "v.mp4"
    assert await p.download("v1_sync", dest) == len(MP4)
    await p.close()


@pytest.mark.asyncio
async def test_a_uri_video_waits_for_its_file_then_downloads_with_the_key(tmp_path):
    states = iter(["PROCESSING", "ACTIVE"])
    fetched: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/interactions/v1_u"):
            return httpx.Response(200, json=_done("v1_u", video={"type": "video", "mime_type": "video/mp4",
                                                                  "uri": BASE + "/files/abc-123:download?alt=media"}))
        if path.endswith("/files/abc-123"):
            return httpx.Response(200, json={"name": "files/abc-123", "state": next(states)})
        if path.endswith("/files/abc-123:download"):
            fetched.append(req)
            return httpx.Response(200, content=MP4)
        return httpx.Response(404)

    p = _provider(handler, tmp_path, file_poll_s=0.01)
    dest = tmp_path / "u.mp4"
    assert await p.download("v1_u", dest) == len(MP4)
    assert fetched and fetched[0].headers["x-goog-api-key"] == FAKE_KEY and FAKE_KEY not in str(fetched[0].url)
    await p.close()


@pytest.mark.asyncio
async def test_by_default_the_video_is_waited_for_and_never_asked_about(tmp_path, monkeypatch):
    """2026-10-06: Google answers every GET of a background Omni interaction with a 400, so the
    default is one POST that holds until the video is made — no GET at all."""
    monkeypatch.delenv("OMNIAPI_GEMINI_VIDEO_BACKGROUND", raising=False)
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=_done("v1_wait", usage=USAGE)) if req.method == "POST" else httpx.Response(400)

    p = _provider(handler, tmp_path)
    assert p.background is False and p.waits_in_submit is True
    sub = await p.submit(VideoRequest(model=OMNI, prompt="x", duration=3, resolution="720p"))
    assert sub.status == "completed" and "background" not in json.loads(seen[0].content)
    state = await p.status("v1_wait")
    assert state.status == "completed" and state.cost_usd == pytest.approx(cost_from_usage(USAGE, PRICING))
    assert await p.download("v1_wait", tmp_path / "w.mp4") == len(MP4)
    assert [r.method for r in seen] == ["POST"]
    monkeypatch.setenv("OMNIAPI_GEMINI_VIDEO_BACKGROUND", "1")
    assert _provider(handler, tmp_path).background is True
    await p.close()


@pytest.mark.asyncio
async def test_a_1080p_video_comes_as_a_file_fetched_without_asking(tmp_path):
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(f"{req.method} {req.url.path}")
        if req.method == "POST":
            assert json.loads(req.content)["response_format"]["delivery"] == "uri"
            return httpx.Response(200, json=_done("v1_hd", usage=USAGE, video={"type": "video", "mime_type": "video/mp4",
                                                                                "uri": BASE + "/files/hd-1:download?alt=media"}))
        if req.url.path.endswith("/files/hd-1"):
            return httpx.Response(200, json={"state": "ACTIVE"})
        if req.url.path.endswith("/files/hd-1:download"):
            return httpx.Response(200, content=MP4)
        return httpx.Response(400)

    p = _provider(handler, tmp_path, file_poll_s=0.01)
    await p.submit(VideoRequest(model=OMNI, prompt="x", resolution="1080p"))
    assert (await p.status("v1_hd")).status == "completed"
    assert await p.download("v1_hd", tmp_path / "hd.mp4") == len(MP4)
    assert not any("/interactions/" in x for x in seen)
    await p.close()


# ------------------------------------------------------------------ the catalog
def test_omni_is_wired_with_its_params_and_price():
    cat = ModelCatalog()
    omni = cat.get(OMNI, modality="video")
    assert omni.implemented and omni.video_route == "google" and "implemented" not in omni.to_dict()
    vp = omni.video_params
    assert vp["durations"] == list(range(3, 11)) and vp["resolutions"] == ["360p", "720p", "1080p"]
    assert vp["aspect_ratios"] == ["16:9", "9:16"] and vp["frames"] == ["first_frame", "last_frame"]
    assert vp["audio"] is True and vp["audio_fixed"] is True
    at = lambda res: estimate(omni.pricing, vp, duration=5, resolution=res, audio=None, first_frame=False, frames=0)  # noqa: E731
    assert at("720p")["usd"] == pytest.approx(5 * 5792 * 17.5 / 1e6) and at(None)["usd"] == at("720p")["usd"]
    assert at("1080p")["basis"] == "per_token" and at("1080p")["usd"] is None  # Google states 720p's tokens only
    assert check_request(vp, duration=5, audio=False) and "always makes sound" in check_request(vp, duration=5, audio=False)
    assert "together with a first frame" in check_request(vp, duration=5, last_frame=True)
    assert check_request(vp, duration=5, first_frame=True, last_frame=True) is None
    assert "Supported durations: 3-10" in check_request(vp, duration=12)


def test_openrouters_google_video_rows_are_hidden_while_the_google_key_is_set():
    assert direct_video_twin("google/gemini-omni-1.1-flash") == "google" and direct_video_twin("google/veo-3.1-lite") == "google"
    assert direct_video_twin("google/gemini-3.1-flash-image") is None and direct_video_twin("alibaba/wan-3.0") is None
    cat = ModelCatalog()
    cat._merge_videos(DiscoveryResult(provider="openrouter-videos", models=sandbox_listing(), fetched_at=0))
    without = {e.id for e in cat.models(modality="video")}
    assert {"google/gemini-omni-1.1-flash", "google/veo-3.1-lite", OMNI} <= without
    gemini = SimpleNamespace(enabled=True, api_key=FAKE_KEY)
    cat.set_direct_providers(SimpleNamespace(providers=SimpleNamespace(gemini=gemini)))
    with_key = {e.id for e in cat.models(modality="video")}
    assert OMNI in with_key and not {"google/gemini-omni-1.1-flash", "google/veo-3.1-lite"} & with_key
    assert "alibaba/wan-3.0" in with_key
    assert "google/veo-3.1-lite" in {e.id for e in cat.models(modality="video", include_duplicates=True)}
    gemini.enabled = False  # switched off: OpenRouter's rows come back
    cat.set_direct_providers(SimpleNamespace(providers=SimpleNamespace(gemini=gemini)))
    assert "google/gemini-omni-1.1-flash" in {e.id for e in cat.models(modality="video")}


# ------------------------------------------------------------------ the job runner
@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIAPI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OMNIAPI_DEV", "1")
    monkeypatch.delenv("OMNIAPI_OFFLINE", raising=False)
    monkeypatch.setenv("OMNIAPI_VIDEO_POLL", "0.05")
    monkeypatch.setattr(catalog, "_entries", dict(catalog._entries))
    store = Store(tmp_path / "omniapi.db")
    await store.open()
    gemini = SimpleNamespace(enabled=True, api_key=FAKE_KEY, base_url=BASE + "/")
    settings = SimpleNamespace(video=VideoSettings(), storage=SimpleNamespace(base_path=str(tmp_path / "works")),
                               providers=SimpleNamespace(gemini=gemini, openrouter=None),
                               defaults=SimpleNamespace(video=None), images=SimpleNamespace(default_model=None))
    from omniapi_mcp.bus import EventBus as Bus

    made: list[VideoJobs] = []
    vendor = SimpleNamespace(handler=None, posts=[])

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            vendor.posts.append(json.loads(req.content))
        return vendor.handler(req)

    real = G.GeminiVideoProvider
    monkeypatch.delenv("OMNIAPI_GEMINI_VIDEO_BACKGROUND", raising=False)
    monkeypatch.setattr(G, "GeminiVideoProvider", lambda key, base: real(key, base, transport=httpx.MockTransport(handler),
                                                                          cache_dir=tmp_path / "cache"))

    def new_jobs() -> VideoJobs:
        j = VideoJobs(store, Bus(), lambda: settings)
        made.append(j)
        return j

    yield SimpleNamespace(store=store, settings=settings, new=new_jobs, vendor=vendor, gemini=gemini, tmp=tmp_path,
                          monkeypatch=monkeypatch)
    for j in made:
        await j.close()
    await store.close()


async def _until(store: Store, gid: str, statuses: tuple[str, ...], timeout: float = 10.0) -> dict:
    t0 = time.monotonic()
    row: dict = {}
    while time.monotonic() - t0 < timeout:
        row = await store.generation(gid)
        if row["status"] in statuses:
            return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"{gid} never reached {statuses}: {row.get('status')} {row.get('error')}")


def _answers_with_the_video(usage: dict | None):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(200, json=_done("v1_job", usage=usage))
        return httpx.Response(400, json={"error": {"code": 400, "message": "Multiple authentication credentials received."}})

    return handler


@pytest.mark.asyncio
async def test_a_gemini_video_is_sent_watched_collected_and_billed_from_its_usage(env):
    env.vendor.handler = _answers_with_the_video(USAGE)
    jobs = env.new()
    assert jobs.default_model() == OMNI  # no OpenRouter key: the Google one makes Omni the usable default
    row = await jobs.start({"prompt": "a lighthouse at dawn", "model": OMNI, "resolution": "720p", "aspect_ratio": "9:16"}, source="gui")
    assert row["status"] == "running"  # the caller is not kept waiting while Google makes it
    assert row["estimate"]["basis"] == "per_second" and row["estimate"]["usd"] == pytest.approx(5 * 0.10136)
    done = await _until(env.store, row["id"], ("done",))
    sent = env.vendor.posts[0]
    assert sent["response_format"] == {"type": "video", "aspect_ratio": "9:16", "resolution": "720p", "duration": "5s"}
    assert "generate_audio" not in json.dumps(sent) and "background" not in sent
    assert done["provider"] == "google" and done["remote_id"] == "v1_job"
    expected = cost_from_usage(USAGE, catalog.get(OMNI, modality="video").pricing)
    assert done["cost_usd"] == pytest.approx(expected) and (done["remote"] or {}).get("charged") == "yes"
    work = (await env.store.artifacts_by_ids(done["artifact_ids"]))[0]
    assert work["provider"] == "google" and work["model"] == OMNI and Path(work["file_path"]).read_bytes() == MP4


@pytest.mark.asyncio
async def test_without_usage_the_estimate_is_billed_and_marked_likely(env):
    env.vendor.handler = _answers_with_the_video(None)
    jobs = env.new()
    row = await jobs.start({"prompt": "rain on a window", "model": OMNI, "duration": 3}, source="gui")
    done = await _until(env.store, row["id"], ("done",))
    assert done["cost_usd"] == pytest.approx(3 * 0.10136)
    assert done["remote"]["charged"] == "likely" and done["remote"]["cost_from"] == "estimate"


@pytest.mark.asyncio
async def test_requests_omni_cannot_take_are_refused_before_anything_is_sent(env):
    env.vendor.handler = lambda req: httpx.Response(500)
    jobs = env.new()
    with pytest.raises(VideoError, match="always makes sound"):
        await jobs.start({"prompt": "x", "model": OMNI, "generate_audio": False})
    with pytest.raises(VideoError, match="Supported durations: 3-10"):
        await jobs.start({"prompt": "x", "model": OMNI, "duration": 15})
    with pytest.raises(VideoError, match="Supported aspect ratios"):
        await jobs.start({"prompt": "x", "model": OMNI, "aspect_ratio": "1:1"})
    env.gemini.api_key = ""
    with pytest.raises(VideoError, match="Google key") as err:
        await jobs.start({"prompt": "x", "model": OMNI})
    assert err.value.status == 409
    assert env.vendor.posts == []


@pytest.mark.asyncio
async def test_no_paid_tier_ends_the_job_with_the_plain_message(env):
    env.vendor.handler = lambda req: httpx.Response(429, json={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                                                         "message": "free_tier limit: 0"}})
    jobs = env.new()
    row = await jobs.start({"prompt": "x", "model": OMNI}, source="gui")
    failed = await _until(env.store, row["id"], ("error",))
    assert failed["error"].startswith("Gemini 影片要開付費層") and failed["error_kind"] == "quota"
    assert (await jobs.public(row["id"]))["video"]["charged"] == "no"


@pytest.mark.asyncio
async def test_a_service_that_stops_while_google_makes_the_video_loses_it_and_says_so(env):
    release = asyncio.Event()

    async def slow(req: httpx.Request) -> httpx.Response:
        await release.wait()
        return httpx.Response(200, json=_done("v1_slow", usage=USAGE))

    env.vendor.handler = None
    jobs = env.new()
    jobs._google()._client._transport = httpx.MockTransport(slow)
    row = await jobs.start({"prompt": "x", "model": OMNI, "duration": 3}, source="gui")
    gid = row["id"]
    await asyncio.sleep(0.2)
    assert (await env.store.generation(gid))["remote_id"] is None
    # an old row this process is still sending is not taken for dead
    await env.store.db.execute("UPDATE generations SET created_at=created_at-600 WHERE id=?", (gid,))
    await env.store.db.commit()
    await jobs.resume()
    assert (await env.store.generation(gid))["status"] == "running"
    await jobs.close()  # the service stops while Google is still making it
    again = env.new()
    await again.resume()
    lost = await env.store.generation(gid)
    assert lost["status"] == "interrupted" and lost["error_kind"] == "lost"
    assert "probably made and billed" in lost["error"] and "cannot be collected" in lost["error"]
    assert (await again.public(gid))["video"]["charged"] == "likely"
    release.set()


# ------------------------------------------------------------------ credentials on the way (the 2026-10-06 live run)
@pytest.mark.asyncio
async def test_a_redirect_to_a_url_with_its_own_credential_goes_without_the_key(tmp_path):
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.host == "gl.test" and req.url.path.endswith("/interactions/v1_r"):
            return httpx.Response(302, headers={"location": "https://gl.test/v1beta/interactions/v1_r2"})
        if req.url.path.endswith("/interactions/v1_r2"):
            return httpx.Response(307, headers={"location": "https://storage.test/blob?X-Goog-Signature=abc&alt=media"})
        if req.url.host == "storage.test":
            if "x-goog-api-key" in req.headers:
                return httpx.Response(400, json={"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                                           "message": "Multiple authentication credentials received. Please pass only one."}})
            return httpx.Response(200, json=_done("v1_r", usage=USAGE))
        return httpx.Response(404)

    hops: list[dict] = []
    p = _provider(handler, tmp_path)
    p.trace = hops.append
    state = await p.status("v1_r")
    assert state.status == "completed"
    assert [("x-goog-api-key" in r.headers) for r in seen] == [True, True, False]  # same host keeps it, the signed URL does not
    assert all("content-type" not in r.headers for r in seen)  # a GET carries no body type
    text = json.dumps(hops)
    assert FAKE_KEY not in text and "abc" not in text and [h["status"] for h in hops] == [302, 307, 200]
    await p.close()


@pytest.mark.asyncio
async def test_a_4xx_while_asking_is_not_retried_forever(tmp_path):
    multi = {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                       "message": "Multiple authentication credentials received. Please pass only one."}}
    answers = {"v1_multi": httpx.Response(400, json=multi),
               "v1_slow": httpx.Response(429, json={"error": {"code": 429, "message": "slow down", "status": "RESOURCE_EXHAUSTED"}}),
               "v1_denied": httpx.Response(403, json={"error": {"code": 403, "message": "Permission denied", "status": "PERMISSION_DENIED"}})}
    p = _provider(lambda req: answers[req.url.path.rsplit("/", 1)[-1]], tmp_path)
    with pytest.raises(VideoProviderError) as multi_err:
        await p.status("v1_multi")
    assert multi_err.value.transient is False and multi_err.value.error_kind == "auth" and multi_err.value.status == 400
    with pytest.raises(VideoProviderError) as denied:
        await p.status("v1_denied")
    assert denied.value.transient is False and denied.value.error_kind == "auth"
    with pytest.raises(VideoProviderError) as slow:
        await p.status("v1_slow")
    assert slow.value.transient is True
    await p.close()


@pytest.mark.asyncio
async def test_the_runner_stops_asking_on_a_refusal_and_keeps_the_id(env):
    gets = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(200, json={"id": "v1_job", "status": "in_progress"})
        gets["n"] += 1
        if gets["n"] == 1:
            return httpx.Response(400, json={"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                                       "message": "Multiple authentication credentials received. Please pass only one."}})
        return httpx.Response(200, json=_done("v1_job", usage=USAGE))

    env.vendor.handler = handler
    env.monkeypatch.setenv("OMNIAPI_GEMINI_VIDEO_BACKGROUND", "1")  # the way that asks by id
    jobs = env.new()
    row = await jobs.start({"prompt": "x", "model": OMNI, "duration": 3}, source="gui")
    stopped = await _until(env.store, row["id"], ("gave_up",))
    assert gets["n"] == 1 and stopped["remote_id"] == "v1_job" and stopped["error_kind"] == "auth"
    assert "more than one credential" in stopped["error"] and "ask again" in stopped["error"]
    pub = await jobs.public(row["id"])
    assert pub["video"]["can_recheck"] is True and pub["video"]["charged"] == "likely"
    # fixed: "ask again" asks once more with the same id and collects it — nothing is sent again
    await jobs.recheck(row["id"])
    done = await _until(env.store, row["id"], ("done",))
    assert done["remote_id"] == "v1_job" and len(env.vendor.posts) == 1
