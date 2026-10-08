"""A Suno job answers with two songs: both are downloaded, saved and indexed.

- provider: every song of the job comes back (``extra_tracks``), on both
  routes, unless switched off; a second song that cannot be fetched does not
  fail the call (the job is paid for);
- tool: the first song keeps the old fields and file name, ``tracks`` lists
  every song, the second is filed next to the first as ``<stem>_2``;
- index: one work per song, the second hangs under the first (lineage), each
  with its own id / title / length, the cost split and still adding up;
- settings: ``music.suno_all_tracks`` in settings.json (and the environment).

All kie traffic goes to ``httpx.MockTransport``; the key is a placeholder.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace as NS

import httpx
import pytest

from omniapi_mcp.artifacts import CallInfo, index_result
from omniapi_mcp.bus import EventBus
from omniapi_mcp.capabilities.music import SunoProvider
from omniapi_mcp.config import user_settings as US
from omniapi_mcp.config.settings import KieSettings, MusicSettings, Settings
from omniapi_mcp.providers.base import ProviderConfig
from omniapi_mcp.store.db import Store
from omniapi_mcp.tools.music_generation import MusicGenerationTool, suno_all_tracks

FAKE_KEY = "unit-test-not-a-key"
BASE = "https://kie.test"
CREATE = "/api/v1/jobs/createTask"
RECORD = "/api/v1/jobs/recordInfo"
A, B = "https://cdn.test/a.mp3", "https://cdn.test/b.mp3"
TRACKS = [
    {"id": "aud-A", "audio_url": A, "title": "Night", "duration": 198.4, "tags": "rock"},
    {"id": "aud-B", "audio_url": B, "title": "Night (take 2)", "duration": 201.0, "tags": "rock"},
]


def _transport(routes: dict[str, list]):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.url.path if request.url.host == "kie.test" else str(request.url)
        seen.append(key)
        queue = routes.get(key)
        if not queue:
            return httpx.Response(404, json={"code": 404, "msg": f"unscripted {key}"})
        status, body = queue.pop(0) if len(queue) > 1 else queue[0]
        return httpx.Response(status, content=body) if isinstance(body, bytes) else httpx.Response(status, json=body)

    return handler, seen


def _ok(data):
    return (200, {"code": 200, "msg": "success", "data": data})


def _jobs(result, credits=12, files=(A, B)):
    routes = {
        CREATE: [_ok({"taskId": "job-1"})],
        RECORD: [_ok({"taskId": "job-1", "state": "generating"}),
                 _ok({"taskId": "job-1", "state": "success", "resultJson": json.dumps(result), "creditsConsumed": credits})],
    }
    for f in files:
        routes[f] = [(200, b"BYTES:" + f.encode())]
    return routes


def _legacy(tracks, files=(A, B)):
    routes = {
        "/api/v1/generate": [_ok({"taskId": "old-1"})],
        "/api/v1/generate/record-info": [_ok({"status": "SUCCESS", "response": {"sunoData": tracks}})],
    }
    for f in files:
        routes[f] = [(200, b"BYTES:" + f.encode())]
    return routes


def _wire(p: SunoProvider, routes) -> list[str]:
    handler, seen = _transport(routes)
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def no_sleep(_s):
        return None

    p.kie._sleep = no_sleep
    return seen


def _provider(answers, **kw) -> tuple[SunoProvider, list[str]]:
    p = SunoProvider(ProviderConfig(api_key=FAKE_KEY, base_url=BASE, timeout=30.0), **kw)
    return p, _wire(p, answers)


# ---- provider ----------------------------------------------------------------


class TestProvider:
    async def test_jobs_generate_downloads_both_songs(self):
        p, seen = _provider(_jobs({"code": 200, "data": TRACKS}))
        r = await p.generate("a song", model="V6")
        assert r.audio_data == b"BYTES:" + A.encode() and r.metadata["audio_id"] == "aud-A"
        assert r.metadata["audio_ids"] == ["aud-A", "aud-B"] and r.metadata["all_tracks"] == [A, B]
        (extra,) = r.extra_tracks
        assert extra["audio_data"] == b"BYTES:" + B.encode()
        assert (extra["audio_id"], extra["title"], extra["duration"], extra["url"]) == ("aud-B", "Night (take 2)", 201.0, B)
        assert seen.count(A) == seen.count(B) == 1
        assert r.metadata["cost_usd"] == pytest.approx(0.06)  # one job, one cost: not doubled

    async def test_legacy_generate_downloads_both_songs(self):
        legacy = [{"id": "aud-A", "audioUrl": A, "title": "T"}, {"id": "aud-B", "audioUrl": B, "title": "T"}]
        p, _ = _provider(_legacy(legacy), routes="legacy")
        r = await p.generate("a song")
        assert r.metadata["route"] == "legacy" and [e["audio_id"] for e in r.extra_tracks] == ["aud-B"]
        assert r.extra_tracks[0]["audio_data"] == b"BYTES:" + B.encode()

    @pytest.mark.parametrize("op,call", [
        ("extend", lambda p: p.extend(audio_id="aud-0")),
        ("cover", lambda p: p.cover(upload_url="https://x.test/in.mp3", prompt="jazz")),
        ("upload_extend", lambda p: p.upload_extend(upload_url="https://x.test/in.mp3")),
        ("add_instrumental", lambda p: p.add_instrumental(upload_url="https://x.test/in.mp3", title="t", tags="a", negative_tags="b")),
        ("add_vocals", lambda p: p.add_vocals(upload_url="https://x.test/in.mp3", prompt="p", title="t", style="s", negative_tags="n")),
    ])
    async def test_every_audio_operation_keeps_both(self, op, call):
        p, _ = _provider(_jobs({"data": TRACKS}), routes=op)
        r = await call(p)
        assert r.metadata["operation"] == op and len(r.extra_tracks) == 1 and r.extra_tracks[0]["audio_data"]

    async def test_switched_off_downloads_only_the_first(self):
        p, seen = _provider(_jobs({"data": TRACKS}), all_tracks=False)
        r = await p.generate("a song")
        assert r.extra_tracks == [] and B not in seen
        assert r.metadata["audio_ids"] == ["aud-A", "aud-B"]  # still says the job had two

    async def test_a_call_can_override_the_switch(self):
        p, seen = _provider(_jobs({"data": TRACKS}), all_tracks=True)
        r = await p.generate("a song", all_tracks=False)
        assert r.extra_tracks == [] and B not in seen

    async def test_a_second_song_that_cannot_be_fetched_does_not_fail_the_call(self):
        p, _ = _provider(_jobs({"data": TRACKS}, files=(A,)))  # b.mp3 answers 404
        r = await p.generate("a song")
        assert r.audio_data and r.extra_tracks[0]["audio_id"] == "aud-B"
        assert "audio_data" not in r.extra_tracks[0] and "404" in r.extra_tracks[0]["error"]

    async def test_a_single_song_job_has_no_extras(self):
        p, _ = _provider(_jobs({"data": TRACKS[:1]}, files=(A,)))
        r = await p.generate("a song")
        assert r.extra_tracks == []

    async def test_bare_result_urls_keep_every_url(self):
        p, _ = _provider(_jobs({"resultUrls": [A, B]}))  # no old record-info scripted: ids unknown
        r = await p.generate("a song")
        assert r.metadata["audio_id"] is None and r.extra_tracks[0]["url"] == B and r.extra_tracks[0]["audio_data"]


# ---- tool --------------------------------------------------------------------


def _tool(tmp_path, music=None, **kie) -> MusicGenerationTool:
    settings = NS(
        providers=NS(elevenlabs=None, kie=KieSettings(api_key=FAKE_KEY, enabled=True, base_url=BASE, **kie)),
        storage=NS(base_path=str(tmp_path)),
        **({"music": music} if music is not None else {}),
    )
    return MusicGenerationTool(settings=settings)


class TestTool:
    async def test_both_songs_are_saved_and_listed(self, tmp_path):
        tool = _tool(tmp_path)
        _wire(tool._suno(), _jobs({"data": TRACKS}))
        out = await tool.generate("a song", model="V6")
        first, second = Path(out["audio_path"]), Path(out["tracks"][1]["audio_path"])
        # the first song keeps the old fields and file name
        assert first.name.startswith("music_") and first.read_bytes() == b"BYTES:" + A.encode()
        assert (out["audio_id"], out["title"], out["duration"]) == ("aud-A", "Night", 198.4)
        assert out["cost_usd"] == pytest.approx(0.06) and out["track_count"] == 2
        # the second is filed next to it
        assert second.parent == first.parent and second.name == f"{first.stem}_2.mp3"
        assert second.read_bytes() == b"BYTES:" + B.encode()
        assert out["tracks"][0] == {"audio_path": str(first), "audio_id": "aud-A", "title": "Night", "duration": 198.4,
                                    "bytes": len(b"BYTES:" + A.encode())}
        assert out["tracks"][1] == {"audio_path": str(second), "audio_id": "aud-B", "title": "Night (take 2)",
                                    "duration": 201.0, "bytes": len(b"BYTES:" + B.encode())}

    async def test_extend_saves_both_too(self, tmp_path):
        tool = _tool(tmp_path, suno_routes="extend")
        _wire(tool._suno(), _jobs({"data": TRACKS}))
        out = await tool.edit(action="extend", audio_id="aud-0")
        assert out["operation"] == "extend" and out["track_count"] == 2 and Path(out["tracks"][1]["audio_path"]).is_file()

    async def test_a_lost_second_song_is_reported_not_saved(self, tmp_path):
        tool = _tool(tmp_path)
        _wire(tool._suno(), _jobs({"data": TRACKS}, files=(A,)))
        out = await tool.generate("a song", model="V6")
        assert out["tracks"][1]["audio_id"] == "aud-B" and "error" in out["tracks"][1]
        assert "audio_path" not in out["tracks"][1]
        assert len(list(Path(out["audio_path"]).parent.iterdir())) == 1

    async def test_setting_off_keeps_the_old_answer(self, tmp_path):
        tool = _tool(tmp_path, music=MusicSettings(suno_all_tracks=False))
        assert tool._suno().all_tracks is False
        _wire(tool._suno(), _jobs({"data": TRACKS}))
        out = await tool.generate("a song", model="V6")
        assert "tracks" not in out and len(list(Path(out["audio_path"]).parent.iterdir())) == 1

    def test_setting_reader_defaults_to_all(self):
        assert suno_all_tracks(None) is True and suno_all_tracks(NS()) is True
        assert suno_all_tracks(NS(music=NS(suno_all_tracks=False))) is False


# ---- index -------------------------------------------------------------------


@pytest.fixture
async def ctx(tmp_path):
    store = Store(tmp_path / "a.db")
    await store.open()
    c = NS(store=store, bus=EventBus(), settings=NS(storage=NS(base_path=str(tmp_path / "storage"))))
    yield c
    await store.close()


def _songs(base: Path) -> tuple[Path, Path]:
    d = base / "music" / "2026-10-06"
    d.mkdir(parents=True)
    a, b = d / "music_20261006010203_abcd1234.mp3", d / "music_20261006010203_abcd1234_2.mp3"
    a.write_bytes(b"ID3a")
    b.write_bytes(b"ID3bb")
    return a, b


class TestIndex:
    async def test_one_work_per_song_linked_and_cost_split(self, ctx):
        a, b = _songs(Path(ctx.settings.storage.base_path))
        result = {
            "audio_path": str(a), "provider": "suno", "operation": "generate", "task_id": "job-1", "audio_id": "aud-A",
            "title": "Night", "duration": 198.4, "cost_usd": 0.06, "track_count": 2,
            "tracks": [
                {"audio_path": str(a), "audio_id": "aud-A", "title": "Night", "duration": 198.4, "bytes": 4},
                {"audio_path": str(b), "audio_id": "aud-B", "title": "Night (take 2)", "duration": 201.0, "bytes": 5},
            ],
        }
        q = ctx.bus.subscribe()
        rows = await index_result(ctx, CallInfo("generate_music", {"prompt": "lofi", "model": "V6"}, call_id="c1"), result)
        assert [r["file_path"] for r in rows] == [str(a.resolve()), str(b.resolve())]
        one, two = rows
        assert (one["title"], one["duration_s"], one["meta"]["audio_id"]) == ("Night", 198.4, "aud-A")
        assert (two["title"], two["duration_s"], two["meta"]["audio_id"]) == ("Night (take 2)", 201.0, "aud-B")
        assert one["parent_id"] is None and two["parent_id"] == one["id"]
        assert (one["meta"]["track"], two["meta"]["track"], two["meta"]["track_count"]) == (1, 2, 2)
        assert one["meta"]["task_id"] == two["meta"]["task_id"] == "job-1"
        assert one["cost_usd"] + two["cost_usd"] == pytest.approx(0.06) and one["cost_usd"] == pytest.approx(0.03)
        assert one["call_id"] == two["call_id"] == "c1" and one["prompt"] == two["prompt"] == "lofi"
        # both reach the works wall as new works
        events = [q.get_nowait() for _ in range(2)]
        assert [e["artifact"]["id"] for e in events if e["type"] == "artifact.created"] == [one["id"], two["id"]]
        assert [c["id"] for c in await ctx.store.artifact_children(one["id"])] == [two["id"]]

    async def test_uneven_cost_still_adds_up(self, ctx):
        base = Path(ctx.settings.storage.base_path)
        d = base / "music" / "x"
        d.mkdir(parents=True)
        paths = [d / f"music_20261006010203_ff_{i}.mp3" for i in range(3)]
        for p in paths:
            p.write_bytes(b"ID3")
        result = {"audio_path": str(paths[0]), "cost_usd": 0.1,
                  "tracks": [{"audio_path": str(p), "audio_id": f"id{i}"} for i, p in enumerate(paths)]}
        rows = await index_result(ctx, CallInfo("generate_music", {"prompt": "x"}), result)
        assert len(rows) == 3 and sum(r["cost_usd"] for r in rows) == pytest.approx(0.1, abs=1e-9)

    async def test_a_song_without_a_file_is_skipped(self, ctx):
        a, _ = _songs(Path(ctx.settings.storage.base_path))
        result = {"audio_path": str(a), "cost_usd": 0.06, "tracks": [
            {"audio_path": str(a), "audio_id": "aud-A"}, {"audio_id": "aud-B", "error": "HTTP 404"}]}
        rows = await index_result(ctx, CallInfo("generate_music", {"prompt": "x"}), result)
        assert len(rows) == 1 and rows[0]["cost_usd"] == pytest.approx(0.06) and "track" not in rows[0]["meta"]

    async def test_single_song_result_is_indexed_as_before(self, ctx):
        a, _ = _songs(Path(ctx.settings.storage.base_path))
        rows = await index_result(ctx, CallInfo("generate_music", {"prompt": "x"}),
                                  {"audio_path": str(a), "title": "Solo", "duration": 3, "cost_usd": 0.05, "audio_id": "aud-A"})
        assert len(rows) == 1 and rows[0]["meta"] == {"audio_id": "aud-A"} and rows[0]["cost_usd"] == 0.05

    async def test_backfill_of_old_music_is_unchanged(self, ctx):
        from omniapi_mcp.artifacts import backfill

        a, b = _songs(Path(ctx.settings.storage.base_path))
        res = await backfill(ctx.store, ctx.settings.storage.base_path)
        assert res["added"] == {"music": 2}
        rows = {Path(r["file_path"]).name: r for r in await ctx.store.artifacts(limit=10)}
        assert rows[a.name]["parent_id"] is None and rows[b.name]["parent_id"] is None  # no guessing at lineage
        assert rows[a.name]["source"] == rows[b.name]["source"] == "backfill"


# ---- settings ----------------------------------------------------------------


@pytest.fixture
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIAPI_HOME", str(tmp_path / "home"))
    for k in list(os.environ):
        if k.upper().startswith(("PROVIDERS__", "MUSIC__")):
            monkeypatch.delenv(k)
    monkeypatch.setattr("omniapi_mcp.layout.env_file", lambda *a, **k: None)


class TestSetting:
    def test_default_is_all(self):
        assert Settings().music.suno_all_tracks is True

    def test_settings_json_switches_it_off(self, _home):
        overlay = US.normalize_overlay({"music": {"suno_all_tracks": False}})
        assert overlay == {"music": {"suno_all_tracks": False}}
        eff, env = US.build_settings(overlay)
        assert eff.music.suno_all_tracks is False and env.music.suno_all_tracks is True
        view = US.describe(eff, env, overlay)
        assert view["music"]["suno_all_tracks"] == {"value": False, "source": "settings"}
        assert "video" in view  # the other section is still there

    def test_bad_values_are_dropped_or_refused(self, _home):
        assert US.normalize_overlay({"music": {"suno_all_tracks": "no", "bogus": 1}}) == {}
        with pytest.raises(US.SettingsError):
            US.apply_patch({}, {"music": {"suno_all_tracks": 0}})
        with pytest.raises(US.SettingsError):
            US.apply_patch({}, {"music": {"bogus": True}})

    def test_patch_sets_and_clears(self, _home):
        o, changed = US.apply_patch({}, {"music": {"suno_all_tracks": False}})
        assert o == {"music": {"suno_all_tracks": False}} and changed == ["music.suno_all_tracks"]
        o2, changed = US.apply_patch(o, {"music": {"suno_all_tracks": None}})
        assert o2 == {} and changed == ["music.suno_all_tracks"]

    def test_environment(self, _home, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("MUSIC__SUNO_ALL_TRACKS", "false")
        assert Settings().music.suno_all_tracks is False
