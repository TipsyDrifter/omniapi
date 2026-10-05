"""Each provider's last connection result (1.3-M4): did listing its models, or
testing its key, work — and if not, why.

"Has a key and is switched on" does not mean "works". This module keeps the
last answer the vendor gave, per key, in ``<data home>/cache/provider-health.json``
so it survives a restart. Nothing here calls a vendor: the results come from
what already happens — model discovery (startup, a key change, ``/api/models?refresh``)
and ``POST /api/settings/test-key``.

Records are filed under a fingerprint of the key they were made with (a
truncated SHA-256, never the key), so a result always belongs to the key in
effect: change the key and the provider reads "not tested yet" until the new
one has been listed or tested. A few fingerprints are kept per provider, so
testing a pasted key that is then not saved does not wipe the result of the
key in use.

Reasons (``reason``), stable for the page to translate:

* ``rejected``       401 / 403 — the key is wrong, revoked or lacks access
* ``payment``        402 — out of credit / billing
* ``rate_limited``   429
* ``provider_error`` 5xx — the vendor is having trouble
* ``http_error``     any other HTTP status
* ``network``        could not reach the vendor (DNS, refused, timed out)
* ``format``         the key cannot be a key (empty, whitespace inside, too short)
* ``error``          anything else
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from .paths import cache_dir

logger = logging.getLogger(__name__)

FILE_NAME = "provider-health.json"
#: fingerprints kept per provider (the key in use plus a few tested ones)
KEEP_PER_PROVIDER = 4
MESSAGE_MAX = 200

_lock = threading.Lock()


def health_path() -> Path:
    return cache_dir() / FILE_NAME


def fingerprint(key: str) -> str:
    """A short, one-way name for a key (enough to tell keys apart, useless to call with)."""
    return hashlib.sha256(("omniapi-key:" + key).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- classify
def classify(exc: BaseException, *, key: str = "") -> dict[str, Any]:
    """``{reason, status?, message}`` for an exception from a vendor call.
    The message never carries the key."""
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            reason, msg = "rejected", "the provider rejected the key"
        elif code == 402:
            reason, msg = "payment", "the provider wants payment (out of credit?)"
        elif code == 429:
            reason, msg = "rate_limited", "the provider is rate-limiting this key"
        elif code >= 500:
            reason, msg = "provider_error", f"the provider is having trouble (HTTP {code})"
        else:
            reason, msg = "http_error", f"the provider answered HTTP {code}"
        return {"reason": reason, "status": code, "message": msg}
    if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError, TimeoutError)):
        return {"reason": "network", "message": "the provider did not answer in time"}
    if isinstance(exc, httpx.TransportError):
        return {"reason": "network", "message": f"could not reach the provider ({exc.__class__.__name__})"}
    text = str(exc)
    if key:
        text = text.replace(key, "***")
    return {"reason": "error", "message": (f"{exc.__class__.__name__}: {text}" if text else exc.__class__.__name__)[:MESSAGE_MAX]}


# ---------------------------------------------------------------- file
def _read(path: Optional[Path] = None) -> dict[str, Any]:
    p = path or health_path()
    for attempt in range(5):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (PermissionError, json.JSONDecodeError):  # a writer is replacing it this instant
            time.sleep(0.03 * (attempt + 1))
        except OSError:
            return {}
    return {}


def _write(data: dict[str, Any], path: Optional[Path] = None) -> None:
    p = path or health_path()
    tmp = p.with_name(f".{p.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        for attempt in range(10):
            try:
                os.replace(tmp, p)
                return
            except PermissionError:
                time.sleep(0.03 * (attempt + 1))
        logger.warning("could not update %s (file in use)", p)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def record(
    provider: str,
    key: str,
    *,
    ok: bool,
    source: str,
    at: Optional[float] = None,
    listed: Optional[int] = None,
    reason: Optional[str] = None,
    status: Optional[int] = None,
    message: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """File one result for ``provider`` (catalog name) under ``key``'s
    fingerprint. An older result never replaces a newer one. Best effort:
    a failure to write is logged, never raised."""
    if not key:
        return None
    at = time.time() if at is None else at
    fp = fingerprint(key)
    entry: dict[str, Any] = {"ok": bool(ok), "source": source, "at": at, "last4": key[-4:] if len(key) >= 8 else None}
    if listed is not None:
        entry["listed"] = int(listed)
    if not ok:
        entry["reason"] = reason or "error"
        if status is not None:
            entry["status"] = int(status)
    if message:
        entry["message"] = str(message).replace(key, "***")[:MESSAGE_MAX]
    if extra:
        entry.update(extra)
    try:
        with _lock:
            data = _read()
            per = data.get(provider) if isinstance(data.get(provider), dict) else {}
            old = per.get(fp) if isinstance(per.get(fp), dict) else None
            if old and float(old.get("at") or 0) > at:
                return old
            if ok:
                entry["last_ok_at"] = at
            elif old and old.get("last_ok_at"):
                entry["last_ok_at"] = old["last_ok_at"]
            per[fp] = entry
            if len(per) > KEEP_PER_PROVIDER:
                newest = sorted(per.items(), key=lambda kv: float((kv[1] or {}).get("at") or 0), reverse=True)
                per = dict(newest[:KEEP_PER_PROVIDER])
            data[provider] = per
            _write(data)
    except Exception as e:  # pragma: no cover - the status is a convenience, never a failure
        logger.warning("could not record the connection result for %s: %s", provider, e)
    return entry


def lookup(provider: str, key: str) -> Optional[dict[str, Any]]:
    """The last result for this provider and exactly this key, or ``None``."""
    if not key:
        return None
    per = _read().get(provider)
    if not isinstance(per, dict):
        return None
    hit = per.get(fingerprint(key))
    return dict(hit) if isinstance(hit, dict) else None


def view(provider: str, key: str, *, online_models: Optional[int] = None) -> Optional[dict[str, Any]]:
    """What the settings and models pages show (``None`` without a key):

    ``{state: ok|failed|untested, ok, source: discovery|test|null, checked_at,
    listed, online_models, reason, status, message, last_ok_at}``"""
    if not key:
        return None
    hit = lookup(provider, key)
    out: dict[str, Any] = {
        "state": "untested",
        "ok": None,
        "source": None,
        "checked_at": None,
        "listed": None,
        "online_models": online_models,
        "reason": None,
        "status": None,
        "message": None,
        "last_ok_at": None,
    }
    if hit:
        out.update({
            "state": "ok" if hit.get("ok") else "failed",
            "ok": bool(hit.get("ok")),
            "source": hit.get("source"),
            "checked_at": hit.get("at"),
            "listed": hit.get("listed"),
            "reason": hit.get("reason"),
            "status": hit.get("status"),
            "message": hit.get("message"),
            "last_ok_at": hit.get("last_ok_at"),
        })
        for k in ("credits",):
            if k in hit:
                out[k] = hit[k]
    return out
