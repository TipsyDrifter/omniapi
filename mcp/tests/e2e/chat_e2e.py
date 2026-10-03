"""Chat foundation (1.2-M1) end-to-end against an OFFLINE sandbox daemon (no vendor call):

    python tests/e2e/chat_e2e.py seed <empty OMNIAPI_HOME>     # optional: a v1.1-shaped database with one old chat
    OMNIAPI_HOME=<that dir> OMNIAPI_DEV=1 OMNIAPI_OFFLINE=1 omni serve --port 7803
    python tests/e2e/chat_e2e.py [port]

Walks the roadmap's acceptance for 1.2-M1 the way the GUI will (REST + /ws):
send one with an image, regenerate it, both versions exist and switch; edit
the first message, a new branch appears and the old one is still there; the
one-reply lock answers 409; delete the chat, it leaves the list while its
ledger rows stay. With the seed step, the chat written before the upgrade
opens with the same content.

1.2-M2 (attachments, backend): the model list gives every text model a
``vision`` boolean; the seeing echo model is sent the image, the blind one
(``echo-blind``) answers without it and the turn, the event and the stored
reply say one image was skipped.

1.2-M4 (generation in a chat, backend): every text model says whether it
takes tools; asking the echo model for a picture brings a pending proposal
and nothing is spent; the estimate answers; accepting runs a chat-sourced
generation whose work is on the wall and on the message; declining, two
proposals at once, a waiting proposal declined by sending on, and the blind
model proposing nothing; the export marks proposals; a reply being
regenerated reports its place among versions (``live.versions``).

1.2-M5 (file attachments, backend): chat uploads take any file and say what
it holds; a PDF and a csv asked about (the PDF as it is, the csv as text);
a big file goes as its opening and the echo model reads on with
``read_file`` (``chat.tool`` events); an unknown binary is said to be
unreadable; audio the model cannot hear makes the turn wait for the owner —
transcribe then reply, or reply without it — and the transcript is readable
afterwards; the export marks files and reads; deleting leaves the uploads.
"""

import asyncio
import io
import json
import sqlite3
import sys
from pathlib import Path

import httpx
import websockets

OLD_CID = "e2e-v11-chat"
OLD_MESSAGES = [("user", "升級前的第一問"), ("assistant", "升級前的第一答"), ("user", "升級前的第二問"), ("assistant", "升級前的第二答")]


def seed(home: Path) -> None:
    """A database as v1.1 left it: no parent_id / attachments_json, user_version 0."""
    home.mkdir(parents=True, exist_ok=True)
    db = home / "omniapi.db"
    if db.exists():
        raise SystemExit(f"{db} exists already — seed an empty sandbox home")
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE conversations (id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,"
        " model TEXT, harness TEXT, cwd TEXT, status TEXT, system_prompt TEXT, meta_json TEXT);"
        "CREATE TABLE messages (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE, seq INTEGER NOT NULL,"
        " role TEXT NOT NULL, content_json TEXT NOT NULL, created_at REAL NOT NULL, model TEXT, usage_json TEXT, cost_usd REAL, reasoning TEXT,"
        " tool_calls_json TEXT, meta_json TEXT);"
    )
    con.execute("INSERT INTO conversations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (OLD_CID, "chat", "升級前的對話", 1_700_000_000, 1_700_000_100, "echo-fast", None, None, "open", None, json.dumps({"source": "gui"})))
    for seq, (role, text) in enumerate(OLD_MESSAGES, 1):
        con.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"old{seq}", OLD_CID, seq, role, json.dumps(text, ensure_ascii=False), 1_700_000_000 + seq,
                     "echo-fast" if role == "assistant" else None, None, 0.0 if role == "assistant" else None, None, None,
                     json.dumps({"state": "done"}) if role == "assistant" else None))
    con.commit()
    con.close()
    print("seeded", db)


PORT = 7803
BASE = f"http://127.0.0.1:{PORT}"


def check(label: str, ok: bool, detail: object = "") -> None:
    if not ok:
        print("FAIL " + label, detail)
        raise SystemExit(1)
    print("PASS " + label)


def png() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (40, 90, 200)).save(buf, format="PNG")
    return buf.getvalue()


async def ws_collect(stop: asyncio.Event, out: list) -> None:
    async with websockets.connect(BASE.replace("http", "ws") + "/ws") as ws:
        while not stop.is_set():
            try:
                out.append(json.loads(await asyncio.wait_for(ws.recv(), timeout=0.5)))
            except asyncio.TimeoutError:
                continue


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE, timeout=60) as http:
        st = (await http.get("/api/status")).json()
        check("the daemon is an offline dev sandbox", st["offline"] and st["dev"], st)
        check("this is not the owner's service", PORT != 7788)

        # ---- the chat written before the upgrade
        old = await http.get(f"/api/chat/{OLD_CID}")
        if old.status_code == 404:
            print("SKIP the pre-upgrade chat (run the seed step before starting the daemon to check it)")
        else:
            old = old.json()
            check("the pre-upgrade chat opens with the same content in the same order",
                  [(m["role"], m["content"]) for m in old["messages"]] == OLD_MESSAGES, old["messages"])
            check("it is one branch: every message is the only version", all(m["versions"]["count"] == 1 for m in old["messages"]))
            check("its totals are unchanged", old["n_messages"] == 4 and old["title"] == "升級前的對話", (old["n_messages"], old["title"]))

        stop, events = asyncio.Event(), []
        ws_task = asyncio.create_task(ws_collect(stop, events))
        await asyncio.sleep(0.5)

        # ---- an image travels by id
        up = await http.post("/api/uploads", params={"filename": "截圖.png"}, content=png())
        check("upload accepted", up.status_code == 200, up.text)
        up = up.json()
        check("the upload answer names no path on disk", "file_path" not in up and up["file_url"] == f"/api/uploads/{up['id']}/file", up)

        conv = (await http.post("/api/chat", json={"model": "echo-fast"})).json()
        cid = conv["id"]
        first = (await http.post(f"/api/chat/{cid}/messages", params={"wait": "true"},
                                 json={"text": "這張圖是什麼", "attachments": [{"upload_id": up["id"]}]})).json()
        check("the echo model says how many images it got", first["state"] == "done" and "附了 **1** 張圖" in first["message"]["content"], first)
        bad = await http.post(f"/api/chat/{cid}/messages", json={"text": "x", "attachments": [{"file_path": "C:/Windows/win.ini"}]})
        check("an attachment given as a path is refused", bad.status_code == 400, bad.text)

        # ---- 1.2-M2: who sees images is the catalog's vision flag
        texts = (await http.get("/api/models", params={"modality": "text"})).json()["models"]["text"]
        check("every text model in the list carries a vision boolean", texts and all(isinstance((m.get("capabilities") or {}).get("vision"), bool) for m in texts),
              [m["id"] for m in texts if not isinstance((m.get("capabilities") or {}).get("vision"), bool)])
        echo_vision = {m["id"]: m["capabilities"]["vision"] for m in texts if m["provider"] == "echo"}
        check("the sandbox has a seeing and a blind echo model", echo_vision == {"echo": True, "echo-fast": True, "echo-blind": False}, echo_vision)
        check("the seeing echo model was sent the image (vision turn, nothing skipped)",
              first["vision"] is True and first["images_skipped"] == 0 and first["message"]["meta"].get("images_sent") == 1, first["message"]["meta"])
        blind = (await http.post("/api/chat", json={"model": "echo-blind"})).json()
        bl = (await http.post(f"/api/chat/{blind['id']}/messages", params={"wait": "true"},
                              json={"text": "你看得到嗎", "attachments": [{"upload_id": up["id"]}]})).json()
        check("a blind model still answers an image message (no 400)", bl.get("state") == "done", bl)
        check("…without the image, and the turn says one was skipped",
              bl["vision"] is False and bl["images_skipped"] == 1 and "附了" not in bl["message"]["content"], {k: bl.get(k) for k in ("vision", "images_skipped")})
        check("the stored reply keeps the mark", (await http.get(f"/api/chat/{blind['id']}")).json()["messages"][-1]["meta"].get("images_skipped") == 1)

        # ---- regenerate: two versions, switch between them
        r1 = first["message"]
        r2 = (await http.post(f"/api/chat/{cid}/messages/{r1['id']}/regenerate", params={"wait": "true"}, json={"model": "echo"})).json()
        check("regenerate answers the same question again", r2["state"] == "done" and r2["message"]["parent_id"] == r1["parent_id"], r2)
        got = (await http.get(f"/api/chat/{cid}")).json()
        last = got["messages"][-1]
        check("both versions exist and the new one is shown", last["id"] == r2["message"]["id"] and last["versions"]["count"] == 2
              and last["versions"]["ids"] == [r1["id"], r2["message"]["id"]], last.get("versions"))
        sw = (await http.post(f"/api/chat/{cid}/switch", json={"message_id": r1["id"]})).json()
        check("switching shows version 1", sw["messages"][-1]["id"] == r1["id"] and sw["messages"][-1]["versions"]["index"] == 1)
        sw = (await http.post(f"/api/chat/{cid}/switch", json={"message_id": r2["message"]["id"]})).json()
        check("and back to version 2", sw["messages"][-1]["id"] == r2["message"]["id"] and sw["messages"][-1]["versions"]["index"] == 2)

        # ---- a second turn, then edit the first message
        second = (await http.post(f"/api/chat/{cid}/messages", params={"wait": "true"}, json={"text": "第二問"})).json()
        check("the second turn sees two turns of history", "第 **2** 輪" in second["message"]["content"], second["message"]["content"][:60])
        old_path = [m["id"] for m in (await http.get(f"/api/chat/{cid}")).json()["messages"]]
        q1 = old_path[0]
        ed = (await http.post(f"/api/chat/{cid}/messages/{q1}/edit", params={"wait": "true"}, json={"text": "改過的第一問"})).json()
        check("the edited first message starts a new branch", ed["state"] == "done" and ed["user_message"]["parent_id"] is None, ed)
        check("its reply only saw the new branch", "第 **1** 輪" in ed["message"]["content"] and "附了" in ed["message"]["content"],
              ed["message"]["content"][:80])
        now = (await http.get(f"/api/chat/{cid}")).json()
        check("the conversation now shows the new branch", [m["content"] for m in now["messages"] if m["role"] == "user"] == ["改過的第一問"]
              and now["messages"][0]["versions"] == {"count": 2, "index": 2, "ids": [q1, ed["user_message"]["id"]]}, now["messages"][0])
        back = (await http.post(f"/api/chat/{cid}/switch", json={"message_id": q1})).json()
        check("the old branch is still there, down to its last reply", [m["id"] for m in back["messages"]] == old_path)

        # ---- one reply at a time
        slow = (await http.post(f"/api/chat/{cid}/messages", json={"text": "慢慢回", "model": "echo"})).json()
        check("a slow reply is streaming", slow["state"] == "streaming", slow)
        for label, req in (("send", http.post(f"/api/chat/{cid}/messages", json={"text": "插隊"})),
                           ("regenerate", http.post(f"/api/chat/{cid}/messages/{r1['id']}/regenerate", json={})),
                           ("edit", http.post(f"/api/chat/{cid}/messages/{q1}/edit", json={"text": "x"})),
                           ("switch", http.post(f"/api/chat/{cid}/switch", json={"message_id": r1["id"]}))):
            r = await req
            check(f"{label} while a reply streams is 409", r.status_code == 409, f"{r.status_code} {r.text[:120]}")
        check("cancel stops it", (await http.post(f"/api/chat/{cid}/cancel")).json()["cancelled"] is True)

        # ---- export: the shown branch, images marked
        md = (await http.get(f"/api/chat/{cid}/export", params={"download": "false"})).text
        check("the export marks the image and says the chat has branches", "> 附圖 1 張：截圖.png" in md and "有分岔" in md, md[:400])

        # ---- delete
        calls_before = [c for c in (await http.get("/api/calls", params={"tool": "chat", "limit": 200})).json() if c["conversation_id"] == cid]
        # first, regenerate, second, edit, and the cancelled slow one: one row per reply
        check("every reply is on the ledger", len(calls_before) == 5, [(c["status"], c.get("error")) for c in calls_before])
        gone = (await http.delete(f"/api/chat/{cid}")).json()
        check("delete says it deleted the chat", gone["deleted"] is True and gone["messages"] > 0, gone)
        check("the chat is gone from the list", cid not in [c["id"] for c in (await http.get("/api/chat", params={"archived": "true"})).json()])
        check("and cannot be opened", (await http.get(f"/api/chat/{cid}")).status_code == 404)
        calls_after = [c for c in (await http.get("/api/calls", params={"tool": "chat", "limit": 200})).json() if c["conversation_id"] == cid]
        check("its ledger rows are all still there", [c["id"] for c in calls_after] == [c["id"] for c in calls_before])
        check("the costs page still answers", (await http.get("/api/costs")).status_code == 200)
        check("the uploaded image outlives the chat", (await http.get(up["file_url"])).status_code == 200)

        await proposals(http, events)
        await files(http, events)

        await asyncio.sleep(0.5)
        stop.set()
        await ws_task
        actions = [e.get("action") for e in events if e.get("type") == "chat.started" and e.get("conversation_id") == cid]
        check("the bus announced each kind of turn", {"send", "regenerate", "edit"} <= set(actions), actions)
        blind_started = [e for e in events if e.get("type") == "chat.started" and e.get("conversation_id") == blind["id"]]
        check("chat.started tells the GUI the blind model will skip the image",
              blind_started and blind_started[0].get("vision") is False and blind_started[0].get("images_skipped") == 1, blind_started)
        check("the bus announced the delete", any(e.get("type") == "chat.deleted" and e.get("conversation_id") == cid for e in events))
    print("ALL PASS")


async def until(http: httpx.AsyncClient, cid: str, pid: str, states: set, seconds: float = 30) -> dict:
    """Poll the conversation until proposal ``pid`` is in one of ``states``."""
    deadline = asyncio.get_event_loop().time() + seconds
    while True:
        conv = (await http.get(f"/api/chat/{cid}")).json()
        found = [p for m in conv["messages"] for p in m.get("proposals") or [] if p["id"] == pid]
        if found and found[0]["state"] in states:
            return found[0]
        if asyncio.get_event_loop().time() > deadline:
            raise SystemExit(f"FAIL proposal {pid} never reached {states}: {found}")
        await asyncio.sleep(0.2)


async def proposals(http: httpx.AsyncClient, events: list) -> None:
    """1.2-M4: proposal → estimate → accept → work on the wall and the message; decline; two; auto-decline; blind."""
    texts = (await http.get("/api/models", params={"modality": "text"})).json()["models"]["text"]
    check("every text model in the list carries a tools boolean", all(isinstance((m.get("capabilities") or {}).get("tools"), bool) for m in texts),
          [m["id"] for m in texts if not isinstance((m.get("capabilities") or {}).get("tools"), bool)])
    echo_tools = {m["id"]: m["capabilities"]["tools"] for m in texts if m["provider"] == "echo"}
    check("the sandbox has echo models with tools and a blind one without", echo_tools == {"echo": True, "echo-fast": True, "echo-blind": False}, echo_tools)

    gen_tools = "generate_image,edit_image,generate_speech"
    ledger_before = {c["id"] for c in (await http.get("/api/calls", params={"tool": gen_tools, "limit": 500})).json()}
    gens_before = {g["id"] for g in (await http.get("/api/generations", params={"limit": 200})).json()}

    conv = (await http.post("/api/chat", json={"model": "echo-fast"})).json()
    cid = conv["id"]
    turn = (await http.post(f"/api/chat/{cid}/messages", params={"wait": "true"}, json={"text": "幫我畫一隻戴耳機的貓"})).json()
    check("a turn on a model with tools says so", turn.get("tools") is True, turn.get("tools"))
    props = turn["message"].get("proposals") or []
    check("asking for a picture brings one pending proposal", len(props) == 1 and props[0]["state"] == "pending" and props[0]["kind"] == "image", props)
    p = props[0]
    opts = (await http.get("/api/generate/options")).json()["kinds"]["image"]
    usable = {m["id"] for m in opts["models"] if m["available"]}
    check("it recommends one of the generate page's models (the sandbox default)", p["model"] in usable and p["model"] == opts["default_model"], (p["model"], opts["default_model"]))
    got = (await http.get(f"/api/chat/{cid}")).json()
    check("the conversation is open again, not running", got["status"] == "open", got["status"])
    ledger_now = {c["id"] for c in (await http.get("/api/calls", params={"tool": gen_tools, "limit": 500})).json()}
    gens_now = {g["id"] for g in (await http.get("/api/generations", params={"limit": 200})).json()}
    check("before pressing 生成 nothing is generated or billed", ledger_now == ledger_before and gens_now == gens_before)
    est = await http.post("/api/generate/estimate", json={"kind": "image", "params": {"prompt": p["prompt"], "model": p["model"]}})
    check("the card's estimate answers (sandbox: free)", est.status_code == 200 and est.json()["basis"] == "sandbox", est.text)

    acc = await http.post(f"/api/chat/{cid}/proposals/{p['id']}/accept", json={"prompt": "一隻戴著耳機的橘貓"})
    check("accept starts generating", acc.status_code == 200 and acc.json()["state"] == "generating" and acc.json()["generation_id"], acc.text)
    again = await http.post(f"/api/chat/{cid}/proposals/{p['id']}/accept", json={})
    check("pressing 生成 twice is 409", again.status_code == 409, again.status_code)
    done = await until(http, cid, p["id"], {"done", "failed"})
    check("the proposal ends done with a work", done["state"] == "done" and done.get("artifact", {}).get("id"), done)
    art = done["artifact"]
    check("the work's file opens from the message", (await http.get(art["file_url"])).status_code == 200 and art["thumb_url"])
    wall = (await http.get("/api/artifacts", params={"source": "chat", "limit": 50})).json()
    wall_items = wall.get("items", wall) if isinstance(wall, dict) else wall
    row = next((a for a in wall_items if a["id"] == art["id"]), None)
    check("the work is on the wall with source chat", row is not None and row["source"] == "chat", (row or {}).get("source"))
    gen = (await http.get(f"/api/generations/{acc.json()['generation_id']}")).json()
    check("the generation job is chat-sourced and names the conversation", gen["source"] == "chat" and gen["meta"]["conversation_id"] == cid, gen.get("meta"))
    check("the edited prompt was used", gen["params"].get("prompt") == "一隻戴著耳機的橘貓", gen["params"])
    got = (await http.get(f"/api/chat/{cid}")).json()
    check("result messages are not shown or counted", [m["role"] for m in got["messages"]] == ["user", "assistant"] and got["n_messages"] == 2,
          ([m["role"] for m in got["messages"]], got["n_messages"]))
    listed = next(c for c in (await http.get("/api/chat")).json() if c["id"] == cid)
    check("…in the list either", listed["n_messages"] == 2, listed["n_messages"])
    nxt = (await http.post(f"/api/chat/{cid}/messages", params={"wait": "true"}, json={"text": "畫好了嗎"})).json()
    check("the next turn knows the work was made", "做好了" in nxt["message"]["content"] and art["id"] in nxt["message"]["content"], nxt["message"]["content"][:200])
    md = (await http.get(f"/api/chat/{cid}/export", params={"download": "false"})).text
    check("the export marks the proposal and its work", "> 提議生成圖片" in md and f"作品 `{art['id']}`" in md, md[:600])

    # ---- the live reply's place among versions (1.2-M3 left this for M4)
    first_reply = got["messages"][1]["id"]
    rg = (await http.post(f"/api/chat/{cid}/messages/{first_reply}/regenerate", json={"model": "echo"})).json()
    live = (await http.get(f"/api/chat/{cid}")).json().get("live") or {}
    check("a reply being regenerated shows n/n while it streams", live.get("versions", {}).get("count") == 2
          and live["versions"]["ids"][-1] == f"live-{rg['turn_id']}", live.get("versions"))
    await http.post(f"/api/chat/{cid}/cancel")

    # ---- decline, twice is 409; reopening it generates after all
    q = (await http.post("/api/chat", json={"model": "echo-fast"})).json()["id"]
    pq = (await http.post(f"/api/chat/{q}/messages", params={"wait": "true"}, json={"text": "畫一座山"})).json()["message"]["proposals"][0]
    dec = await http.post(f"/api/chat/{q}/proposals/{pq['id']}/decline")
    check("decline marks it declined", dec.status_code == 200 and dec.json()["state"] == "declined" and not dec.json().get("auto"), dec.text)
    check("declining again is 409", (await http.post(f"/api/chat/{q}/proposals/{pq['id']}/decline")).status_code == 409)
    reopened = await http.post(f"/api/chat/{q}/proposals/{pq['id']}/accept", json={})
    check("a declined one may be reopened and accepted", reopened.status_code == 200 and reopened.json()["state"] == "generating", reopened.text)
    check("…and it ends done", (await until(http, q, pq["id"], {"done", "failed"}))["state"] == "done")

    # ---- two proposals, then sending on declines both
    two = (await http.post(f"/api/chat/{q}/messages", params={"wait": "true"}, json={"text": "畫兩張不同的海"})).json()["message"]
    check("two proposals in one reply", [x["state"] for x in two.get("proposals") or []] == ["pending", "pending"], two.get("proposals"))
    on = (await http.post(f"/api/chat/{q}/messages", params={"wait": "true"}, json={"text": "先不要了，聊別的"})).json()
    after = (await http.get(f"/api/chat/{q}")).json()
    settled = [x for m in after["messages"] if m["id"] == two["id"] for x in m["proposals"]]
    check("sending on declines waiting proposals automatically", [(x["state"], x.get("auto")) for x in settled] == [("declined", True)] * 2, settled)
    check("…and the model is told", "沒有回應這個提議" in on["message"]["content"], on["message"]["content"][:200])

    # ---- a model that cannot use tools proposes nothing
    blind = (await http.post("/api/chat", json={"model": "echo-blind"})).json()["id"]
    bt = (await http.post(f"/api/chat/{blind}/messages", params={"wait": "true"}, json={"text": "幫我畫一隻貓"})).json()
    check("the blind model gets no tools and proposes nothing", bt["tools"] is False and bt["message"]["proposals"] == [], (bt["tools"], bt["message"].get("proposals")))

    await asyncio.sleep(0.3)
    seen = [(e["proposal"]["state"], e["action"]) for e in events if e.get("type") == "chat.proposal" and e.get("conversation_id") == cid]
    check("the bus announced the proposal and each state change", seen[:3] == [("pending", "created"), ("generating", "updated"), ("done", "updated")], seen)


def pdf(pages: list[str]) -> bytes:
    """A small valid PDF, one line of text per page."""
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            f"<< /Type /Pages /Kids [{' '.join(f'{4 + 2 * i} 0 R' for i in range(len(pages)))}] /Count {len(pages)} >>".encode(),
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    for i, text in enumerate(pages):
        content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>".encode())
        objs.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
    out, offsets = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for k, body in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(f"{k} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets))
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


async def wait_reply(http: httpx.AsyncClient, cid: str, mid: str, seconds: float = 30) -> dict:
    """Poll until message ``mid`` has finished (a reply that waited for the owner)."""
    deadline = asyncio.get_event_loop().time() + seconds
    while True:
        conv = (await http.get(f"/api/chat/{cid}")).json()
        m = next((x for x in conv["messages"] if x["id"] == mid), None)
        if m and conv.get("live") is None and (m.get("meta") or {}).get("state") in ("done", "error", "cancelled"):
            return m
        if asyncio.get_event_loop().time() > deadline:
            raise SystemExit(f"FAIL reply {mid} never finished: {(m or {}).get('meta')}")
        await asyncio.sleep(0.2)


async def files(http: httpx.AsyncClient, events: list) -> None:
    """1.2-M5: any file; PDF + csv asked about; a big file read with the tools; an unreadable file; audio asked about first."""
    limits = (await http.get("/api/uploads/limits")).json()
    check("the upload limits are readable (50 MB files, 10 per message)", limits["max_bytes"]["file"] == 50 * 1024 * 1024 and limits["max_attachments"] == 10, limits)
    texts = (await http.get("/api/models", params={"modality": "text"})).json()["models"]["text"]
    check("every text model says whether it takes PDF and audio", all(isinstance(m["capabilities"].get("pdf"), bool) and isinstance(m["capabilities"].get("audio"), bool) for m in texts),
          [m["id"] for m in texts if not isinstance(m["capabilities"].get("pdf"), bool)])
    check("the generate page still gets images and audio only", (await http.post("/api/uploads", params={"filename": "x.exe"}, content=b"MZ")).status_code == 415)

    up = lambda name, data: http.post("/api/uploads", params={"filename": name, "purpose": "chat"}, content=data)  # noqa: E731
    p = (await up("新品企劃.pdf", pdf(["Launch plan: three cup sleeves", "Budget 120k"]))).json()
    c = (await up("試賣.csv", "日期,門市,杯數\n9/1,信義,120\n9/2,信義,98\n".encode("utf-8"))).json()
    check("the upload says what the files hold", p["kind"] == "file" and p["info"]["pages"] == 2 and c["info"]["rows"] == 3 and c["info"]["cols"] == 3, (p["info"], c["info"]))
    conv = (await http.post("/api/chat", json={"model": "echo-fast"})).json()
    cid = conv["id"]
    t = (await http.post(f"/api/chat/{cid}/messages", params={"wait": "true"},
                         json={"text": "這兩份在講什麼", "attachments": [{"upload_id": p["id"]}, {"upload_id": c["id"]}]})).json()
    m = t["message"]
    check("PDF + csv: the PDF went as it is, the csv as text", [(e["name"], e["mode"]) for e in m["meta"]["files"]] == [("新品企劃.pdf", "native"), ("試賣.csv", "text")], m["meta"].get("files"))
    check("…and the reply speaks of their content", "9/1,信義,120" in m["content"] and "新品企劃.pdf" in m["content"], m["content"][:400])
    blind = (await http.post("/api/chat", json={"model": "echo-blind"})).json()["id"]
    bt = (await http.post(f"/api/chat/{blind}/messages", params={"wait": "true"}, json={"text": "這份在講什麼", "attachments": [{"upload_id": p["id"]}]})).json()
    check("a model without PDF input gets the PDF's text", bt["message"]["meta"]["files"][0]["mode"] == "text" and "Launch plan" in bt["message"]["content"], bt["message"]["meta"]["files"])

    big = (await up("門市紀錄.txt", ("開頭第一句。" + "紀錄內容" * 30_000).encode("utf-8"))).json()
    rt = (await http.post(f"/api/chat/{cid}/messages", params={"wait": "true"}, json={"text": "請讀這份紀錄", "attachments": [{"upload_id": big["id"]}]})).json()
    rm = rt["message"]
    files_meta = {e["name"]: e["mode"] for e in rm["meta"]["files"]}
    check("a big file goes as its opening", files_meta.get("門市紀錄.txt") == "excerpt", rm["meta"]["files"])
    check("…and the model read on with read_file (one more round, one ledger row)", rm["meta"].get("tool_rounds") == 1 and rm["meta"]["reads"][0]["tool"] == "read_file"
          and "read_file 讀到" in rm["content"], {k: rm["meta"].get(k) for k in ("tool_rounds", "reads", "model_calls")})
    await asyncio.sleep(0.3)
    reading = [e for e in events if e.get("type") == "chat.tool" and e.get("conversation_id") == cid]
    check("the bus said what was being read", [e["state"] for e in reading][:2] == ["running", "done"], reading[:2])

    junk = (await up("杯型_v2.skp", bytes(range(256)) * 16)).json()
    check("an unknown binary is taken and marked unreadable at once", junk["kind"] == "file" and junk["info"]["readable"] is False, junk["info"])
    jt = (await http.post(f"/api/chat/{cid}/messages", params={"wait": "true"}, json={"text": "這是什麼", "attachments": [{"upload_id": junk["id"]}]})).json()
    jf = [e for e in jt["message"]["meta"]["files"] if e["name"] == "杯型_v2.skp"][0]
    check("the model is told it cannot be read, and the reply says so", jf["mode"] == "unreadable" and "讀不了" in jt["message"]["content"], jf)

    # ---- audio the model cannot hear: asked first
    au = (await up("店長訪談.m4a", b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 2048)).json()
    check("an audio upload is audio", au["kind"] == "audio", au)
    a_conv = (await http.post("/api/chat", json={"model": "echo-fast"})).json()["id"]
    at = (await http.post(f"/api/chat/{a_conv}/messages", json={"text": "訪談重點是什麼", "attachments": [{"upload_id": au["id"]}]})).json()
    check("audio the model cannot hear: the turn waits for the owner", at["state"] == "awaiting" and at["message"]["proposals"][0]["kind"] == "transcript", at.get("state"))
    q = at["message"]["proposals"][0]
    got = (await http.get(f"/api/chat/{a_conv}")).json()
    check("GET shows the question on the waiting reply", got["messages"][-1]["meta"]["state"] == "awaiting" and got["messages"][-1]["proposals"][0]["state"] == "pending"
          and got["live"] is None, got["messages"][-1].get("meta"))
    est = await http.post("/api/generate/estimate", json={"kind": "transcript", "params": {"model": q["model"]}, "duration_s": 60})
    check("the card's estimate answers", est.status_code == 200, est.text)
    acc = await http.post(f"/api/chat/{a_conv}/proposals/{q['id']}/accept", json={})
    check("轉錄後回覆 starts the transcription", acc.status_code == 200 and acc.json()["state"] == "generating", acc.text)
    reply = await wait_reply(http, a_conv, at["message"]["id"])
    check("the reply then streams into the same message, from the transcript", reply["meta"]["state"] == "done" and reply["meta"]["files"][0].get("transcribed")
          and "示範逐字稿" in reply["content"] and reply["proposals"][0]["state"] == "done", reply["meta"].get("files"))
    later = (await http.post(f"/api/chat/{a_conv}/messages", params={"wait": "true"}, json={"text": "請讀錄音的逐字稿"})).json()
    check("read_file reads the transcript afterwards", later["message"]["meta"].get("reads") and "示範逐字稿" in later["message"]["content"], later["message"]["meta"].get("reads"))
    d_conv = (await http.post("/api/chat", json={"model": "echo-fast"})).json()["id"]
    au2 = (await up("另一段.m4a", b"\x00\x00\x00\x18ftypM4A " + b"\x01" * 512)).json()
    dt = (await http.post(f"/api/chat/{d_conv}/messages", json={"text": "這段呢", "attachments": [{"upload_id": au2["id"]}]})).json()
    await http.post(f"/api/chat/{d_conv}/proposals/{dt['message']['proposals'][0]['id']}/decline")
    dr = await wait_reply(http, d_conv, dt["message"]["id"])
    check("不轉錄，直接回覆: the model is told it cannot hear", dr["meta"]["files"][0]["reason"] == "audio_unheard" and "聽不到" in dr["content"], dr["meta"]["files"])
    md = (await http.get(f"/api/chat/{cid}/export", params={"download": "false"})).text
    check("the export marks files, how they went and what was read", "> 附件 2 個：" in md and "新品企劃.pdf：原樣送出" in md and "> 讀過" in md, md[:800])
    gone = (await http.delete(f"/api/chat/{cid}")).json()
    check("deleting the chat leaves the uploaded files", gone["deleted"] and (await http.get(p["file_url"])).status_code == 200)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "seed":
        seed(Path(sys.argv[2]))
        raise SystemExit(0)
    if len(sys.argv) > 1:
        PORT = int(sys.argv[1])
        BASE = f"http://127.0.0.1:{PORT}"
    asyncio.run(main())
