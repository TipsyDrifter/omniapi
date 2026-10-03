"""How each attached file reaches the routed model (1.2-M5, D41).

``decide()`` is the one place that answers it, per file, given the routed
model and provider (``NativeSupport``), whether the turn offers the file
tools, and what is left of the request's budgets (``Budget``):

* ``native``     — sent as it is, so the model sees layout and charts:
  a PDF as Anthropic's ``document`` block (``format: document``) or as the
  OpenAI-style ``file`` part (``format: file``: OpenAI Chat Completions,
  OpenRouter); an audio file as ``input_audio`` (``format: input_audio``).
* ``text``       — its extracted text, whole, inside the message.
* ``excerpt``    — too long to inline: what it is (pages, rows, chars) and
  its opening; the rest is for the tools to read (or, without tools, the
  model is told it only saw the beginning).
* ``unreadable`` — nothing to give: an unknown binary, a broken or
  encrypted file, an audio file the model cannot hear and nobody has
  transcribed. The model is told so, plainly (D41: never pretend).

Which provider takes what natively (docs checked 2026-10-03, see
docs/research/2026-10-03-各家API檔案輸入與程式執行查證.md; the numbers below
are from summaries and are to be re-checked against the official pages):
Anthropic takes PDF (32 MB per request, 100 pages under a 1M context);
OpenAI's Chat Completions takes PDF as a ``file`` part and audio only on its
audio models (none of which the catalog routes, so no audio there unless a
model is flagged ``audio_input``); OpenRouter takes ``file`` / ``input_audio``
per model (``architecture.input_modalities``); Gemini's OpenAI-compatible
endpoint documents ``input_audio`` but says nothing about PDF — so Gemini
always gets extracted text for a PDF (決策記錄 1.2-M5-d); DeepSeek takes
text and images only.

A file sent natively is still readable through the tools when the turn
offers them (the model may want the exact text).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import files as F

# ---------------------------------------------------------------- budgets (決策記錄 1.2-M5-e)
#: one file inlined whole up to this many characters (~10k tokens of English,
#: up to ~30k of Chinese — a few percent of the smallest context we route to)
MAX_INLINE_FILE_CHARS = 30_000
#: all inlined file text of one request; past it the older files drop to excerpts
MAX_INLINE_TOTAL_CHARS = 60_000
#: the opening an excerpt carries
EXCERPT_CHARS = 3_000
#: one natively sent file (PDF / audio) after base64
NATIVE_MAX_FILE_B64 = 10 * 1024 * 1024
#: everything inline in one request after base64 — images included — per
#: provider, under each documented request cap (Anthropic 32 MB; Gemini 20 MB
#: inline; OpenAI 50 MB of files; OpenRouter undocumented: Gemini's figure)
NATIVE_REQUEST_B64 = {"anthropic": 28 * 1024 * 1024, "openai": 40 * 1024 * 1024, "openrouter": 18 * 1024 * 1024,
                      "google": 18 * 1024 * 1024, "echo": 18 * 1024 * 1024}
#: PDF pages sent natively in one request (Anthropic: 100 under a 1M context)
NATIVE_PDF_MAX_PAGES = 100
#: the audio formats ``input_audio`` takes everywhere it exists
NATIVE_AUDIO_FORMATS = ("wav", "mp3")


@dataclass(frozen=True)
class NativeSupport:
    provider: str = ""
    pdf: Optional[str] = None  # None | "document" (Anthropic block) | "file" (OpenAI-style part)
    audio: bool = False

    @property
    def budget(self) -> int:
        return NATIVE_REQUEST_B64.get(self.provider, 0)


def native_support(provider: Any, model: str) -> NativeSupport:
    """What the routed model takes as it is (see the module docstring)."""
    from ..capabilities.echo import echo_audio, echo_files
    from ..catalog import catalog

    key = getattr(provider, "PROVIDER_KEY", None) or ""
    if key == "echo":
        return NativeSupport("echo", "file" if echo_files(model) else None, echo_audio(model))
    entry = catalog.get(model, key) if key else None
    if entry is None:  # a model the catalog does not know: text only, like images (決策記錄 1.2-M2-a)
        return NativeSupport(key)
    # the rules live on the catalog entry (ModelEntry.pdf / .audio) so /api/models says the same
    pdf = ("document" if key == "anthropic" else "file") if entry.pdf else None
    return NativeSupport(key, pdf, entry.audio)


# ---------------------------------------------------------------- files on a branch
@dataclass
class BranchFile:
    """A non-image attachment on the branch a reply answers."""

    key: str  # attachment key (message id : index)
    id: str  # upload id or work id: the handle the tools take
    ref: dict[str, str]  # {upload_id} | {artifact_id}
    kind: str  # file | audio (the attachment kind)
    name: str
    message_id: str
    path: Optional[Path] = None
    mime: Optional[str] = None
    bytes: Optional[int] = None
    text_work: Optional[str] = None  # a lyrics / transcript work's body (no file to parse)
    work_kind: Optional[str] = None
    transcript_id: Optional[str] = None  # the transcript that makes an audio file readable
    transcript_text: Optional[str] = None
    duration_s: Optional[float] = None
    extracted: Optional[F.Extracted] = None
    sent: Optional[dict[str, Any]] = None  # how this turn sent it (Delivery.meta), for list_files

    @property
    def missing(self) -> bool:
        return self.text_work is None and (self.path is None or not self.path.is_file())

    async def extract(self) -> F.Extracted:
        """The file's text (cached), the transcript's for a transcribed audio file."""
        if self.extracted is not None:
            return self.extracted
        if self.kind == "audio":
            if self.transcript_text is not None:
                ex = F.from_text("audio", self.transcript_text, transcript=self.transcript_id)
            else:
                ex = F.unreadable("audio", "audio")
        elif self.text_work is not None:
            ex = F.from_text(self.work_kind or "text", self.text_work)
        elif self.missing:
            ex = F.unreadable("binary", "missing")
        else:
            try:
                ex = await F.aextract(self.path, self.name)
            except FileNotFoundError:
                ex = F.unreadable("binary", "missing")
        self.extracted = ex
        return ex

    def facts(self) -> str:
        """What it is, in a few words: 「PDF，12 頁，約 12,400 字」."""
        ex = self.extracted
        bits = [F.TYPE_NAMES.get(ex.type, ex.type) if ex else ("音檔" if self.kind == "audio" else "檔案")]
        info = ex.info if ex else {}
        if info.get("pages"):
            bits.append(f"{info['pages']} 頁")
        if info.get("slides"):
            bits.append(f"{info['slides']} 張投影片")
        if info.get("sheets"):
            bits.append(f"{len(info['sheets'])} 個工作表、{info.get('rows', 0):,} 列")
        elif info.get("rows"):
            bits.append(f"{info['rows']:,} 列")
        if self.kind == "audio" and self.duration_s:
            bits.append(f"{self.duration_s:.0f} 秒")
        if ex is not None and ex.readable:
            bits.append(f"{ex.chars:,} 字")
        elif self.bytes:
            bits.append(f"{self.bytes:,} bytes")
        return "，".join(bits)


class FileSet:
    """The files on a branch, oldest first, addressable by id or name."""

    def __init__(self, items: Optional[list[BranchFile]] = None) -> None:
        self.items: list[BranchFile] = list(items or [])

    def __bool__(self) -> bool:
        return bool(self.items)

    def __iter__(self):
        return iter(self.items)

    def unique(self) -> list[BranchFile]:
        """One entry per id (the newest occurrence), oldest first."""
        seen: dict[str, BranchFile] = {}
        for f in self.items:
            seen.pop(f.id, None)
            seen[f.id] = f
        return list(seen.values())

    def find(self, handle: Any) -> BranchFile:
        """By id, else by exact name, else by a name it uniquely contains; ``KeyError`` with a hint."""
        h = str(handle or "").strip()
        files = self.unique()
        if not h:
            if len(files) == 1:
                return files[0]
            raise KeyError("要指定 file（檔案 id 或檔名）")
        for f in files:
            if f.id == h:
                return f
        for f in files:
            if f.name == h:
                return f
        near = [f for f in files if h.lower() in f.name.lower()]
        if len(near) == 1:
            return near[0]
        raise KeyError("找不到這個檔案；這段對話裡的檔案：" + "、".join(f"{f.name}（id: {f.id}）" for f in files))


# ---------------------------------------------------------------- the decision
@dataclass
class Budget:
    chars: int = MAX_INLINE_TOTAL_CHARS
    native_b64: int = 0
    pdf_pages: int = NATIVE_PDF_MAX_PAGES


@dataclass
class Delivery:
    mode: str  # native | text | excerpt | unreadable
    parts: list[dict[str, Any]] = field(default_factory=list)  # OpenAI-style content parts
    meta: dict[str, Any] = field(default_factory=dict)  # what the reply's meta.files records


def _b64_size(n_bytes: int) -> int:
    return (n_bytes + 2) // 3 * 4


def _header(f: BranchFile, tail: str) -> str:
    return f"[附件 {f.name}（id: {f.id}；{f.facts()}）{tail}]"


def _cut(text: str, n: int) -> str:
    """The first ``n`` characters, ended at a line break when one is near."""
    if len(text) <= n:
        return text
    head = text[:n]
    nl = head.rfind("\n")
    return head[:nl] if nl > n * 0.6 else head


def decide(f: BranchFile, support: NativeSupport, budget: Budget, *, tools: bool) -> Delivery:
    """How ``f`` goes to the model (see the module docstring). Spends from
    ``budget``; call it newest file first so the older ones are the ones
    that drop to excerpts. ``f.extract()`` must have run."""
    ex = f.extracted or F.unreadable("binary", "missing")
    base: dict[str, Any] = {"id": f.id, "name": f.name, "kind": f.kind, "type": ex.type, "message_id": f.message_id}
    read_hint = "；需要精確文字可以用 read_file 讀" if tools and ex.readable else ""

    if f.missing:
        meta = {**base, "mode": "unreadable", "reason": "missing", "reason_text": F.REASONS["missing"]}
        return Delivery("unreadable", [{"type": "text", "text": _header(f, "：檔案已不在，沒有內容可以給你，不要假裝看過")}], meta)

    # audio: heard as it is, else its transcript, else nothing to give
    if f.kind == "audio":
        fmt = F.audio_format(f.path) if f.path is not None else None
        size = _b64_size(f.bytes or (f.path.stat().st_size if f.path else 0))
        if support.audio and fmt in NATIVE_AUDIO_FORMATS and size <= NATIVE_MAX_FILE_B64 and size <= budget.native_b64:
            budget.native_b64 -= size
            data = base64.b64encode(f.path.read_bytes()).decode("ascii")
            parts = [{"type": "text", "text": _header(f, "：原樣附上這段錄音" + read_hint)},
                     {"type": "input_audio", "input_audio": {"data": data, "format": fmt}}]
            return Delivery("native", parts, {**base, "mode": "native", "format": "input_audio", "audio_format": fmt})
        if not ex.readable:
            why = "這個模型聽不到音檔" if not support.audio else (
                "這個音檔的格式不能原樣送（只收 wav、mp3）" if fmt not in NATIVE_AUDIO_FORMATS else "這個音檔太大，不能原樣送")
            tail = f"：{why}，也沒有轉成文字，內容未知；照實告訴主人你聽不到，不要假裝聽過"
            meta = {**base, "mode": "unreadable", "reason": "audio_unheard", "reason_text": why + "，也沒有轉成文字"}
            return Delivery("unreadable", [{"type": "text", "text": _header(f, tail)}], meta)
        base.update(transcribed=True, transcript_id=f.transcript_id)  # fall through: its transcript is its text

    # a PDF the model reads as it is
    elif ex.type == "pdf" and support.pdf and ex.reason not in ("encrypted", "corrupt", "empty"):
        pages = int(ex.info.get("pages") or 0)
        size = _b64_size(f.bytes or f.path.stat().st_size)
        if pages <= budget.pdf_pages and size <= NATIVE_MAX_FILE_B64 and size <= budget.native_b64:
            budget.native_b64 -= size
            budget.pdf_pages -= pages
            data = base64.b64encode(f.path.read_bytes()).decode("ascii")
            parts = [{"type": "text", "text": _header(f, "：原樣附上（看得到版面與圖表）" + read_hint)},
                     {"type": "file", "file": {"filename": f.name, "file_data": f"data:application/pdf;base64,{data}"}}]
            return Delivery("native", parts, {**base, "mode": "native", "format": support.pdf, "pages": pages})

    if not ex.readable:
        reason = ex.reason or "binary"
        why = ex.info.get("reason_text") or F.REASONS.get(reason, reason)
        meta = {**base, "mode": "unreadable", "reason": reason, "reason_text": why}
        return Delivery("unreadable", [{"type": "text", "text": _header(f, f"：讀不了——{why}。沒有內容可以給你，不要假裝看過")}], meta)

    note = ""
    if ex.info.get("truncated"):
        note = "（抽取時有截斷：" + "；".join(ex.info.get("truncated_notes") or []) + "）"
    if ex.chars <= MAX_INLINE_FILE_CHARS and ex.chars <= budget.chars:
        budget.chars -= ex.chars
        body = f"{_header(f, '：全文如下' + note)}\n{ex.text}\n[附件 {f.name} 結束]"
        return Delivery("text", [{"type": "text", "text": body}], {**base, "mode": "text", "chars": ex.chars,
                                                                   **({"truncated": True} if note else {})})
    head = _cut(ex.text, EXCERPT_CHARS)
    budget.chars = max(0, budget.chars - len(head))
    if tools:
        tail = f"：太長，只放了開頭 {len(head):,} 字{note}；其餘用 read_file（可指定頁、工作表與列、行或字數範圍）或 search_file 讀"
    else:
        tail = f"：太長，你只看得到開頭 {len(head):,} 字{note}，其餘的內容這次讀不到；回答時要說明只看了開頭"
    body = f"{_header(f, tail)}\n{head}\n[附件 {f.name} 開頭到此為止]"
    meta = {**base, "mode": "excerpt", "chars": ex.chars, "sent_chars": len(head), "tools": tools, **({"truncated": True} if note else {})}
    if ex.type in ("csv", "xlsx") and ex.info.get("rows"):
        # rows of the opening (an xlsx opening starts with its first sheet's header line)
        meta.update(rows=ex.info["rows"], sent_rows=max(0, head.count("\n") + 1 - (1 if ex.type == "xlsx" else 0)))
    return Delivery("excerpt", [{"type": "text", "text": body}], meta)


def summarize(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """A reply's file report (meta), the counts in the style of ``images_*``."""
    count = lambda mode: sum(1 for e in entries if e.get("mode") == mode)  # noqa: E731
    return {"files": entries, "files_native": count("native"), "files_text": count("text"), "files_excerpt": count("excerpt"),
            "files_unreadable": count("unreadable"), "files_missing": sum(1 for e in entries if e.get("reason") == "missing")}


def describe_delivery(e: dict[str, Any]) -> str:
    """One file's line for the export: 「原樣送出」「抽出文字 12,400 字」…"""
    mode = e.get("mode")
    if mode == "native":
        return "原樣送出" + ("（音訊）" if e.get("format") == "input_audio" else "")
    if mode == "text":
        if e.get("transcribed"):
            return f"轉成文字後送出（逐字稿 {int(e.get('chars') or 0):,} 字）"
        return f"抽出文字 {int(e.get('chars') or 0):,} 字"
    if mode == "excerpt":
        rest = "其餘模型用工具自己讀" if e.get("tools") else "模型只看到開頭"
        if e.get("rows"):
            return f"只送了前 {int(e.get('sent_rows') or 0):,} 列（共 {int(e['rows']):,} 列），{rest}"
        return f"只送了開頭 {int(e.get('sent_chars') or 0):,} 字（共 {int(e.get('chars') or 0):,} 字），{rest}"
    return f"讀不了：{e.get('reason_text') or e.get('reason') or '原因不明'}"
