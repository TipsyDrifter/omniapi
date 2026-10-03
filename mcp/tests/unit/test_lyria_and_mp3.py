"""1.1-M5: Google Lyria through the Interactions API, and Gemini TTS as MP3
when a system ffmpeg is there. No vendor is called: the SDK client is a fake."""

from __future__ import annotations

import base64
from types import SimpleNamespace as NS

import pytest

from omniapi_mcp.capabilities.music import LyriaProvider
from omniapi_mcp.capabilities.speech import GeminiSpeechProvider
from omniapi_mcp.providers.base import ProviderConfig, ProviderError
from omniapi_mcp.tools.music_generation import MusicGenerationTool
from omniapi_mcp.utils import audio as audio_utils


def _lyria(reply):
    p = LyriaProvider(ProviderConfig(api_key="k", timeout=30, enabled=True))
    calls = []

    async def create(**kw):
        calls.append(kw)
        if isinstance(reply, Exception):
            raise reply
        return reply

    p.client = NS(aio=NS(interactions=NS(create=create)))
    return p, calls


async def test_lyria_sends_the_prompt_and_reads_the_documented_reply():
    reply = NS(id="int_1", output_audio=base64.b64encode(b"ID3song").decode(), output_text="[Verse]\nla la")
    p, calls = _lyria(reply)
    r = await p.generate("city pop about rain", model="lyria-3.5")
    assert calls == [{"model": "lyria-3.5", "input": "city pop about rain"}]  # nothing but the prompt
    assert r.audio_data == b"ID3song" and r.output_format == "mp3" and r.text == "[Verse]\nla la"
    assert r.metadata["cost_usd"] == 0.08 and r.metadata["provider"] == "google" and r.metadata["task_id"] == "int_1"


async def test_lyria_no_vocals_is_a_sentence_in_the_prompt_and_the_clip_is_thirty_seconds():
    p, calls = _lyria(NS(output_audio=NS(data=b"RIFFxx", mime_type="audio/wav")))
    r = await p.generate("lofi piano", model="lyria-3-clip-preview", instrumental=True, output_format="wav")
    assert calls[0]["input"].endswith("Instrumental only, no vocals.") and calls[0]["response_format"] == {"type": "audio"}
    assert (r.output_format, r.metadata["cost_usd"], r.metadata["duration"]) == ("wav", 0.04, 30.0)
    await p.generate("ambient, no vocals please", instrumental=True)
    assert calls[1]["input"] == "ambient, no vocals please"  # already says so: not repeated


async def test_lyria_finds_audio_among_the_outputs_and_fails_clearly_without_any():
    part = NS(type="audio", data=base64.b64encode(b"mp3bytes").decode(), mime_type="audio/mpeg")
    p, _ = _lyria(NS(output_audio=None, outputs=[NS(type="text", data=None, mime_type=None), part]))
    assert (await p.generate("x")).audio_data == b"mp3bytes"
    p, _ = _lyria(NS(output_audio=None, outputs=[]))
    with pytest.raises(ProviderError, match="no audio"):
        await p.generate("x")
    p, _ = _lyria(RuntimeError("429 RESOURCE_EXHAUSTED"))
    with pytest.raises(ProviderError, match="RESOURCE_EXHAUSTED"):
        await p.generate("x")
    with pytest.raises(ProviderError, match="not supported"):
        await p.generate("x", model="lyria-realtime-exp")


def _settings(tmp_path, **providers):
    slots = {k: None for k in ("openai", "gemini", "elevenlabs", "kie")}
    slots.update(providers)
    return NS(providers=NS(**slots), storage=NS(base_path=str(tmp_path)))


async def test_the_music_tool_routes_lyria_with_a_gemini_key_and_reports_its_price(tmp_path):
    key = NS(enabled=True, api_key="AQ.developer-key", timeout=30, max_retries=0)
    tool = MusicGenerationTool(_settings(tmp_path, gemini=key))
    assert {"lyria-3.5", "lyria-3-clip-preview"} <= set(tool.available_models())
    provider = tool._model_map["lyria-3.5"]

    async def create(**kw):
        return NS(id="int_9", output_audio=base64.b64encode(b"ID3").decode(), output_text="words")

    provider.client = NS(aio=NS(interactions=NS(create=create)))
    out = await tool.generate("a song", model="lyria-3.5")
    assert out["audio_path"].endswith(".mp3") and (tmp_path / "music").is_dir()
    assert (out["model"], out["cost_usd"], out["lyrics"], out["provider"]) == ("lyria-3.5", 0.08, "words", "google")
    # a service-account file path in the key slot is not an API key
    assert MusicGenerationTool(_settings(tmp_path, gemini=NS(enabled=True, api_key="C:/keys/sa.json"))).available_models() == []


# --------------------------------------------------------------------------- Gemini TTS as MP3


def _gemini_tts(pcm=b"\x00\x00" * 2400):
    p = GeminiSpeechProvider(ProviderConfig(api_key="k", timeout=30, enabled=True))
    inline = NS(data=pcm, mime_type="audio/L16;codec=pcm;rate=24000")

    async def generate_content(**kw):
        return NS(candidates=[NS(content=NS(parts=[NS(inline_data=inline)]))])

    p.client = NS(aio=NS(models=NS(generate_content=generate_content)))
    return p


async def test_gemini_tts_is_mp3_when_ffmpeg_can_encode_it(monkeypatch):
    seen = {}

    async def fake_mp3(pcm, *, sample_rate, channels=1, **kw):
        seen.update(rate=sample_rate, channels=channels, n=len(pcm))
        return b"ID3fake"

    monkeypatch.setattr("omniapi_mcp.capabilities.speech.ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr("omniapi_mcp.capabilities.speech.pcm_to_mp3", fake_mp3)
    r = await _gemini_tts().synthesize("hi", output_format="mp3_44100_128")
    assert (r.output_format, r.audio_data) == ("mp3", b"ID3fake") and seen == {"rate": 24000, "channels": 1, "n": 4800}


async def test_gemini_tts_falls_back_to_wav_without_ffmpeg_or_when_it_fails(monkeypatch):
    monkeypatch.setattr("omniapi_mcp.capabilities.speech.ffmpeg_path", lambda: None)
    r = await _gemini_tts().synthesize("hi", output_format="mp3")
    assert r.output_format == "wav" and r.audio_data[:4] == b"RIFF"

    async def broken(*a, **kw):
        return None

    monkeypatch.setattr("omniapi_mcp.capabilities.speech.ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr("omniapi_mcp.capabilities.speech.pcm_to_mp3", broken)
    r = await _gemini_tts().synthesize("hi", output_format="mp3")
    assert r.output_format == "wav" and r.audio_data[:4] == b"RIFF"
    assert (await _gemini_tts().synthesize("hi", output_format="wav")).output_format == "wav"


@pytest.mark.skipif(audio_utils.ffmpeg_path() is None, reason="no ffmpeg on PATH")
async def test_the_real_ffmpeg_encodes_pcm_to_mp3():
    import math
    import struct

    pcm = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / 24000))) for i in range(24000))
    mp3 = await audio_utils.pcm_to_mp3(pcm, sample_rate=24000)
    assert mp3 and len(mp3) < len(pcm) and (mp3[:3] == b"ID3" or mp3[0] == 0xFF)
