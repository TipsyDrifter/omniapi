"""GUI generation jobs (v1.1, 1.1-M3): failure kinds, cost estimates, which
models can be called, request checking, and the waiting behaviour the jobs
rely on. The full path through a daemon is ``tests/e2e/generate_e2e.py``."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import pytest

from omniapi_mcp.bus import EventBus
from omniapi_mcp.core.job_manager import JobManager, wait_to_finish
from omniapi_mcp.generate import GenerationError, GenerationManager, classify
from omniapi_mcp.generate.estimate import estimate
from omniapi_mcp.generate.manager import tool_for
from omniapi_mcp.generate.options import models_for
from omniapi_mcp.recorder import CallScope, call_scope, make_recorded
from omniapi_mcp.store.db import Store


@pytest.fixture
async def store(tmp_path):
    s = Store(tmp_path / "g.db")
    await s.open()
    yield s
    await s.close()


@pytest.fixture
def online(monkeypatch):
    monkeypatch.delenv("OMNIAPI_OFFLINE", raising=False)
    monkeypatch.delenv("OMNIAPI_DEV", raising=False)


# --------------------------------------------------------------------------- failure kinds


@pytest.mark.parametrize(
    "message, kind",
    [
        ("Error code: 429 - {'error': {'code': 'insufficient_quota', 'message': 'You exceeded your current quota'}}", "quota"),
        ("429 RESOURCE_EXHAUSTED. Quota exceeded for metric generate_content", "quota"),
        ("Error code: 401 - Incorrect API key provided: sk-***", "auth"),
        ("Error code: 400 - Your request was rejected as a result of our safety system.", "rejected"),
        ("ProviderError: content blocked by moderation", "rejected"),
        ("ReadTimeout: The read operation timed out", "timeout"),
        ("Error code: 413 - Maximum content size limit (26214400) exceeded", "too_large"),
        ("RuntimeError: No music provider for model 'lyria-3.5'. Available models: ['V6']", "unavailable"),
        ("This daemon is an offline sandbox (OMNIAPI_OFFLINE=1): the 'edit_music' tool would call a real vendor", "offline"),
        ("ValueError: Provide either image_data or image_path", "other"),
        ("ElevenLabs TTS returned HTTP 422: voice_id must be a valid id", "invalid"),
    ],
)
def test_failure_kinds(message, kind):
    assert classify(message) == kind


def test_a_timeout_exception_is_a_timeout_whatever_it_says():
    assert classify(asyncio.TimeoutError()) == "timeout"


# --------------------------------------------------------------------------- estimates


async def test_flat_prices_are_a_multiplication(store, online):
    est = await estimate(store, kind="image", tool="generate_image", params={"model": "gemini-3-pro-image", "n": 2, "image_size": "4K"})
    assert est["basis"] == "per_image" and est["usd"] == pytest.approx(0.48) and est["tier"] == "4K"
    est = await estimate(store, kind="speech", tool="generate_speech", params={"model": "eleven_flash_v2_5", "text": "字" * 500})
    assert est["basis"] == "per_1k_chars" and est["usd"] == pytest.approx(0.025)
    est = await estimate(store, kind="music", tool="generate_music", params={"model": "music_v2", "music_length_ms": 150000})
    assert est["basis"] == "per_minute" and est["usd"] == pytest.approx(0.375)
    est = await estimate(store, kind="transcript", tool="transcribe_audio", params={"model": "gpt-transcribe"}, duration_s=18.3)
    assert est["basis"] == "per_minute" and est["usd"] == pytest.approx(0.0045 * 18.3 / 60, abs=1e-6)


async def test_what_cannot_be_computed_is_not_guessed(store, online):
    est = await estimate(store, kind="music", tool="generate_music", params={"model": "V6"})
    assert est["basis"] == "credits" and est["usd"] is None
    est = await estimate(store, kind="music", tool="generate_music", params={"model": "music_v2"})
    assert est["basis"] == "needs_length" and est["usd"] is None
    est = await estimate(store, kind="transcript", tool="transcribe_audio", params={"model": "gpt-transcribe"})
    assert est["basis"] == "needs_length"
    est = await estimate(store, kind="image", tool="generate_image", params={"model": "gpt-image-2"})
    assert est["basis"] == "unknown" and est["usd"] is None  # token-priced, nothing generated yet


async def test_token_priced_images_are_estimated_from_what_they_cost_before(store, online):
    for i, (quality, cost) in enumerate([("low", 0.006), ("low", 0.008), ("high", 0.21)]):
        await store.add_artifact(kind="image", tool="generate_image", model="gpt-image-2", file_path=f"/x/{i}.png",
                                 cost_usd=cost, params={"quality": quality, "size": "1024x1024"})
    est = await estimate(store, kind="image", tool="generate_image", params={"model": "gpt-image-2", "quality": "low", "size": "1024x1024", "n": 2})
    assert est["basis"] == "history" and est["samples"] == 2
    assert est["usd"] == pytest.approx(0.014) and est["low"] == pytest.approx(0.012) and est["high"] == pytest.approx(0.016)
    # an edit borrows the history of fresh images of the same model
    est = await estimate(store, kind="image", tool="edit_image", params={"model": "gpt-image-2", "quality": "high", "size": "1024x1024"})
    assert est["basis"] == "history" and est["usd"] == pytest.approx(0.21)
    # settings never seen before: the model's own average, and it says so
    est = await estimate(store, kind="image", tool="generate_image", params={"model": "gpt-image-2", "quality": "medium", "size": "1536x1024"})
    assert est["basis"] == "history_model" and est["samples"] == 3


async def test_the_sandbox_charges_nothing(store, monkeypatch):
    monkeypatch.setenv("OMNIAPI_OFFLINE", "1")
    assert await estimate(store, kind="image", tool="generate_image", params={"model": "gpt-image-2"}) == {"basis": "sandbox", "usd": 0.0}


# --------------------------------------------------------------------------- which models can be called


def _ctx(*, image=(), speech=(), music=(), transcribe=None, keys=()):
    async def ensure():
        return None

    providers = NS(**{slot: NS(enabled=True, api_key="k") if slot in keys else None for slot in ("openai", "gemini", "elevenlabs", "kie")})
    return NS(
        settings=NS(providers=providers, images=NS(default_model="gpt-image-2")),
        image_generation_tool=NS(ensure_providers_registered=ensure, provider_registry=NS(get_supported_models=lambda: set(image))),
        speech_tool=NS(available_models=lambda: list(speech), _default_model=lambda: None),
        music_generation_tool=NS(available_models=lambda: list(music), _default_model=lambda: None),
        transcription_tool=NS(_provider=NS(get_supported_models=lambda: set(transcribe)) if transcribe else None),
    )


async def test_a_model_is_usable_only_when_its_tool_can_route_it(online):
    ctx = _ctx(music={"V6", "V6_MINI"}, keys={"kie", "gemini"})
    by_id = {m["id"]: m for m in await models_for(ctx, "music")}
    assert by_id["V6"]["available"] is True
    # key missing: says which one to set
    assert by_id["music_v2"]["unavailable"] == {"reason": "missing_key", "env": "PROVIDERS__ELEVENLABS__API_KEY"}
    # the key is there but no tool generates with it yet
    assert by_id["lyria-3.5"]["unavailable"] == {"reason": "not_implemented"}


async def test_an_offline_daemon_offers_everything_only_as_a_dev_sandbox(monkeypatch):
    monkeypatch.setenv("OMNIAPI_OFFLINE", "1")
    monkeypatch.delenv("OMNIAPI_DEV", raising=False)
    models = await models_for(_ctx(), "speech")
    assert models and all(m["unavailable"] == {"reason": "offline"} for m in models)
    monkeypatch.setenv("OMNIAPI_DEV", "1")
    assert all(m["available"] for m in await models_for(_ctx(), "speech"))


# --------------------------------------------------------------------------- requests


def test_an_image_request_with_a_source_is_an_edit():
    assert tool_for("image", {}) == "generate_image"
    assert tool_for("image", {"images": [{"artifact_id": "a"}]}) == "edit_image"
    assert tool_for("transcript", {"audio": {"upload_id": "u"}}) == "transcribe_audio"


async def test_requests_are_checked_before_a_job_exists(store, tmp_path):
    m = GenerationManager(store, EventBus())
    with pytest.raises(GenerationError, match="does not take"):
        await m.prepare("image", {"prompt": "x", "image_path": "C:/Windows/win.ini"}, None)
    with pytest.raises(GenerationError, match="invalid request"):
        await m.prepare("speech", {"text": "x", "speed": 9}, None)
    with pytest.raises(GenerationError, match="needs an audio source"):
        await m.prepare("transcript", {}, {})
    with pytest.raises(GenerationError, match="takes no source"):
        await m.prepare("music", {"prompt": "x"}, {"audio": {"upload_id": "u"}})

    image = tmp_path / "a.png"
    image.write_bytes(b"x")
    work = await store.add_artifact(kind="image", tool="generate_image", file_path=str(image))
    with pytest.raises(GenerationError, match="cannot be used here"):
        await m.prepare("transcript", {}, {"audio": {"artifact_id": work["id"]}})
    tool, args, sources = await m.prepare("image", {"prompt": "night", "quality": ""}, {"images": [{"artifact_id": work["id"], "file_path": "ignored"}]})
    assert tool == "edit_image" and args == {"prompt": "night", "image_path": str(image)}
    assert sources == {"images": [{"artifact_id": work["id"]}]}  # ids only: nothing else from the request is kept
    image.unlink()
    with pytest.raises(GenerationError, match="no longer on disk") as gone:
        await m.prepare("image", {"prompt": "night"}, {"images": [{"artifact_id": work["id"]}]})
    assert gone.value.status == 410


async def test_jobs_left_running_by_a_dead_process_are_marked_interrupted(store):
    await store.create_generation("g1", kind="music", tool="generate_music", model="V6", title="t", params={"prompt": "p"}, sources={}, estimate=None, source="gui")
    await store.create_generation("g2", kind="image", tool="generate_image", model=None, title="t", params={}, sources={}, estimate=None, source="gui")
    await store.update_generation("g2", status="done", artifact_ids=["a"])
    assert await store.interrupt_running_generations() == 1
    g1, g2 = await store.generation("g1"), await store.generation("g2")
    assert (g1["status"], g1["error_kind"]) == ("interrupted", "interrupted") and g1["finished_at"]
    assert g2["status"] == "done" and g2["artifact_ids"] == ["a"]


# --------------------------------------------------------------------------- waiting


async def test_a_gui_job_waits_past_the_ticket_window_and_takes_the_work_with_it_when_cancelled():
    jobs = JobManager(soft_timeout=0.05)

    async def slow():
        await asyncio.sleep(0.2)
        return {"ok": True}

    assert (await jobs.run("x", slow()))["status"] == "running"  # an MCP caller gets a ticket

    token = wait_to_finish.set(True)
    try:
        assert await jobs.run("x", slow()) == {"ok": True}  # a GUI job gets the result

        finished = []

        async def never():
            try:
                await asyncio.sleep(30)
            finally:
                finished.append("stopped")

        waiter = asyncio.create_task(jobs.run("y", never()))
        await asyncio.sleep(0.05)
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        await asyncio.sleep(0.05)
        assert finished == ["stopped"]  # the generation did not keep running behind the cancelled job
    finally:
        wait_to_finish.reset(token)
        await jobs.close()


async def test_a_job_cancelled_before_its_first_step_is_still_recorded_as_cancelled(store, monkeypatch):
    # start() returns before the job's task has run at all; a cancel landing in that
    # window used to skip _run entirely and leave the row 'running' forever
    monkeypatch.setenv("OMNIAPI_OFFLINE", "1")
    m = GenerationManager(store, EventBus())
    ended: list[tuple[str, bool]] = []

    async def on_finished(public, shutting_down):
        ended.append((public["status"], shutting_down))

    g = await m.start("speech", {"text": "hello"}, on_finished=on_finished)
    m._tasks[g["id"]].cancel()  # no await in between: the task has not stepped yet
    row = await m.cancel(g["id"])
    assert row["status"] == "cancelled" and row["finished_at"]
    assert ended == [("cancelled", False)]
    assert m.live_count == 0 and not m._unstarted and not m._settling


async def test_a_shutdown_right_after_a_start_records_the_job_as_cancelled(store, monkeypatch):
    monkeypatch.setenv("OMNIAPI_OFFLINE", "1")
    m = GenerationManager(store, EventBus())
    ended: list[tuple[str, bool]] = []

    async def on_finished(public, shutting_down):
        ended.append((public["status"], shutting_down))

    g = await m.start("speech", {"text": "hello"}, on_finished=on_finished)
    await m.close()
    assert (await store.generation(g["id"]))["status"] == "cancelled"
    assert ended == [("cancelled", True)]  # the shutdown's doing, not somebody's cancel


async def test_a_scoped_call_is_recorded_under_the_callers_door_and_reports_back(store):
    ctx = NS(store=store, bus=EventBus())
    recorded = make_recorded(lambda: ctx, source="mcp")

    @recorded
    async def health_check():
        return {"ok": True}

    scope = CallScope(source="gui")
    token = call_scope.set(scope)
    try:
        await health_check()
    finally:
        call_scope.reset(token)
    await health_check()
    calls = await store.calls(limit=5)
    assert [c["source"] for c in calls] == ["mcp", "gui"]
    assert scope.call_id == calls[1]["id"]
