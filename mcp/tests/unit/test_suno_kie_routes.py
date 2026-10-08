"""Suno over kie.ai: which operation goes where, and what goes on the wire.

Two routes per operation (see ``SUNO_JOBS_MODELS`` in capabilities/music.py):

- ``legacy`` — the old per-operation endpoints; the request bodies here are
  pinned byte-for-byte to what the provider sent before the move, so the
  default behaviour cannot drift.
- ``jobs`` — kie's unified createTask/recordInfo; request bodies follow the
  new documentation page of each operation. The ``resultJson`` shapes used for
  the answers are ASSUMPTIONS (no kie page documents them for Suno): the
  documented generic ``{"resultUrls": [...]}`` and the documented callback
  payload shape.

All traffic goes to ``httpx.MockTransport``; the key is a placeholder.
"""

import json
from types import SimpleNamespace

import httpx
import pytest

from omniapi_mcp.capabilities.music import (
    SUNO_DEFAULT_ROUTES,
    SUNO_JOBS_MODELS,
    SUNO_LEGACY_ONLY,
    SunoProvider,
    suno_routes,
)
from omniapi_mcp.config.settings import KieSettings, Settings
from omniapi_mcp.providers.base import ProviderConfig, ProviderError
from omniapi_mcp.tools.music_generation import MusicGenerationTool

FAKE_KEY = "unit-test-not-a-key"
BASE = "https://kie.test"
CB = "https://example.com/omniapi-mcp/suno-callback"
CREATE = "/api/v1/jobs/createTask"
RECORD = "/api/v1/jobs/recordInfo"


class Fake:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.answers: dict[str, list] = {}

    def on(self, key: str, *answers):
        self.answers.setdefault(key, []).extend(answers)
        return self

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = request.url.path if request.url.host == "kie.test" else str(request.url)
        queue = self.answers.get(key)
        if not queue:
            return httpx.Response(404, json={"code": 404, "msg": f"unscripted {key}"})
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        status, body = answer
        if isinstance(body, bytes):
            return httpx.Response(status, content=body)
        return httpx.Response(status, json=body)

    def posted(self, path: str) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if r.method == "POST" and r.url.path == path]

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests if r.url.host == "kie.test"]


def provider(fake: Fake, routes=None, **kw) -> SunoProvider:
    p = SunoProvider(ProviderConfig(api_key=FAKE_KEY, base_url=BASE, timeout=30.0), routes=routes, **kw)
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(fake))

    async def no_sleep(_s):
        return None

    p.kie._sleep = no_sleep
    return p


def ok(data):
    return (200, {"code": 200, "msg": "success", "data": data})


def jobs_success(result, credits=None):
    data = {"taskId": "job-1", "model": "x", "state": "success",
            "resultJson": result if isinstance(result, str) else json.dumps(result)}
    if credits is not None:
        data["creditsConsumed"] = credits
    return ok(data)


TRACKS = [
    {"id": "aud-A", "audio_url": "https://cdn.test/a.mp3", "title": "Night", "duration": 198.4, "tags": "rock"},
    {"id": "aud-B", "audio_url": "https://cdn.test/b.mp3", "title": "Night", "duration": 201.0, "tags": "rock"},
]


def jobs_fake(result, credits=None, files=("https://cdn.test/a.mp3",)):
    fake = Fake().on(CREATE, ok({"taskId": "job-1"})).on(RECORD, ok({"taskId": "job-1", "state": "generating"}),
                                                          jobs_success(result, credits))
    for f in files:
        fake.on(f, (200, b"BYTES:" + f.encode()))
    return fake


# ---- the route table -------------------------------------------------------


class TestRouteTable:
    def test_verified_operations_default_to_jobs(self):
        # all but upload_extend were run once with a real key (2026-10-05/06)
        assert {op for op, r in SUNO_DEFAULT_ROUTES.items() if r == "jobs"} == {
            "generate", "extend", "cover", "add_instrumental", "add_vocals", "generate_lyrics", "to_wav", "to_mp4"}
        assert SUNO_DEFAULT_ROUTES["upload_extend"] == "legacy"
        assert set(SUNO_DEFAULT_ROUTES) == set(SUNO_JOBS_MODELS) | set(SUNO_LEGACY_ONLY)

    def test_new_model_values_match_the_docs(self):
        assert SUNO_JOBS_MODELS == {
            "generate": "ai-music-api/generate",
            "extend": "ai-music-api/extend",
            "cover": "ai-music-api/upload-and-cover-audio",
            "upload_extend": "ai-music-api/upload-and-extend-audio",
            "add_instrumental": "ai-music-api/add-instrumental",
            "add_vocals": "ai-music-api/add-vocals",
            "generate_lyrics": "ai-music-api/generate-lyrics",
            "to_wav": "ai-music-api/convert-to-wav-format",
            "to_mp4": "ai-music-api/create-music-video",
        }
        assert set(SUNO_LEGACY_ONLY) == {"separate_vocals", "get_timestamped_lyrics"}

    @pytest.mark.parametrize("spec", ["legacy", "old", "none"])
    def test_specs_that_keep_everything_old(self, spec):
        assert set(suno_routes(spec).values()) == {"legacy"}

    @pytest.mark.parametrize("spec", ["", "default", None])
    def test_default_spec_is_the_built_in_table(self, spec):
        assert suno_routes(spec) == SUNO_DEFAULT_ROUTES

    @pytest.mark.parametrize("spec", ["jobs", "new", "all", "JOBS"])
    def test_all_jobs(self, spec):
        r = suno_routes(spec)
        assert {op for op, v in r.items() if v == "jobs"} == set(SUNO_JOBS_MODELS)
        assert r["separate_vocals"] == r["get_timestamped_lyrics"] == "legacy"

    def test_comma_list_aliases_and_ignored_names(self, caplog):
        r = suno_routes("upload_extend, lyrics,wav,separate_vocals,bogus")
        assert {op for op, v in r.items() if v == "jobs"} == {op for op, v in SUNO_DEFAULT_ROUTES.items() if v == "jobs"} | {"upload_extend"}
        assert "separate_vocals" in caplog.text and "bogus" in caplog.text


# ---- legacy route: pinned to the pre-move request bodies -------------------


class TestLegacyBodiesUnchanged:
    async def test_generate(self):
        fake = Fake().on("/api/v1/generate", ok({"taskId": "old-1"})).on(
            "/api/v1/generate/record-info",
            ok({"status": "TEXT_SUCCESS"}),
            ok({"status": "SUCCESS", "response": {"sunoData": [
                {"id": "aud-A", "audioUrl": "https://cdn.test/a.mp3", "title": "T", "duration": 3.0, "tags": "x"},
                {"id": "aud-B", "audioUrl": "https://cdn.test/b.mp3"}]}}),
        ).on("https://cdn.test/a.mp3", (200, b"ID3"))
        p = provider(fake, routes="legacy")
        r = await p.generate("a song", model="V6", instrumental=True, style="pop", title="T", customMode=True,
                             vocalGender="f", negativeTags="metal", styleWeight=0.5, personaId="p1")
        assert fake.posted("/api/v1/generate") == [{
            "prompt": "a song", "model": "V6", "customMode": True, "instrumental": True, "callBackUrl": CB,
            "style": "pop", "title": "T", "vocalGender": "f", "negativeTags": "metal", "styleWeight": 0.5,
            "personaId": "p1",
        }]
        assert r.audio_data == b"ID3"
        assert r.metadata["task_id"] == "old-1" and r.metadata["audio_id"] == "aud-A"
        assert r.metadata["audio_ids"] == ["aud-A", "aud-B"] and "cost_usd" not in r.metadata
        assert CREATE not in " ".join(fake.paths())

    async def test_extend_without_custom_mode(self):
        fake = Fake().on("/api/v1/generate/extend", ok({"taskId": "old-2"})).on(
            "/api/v1/generate/record-info",
            ok({"status": "SUCCESS", "response": {"sunoData": [{"id": "x", "audioUrl": "https://cdn.test/e.mp3"}]}}),
        ).on("https://cdn.test/e.mp3", (200, b"E"))
        await provider(fake, routes="legacy").extend(audio_id="aud-A")
        assert fake.posted("/api/v1/generate/extend") == [
            {"audioId": "aud-A", "model": "V6", "defaultParamFlag": False, "callBackUrl": CB}]

    async def test_wav(self):
        fake = Fake().on("/api/v1/wav/generate", ok({"taskId": "w"})).on(
            "/api/v1/wav/record-info",
            ok({"successFlag": "PENDING"}),
            ok({"successFlag": "SUCCESS", "response": {"audioWavUrl": "https://cdn.test/a.wav"}}),
        ).on("https://cdn.test/a.wav", (200, b"RIFF"))
        r = await provider(fake, routes="legacy").to_wav(task_id="t0", audio_id="aud-A")
        assert fake.posted("/api/v1/wav/generate") == [{"taskId": "t0", "audioId": "aud-A", "callBackUrl": CB}]
        assert r.audio_data == b"RIFF" and r.output_format == "wav"

    async def test_lyrics(self):
        fake = Fake().on("/api/v1/lyrics", ok({"taskId": "l"})).on(
            "/api/v1/lyrics/record-info",
            ok({"status": "SUCCESS", "response": {"data": [{"text": "[Verse] hi", "title": "Hi"}]}}),
        )
        r = await provider(fake, routes="legacy").generate_lyrics(prompt="about rain")
        assert fake.posted("/api/v1/lyrics") == [{"prompt": "about rain", "callBackUrl": CB}]
        assert r.text == "[Verse] hi" and r.metadata["title"] == "Hi"

    async def test_failure_status_is_reported(self):
        fake = Fake().on("/api/v1/generate", ok({"taskId": "old-3"})).on(
            "/api/v1/generate/record-info",
            ok({"status": "GENERATE_AUDIO_FAILED", "errorMessage": "upstream broke", "errorCode": None}),
        )
        with pytest.raises(ProviderError) as ei:
            await provider(fake, routes="legacy").generate("x")
        assert "upstream broke" in str(ei.value) and ei.value.error_code == "GENERATION_FAILED"


# ---- jobs route: request bodies per the new pages --------------------------


class TestJobsRequests:
    async def test_generate(self):
        fake = jobs_fake({"data": TRACKS}, credits=12)
        p = provider(fake, routes="jobs")
        r = await p.generate("a song", model="V6_MINI", instrumental=False, style="pop", title="T",
                             customMode=True, vocalGender="m", negativeTags="metal", styleWeight=0.65,
                             weirdnessConstraint=0.3, audioWeight=0.2, personaId="p1", personaModel="style_persona")
        assert fake.posted(CREATE) == [{"model": "ai-music-api/generate", "input": {
            "prompt": "a song", "model": "V6_MINI", "custom_mode": True, "instrumental": False, "style": "pop",
            "title": "T", "vocal_gender": "m", "negative_tags": "metal", "style_weight": 0.65,
            "weirdness_constraint": 0.3, "audio_weight": 0.2, "persona_id": "p1", "persona_model": "style_persona",
        }}]  # no callBackUrl: optional on createTask and we poll
        assert r.audio_data == b"BYTES:https://cdn.test/a.mp3"
        assert r.metadata["task_id"] == "job-1" and r.metadata["audio_id"] == "aud-A"
        assert r.metadata["audio_ids"] == ["aud-A", "aud-B"] and r.metadata["duration"] == 198.4
        assert r.metadata["cost_usd"] == pytest.approx(0.06)  # 12 credits x 0.005
        assert r.metadata["route"] == "jobs"

    async def test_generate_passes_a_callers_callback(self):
        fake = jobs_fake({"data": TRACKS})
        await provider(fake, routes="generate").generate("x", callBackUrl="https://mine.test/cb")
        assert fake.posted(CREATE)[0]["callBackUrl"] == "https://mine.test/cb"

    async def test_extend_inherit_and_custom(self):
        fake = jobs_fake({"data": TRACKS})
        p = provider(fake, routes="extend")
        await p.extend(audio_id="aud-A", model="V6")
        await p.extend(audio_id="aud-A", default_param_flag=True, prompt="more", style="pop", title="T2",
                       continue_at=42.5)
        assert [b["input"] for b in fake.posted(CREATE)] == [
            {"audio_id": "aud-A", "model": "V6"},
            {"audio_id": "aud-A", "model": "V6", "prompt": "more", "style": "pop", "title": "T2", "continue_at": 42.5},
        ]
        assert {b["model"] for b in fake.posted(CREATE)} == {"ai-music-api/extend"}

    async def test_cover(self):
        fake = jobs_fake({"data": TRACKS})
        await provider(fake, routes="cover").cover(upload_url="https://src.test/a.mp3", prompt="la la",
                                                   instrumental=True, style="jazz", title="C")
        assert fake.posted(CREATE) == [{"model": "ai-music-api/upload-and-cover-audio", "input": {
            "upload_url": "https://src.test/a.mp3", "prompt": "la la", "instrumental": True, "model": "V6",
            "style": "jazz", "title": "C"}}]

    async def test_upload_extend(self):
        fake = jobs_fake({"data": TRACKS})
        await provider(fake, routes="upload_extend").upload_extend(
            upload_url="https://src.test/a.mp3", default_param_flag=True, prompt="p", style="s", title="t",
            continue_at=10)
        assert fake.posted(CREATE) == [{"model": "ai-music-api/upload-and-extend-audio", "input": {
            "upload_url": "https://src.test/a.mp3", "instrumental": False, "model": "V6",
            "prompt": "p", "style": "s", "title": "t", "continue_at": 10}}]

    async def test_add_instrumental_drops_undocumented_persona(self):
        fake = jobs_fake({"data": TRACKS})
        await provider(fake, routes="add_instrumental").add_instrumental(
            upload_url="https://src.test/v.mp3", title="T", tags="piano", negative_tags="metal",
            vocalGender="f", personaId="p1")
        assert fake.posted(CREATE) == [{"model": "ai-music-api/add-instrumental", "input": {
            "upload_url": "https://src.test/v.mp3", "title": "T", "tags": "piano", "negative_tags": "metal",
            "model": "V6", "vocal_gender": "f"}}]

    async def test_add_vocals(self):
        fake = jobs_fake({"data": TRACKS})
        await provider(fake, routes="add_vocals").add_vocals(
            upload_url="https://src.test/i.mp3", prompt="sing", title="T", style="soul", negative_tags="rap",
            model="V6_WILD")
        assert fake.posted(CREATE) == [{"model": "ai-music-api/add-vocals", "input": {
            "prompt": "sing", "title": "T", "negative_tags": "rap", "style": "soul",
            "upload_url": "https://src.test/i.mp3", "model": "V6_WILD"}}]

    async def test_lyrics(self):
        fake = jobs_fake({"data": [{"text": "[Verse] a", "title": "A", "status": "complete"},
                                   {"text": "[Verse] b", "title": "B", "status": "complete"}]}, credits=1, files=())
        r = await provider(fake, routes="lyrics").generate_lyrics(prompt="rain")
        assert fake.posted(CREATE) == [{"model": "ai-music-api/generate-lyrics", "input": {"prompt": "rain"}}]
        assert r.text == "[Verse] a" and r.metadata["variants"] == ["[Verse] a", "[Verse] b"]
        assert r.metadata["cost_usd"] == pytest.approx(0.005)

    async def test_lyrics_in_result_object(self):
        fake = jobs_fake({"resultObject": {"data": [{"text": "x", "title": "X"}]}}, files=())
        r = await provider(fake, routes="lyrics").generate_lyrics(prompt="rain")
        assert r.text == "x"

    async def test_wav_from_result_urls(self):
        fake = jobs_fake({"resultUrls": ["https://cdn.test/a.wav"]}, credits=2, files=("https://cdn.test/a.wav",))
        r = await provider(fake, routes="wav").to_wav(task_id="t0", audio_id="aud-A")
        assert fake.posted(CREATE) == [{"model": "ai-music-api/convert-to-wav-format",
                                        "input": {"task_id": "t0", "audio_id": "aud-A"}}]
        assert r.output_format == "wav" and r.audio_data == b"BYTES:https://cdn.test/a.wav"
        assert r.metadata["source_task_id"] == "t0" and r.metadata["cost_usd"] == pytest.approx(0.01)

    async def test_mp4(self):
        fake = jobs_fake({"video_url": "https://cdn.test/v.mp4"}, files=("https://cdn.test/v.mp4",))
        r = await provider(fake, routes="mp4").to_mp4(task_id="t0", audio_id="aud-A", author="Me",
                                                     domain_name="me.test")
        assert fake.posted(CREATE) == [{"model": "ai-music-api/create-music-video", "input": {
            "task_id": "t0", "audio_id": "aud-A", "author": "Me", "domain_name": "me.test"}}]
        assert r.output_format == "mp4" and r.metadata["type"] == "video"


class TestJobsResults:
    async def test_result_urls_only_falls_back_to_old_record_for_ids(self):
        fake = jobs_fake({"resultUrls": ["https://cdn.test/b.mp3"]}, files=("https://cdn.test/b.mp3",)).on(
            "/api/v1/generate/record-info",
            ok({"status": "SUCCESS", "response": {"sunoData": [
                {"id": "aud-A", "audioUrl": "https://cdn.test/a.mp3"},
                {"id": "aud-B", "audioUrl": "https://cdn.test/b.mp3", "title": "B"}]}}),
        )
        r = await provider(fake, routes="generate").generate("x")
        assert r.audio_data == b"BYTES:https://cdn.test/b.mp3"
        assert r.metadata["audio_id"] == "aud-B" and r.metadata["title"] == "B"  # matched by URL, not by order

    async def test_result_urls_only_without_old_record_still_saves_the_file(self):
        fake = jobs_fake({"resultUrls": ["https://cdn.test/a.mp3"]})
        r = await provider(fake, routes="generate").generate("x")
        assert r.audio_data and r.metadata["audio_id"] is None
        assert r.metadata["all_tracks"] == ["https://cdn.test/a.mp3"]

    async def test_old_record_shape_inside_result_json(self):
        fake = jobs_fake({"response": {"sunoData": [{"id": "aud-Z", "audioUrl": "https://cdn.test/a.mp3"}]}})
        r = await provider(fake, routes="generate").generate("x")
        assert r.metadata["audio_id"] == "aud-Z"

    async def test_unrecognised_result_names_the_task(self):
        fake = jobs_fake({"something": "else"}, files=())
        with pytest.raises(ProviderError) as ei:
            await provider(fake, routes="generate").generate("x")
        assert ei.value.error_code == "UNRECOGNISED_RESULT" and "job-1" in str(ei.value)

    async def test_task_failure(self):
        fake = Fake().on(CREATE, ok({"taskId": "job-9"})).on(
            RECORD, ok({"taskId": "job-9", "state": "fail", "failCode": "400", "failMsg": "Lyrics contained copyrighted material"}))
        with pytest.raises(ProviderError) as ei:
            await provider(fake, routes="generate").generate("x")
        assert "copyrighted" in str(ei.value) and ei.value.provider_name == "suno"

    async def test_insufficient_credits_at_create(self):
        fake = Fake().on(CREATE, (200, {"code": 402, "msg": "Insufficient credits"}))
        with pytest.raises(ProviderError) as ei:
            await provider(fake, routes="generate").generate("x")
        assert ei.value.error_code == "INSUFFICIENT_CREDITS"
        assert len(fake.requests) == 1

    async def test_custom_credit_price(self):
        fake = jobs_fake({"data": TRACKS}, credits=10)
        r = await provider(fake, routes="generate", credit_usd=0.01).generate("x")
        assert r.metadata["cost_usd"] == pytest.approx(0.1)


class TestLegacyOnlyOperations:
    async def test_separate_vocals_stays_old_even_when_all_jobs(self):
        fake = Fake().on("/api/v1/vocal-removal/generate", ok({"taskId": "s"})).on(
            "/api/v1/vocal-removal/record-info",
            ok({"successFlag": "SUCCESS", "response": {"vocalUrl": "https://cdn.test/v.mp3",
                                                       "instrumentalUrl": "https://cdn.test/i.mp3"}}),
        ).on("https://cdn.test/v.mp3", (200, b"V")).on("https://cdn.test/i.mp3", (200, b"I"))
        r = await provider(fake, routes="jobs").separate_vocals(task_id="t0", audio_id="aud-A")
        assert fake.posted("/api/v1/vocal-removal/generate") == [
            {"taskId": "t0", "audioId": "aud-A", "type": "separate_vocal", "callBackUrl": CB}]
        assert r.files == {"vocal": b"V", "instrumental": b"I"}
        assert not fake.posted(CREATE)

    async def test_timestamped_lyrics_stays_old_and_sync(self):
        fake = Fake().on("/api/v1/generate/get-timestamped-lyrics",
                         ok({"alignedWords": [{"word": "hi", "startS": 1.0}], "waveformData": [0, 1]}))
        r = await provider(fake, routes="jobs").get_timestamped_lyrics(task_id="t0", audio_id="aud-A")
        assert fake.posted("/api/v1/generate/get-timestamped-lyrics") == [{"taskId": "t0", "audioId": "aud-A"}]
        assert r.data["alignedWords"][0]["word"] == "hi"


# ---- settings and wiring ---------------------------------------------------


class TestSettings:
    def test_defaults(self):
        s = KieSettings()
        assert s.request_timeout == 60.0 and s.poll_timeout == 900.0 and s.timeout is None
        assert s.suno_routes == ""

    def test_old_timeout_still_works(self):
        s = KieSettings(timeout=300)
        assert s.poll_timeout == 300.0 and s.request_timeout == 60.0
        s = KieSettings(timeout=30)
        assert s.poll_timeout == 30.0 and s.request_timeout == 30.0

    def test_explicit_new_values_win(self):
        s = KieSettings(timeout=300, poll_timeout=1200, request_timeout=90)
        assert s.poll_timeout == 1200 and s.request_timeout == 90

    def test_nonsense_old_timeout_is_ignored(self):
        s = KieSettings(timeout=0)
        assert s.poll_timeout == 900.0

    def test_env_with_only_the_old_name_loads(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)  # no stray .env
        monkeypatch.setenv("PROVIDERS__KIE__API_KEY", FAKE_KEY)
        monkeypatch.setenv("PROVIDERS__KIE__ENABLED", "true")
        monkeypatch.setenv("PROVIDERS__KIE__TIMEOUT", "420")
        s = Settings()
        assert s.providers.kie.poll_timeout == 420.0 and s.providers.kie.request_timeout == 60.0

    def test_env_new_names(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PROVIDERS__KIE__API_KEY", FAKE_KEY)
        monkeypatch.setenv("PROVIDERS__KIE__POLL_TIMEOUT", "600")
        monkeypatch.setenv("PROVIDERS__KIE__SUNO_ROUTES", "generate,wav")
        s = Settings()
        assert s.providers.kie.poll_timeout == 600.0 and s.providers.kie.suno_routes == "generate,wav"


def _tool(tmp_path, **kie):
    settings = SimpleNamespace(
        providers=SimpleNamespace(elevenlabs=None,
                                  kie=KieSettings(api_key=FAKE_KEY, enabled=True, base_url=BASE, **kie)),
        storage=SimpleNamespace(base_path=str(tmp_path)),
    )
    return MusicGenerationTool(settings=settings)


class TestToolWiring:
    def test_timeouts_and_routes_reach_the_provider(self, tmp_path):
        p = _tool(tmp_path, poll_timeout=777, request_timeout=33, suno_routes="lyrics")._suno()
        assert p.kie.poll_timeout == 777 and p.kie.request_timeout == 33
        assert p.config.timeout == 33
        assert p.route("generate_lyrics") == "jobs" and p.route("generate") == "jobs" and p.route("extend") == "jobs"
        assert p.route("upload_extend") == "legacy"
        assert p.credit_usd == pytest.approx(0.005)  # from the catalogue

    def test_old_timeout_only(self, tmp_path):
        p = _tool(tmp_path, timeout=300)._suno()
        assert p.kie.poll_timeout == 300 and p.kie.request_timeout == 60

    async def test_jobs_generate_is_saved_locally_with_cost(self, tmp_path):
        tool = _tool(tmp_path, suno_routes="generate")
        fake = jobs_fake({"data": TRACKS}, credits=12)
        tool._suno()._client = httpx.AsyncClient(transport=httpx.MockTransport(fake))

        async def no_sleep(_s):
            return None

        tool._suno().kie._sleep = no_sleep
        out = await tool.generate("a song", model="V6")
        assert out["audio_id"] == "aud-A" and out["task_id"] == "job-1"
        assert out["cost_usd"] == pytest.approx(0.06)
        from pathlib import Path

        saved = Path(out["audio_path"])
        assert saved.is_relative_to(tmp_path.resolve()) and saved.read_bytes() == b"BYTES:https://cdn.test/a.mp3"
        assert out["bytes"] == len(b"BYTES:https://cdn.test/a.mp3")

    async def test_legacy_generate_output_has_the_same_keys_as_before(self, tmp_path):
        tool = _tool(tmp_path, suno_routes="legacy")
        fake = Fake().on("/api/v1/generate", ok({"taskId": "old-1"})).on(
            "/api/v1/generate/record-info",
            ok({"status": "SUCCESS", "response": {"sunoData": [
                {"id": "aud-A", "audioUrl": "https://cdn.test/a.mp3", "title": "T", "duration": 3.0}]}}),
        ).on("https://cdn.test/a.mp3", (200, b"ID3"))
        tool._suno()._client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
        out = await tool.generate("a song")
        assert set(out) == {"audio_path", "audio_url", "provider", "operation", "task_id", "audio_id", "title",
                            "duration", "output_format", "bytes"}
