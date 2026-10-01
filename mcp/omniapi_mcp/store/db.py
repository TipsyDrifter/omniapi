"""SQLite store at ``~/.omniapi/omniapi.db`` (aiosqlite, no ORM).

Tables
------
calls          one row per MCP tool call (tool, model, cost, duration, status)
conversations  chat threads and agent runs share this header table
messages       chat turns (M6 fills; the daemon REST can already write them)
runs           agent runs (M3 fills)
events         per-run event stream (M3 fills)

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
"""


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
            "messages": {"meta_json": "TEXT"},
        }
        for table, cols in wanted.items():
            cur = await self._db.execute(f"PRAGMA table_info({table})")
            have = {r[1] for r in await cur.fetchall()}
            for name, decl in cols.items():
                if name not in have:
                    await self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                    logger.info("Store migration: %s.%s added", table, name)

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
            where.append("tool=?"); params.append(tool)
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
    ) -> dict[str, Any]:
        cur = await self.db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM messages WHERE conversation_id=?", (cid,))
        seq = (await cur.fetchone())[0]
        mid = uuid.uuid4().hex[:16]
        now = time.time()
        await self.db.execute(
            "INSERT INTO messages (id, conversation_id, seq, role, content_json, created_at, model, usage_json, cost_usd, reasoning, tool_calls_json, meta_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (mid, cid, seq, role, _dumps(content), now, model, _dumps(usage), cost_usd, reasoning, _dumps(tool_calls), _dumps(meta)),
        )
        await self.db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, cid))
        await self.db.commit()
        return {"id": mid, "conversation_id": cid, "seq": seq, "role": role, "content": content, "created_at": now,
                "model": model, "usage": usage, "cost_usd": cost_usd, "reasoning": reasoning, "tool_calls": tool_calls, "meta": meta}

    async def chats(self, *, limit: int = 100, include_archived: bool = False) -> list[dict[str, Any]]:
        """Chat conversations with their message count and total cost, most
        recently active first."""
        sql = (
            "SELECT c.*, COUNT(m.id) AS n_messages, COALESCE(SUM(m.cost_usd),0) AS cost_usd,"
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

    async def messages(self, cid: str, *, limit: int = 500) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY seq ASC LIMIT ?", (cid, limit)
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

    # ------------------------------------------------------------ misc
    async def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {"path": str(self.path)}
        for table in ("calls", "conversations", "messages", "runs", "events"):
            cur = await self.db.execute(f"SELECT COUNT(*) FROM {table}")
            out[table] = (await cur.fetchone())[0]
        return out
