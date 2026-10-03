"""SQLite store at ``~/.omniapi/omniapi.db`` (aiosqlite, no ORM).

Tables
------
calls          one row per MCP tool call (tool, model, cost, duration, status)
conversations  chat threads and agent runs share this header table
messages       chat turns (M6 fills; the daemon REST can already write them)
runs           agent runs (M3 fills)
events         per-run event stream (M3 fills)
artifacts      one row per generated file — the works library (v1.1)
uploads        reference images / audio handed in for a generation (v1.1)

All JSON columns hold ``json.dumps`` text. Timestamps are unix seconds
(REAL). Ids are strings chosen by the caller (uuid hex / run ids) except
``events.id`` which autoincrements.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from ..catalog.paths import data_home

logger = logging.getLogger(__name__)

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS calls (
  id TEXT PRIMARY KEY,
  ts REAL NOT NULL,
  tool TEXT NOT NULL,
  status TEXT NOT NULL,            -- started | ok | error | ticket
  duration_ms INTEGER,
  model TEXT,
  provider TEXT,
  cost_usd REAL,
  usage_json TEXT,
  args_json TEXT,
  result_json TEXT,                -- summary, truncated
  error TEXT,
  session_id TEXT,
  source TEXT,                     -- mcp | rest | cli
  conversation_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_calls_ts ON calls(ts DESC);
CREATE INDEX IF NOT EXISTS idx_calls_tool ON calls(tool);

CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,              -- chat | run
  title TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  model TEXT,
  harness TEXT,
  cwd TEXT,
  status TEXT,                     -- open | running | done | error | archived
  system_prompt TEXT,
  meta_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_conv_updated ON conversations(updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  role TEXT NOT NULL,              -- system | user | assistant | tool
  content_json TEXT NOT NULL,
  created_at REAL NOT NULL,
  model TEXT,
  usage_json TEXT,
  cost_usd REAL,
  reasoning TEXT,
  tool_calls_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id, seq);

CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
  title TEXT,
  prompt TEXT,
  harness TEXT NOT NULL,
  model TEXT,
  cwd TEXT,
  state TEXT NOT NULL,             -- starting | running | done | error | cancelled | dead
  started_at REAL NOT NULL,
  ended_at REAL,
  turns INTEGER DEFAULT 0,
  context_tokens INTEGER,
  cost_usd REAL,
  pid INTEGER,
  session_id TEXT,                 -- harness-side session id (for resume)
  dispatcher TEXT,
  result TEXT,
  error TEXT,
  meta_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_state ON runs(state);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  ts REAL NOT NULL,
  type TEXT NOT NULL,
  payload_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);

CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY,
  created_at REAL NOT NULL,
  kind TEXT NOT NULL,              -- image | speech | music | transcript | lyrics
  tool TEXT,
  model TEXT,
  provider TEXT,
  title TEXT,
  prompt TEXT,
  params_json TEXT,
  file_path TEXT NOT NULL UNIQUE,  -- absolute; one row per file
  mime TEXT,
  bytes INTEGER,
  width INTEGER,
  height INTEGER,
  duration_s REAL,
  text TEXT,                       -- transcript / lyrics body
  cost_usd REAL,
  source TEXT,                     -- mcp | gui | cli | backfill
  call_id TEXT,
  parent_id TEXT,                  -- the artifact an edit started from
  hidden INTEGER NOT NULL DEFAULT 0,
  meta_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_artifacts_created ON artifacts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_artifacts_kind ON artifacts(kind, created_at DESC);

CREATE TABLE IF NOT EXISTS uploads (
  id TEXT PRIMARY KEY,
  created_at REAL NOT NULL,
  kind TEXT NOT NULL,              -- image | audio | file (1.2-M5)
  filename TEXT,
  file_path TEXT NOT NULL,
  mime TEXT,
  bytes INTEGER,
  meta_json TEXT                   -- 1.2-M5: {info: what extraction found, transcript_id}
);

CREATE TABLE IF NOT EXISTS generations (
  id TEXT PRIMARY KEY,
  created_at REAL NOT NULL,
  finished_at REAL,
  kind TEXT NOT NULL,              -- image | speech | music | transcript
  tool TEXT NOT NULL,              -- the tool handler that runs it
  model TEXT,
  title TEXT,                      -- what the waiting card shows
  params_json TEXT,                -- the full request, so a failed one can be sent again
  sources_json TEXT,               -- {image|audio: {artifact_id|upload_id}} — ids, never paths
  status TEXT NOT NULL,            -- running | done | error | cancelled | interrupted
  error TEXT,
  error_kind TEXT,                 -- quota | auth | rejected | timeout | too_large | unavailable | offline | interrupted | other
  estimate_json TEXT,              -- the estimate shown before sending
  cost_usd REAL,
  call_id TEXT,
  artifact_ids_json TEXT,
  source TEXT
);
CREATE INDEX IF NOT EXISTS idx_generations_created ON generations(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_generations_status ON generations(status);
"""


#: ``add_message(parent_id=LEAF)``: follow the conversation's current leaf
LEAF: Any = object()


def _dumps(obj: Any) -> Optional[str]:
    if obj is None:
        return None
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        return json.dumps(str(obj))


def _loads(s: Optional[str]) -> Any:
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return s


def _row(r: aiosqlite.Row | None) -> Optional[dict[str, Any]]:
    if r is None:
        return None
    d = dict(r)
    for k in list(d):
        if k.endswith("_json"):
            d[k[:-5]] = _loads(d.pop(k))
    return d


class Store:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (data_home() / "omniapi.db")
        self._db: Optional[aiosqlite.Connection] = None

    async def open(self) -> None:
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._migrate()
        await self._db.commit()
        logger.info("Store opened at %s", self.path)

    async def _migrate(self) -> None:
        """Additive column migrations (``CREATE TABLE IF NOT EXISTS`` never
        alters a table that already exists)."""
        assert self._db is not None
        wanted = {
            # M6: how a reply ended (done / cancelled / error), provider, duration
            # 1.2-M1: images a message carries, by id (決策記錄 1.2-M1-a) and the
            # message it answers / follows (決策記錄 1.2-M1-b)
            "messages": {"meta_json": "TEXT", "attachments_json": "TEXT", "parent_id": "TEXT"},
            # 1.2-M4: where a generation was asked for beyond its door ({conversation_id, message_id, tool_call_id})
            "generations": {"meta_json": "TEXT"},
            # 1.2-M5: what a file holds (pages, rows, readable…) and the transcript that makes an audio file readable
            "uploads": {"meta_json": "TEXT"},
        }
        for table, cols in wanted.items():
            cur = await self._db.execute(f"PRAGMA table_info({table})")
            have = {r[1] for r in await cur.fetchall()}
            for name, decl in cols.items():
                if name not in have:
                    await self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                    logger.info("Store migration: %s.%s added", table, name)
        await self._db.execute("CREATE INDEX IF NOT EXISTS idx_msg_parent ON messages(conversation_id, parent_id)")
        cur = await self._db.execute("PRAGMA user_version")
        version = (await cur.fetchone())[0]
        if version < 1:
            await self._link_linear_history()
            await self._db.execute("PRAGMA user_version=1")

    async def _link_linear_history(self) -> None:
        """1.2-M1-b: messages written before branching existed form one line in
        ``seq`` order — chain each to the one before it and make the last one
        the conversation's current leaf. Runs once (``user_version``); it also
        only touches conversations where no message has a parent yet, so a
        forced re-run leaves branched conversations alone."""
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT conversation_id FROM messages GROUP BY conversation_id HAVING COUNT(parent_id)=0"
        )
        cids = [r[0] for r in await cur.fetchall()]
        for cid in cids:
            cur = await self._db.execute("SELECT id FROM messages WHERE conversation_id=? ORDER BY seq ASC", (cid,))
            ids = [r[0] for r in await cur.fetchall()]
            await self._db.executemany("UPDATE messages SET parent_id=? WHERE id=?", list(zip(ids[:-1], ids[1:])))
            await self._db.execute(
                "UPDATE conversations SET meta_json=json_set(COALESCE(meta_json,'{}'),'$.leaf_id',?) WHERE id=?"
                " AND json_extract(COALESCE(meta_json,'{}'),'$.leaf_id') IS NULL",
                (ids[-1], cid),
            )
        if cids:
            logger.info("Store migration: %d conversation(s) linked into a single branch", len(cids))

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Store is not open")
        return self._db

    # ------------------------------------------------------------ calls
    async def call_started(
        self,
        tool: str,
        args: dict[str, Any] | None,
        *,
        source: str = "mcp",
        session_id: str | None = None,
        conversation_id: str | None = None,
    ) -> str:
        call_id = uuid.uuid4().hex[:16]
        await self.db.execute(
            "INSERT INTO calls (id, ts, tool, status, args_json, source, session_id, conversation_id)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (call_id, time.time(), tool, "started", _dumps(args), source, session_id, conversation_id),
        )
        await self.db.commit()
        return call_id

    async def call_finished(
        self,
        call_id: str,
        *,
        status: str,
        duration_ms: int,
        model: str | None = None,
        provider: str | None = None,
        cost_usd: float | None = None,
        usage: dict[str, Any] | None = None,
        result: Any = None,
        error: str | None = None,
    ) -> None:
        await self.db.execute(
            "UPDATE calls SET status=?, duration_ms=?, model=?, provider=?, cost_usd=?,"
            " usage_json=?, result_json=?, error=? WHERE id=?",
            (status, duration_ms, model, provider, cost_usd, _dumps(usage), _dumps(result), error, call_id),
        )
        await self.db.commit()

    async def calls(
        self, *, limit: int = 50, tool: str | None = None, since: float | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM calls"
        where, params = [], []
        if tool:
            tools = [t for t in tool.split(",") if t]
            where.append(f"tool IN ({', '.join('?' for _ in tools)})"); params += tools
        if since:
            where.append("ts>=?"); params.append(since)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        cur = await self.db.execute(sql, params)
        return [_row(r) for r in await cur.fetchall()]

    async def call(self, call_id: str) -> Optional[dict[str, Any]]:
        cur = await self.db.execute("SELECT * FROM calls WHERE id=?", (call_id,))
        return _row(await cur.fetchone())

    async def cost_summary(self, *, days: int = 30) -> dict[str, Any]:
        since = time.time() - days * 86400
        cur = await self.db.execute(
            "SELECT COALESCE(model,'?') AS model, COUNT(*) AS n, COALESCE(SUM(cost_usd),0) AS cost"
            " FROM calls WHERE ts>=? AND status IN ('ok','ticket') GROUP BY model ORDER BY cost DESC",
            (since,),
        )
        by_model = [dict(r) for r in await cur.fetchall()]
        cur = await self.db.execute(
            "SELECT date(ts,'unixepoch','localtime') AS day, COUNT(*) AS n, COALESCE(SUM(cost_usd),0) AS cost"
            " FROM calls WHERE ts>=? GROUP BY day ORDER BY day DESC",
            (since,),
        )
        by_day = [dict(r) for r in await cur.fetchall()]
        cur = await self.db.execute(
            "SELECT COALESCE(SUM(cost_usd),0) AS cost, COUNT(*) AS n FROM calls WHERE ts>=?", (since,)
        )
        total = dict(await cur.fetchone())
        return {"days": days, "total": total, "by_model": by_model, "by_day": by_day, "runs": await self._runs_ledger(since)}

    async def _runs_ledger(self, since: float) -> dict[str, Any]:
        """派工帳：runs 表的 harness 自報 cost_usd。與工具呼叫帳（calls 表）口徑不同、不可相加。

        cost_usd 為 NULL（例如 Codex 不回報）不當 0：另計 unreported；某 harness 全部未回報時 cost 為 None。
        """
        cur = await self.db.execute(
            "SELECT COALESCE(SUM(cost_usd),0) AS cost, COUNT(*) AS n,"
            " SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END) AS unreported"
            " FROM runs WHERE started_at>=?",
            (since,),
        )
        total = dict(await cur.fetchone())
        total["unreported"] = total["unreported"] or 0
        cur = await self.db.execute(
            "SELECT harness, COUNT(*) AS n, SUM(cost_usd) AS cost,"
            " SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END) AS unreported"
            " FROM runs WHERE started_at>=? GROUP BY harness"
            " ORDER BY (SUM(cost_usd) IS NULL), SUM(cost_usd) DESC, n DESC",
            (since,),
        )
        by_harness = [dict(r) for r in await cur.fetchall()]
        cur = await self.db.execute(
            "SELECT date(started_at,'unixepoch','localtime') AS day, COUNT(*) AS n, COALESCE(SUM(cost_usd),0) AS cost"
            " FROM runs WHERE started_at>=? GROUP BY day ORDER BY day DESC",
            (since,),
        )
        by_day = [dict(r) for r in await cur.fetchall()]
        return {"total": total, "by_harness": by_harness, "by_day": by_day}

    # ------------------------------------------------------------ conversations
    async def create_conversation(
        self,
        *,
        kind: str,
        title: str | None = None,
        model: str | None = None,
        harness: str | None = None,
        cwd: str | None = None,
        system_prompt: str | None = None,
        meta: dict[str, Any] | None = None,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        cid = conversation_id or uuid.uuid4().hex[:16]
        now = time.time()
        await self.db.execute(
            "INSERT INTO conversations (id, kind, title, created_at, updated_at, model, harness, cwd, status, system_prompt, meta_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (cid, kind, title, now, now, model, harness, cwd, "open", system_prompt, _dumps(meta)),
        )
        await self.db.commit()
        return (await self.conversation(cid)) or {"id": cid}

    async def update_conversation(self, cid: str, **fields: Any) -> None:
        if not fields:
            return
        cols, params = [], []
        for k, v in fields.items():
            if k == "meta":
                k, v = "meta_json", _dumps(v)
            cols.append(f"{k}=?"); params.append(v)
        cols.append("updated_at=?"); params.append(time.time())
        params.append(cid)
        await self.db.execute(f"UPDATE conversations SET {', '.join(cols)} WHERE id=?", params)
        await self.db.commit()

    async def conversation(self, cid: str) -> Optional[dict[str, Any]]:
        cur = await self.db.execute("SELECT * FROM conversations WHERE id=?", (cid,))
        return _row(await cur.fetchone())

    async def conversations(self, *, limit: int = 50, kind: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM conversations"
        params: list[Any] = []
        if kind:
            sql += " WHERE kind=?"; params.append(kind)
        sql += " ORDER BY updated_at DESC LIMIT ?"; params.append(limit)
        cur = await self.db.execute(sql, params)
        return [_row(r) for r in await cur.fetchall()]

    async def add_message(
        self,
        cid: str,
        *,
        role: str,
        content: Any,
        model: str | None = None,
        usage: dict[str, Any] | None = None,
        cost_usd: float | None = None,
        reasoning: str | None = None,
        tool_calls: Any = None,
        meta: dict[str, Any] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        parent_id: Any = LEAF,
    ) -> dict[str, Any]:
        """Append a message. ``parent_id`` is the message it follows: by default
        the conversation's current leaf (a plain append), ``None`` for a new
        first message (an edit of the opening one). The new message becomes
        the current leaf."""
        if parent_id is LEAF:
            parent_id = await self.leaf_id(cid)
        cur = await self.db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM messages WHERE conversation_id=?", (cid,))
        seq = (await cur.fetchone())[0]
        mid = uuid.uuid4().hex[:16]
        now = time.time()
        await self.db.execute(
            "INSERT INTO messages (id, conversation_id, seq, role, content_json, created_at, model, usage_json, cost_usd, reasoning,"
            " tool_calls_json, meta_json, attachments_json, parent_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mid, cid, seq, role, _dumps(content), now, model, _dumps(usage), cost_usd, reasoning, _dumps(tool_calls), _dumps(meta),
             _dumps(attachments or None), parent_id),
        )
        await self.db.execute(
            "UPDATE conversations SET updated_at=?, meta_json=json_set(COALESCE(meta_json,'{}'),'$.leaf_id',?) WHERE id=?", (now, mid, cid)
        )
        await self.db.commit()
        return {"id": mid, "conversation_id": cid, "seq": seq, "role": role, "content": content, "created_at": now,
                "model": model, "usage": usage, "cost_usd": cost_usd, "reasoning": reasoning, "tool_calls": tool_calls, "meta": meta,
                "attachments": attachments or None, "parent_id": parent_id}

    async def message(self, mid: str) -> Optional[dict[str, Any]]:
        cur = await self.db.execute("SELECT * FROM messages WHERE id=?", (mid,))
        return _row(await cur.fetchone())

    async def update_message(self, mid: str, *, content: Any = LEAF, meta: Any = LEAF, **fields: Any) -> None:
        """Rewrite a stored message's content and/or meta (``LEAF`` = leave it):
        a tool call's state lives on its message and its result message is
        brought up to date in place (1.2-M4). Not a change of activity.
        ``fields`` (``model``, ``usage``, ``cost_usd``, ``reasoning``,
        ``tool_calls``) fill in a reply that waited for the owner (1.2-M5)."""
        sets, vals = [], []
        if content is not LEAF:
            sets.append("content_json=?"); vals.append(_dumps(content))
        if meta is not LEAF:
            sets.append("meta_json=?"); vals.append(_dumps(meta))
        for k, v in fields.items():
            if k not in ("model", "usage", "cost_usd", "reasoning", "tool_calls"):
                raise ValueError(f"cannot update message field {k}")
            json_col = k in ("usage", "tool_calls")
            sets.append(f"{k}_json=?" if json_col else f"{k}=?"); vals.append(_dumps(v) if json_col else v)
        if not sets:
            return
        await self.db.execute(f"UPDATE messages SET {', '.join(sets)} WHERE id=?", [*vals, mid])
        await self.db.commit()

    async def messages_with_tool_state(self, state: str) -> list[dict[str, Any]]:
        """Assistant messages holding a tool call in ``state`` (any conversation).
        A coarse text match narrows it down; callers check the parsed meta."""
        cur = await self.db.execute(
            "SELECT * FROM messages WHERE role='assistant' AND (tool_calls_json IS NOT NULL OR meta_json LIKE '%\"gate\"%')"
            " AND meta_json LIKE ?", (f'%"{state}"%',)
        )
        return [_row(r) for r in await cur.fetchall()]

    async def leaf_id(self, cid: str) -> Optional[str]:
        """The tip of the branch the conversation is on (``meta.leaf_id``);
        falls back to the newest message when the pointer is missing or stale."""
        cur = await self.db.execute(
            "SELECT m.id FROM conversations c JOIN messages m ON m.id=json_extract(c.meta_json,'$.leaf_id')"
            " AND m.conversation_id=c.id WHERE c.id=?",
            (cid,),
        )
        row = await cur.fetchone()
        if row:
            return row[0]
        cur = await self.db.execute("SELECT id FROM messages WHERE conversation_id=? ORDER BY seq DESC LIMIT 1", (cid,))
        row = await cur.fetchone()
        return row[0] if row else None

    async def set_leaf(self, cid: str, mid: Optional[str]) -> None:
        """Point the conversation at another branch tip (not a change of activity:
        ``updated_at`` stays, so switching versions does not reorder the list)."""
        await self.db.execute(
            "UPDATE conversations SET meta_json=json_set(COALESCE(meta_json,'{}'),'$.leaf_id',?) WHERE id=?", (mid, cid)
        )
        await self.db.commit()

    async def delete_conversation(self, cid: str) -> dict[str, int]:
        """Delete a conversation and its messages. Done by hand: ``foreign_keys``
        is off for this database, so ``ON DELETE CASCADE`` never fires
        (決策記錄 1.2-M1-d). The call ledger, works and uploads are left alone."""
        cur = await self.db.execute("DELETE FROM messages WHERE conversation_id=?", (cid,))
        n_messages = cur.rowcount
        cur = await self.db.execute("DELETE FROM conversations WHERE id=?", (cid,))
        n_conv = cur.rowcount
        await self.db.commit()
        return {"conversations": n_conv, "messages": n_messages}

    async def chats(self, *, limit: int = 100, include_archived: bool = False) -> list[dict[str, Any]]:
        """Chat conversations with their message count (yours and the replies;
        a tool call's result message is not one) and total cost, most recently
        active first."""
        sql = (
            "SELECT c.*, COALESCE(SUM(CASE WHEN m.role IN ('user','assistant') THEN 1 ELSE 0 END),0) AS n_messages,"
            " COALESCE(SUM(m.cost_usd),0) AS cost_usd,"
            " COALESCE(SUM(CASE WHEN m.role='assistant' AND m.cost_usd IS NOT NULL THEN 1 ELSE 0 END),0) AS priced,"
            " COALESCE(SUM(CASE WHEN m.role='assistant' AND m.cost_usd IS NULL AND m.content_json NOT IN ('\"\"','null') THEN 1 ELSE 0 END),0) AS unpriced"
            " FROM conversations c LEFT JOIN messages m ON m.conversation_id=c.id"
            " WHERE c.kind='chat'"
        )
        if not include_archived:
            sql += " AND COALESCE(c.status,'open')<>'archived'"
        sql += " GROUP BY c.id ORDER BY c.updated_at DESC LIMIT ?"
        cur = await self.db.execute(sql, (limit,))
        return [_row(r) for r in await cur.fetchall()]

    async def messages(self, cid: str, *, limit: Optional[int] = 500) -> list[dict[str, Any]]:
        """Every message of a conversation, all branches, in ``seq`` order
        (``limit=None``: no cap — the branch walk needs the whole tree)."""
        cur = await self.db.execute(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY seq ASC LIMIT ?", (cid, -1 if limit is None else limit)
        )
        return [_row(r) for r in await cur.fetchall()]

    # ------------------------------------------------------------ runs / events (M3)
    async def create_run(self, run_id: str, **fields: Any) -> None:
        fields.setdefault("started_at", time.time())
        fields.setdefault("state", "starting")
        if "meta" in fields:
            fields["meta_json"] = _dumps(fields.pop("meta"))
        cols = ["id"] + list(fields)
        await self.db.execute(
            f"INSERT INTO runs ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
            [run_id] + list(fields.values()),
        )
        await self.db.commit()

    async def update_run(self, run_id: str, **fields: Any) -> None:
        if not fields:
            return
        if "meta" in fields:
            fields["meta_json"] = _dumps(fields.pop("meta"))
        cols = [f"{k}=?" for k in fields]
        await self.db.execute(f"UPDATE runs SET {', '.join(cols)} WHERE id=?", [*fields.values(), run_id])
        await self.db.commit()

    async def run(self, run_id: str) -> Optional[dict[str, Any]]:
        cur = await self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,))
        return _row(await cur.fetchone())

    async def runs(self, *, limit: int = 50, state: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM runs"
        params: list[Any] = []
        if state:
            sql += " WHERE state=?"; params.append(state)
        sql += " ORDER BY started_at DESC LIMIT ?"; params.append(limit)
        cur = await self.db.execute(sql, params)
        return [_row(r) for r in await cur.fetchall()]

    async def run_children(self, run_id: str) -> list[dict[str, Any]]:
        """Runs that resumed ``run_id`` (meta.resume_run_id), oldest first."""
        cur = await self.db.execute(
            "SELECT * FROM runs WHERE json_extract(meta_json,'$.resume_run_id')=? ORDER BY started_at ASC", (run_id,)
        )
        return [_row(r) for r in await cur.fetchall()]

    async def cwds(self, *, limit: int = 30) -> list[dict[str, Any]]:
        """Working directories used by past runs, most recently used first."""
        cur = await self.db.execute(
            "SELECT cwd, COUNT(*) AS n, MAX(started_at) AS last_used FROM runs"
            " WHERE cwd IS NOT NULL AND cwd<>'' GROUP BY cwd ORDER BY last_used DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def add_event(self, run_id: str, type_: str, payload: Any = None, ts: float | None = None) -> int:
        cur = await self.db.execute(
            "INSERT INTO events (run_id, ts, type, payload_json) VALUES (?,?,?,?)",
            (run_id, ts or time.time(), type_, _dumps(payload)),
        )
        await self.db.commit()
        return cur.lastrowid

    async def events(self, run_id: str, *, after_id: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id ASC LIMIT ?", (run_id, after_id, limit)
        )
        return [_row(r) for r in await cur.fetchall()]

    # ------------------------------------------------------------ artifacts / uploads (v1.1)
    _ARTIFACT_COLS = (
        "kind", "tool", "model", "provider", "title", "prompt", "file_path", "mime", "bytes",
        "width", "height", "duration_s", "text", "cost_usd", "source", "call_id", "parent_id",
    )

    async def add_artifact(self, *, artifact_id: str | None = None, created_at: float | None = None,
                           params: Any = None, meta: Any = None, **fields: Any) -> Optional[dict[str, Any]]:
        """Index one generated file. ``file_path`` is unique: indexing the same
        file twice is a no-op that returns ``None`` (backfill relies on this)."""
        unknown = set(fields) - set(self._ARTIFACT_COLS)
        if unknown:
            raise ValueError(f"unknown artifact fields: {sorted(unknown)}")
        aid = artifact_id or uuid.uuid4().hex[:16]
        cols = ["id", "created_at", "params_json", "meta_json", *fields]
        vals = [aid, created_at or time.time(), _dumps(params), _dumps(meta), *fields.values()]
        cur = await self.db.execute(
            f"INSERT OR IGNORE INTO artifacts ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", vals
        )
        await self.db.commit()
        if cur.rowcount == 0:
            return None
        return await self.artifact(aid)

    async def artifact(self, artifact_id: str) -> Optional[dict[str, Any]]:
        cur = await self.db.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,))
        return _row(await cur.fetchone())

    async def artifact_by_path(self, file_path: str) -> Optional[dict[str, Any]]:
        cur = await self.db.execute("SELECT * FROM artifacts WHERE file_path=?", (file_path,))
        return _row(await cur.fetchone())

    async def artifacts(
        self,
        *,
        limit: int = 60,
        kind: str | None = None,
        model: str | None = None,
        source: str | None = None,
        q: str | None = None,
        before: float | None = None,
        include_hidden: bool = False,
        only_hidden: bool = False,
        since: float | None = None,
        until: float | None = None,
        call_id: str | None = None,
        artifact_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Newest first. ``before`` (a ``created_at``) pages backwards; ``q``
        searches prompt, title, transcript text and the file name.
        ``kind`` may be several, comma-separated (``music,lyrics``)."""
        where, params = self._artifact_where(kind=kind, model=model, source=source, q=q, include_hidden=include_hidden,
                                             only_hidden=only_hidden, since=since, until=until, call_id=call_id, artifact_id=artifact_id)
        if before:
            where.append("created_at<?"); params.append(before)
        sql = "SELECT * FROM artifacts" + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        cur = await self.db.execute(sql, params)
        return [_row(r) for r in await cur.fetchall()]

    @staticmethod
    def _artifact_where(*, kind: str | None = None, model: str | None = None, source: str | None = None, q: str | None = None,
                        include_hidden: bool = False, only_hidden: bool = False, since: float | None = None,
                        until: float | None = None, call_id: str | None = None,
                        artifact_id: str | None = None) -> tuple[list[str], list[Any]]:
        where: list[str] = []
        params: list[Any] = []
        if only_hidden:
            where.append("hidden=1")
        elif not include_hidden:
            where.append("hidden=0")
        if kind:
            kinds = [k for k in kind.split(",") if k]
            where.append(f"kind IN ({', '.join('?' for _ in kinds)})"); params += kinds
        if model == "-":  # works that carry no model (backfilled audio)
            where.append("model IS NULL")
        elif model:
            where.append("model=?"); params.append(model)
        for col, val in (("source", source), ("call_id", call_id), ("id", artifact_id)):
            if val:
                where.append(f"{col}=?"); params.append(val)
        if since:
            where.append("created_at>=?"); params.append(since)
        if until:
            where.append("created_at<?"); params.append(until)
        if q:
            like = f"%{q}%"
            where.append("(prompt LIKE ? OR title LIKE ? OR text LIKE ? OR file_path LIKE ? OR model LIKE ?)"); params += [like] * 5
        return where, params

    async def artifact_counts(self, **filters: Any) -> dict[str, int]:
        """Visible works per kind; with filters (the wall's current ones,
        except ``kind``) the counts follow them."""
        where, params = self._artifact_where(**filters)
        cur = await self.db.execute(
            "SELECT kind, COUNT(*) AS n FROM artifacts" + (" WHERE " + " AND ".join(where) if where else "") + " GROUP BY kind", params
        )
        return {r["kind"]: r["n"] for r in await cur.fetchall()}

    async def artifact_facets(self) -> dict[str, Any]:
        """What the wall's filters can offer: models and doors that actually
        occur among the visible works, the hidden count, the date span."""
        out: dict[str, Any] = {}
        for col in ("model", "source"):
            cur = await self.db.execute(f"SELECT {col} AS v, COUNT(*) AS n FROM artifacts WHERE hidden=0 GROUP BY {col} ORDER BY n DESC")
            out[col + "s"] = [{"value": r["v"], "n": r["n"]} for r in await cur.fetchall()]
        cur = await self.db.execute("SELECT COUNT(*) AS n FROM artifacts WHERE hidden=1")
        out["hidden"] = (await cur.fetchone())["n"]
        cur = await self.db.execute("SELECT MIN(created_at) AS a, MAX(created_at) AS b FROM artifacts WHERE hidden=0")
        r = await cur.fetchone()
        out["oldest"], out["newest"] = r["a"], r["b"]
        return out

    async def artifact_children(self, artifact_id: str) -> list[dict[str, Any]]:
        """Works made from this one (edits of an image, transcripts of an audio)."""
        cur = await self.db.execute("SELECT * FROM artifacts WHERE parent_id=? ORDER BY created_at ASC", (artifact_id,))
        return [_row(r) for r in await cur.fetchall()]

    async def artifact_neighbours(self, created_at: float, **filters: Any) -> dict[str, Optional[str]]:
        """The ids just newer and just older than a work, under the wall's filters."""
        where, params = self._artifact_where(**filters)
        base = "SELECT id FROM artifacts WHERE " + " AND ".join([*where, "created_at{op}?"]) + " ORDER BY created_at {dir} LIMIT 1"
        cur = await self.db.execute(base.format(op=">", dir="ASC"), [*params, created_at])
        newer = await cur.fetchone()
        cur = await self.db.execute(base.format(op="<", dir="DESC"), [*params, created_at])
        older = await cur.fetchone()
        return {"newer": newer["id"] if newer else None, "older": older["id"] if older else None}

    async def artifact_ids_by_call(self, call_ids: list[str]) -> dict[str, list[str]]:
        if not call_ids:
            return {}
        cur = await self.db.execute(
            f"SELECT id, call_id FROM artifacts WHERE call_id IN ({', '.join('?' for _ in call_ids)}) ORDER BY created_at ASC", call_ids
        )
        out: dict[str, list[str]] = {}
        for r in await cur.fetchall():
            out.setdefault(r["call_id"], []).append(r["id"])
        return out

    async def settle_ticket_call(self, call_id: str, *, model: str | None, provider: str | None, cost_usd: float | None) -> None:
        """A call that returned a ticket has now really finished: put what it
        cost on its own ledger row (it used to be recorded only if someone
        came back for the result — on *that* call's row)."""
        await self.db.execute(
            "UPDATE calls SET status='ok', model=COALESCE(?, model), provider=COALESCE(?, provider), cost_usd=? WHERE id=? AND status IN ('ticket','started')",
            (model, provider, cost_usd, call_id),
        )
        await self.db.commit()

    async def set_artifact_hidden(self, artifact_id: str, hidden: bool) -> None:
        """Hiding takes a work off the wall; the file is never touched."""
        await self.db.execute("UPDATE artifacts SET hidden=? WHERE id=?", (1 if hidden else 0, artifact_id))
        await self.db.commit()

    async def add_upload(self, *, upload_id: str, kind: str, filename: str, file_path: str, mime: str, size: int,
                         meta: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        now = time.time()
        await self.db.execute(
            "INSERT INTO uploads (id, created_at, kind, filename, file_path, mime, bytes, meta_json) VALUES (?,?,?,?,?,?,?,?)",
            (upload_id, now, kind, filename, file_path, mime, size, _dumps(meta)),
        )
        await self.db.commit()
        return {"id": upload_id, "created_at": now, "kind": kind, "filename": filename, "file_path": file_path, "mime": mime,
                "bytes": size, "meta": meta}

    async def update_upload_meta(self, upload_id: str, **fields: Any) -> Optional[dict[str, Any]]:
        """Merge ``fields`` into an upload's meta (1.2-M5: ``info``, ``transcript_id``)."""
        row = await self.upload(upload_id)
        if not row:
            return None
        meta = {**(row.get("meta") or {}), **fields}
        await self.db.execute("UPDATE uploads SET meta_json=? WHERE id=?", (_dumps(meta), upload_id))
        await self.db.commit()
        row["meta"] = meta
        return row

    async def upload(self, upload_id: str) -> Optional[dict[str, Any]]:
        cur = await self.db.execute("SELECT * FROM uploads WHERE id=?", (upload_id,))
        return _row(await cur.fetchone())

    async def artifacts_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        if not ids:
            return []
        cur = await self.db.execute(f"SELECT * FROM artifacts WHERE id IN ({', '.join('?' for _ in ids)})", ids)
        rows = {r["id"]: _row(r) for r in await cur.fetchall()}
        return [rows[i] for i in ids if i in rows]

    async def artifact_costs(self, *, tool: str, model: str, limit: int = 200) -> list[dict[str, Any]]:
        """What past works of one tool and model actually cost (newest first):
        the basis for estimating token-priced models."""
        cur = await self.db.execute(
            "SELECT cost_usd, params_json, LENGTH(prompt) AS prompt_len FROM artifacts"
            " WHERE tool=? AND model=? AND cost_usd IS NOT NULL AND cost_usd>0 ORDER BY created_at DESC LIMIT ?",
            (tool, model, limit),
        )
        return [_row(r) for r in await cur.fetchall()]

    # ------------------------------------------------------------ generation jobs (v1.1)
    _GENERATION_COLS = ("finished_at", "model", "title", "status", "error", "error_kind", "cost_usd", "call_id")

    async def create_generation(self, generation_id: str, *, kind: str, tool: str, model: str | None, title: str | None,
                                params: Any, sources: Any, estimate: Any, source: str, meta: Any = None) -> dict[str, Any]:
        await self.db.execute(
            "INSERT INTO generations (id, created_at, kind, tool, model, title, params_json, sources_json, status, estimate_json, source, meta_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (generation_id, time.time(), kind, tool, model, title, _dumps(params), _dumps(sources), "running", _dumps(estimate), source,
             _dumps(meta or None)),
        )
        await self.db.commit()
        return await self.generation(generation_id)  # type: ignore[return-value]

    async def update_generation(self, generation_id: str, *, artifact_ids: list[str] | None = None, **fields: Any) -> None:
        unknown = set(fields) - set(self._GENERATION_COLS)
        if unknown:
            raise ValueError(f"unknown generation fields: {sorted(unknown)}")
        sets = [f"{k}=?" for k in fields]
        vals = list(fields.values())
        if artifact_ids is not None:
            sets.append("artifact_ids_json=?"); vals.append(_dumps(artifact_ids))
        if not sets:
            return
        await self.db.execute(f"UPDATE generations SET {', '.join(sets)} WHERE id=?", [*vals, generation_id])
        await self.db.commit()

    async def generation(self, generation_id: str) -> Optional[dict[str, Any]]:
        cur = await self.db.execute("SELECT * FROM generations WHERE id=?", (generation_id,))
        return _row(await cur.fetchone())

    async def generations(self, *, limit: int = 30, status: str | None = None, kind: str | None = None) -> list[dict[str, Any]]:
        where, params = [], []
        for col, val in (("status", status), ("kind", kind)):
            if val:
                where.append(f"{col}=?"); params.append(val)
        sql = "SELECT * FROM generations" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_at DESC LIMIT ?"
        cur = await self.db.execute(sql, [*params, limit])
        return [_row(r) for r in await cur.fetchall()]

    async def chat_generation_costs(self, cids: Optional[list[str]] = None) -> dict[str, float]:
        """conversation id → the actual cost of every generation started from
        it (proposals and transcriptions, every branch; a job that reported no
        cost is left out). A chat with none is not in the answer."""
        sql = ("SELECT json_extract(meta_json,'$.conversation_id') AS cid, SUM(cost_usd) AS cost FROM generations"
               " WHERE cost_usd IS NOT NULL AND json_extract(meta_json,'$.conversation_id') IS NOT NULL")
        params: list[Any] = []
        if cids is not None:
            if not cids:
                return {}
            sql += f" AND json_extract(meta_json,'$.conversation_id') IN ({', '.join('?' for _ in cids)})"
            params += cids
        cur = await self.db.execute(sql + " GROUP BY cid", params)
        return {r[0]: float(r[1]) for r in await cur.fetchall()}

    async def interrupt_running_generations(self) -> int:
        """At startup nothing can still be running: the tasks died with the
        previous process."""
        cur = await self.db.execute(
            "UPDATE generations SET status='interrupted', error_kind='interrupted', finished_at=?,"
            " error='the service restarted while this was generating' WHERE status='running'",
            (time.time(),),
        )
        await self.db.commit()
        return cur.rowcount

    # ------------------------------------------------------------ misc
    async def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {"path": str(self.path)}
        for table in ("calls", "conversations", "messages", "runs", "events", "artifacts", "generations"):
            cur = await self.db.execute(f"SELECT COUNT(*) FROM {table}")
            out[table] = (await cur.fetchone())[0]
        return out
