"""1.2-M1 對話地基: attachments by id, branches (regenerate / edit / switch),
the one-reply lock, deleting a chat, and the upgrade of a v1.1 database."""

from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import time
from types import SimpleNamespace as NS

import pytest
from PIL import Image

from omniapi_mcp.bus import EventBus
from omniapi_mcp.chat import ChatError, ChatManager
from omniapi_mcp.chat.images import PreparedImage
from omniapi_mcp.chat.manager import branch_path, newest_leaf, to_provider_message
from omniapi_mcp.store.db import Store
from omniapi_mcp.tools.text import TextTool


def _settings_without_providers():
    off = NS(enabled=False, api_key="")
    return NS(providers=NS(openai=off, deepseek=off, anthropic=off, gemini=off, openrouter=off))


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


class Capture:
    """A text tool that records what each reply was sent. ``gate`` (an Event)
    holds every reply until it is set — a reply that is reliably still running."""

    def __init__(self, key: str = "fake", gate: asyncio.Event | None = None):
        self.key = key
        self.gate = gate
        self.histories: list[list[dict]] = []

    def route(self, model):
        if model == "nope":
            raise RuntimeError("No text provider for model 'nope'.")
        return model, NS(PROVIDER_KEY=self.key)

    async def stream(self, messages, model=None, **params):
        self.histories.append(messages)
        if self.gate is not None:
            await self.gate.wait()
        n = sum(1 for m in messages if m["role"] == "user")
        text = f"reply to {n} question(s) by {model}"
        yield {"type": "text", "delta": text}
        yield {"type": "done", "text": text, "model": model, "provider": self.key, "usage": {"prompt_tokens": 1, "completion_tokens": 1},
               "cost_usd": 0.001, "finish_reason": "stop"}


@pytest.fixture
def dev(monkeypatch):
    monkeypatch.setenv("OMNIAPI_DEV", "1")
    monkeypatch.setenv("OMNIAPI_ECHO_DELAY", "0")


@pytest.fixture
async def store(tmp_path):
    s = Store(tmp_path / "chat.db")
    await s.open()
    yield s
    await s.close()


@pytest.fixture
async def chat(store, dev):
    mgr = ChatManager(store, EventBus(), TextTool(_settings_without_providers()))
    yield mgr
    await mgr.close()


@pytest.fixture
async def cap(store):
    tool = Capture()
    mgr = ChatManager(store, EventBus(), tool)
    mgr.tool = tool
    yield mgr
    await mgr.close()


@pytest.fixture
async def images(store, tmp_path):
    """Two image uploads, one image work, one audio upload — rows plus real files."""
    paths = []
    for i in range(3):
        p = tmp_path / f"img{i}.png"
        p.write_bytes(_png())
        paths.append(p)
    up1 = await store.add_upload(upload_id="up1", kind="image", filename="截圖.png", file_path=str(paths[0]), mime="image/png", size=10)
    up2 = await store.add_upload(upload_id="up2", kind="image", filename="b.png", file_path=str(paths[1]), mime="image/png", size=10)
    work = await store.add_artifact(artifact_id="art1", kind="image", file_path=str(paths[2]), title="燈塔")
    (tmp_path / "a.wav").write_bytes(b"RIFF")
    audio = await store.add_upload(upload_id="aud1", kind="audio", filename="a.wav", file_path=str(tmp_path / "a.wav"), mime="audio/wav", size=4)
    return NS(up1=up1, up2=up2, work=work, audio=audio)


async def _settle(mgr: ChatManager, cid: str):
    task = mgr._tasks.get(cid)
    if task:
        await asyncio.wait_for(task, 5)


def _path_text(conv) -> list[tuple[str, str]]:
    return [(m["role"], m["content"]) for m in conv["messages"]]


# ================================================================ upgrade of a v1.1 database
V11_SCHEMA = (
    "CREATE TABLE conversations (id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,"
    " model TEXT, harness TEXT, cwd TEXT, status TEXT, system_prompt TEXT, meta_json TEXT);"
    "CREATE TABLE messages (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, seq INTEGER NOT NULL, role TEXT NOT NULL,"
    " content_json TEXT NOT NULL, created_at REAL NOT NULL, model TEXT, usage_json TEXT, cost_usd REAL, reasoning TEXT, tool_calls_json TEXT, meta_json TEXT);"
)


def _old_db(path):
    con = sqlite3.connect(path)
    con.executescript(V11_SCHEMA)
    con.execute("INSERT INTO conversations VALUES ('old','chat','舊對話',1,5,'echo-fast',NULL,NULL,'open',NULL,'{\"source\": \"gui\"}')")
    con.execute("INSERT INTO conversations VALUES ('empty','chat','空的',1,1,'echo-fast',NULL,NULL,'open',NULL,NULL)")
    # inserted out of order on purpose: the line is the seq order, not the rowid order
    rows = [("m3", 3, "user", "第二問"), ("m1", 1, "user", "第一問"), ("m4", 4, "assistant", "第二答"), ("m2", 2, "assistant", "第一答")]
    for mid, seq, role, text in rows:
        con.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,NULL,NULL,NULL,NULL,NULL,NULL)", (mid, "old", seq, role, json.dumps(text, ensure_ascii=False), float(seq)))
    con.commit()
    con.close()


async def test_a_v11_database_upgrades_into_one_branch_in_seq_order(tmp_path, dev):
    path = tmp_path / "old.db"
    _old_db(path)
    s = Store(path)
    await s.open()
    try:
        raw = {m["id"]: m for m in await s.messages("old")}
        assert [raw[m]["parent_id"] for m in ("m1", "m2", "m3", "m4")] == [None, "m1", "m2", "m3"]
        assert (await s.conversation("old"))["meta"] == {"source": "gui", "leaf_id": "m4"}
        assert (await s.conversation("empty"))["meta"] is None  # a conversation without messages is left as it was

        mgr = ChatManager(s, EventBus(), TextTool(_settings_without_providers()))
        conv = await mgr.get("old")
        assert _path_text(conv) == [("user", "第一問"), ("assistant", "第一答"), ("user", "第二問"), ("assistant", "第二答")]
        assert all(m["versions"] == {"count": 1, "index": 1, "ids": [m["id"]]} for m in conv["messages"])
        assert (await mgr.get("empty"))["messages"] == []
        # and the old conversation goes on from where it was
        third = await mgr.send_and_wait("old", "第三問")
        assert third["message"]["parent_id"] == third["user_message"]["id"] and third["user_message"]["parent_id"] == "m4"
        assert "第 **3** 輪" in third["message"]["content"]
        empty = await mgr.send_and_wait("empty", "第一句")
        assert empty["user_message"]["parent_id"] is None and empty["state"] == "done"
        await mgr.close()
    finally:
        await s.close()

    s2 = Store(path)  # opening again changes nothing
    await s2.open()
    try:
        cur = await s2.db.execute("PRAGMA user_version")
        assert (await cur.fetchone())[0] == 1
        assert {m["id"]: m["parent_id"] for m in await s2.messages("old")}["m3"] == "m2"
        mgr = ChatManager(s2, EventBus(), TextTool(_settings_without_providers()))
        assert [m["content"] for m in (await mgr.get("old"))["messages"]][:4] == ["第一問", "第一答", "第二問", "第二答"]
    finally:
        await s2.close()


async def test_linking_again_leaves_branched_conversations_alone(store, cap):
    conv = await cap.create(model="m")
    first = await cap.send_and_wait(conv["id"], "一")
    await cap.wait(await cap.edit(conv["id"], first["user_message"]["id"], "一（改）"))  # two opening messages, both without parent
    before = {m["id"]: m["parent_id"] for m in await store.messages(conv["id"])}
    await store._link_linear_history()
    await store.db.commit()
    assert {m["id"]: m["parent_id"] for m in await store.messages(conv["id"])} == before


# ================================================================ pure helpers
def test_branch_path_and_newest_leaf():
    rows = [{"id": "a", "seq": 1, "parent_id": None}, {"id": "b", "seq": 2, "parent_id": "a"}, {"id": "c", "seq": 3, "parent_id": "a"},
            {"id": "d", "seq": 4, "parent_id": "b"}, {"id": "e", "seq": 5, "parent_id": None}]
    assert [r["id"] for r in branch_path(rows, "d")] == ["a", "b", "d"]
    assert branch_path(rows, None) == [] and branch_path(rows, "zz") == []
    assert newest_leaf(rows, "a") == "d" and newest_leaf(rows, "c") == "c" and newest_leaf(rows, "e") == "e"


def test_provider_message_is_text_unless_the_reply_takes_images():
    m = {"id": "m1", "role": "user", "content": "看這張", "attachments": [{"kind": "image", "upload_id": "u1"}, {"kind": "image", "artifact_id": "a1"}]}
    assert to_provider_message(m, images=False) == {"role": "user", "content": "看這張"}
    img = PreparedImage("image/png", "QUJD")
    parts = to_provider_message(m, images=True, prepared={"m1:0": img, "m1:1": img})["content"]
    assert parts[0] == {"type": "text", "text": "看這張"}
    assert parts[1:] == [{"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}] * 2
    assert to_provider_message({"role": "user", "content": "", "attachments": m["attachments"]}, images=False)["content"] == "[2 張圖片]"
    assert to_provider_message({"role": "assistant", "content": ""}, images=True) is None
    # 1.2-M4: a tool result goes to the provider as a tool message; folded away when the turn has no tools
    assert to_provider_message({"role": "tool", "content": "x", "meta": {"tool_call_id": "c1"}}, images=True) == \
        {"role": "tool", "tool_call_id": "c1", "content": "x"}
    assert to_provider_message({"role": "tool", "content": "x"}, images=True, tools=False) is None
    assert to_provider_message({"role": "system", "content": "x"}, images=True) is None


# ================================================================ attachments
async def test_attachments_are_stored_as_ids_and_listed_with_urls(chat, store, images):
    conv = await chat.create(model="echo-fast")
    res = await chat.send_and_wait(conv["id"], "這兩張圖差在哪", attachments=[{"upload_id": "up1"}, {"kind": "image", "artifact_id": "art1"}])
    assert res["state"] == "done"
    assert "這一則附了 **2** 張圖" in res["message"]["content"]  # the echo model saw both

    cur = await store.db.execute("SELECT attachments_json FROM messages WHERE id=?", (res["user_message"]["id"],))
    stored = json.loads((await cur.fetchone())[0])
    assert stored == [{"kind": "image", "upload_id": "up1"}, {"kind": "image", "artifact_id": "art1"}]  # ids only, no paths

    got = await chat.get(conv["id"])
    atts = got["messages"][0]["attachments"]
    assert [a["name"] for a in atts] == ["截圖.png", "燈塔"] and all(a["exists"] for a in atts)
    assert atts[0]["file_url"] == "/api/uploads/up1/file" and atts[1]["thumb_url"] == "/api/artifacts/art1/thumb"
    assert "file_path" not in json.dumps(got, ensure_ascii=False)
    assert got["messages"][1]["attachments"] is None

    _, md = await chat.export_markdown(conv["id"])
    assert "> 附圖 2 張：截圖.png、燈塔" in md

    # an image-only message is a message; the title says it was an image
    conv2 = await chat.create(model="echo-fast")
    only = await chat.send_and_wait(conv2["id"], "", attachments=[{"upload_id": "up2"}])
    assert only["state"] == "done" and "附了 **1** 張圖" in only["message"]["content"]
    assert (await chat.get(conv2["id"]))["title"] == "（圖片）"


async def test_bad_attachments_are_refused_before_anything_is_stored(chat, store, images):
    conv = await chat.create(model="echo-fast")
    cases = [
        ([{"kind": "image", "upload_id": "aud1"}], 400),      # 1.2-M5: audio is attachable, but not as an image
        ([{"kind": "video", "upload_id": "up1"}], 400),
        ([{"upload_id": "nope"}], 404),
        ([{"artifact_id": "nope"}], 404),
        ([{"file_path": "C:/Windows/win.ini"}], 400),          # paths never
        ([{"upload_id": "up1", "artifact_id": "art1"}], 400),  # one or the other
        ([{"kind": "audio", "upload_id": "up1"}], 400),
        ("up1", 400),
        ([{"upload_id": "up1"}] * 11, 400),
    ]
    for atts, status in cases:
        with pytest.raises(ChatError) as e:
            await chat.send(conv["id"], "x", attachments=atts)
        assert e.value.status == status, atts
    assert await store.messages(conv["id"]) == [] and not chat.busy(conv["id"])
    with pytest.raises(ChatError, match="empty"):
        await chat.send(conv["id"], "  ", attachments=[])


async def test_a_model_the_catalog_does_not_know_gets_text_only(cap, images):
    conv = await cap.create(model="gpt-x")
    res = await cap.send_and_wait(conv["id"], "看圖", attachments=[{"upload_id": "up1"}])
    assert cap.tool.histories[-1] == [{"role": "user", "content": "看圖"}]
    assert res["vision"] is False and res["images_skipped"] == 1  # 1.2-M2: said, not silently dropped


# ================================================================ regenerate / switch
async def test_regenerate_adds_a_version_and_switching_moves_the_branch(cap, store):
    conv = await cap.create(model="m1")
    cid = conv["id"]
    first = await cap.send_and_wait(cid, "問題")
    old_reply = first["message"]

    again = await cap.wait(await cap.regenerate(cid, old_reply["id"], model="m2"))
    assert again["action"] == "regenerate" and again["regenerate_of"] == old_reply["id"]
    assert again["message"]["parent_id"] == old_reply["parent_id"] == first["user_message"]["id"]
    assert again["message"]["model"] == "m2"
    # the regenerated reply was sent the question, not the old answer
    assert cap.tool.histories[-1] == [{"role": "user", "content": "問題"}]

    got = await cap.get(cid)
    assert [m["id"] for m in got["messages"]] == [first["user_message"]["id"], again["message"]["id"]]
    assert got["messages"][1]["versions"] == {"count": 2, "index": 2, "ids": [old_reply["id"], again["message"]["id"]]}
    assert got["leaf_id"] == again["message"]["id"] and got["n_messages"] == 3
    assert len(await store.calls(tool="chat")) == 2  # one ledger row per reply

    back = await cap.switch(cid, old_reply["id"])
    assert back["messages"][1]["id"] == old_reply["id"] and back["messages"][1]["versions"]["index"] == 1

    # regenerating from the user message answers it again too
    third = await cap.wait(await cap.regenerate(cid, first["user_message"]["id"]))
    assert third["regenerate_of"] is None and third["message"]["parent_id"] == first["user_message"]["id"]
    assert (await cap.get(cid))["messages"][1]["versions"]["count"] == 3


async def test_history_follows_only_the_current_branch(cap):
    conv = await cap.create(model="m")
    cid = conv["id"]
    q1 = await cap.send_and_wait(cid, "Q1")
    await cap.send_and_wait(cid, "Q2-old")
    # branch at Q1's reply: switch back to it, then ask something else
    await cap.switch(cid, q1["message"]["id"])
    # switching to a message with children goes to the newest leaf below it, i.e. back to Q2-old's reply
    assert (await cap.get(cid))["messages"][-1]["content"].startswith("reply to 2")
    edited = await cap.wait(await cap.edit(cid, (await cap.get(cid))["messages"][2]["id"], "Q2-new"))
    assert cap.tool.histories[-1] == [{"role": "user", "content": "Q1"}, {"role": "assistant", "content": q1["message"]["content"]},
                                      {"role": "user", "content": "Q2-new"}]
    await cap.send_and_wait(cid, "Q3")
    sent = [m["content"] for m in cap.tool.histories[-1] if m["role"] == "user"]
    assert sent == ["Q1", "Q2-new", "Q3"]  # Q2-old is on the other branch
    assert edited["message"]["content"].startswith("reply to 2")


async def test_edit_branches_from_the_first_message_and_the_old_branch_stays(cap, images):
    conv = await cap.create(model="m")
    cid = conv["id"]
    a = await cap.send_and_wait(cid, "原本的第一問", attachments=[{"upload_id": "up1"}])
    await cap.send_and_wait(cid, "原本的第二問")
    old_path = [m["id"] for m in (await cap.get(cid))["messages"]]

    e = await cap.wait(await cap.edit(cid, a["user_message"]["id"], "改過的第一問"))
    assert e["action"] == "edit" and e["edit_of"] == a["user_message"]["id"] and e["user_message"]["parent_id"] is None
    assert [x["upload_id"] for x in e["user_message"]["attachments"]] == ["up1"]  # images go along unless replaced
    got = await cap.get(cid)
    assert _path_text(got)[0] == ("user", "改過的第一問") and len(got["messages"]) == 2
    assert got["messages"][0]["versions"] == {"count": 2, "index": 2, "ids": [a["user_message"]["id"], e["user_message"]["id"]]}

    back = await cap.switch(cid, a["user_message"]["id"])
    assert [m["id"] for m in back["messages"]] == old_path  # the whole old branch, down to its newest reply

    dropped = await cap.wait(await cap.edit(cid, a["user_message"]["id"], "不帶圖", attachments=[]))
    assert dropped["user_message"]["attachments"] is None
    with pytest.raises(ChatError) as notmine:
        await cap.edit(cid, a["message"]["id"], "改回覆")
    assert notmine.value.status == 400
    with pytest.raises(ChatError) as gone:
        await cap.edit(cid, "nope", "x")
    assert gone.value.status == 404


async def test_ids_from_another_conversation_are_not_found(cap):
    a = await cap.create(model="m")
    b = await cap.create(model="m")
    ra = await cap.send_and_wait(a["id"], "A")
    for call in (cap.regenerate(b["id"], ra["message"]["id"]), cap.edit(b["id"], ra["user_message"]["id"], "x"), cap.switch(b["id"], ra["message"]["id"])):
        with pytest.raises(ChatError) as e:
            await call
        assert e.value.status == 404


# ================================================================ the one-reply lock
async def test_one_reply_at_a_time_for_send_regenerate_edit_and_switch(store):
    gate = asyncio.Event()
    mgr = ChatManager(store, EventBus(), Capture(gate=gate))
    try:
        conv = await mgr.create(model="m")
        cid = conv["id"]
        gate.set()
        first = await mgr.send_and_wait(cid, "一")
        gate.clear()
        await mgr.send(cid, "二")  # now held open by the gate
        for attempt in (mgr.send(cid, "插隊"), mgr.regenerate(cid, first["message"]["id"]), mgr.edit(cid, first["user_message"]["id"], "改"),
                        mgr.switch(cid, first["message"]["id"])):
            with pytest.raises(ChatError) as busy:
                await attempt
            assert busy.value.status == 409
        with pytest.raises(ChatError) as arch:
            await mgr.update(cid, archived=True)
        assert arch.value.status == 409
        gate.set()
        await _settle(mgr, cid)
        assert len(await store.messages(cid)) == 4  # nothing extra was stored by the refused requests

        # two requests at the same moment: exactly one wins
        gate.clear()
        results = await asyncio.gather(mgr.regenerate(cid, first["message"]["id"]), mgr.edit(cid, first["user_message"]["id"], "同時"),
                                       return_exceptions=True)
        assert sum(isinstance(r, ChatError) and r.status == 409 for r in results) == 1
        gate.set()
        await _settle(mgr, cid)
        assert not mgr.busy(cid)
    finally:
        gate.set()
        await mgr.close()


async def test_archived_conversations_refuse_every_kind_of_turn(cap):
    conv = await cap.create(model="m")
    cid = conv["id"]
    r = await cap.send_and_wait(cid, "一")
    await cap.update(cid, archived=True)
    for attempt in (cap.regenerate(cid, r["message"]["id"]), cap.edit(cid, r["user_message"]["id"], "改"), cap.switch(cid, r["message"]["id"])):
        with pytest.raises(ChatError) as e:
            await attempt
        assert e.value.status == 409


async def test_a_failed_setup_releases_the_lock(cap):
    conv = await cap.create(model="m")
    cid = conv["id"]
    with pytest.raises(ChatError):
        await cap.send(cid, "x", model="nope")
    with pytest.raises(ChatError):
        await cap.regenerate(cid, "nope")
    assert not cap.busy(cid)
    assert (await cap.send_and_wait(cid, "好了"))["state"] == "done"


# ================================================================ delete
async def test_delete_removes_the_chat_but_keeps_ledger_works_and_uploads(cap, store, images):
    q = cap.bus.subscribe()
    conv = await cap.create(model="m")
    cid = conv["id"]
    first = await cap.send_and_wait(cid, "一", attachments=[{"upload_id": "up1"}, {"artifact_id": "art1"}])
    await cap.wait(await cap.regenerate(cid, first["message"]["id"]))
    keep = await cap.create(model="m")
    await cap.send_and_wait(keep["id"], "留著")

    out = await cap.delete(cid)
    assert out == {"conversation_id": cid, "deleted": True, "messages": 3, "cancelled": False}
    assert [c["id"] for c in await cap.list(include_archived=True)] == [keep["id"]]
    with pytest.raises(ChatError) as gone:
        await cap.get(cid)
    assert gone.value.status == 404
    assert await store.messages(cid) == [] and await store.conversation(cid) is None
    assert len(await store.messages(keep["id"])) == 2  # the other chat is untouched

    calls = await store.calls(tool="chat")
    assert len([c for c in calls if c["conversation_id"] == cid]) == 2  # the money spent stays on the ledger
    summary = await store.cost_summary(days=1)
    assert summary["total"]["n"] == 3 and round(summary["total"]["cost"], 6) == 0.003
    assert await store.upload("up1") and await store.artifact("art1")
    assert any(e["type"] == "chat.deleted" and e["conversation_id"] == cid for e in [q.get_nowait() for _ in range(q.qsize())])

    with pytest.raises(ChatError) as again:
        await cap.delete(cid)
    assert again.value.status == 404


async def test_deleting_a_streaming_chat_cancels_it_first_and_leaves_nothing(store):
    gate = asyncio.Event()
    mgr = ChatManager(store, EventBus(), Capture(gate=gate))
    try:
        conv = await mgr.create(model="m")
        cid = conv["id"]
        await mgr.send(cid, "一")
        await asyncio.sleep(0.05)
        out = await mgr.delete(cid)
        assert out["cancelled"] is True and out["deleted"] is True
        await asyncio.sleep(0.05)
        cur = await store.db.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=?", (cid,))
        assert (await cur.fetchone())[0] == 0  # the partial reply did not land afterwards
        (call,) = await store.calls(tool="chat")
        assert call["status"] == "error" and call["error"] == "cancelled"
        assert not mgr.busy(cid) and mgr.live_count == 0
    finally:
        gate.set()
        await mgr.close()


async def test_runs_cannot_be_deleted_through_chat(cap, store):
    run_conv = await store.create_conversation(kind="run", title="派工")
    with pytest.raises(ChatError) as e:
        await cap.delete(run_conv["id"])
    assert e.value.status == 404 and await store.conversation(run_conv["id"])


# ================================================================ REST
@pytest.fixture
def daemon(tmp_path, monkeypatch, dev):
    """The real FastAPI app over a tmp store, echo models on (as in test_chat_entrypoints)."""
    from fastapi.testclient import TestClient

    from omniapi_mcp import server as srv
    from omniapi_mcp.daemon import app as dapp
    from omniapi_mcp.runtime import runtime

    monkeypatch.setenv("OMNIAPI_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setattr(srv.mcp, "_session_manager", None)
    monkeypatch.setattr(srv.mcp.settings, "streamable_http_path", srv.mcp.settings.streamable_http_path)
    monkeypatch.setattr(srv, "settings", srv.settings)
    held: dict = {}

    async def acquire(settings, owner="daemon"):
        store = Store(tmp_path / "daemon.db")
        await store.open()
        bus = EventBus()
        mgr = ChatManager(store, bus, TextTool(_settings_without_providers()))
        runtime.context = NS(chat=mgr, bus=bus, store=store, mode="daemon")
        held.update(store=store, chat=mgr)
        return runtime.context

    async def release(owner="daemon"):
        await held["chat"].close()
        await held["store"].close()
        runtime.context = None

    monkeypatch.setattr(runtime, "acquire", acquire)
    monkeypatch.setattr(runtime, "release", release)
    monkeypatch.setattr(runtime, "context", None)
    settings = NS(storage=NS(base_path=str(tmp_path / "storage")))
    app = dapp.create_app(settings, host="127.0.0.1", port=7799)
    with TestClient(app, base_url="http://127.0.0.1:7799") as client:
        yield client


def test_rest_attachments_regenerate_edit_switch_and_delete(daemon):
    up = daemon.post("/api/uploads", params={"filename": "ref.png"}, content=_png()).json()
    assert "file_path" not in up and up["file_url"] == f"/api/uploads/{up['id']}/file"

    conv = daemon.post("/api/chat", json={"model": "echo-fast", "message": "看圖", "attachments": [{"upload_id": up["id"]}]}).json()
    cid = conv["id"]
    assert conv["turn"]["user_message"]["attachments"][0]["upload_id"] == up["id"]
    for _ in range(100):  # the first reply streams in the app's own loop
        if daemon.get(f"/api/chat/{cid}").json()["live"] is None:
            break
        time.sleep(0.02)
    sent = daemon.post(f"/api/chat/{cid}/messages", params={"wait": "true"}, json={"text": "第二則", "attachments": [{"upload_id": up["id"]}]}).json()
    assert sent["state"] == "done" and "附了 **1** 張圖" in sent["message"]["content"]
    bad = daemon.post(f"/api/chat/{cid}/messages", json={"text": "x", "attachments": [{"upload_id": "nope"}]})
    assert bad.status_code == 404

    reply = sent["message"]
    regen = daemon.post(f"/api/chat/{cid}/messages/{reply['id']}/regenerate", params={"wait": "true"}, json={"model": "echo"}).json()
    assert regen["state"] == "done" and regen["message"]["model"] == "echo" and regen["regenerate_of"] == reply["id"]
    assert daemon.post(f"/api/chat/{cid}/messages/{reply['id']}/regenerate", params={"wait": "true"}).json()["state"] == "done"  # no body
    got = daemon.get(f"/api/chat/{cid}").json()
    assert got["messages"][-1]["versions"]["count"] == 3 and got["messages"][-1]["versions"]["index"] == 3

    switched = daemon.post(f"/api/chat/{cid}/switch", json={"message_id": reply["id"]}).json()
    assert switched["messages"][-1]["id"] == reply["id"] and switched["leaf_id"] == reply["id"]
    assert daemon.post(f"/api/chat/{cid}/switch", json={}).status_code == 400
    assert daemon.post(f"/api/chat/{cid}/switch", json={"message_id": "nope"}).status_code == 404

    first_user = got["messages"][0]
    edited = daemon.post(f"/api/chat/{cid}/messages/{first_user['id']}/edit", params={"wait": "true"}, json={"text": "改過的"}).json()
    assert edited["state"] == "done" and edited["user_message"]["parent_id"] is None
    assert daemon.post(f"/api/chat/{cid}/messages/{first_user['id']}/edit", json={"text": "x", "path": "C:/"}).status_code == 400
    now = daemon.get(f"/api/chat/{cid}").json()
    assert [m["content"] for m in now["messages"] if m["role"] == "user"] == ["改過的"]
    assert now["messages"][0]["versions"]["count"] == 2

    raw = daemon.get(f"/api/conversations/{cid}").json()  # the raw view still has every branch
    assert len(raw["messages"]) == 8

    assert daemon.delete(f"/api/chat/{cid}").json()["deleted"] is True
    assert daemon.get(f"/api/chat/{cid}").status_code == 404
    assert cid not in [c["id"] for c in daemon.get("/api/chat", params={"archived": "true"}).json()]
    calls = daemon.get("/api/calls", params={"tool": "chat"}).json()
    assert len([c for c in calls if c["conversation_id"] == cid]) == 5  # the ledger still answers, with the deleted chat's rows
    assert daemon.get("/api/costs").status_code == 200
    assert daemon.get(f"/api/uploads/{up['id']}/file").content == _png()  # the upload outlives the chat
    assert daemon.delete(f"/api/chat/{cid}").status_code == 404
