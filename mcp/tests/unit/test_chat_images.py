"""1.2-M2 聊天附件 (backend): images reach the vendors in their own shape,
sized down before sending; the catalog ``vision`` flag decides who gets them
(決策記錄 1.2-M2-a); a text-only model answers anyway and says how many it
did not see. No vendor is called: every provider's HTTP goes to a mock
transport that records the request body."""

from __future__ import annotations

import base64
import io
import json
import random
import time
from types import SimpleNamespace as NS

import httpx
import pytest
from PIL import Image

# shared fixtures (tests/unit is on sys.path under pytest's default import mode)
from test_chat_branches import (  # noqa: F401
    _png,
    _settings_without_providers,
    chat,
    daemon,
    dev,
    images,
    store,
)

from omniapi_mcp.bus import EventBus
from omniapi_mcp.capabilities.text import AnthropicTextProvider
from omniapi_mcp.catalog import catalog
from omniapi_mcp.catalog.catalog import ModelCatalog, ModelEntry, discovered_vision
from omniapi_mcp.catalog.discovery import DiscoveredModel, DiscoveryResult
from omniapi_mcp.chat import ChatManager
from omniapi_mcp.chat import images as imgmod
from omniapi_mcp.chat import manager as mgrmod
from omniapi_mcp.chat.images import ImageUnreadable, prepare_image
from omniapi_mcp.chat.manager import accepts_images
from omniapi_mcp.tools.text import TextTool


def _image(path, size=(8, 8), fmt="PNG", mode="RGB", color=(200, 30, 30), **save):
    Image.new(mode, size, color if mode != "RGBA" else color + (128,)).save(path, format=fmt, **save)
    return path


def _decode(prepared) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(prepared.data)))


# ================================================================ sizing / converting
def test_an_image_that_already_fits_goes_as_it_is(tmp_path):
    p = _image(tmp_path / "a.png")
    got = prepare_image(p)
    assert got.mime == "image/png" and base64.b64decode(got.data) == p.read_bytes()
    assert got.part() == {"type": "image_url", "image_url": {"url": "data:image/png;base64," + got.data}}
    assert prepare_image(p) is got  # cached by path + mtime + size


def test_a_large_image_is_scaled_to_the_side_limit(tmp_path):
    p = _image(tmp_path / "big.jpg", size=(3000, 1200), fmt="JPEG")
    got = prepare_image(p)
    assert got.mime == "image/jpeg" and max(_decode(got).size) == imgmod.MAX_IMAGE_SIDE == 2000
    assert _decode(got).size == (2000, 800)


def test_formats_vendors_may_not_read_are_converted(tmp_path):
    gif = prepare_image(_image(tmp_path / "a.gif", fmt="GIF", mode="P"))
    assert gif.mime == "image/png" and _decode(gif).format == "PNG"  # first frame as PNG
    bmp = prepare_image(_image(tmp_path / "a.bmp", fmt="BMP"))
    assert bmp.mime == "image/jpeg"
    tif = prepare_image(_image(tmp_path / "a.tiff", fmt="TIFF", mode="RGBA"))
    assert tif.mime == "image/png" and _decode(tif).mode == "RGBA"  # transparency kept


def test_a_rotated_photo_is_turned_upright(tmp_path):
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90° CW to view
    p = tmp_path / "phone.jpg"
    Image.new("RGB", (40, 20), (10, 10, 10)).save(p, format="JPEG", exif=exif.tobytes())
    got = prepare_image(p)
    assert _decode(got).size == (20, 40)


def test_an_image_over_the_byte_limit_is_shrunk_until_it_fits(tmp_path, monkeypatch):
    monkeypatch.setattr(imgmod, "MAX_IMAGE_B64_BYTES", 40_000)
    noise = Image.frombytes("RGB", (600, 600), random.Random(1).randbytes(600 * 600 * 3))
    p = tmp_path / "noise.png"
    noise.save(p, format="PNG")
    assert len(p.read_bytes()) * 4 // 3 > 40_000
    got = prepare_image(p)
    assert got.size <= 40_000 and got.mime == "image/jpeg"


def test_not_an_image_is_unreadable(tmp_path):
    p = tmp_path / "x.png"
    p.write_bytes(b"not an image")
    with pytest.raises(ImageUnreadable):
        prepare_image(p)


# ================================================================ anthropic block
def test_anthropic_gets_its_own_image_block():
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "這是什麼"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJD"}},
        {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
    ]}]
    _, out = AnthropicTextProvider._convert_messages(msgs)
    assert out == [{"role": "user", "content": [
        {"type": "text", "text": "這是什麼"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "QUJD"}},
        {"type": "image", "source": {"type": "url", "url": "https://example.com/a.png"}},
    ]}]


# ================================================================ vision flag
def test_openrouter_input_modalities_map_to_vision():
    assert discovered_vision({"architecture": {"input_modalities": ["text", "image"], "modality": "text+image->text"}}) is True
    assert discovered_vision({"architecture": {"input_modalities": ["text"], "modality": "text->text"}}) is False
    assert discovered_vision({"architecture": {"modality": "text+image->text"}}) is True  # older listings: the string
    assert discovered_vision({"architecture": {"modality": "text->text+image"}}) is False  # image OUT is not vision
    assert discovered_vision({}) is None and discovered_vision({"architecture": "x"}) is None


def test_discovered_models_carry_vision_and_every_text_model_lists_a_boolean(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIAPI_HOME", str(tmp_path / "home"))
    cat = ModelCatalog()
    cat._merge("openrouter", DiscoveryResult(provider="openrouter", fetched_at=time.time(), models=[
        DiscoveredModel(id="acme/seer", extra={"architecture": {"input_modalities": ["text", "image"], "modality": "text+image->text"}}),
        DiscoveredModel(id="acme/plain", extra={"architecture": {"input_modalities": ["text"], "modality": "text->text"}}),
        DiscoveredModel(id="acme/unsaid"),
    ]))
    assert cat.vision("acme/seer", "openrouter") and not cat.vision("acme/plain", "openrouter") and not cat.vision("acme/unsaid", "openrouter")
    texts = cat.snapshot(modality="text")["models"]["text"]
    assert texts and all(isinstance(m["capabilities"].get("vision"), bool) for m in texts)
    by_id = {m["id"]: m for m in texts}
    assert by_id["acme/seer"]["capabilities"]["vision"] is True and by_id["acme/unsaid"]["capabilities"]["vision"] is False
    assert by_id["deepseek-v4-pro"]["capabilities"]["vision"] is False and by_id["deepseek-flash"]["capabilities"]["vision"] is True


def test_the_gate_follows_the_routed_model():
    echo = NS(PROVIDER_KEY="echo")
    assert accepts_images(echo, "echo") and accepts_images(echo, "echo-fast") and not accepts_images(echo, "echo-blind")
    assert accepts_images(NS(PROVIDER_KEY="openai"), "gpt-5.4-mini")
    assert accepts_images(NS(PROVIDER_KEY="anthropic"), "claude-sonnet-5")
    assert accepts_images(NS(PROVIDER_KEY="google"), "gemini-3.8-flash")
    assert not accepts_images(NS(PROVIDER_KEY="deepseek"), "deepseek-v4-pro")
    assert not accepts_images(NS(PROVIDER_KEY="openrouter"), "acme/never-listed")  # unknown → text only
    assert not accepts_images(NS(PROVIDER_KEY="fake"), "gpt-5.4-mini")  # flags belong to the provider that serves it


# ================================================================ the chat turn (echo)
async def test_echo_blind_answers_without_the_images_and_says_so(chat, images):
    conv = await chat.create(model="echo-blind")
    started = await chat.send(conv["id"], "看這張", attachments=[{"upload_id": "up1"}, {"artifact_id": "art1"}])
    assert started["vision"] is False and started["images_skipped"] == 2  # known before the reply starts
    res = await chat.wait(started)
    assert res["state"] == "done" and "附了" not in res["message"]["content"]
    meta = res["message"]["meta"]
    assert meta["vision"] is False and meta["images_skipped"] == 2 and meta["images_sent"] == 0
    # the same images, switched to a model that sees: sent this time
    again = await chat.wait(await chat.regenerate(conv["id"], res["message"]["id"], model="echo-fast"))
    assert again["vision"] is True and again["images_skipped"] == 0
    assert "附了 **2** 張圖" in again["message"]["content"] and again["message"]["meta"]["images_sent"] == 2


async def test_a_turn_without_images_records_nothing_about_them(chat):
    conv = await chat.create(model="echo-blind")
    res = await chat.send_and_wait(conv["id"], "純文字")
    assert res["images_skipped"] == 0 and "images_sent" not in res["message"]["meta"]


class Grab:
    """The ``stream`` of a text tool, recording what it was sent (echo-backed)."""

    def __init__(self, inner):
        self.inner = inner
        self.histories: list = []

    def route(self, model):
        return self.inner.route(model)

    async def stream(self, messages, model=None, **params):
        self.histories.append(messages)
        async for piece in self.inner.stream(messages, model=model, **params):
            yield piece


@pytest.fixture
async def grab(store, dev):
    tool = Grab(TextTool(_settings_without_providers()))
    mgr = ChatManager(store, EventBus(), tool)
    mgr.tool = tool
    yield mgr
    await mgr.close()


async def test_a_vanished_file_is_left_out_with_a_note(grab, images, tmp_path):
    conv = await grab.create(model="echo-fast")
    first = await grab.send_and_wait(conv["id"], "第一張", attachments=[{"upload_id": "up1"}])
    assert first["message"]["meta"]["images_sent"] == 1
    (tmp_path / "img0.png").unlink()  # up1's file is gone
    nxt = await grab.send_and_wait(conv["id"], "還記得嗎", attachments=[{"upload_id": "up2"}])
    assert nxt["state"] == "done"
    meta = nxt["message"]["meta"]
    assert meta["images_missing"] == 1 and meta["images_sent"] == 1
    old_user = grab.tool.histories[-1][0]["content"]
    assert old_user[0] == {"type": "text", "text": "第一張"} and old_user[1] == {"type": "text", "text": "[1 張附圖的檔案已不在，沒有送出]"}
    assert grab.tool.histories[-1][-1]["content"][1]["type"] == "image_url"


async def test_past_the_request_budget_the_older_images_stay_behind(grab, images, monkeypatch):
    one = prepare_image(images.up1["file_path"]).size
    monkeypatch.setattr(mgrmod, "MAX_REQUEST_IMAGE_B64_BYTES", one + one // 2)  # room for one image
    conv = await grab.create(model="echo-fast")
    await grab.send_and_wait(conv["id"], "舊的", attachments=[{"upload_id": "up1"}])
    res = await grab.send_and_wait(conv["id"], "新的", attachments=[{"upload_id": "up2"}])
    assert res["images_skipped"] == 1 and res["message"]["meta"]["images_sent"] == 1
    hist = grab.tool.histories[-1]
    assert hist[0]["content"][-1] == {"type": "text", "text": "[1 張較早的附圖超過單次可送的大小，沒有送出]"}
    assert [p["type"] for p in hist[-1]["content"]] == ["text", "image_url"]


# ================================================================ the vendors' request bodies
def _sse(events: list[str]) -> bytes:
    return "".join(events).encode()


def _openai_stream(model: str) -> bytes:
    chunk = lambda delta, finish=None: "data: " + json.dumps({"id": "c", "object": "chat.completion.chunk", "created": 1, "model": model,  # noqa: E731
                                                                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"
    usage = "data: " + json.dumps({"id": "c", "object": "chat.completion.chunk", "created": 1, "model": model, "choices": [],
                                   "usage": {"prompt_tokens": 900, "completion_tokens": 3, "total_tokens": 903}}) + "\n\n"
    return _sse([chunk({"role": "assistant", "content": "一隻"}), chunk({"content": "貓"}, "stop"), usage, "data: [DONE]\n\n"])


def _anthropic_stream(model: str) -> bytes:
    ev = lambda name, data: f"event: {name}\ndata: {json.dumps(data)}\n\n"  # noqa: E731
    return _sse([
        ev("message_start", {"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant", "model": model, "content": [],
                                                                 "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 900, "output_tokens": 1}}}),
        ev("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ev("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "一隻貓"}}),
        ev("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 3}}),
        ev("message_stop", {"type": "message_stop"}),
    ])


@pytest.fixture
async def vendors(store, monkeypatch):
    """A text tool with all five vendors configured, each on a mock transport."""
    from anthropic import AsyncAnthropic
    from openai import AsyncOpenAI

    monkeypatch.delenv("OMNIAPI_OFFLINE", raising=False)
    on = NS(enabled=True, api_key="test-key", organization=None, base_url=None, timeout=5, max_retries=0)
    tool = TextTool(NS(providers=NS(openai=on, deepseek=on, anthropic=on, gemini=on, openrouter=on)))
    sent: list[dict] = []

    def handler(request, response=httpx.Response, stream=_openai_stream):
        body = json.loads(request.content)
        sent.append({"host": request.url.host, "path": request.url.path, "body": body})
        return response(200, content=stream(body["model"]), headers={"content-type": "text/event-stream"})

    for key, p in tool._by_key.items():
        if key == "anthropic":  # this SDK version runs on httpx2 (same API, its own classes)
            import httpx2

            http2 = httpx2.AsyncClient(transport=httpx2.MockTransport(lambda r: handler(r, httpx2.Response, _anthropic_stream)))
            p.client = AsyncAnthropic(api_key="test-key", base_url="https://api.anthropic.com", http_client=http2, max_retries=0)
        else:
            http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            p.client = AsyncOpenAI(api_key="test-key", base_url=str(p.client.base_url), http_client=http, max_retries=0)
    # two OpenRouter models as discovery would list them
    monkeypatch.setitem(catalog._entries, ("openrouter", "acme/seer"),
                        ModelEntry(id="acme/seer", provider="openrouter", modality="text", status="discovered", capabilities={"vision": True}))
    monkeypatch.setitem(catalog._entries, ("openrouter", "acme/plain"),
                        ModelEntry(id="acme/plain", provider="openrouter", modality="text", status="discovered", capabilities={"vision": False}))
    mgr = ChatManager(store, EventBus(), tool)
    yield NS(chat=mgr, sent=sent)
    await mgr.close()
    await tool.close()


@pytest.mark.parametrize("model,host", [
    ("gpt-5.4-mini", "api.openai.com"),
    ("deepseek-flash", "api.deepseek.com"),
    ("gemini-3.8-flash", "generativelanguage.googleapis.com"),
    ("acme/seer", "openrouter.ai"),
])
async def test_openai_family_gets_base64_image_url_parts(vendors, images, model, host):
    conv = await vendors.chat.create(model=model)
    res = await vendors.chat.send_and_wait(conv["id"], "這張圖裡有什麼", attachments=[{"upload_id": "up1"}])
    assert res["state"] == "done" and res["message"]["content"] == "一隻貓", res
    req = vendors.sent[-1]
    assert req["host"] == host and req["path"].endswith("/chat/completions")
    user = req["body"]["messages"][-1]
    assert user["role"] == "user" and user["content"][0] == {"type": "text", "text": "這張圖裡有什麼"}
    part = user["content"][1]
    assert part["type"] == "image_url" and part["image_url"]["url"].startswith("data:image/png;base64,")
    assert base64.b64decode(part["image_url"]["url"].split(",", 1)[1]) == _png()
    assert res["message"]["usage"]["prompt_tokens"] == 900  # the vendor's own count, images included


async def test_anthropic_gets_a_base64_image_block(vendors, images):
    conv = await vendors.chat.create(model="claude-sonnet-5")
    res = await vendors.chat.send_and_wait(conv["id"], "這張圖裡有什麼", attachments=[{"artifact_id": "art1"}])
    assert res["state"] == "done" and res["message"]["content"] == "一隻貓", res
    req = vendors.sent[-1]
    assert req["host"] == "api.anthropic.com" and req["path"] == "/v1/messages"
    content = req["body"]["messages"][-1]["content"]
    assert content[0] == {"type": "text", "text": "這張圖裡有什麼"}
    assert content[1]["type"] == "image" and content[1]["source"]["type"] == "base64" and content[1]["source"]["media_type"] == "image/png"
    assert base64.b64decode(content[1]["source"]["data"]) == _png()


@pytest.mark.parametrize("model", ["deepseek-v4-pro", "acme/plain"])
async def test_text_only_vendor_models_get_text_and_the_turn_says_so(vendors, images, model):
    conv = await vendors.chat.create(model=model)
    res = await vendors.chat.send_and_wait(conv["id"], "看圖", attachments=[{"upload_id": "up1"}])
    assert res["state"] == "done" and res["vision"] is False and res["images_skipped"] == 1
    assert vendors.sent[-1]["body"]["messages"][-1] == {"role": "user", "content": "看圖"}
    assert "base64" not in json.dumps(vendors.sent[-1]["body"])


# ================================================================ REST
def test_models_list_carries_vision_for_every_text_model(daemon):
    texts = daemon.get("/api/models", params={"modality": "text"}).json()["models"]["text"]
    assert all(isinstance(m["capabilities"]["vision"], bool) for m in texts)
    echo = {m["id"]: m["capabilities"]["vision"] for m in texts if m["provider"] == "echo"}
    assert echo == {"echo": True, "echo-fast": True, "echo-blind": False}


def test_rest_turn_reports_skipped_images(daemon):
    up = daemon.post("/api/uploads", params={"filename": "ref.png"}, content=_png()).json()
    conv = daemon.post("/api/chat", json={"model": "echo-blind"}).json()
    res = daemon.post(f"/api/chat/{conv['id']}/messages", params={"wait": "true"},
                      json={"text": "看圖", "attachments": [{"upload_id": up["id"]}]}).json()
    assert res["state"] == "done" and res["vision"] is False and res["images_skipped"] == 1
    assert res["message"]["meta"]["images_skipped"] == 1
    got = daemon.get(f"/api/chat/{conv['id']}").json()
    assert got["messages"][-1]["meta"]["images_skipped"] == 1
