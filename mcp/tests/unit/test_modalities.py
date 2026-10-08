"""The modality list has one source per side (1.4-M1).

* the backend's ``omniapi_mcp/modalities.py`` and every table derived from it
* ``catalog.json``: its modalities and pricing units are the known ones
* the GUI's ``gui/src/lib/modalities.ts`` (read as text) says the same thing
* ``.mp4``: audio only when an audio tool wrote it or it sits in an audio
  tool's folder; the backfill of an existing works folder is unchanged
"""

from __future__ import annotations

import json
import re
import struct
import wave
from pathlib import Path

import pytest

from omniapi_mcp import modalities as M
from omniapi_mcp.artifacts.index import _scan
from omniapi_mcp.catalog.catalog import MODALITIES, PRICING_UNITS, ModelCatalog, validate

REPO = Path(__file__).resolve().parents[3]
GUI_MODALITIES = REPO / "gui" / "src" / "lib" / "modalities.ts"
GUI_WALL = REPO / "gui" / "src" / "components" / "works" / "wall.ts"
CATALOG_JSON = Path(M.__file__).with_name("catalog") / "catalog.json"


# ----------------------------------------------------------------- backend
def test_the_table_is_consistent():
    assert M.KINDS == ("image", "speech", "music", "transcript", "video")
    assert M.CATALOG_OF == {"image": "image", "speech": "speech", "music": "music", "transcript": "transcription", "video": "video"}
    assert M.WORK_KINDS == ("image", "speech", "music", "transcript", "video", "lyrics")
    assert M.MEDIA_OF["video"] == "video" and M.BY_KIND["video"].folder == "videos"
    assert set(M.CATALOG_MODALITIES) == {"text"} | set(M.CATALOG_OF.values())
    assert MODALITIES == M.CATALOG_MODALITIES
    # every tool's work kind is a work kind; every kind has its plain tool
    assert set(M.KIND_BY_TOOL.values()) <= set(M.WORK_KINDS)
    for m in M.GENERATION:
        assert M.KIND_BY_TOOL[m.tool] == m.kind
    assert M.SOURCE_SLOTS["images"][0] == {"image"}
    assert M.SOURCE_SLOTS["audio"][0] == {"speech", "music"}
    assert M.SOURCE_SLOTS["frames"] == ({"image"}, {"image"})  # a video's first / last frame: image works or uploads


def test_derived_tables_match_what_they_replaced():
    from omniapi_mcp.artifacts.index import KIND_BY_TOOL
    from omniapi_mcp.chat.manager import _KIND_WORD, _WORK_ATTACH_KIND
    from omniapi_mcp.config.user_settings import DEFAULT_KINDS, _KIND_MODALITY
    from omniapi_mcp.generate.manager import tool_for
    from omniapi_mcp.generate.options import MODALITY

    assert KIND_BY_TOOL == {
        "generate_image": "image", "edit_image": "image", "generate_speech": "speech", "generate_music": "music",
        "edit_music": "music", "compose_music": "music", "music_utility": "music", "music_lyrics": "lyrics",
        "transcribe_audio": "transcript", "generate_video": "video",
    }
    assert _WORK_ATTACH_KIND == {"image": "image", "speech": "audio", "music": "audio", "transcript": "file", "lyrics": "file",
                                 "video": None}  # a chat cannot take a video: refused with a reason
    assert DEFAULT_KINDS == ("chat", "dispatch", "image", "speech", "music", "transcript", "video")
    assert _KIND_MODALITY == MODALITY == M.CATALOG_OF
    assert _KIND_WORD["transcript"] == "逐字稿" and _KIND_WORD["image"] == "圖片"
    assert [tool_for(k, {}) for k in M.KINDS] == ["generate_image", "generate_speech", "generate_music", "transcribe_audio", "generate_video"]
    assert tool_for("video", {"frames": {"first": {"artifact_id": "x"}}}) == "generate_video"  # frames do not change the tool


def test_a_table_that_misses_a_kind_stops_the_import():
    with pytest.raises(RuntimeError, match=r"missing \['video'\]"):
        M.require_keys({"image": 1}, ("image", "video"), "a table")
    with pytest.raises(RuntimeError, match=r"unknown \['video'\]"):
        M.require_keys({"image": 1, "video": 2}, ("image",), "a table")


def test_every_paid_generation_tool_is_known_to_the_sandbox_and_the_guard():
    from omniapi_mcp.artifacts.fakes import FAKEABLE
    from omniapi_mcp.devmode import PAID_TOOLS

    for m in M.GENERATION:
        for tool in (m.tool, m.source_tool):
            if tool:
                assert tool in PAID_TOOLS and tool in FAKEABLE, tool


# ----------------------------------------------------------------- catalog
def test_catalog_json_is_sound():
    raw = json.loads(CATALOG_JSON.read_text(encoding="utf-8"))
    assert validate(raw) == []
    seen = {m["modality"] for m in raw["models"]}
    assert seen == set(M.CATALOG_MODALITIES)  # every known modality has models, and no other one appears
    units = {(m.get("pricing") or {}).get("unit") for m in raw["models"]} - {None}
    assert units <= set(PRICING_UNITS)


def test_validate_names_each_problem():
    raw = json.loads(CATALOG_JSON.read_text(encoding="utf-8"))
    raw["models"] = raw["models"][:3] + [
        {"id": "v1", "provider": "openai", "modality": "hologram", "name": "V", "status": "current", "pricing": {"unit": "per_second"}},
        {"id": "x", "provider": "nobody", "modality": "text", "status": "gone", "shutdown": "soon"},
        {"provider": "openai", "modality": "text", "name": "no id", "status": "current"},
    ]
    problems = "\n".join(validate(raw))
    assert "unknown modality 'hologram'" in problems
    assert "unknown pricing.unit 'per_second'" in problems
    assert "provider 'nobody' is not defined" in problems
    assert "unknown status 'gone'" in problems
    assert "shutdown 'soon' is not YYYY-MM-DD" in problems
    assert "missing ['id']" in problems and "missing ['name']" in problems
    assert "tiers.cheap" in problems  # deepseek-flash was cut from the list above


def test_a_bad_catalog_is_logged_not_fatal(tmp_path, caplog):
    raw = json.loads(CATALOG_JSON.read_text(encoding="utf-8"))
    raw["models"].append({"id": "v1", "provider": "openai", "modality": "hologram", "name": "V", "status": "current"})
    raw["models"].append({"provider": "openai", "modality": "text"})  # no id: skipped, not a KeyError
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with caplog.at_level("ERROR"):
        cat = ModelCatalog(path)
    assert cat.get("gpt-6-sol") is not None  # the rest still loads
    assert any("unknown modality 'hologram'" in p for p in cat.problems)
    assert any("catalog.json: " in r.getMessage() and "hologram" in r.getMessage() for r in caplog.records)


def test_the_catalog_reconciliation_of_2026_10_05():
    cat = ModelCatalog()
    for mid in ("eleven_v4", "eleven_v4_turbo"):
        e = cat.get(mid)
        assert e and e.status == "current" and e.modality == "speech" and e.implemented
    assert cat.get("eleven_v4").pricing["text"] == 0.08 and cat.get("eleven_v4_turbo").pricing["text"] == 0.04
    for mid, day in (("gpt-image-1.5", "2026-12-01"), ("gpt-image-1-mini", "2026-12-01"), ("tts-1", "2027-01-06"), ("tts-1-hd", "2027-01-06")):
        assert (cat.get(mid).status, cat.get(mid).shutdown) == ("deprecated", day), mid
    assert cat.get("gpt-image-1.5").replacement == "gpt-image-2.5-sunburst"
    assert cat.get("music_v1").status == "deprecated"
    assert cat.get("gemini-2.5-flash-image").status == "retired"
    assert cat.get("gemini-2.5-flash").status == "retired" and "used it before" in cat.get("gemini-2.5-flash").note
    assert cat.get("gpt-image-1-mini").pricing["unit"] == "per_1m_tokens"
    assert cat.get("gemini-3.1-flash-lite-image").pricing == {"unit": "per_image", "1K": 0.0336}
    assert cat._raw["updated"] == "2026-10-05"


def test_eleven_v4_routes_through_the_speech_tool():
    from omniapi_mcp.capabilities.speech import ElevenLabsProvider

    assert {"eleven_v4", "eleven_v4_turbo"} <= ElevenLabsProvider.SUPPORTED_MODELS


# ----------------------------------------------------------------- the GUI's copy
def _ts_list(src: str, name: str) -> list[str]:
    m = re.search(rf"export const {name} = \[([^\]]*)\] as const", src)
    assert m, f"{name} not found in modalities.ts"
    return re.findall(r'"([^"]+)"', m.group(1))


def _ts_rows(src: str, name: str, field: str) -> dict[str, str]:
    block = re.search(rf"export const {name} = \{{(.*?)\}} as const satisfies", src, re.S)
    assert block, f"{name} not found in modalities.ts"
    return dict(re.findall(rf'^\s*(\w+): \{{ {field}: "([^"]+)"', block.group(1), re.M))


def test_the_gui_says_the_same():
    src = GUI_MODALITIES.read_text(encoding="utf-8")
    assert tuple(_ts_list(src, "GEN_KINDS")) == M.KINDS
    assert tuple(_ts_list(src, "GEN_KINDS") + _ts_list(src, "EXTRA_WORK_KINDS")) == M.WORK_KINDS
    assert set(_ts_list(src, "CATALOG_MODALITIES")) == set(M.CATALOG_MODALITIES)
    assert _ts_rows(src, "GEN_KIND_INFO", "modality") == M.CATALOG_OF
    assert _ts_rows(src, "WORK_KIND_INFO", "media") == M.MEDIA_OF


def test_the_gui_names_every_generation_tool():
    src = GUI_WALL.read_text(encoding="utf-8")
    block = re.search(r"export const TOOL_ZH: Record<string, string> = \{(.*?)\};", src, re.S)
    assert block
    assert set(re.findall(r"^\s*(\w+):", block.group(1), re.M)) == set(M.KIND_BY_TOOL)


# ----------------------------------------------------------------- the v1.2.0 acceptance leftovers
def _speech_ctx(default=None):
    from types import SimpleNamespace

    from omniapi_mcp.capabilities.speech import ElevenLabsProvider, OpenAITTSProvider

    providers = [SimpleNamespace(DEFAULT_MODEL=ElevenLabsProvider.DEFAULT_MODEL), SimpleNamespace(DEFAULT_MODEL=OpenAITTSProvider.DEFAULT_MODEL)]
    tool = SimpleNamespace(_providers=providers, _default_model=lambda: "eleven_flash_v2_5")
    settings = SimpleNamespace(defaults=SimpleNamespace(speech=default), images=SimpleNamespace(default_model=None))
    return SimpleNamespace(speech_tool=tool, settings=settings)


def _rows(*pairs):
    return [{"id": i, "provider": p, "available": True, "status": "current"} for i, p in pairs]


def test_a_chat_speech_proposal_does_not_default_to_elevenlabs():
    from omniapi_mcp.chat.tools import _chat_default

    rows = _rows(("gpt-4o-mini-tts", "openai"), ("eleven_flash_v2_5", "elevenlabs"), ("eleven_v4", "elevenlabs"))
    assert _chat_default(_speech_ctx(), "speech", rows, rows) == "gpt-4o-mini-tts"
    # the owner's own speech default wins, ElevenLabs or not
    assert _chat_default(_speech_ctx("eleven_v4"), "speech", rows, rows) == "eleven_v4"
    # only ElevenLabs connected: it is still offered rather than nothing
    only = _rows(("eleven_flash_v2_5", "elevenlabs"))
    assert _chat_default(_speech_ctx(), "speech", only, only) == "eleven_flash_v2_5"


def _mp3(path: Path, header: bytes, size: int) -> None:
    path.write_bytes(header + b"\x00" * (size - len(header)))


def test_a_works_wall_mp3_has_a_length(tmp_path):
    """A speech / music work is indexed without a length (the tools do not
    report one); the chat reads it from the file so the transcription card
    can estimate. MPEG-1 (ElevenLabs, 44.1 kHz) and MPEG-2 (OpenAI TTS,
    24 kHz) Layer III both count."""
    from omniapi_mcp.chat.files import work_seconds

    m1 = tmp_path / "a.mp3"
    _mp3(m1, bytes([0xFF, 0xFB, 0x90, 0x64]), 160_000)  # MPEG-1 L3, 128 kbps -> 10 s
    m2 = tmp_path / "b.mp3"
    _mp3(m2, bytes([0xFF, 0xF3, 0x84, 0xC4]), 40_000)  # MPEG-2 L3, index 8 = 64 kbps -> 5 s
    assert work_seconds({"file_path": str(m1), "duration_s": None}) == pytest.approx(10.0)
    assert work_seconds({"file_path": str(m2), "duration_s": None}) == pytest.approx(5.0)
    assert work_seconds({"file_path": str(m1), "duration_s": 3.5}) == 3.5  # a stored length wins
    assert work_seconds({"file_path": str(tmp_path / "gone.mp3"), "duration_s": None}) is None


# ----------------------------------------------------------------- .mp4
def test_mp4_is_video_only_from_the_video_tool_or_folder():
    assert M.is_video_file("clip.mp4", tool="generate_video") and M.is_video_file("clip.mp4", folder="videos")
    assert M.is_video_file("clip.webm", folder="videos")
    assert not M.is_video_file("song.mp4", tool="generate_music")  # Suno's music video stays music
    assert not M.is_video_file("song.mp4", folder="music") and not M.is_video_file("clip.mp4")
    assert not M.is_video_file("a.png", tool="generate_video")


def test_mp4_is_audio_only_from_an_audio_tool_or_folder():
    assert M.is_audio_file("a.mp3") and M.is_audio_file("a.WAV")
    assert M.is_audio_file("song.mp4", tool="generate_music")
    assert M.is_audio_file("song.mp4", tool="music_utility")
    assert M.is_audio_file("song.mp4", folder="music")
    assert not M.is_audio_file("clip.mp4")
    assert not M.is_audio_file("clip.mp4", tool="generate_image")
    assert not M.is_audio_file("clip.mp4", folder="videos")
    assert not M.is_audio_file("a.png", tool="generate_music")
    # inputs: an .mp4 is still an upload and a transcription input
    assert ".mp4" in M.UPLOAD_AUDIO_EXTS and ".mp4" in M.TRANSCRIBE_INPUT_EXTS and ".mp4" in M.CHAT_AUDIO_EXTS
    assert ".mp4" not in M.AUDIO_EXTS


def _wav(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(8000)
        w.writeframes(struct.pack("<h", 0) * 800)


def test_backfill_of_an_existing_folder_is_unchanged(tmp_path):
    """The folders as v1.3 wrote them, .mp4 in each of the three audio-ish
    ones: every file is indexed as the same kind as before."""
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "img_20261001120000_a.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    _wav(tmp_path / "audio" / "speech_20261001120000_a.wav")
    (tmp_path / "audio" / "speech_20261001120001_b.mp3").write_bytes(b"ID3")
    (tmp_path / "audio" / "speech_20261001120002_c.mp4").write_bytes(b"x")
    (tmp_path / "music" / "2026-10-01").mkdir(parents=True)
    (tmp_path / "music" / "2026-10-01" / "music_20261001120000_a.mp4").write_bytes(b"x")
    (tmp_path / "music" / "2026-10-01" / "music_20261001120001_b.mp3").write_bytes(b"x")
    (tmp_path / "music" / "2026-10-01" / "lyrics_20261001120002.txt").write_text("[Verse]\nla", encoding="utf-8")
    (tmp_path / "music" / "2026-10-01" / "stem_vocals_20261001120003.wav").write_bytes(b"x")
    (tmp_path / "transcripts").mkdir()
    (tmp_path / "transcripts" / "transcript_20261001120000_a.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "transcripts" / "odd.mp4").write_bytes(b"x")
    (tmp_path / "videos").mkdir()  # 1.4-M3: the video tool's folder (unreadable files still count)
    (tmp_path / "videos" / "clip.mp4").write_bytes(b"x")
    (tmp_path / "videos" / "clip.mp4.part").write_bytes(b"x")  # a download that never finished: not a work

    found = {Path(f["file_path"]).name: f["kind"] for f in _scan(tmp_path)}
    assert found == {
        "img_20261001120000_a.png": "image",
        "speech_20261001120000_a.wav": "speech",
        "speech_20261001120001_b.mp3": "speech",
        "speech_20261001120002_c.mp4": "speech",
        "music_20261001120000_a.mp4": "music",
        "music_20261001120001_b.mp3": "music",
        "lyrics_20261001120002.txt": "lyrics",
        "stem_vocals_20261001120003.wav": "music",
        "transcript_20261001120000_a.txt": "transcript",
        "odd.mp4": "transcript",  # as before: whatever audio sits in transcripts/ is filed there
        "clip.mp4": "video",
    }
