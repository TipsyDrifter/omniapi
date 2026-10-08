"""Smoke test for scripts/kie_suno_probe.py against a fake kie (no network,
no key): every op runs in order, raw answers land on disk, an answer the
provider does not recognise is kept without stopping the run, and the ops
that lack an input are skipped."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx

from omniapi_mcp.capabilities.music import SunoProvider
from omniapi_mcp.providers.base import ProviderConfig

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "kie_suno_probe.py"
A, B = "https://cdn.test/a.mp3", "https://cdn.test/b.mp3"
SONGS = [{"id": "aud-A", "audio_url": A, "title": "Probe", "duration": 30.0},
         {"id": "aud-B", "audio_url": B, "title": "Probe", "duration": 31.0}]
RESULTS = {
    "ai-music-api/generate": {"code": 200, "data": SONGS},
    "ai-music-api/extend": {"code": 200, "data": SONGS},
    "ai-music-api/generate-lyrics": {"code": 200, "data": [{"text": "[Verse] rain", "title": "Rain"}]},
    "ai-music-api/convert-to-wav-format": {"audioWavUrl": "https://cdn.test/a.wav"},
    "ai-music-api/create-music-video": {"something": "unexpected"},  # not recognised: kept, run goes on
}


def _load():
    spec = importlib.util.spec_from_file_location("kie_suno_probe", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_kie():
    tasks: dict[str, str] = {}
    calls = {"credit": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host != "kie.test":
            return httpx.Response(200, content=b"BYTES:" + str(request.url).encode())
        if path == "/api/v1/jobs/createTask":
            body = json.loads(request.content)
            tid = f"job-{len(tasks) + 1}"
            tasks[tid] = body["model"]
            return httpx.Response(200, json={"code": 200, "data": {"taskId": tid}})
        if path == "/api/v1/jobs/recordInfo":
            tid = request.url.params["taskId"]
            result = RESULTS[tasks[tid]]
            return httpx.Response(200, json={"code": 200, "data": {
                "taskId": tid, "state": "success", "resultJson": json.dumps(result), "creditsConsumed": 12}})
        if path == "/api/v1/chat/credit":
            calls["credit"] += 1
            return httpx.Response(200, json={"code": 200, "data": 1000 - calls["credit"]})
        return httpx.Response(404, json={"code": 404, "msg": "unscripted"})

    return handler, tasks


async def test_probe_runs_every_op_and_keeps_the_raw_answers(tmp_path, capsys):
    mod = _load()
    handler, tasks = _fake_kie()

    def make(credit_usd):
        p = SunoProvider(ProviderConfig(api_key="unit-test-not-a-key", base_url="https://kie.test", timeout=5.0),
                         routes="jobs", credit_usd=credit_usd)
        p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        async def no_sleep(_s):
            return None

        p.kie._sleep = no_sleep
        return p

    out = tmp_path / "probe"
    code = await mod.run(["--spend", "--ops", "generate,extend,cover,lyrics,wav,mp4", "--out", str(out)], make_provider=make)
    printed = capsys.readouterr().out
    assert code == 1  # mp4 was not recognised
    assert "unit-test-not-a-key" not in printed

    summary = {r["op"]: r for r in json.loads((out / "summary.json").read_text(encoding="utf-8"))}
    assert summary["generate"]["status"] == "ok" and summary["generate"]["fields"] == "ok"
    assert summary["generate"]["credits"] == 12 and summary["generate"]["cost_usd"] == 0.06
    assert summary["extend"]["status"] == "ok"
    assert summary["cover"]["status"] == "skipped" and "--audio-url" in summary["cover"]["fields"]
    assert summary["lyrics"]["status"] == "ok" and summary["wav"]["status"] == "ok"
    assert summary["mp4"]["status"] == "unrecognised"
    assert "raw resultJson (first 300 chars)" in printed and "unexpected" in printed

    # the later steps used the song generate made
    req = json.loads((out / "request_wav.json").read_text(encoding="utf-8"))
    assert req["input"] == {"task_id": "job-1", "audio_id": "aud-A"}
    assert json.loads((out / "request_extend.json").read_text(encoding="utf-8"))["input"]["audio_id"] == "aud-A"
    # raw polls, parsed results and files are on disk
    assert (out / "record_generate_01.json").is_file() and (out / "record_mp4_01.json").is_file()
    gen = json.loads((out / "result_generate.json").read_text(encoding="utf-8"))
    assert gen["result"]["metadata"]["audio_ids"] == ["aud-A", "aud-B"] and gen["balance_before"] is not None
    assert (out / "generate_1.mp3").is_file() and (out / "generate_2.mp3").is_file()
    assert "error" in json.loads((out / "result_mp4.json").read_text(encoding="utf-8"))
    assert list(tasks.values()) == ["ai-music-api/generate", "ai-music-api/extend", "ai-music-api/generate-lyrics",
                                    "ai-music-api/convert-to-wav-format", "ai-music-api/create-music-video"]


async def test_without_spend_nothing_is_called(tmp_path, capsys):
    mod = _load()

    def boom(_credit_usd):
        raise AssertionError("no provider may be built on a dry run")

    assert await mod.run(["--ops", "generate,wav"], make_provider=boom) == 0
    assert "dry run" in capsys.readouterr().out


async def test_unknown_op_is_refused(capsys):
    mod = _load()
    assert await mod.run(["--ops", "generate,karaoke"]) == 2
    assert "karaoke" in capsys.readouterr().out
