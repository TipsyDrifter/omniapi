"""Tools a chat model may call — one registry, so the next tool is one class.

A chat reply may come back with tool calls next to its text (OpenAI-shaped:
``{id, type: "function", function: {name, arguments}}``). What happens to
them is decided here, not in the streaming loop:

* **which tools a turn offers** — ``ToolRegistry.specs(env)``: every
  registered tool's ``spec(env)`` (OpenAI function-tool form; ``None`` leaves
  it out this turn). The chat manager only asks when the routed model can
  call tools (its catalog ``tools`` flag) and the turn came from the GUI.
* **what one call becomes** — ``ChatTool.admit(args)``: a *record* (a dict
  with at least ``state``) stored on the assistant message under
  ``meta.tools[tool_call_id]``, or ``ToolArgsError`` to ignore the call. A
  reply makes at most ``max_per_reply`` calls of one tool; the rest are
  ignored too. Ignored calls are dropped from the stored ``tool_calls`` and
  listed in ``meta.tool_calls_ignored``, so the history never holds a call
  without a result.
* **what the model reads back** — ``ChatTool.result_text(record)``: the
  content of the ``tool`` message that answers the call, written the first
  time the record leaves an open state and rewritten as it changes.
* **who settles it** — a record in an ``OPEN_STATES`` state waits for the
  owner (``propose_generation``: the accept / decline endpoints). When the
  owner moves on instead (send, regenerate, edit), the manager settles it
  with ``auto_settle(record)``. Everything else is the tool's own business
  (``ProposeGeneration`` runs a generation job and is told how it ended).

Tools that run by themselves (``auto``, 1.2-M5): ``list_files``,
``read_file``, ``search_file`` read the files on the branch. The chat
manager runs them inside the turn and calls the model again with the
results, up to ``MAX_TOOL_ROUNDS`` rounds and ``MAX_TURN_READ_CHARS``
characters read per turn; they never leave a record or a tool message (what
was read is listed on the reply as ``meta.reads``). Proposals stay out of
that loop (決策記錄 1.2-M4-c): a proposal made in a round that also reads is
kept for the end of the turn and the model is told so.

``TranscriptionGate`` (1.2-M5, the owner's call 2026-10-03) is not offered to
any model: when the owner sends an audio file the routed model cannot hear,
the chat asks first — one record per file, the same proposal shape and
endpoints — and the reply runs once every file is transcribed or declined.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

#: states in which a call waits for the owner; anything else has a result message
OPEN_STATES = frozenset({"pending"})
#: states that are over (no further change expected)
FINAL_STATES = frozenset({"done", "declined", "failed", "cancelled"})
#: states a proposal may be accepted from: waiting, or tried and stopped, or
#: declined and reopened by the owner (D43)
ACCEPTABLE_STATES = frozenset({"pending", "failed", "cancelled", "declined"})


class ToolArgsError(ValueError):
    """The model's arguments cannot make a call: the call is ignored."""


@dataclass
class ToolEnv:
    """What a tool may look at when it describes itself for a turn."""

    ctx: Any = None  # the daemon's ServerContext (generation options); None in bare tests
    source: str = "gui"  # the door the turn came from: proposals need someone to press a button
    files: Any = None  # the branch's files (delivery.FileSet) — the file tools are offered only with some


def parse_arguments(raw: Any) -> dict[str, Any]:
    """A call's ``function.arguments`` (a JSON string, sometimes already an
    object, sometimes empty) → a dict, or ``ToolArgsError``."""
    if isinstance(raw, dict):
        return raw
    if raw in (None, ""):
        return {}
    try:
        val = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as e:
        raise ToolArgsError(f"arguments are not JSON: {e}") from e
    if not isinstance(val, dict):
        raise ToolArgsError("arguments are not an object")
    return val


class ChatTool:
    """One tool a chat model may call. Subclasses set ``name`` and override
    ``spec`` / ``admit`` / ``result_text``; ``view_key`` names the list the
    API shows its records under on the assistant message."""

    name: str = ""
    max_per_reply: int = 3
    view_key: str = "tool_calls_state"
    #: runs by itself inside the turn (1.2-M5): ``run`` instead of ``admit``
    auto: bool = False
    #: waits for the owner; accepting starts a generation job (``job``)
    confirmable: bool = False
    #: a settled record is answered by a ``tool`` message in the history
    writes_result: bool = True

    async def spec(self, env: ToolEnv) -> Optional[dict[str, Any]]:
        raise NotImplementedError

    async def run(self, args: dict[str, Any], files: Any, budget: "ReadBudget") -> "ToolRun":
        raise NotImplementedError

    async def job(self, record: dict[str, Any], env: ToolEnv, *, prompt: Optional[str] = None, model: Optional[str] = None,
                  params: Optional[dict[str, Any]] = None) -> tuple[str, dict[str, Any], Optional[dict[str, Any]]]:
        """``(generation kind, params, sources)`` for accepting ``record``."""
        raise NotImplementedError

    def admit(self, args: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def result_text(self, record: dict[str, Any]) -> str:
        return json.dumps({k: v for k, v in record.items() if k != "tool"}, ensure_ascii=False)

    def auto_settle(self, record: dict[str, Any]) -> dict[str, Any]:
        """The fields an open record takes when the owner moves on without deciding."""
        return {"state": "declined", "auto": True}


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ChatTool] = {}

    def register(self, tool: ChatTool) -> ChatTool:
        if not tool.name:
            raise ValueError("a chat tool needs a name")
        self._tools[tool.name] = tool
        return tool

    def get(self, name: Optional[str]) -> Optional[ChatTool]:
        return self._tools.get(name or "")

    def __iter__(self):
        return iter(self._tools.values())

    async def specs(self, env: ToolEnv) -> list[dict[str, Any]]:
        out = []
        for tool in self._tools.values():
            try:
                spec = await tool.spec(env)
            except Exception as e:  # a tool that cannot describe itself is left out, the chat goes on
                logger.warning("chat tool %s could not describe itself: %s", tool.name, e)
                spec = None
            if spec:
                out.append(spec)
        return out


# ---------------------------------------------------------------- propose_generation
GENERATION_KINDS = ("image", "speech")
_KIND_NAME = {"image": "圖片", "speech": "語音"}
MAX_PROMPT_CHARS = 4000
MAX_NOTE_CHARS = 300


def _price(pricing: Any, kind: str) -> str:
    """A catalog price as one short phrase the chat model can compare."""
    if not isinstance(pricing, dict):
        return "價格未知"
    unit = pricing.get("unit")
    if unit == "per_image":
        tiers = [(k, v) for k, v in pricing.items() if k in ("0.5K", "1K", "2K", "4K") and isinstance(v, (int, float))]
        if tiers:
            lo, hi = min(tiers, key=lambda t: t[1]), max(tiers, key=lambda t: t[1])
            return f"每張 ${lo[1]:g}（{lo[0]}）" + (f"～${hi[1]:g}（{hi[0]}）" if hi != lo else "")
    if unit == "per_1m_tokens":
        out = pricing.get("image_output") if kind == "image" else pricing.get("audio_output")
        return f"依 token 計價（輸出每百萬 token ${out:g}）" if isinstance(out, (int, float)) else "依 token 計價"
    if unit == "per_1k_chars" and isinstance(pricing.get("text"), (int, float)):
        return f"每千字 ${pricing['text']:g}"
    if unit == "per_1m_chars" and isinstance(pricing.get("text"), (int, float)):
        return f"每百萬字 ${pricing['text']:g}"
    return "價格未知"


#: kind -> {"models": [{id, name, pricing}], "default": id | None, "sandbox": bool}
Menu = dict[str, dict[str, Any]]


async def generation_menu(ctx: Any) -> Menu:
    """The image and speech models the generate page can call right now (the
    same data, 決策記錄 1.2-M4-a / D38), current ones only. In the offline
    sandbox every model is answered by a stand-in, so all of them are listed."""
    from ..devmode import dev_enabled, offline
    from ..generate.options import _default_model, models_for

    menu: Menu = {}
    for kind in GENERATION_KINDS:
        rows = await models_for(ctx, kind)
        usable = [r for r in rows if r.get("available") and r.get("status") == "current"]
        menu[kind] = {
            "models": [{"id": r["id"], "name": r.get("name") or r["id"], "pricing": r.get("pricing")} for r in usable],
            "default": _default_model(ctx, kind, rows) if usable else None,
            "sandbox": offline() and dev_enabled(),
        }
    return menu


def describe(menu: Menu) -> str:
    """The tool description: what it does, that nothing is spent until the
    owner presses the button, and the menu with prices."""
    lines = [
        "向主人提議生成一張圖片或一段語音。呼叫後不會立刻生成：對話裡會出現一張提議卡，"
        "主人按「生成」才會真的做（才花錢），也可以改提示詞或模型、或說不用了。"
        "主人想要圖片或語音、或明顯用得上時才提議；一次回覆最多 3 個。結果會在之後的工具訊息裡告訴你。",
    ]
    defaults = []
    for kind in GENERATION_KINDS:
        info = menu.get(kind) or {}
        models = info.get("models") or []
        if not models:
            lines.append(f"{_KIND_NAME[kind]}（kind={kind}）：目前沒有可用的模型，不要提議這一類。")
            continue
        note = "（離線沙盒：不呼叫供應商、不計費）" if info.get("sandbox") else ""
        lines.append(f"{_KIND_NAME[kind]}（kind={kind}）可用模型{note}：")
        lines += [f"- {m['id']}（{m['name']}）：{_price(m.get('pricing'), kind)}" for m in models]
        if info.get("default"):
            defaults.append(f"{kind}={info['default']}")
    if defaults:
        lines.append("預設模型：" + "；".join(defaults) + "。不確定時可以不填 model。")
    return "\n".join(lines)


class ProposeGeneration(ChatTool):
    """``propose_generation`` (決策記錄 1.2-M4-a): the model proposes, the owner
    decides (D36). A record is ``{state, kind, prompt, model?, voice?, note?,
    proposed}`` plus, as it goes, ``generation_id``, ``artifact``, ``error``,
    ``error_kind``, ``auto``, ``attempts``. ``prompt`` / ``model`` become what
    was actually used once accepted; ``proposed: {prompt, model?, voice?}``
    keeps what the model proposed.

    States: ``pending`` → ``generating`` (accepted) → ``done`` / ``failed`` /
    ``cancelled``; ``pending`` / ``failed`` / ``cancelled`` → ``declined`` (by
    the owner, or ``auto`` when the owner moved on). A failed, cancelled or
    declined one may be accepted again (``ACCEPTABLE_STATES``)."""

    name = "propose_generation"
    max_per_reply = 3
    view_key = "proposals"
    confirmable = True

    def __init__(self, menu: Optional[Callable[[Any], Awaitable[Menu]]] = None) -> None:
        self._menu = menu or generation_menu

    async def menu(self, env: ToolEnv) -> Menu:
        return await self._menu(env.ctx)

    async def spec(self, env: ToolEnv) -> Optional[dict[str, Any]]:
        if env.source != "gui":
            return None  # only the GUI has a button to press (1.2-M4)
        if env.ctx is None and self._menu is generation_menu:
            return None  # no daemon around to generate anything
        menu = await self.menu(env)
        if not any((menu.get(k) or {}).get("models") for k in GENERATION_KINDS):
            return None  # nothing could be generated: do not invite proposals
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": describe(menu),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": list(GENERATION_KINDS), "description": "image＝圖片；speech＝語音（朗讀 prompt 的文字）"},
                        "prompt": {"type": "string", "description": "圖片：生成用的提示詞；語音：要唸出來的完整文字"},
                        "model": {"type": "string", "description": "推薦的生成模型 id（從說明裡的清單挑；可省略）"},
                        "voice": {"type": "string", "description": "語音才用：聲音 id（可省略）"},
                        "note": {"type": "string", "description": "一句話說明為什麼這樣提議（可省略）"},
                    },
                    "required": ["kind", "prompt"],
                },
            },
        }

    def admit(self, args: dict[str, Any]) -> dict[str, Any]:
        kind = str(args.get("kind") or "").strip().lower()
        if kind not in GENERATION_KINDS:
            raise ToolArgsError(f"kind must be one of {list(GENERATION_KINDS)}")
        prompt = args.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ToolArgsError("prompt is empty")
        rec: dict[str, Any] = {"state": "pending", "kind": kind, "prompt": prompt.strip()[:MAX_PROMPT_CHARS]}
        for key, cap in (("model", 200), ("note", MAX_NOTE_CHARS)):
            val = args.get(key)
            if isinstance(val, str) and val.strip():
                rec[key] = val.strip()[:cap]
        if kind == "speech" and isinstance(args.get("voice"), str) and args["voice"].strip():
            rec["voice"] = args["voice"].strip()[:200]
        # what the model proposed, kept as it was: accepting overwrites prompt / model with
        # what was actually used, and the card still shows 「你改過」「模型擬的是」
        rec["proposed"] = {k: rec[k] for k in ("prompt", "model", "voice") if k in rec}
        return rec

    def result_text(self, record: dict[str, Any]) -> str:
        state = record.get("state")
        what = _KIND_NAME.get(record.get("kind") or "", "作品")
        if state == "generating":
            return f"主人按下了生成，{what}正在用 {record.get('model') or '預設模型'} 生成中，還沒好。"
        if state == "done":
            art = record.get("artifact") or {}
            return (f"{what}做好了：作品 {art.get('id')}（{art.get('name') or '檔案'}），模型 {record.get('model') or '—'}。"
                    "主人在對話裡與作品牆都看得到它。")
        if state == "declined":
            if record.get("auto"):
                return "主人沒有回應這個提議就繼續對話了，視為不用了，沒有生成。"
            return "主人說不用了，沒有生成。"
        if state == "failed":
            return f"生成失敗（{record.get('error_kind') or 'other'}）：{str(record.get('error') or '原因不明')[:300]}"
        if state == "cancelled":
            return "主人中止了生成，沒有作品。"
        return "主人還沒有決定。"

    async def request(self, record: dict[str, Any], env: ToolEnv, *, prompt: Optional[str] = None,
                      model: Optional[str] = None, params: Optional[dict[str, Any]] = None) -> tuple[str, dict[str, Any]]:
        """``(kind, generation params)`` for accepting ``record`` with the
        owner's edits. The model is the owner's pick, else the proposal's,
        and when that one is not usable (or none was named) the kind's
        default (D38; the generate page's last-used model lives in the
        browser, so the page sends it as ``model`` when it wants it)."""
        kind = record["kind"]
        menu = (await self.menu(env)).get(kind) or {}
        usable = {m["id"] for m in menu.get("models") or []}
        chosen = next((m for m in (model, record.get("model")) if isinstance(m, str) and m.strip() and m.strip() in usable), None)
        chosen = chosen or menu.get("default")
        text = prompt.strip() if isinstance(prompt, str) and prompt.strip() else record["prompt"]
        out = dict(params or {})
        out["prompt" if kind == "image" else "text"] = text
        if chosen:
            out["model"] = chosen
        if kind == "speech" and record.get("voice") and "voice" not in out:
            out["voice"] = record["voice"]
        return kind, out

    async def job(self, record: dict[str, Any], env: ToolEnv, *, prompt: Optional[str] = None, model: Optional[str] = None,
                  params: Optional[dict[str, Any]] = None) -> tuple[str, dict[str, Any], Optional[dict[str, Any]]]:
        kind, out = await self.request(record, env, prompt=prompt, model=model, params=params)
        return kind, out, None


# ---------------------------------------------------------------- file tools (1.2-M5)
#: one read_file answer, characters
MAX_READ_CHARS = 20_000
#: search_file hits listed
MAX_SEARCH_HITS = 20
#: rounds of automatic tools in one turn (each round is one more model call, one more cost)
MAX_TOOL_ROUNDS = 6
#: characters all reads of one turn may return
MAX_TURN_READ_CHARS = 150_000
#: what the model reads when the turn's reading is over
LIMIT_NOTE = "（這一回合讀檔的次數或字數已到上限，不能再讀了；請用已經讀到的內容直接回答，並說明有哪些沒讀到。）"


@dataclass
class ReadBudget:
    chars: int = MAX_TURN_READ_CHARS

    @property
    def spent(self) -> bool:
        return self.chars <= 0


@dataclass
class ToolRun:
    text: str  # what the model reads back
    read: Optional[dict[str, Any]] = None  # what the reply's meta.reads records: {tool, file_id, name, what, chars, error?}


def _file_list(files: Any) -> str:
    return "\n".join(f"- {f.name}（id: {f.id}；{'音檔' if f.kind == 'audio' else '檔案'}）" for f in files.unique())


class _FileTool(ChatTool):
    auto = True
    max_per_reply = 8
    writes_result = False

    async def spec(self, env: ToolEnv) -> Optional[dict[str, Any]]:
        if not env.files:
            return None
        return {"type": "function", "function": {"name": self.name, "description": self.describe(env.files), "parameters": self.parameters()}}

    def describe(self, files: Any) -> str:
        raise NotImplementedError

    def parameters(self) -> dict[str, Any]:
        raise NotImplementedError

    def admit(self, args: dict[str, Any]) -> dict[str, Any]:
        return {"state": "done"}


class ListFiles(_FileTool):
    name = "list_files"
    max_per_reply = 2

    def describe(self, files: Any) -> str:
        return "列出這段對話（目前這一條）附過的所有檔案：名稱、id、類型、大小、頁數／工作表與列數／投影片數、字數、讀不讀得了、這次怎麼送給你的。"

    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def run(self, args: dict[str, Any], files: Any, budget: ReadBudget) -> ToolRun:
        lines = []
        for f in files.unique():
            ex = await f.extract()
            sent = (f.sent or {}).get("mode")
            how = {"native": "原樣送出", "text": "全文已在訊息裡", "excerpt": "只放了開頭", "unreadable": "沒有送出"}.get(sent or "", "較早的訊息")
            state = "讀得了" if ex.readable else f"讀不了（{ex.info.get('reason_text') or ex.reason}）"
            sheets = ""
            if ex.info.get("sheets"):
                sheets = "；工作表：" + "、".join(f"{i}.{s['name']}（{s['rows']} 列）" for i, s in enumerate(ex.info["sheets"], 1))
            lines.append(f"- {f.name}｜id: {f.id}｜{f.facts()}｜{state}｜這次：{how}{sheets}")
        text = "\n".join(lines) or "（這段對話沒有附檔案）"
        return ToolRun(text, {"tool": self.name, "what": f"檔案清單（{len(lines)} 個）", "chars": len(text)})


class ReadFile(_FileTool):
    name = "read_file"

    def describe(self, files: Any) -> str:
        return ("讀一個附件檔案的一段文字（OmniAPI 在本機抽出的文字）。用 pages 讀 PDF 的頁或 PowerPoint 的投影片（例如 \"3-7\"）；"
                "用 sheet（名稱或第幾個）加 rows（例如 \"1-200\"）讀 Excel；用 lines 讀第幾行（CSV 的第幾列也用 lines）；"
                f"或用 start＋length 讀字元範圍。一次最多回 {MAX_READ_CHARS:,} 字，太長會告訴你下一段從哪裡接。"
                "不確定在哪裡時先用 search_file 找。這段對話裡的檔案：\n" + _file_list(files))

    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {
            "file": {"type": "string", "description": "檔案 id（或檔名）"},
            "pages": {"type": "string", "description": "PDF 頁碼或投影片範圍，例如 \"3\" 或 \"3-7\""},
            "sheet": {"type": "string", "description": "Excel 工作表名稱或序號（1 起算）"},
            "rows": {"type": "string", "description": "配合 sheet：列範圍，例如 \"1-200\"（第 1 列通常是標題）"},
            "lines": {"type": "string", "description": "行範圍，例如 \"100-200\"（文字檔、程式碼、CSV）"},
            "start": {"type": "integer", "description": "從第幾個字開始（0 起算）"},
            "length": {"type": "integer", "description": f"讀幾個字（最多 {MAX_READ_CHARS:,}）"},
        }, "required": ["file"]}

    async def run(self, args: dict[str, Any], files: Any, budget: ReadBudget) -> ToolRun:
        from . import files as F

        try:
            f = files.find(args.get("file"))
        except KeyError as e:
            return ToolRun(str(e.args[0]), {"tool": self.name, "what": "找不到檔案", "chars": 0, "error": True})
        ex = await f.extract()
        if not ex.readable:
            why = ex.info.get("reason_text") or F.REASONS.get(ex.reason or "", ex.reason)
            return ToolRun(f"{f.name} 讀不了：{why}", {"tool": self.name, "file_id": f.id, "name": f.name, "what": "讀不了", "chars": 0, "error": True})
        if budget.spent:
            return ToolRun(LIMIT_NOTE, {"tool": self.name, "file_id": f.id, "name": f.name, "what": "已到讀取上限", "chars": 0, "error": True})
        try:
            got = F.read_range(ex, start=args.get("start"), length=args.get("length"), lines=args.get("lines"), pages=args.get("pages"),
                               sheet=args.get("sheet"), rows=args.get("rows"), cap=min(MAX_READ_CHARS, budget.chars))
        except ValueError as e:
            return ToolRun(f"參數不對：{e}", {"tool": self.name, "file_id": f.id, "name": f.name, "what": "參數不對", "chars": 0, "error": True})
        budget.chars -= len(got.text)
        head = f"{f.name} {got.label}（{len(got.text):,} 字；全檔 {ex.chars:,} 字）："
        tail = f"\n（{got.more}）" if got.more else ""
        return ToolRun(f"{head}\n{got.text}{tail}", {"tool": self.name, "file_id": f.id, "name": f.name, "what": got.label, "chars": len(got.text)})


class SearchFile(_FileTool):
    name = "search_file"

    def describe(self, files: Any) -> str:
        return (f"在附件檔案的文字裡找關鍵字（不分大小寫），回傳最多 {MAX_SEARCH_HITS} 個命中的位置（頁、工作表與列、或行）與前後文，"
                "再用 read_file 讀那一段。不給 file 就找全部檔案。這段對話裡的檔案：\n" + _file_list(files))

    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {
            "query": {"type": "string", "description": "要找的字"},
            "file": {"type": "string", "description": "檔案 id（或檔名）；省略＝全部檔案"},
        }, "required": ["query"]}

    async def run(self, args: dict[str, Any], files: Any, budget: ReadBudget) -> ToolRun:
        from . import files as F

        query = str(args.get("query") or "").strip()
        if not query:
            return ToolRun("參數不對：query 是空的", {"tool": self.name, "what": "參數不對", "chars": 0, "error": True})
        try:
            targets = [files.find(args["file"])] if args.get("file") else files.unique()
        except KeyError as e:
            return ToolRun(str(e.args[0]), {"tool": self.name, "what": "找不到檔案", "chars": 0, "error": True})
        lines, total, left = [], 0, MAX_SEARCH_HITS
        for f in targets:
            ex = await f.extract()
            if not ex.readable:
                continue
            hits, n = F.search(ex, query, max_hits=left)
            total += n
            left -= len(hits)
            lines += [f"- {f.name}（id: {f.id}）{h.where}，第 {h.offset:,} 字：{h.snippet}" for h in hits]
        if not total:
            text = f"找不到「{query}」。"
        else:
            text = f"「{query}」共 {total} 處" + (f"，列出前 {MAX_SEARCH_HITS} 處" if total > MAX_SEARCH_HITS else "") + "：\n" + "\n".join(lines)
        budget.chars -= len(text)
        name = targets[0].name if len(targets) == 1 else None
        return ToolRun(text, {"tool": self.name, "file_id": targets[0].id if name else None, "name": name,
                              "what": f"搜尋「{query[:40]}」（{total} 處）", "chars": len(text)})


# ---------------------------------------------------------------- the transcription question (1.2-M5)
async def transcription_default(ctx: Any) -> Optional[str]:
    """The transcription model the card offers first (the generate page's default)."""
    if ctx is None:
        return None
    from ..generate.options import _default_model, models_for

    rows = await models_for(ctx, "transcript")
    return _default_model(ctx, "transcript", rows) if any(r.get("available") for r in rows) else None


class TranscriptionGate(ChatTool):
    """Not a tool any model calls: the chat's own question before a reply
    whose message carries audio the routed model cannot hear. A record is
    ``{gate: True, state, kind: "transcript", prompt: "", model, file: {id,
    name, kind, bytes, duration_s}, duration_s, ref}`` — the proposal shape
    (same states, endpoints and ``chat.proposal`` events). Accepting runs
    the v1.1 transcription job on that file; the transcript becomes a work
    and the file's text. Declining (also from failed / cancelled) means
    "reply without it"."""

    name = "transcribe_audio"
    view_key = "proposals"
    confirmable = True
    writes_result = False
    max_per_reply = 10

    def __init__(self, default: Optional[Callable[[Any], Awaitable[Optional[str]]]] = None) -> None:
        self._default = default or transcription_default

    async def spec(self, env: ToolEnv) -> Optional[dict[str, Any]]:
        return None

    async def record(self, f: Any, env: ToolEnv) -> dict[str, Any]:
        model = await self._default(env.ctx)
        rec: dict[str, Any] = {"tool": self.name, "gate": True, "state": "pending", "kind": "transcript", "prompt": "",
                               "file": {"id": f.id, "name": f.name, "kind": f.kind, "bytes": f.bytes, "duration_s": f.duration_s},
                               "ref": dict(f.ref)}
        if model:
            rec["model"] = model
        if f.duration_s:
            rec["duration_s"] = f.duration_s
        return rec

    async def job(self, record: dict[str, Any], env: ToolEnv, *, prompt: Optional[str] = None, model: Optional[str] = None,
                  params: Optional[dict[str, Any]] = None) -> tuple[str, dict[str, Any], Optional[dict[str, Any]]]:
        out = {k: v for k, v in (params or {}).items() if k in ("language", "response_format", "temperature")}
        chosen = model or record.get("model") or await self._default(env.ctx)
        if chosen:
            out["model"] = chosen
        if isinstance(prompt, str) and prompt.strip():
            out["prompt"] = prompt.strip()[:MAX_PROMPT_CHARS]  # a transcription prompt is a vocabulary hint
        return "transcript", out, {"audio": dict(record["ref"])}


def default_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(ProposeGeneration())
    reg.register(ListFiles())
    reg.register(ReadFile())
    reg.register(SearchFile())
    reg.register(TranscriptionGate())
    return reg
