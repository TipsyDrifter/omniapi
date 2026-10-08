"""Why a generation failed, in a handful of kinds the GUI can explain.

The vendors word their errors differently; this is the one place that reads
those words. The raw message is always kept next to the kind.
"""

from __future__ import annotations

import asyncio
import re

KINDS = ("quota", "auth", "rejected", "timeout", "too_large", "unavailable", "offline", "interrupted", "invalid", "other",
         # 1.4-M3, video only (set by the job runner, never read from words): we stopped waiting at
         # the time limit / the vendor no longer knows the job or the service died before its id
         # came back / done there but the file would not download
         "gave_up", "lost", "download")

# first match wins — order matters: a 429 that says "insufficient_quota" is a
# quota problem, and a 400 that says "safety" is a rejection, not a bad request
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("offline", re.compile(r"offline sandbox|OMNIAPI_OFFLINE", re.I)),
    ("quota", re.compile(r"insufficient[_ ]quota|quota|billing|credit|payment required|\b402\b|\b429\b|rate.?limit|too many requests|resource.?exhausted", re.I)),
    ("auth", re.compile(r"\b401\b|\b403\b|unauthori[sz]ed|invalid.{0,12}api.?key|incorrect api key|permission.?denied|authentication", re.I)),
    ("rejected", re.compile(r"moderation|content.?policy|safety|blocked|prohibited|sensitive|not allowed|violat", re.I)),
    ("too_large", re.compile(r"\b413\b|too large|exceeds? .{0,30}(size|limit|mb)|maximum .{0,20}(size|length)", re.I)),
    ("timeout", re.compile(r"timed? ?out|timeout|deadline.?exceeded|\b504\b|\b408\b", re.I)),
    ("unavailable", re.compile(r"no \w+ provider|not configured|not supported by|api_key|\b50[0-3]\b|overloaded|unavailable|connection|connect", re.I)),
    ("invalid", re.compile(r"validation error|invalid|\b400\b|\b422\b|unsupported|must be|required", re.I)),
)


def classify(error: BaseException | str) -> str:
    """One of ``KINDS`` for an exception (or its message)."""
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    told = getattr(error, "error_kind", None)  # a provider that classified its own failure (ProviderError)
    if isinstance(told, str) and told in KINDS:
        return told
    text = error if isinstance(error, str) else f"{type(error).__name__}: {error}"
    for kind, pattern in _RULES:
        if pattern.search(text):
            return kind
    return "other"
