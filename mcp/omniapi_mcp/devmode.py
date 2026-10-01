"""Development switches, read from the environment of the daemon process.

``OMNIAPI_DEV=1``      enables the development stand-ins: the ``replay``
                       harness and the ``echo`` text models (no vendor call).
``OMNIAPI_OFFLINE=1``  makes the daemon refuse everything that would reach a
                       real vendor — text models, agent runs, image / speech /
                       music / transcription tools. Use it together with
                       ``OMNIAPI_DEV=1`` for a sandbox that cannot spend money
                       no matter what a test script or an agent sends.

Why a hard switch: on 2026-09-29 a test script that lost a conversation id
fell back to the default model and billed two real DeepSeek calls from the
sandbox. "Remember to pass --model echo" is an instruction; this is a lock.
"""

from __future__ import annotations

import os

#: MCP tools that always call a paid vendor (text / agent / chat tools are
#: guarded where the model is resolved, so echo and replay still work offline)
PAID_TOOLS = frozenset(
    {
        "generate_image",
        "edit_image",
        "transcribe_audio",
        "generate_speech",
        "generate_music",
        "edit_music",
        "music_lyrics",
        "music_utility",
        "compose_music",
    }
)


def dev_enabled() -> bool:
    return os.environ.get("OMNIAPI_DEV") == "1"


def offline() -> bool:
    return os.environ.get("OMNIAPI_OFFLINE") == "1"


def offline_message(what: str) -> str:
    return (
        f"This daemon is an offline sandbox (OMNIAPI_OFFLINE=1): {what} would call a real vendor and cost money, so it was refused. "
        "Use the echo model (chat) or the replay harness (runs)."
    )


def refuse_if_offline(what: str) -> None:
    if offline():
        raise RuntimeError(offline_message(what))
