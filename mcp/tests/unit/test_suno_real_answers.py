"""Suno on kie's unified endpoints, replayed from real answers.

``tests/fixtures/kie_suno/<op>.json`` holds, per operation, the createTask
body we sent, the first and the last ``recordInfo`` answer of one real task
(V6_MINI, 2026-10-06), with ids and hosts replaced and long texts shortened.
Each test replays them through the provider on the default route table and
checks what it makes of them — so a change to the parsing is checked against
what kie really sends, not against a guess.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from omniapi_mcp.capabilities.music import SUNO_DEFAULT_ROUTES, SunoProvider
from omniapi_mcp.providers.base import ProviderConfig, ProviderError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "kie_suno"


def _load(op: str) -> dict:
    return json.loads((FIXTURES / f"{op}.json").read_text(encoding="utf-8"))


def _provider(op: str) -> tuple[SunoProvider, list[httpx.Request]]:
    fx = _load(op)
    seen: list[httpx.Request] = []
    polls = [fx["pending"], fx["final"]]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host != "kie.test":
            return httpx.Response(200, content=b"FILE:" + str(request.url).encode())
        if request.url.path == "/api/v1/jobs/createTask":
            return httpx.Response(200, json={"code": 200, "msg": "success", "data": {"taskId": fx["final"]["data"]["taskId"]}})
        if request.url.path == "/api/v1/jobs/recordInfo":
            answer = polls.pop(0) if len(polls) > 1 else polls[0]
            return httpx.Response(200, json={"code": answer["status"], "msg": "success", "data": answer["data"]})
        return httpx.Response(404, json={"code": 404, "msg": "unscripted"})

    p = SunoProvider(ProviderConfig(api_key="unit-test-not-a-key", base_url="https://kie.test", timeout=5.0))
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def no_sleep(_s):
        return None

    p.kie._sleep = no_sleep
    return p, seen


def _sent(seen: list[httpx.Request]) -> dict:
    (body,) = [json.loads(r.content) for r in seen if r.url.path == "/api/v1/jobs/createTask"]
    return body


def test_the_default_table_matches_what_was_verified():
    for op in ("generate", "extend", "cover", "add_instrumental", "add_vocals", "generate_lyrics", "to_wav", "to_mp4"):
        assert SUNO_DEFAULT_ROUTES[op] == "jobs", op
    assert SUNO_DEFAULT_ROUTES["upload_extend"] == "custom"  # custom mode worked (2026-10-08), plain mode failed upstream


@pytest.mark.parametrize("op,call", [
    ("generate", lambda p, i: p.generate(i["prompt"], model=i["model"], instrumental=i["instrumental"])),
    ("extend", lambda p, i: p.extend(audio_id=i["audio_id"], model=i["model"])),
    ("cover", lambda p, i: p.cover(upload_url=i["upload_url"], prompt=i["prompt"], model=i["model"], instrumental=True)),
    ("add_instrumental", lambda p, i: p.add_instrumental(upload_url=i["upload_url"], title=i["title"], tags=i["tags"],
                                                         negative_tags=i["negative_tags"], model=i["model"])),
    ("add_vocals", lambda p, i: p.add_vocals(upload_url=i["upload_url"], prompt=i["prompt"], title=i["title"], style=i["style"],
                                             negative_tags=i["negative_tags"], model=i["model"])),
])
async def test_song_operations(op, call):
    fx = _load(op)
    p, seen = _provider(op)
    r = await call(p, fx["request"]["input"])
    assert _sent(seen) == {"model": fx["request"]["model"], "input": fx["request"]["input"]}  # same body as the real run
    songs = json.loads(fx["final"]["data"]["resultJson"])["data"]
    assert len(songs) == 2
    md = r.metadata
    assert md["route"] == "jobs" and md["operation"] == op
    assert md["audio_id"] == songs[0]["id"] and md["duration"] == songs[0]["duration"] and md["title"] == songs[0]["title"]
    assert md["audio_ids"] == [s["id"] for s in songs]
    assert r.audio_data == b"FILE:" + songs[0]["audio_url"].encode()
    (second,) = r.extra_tracks
    assert second["audio_id"] == songs[1]["id"] and second["audio_data"] == b"FILE:" + songs[1]["audio_url"].encode()
    assert md["cost_usd"] == pytest.approx(0.06)  # creditsConsumed 12 for the job, both songs


async def test_extend_and_cover_answer_with_empty_titles():
    # seen for real: extend's first song and both cover songs come back with title ""
    for op in ("extend", "cover"):
        fx = _load(op)
        p, _ = _provider(op)
        i = fx["request"]["input"]
        r = await (p.extend(audio_id=i["audio_id"], model=i["model"]) if op == "extend"
                   else p.cover(upload_url=i["upload_url"], prompt=i["prompt"], model=i["model"], instrumental=True))
        assert r.metadata["title"] == ""


async def test_lyrics():
    fx = _load("lyrics")
    p, seen = _provider("lyrics")
    r = await p.generate_lyrics(prompt=fx["request"]["input"]["prompt"])
    items = json.loads(fx["final"]["data"]["resultJson"])["resultObject"]["lyricsData"]
    assert _sent(seen)["input"] == fx["request"]["input"]
    assert r.text == items[0]["text"] and r.metadata["title"] == items[0]["title"] == "Rain Glass"
    assert r.metadata["variants"] == [i["text"] for i in items] and len(items) == 2
    assert r.metadata["cost_usd"] == pytest.approx(0.002)  # 0.4 credits


@pytest.mark.parametrize("op,method,fmt,credits", [("wav", "to_wav", "wav", 0.4), ("mp4", "to_mp4", "mp4", 2.0)])
async def test_wav_and_mp4_come_back_as_result_urls(op, method, fmt, credits):
    fx = _load(op)
    p, seen = _provider(op)
    i = fx["request"]["input"]
    kw = {"author": i["author"]} if "author" in i else {}
    r = await getattr(p, method)(task_id=i["task_id"], audio_id=i["audio_id"], **kw)
    (url,) = json.loads(fx["final"]["data"]["resultJson"])["resultUrls"]
    assert _sent(seen)["input"] == i
    assert r.output_format == fmt and r.audio_data == b"FILE:" + url.encode() and r.metadata["route"] == "jobs"
    assert r.metadata["cost_usd"] == pytest.approx(credits * 0.005)


async def test_upload_extend_failure_as_it_really_came_back():
    # the one real task failed upstream: state fail, failCode "400", credits refunded (12 while running, 0 at the end)
    fx = _load("upload_extend")
    assert fx["pending"]["data"]["creditsConsumed"] == 12.0 and fx["final"]["data"]["creditsConsumed"] == 0.0
    p, _ = _provider("upload_extend")
    p.routes["upload_extend"] = "jobs"  # the default keeps it on the old endpoint
    with pytest.raises(ProviderError) as ei:
        await p.upload_extend(upload_url=fx["request"]["input"]["upload_url"], instrumental=True,
                              model=fx["request"]["input"]["model"])
    assert "Audio generation failed" in str(ei.value)
    # "please try again later" is an upstream failure, not a bad request from us
    assert ei.value.error_code == "GENERATION_FAILED"
