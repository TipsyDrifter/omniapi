"""Live model discovery — asks each provider which models are ONLINE right now.

One small async fetcher per provider API shape. Every fetcher returns a list
of ``DiscoveredModel`` (id + whatever cheap metadata the listing endpoint
gives). Failures never raise: the catalog falls back to the on-disk cache
(see ``catalog.py``), so a provider being down does not break startup.

Kept SDK-free on purpose (plain httpx) so a listing call is uniform, cheap
and easy to mock in tests.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from .paths import cache_dir

logger = logging.getLogger(__name__)

DISCOVERY_TIMEOUT = 8.0  # seconds per provider — startup must stay snappy
CACHE_TTL_SECONDS = 24 * 3600


@dataclass
class DiscoveredModel:
    id: str
    display_name: str | None = None
    context: int | None = None
    max_output: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class DiscoveryResult:
    provider: str
    models: list[DiscoveredModel]
    fetched_at: float
    from_cache: bool = False
    error: str | None = None

    @property
    def ids(self) -> set[str]:
        return {m.id for m in self.models}


# --------------------------------------------------------------------------
# Fetchers
# --------------------------------------------------------------------------

async def fetch_openai_compatible(
    base_url: str, api_key: str, *, timeout: float = DISCOVERY_TIMEOUT
) -> list[DiscoveredModel]:
    """``GET {base_url}/models`` — OpenAI, DeepSeek, OpenRouter and friends."""
    url = base_url.rstrip("/") + "/models"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, headers={"Authorization": f"Bearer {api_key}"})
        resp.raise_for_status()
        data = resp.json()
    out: list[DiscoveredModel] = []
    for item in data.get("data", []):
        mid = item.get("id")
        if not mid:
            continue
        out.append(
            DiscoveredModel(
                id=mid,
                display_name=item.get("name") or item.get("display_name"),
                context=item.get("context_length") or item.get("max_input_tokens"),
                max_output=(item.get("top_provider") or {}).get("max_completion_tokens")
                if isinstance(item.get("top_provider"), dict)
                else item.get("max_tokens"),
                extra={
                    k: item[k]
                    for k in ("pricing", "owned_by", "created", "architecture", "supported_parameters")
                    if k in item
                },
            )
        )
    return out


async def fetch_anthropic(
    api_key: str,
    *,
    base_url: str = "https://api.anthropic.com",
    timeout: float = DISCOVERY_TIMEOUT,
) -> list[DiscoveredModel]:
    """``GET /v1/models`` (paginated) on the Anthropic API."""
    url = base_url.rstrip("/") + "/v1/models"
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    out: list[DiscoveredModel] = []
    params: dict[str, Any] = {"limit": 100}
    async with httpx.AsyncClient(timeout=timeout) as client:
        for _ in range(10):  # hard stop on pagination
            resp = await client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()
            for item in data.get("data", []):
                if item.get("id"):
                    out.append(
                        DiscoveredModel(
                            id=item["id"],
                            display_name=item.get("display_name"),
                            extra={"created_at": item.get("created_at")},
                        )
                    )
            if not data.get("has_more") or not data.get("last_id"):
                break
            params["after_id"] = data["last_id"]
    return out


async def fetch_gemini(
    api_key: str,
    *,
    base_url: str = "https://generativelanguage.googleapis.com/v1beta",
    timeout: float = DISCOVERY_TIMEOUT,
) -> list[DiscoveredModel]:
    """``GET /v1beta/models`` on the Gemini Developer API (paginated).

    Ids come back as ``models/gemini-...``; the prefix is stripped so ids
    match what ``generate_content`` accepts and what the catalog uses.
    """
    url = base_url.rstrip("/") + "/models"
    out: list[DiscoveredModel] = []
    # key 走 header 不走 query：httpx 的 INFO log 會把完整 URL（含 ?key=）寫進 daemon.log
    params: dict[str, Any] = {"pageSize": 200}
    headers = {"x-goog-api-key": api_key}
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        for _ in range(10):
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            for item in data.get("models", []):
                name = item.get("name", "")
                mid = name.split("/", 1)[1] if name.startswith("models/") else name
                if not mid:
                    continue
                out.append(
                    DiscoveredModel(
                        id=mid,
                        display_name=item.get("displayName"),
                        context=item.get("inputTokenLimit"),
                        max_output=item.get("outputTokenLimit"),
                        extra={
                            "methods": item.get("supportedGenerationMethods", []),
                            "description": item.get("description"),
                        },
                    )
                )
            token = data.get("nextPageToken")
            if not token:
                break
            params["pageToken"] = token
    return out


async def fetch_elevenlabs(
    api_key: str,
    *,
    base_url: str = "https://api.elevenlabs.io",
    timeout: float = DISCOVERY_TIMEOUT,
) -> list[DiscoveredModel]:
    """``GET /v1/models`` on ElevenLabs (TTS models; music is not listed)."""
    url = base_url.rstrip("/") + "/v1/models"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, headers={"xi-api-key": api_key})
        resp.raise_for_status()
        data = resp.json()
    out: list[DiscoveredModel] = []
    for item in data if isinstance(data, list) else []:
        mid = item.get("model_id")
        if not mid:
            continue
        out.append(
            DiscoveredModel(
                id=mid,
                display_name=item.get("name"),
                extra={
                    k: item.get(k)
                    for k in (
                        "can_do_text_to_speech",
                        "can_do_voice_conversion",
                        "languages",
                        "description",
                    )
                    if k in item
                },
            )
        )
    return out


# --------------------------------------------------------------------------
# Cache + orchestration
# --------------------------------------------------------------------------

def _cache_path(provider: str) -> Path:
    return cache_dir() / f"discovery-{provider}.json"


def load_cached(provider: str) -> DiscoveryResult | None:
    p = _cache_path(provider)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return DiscoveryResult(
            provider=provider,
            models=[DiscoveredModel(**m) for m in raw.get("models", [])],
            fetched_at=float(raw.get("fetched_at", 0)),
            from_cache=True,
        )
    except Exception as e:  # corrupt cache is not fatal
        logger.warning("Ignoring corrupt discovery cache for %s: %s", provider, e)
        return None


def save_cached(result: DiscoveryResult) -> None:
    try:
        _cache_path(result.provider).write_text(
            json.dumps(
                {
                    "provider": result.provider,
                    "fetched_at": result.fetched_at,
                    "models": [asdict(m) for m in result.models],
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("Could not write discovery cache for %s: %s", result.provider, e)


Fetcher = Callable[[], Awaitable[list[DiscoveredModel]]]


async def discover(
    provider: str,
    fetcher: Fetcher,
    *,
    force: bool = False,
    ttl: float = CACHE_TTL_SECONDS,
) -> DiscoveryResult:
    """Run one provider's fetcher with cache-first semantics.

    - fresh cache (younger than ``ttl``) and not ``force`` → return cache
    - otherwise fetch; on success write cache
    - on failure → stale cache if any, else an empty result carrying ``error``
    """
    cached = load_cached(provider)
    now = time.time()
    if cached and not force and now - cached.fetched_at < ttl:
        return cached
    try:
        models = await asyncio.wait_for(fetcher(), timeout=DISCOVERY_TIMEOUT + 2)
        result = DiscoveryResult(provider=provider, models=models, fetched_at=now)
        save_cached(result)
        logger.info("Discovered %d models from %s", len(models), provider)
        return result
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        logger.warning("Discovery failed for %s (%s); using cache=%s", provider, msg, bool(cached))
        if cached:
            cached.error = msg
            return cached
        return DiscoveryResult(provider=provider, models=[], fetched_at=0.0, error=msg)
