"""1.4-M2: OpenRouter's image models — the roster, duplicates, prices, the
provider, edit routing and the pre-send checks.

Fixtures are the real public listing of 2026-10-05
(``tests/fixtures/openrouter/``: ``GET /api/v1/images/models`` and every
model's ``/endpoints``). Nothing here reaches the network.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from types import SimpleNamespace as NS

import httpx
import pytest

from omniapi_mcp.catalog.catalog import OR_IMAGES, ModelCatalog
from omniapi_mcp.catalog.discovery import DiscoveredModel, DiscoveryResult
from omniapi_mcp.catalog import openrouter_images as ORI
from omniapi_mcp.providers.base import ProviderConfig, ProviderError

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "openrouter"
MODELS = json.loads((FIX / "images_models.json").read_text(encoding="utf-8"))
ENDPOINTS = json.loads((FIX / "images_endpoints.json").read_text(encoding="utf-8"))
FAKE_KEY = "test-not-a-real-key"


def _listing() -> list[DiscoveredModel]:
    return ORI.from_listing(MODELS, ENDPOINTS)


def _settings(**keys: bool):
    """Providers with a key exactly where ``keys`` says True."""
    def slot(on: bool):
        return NS(enabled=on, api_key=FAKE_KEY if on else "", base_url=None, timeout=30, max_retries=0, organization=None)
    names = ("openai", "gemini", "deepseek", "anthropic", "openrouter", "elevenlabs", "kie")
    return NS(providers=NS(**{n: slot(keys.get(n, False)) for n in names}))


@pytest.fixture
def cat() -> ModelCatalog:
    c = ModelCatalog()
    c._merge_images(DiscoveryResult(provider=OR_IMAGES, models=_listing(), fetched_at=time.time()))
    return c


# ---------------------------------------------------------------- reading the roster
def test_the_fixture_is_the_whole_roster():
    assert len(MODELS["data"]) == 57 and set(ENDPOINTS) == {m["id"] for m in MODELS["data"]}


def test_request_shape_from_supported_parameters(cat):
    grok = cat.openrouter_image("x-ai/grok-imagine-image-2.0").image_params
    assert grok["resolutions"] == ["1K", "2K"] and grok["qualities"] == ["low", "medium"]
    assert grok["max_references"] == 3 and grok["max_n"] == 1 and "16:9" in grok["aspect_ratios"]
    ming = cat.openrouter_image("inclusionai/ming-image-0.1-design").image_params
    assert ming["max_references"] == 0 and ming["resolutions"] == []
    styles = cat.openrouter_image("recraft/recraft-v4-styles").image_params
    assert styles["min_references"] == 1  # restyles a given image: cannot run without one
    flux = cat.openrouter_image("black-forest-labs/flux-3-image")
    assert flux.vendor == "black-forest-labs" and flux.vendor_label == "Black Forest Labs" and flux.name == "FLUX.3 Image"
    assert flux.capabilities == {"edit": True, "multi_reference": True}


def test_rows_that_cannot_be_used_say_why(cat):
    muse = cat.openrouter_image("meta/muse-image")  # listed, served by nobody
    assert muse.implemented is False and "no provider" in muse.note
    vector = cat.openrouter_image("recraft/recraft-v4.1-vector")  # SVG only
    assert vector.implemented is False and "SVG" in vector.note
    assert cat.openrouter_image("recraft/recraft-v4.1").implemented is True


def test_preview_aliases_are_hidden_and_closed_models_stay_closed(cat):
    prev = cat.openrouter_image("google/gemini-3.1-flash-image-preview")
    assert prev.snapshot is True
    ids = {e.id for e in cat.models(modality="image")}
    assert "google/gemini-3.1-flash-image-preview" not in ids and "google/gemini-3.1-flash-image" in ids
    # Google closed gemini-2.5-flash-image: the curated row says so, the OpenRouter copy follows
    old = cat.openrouter_image("google/gemini-2.5-flash-image")
    assert old.status == "retired" and "google/gemini-2.5-flash-image" not in ids
    # a deprecated OpenAI model keeps its shutdown date on the OpenRouter copy
    gi1 = cat.openrouter_image("openai/gpt-image-1")
    assert gi1.status == "deprecated" and gi1.shutdown == cat.get("gpt-image-1", "openai").shutdown


def test_a_same_named_chat_model_and_image_model_live_side_by_side(cat):
    cat._merge("openrouter", DiscoveryResult(provider="openrouter", fetched_at=time.time(),
                                             models=[DiscoveredModel(id="google/gemini-3.1-flash-image", display_name="chat")]))
    assert cat.get("google/gemini-3.1-flash-image", "openrouter").modality == "text"
    assert cat.get("google/gemini-3.1-flash-image", modality="image").modality == "image"
    assert cat.get("google/gemini-3.1-flash-image").modality == "text"  # without a modality: the chat row wins
    # the chat listing does not take the image rows offline
    assert cat.openrouter_image("x-ai/grok-imagine-image-2.0").online is True


def test_a_model_that_left_the_roster_goes_offline(cat):
    cat._merge_images(DiscoveryResult(provider=OR_IMAGES, fetched_at=time.time(),
                                      models=[m for m in _listing() if m.id != "qwen/qwen-image-3"]))
    assert cat.openrouter_image("qwen/qwen-image-3").online is False


# ---------------------------------------------------------------- duplicates
def test_direct_vendors_hide_their_openrouter_copies_only_with_a_key(cat):
    def listed():
        return {e.id for e in cat.models(modality="image")}

    cat.set_direct_providers(_settings(openrouter=True))
    assert "openai/gpt-image-2" in listed() and "google/gemini-3-pro-image" in listed()
    cat.set_direct_providers(_settings(openrouter=True, openai=True))
    assert "openai/gpt-image-2" not in listed() and "openai/gpt-5-image" not in listed()
    assert "google/gemini-3-pro-image" in listed()  # no Google key: the OpenRouter copy is the way in
    cat.set_direct_providers(_settings(openrouter=True, openai=True, gemini=True))
    assert not any(i.startswith(("openai/", "google/")) for i in listed())
    assert "x-ai/grok-imagine-image-2.0" in listed()
    # hidden, not gone: still callable by id, still in an explicit listing
    assert cat.openrouter_image("openai/gpt-image-2") is not None
    assert "openai/gpt-image-2" in {e.id for e in cat.models(modality="image", include_duplicates=True)}


def test_the_rule_is_one_table():
    assert ORI.direct_twin("openai/gpt-image-2") == "openai" and ORI.direct_twin("google/x") == "google"
    assert ORI.direct_twin("x-ai/grok-imagine-image-2.0") is None


# ---------------------------------------------------------------- prices
def _est(cat, mid, **kw):
    e = cat.openrouter_image(mid)
    args = dict(resolution=None, quality=None, aspect_ratio=None, n=1, refs=0)
    args.update(kw)
    return ORI.estimate(e.pricing, **args)


def test_variant_prices_match_resolution_and_quality(cat):
    assert _est(cat, "x-ai/grok-imagine-image-2.0", resolution="1K", quality="low") == pytest.approx(
        {"basis": "variant", "usd": 0.04, "n": 1, "unit_price": 0.04, "variant": "low_1k", "refs": None,
         "ref_usd": None, "refs_unpriced": None, "stale": None})
    assert _est(cat, "x-ai/grok-imagine-image-2.0", resolution="2K", quality="medium")["usd"] == pytest.approx(0.08)
    # with a source image: + input_image per image
    e = _est(cat, "x-ai/grok-imagine-image-2.0", resolution="1K", quality="low", refs=1)
    assert e["usd"] == pytest.approx(0.05) and e["ref_usd"] == pytest.approx(0.01)
    assert _est(cat, "black-forest-labs/flux-3-image", resolution="768")["usd"] == pytest.approx(0.041)
    assert _est(cat, "black-forest-labs/flux-3-image", resolution="1.5K")["usd"] == pytest.approx(0.07)
    assert _est(cat, "qwen/qwen-image-3-pro", resolution="2K", n=2)["usd"] == pytest.approx(0.15)


def test_an_unmatched_variant_gives_a_range_not_a_guess(cat):
    r = _est(cat, "x-ai/grok-imagine-image-2.0", resolution="1K")  # no quality said: low or medium at 1K
    assert r["basis"] == "range" and r["usd"] is None and (r["low"], r["high"]) == pytest.approx((0.04, 0.06))
    r = _est(cat, "x-ai/grok-imagine-image-2.0")  # nothing said: every line
    assert (r["low"], r["high"]) == pytest.approx((0.04, 0.08))
    # "high_resolution" is not a tier we can name: 1K might or might not be it
    s = _est(cat, "bytedance-seed/seedream-5-0-pro", resolution="2K")
    assert s["basis"] == "range" and (s["low"], s["high"]) == pytest.approx((0.045, 0.09))


def test_the_variantless_line_is_the_base_tier(cat):
    # riverflow v2.5 pro: base 0.13, 2k 0.15, 4k 0.17 -> 1K is the base
    assert _est(cat, "sourceful/riverflow-v2.5-pro", resolution="1K")["usd"] == pytest.approx(0.13)
    assert _est(cat, "sourceful/riverflow-v2.5-pro", resolution="4K")["usd"] == pytest.approx(0.17)
    assert _est(cat, "recraft/recraft-v4.1")["usd"] == pytest.approx(0.035)  # one flat price


def test_other_units(cat):
    mp = _est(cat, "black-forest-labs/flux.2-pro")
    assert mp["basis"] == "per_megapixel" and mp["approx"] is True and mp["usd"] == pytest.approx(0.03 * 1.05)
    assert _est(cat, "microsoft/mai-image-2.6")["basis"] == "per_token"
    assert _est(cat, "krea/krea-2-medium")["basis"] == "no_price"
    styles = _est(cat, "recraft/recraft-v4-styles", refs=2)  # input_reference per request: once
    assert styles["usd"] == pytest.approx(0.035 + 0.005)
    flex = _est(cat, "black-forest-labs/flux.2-flex", refs=1)
    assert flex["refs_unpriced"] is True  # input priced per megapixel: said, not computed


def test_two_providers_pricing_apart_show_a_range(cat):
    r = _est(cat, "google/gemini-3-pro-image-preview")
    assert r["basis"] == "per_token"  # token-priced either way
    lines = [{"billable": "output_image", "unit": "image", "cost_usd": 0.04},
             {"billable": "output_image", "unit": "image", "cost_usd": 0.05}]
    assert ORI.estimate({"lines": lines}, resolution=None, quality=None, aspect_ratio=None, n=1, refs=0)["basis"] == "range"


# ---------------------------------------------------------------- pre-send checks
def test_check_request_says_what_is_wrong(cat):
    ming = cat.openrouter_image("inclusionai/ming-image-0.1-design").image_params
    assert "takes no reference image" in ORI.check_request(ming, refs=1)
    grok = cat.openrouter_image("x-ai/grok-imagine-image-2.0").image_params
    assert "at most 3" in ORI.check_request(grok, refs=4)
    msg = ORI.check_request(grok, refs=0, resolution="4K")
    assert "invalid resolution" in msg and "1K, 2K" in msg
    assert ORI.check_request(grok, refs=0, resolution="2K", aspect_ratio="16:9", quality="low") is None
    styles = cat.openrouter_image("recraft/recraft-v4-styles").image_params
    assert "at least 1" in ORI.check_request(styles, refs=0)


def test_check_tool_args_counts_the_source_images(cat):
    ming = cat.openrouter_image("inclusionai/ming-image-0.1-design")
    assert ORI.check_tool_args(ming, "edit_image", {"image_path": "a.png", "prompt": "x"})
    assert ORI.check_tool_args(ming, "generate_image", {"prompt": "x"}) is None
    grok = cat.openrouter_image("x-ai/grok-imagine-image-2.0")
    four = {"image_path": "a", "additional_image_paths": ["b", "c", "d"]}
    assert "at most 3" in ORI.check_tool_args(grok, "edit_image", four)
    assert ORI.check_tool_args(grok, "generate_image", {"quality": "auto"}) is None  # auto is simply not sent
    assert ORI.check_tool_args(None, "edit_image", four) is None  # not an OpenRouter model: not ours to judge


# ---------------------------------------------------------------- fetching
@pytest.mark.asyncio
async def test_fetch_survives_one_models_endpoints_failing():
    data = {"data": [m for m in MODELS["data"] if m["id"] in ("x-ai/grok-imagine-image-2.0", "qwen/qwen-image-3", "krea/krea-2-medium")]}
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        assert "authorization" not in request.headers  # public endpoints: no key goes out
        if request.url.path == "/api/v1/images/models":
            return httpx.Response(200, json=data)
        if "qwen-image-3" in request.url.path:
            return httpx.Response(503, json={"error": {"message": "down"}})
        mid = request.url.path.split("/api/v1/images/models/", 1)[1].rsplit("/endpoints", 1)[0]
        return httpx.Response(200, json=ENDPOINTS[mid])

    previous = {"qwen/qwen-image-3": {"endpoints": ENDPOINTS["qwen/qwen-image-3"]["endpoints"]}}
    got = await ORI.fetch_openrouter_images(previous=previous, transport=httpx.MockTransport(handler))
    by = {m.id: m for m in got}
    assert len(got) == 3 and len(seen) == 4
    assert by["x-ai/grok-imagine-image-2.0"].extra["endpoints"][0]["pricing"]
    qwen = by["qwen/qwen-image-3"].extra
    assert qwen["pricing_stale"] is True and qwen["endpoints_error"].startswith("HTTPStatusError")
    fields = ORI.entry_fields(by["qwen/qwen-image-3"])
    assert fields["pricing"]["stale"] is True and fields["pricing"]["lines"]
    # without a previous listing the failed one is "price unknown", the roster still stands
    got = await ORI.fetch_openrouter_images(transport=httpx.MockTransport(handler))
    q = ORI.entry_fields({m.id: m for m in got}["qwen/qwen-image-3"])
    assert q["pricing"]["unknown"] is True and q["pricing"]["lines"] == []


@pytest.mark.asyncio
async def test_refresh_lists_the_image_roster_only_with_an_openrouter_key(monkeypatch, tmp_path):
    import sys

    catalog_module = sys.modules["omniapi_mcp.catalog.catalog"]
    monkeypatch.delenv("OMNIAPI_OFFLINE", raising=False)
    monkeypatch.setenv("OMNIAPI_HOME", str(tmp_path))  # caches and key health stay in the test's folder
    calls: list[str] = []

    async def fake_roster(*a, **k):
        calls.append("images")
        return _listing()

    async def fake_text(*a, **k):
        return []

    monkeypatch.setattr("omniapi_mcp.catalog.openrouter_images.fetch_openrouter_images", fake_roster)
    monkeypatch.setattr(catalog_module, "fetch_openai_compatible", fake_text)
    c = ModelCatalog()
    await c.refresh(_settings(), force=True)
    assert calls == [] and c.openrouter_image("x-ai/grok-imagine-image-2.0") is None
    res = await c.refresh(_settings(openrouter=True), force=True)
    assert calls == ["images"] and OR_IMAGES in res and len(res[OR_IMAGES].models) == 57
    assert c.openrouter_image("x-ai/grok-imagine-image-2.0").online is True
    assert (tmp_path / "cache" / "discovery-openrouter-images.json").is_file()  # cached for 24 h like the rest
    await c.refresh(_settings(openrouter=True))
    assert calls == ["images"]  # the cache answered


@pytest.mark.asyncio
async def test_the_sandbox_shows_a_stand_in_roster_without_fetching(monkeypatch):
    monkeypatch.setenv("OMNIAPI_OFFLINE", "1")
    monkeypatch.setenv("OMNIAPI_DEV", "1")
    c = ModelCatalog()
    assert await c.refresh(_settings(openrouter=True), force=True) == {}
    assert c.discovery_status() == {}
    sand = {e.id for e in c.models(modality="image", provider="openrouter")}
    assert {"x-ai/grok-imagine-image-2.0", "inclusionai/ming-image-0.1-design", "black-forest-labs/flux-3-image"} <= sand
    assert c.openrouter_image("inclusionai/ming-image-0.1-design").image_params["max_references"] == 0
    assert c.openrouter_image("x-ai/grok-imagine-image-2.0").image_params["qualities"]


# ---------------------------------------------------------------- the provider
def _provider(cat, handler):
    from omniapi_mcp.providers.openrouter_images import OpenRouterImageProvider

    p = OpenRouterImageProvider(ProviderConfig(api_key=FAKE_KEY, base_url="https://openrouter.ai/api/v1", timeout=30), catalog=cat)
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return p


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


@pytest.mark.asyncio
async def test_generate_sends_only_what_the_model_takes_and_reads_the_real_cost(cat):
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        assert request.url.path == "/api/v1/images" and request.headers["authorization"] == f"Bearer {FAKE_KEY}"
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode(), "media_type": "image/jpeg"}],
                                         "usage": {"cost": 0.04}})

    p = _provider(cat, handler)
    r = await p.generate_image("x-ai/grok-imagine-image-2.0", "a boat", quality="low", size="auto", output_format="png",
                               background="auto", n=1, image_size="1K", aspect_ratio="1:1", seed=7)
    assert sent[0] == {"model": "x-ai/grok-imagine-image-2.0", "prompt": "a boat", "resolution": "1K", "aspect_ratio": "1:1", "quality": "low"}
    assert r.image_data == PNG and r.metadata["cost_usd"] == 0.04 and r.metadata["file_format"] == "jpeg"
    # a model without a resolution / quality knob gets neither; a pixel size becomes its ratio
    await p.generate_image("recraft/recraft-v4.1", "x", quality="high", size="1024x1024", image_size="2K")
    assert sent[1] == {"model": "recraft/recraft-v4.1", "prompt": "x", "aspect_ratio": "1:1"}
    await p.generate_image("qwen/qwen-image-3", "x", image_size="2K", seed=3)
    assert sent[2]["seed"] == 3


@pytest.mark.asyncio
async def test_edit_sends_the_sources_as_references(cat):
    sent: list[dict] = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode(), "media_type": "image/png"}], "usage": {"cost": 0.058}})

    p = _provider(cat, handler)
    src = "data:image/png;base64," + base64.b64encode(PNG).decode()
    r = await p.edit_image("black-forest-labs/flux-3-image", src, "make it night", additional_images=[base64.b64encode(PNG).decode()],
                           image_size="1K")
    refs = sent[0]["input_references"]
    assert len(refs) == 2 and refs[0] == {"type": "image_url", "image_url": {"url": src}}
    assert refs[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert r.metadata["references"] == 2 and r.metadata["cost_usd"] == 0.058


@pytest.mark.asyncio
async def test_a_model_without_references_is_refused_before_anything_is_sent(cat):
    def handler(request):  # pragma: no cover - must not be reached
        raise AssertionError("sent")

    p = _provider(cat, handler)
    with pytest.raises(ProviderError) as e:
        await p.edit_image("inclusionai/ming-image-0.1-design", base64.b64encode(PNG).decode(), "x")
    assert e.value.error_kind == "invalid" and "takes no reference image" in str(e.value)
    with pytest.raises(ProviderError, match="no mask"):
        await p.edit_image("black-forest-labs/flux-3-image", "x", "y", mask_data="m")
    with pytest.raises(ProviderError, match="cannot be used in this version"):
        await p.generate_image("recraft/recraft-v4.1-vector", "x")


@pytest.mark.parametrize(
    "status, body, kind, words",
    [
        (402, {"error": {"code": 402, "message": "Insufficient credits. Add more using https://openrouter.ai/credits"}}, "quota", "not enough credits"),
        (401, {"error": {"code": 401, "message": "No auth credentials found"}}, "auth", "API key"),
        (403, {"error": {"code": 403, "message": "Input was flagged by moderation", "metadata": {"reasons": ["sexual"]}}}, "rejected", "content policy"),
        (400, {"error": {"code": 400, "message": "aspect_ratio 7:3 is not supported"}}, "invalid", "takes: resolution 1K, 2K"),
        (502, {"error": {"code": 502, "message": "Provider returned error", "metadata": {"raw": "safety system rejected the prompt"}}}, "rejected", "refused"),
        (502, {"error": {"code": 502, "message": "Provider returned error"}}, "unavailable", "failed"),
        (429, {"error": {"code": 429, "message": "Rate limit exceeded"}}, "quota", "rate limit"),
    ],
)
@pytest.mark.asyncio
async def test_failures_land_in_our_kinds(cat, status, body, kind, words):
    p = _provider(cat, lambda request: httpx.Response(status, json=body))
    with pytest.raises(ProviderError) as e:
        await p.generate_image("x-ai/grok-imagine-image-2.0", "x", image_size="1K", aspect_ratio="1:1")
    assert e.value.error_kind == kind and words in str(e.value)
    from omniapi_mcp.generate.errors import classify

    assert classify(e.value) == kind


def test_the_job_manager_finds_the_kind_under_the_wrappers():
    from omniapi_mcp.generate.manager import _told_kind

    inner = ProviderError("whatever words", provider_name="openrouter", error_kind="quota")
    try:
        try:
            raise inner
        except ProviderError as e:
            raise RuntimeError("Image generation failed: whatever words") from e
    except RuntimeError as outer:
        try:
            raise Exception("Error executing tool generate_image") from outer
        except Exception as top:
            assert _told_kind(top) == "quota"
    assert _told_kind(RuntimeError("plain")) is None


# ---------------------------------------------------------------- routing
@pytest.mark.asyncio
async def test_the_registry_routes_openrouter_ids_to_the_live_roster(cat):
    from omniapi_mcp.providers.openrouter_images import OpenRouterImageProvider
    from omniapi_mcp.providers.registry import ProviderRegistry

    reg = ProviderRegistry()
    p = OpenRouterImageProvider(ProviderConfig(api_key=FAKE_KEY), catalog=cat)
    await reg.register_provider(p)
    assert reg.get_provider_for_model("x-ai/grok-imagine-image-2.0") is p
    assert reg.get_provider_for_model("recraft/recraft-v4.1-vector") is None  # SVG only: not routed
    assert reg.get_provider_for_model("meta/muse-image") is None
    assert "black-forest-labs/flux-3-image" in reg.get_supported_models()
    assert reg.is_model_supported("qwen/qwen-image-3") and not reg.is_model_supported("nope/nope")
    await p.close()


@pytest.mark.asyncio
async def test_edit_routing_keeps_the_two_vendors_and_adds_openrouter(cat, monkeypatch):
    from omniapi_mcp.tools.image_editing import ImageEditingTool

    openai_p, gemini_p, or_p = object(), object(), NS(name="openrouter")
    registry = NS(get_provider_for_model=lambda m: or_p if m.startswith("x-ai/") else None)

    async def registered():
        return None

    gen = NS(provider_registry=registry, ensure_providers_registered=registered)
    t = ImageEditingTool.__new__(ImageEditingTool)
    t._provider, t._gemini_provider, t._generation_tool = openai_p, gemini_p, gen
    assert t._provider_for("gpt-image-2") is openai_p
    assert t._provider_for("gemini-3.1-flash-image") is gemini_p and t._provider_for("nano-banana-2") is gemini_p
    assert t._provider_for("x-ai/grok-imagine-image-2.0") is or_p
    assert t._provider_for("someone/else") is None  # a vendor/model id is never sent to OpenAI
    assert t._provider_for("gpt-image-9") is openai_p  # unknown plain ids: as before
    t._generation_tool = None
    assert t._provider_for("x-ai/grok-imagine-image-2.0") is None


# ---------------------------------------------------------------- chat proposals
@pytest.mark.asyncio
async def test_chat_menu_offers_openrouter_models_with_a_price(cat, monkeypatch):
    from omniapi_mcp.chat import tools as chat_tools

    rows = [dict(cat.openrouter_image(i).to_dict(), available=True)
            for i in ("x-ai/grok-imagine-image-2.0", "recraft/recraft-v4-styles", "black-forest-labs/flux.2-pro", "microsoft/mai-image-2.6")]

    async def models_for(ctx, kind):
        return rows if kind == "image" else []

    import importlib

    options_module = importlib.import_module("omniapi_mcp.generate.options")
    monkeypatch.setattr(options_module, "models_for", models_for)
    monkeypatch.setattr(options_module, "_default_model", lambda ctx, kind, rows: rows[0]["id"] if rows else None)
    menu = await chat_tools.generation_menu(NS(settings=None))
    ids = [m["id"] for m in menu["image"]["models"]]
    assert "recraft/recraft-v4-styles" not in ids  # needs a reference image: a proposal has none
    text = chat_tools.describe(menu)
    assert "x-ai/grok-imagine-image-2.0（xAI Grok Imagine Image 2.0，經 OpenRouter）：每張 $0.04～$0.08（依解析度、品質）" in text  # the menu: every line
    assert "每百萬像素 $0.03" in text and "依 token 計價" in text


# ---------------------------------------------------------------- the sandbox stand-in
@pytest.mark.asyncio
async def test_the_stand_in_generator_honours_the_listing(monkeypatch, tmp_path):
    from omniapi_mcp.artifacts import fakes
    from omniapi_mcp.catalog import catalog as global_catalog

    c = ModelCatalog()
    c._merge_images(DiscoveryResult(provider=OR_IMAGES, models=_listing(), fetched_at=time.time()))
    monkeypatch.setattr(global_catalog, "openrouter_image", c.openrouter_image)
    saved: list[dict] = []

    async def save_image(image_data, metadata, file_format):
        saved.append(metadata)
        return f"img{len(saved)}", tmp_path / "x.png"

    ctx = NS(settings=NS(storage=NS(base_path=str(tmp_path))), storage_manager=NS(save_image=save_image))
    out = await fakes.fake_generate("generate_image", {"prompt": "p", "model": "x-ai/grok-imagine-image-2.0", "aspect_ratio": "16:9"}, ctx)
    assert out["metadata"]["model"] == "x-ai/grok-imagine-image-2.0" and out["metadata"]["size"] == "1024x576"
    with pytest.raises(ValueError, match="takes no reference image"):
        await fakes.fake_generate("edit_image", {"prompt": "p", "model": "inclusionai/ming-image-0.1-design", "image_path": "a.png"}, ctx)
    plain = await fakes.fake_generate("generate_image", {"prompt": "p", "model": "gpt-image-2"}, ctx)
    assert plain["metadata"]["model"] == "echo-image"  # the direct vendors' stand-in is unchanged
