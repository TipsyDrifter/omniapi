"""kie.ai shared client: createTask / recordInfo / download / balance.

Everything runs against ``httpx.MockTransport`` — no network, no real key.
The fake key below is deliberately not shaped like a kie key.
"""

import json

import httpx
import pytest

from omniapi_mcp.generate.errors import classify
from omniapi_mcp.providers.base import ProviderError
from omniapi_mcp.providers.kie_client import (
    CREATE_TASK_PATH,
    RECORD_INFO_PATH,
    KieClient,
    credits_to_usd,
    parse_result_json,
)

FAKE_KEY = "unit-test-not-a-key"
BASE = "https://kie.test"


class Fake:
    """A scripted kie.ai: queues of answers per path, every request recorded."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.answers: dict[str, list] = {}

    def on(self, path: str, *answers):
        self.answers.setdefault(path, []).extend(answers)
        return self

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = request.url.path if request.url.host == "kie.test" else str(request.url)
        queue = self.answers.get(key)
        if not queue:
            return httpx.Response(599, text=f"no answer scripted for {key}")
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, httpx.Response):
            return answer
        status, body = answer
        if isinstance(body, (bytes, str)):
            return httpx.Response(status, content=body if isinstance(body, bytes) else body.encode())
        return httpx.Response(status, json=body)

    def bodies(self, path: str) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if r.url.path == path and r.method == "POST"]


def make_client(fake: Fake, **kw) -> tuple[KieClient, list[float]]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    client = KieClient(FAKE_KEY, base_url=BASE, http=lambda: http, provider_name="suno", **kw)
    slept: list[float] = []

    async def no_sleep(s: float) -> None:
        slept.append(s)

    client._sleep = no_sleep
    return client, slept


def created(task_id: str = "task-1"):
    return (200, {"code": 200, "msg": "success", "data": {"taskId": task_id}})


def record(state: str, **extra):
    return (200, {"code": 200, "msg": "success", "data": {"taskId": "task-1", "model": "m", "state": state, **extra}})


class TestCreateTask:
    async def test_body_auth_and_url(self):
        fake = Fake().on(CREATE_TASK_PATH, created("abc"))
        client, _ = make_client(fake)
        task_id = await client.create_task("vendor/model", {"prompt": "hi", "n": 1})
        assert task_id == "abc"
        req = fake.requests[0]
        assert str(req.url) == f"{BASE}{CREATE_TASK_PATH}"
        assert req.headers["authorization"] == f"Bearer {FAKE_KEY}"
        assert json.loads(req.content) == {"model": "vendor/model", "input": {"prompt": "hi", "n": 1}}

    async def test_callback_only_when_given(self):
        fake = Fake().on(CREATE_TASK_PATH, created())
        client, _ = make_client(fake)
        await client.create_task("m", {}, callback_url="https://cb.test/x")
        assert json.loads(fake.requests[0].content)["callBackUrl"] == "https://cb.test/x"

    async def test_request_timeout_is_per_request(self):
        fake = Fake().on(CREATE_TASK_PATH, created())
        client, _ = make_client(fake, request_timeout=12.0, poll_timeout=999.0)
        await client.create_task("m", {})
        assert fake.requests[0].extensions["timeout"]["read"] == 12.0

    async def test_402_in_body_is_insufficient_credits(self):
        fake = Fake().on(CREATE_TASK_PATH, (200, {"code": 402, "msg": "Credits insufficient", "data": None}))
        client, slept = make_client(fake)
        with pytest.raises(ProviderError) as ei:
            await client.create_task("m", {})
        assert ei.value.error_code == "INSUFFICIENT_CREDITS"
        assert classify(ei.value) == "quota"
        assert len(fake.requests) == 1 and slept == []  # not retried

    async def test_402_as_http_status(self):
        fake = Fake().on(CREATE_TASK_PATH, (402, {"code": 402, "msg": "no credits"}))
        client, _ = make_client(fake)
        with pytest.raises(ProviderError) as ei:
            await client.create_task("m", {})
        assert ei.value.error_code == "INSUFFICIENT_CREDITS"

    async def test_429_is_retried_then_succeeds(self):
        limited = (200, {"code": 429, "msg": "Rate limit exceeded"})
        fake = Fake().on(CREATE_TASK_PATH, limited, limited, created("later"))
        client, slept = make_client(fake, max_retries=3)
        assert await client.create_task("m", {}) == "later"
        assert len(fake.bodies(CREATE_TASK_PATH)) == 3
        assert slept == [client.rate_limit_wait, client.rate_limit_wait]

    async def test_429_gives_up_after_max_retries(self):
        fake = Fake().on(CREATE_TASK_PATH, (429, {"code": 429, "msg": "Rate limit exceeded"}))
        client, _ = make_client(fake, max_retries=2)
        with pytest.raises(ProviderError) as ei:
            await client.create_task("m", {})
        assert ei.value.error_code == "RATE_LIMITED"
        assert classify(ei.value) == "quota"
        assert len(fake.requests) == 3

    async def test_401_is_auth(self):
        fake = Fake().on(CREATE_TASK_PATH, (401, {"code": 401, "msg": "Unauthorized"}))
        client, _ = make_client(fake)
        with pytest.raises(ProviderError) as ei:
            await client.create_task("m", {})
        assert ei.value.error_code == "AUTH_FAILED" and classify(ei.value) == "auth"

    async def test_422_is_invalid(self):
        fake = Fake().on(CREATE_TASK_PATH, (200, {"code": 422, "msg": "custom_mode is required"}))
        client, _ = make_client(fake)
        with pytest.raises(ProviderError) as ei:
            await client.create_task("m", {})
        assert ei.value.error_code == "INVALID_REQUEST" and classify(ei.value) == "invalid"

    async def test_missing_task_id(self):
        fake = Fake().on(CREATE_TASK_PATH, (200, {"code": 200, "data": {}}))
        client, _ = make_client(fake)
        with pytest.raises(ProviderError) as ei:
            await client.create_task("m", {})
        assert ei.value.error_code == "BAD_RESPONSE"

    async def test_connection_timeout(self):
        fake = Fake().on(CREATE_TASK_PATH, httpx.ReadTimeout("slow"))
        client, _ = make_client(fake)
        with pytest.raises(ProviderError) as ei:
            await client.create_task("m", {})
        assert ei.value.error_code == "TIMEOUT" and classify(ei.value) == "timeout"


class TestWaitTask:
    async def test_states_then_success_with_string_result_json(self):
        fake = Fake().on(
            RECORD_INFO_PATH,
            record("waiting"),
            record("queuing"),
            record("generating"),
            record("success", resultJson='{"resultUrls":["https://cdn.test/a.mp3"]}', creditsConsumed=12,
                   costTime=15000),
        )
        client, slept = make_client(fake)
        task = await client.wait_task("task-1")
        assert task.result == {"resultUrls": ["https://cdn.test/a.mp3"]}
        assert task.result_raw == '{"resultUrls":["https://cdn.test/a.mp3"]}'
        assert task.credits_consumed == 12.0 and task.cost_time_ms == 15000
        assert fake.requests[0].url.params["taskId"] == "task-1"
        assert len(slept) == 3 and slept[0] == client.poll_initial and slept[1] > slept[0]  # backoff

    async def test_result_json_already_an_object(self):
        fake = Fake().on(RECORD_INFO_PATH, record("success", resultJson={"resultUrls": ["u"]}))
        client, _ = make_client(fake)
        assert (await client.wait_task("t")).result == {"resultUrls": ["u"]}

    async def test_fail_with_numeric_fail_code(self):
        fake = Fake().on(RECORD_INFO_PATH, record("fail", failCode="402", failMsg="Insufficient balance"))
        client, _ = make_client(fake)
        with pytest.raises(ProviderError) as ei:
            await client.wait_task("task-1")
        assert ei.value.error_code == "INSUFFICIENT_CREDITS" and classify(ei.value) == "quota"
        assert "Insufficient balance" in str(ei.value) and "task-1" in str(ei.value)

    async def test_fail_with_text_reason_keeps_it(self):
        fake = Fake().on(RECORD_INFO_PATH, record("fail", failCode="SENSITIVE_WORD_ERROR",
                                                  failMsg="Lyrics contain sensitive words"))
        client, _ = make_client(fake)
        with pytest.raises(ProviderError) as ei:
            await client.wait_task("task-1")
        assert ei.value.error_code == "KIE_FAIL_SENSITIVE_WORD_ERROR"
        assert classify(ei.value) == "rejected"

    async def test_fail_501_is_generation_failed(self):
        fake = Fake().on(RECORD_INFO_PATH, record("fail", failCode="501", failMsg="Generation failed"))
        client, _ = make_client(fake)
        with pytest.raises(ProviderError) as ei:
            await client.wait_task("task-1")
        assert ei.value.error_code == "KIE_FAIL_501"

    async def test_poll_ceiling_is_separate_from_request_timeout(self):
        fake = Fake().on(RECORD_INFO_PATH, record("generating"))
        client, slept = make_client(fake, request_timeout=5.0, poll_timeout=40.0)
        with pytest.raises(ProviderError) as ei:
            await client.wait_task("task-1")
        assert ei.value.error_code == "TIMEOUT" and classify(ei.value) == "timeout"
        assert sum(slept) == pytest.approx(40.0)
        assert all(r.extensions["timeout"]["read"] == 5.0 for r in fake.requests)

    async def test_poll_429_slows_down_and_continues(self):
        fake = Fake().on(RECORD_INFO_PATH, (429, {"code": 429, "msg": "Rate limit exceeded"}),
                         record("success", resultJson='{"resultUrls":[]}'))
        client, slept = make_client(fake)
        await client.wait_task("t")
        assert slept[0] == client.rate_limit_wait

    async def test_record_null_right_after_creation_is_tolerated(self):
        null = (200, {"code": 422, "msg": "recordInfo is null", "data": None})
        fake = Fake().on(RECORD_INFO_PATH, null, null, record("success", resultJson="{}"))
        client, _ = make_client(fake)
        assert (await client.wait_task("t")).result == {}

    async def test_record_null_forever_fails(self):
        fake = Fake().on(RECORD_INFO_PATH, (200, {"code": 422, "msg": "recordInfo is null", "data": None}))
        client, _ = make_client(fake)
        with pytest.raises(ProviderError) as ei:
            await client.wait_task("t")
        assert ei.value.error_code == "INVALID_REQUEST"

    async def test_transient_network_error_is_retried(self):
        fake = Fake().on(RECORD_INFO_PATH, httpx.ConnectError("blip"), record("success", resultJson="{}"))
        client, _ = make_client(fake)
        assert (await client.wait_task("t")).result == {}

    async def test_persistent_network_error_gives_up(self):
        fake = Fake().on(RECORD_INFO_PATH, httpx.ConnectError("down"))
        client, _ = make_client(fake)
        with pytest.raises(ProviderError) as ei:
            await client.wait_task("t")
        assert ei.value.error_code == "REQUEST_FAILED"
        assert len(fake.requests) == client.poll_error_budget + 1

    async def test_auth_error_while_polling_is_not_retried(self):
        fake = Fake().on(RECORD_INFO_PATH, (200, {"code": 401, "msg": "Unauthorized"}))
        client, _ = make_client(fake)
        with pytest.raises(ProviderError) as ei:
            await client.wait_task("t")
        assert ei.value.error_code == "AUTH_FAILED" and len(fake.requests) == 1


class TestLegacyAndMisc:
    async def test_legacy_poll_success_and_failure(self):
        path = "/api/v1/generate/record-info"
        fake = Fake().on(path, (200, {"code": 200, "data": {"status": "PENDING"}}),
                         (200, {"code": 200, "data": {"status": "SUCCESS", "response": {"x": 1}}}))
        client, _ = make_client(fake)
        data = await client.legacy_poll(path, "t", status_field="status", pending={"PENDING"})
        assert data["response"] == {"x": 1}

        fake2 = Fake().on(path, (200, {"code": 200, "data": {"status": "SENSITIVE_WORD_ERROR",
                                                            "errorMessage": "blocked words"}}))
        client2, _ = make_client(fake2)
        with pytest.raises(ProviderError) as ei:
            await client2.legacy_poll(path, "t", status_field="status", pending={"PENDING"})
        assert "blocked words" in str(ei.value) and classify(ei.value) == "rejected"

    async def test_legacy_submit_reads_body_code(self):
        fake = Fake().on("/api/v1/generate", (200, {"code": 402, "msg": "insufficient"}))
        client, _ = make_client(fake)
        with pytest.raises(ProviderError) as ei:
            await client.legacy_submit("/api/v1/generate", {"a": 1})
        assert ei.value.error_code == "INSUFFICIENT_CREDITS"

    async def test_download_has_no_auth_header(self):
        fake = Fake().on("https://cdn.test/song.mp3", (200, b"ID3audio"))
        client, _ = make_client(fake)
        assert await client.download("https://cdn.test/song.mp3") == b"ID3audio"
        assert "authorization" not in fake.requests[0].headers

    async def test_download_failure(self):
        fake = Fake().on("https://cdn.test/gone.mp3", (404, b"nope"))
        client, _ = make_client(fake)
        with pytest.raises(ProviderError) as ei:
            await client.download("https://cdn.test/gone.mp3")
        assert ei.value.error_code == "DOWNLOAD_FAILED"

    async def test_credits(self):
        fake = Fake().on("/api/v1/chat/credit", (200, {"code": 200, "msg": "success", "data": 812.5}))
        client, _ = make_client(fake)
        assert await client.credits() == 812.5

    def test_helpers(self):
        assert parse_result_json('{"a":1}') == {"a": 1}
        assert parse_result_json("") is None and parse_result_json("not json") is None
        assert parse_result_json(None) is None
        assert credits_to_usd(12, 0.005) == 0.06
        assert credits_to_usd(None, 0.005) is None and credits_to_usd(3, None) is None

    def test_client_has_no_model_family_wording(self):
        import inspect

        from omniapi_mcp.providers import kie_client

        assert "suno" not in inspect.getsource(kie_client).lower()
