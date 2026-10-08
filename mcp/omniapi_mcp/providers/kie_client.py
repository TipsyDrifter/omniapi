"""kie.ai HTTP client — shared by every modality that goes through kie.

kie.ai is an aggregator. Since its 2026 documentation rewrite every model is
reached through one pair of endpoints (docs.kie.ai/market/quickstart.md):

- ``POST /api/v1/jobs/createTask`` with ``{"model", "input", "callBackUrl"?}``
  returns ``data.taskId``;
- ``GET /api/v1/jobs/recordInfo?taskId=`` reports ``data.state`` (waiting /
  queuing / generating / success / fail), and on success ``data.resultJson``,
  a JSON *string* that has to be parsed again
  (docs.kie.ai/market/common/get-task-detail.md).

The older per-model endpoints (``/api/v1/generate`` + ``/generate/record-info``
and friends, now documented under ``docs.kie.ai/old-model/...``) are still
reachable through :meth:`KieClient.legacy_submit` / :meth:`KieClient.legacy_poll`
so a caller can keep an operation on the old route while the new one is
unverified.

kie answers many errors with HTTP 200 and its own ``code`` in the body, so
both are read. Failures become ``ProviderError`` with a message the generation
error classifier (``generate/errors.py``) understands — "insufficient credits"
for 402, "rate limit" for 429, "timed out", "unauthorized" — and a stable
``error_code``.

Nothing here knows which model family is calling: messages say "kie.ai", and
the provider name passed in is what errors are filed under.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx

from .base import ProviderError

logger = logging.getLogger(__name__)

DEFAULT_BASE = "https://api.kie.ai"
CREATE_TASK_PATH = "/api/v1/jobs/createTask"
RECORD_INFO_PATH = "/api/v1/jobs/recordInfo"
#: read-only balance query (docs.kie.ai/common-api/get-account-credits.md)
CREDIT_PATH = "/api/v1/chat/credit"

#: get-task-detail.md "Task States"
PENDING_STATES = frozenset({"waiting", "queuing", "generating"})
SUCCESS_STATE = "success"
FAIL_STATE = "fail"

#: Fallback USD per kie credit. The catalogue carries the same figure as
#: ``pricing.credit_usd`` on every kie model (catalog.json); callers should
#: prefer that value and use this only when the catalogue has none.
DEFAULT_CREDIT_USD = 0.005


@dataclass
class KieTask:
    """A finished (``state == "success"``) task from ``recordInfo``."""

    task_id: str
    model: str | None
    result: Any  # resultJson, parsed (dict / list / None)
    result_raw: str | None  # resultJson exactly as received, for diagnostics
    credits_consumed: float | None
    cost_time_ms: int | None
    record: dict[str, Any] = field(default_factory=dict)  # the whole ``data`` object


def _body_code(body: Any) -> int | None:
    code = body.get("code") if isinstance(body, dict) else None
    if isinstance(code, bool):
        return None
    if isinstance(code, int):
        return code
    if isinstance(code, str) and code.strip().isdigit():
        return int(code.strip())
    return None


def _snippet(text: str, n: int = 300) -> str:
    text = text or ""
    return text if len(text) <= n else text[:n] + "..."


def error_for_code(code: int | None, detail: str, *, provider: str, where: str) -> ProviderError:
    """One ProviderError per kie status ``code`` (HTTP status or body ``code``).

    The wording is chosen so ``generate.errors.classify`` lands on the right
    kind: 402 -> quota, 429 -> quota (rate limit), 401/403 -> auth,
    422/400 -> invalid, 455/5xx -> unavailable.
    """
    detail = _snippet(detail)
    if code in (401, 403):
        return ProviderError(f"kie.ai {where}: unauthorized (HTTP {code}) - the API key was rejected. {detail}",
                             provider_name=provider, error_code="AUTH_FAILED")
    if code == 402:
        return ProviderError(f"kie.ai {where}: insufficient credits (HTTP 402). {detail}",
                             provider_name=provider, error_code="INSUFFICIENT_CREDITS")
    if code == 429:
        return ProviderError(f"kie.ai {where}: rate limit exceeded (HTTP 429). {detail}",
                             provider_name=provider, error_code="RATE_LIMITED")
    if code == 433:
        return ProviderError(f"kie.ai {where}: sub-key usage limit exceeded (quota, code 433). {detail}",
                             provider_name=provider, error_code="INSUFFICIENT_CREDITS")
    if code in (400, 422):
        return ProviderError(f"kie.ai {where}: validation error (HTTP {code}). {detail}",
                             provider_name=provider, error_code="INVALID_REQUEST")
    if code == 451:
        return ProviderError(f"kie.ai {where}: the source file could not be fetched (code 451, invalid source URL). {detail}",
                             provider_name=provider, error_code="INVALID_REQUEST")
    if code == 404:
        return ProviderError(f"kie.ai {where}: not found (HTTP 404). {detail}",
                             provider_name=provider, error_code="NOT_FOUND")
    if code == 455 or (isinstance(code, int) and 500 <= code < 600 and code not in (501, 505)):
        return ProviderError(f"kie.ai {where}: service unavailable (code {code}). {detail}",
                             provider_name=provider, error_code="PROVIDER_UNAVAILABLE")
    if code == 505:
        return ProviderError(f"kie.ai {where}: feature disabled, unavailable (code 505). {detail}",
                             provider_name=provider, error_code="PROVIDER_UNAVAILABLE")
    return ProviderError(f"kie.ai {where} failed (code {code}). {detail}",
                         provider_name=provider, error_code="GENERATION_FAILED")


def error_for_fail(fail_code: Any, fail_msg: Any, *, provider: str, task_id: str) -> ProviderError:
    """A ``state == "fail"`` task -> ProviderError.

    ``failCode`` is documented only as a string; when it is a kie status code
    (402, 429, 501 ...) it is mapped like :func:`error_for_code`, otherwise the
    message is kept verbatim (it carries words like "sensitive" that the
    classifier reads).
    """
    code_s = str(fail_code or "").strip()
    msg = str(fail_msg or "").strip() or "no failMsg given"
    detail = f"{msg} (failCode {code_s or '-'}, task {task_id})"
    # kie reports an upstream generation failure as failCode 400 with "please
    # try again later" (seen live 2026-10-06, credits refunded). That is not a
    # bad request from us, so keep it a plain generation failure rather than
    # letting the 400 read as "your input was wrong".
    if "try again" in msg.lower():
        return ProviderError(f"kie.ai task failed upstream: {detail}", provider_name=provider,
                             error_code="GENERATION_FAILED")
    if code_s.isdigit() and int(code_s) not in (500, 501):
        err = error_for_code(int(code_s), detail, provider=provider, where="task")
        if err.error_code != "GENERATION_FAILED":
            return err
    return ProviderError(f"kie.ai task failed: {detail}", provider_name=provider,
                         error_code=f"KIE_FAIL_{code_s}" if code_s else "GENERATION_FAILED")


def parse_result_json(value: Any) -> Any:
    """``resultJson`` is documented as a JSON string; tolerate an already
    decoded object, an empty string and a malformed one (-> ``None``)."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        if not value.strip():
            return None
        try:
            return json.loads(value)
        except ValueError:
            logger.warning("kie.ai resultJson is not valid JSON: %s", _snippet(value, 200))
            return None
    return None


class KieClient:
    """Bearer-authenticated access to kie.ai: create, poll, download, balance.

    ``http`` is a callable returning the shared ``httpx.AsyncClient`` (the
    provider owns the client's lifetime). ``request_timeout`` is applied per
    request; ``poll_timeout`` bounds the whole wait for a task, separately.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        http: Callable[[], httpx.AsyncClient],
        provider_name: str = "kie",
        request_timeout: float = 60.0,
        poll_timeout: float = 900.0,
        max_retries: int = 3,
    ):
        self.api_key = api_key
        self.base_url = (base_url or DEFAULT_BASE).rstrip("/")
        self._http = http
        self.provider = provider_name
        self.request_timeout = float(request_timeout)
        self.poll_timeout = float(poll_timeout)
        self.max_retries = max(0, int(max_retries))
        # Polling cadence: get-task-detail.md suggests starting at 2-3 s with
        # backoff. Attributes so tests can shrink them.
        self.poll_initial = 3.0
        self.poll_max = 15.0
        self.poll_factor = 1.5
        self.rate_limit_wait = 10.0  # kie's limit is 20 new requests per 10 s
        #: consecutive transient poll failures tolerated before giving up (the
        #: task is already paid for, so one network blip should not lose it)
        self.poll_error_budget = 3
        #: ``recordInfo is null`` / 404 answers tolerated right after creation
        self.not_found_grace = 3
        self._sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    # ---- plumbing ---------------------------------------------------------

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _json(self, resp: httpx.Response, where: str) -> Any:
        try:
            return resp.json()
        except Exception:
            if resp.status_code != 200:
                raise error_for_code(resp.status_code, resp.text, provider=self.provider, where=where)
            raise ProviderError(
                f"kie.ai {where} returned non-JSON (HTTP {resp.status_code}): {_snippet(resp.text, 200)}",
                provider_name=self.provider, error_code="BAD_RESPONSE",
            )

    def _status(self, resp: httpx.Response, body: Any) -> int:
        """The effective status: the HTTP one unless it is 200, then the body's."""
        if resp.status_code != 200:
            return resp.status_code
        code = _body_code(body)
        return 200 if code is None else code

    async def _post_task(self, path: str, body: dict[str, Any], where: str) -> str:
        """POST a task-creating endpoint, retrying 429 (nothing was created, so
        nothing was charged) up to ``max_retries`` times. Returns the taskId."""
        attempt = 0
        while True:
            try:
                resp = await self._http().post(
                    f"{self.base_url}{path}", headers=self.headers(), json=body, timeout=self.request_timeout
                )
            except httpx.TimeoutException as e:
                raise ProviderError(f"kie.ai {where} timed out: {e}", provider_name=self.provider, error_code="TIMEOUT")
            except Exception as e:
                raise ProviderError(f"kie.ai {where} connection failed: {e}", provider_name=self.provider,
                                    error_code="REQUEST_FAILED")
            data = self._json(resp, where)
            status = self._status(resp, data)
            if status == 429 and attempt < self.max_retries:
                attempt += 1
                logger.warning("kie.ai %s rate limited; retry %d/%d in %.0fs", where, attempt, self.max_retries,
                               self.rate_limit_wait)
                await self._sleep(self.rate_limit_wait)
                continue
            if status != 200:
                msg = data.get("msg") if isinstance(data, dict) else None
                raise error_for_code(status, msg or resp.text, provider=self.provider, where=where)
            task_id = ((data.get("data") or {}) if isinstance(data, dict) else {}).get("taskId")
            if not task_id:
                raise ProviderError(f"kie.ai {where} returned no taskId: {_snippet(resp.text)}",
                                    provider_name=self.provider, error_code="BAD_RESPONSE")
            return str(task_id)

    def _next_interval(self, current: float) -> float:
        return min(self.poll_max, current * self.poll_factor)

    # ---- unified market endpoints ----------------------------------------

    async def create_task(self, model: str, input: dict[str, Any], *, callback_url: str | None = None) -> str:
        """``POST /api/v1/jobs/createTask``; returns the taskId."""
        body: dict[str, Any] = {"model": model, "input": input}
        if callback_url:
            body["callBackUrl"] = callback_url
        return await self._post_task(CREATE_TASK_PATH, body, f"createTask({model})")

    async def get_task(self, task_id: str) -> tuple[int, dict[str, Any]]:
        """One ``recordInfo`` call: ``(effective status code, data dict)``."""
        where = "recordInfo"
        try:
            resp = await self._http().get(
                f"{self.base_url}{RECORD_INFO_PATH}", headers=self.headers(), params={"taskId": task_id},
                timeout=self.request_timeout,
            )
        except httpx.TimeoutException as e:
            raise ProviderError(f"kie.ai {where} timed out: {e}", provider_name=self.provider, error_code="TIMEOUT")
        except Exception as e:
            raise ProviderError(f"kie.ai {where} connection failed: {e}", provider_name=self.provider,
                                error_code="REQUEST_FAILED")
        body = self._json(resp, where)
        status = self._status(resp, body)
        data = (body.get("data") if isinstance(body, dict) else None) or {}
        if status != 200 and isinstance(body, dict):
            data = {"_msg": body.get("msg"), **(data if isinstance(data, dict) else {})}
        return status, data if isinstance(data, dict) else {}

    async def wait_task(self, task_id: str) -> KieTask:
        """Poll ``recordInfo`` until success; raise on fail or after ``poll_timeout``."""
        waited = 0.0
        interval = self.poll_initial
        errors = 0
        not_found = 0
        while True:
            try:
                status, data = await self.get_task(task_id)
                errors = 0
            except ProviderError as e:
                if e.error_code not in ("TIMEOUT", "REQUEST_FAILED", "PROVIDER_UNAVAILABLE"):
                    raise
                errors += 1
                if errors > self.poll_error_budget:
                    raise
                logger.warning("kie.ai poll of task %s failed (%s); retrying", task_id, e)
                status, data = -1, {}

            if status == 200:
                state = str(data.get("state") or "").lower()
                if state == SUCCESS_STATE:
                    raw = data.get("resultJson")
                    credits = data.get("creditsConsumed")
                    cost_time = data.get("costTime")
                    return KieTask(
                        task_id=task_id,
                        model=data.get("model"),
                        result=parse_result_json(raw),
                        result_raw=raw if isinstance(raw, str) else (json.dumps(raw) if raw is not None else None),
                        credits_consumed=float(credits) if isinstance(credits, (int, float)) and not isinstance(credits, bool) else None,
                        cost_time_ms=int(cost_time) if isinstance(cost_time, (int, float)) and not isinstance(cost_time, bool) else None,
                        record=data,
                    )
                if state == FAIL_STATE:
                    raise error_for_fail(data.get("failCode"), data.get("failMsg"), provider=self.provider,
                                         task_id=task_id)
                if state and state not in PENDING_STATES:
                    logger.warning("kie.ai task %s reports an undocumented state %r; still waiting", task_id, state)
            elif status == 429:
                logger.warning("kie.ai poll rate limited for task %s; slowing down", task_id)
                interval = max(interval, self.rate_limit_wait)
            elif status in (404, 422):
                # "recordInfo is null" can mean "not visible yet" right after creation
                not_found += 1
                if not_found > self.not_found_grace:
                    raise error_for_code(status, f"{data.get('_msg') or 'task not found'} (task {task_id})",
                                         provider=self.provider, where="recordInfo")
            elif status != -1:
                err = error_for_code(status, f"{data.get('_msg') or ''} (task {task_id})", provider=self.provider,
                                     where="recordInfo")
                if err.error_code != "PROVIDER_UNAVAILABLE":
                    raise err
                errors += 1
                if errors > self.poll_error_budget:
                    raise err

            if waited >= self.poll_timeout:
                raise ProviderError(
                    f"kie.ai task {task_id} timed out after {int(self.poll_timeout)}s of polling "
                    "(it may still finish and be charged upstream).",
                    provider_name=self.provider, error_code="TIMEOUT",
                )
            step = min(interval, max(0.0, self.poll_timeout - waited)) or interval
            await self._sleep(step)
            waited += step
            interval = self._next_interval(interval)

    async def run(self, model: str, input: dict[str, Any], *, callback_url: str | None = None) -> KieTask:
        """createTask + wait: the whole job in one await."""
        task_id = await self.create_task(model, input, callback_url=callback_url)
        logger.info("kie.ai task %s created (%s)", task_id, model)
        return await self.wait_task(task_id)

    # ---- legacy per-model endpoints ---------------------------------------

    async def legacy_submit(self, path: str, body: dict[str, Any]) -> str:
        """POST an old-style job endpoint (``/api/v1/generate`` ...); returns taskId."""
        return await self._post_task(path, body, path)

    async def legacy_poll(
        self,
        path: str,
        task_id: str,
        *,
        status_field: str,
        success: str = "SUCCESS",
        pending: frozenset[str] | set[str] = frozenset(),
    ) -> dict[str, Any]:
        """Poll an old-style record-info endpoint until ``data[status_field]``
        equals ``success``; returns ``data``. A status outside ``pending`` is a
        failure (``errorMessage`` / ``errorCode`` from the record)."""
        waited = 0.0
        interval = self.poll_initial
        errors = 0
        while True:
            try:
                resp = await self._http().get(
                    f"{self.base_url}{path}", headers=self.headers(), params={"taskId": task_id},
                    timeout=self.request_timeout,
                )
                body = self._json(resp, path)
                errors = 0
            except ProviderError:
                raise
            except Exception as e:
                errors += 1
                if errors > self.poll_error_budget:
                    raise ProviderError(f"kie.ai poll {path} failed: {e}", provider_name=self.provider,
                                        error_code="REQUEST_FAILED")
                logger.warning("kie.ai poll %s failed (%s); retrying", path, e)
                body, resp = None, None
            if resp is not None:
                status = self._status(resp, body)
                if status == 429:
                    interval = max(interval, self.rate_limit_wait)
                elif status != 200:
                    raise error_for_code(status, (body or {}).get("msg") if isinstance(body, dict) else resp.text,
                                         provider=self.provider, where=path)
                else:
                    data = (body.get("data") if isinstance(body, dict) else None) or {}
                    state = data.get(status_field)
                    if state == success:
                        return data
                    if state and state not in pending:
                        msg = data.get("errorMessage") or state
                        code = data.get("errorCode")
                        text = f"kie.ai task failed: {msg} (status {state}, task {task_id})"
                        err_code = str(code) if code else "GENERATION_FAILED"
                        if str(code or "").isdigit():
                            mapped = error_for_code(int(str(code)), text, provider=self.provider, where="task")
                            if mapped.error_code not in ("GENERATION_FAILED", "PROVIDER_UNAVAILABLE"):
                                raise mapped
                        raise ProviderError(text, provider_name=self.provider, error_code=err_code)
            if waited >= self.poll_timeout:
                raise ProviderError(
                    f"kie.ai task {task_id} timed out after {int(self.poll_timeout)}s of polling "
                    "(it may still finish and be charged upstream).",
                    provider_name=self.provider, error_code="TIMEOUT",
                )
            step = min(interval, max(0.0, self.poll_timeout - waited)) or interval
            await self._sleep(step)
            waited += step
            interval = self._next_interval(interval)

    async def legacy_get(self, path: str, params: dict[str, Any]) -> tuple[int, Any]:
        """One plain GET on an old-style endpoint: ``(effective status, body)``."""
        resp = await self._http().get(f"{self.base_url}{path}", headers=self.headers(), params=params,
                                      timeout=self.request_timeout)
        body = self._json(resp, path)
        return self._status(resp, body), body

    async def legacy_post(self, path: str, body: dict[str, Any]) -> Any:
        """A synchronous old-style POST (answers in the body); returns the body."""
        try:
            resp = await self._http().post(f"{self.base_url}{path}", headers=self.headers(), json=body,
                                           timeout=self.request_timeout)
        except httpx.TimeoutException as e:
            raise ProviderError(f"kie.ai {path} timed out: {e}", provider_name=self.provider, error_code="TIMEOUT")
        except Exception as e:
            raise ProviderError(f"kie.ai {path} connection failed: {e}", provider_name=self.provider,
                                error_code="REQUEST_FAILED")
        data = self._json(resp, path)
        status = self._status(resp, data)
        if status != 200:
            msg = data.get("msg") if isinstance(data, dict) else None
            raise error_for_code(status, msg or resp.text, provider=self.provider, where=path)
        return data

    # ---- results and balance ---------------------------------------------

    async def download(self, url: str) -> bytes:
        """Fetch a result file. kie's result URLs are public CDN links, so no
        auth header is sent. Done as soon as a task succeeds: the docs give
        both 14 days and "typically 24 hours" for how long the links live."""
        try:
            resp = await self._http().get(url, timeout=self.request_timeout, follow_redirects=True)
        except Exception as e:
            raise ProviderError(f"Failed to download the kie.ai result: {e}", provider_name=self.provider,
                                error_code="DOWNLOAD_FAILED")
        if resp.status_code != 200:
            raise ProviderError(f"Failed to download the kie.ai result (HTTP {resp.status_code}).",
                                provider_name=self.provider, error_code="DOWNLOAD_FAILED")
        return resp.content

    async def credits(self) -> float | None:
        """Credits left on the account (``GET /api/v1/chat/credit``)."""
        try:
            resp = await self._http().get(f"{self.base_url}{CREDIT_PATH}", headers=self.headers(),
                                          timeout=self.request_timeout)
        except Exception as e:
            raise ProviderError(f"kie.ai credit query failed: {e}", provider_name=self.provider,
                                error_code="REQUEST_FAILED")
        body = self._json(resp, "credit")
        status = self._status(resp, body)
        if status != 200:
            raise error_for_code(status, (body or {}).get("msg") if isinstance(body, dict) else resp.text,
                                 provider=self.provider, where="credit")
        value = body.get("data") if isinstance(body, dict) else None
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def credits_to_usd(credits: float | None, credit_usd: float | None) -> float | None:
    """``creditsConsumed`` x USD-per-credit, rounded to 1/10000 of a dollar."""
    if credits is None or credit_usd is None:
        return None
    return round(float(credits) * float(credit_usd), 4)
