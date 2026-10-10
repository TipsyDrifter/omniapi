"""A cost that is an estimate says so (1.4.1): ``cost_estimated`` on ledger rows,
works and generation jobs, set where a vendor reports no usage and the catalog
price can be applied (speech, a per-minute composition with a given length, a
video billed at its estimate, the image estimates), carried through the
recorder, the store, the REST API and the MCP tool result; a 1.4.0 database
upgrades in place and its old rows read ``false``."""

from __future__ import annotations

import asyncio
import os
import sqlite3
from types import SimpleNamespace as NS

import pytest

from omniapi_mcp.bus import EventBus
from omniapi_mcp.capabilities.speech import SpeechResult
from omniapi_mcp.catalog import catalog
from omniapi_mcp.config.settings import Settings
from omniapi_mcp.recorder import extract_call_meta, make_recorded
from omniapi_mcp.store.db import Store
from omniapi_mcp.tools.speech import SpeechTool


# ---------------------------------------------------------------- the catalog's flat prices
def test_flat_speech_prices_are_applied_per_character():
    assert catalog.estimate_speech_cost("eleven_v3", 1000) == pytest.approx(0.08)
    assert catalog.estimate_speech_cost("eleven_flash_v2_5", 2500) == pytest.approx(0.10)
    assert catalog.estimate_speech_cost("tts-1", 1_000_000) == pytest.approx(15.0)  # priced per million characters
    assert catalog.estimate_speech_cost("tts-1-hd", 2000) == pytest.approx(0.06)
    assert catalog.estimate_speech_cost("eleven_v3", 0) == 0.0
    # token-priced or unknown: characters say nothing reliable about the bill
    assert catalog.estimate_speech_cost("gpt-4o-mini-tts", 1000) is None
    assert catalog.estimate_speech_cost("gemini-3.8-flash-tts", 1000) is None
    assert catalog.estimate_speech_cost("no-such-model", 1000) is None


def test_per_minute_prices_need_a_length():
    assert catalog.estimate_per_minute_cost("music_v2_5", 120, modality="music") == pytest.approx(0.30)
    assert catalog.estimate_per_minute_cost("whisper-1", 90, modality="transcription") == pytest.approx(0.009)
    assert catalog.estimate_per_minute_cost("whisper-1", None, modality="transcription") is None
    assert catalog.estimate_per_minute_cost("lyria-3.5", 120, modality="music") is None  # per song, not per minute


# ---------------------------------------------------------------- the speech tool
class _Provider:
    name = "stub"
    MODEL_SHUTDOWN: dict = {}

    def __init__(self, models):
        self._models = set(models)

    def get_supported_models(self):
        return set(self._models)

    def model_status(self, model):
        return "current"

    async def synthesize(self, text, *, voice=None, model=None, output_format="mp3_44100_128", **kw):
        return SpeechResult(audio_data=b"ID3", output_format="mp3", metadata={"provider": "stub", "model": model, "voice_id": "v"})


def _speech_tool(tmp_path, models):
    tool = SpeechTool(NS(providers=NS(), storage=NS(base_path=str(tmp_path))))
    provider = _Provider(models)
    tool._providers.append(provider)
    tool._model_map.update({m: provider for m in models})
    return tool


@pytest.mark.asyncio
async def test_a_speech_call_without_usage_carries_the_catalog_estimate(tmp_path):
    tool = _speech_tool(tmp_path, ["eleven_v3", "tts-1", "gpt-4o-mini-tts", "gemini-3.8-flash-tts"])
    text = "x" * 500
    el = await tool.synthesize(text, model="eleven_v3")
    assert el["cost_usd"] == pytest.approx(0.04) and el["cost_estimated"] is True
    oa = await tool.synthesize(text, model="tts-1")
    assert oa["cost_usd"] == pytest.approx(0.0075) and oa["cost_estimated"] is True
    for token_priced in ("gpt-4o-mini-tts", "gemini-3.8-flash-tts"):
        out = await tool.synthesize(text, model=token_priced)
        assert "cost_usd" not in out and "cost_estimated" not in out  # nothing reliable to record


@pytest.mark.asyncio
async def test_a_cost_the_provider_reports_is_not_called_an_estimate(tmp_path):
    tool = _speech_tool(tmp_path, ["eleven_v3"])

    async def reports(text, *, voice=None, model=None, output_format="mp3", **kw):
        return SpeechResult(audio_data=b"ID3", output_format="mp3", metadata={"provider": "stub", "model": model, "cost_usd": 0.5})

    tool._model_map["eleven_v3"].synthesize = reports
    out = await tool.synthesize("hello", model="eleven_v3")
    assert out["cost_usd"] == 0.5 and "cost_estimated" not in out


# ---------------------------------------------------------------- the recorder: ledger row, bus event, work
def test_extract_call_meta_reads_the_flag_only_with_a_cost():
    assert extract_call_meta({"cost_usd": 0.04, "cost_estimated": True})["cost_estimated"] is True
    assert extract_call_meta({"cost_usd": 0.04})["cost_estimated"] is False
    assert extract_call_meta({"cost_estimated": True})["cost_estimated"] is False  # no cost, nothing to call an estimate
    img = {"metadata": {"model": "m", "cost_estimate": 0.04, "cost_estimated": True}}
    assert extract_call_meta(img)["cost_usd"] == 0.04 and extract_call_meta(img)["cost_estimated"] is True
    assert extract_call_meta({"metadata": {"cost_estimate": 0.04, "cost_estimated": False}})["cost_estimated"] is False


@pytest.fixture
async def store(tmp_path):
    s = Store(tmp_path / "e.db")
    await s.open()
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_the_recorder_marks_the_ledger_row_and_the_work(store, tmp_path):
    audio = tmp_path / "works" / "speech_20261008000000_ab.mp3"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"ID3")
    events = EventBus()
    ctx = NS(store=store, bus=events, settings=NS(storage=NS(base_path=str(tmp_path / "works"))), jobs=None)
    recorded = make_recorded(lambda: ctx)

    @recorded
    async def generate_speech(text: str = "") -> dict:
        return {"audio_path": str(audio), "model": "eleven_v3", "provider": "elevenlabs", "cost_usd": 0.04, "cost_estimated": True}

    @recorded
    async def transcribe_audio(audio_path: str = "") -> dict:
        return {"text": "hi", "model": "x", "provider": "openai", "cost_usd": 0.01}

    await generate_speech(text="x" * 500)
    await transcribe_audio(audio_path="a.mp3")
    rows = {r["tool"]: r for r in await store.calls()}
    assert rows["generate_speech"]["cost_usd"] == 0.04 and rows["generate_speech"]["cost_estimated"] is True
    assert rows["transcribe_audio"]["cost_usd"] == 0.01 and rows["transcribe_audio"]["cost_estimated"] is False
    (work,) = await store.artifacts(kind="speech")
    (transcript,) = await store.artifacts(kind="transcript")
    assert transcript["cost_estimated"] is False
    assert work["cost_usd"] == pytest.approx(0.04) and work["cost_estimated"] is True
    # totals stay sums of whatever is recorded; the flag says an estimate is in them
    summary = await store.cost_summary(days=1)
    assert summary["total"]["cost"] == pytest.approx(0.05) and summary["total"]["cost_estimated"] is True
    by_model = {m["model"]: m for m in summary["by_model"]}
    assert by_model["eleven_v3"]["cost_estimated"] is True and by_model["x"]["cost_estimated"] is False


@pytest.mark.asyncio
async def test_a_ticketed_call_keeps_the_flag_when_it_settles(store):
    cid = await store.call_started("generate_speech", {})
    await store.call_finished(cid, status="ticket", duration_ms=1)
    await store.settle_ticket_call(cid, model="eleven_v3", provider="elevenlabs", cost_usd=0.04, cost_estimated=True)
    row = await store.call(cid)
    assert (row["status"], row["cost_usd"], row["cost_estimated"]) == ("ok", 0.04, True)
    # a call whose cost turns out unknown loses the flag with the cost
    cid2 = await store.call_started("generate_video", {})
    await store.call_finished(cid2, status="ticket", duration_ms=1, cost_usd=0.1, cost_estimated=True)
    await store.call_unsettled(cid2, "not collected")
    row2 = await store.call(cid2)
    assert row2["cost_usd"] is None and row2["cost_estimated"] is False


# ---------------------------------------------------------------- a 1.4.0 database upgrades in place
V140_TABLES = """
CREATE TABLE calls (
  id TEXT PRIMARY KEY, ts REAL NOT NULL, tool TEXT NOT NULL, status TEXT NOT NULL, duration_ms INTEGER, model TEXT,
  provider TEXT, cost_usd REAL, usage_json TEXT, args_json TEXT, result_json TEXT, error TEXT, session_id TEXT,
  source TEXT, conversation_id TEXT
);
CREATE TABLE artifacts (
  id TEXT PRIMARY KEY, created_at REAL NOT NULL, kind TEXT NOT NULL, tool TEXT, model TEXT, provider TEXT, title TEXT,
  prompt TEXT, params_json TEXT, file_path TEXT NOT NULL UNIQUE, mime TEXT, bytes INTEGER, width INTEGER, height INTEGER,
  duration_s REAL, text TEXT, cost_usd REAL, source TEXT, call_id TEXT, parent_id TEXT, hidden INTEGER NOT NULL DEFAULT 0,
  meta_json TEXT
);
CREATE TABLE generations (
  id TEXT PRIMARY KEY, created_at REAL NOT NULL, finished_at REAL, kind TEXT NOT NULL, tool TEXT NOT NULL, model TEXT,
  title TEXT, params_json TEXT, sources_json TEXT, status TEXT NOT NULL, error TEXT, error_kind TEXT, estimate_json TEXT,
  cost_usd REAL, call_id TEXT, artifact_ids_json TEXT, source TEXT, meta_json TEXT, provider TEXT, remote_id TEXT,
  submitted_at REAL, remote_status TEXT, polled_at REAL, lease_owner TEXT, lease_until REAL, remote_json TEXT
);
INSERT INTO calls (id, ts, tool, status, cost_usd, source) VALUES ('c-old', 1759900000, 'generate_image', 'ok', 0.04, 'mcp');
INSERT INTO artifacts (id, created_at, kind, file_path, cost_usd) VALUES ('a-old', 1759900000, 'image', 'C:/w/old.png', 0.04);
INSERT INTO generations (id, created_at, kind, tool, status, cost_usd) VALUES ('g-old', 1759900000, 'image', 'generate_image', 'done', 0.04);
PRAGMA user_version=1;
"""


@pytest.mark.asyncio
async def test_a_140_database_upgrades_cleanly_and_old_rows_are_not_estimates(tmp_path):
    path = tmp_path / "omniapi.db"
    con = sqlite3.connect(path)
    con.executescript(V140_TABLES)
    con.commit()
    con.close()
    s = Store(path)
    await s.open()
    try:
        assert (await s.call("c-old"))["cost_estimated"] is False
        assert (await s.artifact("a-old"))["cost_estimated"] is False
        assert (await s.generation("g-old"))["cost_estimated"] is False
        assert (await s.call("c-old"))["cost_usd"] == 0.04  # nothing else changed
        new = await s.call_started("generate_speech", {})
        await s.call_finished(new, status="ok", duration_ms=1, cost_usd=0.04, cost_estimated=True)
        assert (await s.call(new))["cost_estimated"] is True
    finally:
        await s.close()
    # opening it a second time is a no-op
    s = Store(path)
    await s.open()
    try:
        assert (await s.call("c-old"))["cost_estimated"] is False
    finally:
        await s.close()
    con = sqlite3.connect(path)
    try:
        for table in ("calls", "artifacts", "generations"):
            cols = {r[1]: r for r in con.execute(f"PRAGMA table_info({table})")}
            assert cols["cost_estimated"][2] == "INTEGER" and cols["cost_estimated"][4] == "0", table
    finally:
        con.close()


# ---------------------------------------------------------------- over HTTP
@pytest.fixture
def seeded_daemon(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from omniapi_mcp import server as srv
    from omniapi_mcp.daemon import app as dapp

    for k in list(os.environ):
        if k.upper().startswith(("PROVIDERS__", "TIERS__", "DEFAULTS__")):
            monkeypatch.delenv(k)
    home = tmp_path / "home"
    monkeypatch.setenv("OMNIAPI_HOME", str(home))
    monkeypatch.setenv("OMNIAPI_OFFLINE", "1")
    monkeypatch.setenv("OMNIAPI_DEV", "1")
    monkeypatch.setattr("omniapi_mcp.layout.env_file", lambda *a, **k: None)

    async def no_discovery(*a, **k):
        return {}

    monkeypatch.setattr(catalog, "refresh", no_discovery)
    monkeypatch.setattr(srv.mcp, "_session_manager", None)
    monkeypatch.setattr(srv.mcp.settings, "streamable_http_path", srv.mcp.settings.streamable_http_path)
    ids: dict[str, str] = {}

    async def seed() -> None:
        home.mkdir(parents=True, exist_ok=True)
        s = Store(home / "omniapi.db")
        await s.open()
        ids["est"] = await s.call_started("generate_speech", {"text": "x"})
        await s.call_finished(ids["est"], status="ok", duration_ms=5, model="eleven_v3", provider="elevenlabs",
                              cost_usd=0.04, cost_estimated=True)
        ids["real"] = await s.call_started("generate_image", {})
        await s.call_finished(ids["real"], status="ok", duration_ms=5, model="gpt-image-2", cost_usd=0.1)
        a = await s.add_artifact(kind="speech", tool="generate_speech", model="eleven_v3", file_path="C:/w/s.mp3",
                                 cost_usd=0.04, cost_estimated=True, call_id=ids["est"])
        b = await s.add_artifact(kind="image", tool="generate_image", model="gpt-image-2", file_path="C:/w/i.png", cost_usd=0.1)
        ids["art_est"], ids["art_real"] = a["id"], b["id"]
        await s.create_generation("g" * 16, kind="speech", tool="generate_speech", model="eleven_v3", title="t", params={},
                                  sources={}, estimate={}, source="gui")
        await s.update_generation("g" * 16, status="done", cost_usd=0.04, cost_estimated=True, artifact_ids=[a["id"]])
        await s.create_generation("h" * 16, kind="image", tool="generate_image", model="gpt-image-2", title="t", params={},
                                  sources={}, estimate={}, source="gui")
        await s.update_generation("h" * 16, status="done", cost_usd=0.1, artifact_ids=[b["id"]])
        await s.close()

    asyncio.run(seed())
    app = dapp.create_app(Settings(_env_file=None), host="127.0.0.1", port=7798)
    with TestClient(app, base_url="http://127.0.0.1:7798") as client:
        client.ids = ids
        yield client


def test_the_rest_api_carries_cost_estimated_everywhere_a_cost_comes_from_a_row(seeded_daemon):
    c, ids = seeded_daemon, seeded_daemon.ids
    calls = {r["id"]: r for r in c.get("/api/calls").json()}
    assert calls[ids["est"]]["cost_estimated"] is True and calls[ids["real"]]["cost_estimated"] is False
    one = c.get(f"/api/calls/{ids['est']}").json()
    assert one["cost_usd"] == 0.04 and one["cost_estimated"] is True
    costs = c.get("/api/costs?days=30").json()
    assert costs["total"]["cost"] == pytest.approx(0.14) and costs["total"]["cost_estimated"] is True
    assert {m["model"]: m["cost_estimated"] for m in costs["by_model"]} == {"eleven_v3": True, "gpt-image-2": False}
    assert all(isinstance(d["cost_estimated"], bool) for d in costs["by_day"])
    items = {w["id"]: w for w in c.get("/api/artifacts").json()["items"]}
    assert items[ids["art_est"]]["cost_estimated"] is True and items[ids["art_real"]]["cost_estimated"] is False
    detail = c.get(f"/api/artifacts/{ids['art_est']}").json()
    assert detail["cost_usd"] == 0.04 and detail["cost_estimated"] is True
    jobs = {g["id"]: g for g in c.get("/api/generations").json()}
    assert jobs["g" * 16]["cost_estimated"] is True and jobs["h" * 16]["cost_estimated"] is False
    job = c.get(f"/api/generations/{'g' * 16}").json()
    assert job["cost_usd"] == 0.04 and job["cost_estimated"] is True
    assert job["artifacts"][0]["cost_estimated"] is True  # the work inside the job card


# ---------------------------------------------------------------- ElevenLabs Music: per-minute price x the length asked for
@pytest.mark.asyncio
async def test_elevenlabs_music_with_a_given_length_records_the_per_minute_price_as_an_estimate():
    import httpx

    from omniapi_mcp.capabilities.music import ElevenLabsMusicProvider
    from omniapi_mcp.providers.base import ProviderConfig

    p = ElevenLabsMusicProvider(ProviderConfig(api_key="unit-test-not-a-key", base_url="https://el.test", timeout=30.0))
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b"ID3", headers={"content-type": "audio/mpeg"})))
    p._http = lambda: client  # type: ignore[method-assign]
    sized = await p.compose(prompt="calm piano", music_length_ms=90_000)
    assert sized.metadata["cost_usd"] == pytest.approx(0.225) and sized.metadata["cost_estimated"] is True
    # the model picks the length: nothing is guessed
    free = await p.compose(prompt="calm piano")
    assert "cost_usd" not in free.metadata and "cost_estimated" not in free.metadata
    await client.aclose()
