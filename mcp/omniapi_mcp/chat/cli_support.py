"""Helpers for the two non-GUI chat entry points (M6-k).

* ``reply_summary`` — the result shape shared by the MCP ``chat`` tool and
  ``omni chat --json``.
* Everything else serves ``omni chat``: parsing the REPL's slash commands,
  turning ``/ws`` events into terminal output, and driving one turn against
  the daemon's REST + WebSocket (with a fallback to ``?wait=true`` when the
  socket cannot be opened).

Kept free of typer so the pieces can be tested on their own; output goes
through the ``out`` / ``err`` callables the caller passes in.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import unquote

Writer = Callable[[str], None]


# ---------------------------------------------------------------- shared result shape
def gui_url(host: str, port: int | str, cid: str) -> str:
    return f"http://{host}:{port}/chat/{cid}"


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return "" if content is None else str(content)


def reply_summary(
    *,
    conversation: dict[str, Any],
    message: Optional[dict[str, Any]],
    state: Optional[str],
    requested_model: Optional[str] = None,
    resolved_model: Optional[str] = None,
    error: Optional[str] = None,
    url: Optional[str] = None,
) -> dict[str, Any]:
    """One chat turn, as the MCP tool and ``omni chat --json`` report it.

    ``message`` is the stored assistant message (``None`` if nothing could be
    stored); ``conversation`` is the conversation after the turn (title,
    n_messages).
    """
    msg = message or {}
    meta = msg.get("meta") or {}
    out: dict[str, Any] = {
        "conversation_id": conversation.get("id"),
        "title": conversation.get("title"),
        "state": state or meta.get("state") or ("error" if not message else "done"),
        "text": _text_of(msg.get("content")),
        "model": msg.get("model") or resolved_model,
        "requested_model": meta.get("requested_model") or requested_model,
    }
    if msg.get("reasoning"):
        out["reasoning"] = msg["reasoning"]
    out["usage"] = msg.get("usage")
    out["cost_usd"] = msg.get("cost_usd")
    err = error or meta.get("error") or (None if message else "the reply could not be stored")
    if err:
        out["error"] = err
    out["n_messages"] = conversation.get("n_messages")
    if url:
        out["url"] = url
    return out


def usage_tokens(usage: Optional[dict[str, Any]]) -> tuple[Optional[int], Optional[int]]:
    """(input, output) tokens from an OpenAI- or Anthropic-shaped usage dict."""
    if not isinstance(usage, dict):
        return None, None
    inp = usage.get("prompt_tokens", usage.get("input_tokens"))
    out = usage.get("completion_tokens", usage.get("output_tokens"))
    return inp, out


def summary_line(summary: dict[str, Any]) -> str:
    """The one line printed after a reply."""
    parts = [str(summary.get("model") or summary.get("requested_model") or "?")]
    inp, out = usage_tokens(summary.get("usage"))
    if inp is not None or out is not None:
        parts.append(f"輸入 {inp if inp is not None else '?'}／輸出 {out if out is not None else '?'} tokens")
    cost = summary.get("cost_usd")
    parts.append(f"${cost:.4f}" if isinstance(cost, (int, float)) else "費用未計")
    parts.append(f"對話 {summary.get('conversation_id')}")
    if summary.get("url"):
        parts.append(str(summary["url"]))
    head = "──"
    if summary.get("state") == "cancelled":
        head = "── 已取消 ·"
    elif summary.get("state") == "error":
        head = "── 錯誤 ·"
    return f"{head} " + " · ".join(parts)


# ---------------------------------------------------------------- slash commands
@dataclass(frozen=True)
class Command:
    """One REPL line: ``send`` (a message) or a slash command."""

    name: str  # send | skip | model | system | export | id | exit | help | unknown
    arg: str = ""


_COMMANDS = {"model", "system", "export", "id", "exit", "quit", "help"}


def parse_line(line: Optional[str]) -> Command:
    """Parse a REPL line. ``//text`` sends ``/text`` literally."""
    if line is None:
        return Command("exit")
    raw = line.strip()
    if not raw:
        return Command("skip")
    if raw.startswith("//"):
        return Command("send", raw[1:])
    if not raw.startswith("/"):
        return Command("send", raw)
    head, _, rest = raw[1:].partition(" ")
    name = head.lower()
    if name not in _COMMANDS:
        return Command("unknown", head)
    if name == "quit":
        name = "exit"
    return Command(name, rest.strip())


def clean_input(line: Optional[str], encoding: Optional[str] = None) -> Optional[str]:
    """Undo a wrong stdin decode.

    A console gives ``input()`` proper Unicode, but piped stdin on Windows is
    decoded with the ANSI code page and bytes it cannot map come back as lone
    surrogates, which later crash the JSON encoder. Recover the raw bytes and
    try UTF-8 (what most pipes carry); failing that, replace the bad bytes.
    """
    if line is None or not any("\udc80" <= ch <= "\udcff" for ch in line):
        return line
    raw = line.encode(encoding or "utf-8", "surrogateescape")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(encoding or "utf-8", "replace")


HELP = (
    "指令：/model <名稱>  換下一則用的模型　/system <文字>  設 system prompt（/system 不帶字＝清掉）\n"
    "　　　/export [路徑]  存成 markdown　/id  對話 id 與 GUI 網址　/exit  離開（或 Ctrl+D）\n"
    "　　　回覆中按 Ctrl+C 取消這一則；閒置時 Ctrl+C 離開。以 / 開頭的訊息請打 //"
)


# ---------------------------------------------------------------- export
def export_filename(content_disposition: Optional[str], cid: str) -> str:
    """The filename the daemon suggests (RFC 5987 ``filename*`` first)."""
    cd = content_disposition or ""
    for part in cd.split(";"):
        part = part.strip()
        if part.lower().startswith("filename*="):
            value = part.split("=", 1)[1]
            if "''" in value:
                value = value.split("''", 1)[1]
            name = unquote(value).strip('"')
            if name:
                return Path(name).name
    for part in cd.split(";"):
        part = part.strip()
        if part.lower().startswith("filename="):
            name = part.split("=", 1)[1].strip('"')
            if name:
                return Path(name).name
    return f"chat-{cid}.md"


def export_target(arg: str, suggested: str, cwd: Optional[str] = None) -> Path:
    """Where ``/export [path]`` writes: cwd/suggested, or the given file / directory."""
    base = Path(cwd or os.getcwd())
    if not arg:
        return base / suggested
    p = Path(arg).expanduser()
    if not p.is_absolute():
        p = base / p
    if p.is_dir() or arg.endswith(("/", "\\")):
        return p / suggested
    return p


# ---------------------------------------------------------------- WS events → terminal
class TurnRenderer:
    """Feeds ``/ws`` events for one conversation turn to the terminal.

    ``feed()`` returns True once the turn's ``chat.finished`` arrives. Until
    the turn id is known (the POST may answer after the first deltas) every
    event of this conversation is accepted — the daemon runs one turn per
    conversation at a time.
    """

    def __init__(self, cid: str, out: Writer, err: Writer, *, show_thinking: bool = False, dim: Callable[[str], str] = lambda s: s):
        self.cid = cid
        self.out = out
        self.err = err
        self.show_thinking = show_thinking
        self.dim = dim
        self.turn_id: Optional[str] = None
        self.finished: Optional[dict[str, Any]] = None
        self.text: list[str] = []
        self._thinking_open = False

    def expect(self, turn_id: Optional[str]) -> None:
        if turn_id and not self.turn_id:
            self.turn_id = turn_id

    def _mine(self, event: dict[str, Any]) -> bool:
        if event.get("conversation_id") != self.cid:
            return False
        tid = event.get("turn_id")
        return self.turn_id is None or tid is None or tid == self.turn_id

    def _close_thinking(self) -> None:
        if self._thinking_open:
            self.err("\n")
            self._thinking_open = False

    def feed(self, event: dict[str, Any]) -> bool:
        if self.finished is not None:
            return True
        t = event.get("type")
        if t not in ("chat.started", "chat.delta", "chat.finished") or not self._mine(event):
            return False
        if t == "chat.started":
            self.expect(event.get("turn_id"))
            return False
        if t == "chat.delta":
            self.expect(event.get("turn_id"))
            delta = str(event.get("delta") or "")
            if not delta:
                return False
            if event.get("kind") == "reasoning":
                if self.show_thinking:
                    if not self._thinking_open:
                        self.err(self.dim("〔思考〕"))
                        self._thinking_open = True
                    self.err(self.dim(delta))
                return False
            self._close_thinking()
            self.text.append(delta)
            self.out(delta)
            return False
        # chat.finished
        self.expect(event.get("turn_id"))
        self._close_thinking()
        self.finished = event
        self.end_line()
        return True

    def end_line(self) -> None:
        if self.text and not "".join(self.text).endswith("\n"):
            self.out("\n")

    def print_whole(self, message: Optional[dict[str, Any]]) -> None:
        """Wait mode: print a stored reply in one go."""
        msg = message or {}
        if self.show_thinking and msg.get("reasoning"):
            self.err(self.dim("〔思考〕" + str(msg["reasoning"])) + "\n")
        body = _text_of(msg.get("content"))
        if body:
            self.text.append(body)
            self.out(body)
            self.end_line()


# ---------------------------------------------------------------- the daemon, over HTTP
class ApiError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class ChatApi:
    """Thin client for ``/api/chat``. ``client`` is an ``httpx.Client`` whose
    ``base_url`` is the daemon (a FastAPI ``TestClient`` works too)."""

    SOURCE = "cli"

    def __init__(self, client: Any):
        self.client = client

    @staticmethod
    def _check(r: Any) -> Any:
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except Exception:
                detail = None
            raise ApiError(r.status_code, str(detail or r.text or f"HTTP {r.status_code}"))
        return r

    def create(self, *, model: Optional[str], system: Optional[str], title: Optional[str] = None) -> dict[str, Any]:
        body = {"model": model, "system": system, "title": title, "source": self.SOURCE}
        return self._check(self.client.post("/api/chat", json={k: v for k, v in body.items() if v is not None})).json()

    def get(self, cid: str) -> dict[str, Any]:
        return self._check(self.client.get(f"/api/chat/{cid}")).json()

    def update(self, cid: str, **fields: Any) -> dict[str, Any]:
        return self._check(self.client.patch(f"/api/chat/{cid}", json=fields)).json()

    def send(self, cid: str, text: str, *, model: Optional[str] = None, wait: bool = False) -> dict[str, Any]:
        body = {"text": text, "model": model, "source": self.SOURCE}
        r = self.client.post(f"/api/chat/{cid}/messages", params={"wait": "true"} if wait else None,
                             json={k: v for k, v in body.items() if v is not None}, timeout=None if wait else 30)
        return self._check(r).json()

    def cancel(self, cid: str) -> dict[str, Any]:
        return self._check(self.client.post(f"/api/chat/{cid}/cancel", timeout=30)).json()

    def export(self, cid: str) -> tuple[str, str]:
        r = self._check(self.client.get(f"/api/chat/{cid}/export", params={"download": "true"}))
        return export_filename(r.headers.get("content-disposition"), cid), r.text


# ---------------------------------------------------------------- WebSocket
class WsConn:
    """What ``stream_turn`` needs from a socket: ``recv(timeout)`` → str
    (raises ``TimeoutError`` when nothing came), ``close()``."""

    def recv(self, timeout: float) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class WebsocketsConn(WsConn):
    def __init__(self, url: str, open_timeout: float = 3.0):
        from websockets.sync.client import connect

        self._ws = connect(url, open_timeout=open_timeout, max_size=None)

    def recv(self, timeout: float) -> str:
        data = self._ws.recv(timeout=timeout)
        return data.decode("utf-8") if isinstance(data, bytes) else data

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass


def open_ws(url: str) -> Optional[WsConn]:
    """Open the daemon's ``/ws``; ``None`` when it cannot be reached."""
    try:
        return WebsocketsConn(url)
    except Exception:
        return None


def _is_closed(exc: BaseException) -> bool:
    try:
        from websockets.exceptions import ConnectionClosed

        if isinstance(exc, ConnectionClosed):
            return True
    except Exception:  # pragma: no cover
        pass
    return isinstance(exc, (EOFError, ConnectionError, OSError)) and not isinstance(exc, TimeoutError)


@dataclass
class TurnOutcome:
    state: str
    message: Optional[dict[str, Any]]
    requested_model: Optional[str]
    resolved_model: Optional[str]
    error: Optional[str]
    streamed: bool


def _find_reply(conv: dict[str, Any], turn_id: Optional[str]) -> Optional[dict[str, Any]]:
    replies = [m for m in conv.get("messages") or [] if m.get("role") == "assistant"]
    if turn_id:
        for m in reversed(replies):
            if (m.get("meta") or {}).get("turn_id") == turn_id:
                return m
    return replies[-1] if replies else None


def stream_turn(api: ChatApi, cid: str, text: str, *, model: Optional[str], renderer: TurnRenderer, ws: WsConn,
                recv_timeout: float = 0.5, poll: float = 1.0, notice: Writer = lambda s: None) -> TurnOutcome:
    """Send one message and print its reply as it streams over ``ws``.

    Ctrl+C while the reply is coming cancels the turn (the daemon keeps what
    was said so far); a second Ctrl+C stops waiting. If the socket drops
    before ``chat.finished``, the stored reply is fetched over REST instead.
    """
    started: dict[str, Any] = {}
    cancelled = False
    try:
        started = api.send(cid, text, model=model)
        renderer.expect(started.get("turn_id"))
        while renderer.finished is None:
            try:
                raw = ws.recv(recv_timeout)
            except TimeoutError:
                continue
            except KeyboardInterrupt:
                raise
            except Exception as e:
                if _is_closed(e):
                    notice("（即時連線中斷，改向 daemon 取回完整回覆）\n")
                    break
                raise
            try:
                event = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(event, dict):
                renderer.feed(event)
    except KeyboardInterrupt:
        if not started:
            raise
        cancelled = True
        notice("\n（取消中…）\n")
        try:
            api.cancel(cid)
        except Exception:
            pass
        # the cancel returns after the reply is stored; take whatever the socket still has
        deadline = time.monotonic() + 3
        while renderer.finished is None and time.monotonic() < deadline:
            try:
                raw = ws.recv(0.3)
                event = json.loads(raw)
                if isinstance(event, dict):
                    renderer.feed(event)
            except KeyboardInterrupt:
                break
            except Exception:
                break
    finally:
        ws.close()

    fin = renderer.finished
    if fin is not None:
        return TurnOutcome(state=fin.get("state") or "error", message=fin.get("message"), requested_model=started.get("model"),
                           resolved_model=started.get("resolved_model"), error=fin.get("error"), streamed=True)
    # no chat.finished seen: read the stored result
    turn_id = renderer.turn_id or started.get("turn_id")
    conv: dict[str, Any] = {}
    while True:
        try:
            conv = api.get(cid)
            live = conv.get("live")
            if not live or (turn_id and live.get("turn_id") != turn_id):
                break
            time.sleep(poll)
        except KeyboardInterrupt:
            if cancelled:
                break
            cancelled = True
            try:
                api.cancel(cid)
            except Exception:
                pass
    reply = _find_reply(conv, turn_id)
    printed = "".join(renderer.text)
    if reply:
        body = _text_of(reply.get("content"))
        if body.startswith(printed) and len(body) > len(printed):
            renderer.out(body[len(printed):])
            renderer.text.append(body[len(printed):])
        renderer.end_line()
    meta = (reply or {}).get("meta") or {}
    return TurnOutcome(state=meta.get("state") or ("cancelled" if cancelled else "error"), message=reply,
                       requested_model=started.get("model"), resolved_model=started.get("resolved_model"),
                       error=meta.get("error"), streamed=True)


def wait_turn(api: ChatApi, cid: str, text: str, *, model: Optional[str], renderer: Optional[TurnRenderer],
              notice: Writer = lambda s: None, tick: float = 0.3) -> TurnOutcome:
    """Send and wait for the whole reply (``?wait=true``), then print it.

    The request runs on a worker thread so Ctrl+C stays responsive: it
    cancels the turn and the request then returns with the partial reply.
    """
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["result"] = api.send(cid, text, model=model, wait=True)
        except BaseException as e:  # noqa: BLE001 - re-raised on the main thread
            box["error"] = e

    th = threading.Thread(target=work, name="omni-chat-wait", daemon=True)
    th.start()
    cancelled = False
    while th.is_alive():
        try:
            th.join(tick)
        except KeyboardInterrupt:
            if cancelled:
                raise
            cancelled = True
            notice("\n（取消中…）\n")
            try:
                api.cancel(cid)
            except Exception:
                pass
    if "error" in box:
        raise box["error"]
    res = box.get("result") or {}
    msg = res.get("message")
    if renderer is not None:
        renderer.print_whole(msg)
    meta = (msg or {}).get("meta") or {}
    return TurnOutcome(state=res.get("state") or meta.get("state") or "error", message=msg, requested_model=res.get("model"),
                       resolved_model=res.get("resolved_model"), error=meta.get("error"), streamed=False)


def ws_url(host: str, port: int | str) -> str:
    return f"ws://{host}:{port}/ws"


# ---------------------------------------------------------------- one session (one-shot or REPL)
class ChatSession:
    """``omni chat``: one conversation, driven turn by turn."""

    def __init__(self, api: ChatApi, *, host: str, port: int | str, model: Optional[str], system: Optional[str],
                 cid: Optional[str] = None, stream: bool = True, show_thinking: bool = False,
                 out: Writer, err: Writer, dim: Callable[[str], str] = lambda s: s,
                 ws_factory: Callable[[str], Optional[WsConn]] = open_ws, quiet: bool = False):
        self.api = api
        self.host, self.port = host, port
        self.model = model
        self.system = system
        self.cid = cid
        self.stream = stream
        self.show_thinking = show_thinking
        self.out, self.err, self.dim = out, err, dim
        self.ws_factory = ws_factory
        self.quiet = quiet  # --json: no reply text, no summary line
        self._fell_back = False

    @property
    def url(self) -> Optional[str]:
        return gui_url(self.host, self.port, self.cid) if self.cid else None

    def notice(self, text: str) -> None:
        self.err(self.dim(text))

    def ensure(self) -> str:
        if not self.cid:
            # no model: the daemon's chat default (cheap unless changed in the settings)
            conv = self.api.create(model=self.model, system=self.system)
            self.cid = conv["id"]
        return self.cid

    def ask(self, text: str) -> dict[str, Any]:
        cid = self.ensure()
        renderer = None if self.quiet else TurnRenderer(cid, self.out, self.err, show_thinking=self.show_thinking, dim=self.dim)
        ws = self.ws_factory(ws_url(self.host, self.port)) if (self.stream and renderer is not None) else None
        if self.stream and renderer is not None and ws is None and not self._fell_back:
            self._fell_back = True
            self.notice("（WebSocket 連不上，改成等整段回完再顯示）\n")
        if ws is not None and renderer is not None:
            outcome = stream_turn(self.api, cid, text, model=self.model, renderer=renderer, ws=ws, notice=self.notice)
        else:
            outcome = wait_turn(self.api, cid, text, model=self.model, renderer=renderer, notice=self.notice)
        conv = self.api.get(cid)
        summary = reply_summary(conversation=conv, message=outcome.message, state=outcome.state, requested_model=outcome.requested_model,
                                resolved_model=outcome.resolved_model, error=outcome.error, url=self.url)
        if not self.quiet:
            if summary.get("error") and summary.get("state") == "error":
                self.err(f"錯誤：{summary['error']}\n")
            self.err(self.dim(summary_line(summary)) + "\n")
        return summary

    # ------------------------------------------------------------ REPL
    def handle(self, cmd: Command) -> bool:
        """Run one parsed line; False means leave the REPL."""
        if cmd.name == "exit":
            return False
        if cmd.name == "skip":
            return True
        if cmd.name == "help":
            self.err(HELP + "\n")
            return True
        if cmd.name == "unknown":
            self.err(f"不認得 /{cmd.arg}。{HELP}\n")
            return True
        try:
            if cmd.name == "send":
                self.ask(cmd.arg)
            elif cmd.name == "model":
                if not cmd.arg:
                    self.err(f"目前模型：{self.model or '（沿用對話上次的模型）'}\n")
                elif self.cid:
                    self.api.update(self.cid, model=cmd.arg)
                    self.model = cmd.arg
                    self.err(f"下一則起改用 {cmd.arg}\n")
                else:
                    self.model = cmd.arg
                    self.err(f"改用 {cmd.arg}\n")
            elif cmd.name == "system":
                if self.cid:
                    self.api.update(self.cid, system_prompt=cmd.arg)
                self.system = cmd.arg or None
                self.err("system prompt 已設定\n" if cmd.arg else "system prompt 已清除\n")
            elif cmd.name == "id":
                if self.cid:
                    self.err(f"{self.cid}  {self.url}\n")
                else:
                    self.err("還沒開始對話（送出第一則後才有 id）\n")
            elif cmd.name == "export":
                if not self.cid:
                    self.err("還沒有對話可以匯出\n")
                else:
                    name, md = self.api.export(self.cid)
                    target = export_target(cmd.arg, name)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(md, encoding="utf-8")
                    self.err(f"已存到 {target}\n")
        except ApiError as e:
            self.err(f"錯誤（HTTP {e.status}）：{e.detail}\n")
        except KeyboardInterrupt:
            raise
        except Exception as e:  # daemon gone, disk full … — report, keep the session
            self.err(f"錯誤：{type(e).__name__}: {e}\n")
        return True

    def repl(self, read_line: Callable[[str], str], prompt: str = "› ", encoding: Optional[str] = None) -> None:
        while True:
            try:
                line: Optional[str] = clean_input(read_line(prompt), encoding)
            except (EOFError, KeyboardInterrupt):
                self.err("\n")
                return
            if not self.handle(parse_line(line)):
                return
