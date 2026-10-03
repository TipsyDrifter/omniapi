"""The works library (v1.1, 1.1-M1): index, backfill, stand-in generators,
file endpoints and uploads."""

from __future__ import annotations

import asyncio
import io
import json
import wave
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from PIL import Image

from omniapi_mcp.artifacts import CallInfo, backfill, current_call, index_result
from omniapi_mcp.bus import EventBus
from omniapi_mcp.core.job_manager import JobManager
from omniapi_mcp.recorder import make_recorded
from omniapi_mcp.storage.manager import ImageStorageManager
from omniapi_mcp.store.db import Store


def _png(size=(64, 48)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 30, 90)).save(buf, format="PNG")
    return buf.getvalue()


def _wav(seconds=1.0, rate=8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


@pytest.fixture
async def ctx(tmp_path):
    store = Store(tmp_path / "a.db")
    await store.open()
    base = tmp_path / "storage"
    storage = NS(base_path=str(base), retention_days=30, max_size_gb=10, cleanup_interval_hours=24,
                 create_subdirectories=True, file_permissions="644", cleanup_enabled=False)
    c = NS(store=store, bus=EventBus(), settings=NS(storage=storage), storage_manager=ImageStorageManager(storage), jobs=JobManager())
    await c.storage_manager.initialize()
    yield c
    await c.jobs.close()
    await store.close()


# --------------------------------------------------------------------------- store


async def test_store_artifacts_filter_page_hide_and_unique_path(ctx):
    s = ctx.store
    a = await s.add_artifact(kind="image", file_path="C:/x/a.png", prompt="a red cat", model="m1", source="mcp", created_at=100, params={"size": "1024"})
    b = await s.add_artifact(kind="music", file_path="C:/x/b.mp3", title="夜曲", model="m2", source="gui", created_at=200)
    c = await s.add_artifact(kind="transcript", file_path="C:/x/c.txt", text="hello there", source="mcp", created_at=300)
    assert await s.add_artifact(kind="image", file_path="C:/x/a.png") is None  # same file is indexed once
    assert a["params"] == {"size": "1024"} and a["hidden"] == 0
    assert [r["id"] for r in await s.artifacts()] == [c["id"], b["id"], a["id"]]
    assert [r["id"] for r in await s.artifacts(kind="image")] == [a["id"]]
    assert [r["id"] for r in await s.artifacts(q="cat")] == [a["id"]]
    assert [r["id"] for r in await s.artifacts(q="夜曲")] == [b["id"]]
    assert [r["id"] for r in await s.artifacts(q="hello")] == [c["id"]]
    assert [r["id"] for r in await s.artifacts(before=300, limit=1)] == [b["id"]]
    assert [r["id"] for r in await s.artifacts(source="gui")] == [b["id"]]
    await s.set_artifact_hidden(b["id"], True)
    assert b["id"] not in [r["id"] for r in await s.artifacts()]
    assert b["id"] in [r["id"] for r in await s.artifacts(include_hidden=True)]
    assert await s.artifact_counts() == {"image": 1, "transcript": 1}
    with pytest.raises(ValueError):
        await s.add_artifact(kind="image", file_path="C:/x/d.png", nonsense=1)


# --------------------------------------------------------------------------- indexing tool results


async def test_index_image_result_with_several_images_splits_cost_and_announces(ctx):
    ids = []
    for _ in range(2):
        image_id, _path = await ctx.storage_manager.save_image(_png(), {"prompt": "p"}, "png")
        ids.append(image_id)
        await asyncio.sleep(0)  # ids carry a random part; no need to wait a second
    q = ctx.bus.subscribe()
    result = {"image_id": ids[0], "images": [{"image_id": i} for i in ids],
              "metadata": {"model": "gpt-image-2", "provider": "openai", "cost_estimate": 0.08}}
    call = CallInfo(tool="generate_image", args={"prompt": "two cats", "size": "1024x1024", "image_data": "x" * 999}, call_id="c1", source="mcp")
    rows = await index_result(ctx, call, result)
    assert len(rows) == 2 and {r["kind"] for r in rows} == {"image"}
    r = rows[0]
    assert (r["prompt"], r["model"], r["provider"], r["cost_usd"], r["call_id"], r["source"]) == ("two cats", "gpt-image-2", "openai", 0.04, "c1", "mcp")
    assert (r["width"], r["height"], r["mime"]) == (64, 48, "image/png") and r["bytes"] > 0
    assert r["params"] == {"size": "1024x1024"}  # the prompt has its own column; blobs are never stored
    events = [q.get_nowait() for _ in range(2)]
    assert all(e["type"] == "artifact.created" and e["artifact"]["file_url"].endswith("/file") for e in events)
    assert await index_result(ctx, call, result) == []  # indexing twice adds nothing


async def test_index_speech_music_stems_lyrics_and_transcript(ctx, tmp_path):
    base = Path(ctx.settings.storage.base_path)
    (base / "audio").mkdir(parents=True, exist_ok=True)
    wav = base / "audio" / "speech_x.wav"
    wav.write_bytes(_wav(1.5))
    rows = await index_result(ctx, CallInfo("generate_speech", {"text": "你好", "voice": "v"}), {"audio_path": str(wav), "model": "tts", "provider": "elevenlabs", "voice_id": "v"})
    assert rows[0]["kind"] == "speech" and rows[0]["prompt"] == "你好" and rows[0]["duration_s"] == 1.5 and rows[0]["meta"]["voice_id"] == "v"

    mp3 = base / "audio" / "song.mp3"
    mp3.write_bytes(b"ID3")
    rows = await index_result(ctx, CallInfo("generate_music", {"prompt": "lofi"}), {"audio_path": str(mp3), "title": "Night", "duration": 120, "cost_usd": 0.1})
    assert (rows[0]["kind"], rows[0]["title"], rows[0]["duration_s"], rows[0]["cost_usd"]) == ("music", "Night", 120.0, 0.1)

    s1, s2 = base / "audio" / "stem_vocals.mp3", base / "audio" / "stem_inst.mp3"
    s1.write_bytes(b"1"); s2.write_bytes(b"2")
    rows = await index_result(ctx, CallInfo("music_utility", {"action": "separate_vocals"}), {"stems": {"vocals": str(s1), "instrumental": str(s2)}})
    assert sorted(r["meta"]["stem"] for r in rows) == ["instrumental", "vocals"]

    ly = base / "audio" / "lyrics.txt"
    ly.write_text("la la", encoding="utf-8")
    rows = await index_result(ctx, CallInfo("music_lyrics", {"prompt": "sea"}), {"text": "la la", "lyrics_path": str(ly)})
    assert rows[0]["kind"] == "lyrics" and rows[0]["text"] == "la la"

    long_text = "字" * 5000
    rows = await index_result(ctx, CallInfo("transcribe_audio", {"audio_path": "C:/rec/meeting.m4a", "response_format": "srt"}), {"text": long_text, "model": "gpt-transcribe"})
    t = rows[0]
    assert t["kind"] == "transcript" and t["text"] == long_text and t["file_path"].endswith(".srt")
    assert Path(t["file_path"]).read_text(encoding="utf-8") == long_text  # the full text is on disk, not a ledger summary
    assert t["params"]["audio_path"] == "C:/rec/meeting.m4a"


async def test_index_ignores_tickets_errors_and_other_tools(ctx):
    assert await index_result(ctx, CallInfo("generate_image"), {"status": "running", "task_id": "job_1"}) == []
    assert await index_result(ctx, CallInfo("generate_image"), {"error": "boom"}) == []
    assert await index_result(ctx, CallInfo("complete_text"), {"text": "hi"}) == []
    assert await index_result(ctx, None, {"audio_path": "x"}) == []
    assert await index_result(ctx, CallInfo("generate_speech"), {"audio_path": "C:/nope/missing.mp3"}) == []


async def test_edit_links_to_the_work_it_started_from(ctx):
    src_id, src_path = await ctx.storage_manager.save_image(_png(), {}, "png")
    (parent,) = await index_result(ctx, CallInfo("generate_image", {"prompt": "a"}), {"image_id": src_id})
    out_id, _ = await ctx.storage_manager.save_image(_png((32, 32)), {}, "png")
    (child,) = await index_result(ctx, CallInfo("edit_image", {"prompt": "make it blue", "image_path": str(src_path)}), {"image_id": out_id, "operation": "edit"})
    assert child["parent_id"] == parent["id"]


# --------------------------------------------------------------------------- recorder + job manager


async def test_recorder_indexes_an_inline_result(ctx):
    recorded = make_recorded(lambda: ctx)
    wav = Path(ctx.settings.storage.base_path) / "s.wav"
    wav.write_bytes(_wav())

    @recorded
    async def generate_speech(text: str = "") -> dict:
        return {"audio_path": str(wav), "model": "tts-1"}

    await generate_speech(text="hello")
    (row,) = await ctx.store.artifacts()
    (call,) = await ctx.store.calls(tool="generate_speech")
    assert row["call_id"] == call["id"] and row["prompt"] == "hello"
    assert current_call.get() is None  # nothing leaks out of the call


async def test_a_ticketed_job_is_indexed_when_it_lands(ctx):
    """The call returns a ticket after the soft timeout; the generation ends
    later and nobody may ever fetch it — the work still has to reach the index."""
    wav = Path(ctx.settings.storage.base_path) / "late.wav"
    release = asyncio.Event()

    async def _late(result):
        await index_result(ctx, current_call.get(), result)

    ctx.jobs.on_late_result = _late
    recorded = make_recorded(lambda: ctx)

    async def slow():
        await release.wait()
        wav.write_bytes(_wav())
        return {"audio_path": str(wav), "model": "suno", "title": "Late"}

    @recorded
    async def generate_music(prompt: str = "") -> dict:
        return await ctx.jobs.run("generate_music", slow(), soft_timeout=0.05)

    ticket = await generate_music(prompt="epic")
    assert ticket["status"] == "running" and await ctx.store.artifacts() == []
    release.set()
    for _ in range(50):
        await asyncio.sleep(0.01)
        if await ctx.store.artifacts():
            break
    (row,) = await ctx.store.artifacts()
    (call,) = await ctx.store.calls(tool="generate_music")
    assert (row["title"], row["prompt"], row["call_id"]) == ("Late", "epic", call["id"])
    assert (await ctx.jobs.get(ticket["task_id"]))["title"] == "Late"  # fetching afterwards still works, and adds nothing
    assert len(await ctx.store.artifacts()) == 1


async def test_an_inline_job_is_not_indexed_twice(ctx):
    calls = []

    async def _late(result):
        calls.append(result)

    ctx.jobs.on_late_result = _late

    async def quick():
        return {"ok": True}

    assert await ctx.jobs.run("x", quick()) == {"ok": True}
    assert calls == []  # finished inside the window: the recorder indexes it, not the hook


# --------------------------------------------------------------------------- offline stand-ins


async def test_offline_dev_sandbox_answers_generation_with_stand_ins(ctx, monkeypatch):
    monkeypatch.setenv("OMNIAPI_DEV", "1")
    monkeypatch.setenv("OMNIAPI_OFFLINE", "1")
    ran = []
    recorded = make_recorded(lambda: ctx, source="gui")

    def tool(name):
        async def fn(**kwargs):
            ran.append(name)  # the real handler: must never run offline
            return {}
        fn.__name__ = name
        return recorded(fn)

    img = await tool("generate_image")(prompt="a lighthouse", n=2)
    assert img["metadata"]["provider"] == "echo" and len(img["images"]) == 2
    edit = await tool("edit_image")(prompt="at night", image_path="C:/whatever.png")
    assert edit["operation"] == "edit"
    speech = await tool("generate_speech")(text="早安")
    music = await tool("generate_music")(prompt="lofi", title="Demo")
    text = await tool("transcribe_audio")(audio_path="C:/rec/a.m4a")
    assert Path(speech["audio_path"]).is_file() and Path(music["audio_path"]).is_file() and "a.m4a" in text["text"]
    refused = await tool("compose_music")(action="plan")
    assert refused["status"] == "refused"
    assert ran == []
    kinds = sorted(r["kind"] for r in await ctx.store.artifacts())
    assert kinds == ["image", "image", "image", "music", "speech", "transcript"]
    assert all(r["source"] == "gui" and r["cost_usd"] in (0.0, None) for r in await ctx.store.artifacts())
    base = Path(ctx.settings.storage.base_path).resolve()
    assert all(Path(r["file_path"]).is_relative_to(base) for r in await ctx.store.artifacts())


async def test_offline_without_dev_still_refuses(ctx, monkeypatch):
    monkeypatch.delenv("OMNIAPI_DEV", raising=False)
    monkeypatch.setenv("OMNIAPI_OFFLINE", "1")
    recorded = make_recorded(lambda: ctx)

    @recorded
    async def generate_image(prompt: str = "") -> dict:
        raise AssertionError("must not run")

    assert (await generate_image(prompt="x"))["status"] == "refused"
    assert await ctx.store.artifacts() == []


# --------------------------------------------------------------------------- backfill


async def test_backfill_reads_sidecars_is_idempotent_and_supports_dry_run(ctx):
    base = Path(ctx.settings.storage.base_path)
    day = base / "images" / "2026-07-01"
    day.mkdir(parents=True)
    (day / "img_20260701101500_abc.png").write_bytes(_png((80, 60)))
    (day / "img_20260701101500_abc.json").write_text(json.dumps({
        "image_id": "img_20260701101500_abc", "prompt": "a harbour", "model": "gpt-image-2", "provider": "openai",
        "parameters": {"size": "1024x1024"}, "cost_info": {"estimated_cost_usd": 0.04}}), encoding="utf-8")
    (day / "img_20260702000000_nosidecar.webp").write_bytes(_png())  # extension lies; still listed, size unknown is fine
    (base / "audio" / "2026-07-02").mkdir(parents=True)
    (base / "audio" / "2026-07-02" / "speech_20260702030405_aa.wav").write_bytes(_wav(2))
    (base / "music" / "2026-07-03").mkdir(parents=True)
    (base / "music" / "2026-07-03" / "music_20260703000000_bb.mp3").write_bytes(b"ID3")
    (base / "music" / "2026-07-03" / "lyrics_20260703000001_cc.txt").write_text("verse", encoding="utf-8")
    (base / "music" / "2026-07-03" / "notes.docx").write_bytes(b"?")  # not ours

    dry = await backfill(ctx.store, base, dry_run=True)
    assert dry["added_total"] == 5 and await ctx.store.artifacts() == []
    res = await backfill(ctx.store, base)
    assert res["added"] == {"image": 2, "speech": 1, "music": 1, "lyrics": 1} and res["already_indexed"] == 0
    again = await backfill(ctx.store, base)
    assert again["added_total"] == 0 and again["already_indexed"] == 5

    rows = {Path(r["file_path"]).name: r for r in await ctx.store.artifacts()}
    img = rows["img_20260701101500_abc.png"]
    assert (img["prompt"], img["model"], img["cost_usd"], img["width"], img["source"]) == ("a harbour", "gpt-image-2", 0.04, 80, "backfill")
    assert img["created_at"] < rows["speech_20260702030405_aa.wav"]["created_at"] < rows["music_20260703000000_bb.mp3"]["created_at"]
    assert rows["speech_20260702030405_aa.wav"]["duration_s"] == 2.0
    assert rows["lyrics_20260703000001_cc.txt"]["text"] == "verse"


# --------------------------------------------------------------------------- REST


@pytest.fixture
def client(ctx, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from omniapi_mcp.daemon import app as daemon_app
    from omniapi_mcp.runtime import runtime

    monkeypatch.setenv("OMNIAPI_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(runtime, "context", ctx)
    app = daemon_app.create_app(ctx.settings)
    return TestClient(app)  # no lifespan: the runtime context is the fixture's


async def test_rest_list_file_thumb_hide_and_missing_file(ctx, client):
    image_id, path = await ctx.storage_manager.save_image(_png((400, 200)), {}, "png")
    (row,) = await index_result(ctx, CallInfo("generate_image", {"prompt": "wide"}), {"image_id": image_id})
    await ctx.store.add_artifact(kind="transcript", file_path=str(Path(ctx.settings.storage.base_path) / "t.txt"), text="x" * 1000, created_at=1)

    def go():
        listing = client.get("/api/artifacts").json()
        assert listing["counts"] == {"image": 1, "transcript": 1} and listing["next_before"] is None
        first, second = listing["items"]
        assert first["id"] == row["id"] and first["exists"] is True and first["thumb_url"]
        assert second["exists"] is False and len(second["text"]) == 401 and second["thumb_url"] is None
        assert len(client.get(f"/api/artifacts/{second['id']}").json()["text"]) == 1000
        f = client.get(first["file_url"])
        assert f.status_code == 200 and f.headers["content-type"] == "image/png" and f.content == Path(path).read_bytes()
        assert "attachment" in client.get(first["file_url"], params={"download": True}).headers["content-disposition"]
        t = client.get(first["thumb_url"], params={"w": 200})
        assert t.status_code == 200 and t.headers["content-type"] == "image/webp"
        assert Image.open(io.BytesIO(t.content)).width == 240
        assert client.get(second["file_url"]).status_code == 410
        assert client.get(f"/api/artifacts/{second['id']}/thumb").status_code == 400
        assert client.get("/api/artifacts/nope/file").status_code == 404
        assert client.patch(f"/api/artifacts/{row['id']}", json={"hidden": True}).json()["hidden"] is True
        assert [a["id"] for a in client.get("/api/artifacts").json()["items"]] == [second["id"]]
        assert Path(path).is_file()  # hiding never deletes
        assert client.patch(f"/api/artifacts/{row['id']}", json={"file_path": "C:/x"}).status_code == 400

    await asyncio.to_thread(go)


async def test_rest_upload_whitelist_caps_and_image_check(ctx, client, monkeypatch):
    from omniapi_mcp.daemon import app as daemon_app

    def go():
        ok = client.post("/api/uploads", params={"filename": "../../ref photo.PNG"}, content=_png())
        assert ok.status_code == 200
        up = ok.json()
        assert "file_path" not in up and up["file_url"] == f"/api/uploads/{up['id']}/file"  # 1.2-M1-e: ids, never paths
        (stored,) = [p for p in (Path(ctx.settings.storage.base_path) / "uploads").rglob("*") if p.is_file()]
        stored = stored.resolve()
        assert up["kind"] == "image" and up["filename"] == "ref photo.PNG" and stored.name.startswith("upload_") and stored.suffix == ".png"
        assert stored.is_relative_to(Path(ctx.settings.storage.base_path).resolve() / "uploads")
        assert client.get(f"/api/uploads/{up['id']}/file").content == _png()
        assert client.post("/api/uploads", params={"filename": "a.m4a"}, content=b"audio").json()["kind"] == "audio"
        assert client.post("/api/uploads", params={"filename": "evil.exe"}, content=b"MZ").status_code == 415
        assert client.post("/api/uploads", params={"filename": "fake.png"}, content=b"not an image").status_code == 400
        assert client.post("/api/uploads", params={"filename": "empty.wav"}, content=b"").status_code == 400
        monkeypatch.setattr(daemon_app, "UPLOAD_MAX_AUDIO", 10)
        assert client.post("/api/uploads", params={"filename": "big.mp3"}, content=b"x" * 11).status_code == 413
        left = sorted(p.name for p in (Path(ctx.settings.storage.base_path) / "uploads").rglob("*") if p.is_file())
        assert len(left) == 2  # refused uploads leave nothing behind

    await asyncio.to_thread(go)


async def test_rest_backfill_endpoint(ctx, client):
    day = Path(ctx.settings.storage.base_path) / "images" / "2026-08-01"
    day.mkdir(parents=True)
    (day / "img_20260801000000_zz.png").write_bytes(_png())

    def go():
        assert client.post("/api/artifacts/backfill", params={"dry_run": True}).json()["added_total"] == 1
        assert client.post("/api/artifacts/backfill").json()["added"] == {"image": 1}
        assert client.get("/api/artifacts", params={"source": "backfill"}).json()["items"][0]["width"] == 64

    await asyncio.to_thread(go)
