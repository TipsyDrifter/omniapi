"""The generation modalities, in one place.

Every place that used to list the kinds by hand (the generate page's request
check, its options and estimates, the default-model settings, the works
index, the chat's attachments, the CLI) now derives from ``GENERATION`` or
checks itself against it with :func:`require_keys` — so a kind added here
and forgotten elsewhere fails at import (service start) and in the tests,
instead of quietly falling back to "text" or "music".

Two names per kind, kept on purpose (they are public API on both sides):

* ``kind``    — what a generation job and a work are called (``transcript``)
* ``catalog`` — the model catalog's modality (``transcription``)

The GUI mirrors this table in ``gui/src/lib/modalities.ts``;
``tests/unit/test_modalities.py`` reads that file and fails when the two
disagree.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal, Mapping, Optional

Media = Literal["image", "audio", "text", "video"]


@dataclass(frozen=True)
class Modality:
    #: generation / works kind (``/make/:kind``, ``artifacts.kind``)
    kind: str
    #: the model catalog's modality for this kind
    catalog: str
    #: what the files of this kind are: decides thumbnails, players, which
    #: source slot a work may fill, how a work is attached to a chat
    media: Media
    #: the word the owner reads (chat tool descriptions, export)
    label: str
    #: the MCP tool a plain request of this kind runs
    tool: str
    #: the tool that runs instead when the request carries source files
    #: (an image with a source image is an edit)
    source_tool: Optional[str] = None
    #: the source slot this kind reads (``images`` / ``audio``), if any
    source_slot: Optional[str] = None
    #: ``True``: a request without a source file is refused
    source_required: bool = False
    #: more MCP tools whose output is a work of this kind
    other_tools: tuple[str, ...] = ()
    #: the folder under the storage base its files are written to
    folder: str = ""

    @property
    def tools(self) -> tuple[str, ...]:
        """Every tool whose output is a work of this kind."""
        return tuple(dict.fromkeys(t for t in (self.tool, self.source_tool, *self.other_tools) if t))


#: the generation kinds, in the order the generate page shows them (video last:
#: the four older tabs keep their places)
GENERATION: tuple[Modality, ...] = (
    Modality("image", "image", "image", "圖片", "generate_image", source_tool="edit_image", source_slot="images",
             folder="images"),
    Modality("speech", "speech", "audio", "語音", "generate_speech", folder="audio"),
    Modality("music", "music", "audio", "音樂", "generate_music",
             other_tools=("edit_music", "compose_music", "music_utility"), folder="music"),
    Modality("transcript", "transcription", "text", "逐字稿", "transcribe_audio", source_slot="audio",
             source_required=True, folder="transcripts"),
    # 1.4-M3: a video may start from a first (and a last) frame: image works or uploads
    Modality("video", "video", "video", "影片", "generate_video", source_slot="frames", folder="videos"),
)

KINDS: tuple[str, ...] = tuple(m.kind for m in GENERATION)
BY_KIND: dict[str, Modality] = {m.kind: m for m in GENERATION}
#: generation kind -> catalog modality
CATALOG_OF: dict[str, str] = {m.kind: m.catalog for m in GENERATION}
#: catalog modality -> generation kind
KIND_OF_CATALOG: dict[str, str] = {m.catalog: m.kind for m in GENERATION}

#: works that are not a generation kind of their own: lyrics come out of the
#: music tools (``music_lyrics``, or a song's lyrics file)
EXTRA_WORK_KINDS: dict[str, tuple[Media, tuple[str, ...]]] = {"lyrics": ("text", ("music_lyrics",))}
#: every kind a work can have
WORK_KINDS: tuple[str, ...] = KINDS + tuple(EXTRA_WORK_KINDS)
#: work kind -> media
MEDIA_OF: dict[str, Media] = {**{m.kind: m.media for m in GENERATION}, **{k: v[0] for k, v in EXTRA_WORK_KINDS.items()}}

#: the catalog's modalities, in the catalog's own order (text is not generated: chats use it)
CATALOG_MODALITIES: tuple[str, ...] = ("text", "image", "transcription", "speech", "music", "video")

#: tool -> the kind of work it produces (the works index)
KIND_BY_TOOL: dict[str, str] = {
    **{t: m.kind for m in GENERATION for t in m.tools},
    **{t: k for k, (_media, tools) in EXTRA_WORK_KINDS.items() for t in tools},
}

#: source slot -> (work kinds that may fill it, upload kinds that may fill it)
SOURCE_SLOTS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "images": (frozenset(k for k, md in MEDIA_OF.items() if md == "image" and k in KINDS), frozenset({"image"})),
    "audio": (frozenset(k for k, md in MEDIA_OF.items() if md == "audio" and k in KINDS), frozenset({"audio"})),
    # a video's first / last frame: ``{"first": ref, "last": ref}``, each an image work or upload
    "frames": (frozenset(k for k, md in MEDIA_OF.items() if md == "image" and k in KINDS), frozenset({"image"})),
}


def tool_for(kind: str, has_source: bool) -> str:
    """The tool a request of ``kind`` runs. Unknown kind: ``KeyError``."""
    m = BY_KIND[kind]
    return m.source_tool if has_source and m.source_tool else m.tool


def require_keys(mapping: Mapping[str, object] | Iterable[str], expected: Iterable[str], what: str) -> None:
    """Raise when ``mapping`` (or a set of names) does not cover exactly
    ``expected``. Called at import next to every per-kind table, so a kind
    added to ``GENERATION`` but not to the table stops the service from
    starting and fails every test that imports the module."""
    have, want = set(mapping), set(expected)
    if have != want:
        missing, extra = sorted(want - have), sorted(have - want)
        raise RuntimeError(f"{what} is out of step with omniapi_mcp.modalities: missing {missing}, unknown {extra}")


# ----------------------------------------------------------------- file types
# Which extension is which medium. An ``.mp4`` is a container: the music tools
# write one (Suno's music video), and a video tool will. It is never audio by
# its extension alone — who wrote it (the tool) or where it sits (the folder)
# decides, so a future video does not land in the music tab.

#: images the works index knows
IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
#: always audio, whoever wrote them
AUDIO_EXTS = frozenset({".mp3", ".wav", ".ogg", ".opus", ".flac", ".aac", ".m4a", ".pcm"})
#: audio or video, decided by the tool or the folder
AV_CONTAINER_EXTS = frozenset({".mp4"})
#: what the GUI takes as an audio upload (an input for transcription: the
#: audio track of an .mp4 is what gets transcribed)
UPLOAD_AUDIO_EXTS = (AUDIO_EXTS - {".pcm"}) | AV_CONTAINER_EXTS | {".webm", ".mpga", ".mpeg"}
#: the containers OpenAI's audio API documents for transcription input
TRANSCRIBE_INPUT_EXTS = frozenset({".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".wav", ".webm", ".flac", ".ogg"})
#: what a chat attachment counts as audio by its extension (after its first bytes)
CHAT_AUDIO_EXTS = UPLOAD_AUDIO_EXTS | {".aiff", ".aif"}

#: tools whose output is audio, and the folders they write to
_AUDIO_TOOLS = frozenset(t for m in GENERATION if m.media == "audio" for t in m.tools)
#: folders whose files are audio unless they are text; the transcripts folder
#: is listed too because the backfill has always taken audio files found there
_AUDIO_FOLDERS = frozenset(m.folder for m in GENERATION if m.media in ("audio", "text"))


def is_audio_file(path: str | Path, *, tool: Optional[str] = None, folder: Optional[str] = None) -> bool:
    """Is a generated file audio? An always-audio extension is; an ``.mp4``
    only when the tool that wrote it is an audio tool or it sits in an audio
    tool's folder. Neither known: not audio."""
    ext = Path(path).suffix.lower()
    if ext in AUDIO_EXTS:
        return True
    if ext in AV_CONTAINER_EXTS:
        return (tool is not None and tool in _AUDIO_TOOLS) or (folder is not None and folder in _AUDIO_FOLDERS)
    return False


#: the containers a video tool writes (OpenRouter answers .mp4; the others are
#: what a browser plays, kept for a future vendor)
VIDEO_EXTS = AV_CONTAINER_EXTS | {".webm", ".mov"}
_VIDEO_TOOLS = frozenset(t for m in GENERATION if m.media == "video" for t in m.tools)
_VIDEO_FOLDERS = frozenset(m.folder for m in GENERATION if m.media == "video")


def is_video_file(path: str | Path, *, tool: Optional[str] = None, folder: Optional[str] = None) -> bool:
    """Is a generated file a video? A video container written by a video tool
    or sitting in a video tool's folder. A music tool's ``.mp4`` stays music."""
    if Path(path).suffix.lower() not in VIDEO_EXTS:
        return False
    if tool is not None and tool in _AUDIO_TOOLS:
        return False
    return (tool is not None and tool in _VIDEO_TOOLS) or (folder is not None and folder in _VIDEO_FOLDERS)


def _self_check() -> None:
    if len(set(KINDS)) != len(KINDS) or len(set(CATALOG_OF.values())) != len(KINDS):
        raise RuntimeError("modalities: duplicate kind or catalog modality")
    require_keys(set(CATALOG_MODALITIES) - {"text"}, CATALOG_OF.values(), "CATALOG_MODALITIES")
    for m in GENERATION:
        if m.source_required and m.source_slot is None:
            raise RuntimeError(f"modalities: {m.kind} requires a source but names no slot")
        if m.source_slot is not None and m.source_slot not in SOURCE_SLOTS:
            raise RuntimeError(f"modalities: {m.kind} reads unknown source slot {m.source_slot!r}")
        if not m.folder:
            raise RuntimeError(f"modalities: {m.kind} has no storage folder")


_self_check()
