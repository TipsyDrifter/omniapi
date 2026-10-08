"""OpenRouter's video models (1.4-M3): reading the real listing, which models
make video, what a request costs (every one of the 30 models of the
2026-10-05 listing), what a request may ask for, the vendor's closures, the
catalog rows, and the provider's three calls against a stand-in transport."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest

from omniapi_mcp.capabilities.video import VideoProviderError, VideoRequest
from omniapi_mcp.catalog.catalog import ModelCatalog
from omniapi_mcp.catalog.discovery import DiscoveredModel, DiscoveryResult
from omniapi_mcp.catalog.openrouter_videos import (
    VENDOR_STATUS,
    check_request,
    default_duration,
    entry_fields,
    estimate,
    from_listing,
    makes_video,
    parse_skus,
    price,
    price_class,
    sandbox_listing,
    video_params,
)
from omniapi_mcp.providers.openrouter_videos import OpenRouterVideoProvider

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "openrouter" / "videos_models.json"
RAW = json.loads(FIXTURE.read_text(encoding="utf-8"))
BY_ID = {m["id"]: m for m in RAW["data"]}


def _row(mid: str) -> dict:
    dm = next(d for d in from_listing(RAW) if d.id == mid)
    return entry_fields(dm)


def _canonical(mid: str) -> str:
    """The class of the estimate for the canonical request: the default length,
    the first resolution the model lists, sound as the model does by default, no frame."""
    f = _row(mid)
    vp = f["video_params"]
    est = estimate(f["pricing"], vp, duration=None, resolution=(vp["resolutions"] or [None])[0], audio=None,
                   first_frame=False, frames=0)
    return price_class(est)


# ------------------------------------------------------------------ the listing
def test_the_real_listing_has_thirty_models_and_four_are_not_generation():
    assert len(RAW["data"]) == 30
    left_out = sorted(m["id"] for m in RAW["data"] if not makes_video(m))
    assert left_out == ["black-forest-labs/flux-video-edit", "black-forest-labs/flux-video-upscale", "heygen/avatar-iv",
                        "runway/aleph-2"]


def test_every_listed_sku_key_is_one_we_read():
    """No key of the real listing falls into 'unknown': a new spelling would be logged and priced as 'not computable'."""
    for m in RAW["data"]:
        _rules, unknown = parse_skus(m["pricing_skus"])
        assert unknown == [], (m["id"], unknown)


#: the class of every generation model's estimate for the canonical request (see ``_canonical``)
EXPECTED = {
    "heygen/heygen-video-1": "exact", "minimax/hailuo-3-max": "exact", "alibaba/wan-3.0-prime": "exact",
    "alibaba/wan-3.0": "exact", "bytedance/seedance-2.0-mini": "unknown", "bytedance/seedance-2.5": "unknown",
    "black-forest-labs/flux-3-video": "exact", "minimax/hailuo-3": "exact", "runway/gen-4.5": "exact",
    "x-ai/grok-imagine-video-1.5": "exact", "alibaba/happyhorse-1.1": "exact", "alibaba/happyhorse-1.0": "exact",
    "x-ai/grok-imagine-video": "exact", "kwaivgi/kling-v3.0-pro": "exact", "kwaivgi/kling-v3.0-std": "exact",
    "google/veo-3.1-fast": "exact", "google/veo-3.1-lite": "exact", "kwaivgi/kling-video-o1": "exact",
    "minimax/hailuo-2.3": "exact", "alibaba/wan-2.7": "exact", "bytedance/seedance-2.0": "unknown",
    "bytedance/seedance-2.0-fast": "unknown", "alibaba/wan-2.6": "exact", "bytedance/seedance-1-5-pro": "unknown",
    "openai/sora-2-pro": "exact", "google/veo-3.1": "exact",
}


def test_each_of_the_26_generation_models_lands_in_one_class(capsys):
    got = {m["id"]: _canonical(m["id"]) for m in RAW["data"] if makes_video(m)}
    assert got == EXPECTED
    with capsys.disabled():  # the table the report quotes
        for mid, cls in got.items():
            print(f"  {cls:8} {mid}")


def test_without_a_resolution_most_become_a_range_and_per_token_stays_unknown():
    def cls(mid, **kw):
        f = _row(mid)
        return price_class(estimate(f["pricing"], f["video_params"], duration=None, resolution=None, audio=None,
                                    first_frame=kw.get("first", False), frames=kw.get("frames", 0)))

    assert cls("alibaba/wan-3.0") == "range"
    assert cls("kwaivgi/kling-v3.0-std") == "exact"  # one price at every resolution
    assert cls("runway/gen-4.5") == "exact"
    assert cls("bytedance/seedance-2.5") == "unknown"
    # a frame image: MiniMax lists a reference-image price, whether a frame counts is not documented -> a range
    assert cls("minimax/hailuo-3", first=True, frames=1) == "range"


# ------------------------------------------------------------------ amounts
def _usd(mid: str, **kw) -> float:
    f = _row(mid)
    est = estimate(f["pricing"], f["video_params"], duration=kw.get("duration"), resolution=kw.get("resolution"),
                   audio=kw.get("audio"), first_frame=kw.get("first", False), frames=kw.get("frames", 0))
    assert est["usd"] is not None, est
    return est["usd"]


def test_the_amounts_match_the_measured_bills_and_the_listed_prices():
    # measured 2026-10-05: Grok Imagine Video 480p 1 s with a first frame billed $0.052 ($0.05 + $0.002 for the image)
    assert _usd("x-ai/grok-imagine-video", duration=1, resolution="480p", first=True, frames=1) == pytest.approx(0.052)
    # Wan 3.0 480p 5 s lists $0.25 (billed $0.2125: the estimate is always "about")
    assert _usd("alibaba/wan-3.0", duration=5, resolution="480p") == pytest.approx(0.25)
    assert _usd("alibaba/wan-3.0", duration=5, resolution="1080p") == pytest.approx(1.0)


def test_sound_moves_the_price_when_the_listing_says_so():
    assert _usd("kwaivgi/kling-v3.0-std", duration=5, resolution="720p", audio=True) == pytest.approx(0.63)
    assert _usd("kwaivgi/kling-v3.0-std", duration=5, resolution="720p", audio=False) == pytest.approx(0.42)
    # Veo 3.1 Fast: a sound / no sound line per resolution, the plain one for 1080p
    assert _usd("google/veo-3.1-fast", duration=4, resolution="720p", audio=True) == pytest.approx(0.40)
    assert _usd("google/veo-3.1-fast", duration=4, resolution="720p", audio=False) == pytest.approx(0.32)
    assert _usd("google/veo-3.1-fast", duration=4, resolution="1080p", audio=True) == pytest.approx(0.48)
    assert _usd("google/veo-3.1-fast", duration=8, resolution="4K", audio=False) == pytest.approx(2.0)


def test_image_to_video_has_its_own_price_where_listed():
    assert _usd("alibaba/wan-2.6", duration=5, resolution="720p") == pytest.approx(0.40)
    assert _usd("alibaba/wan-2.6", duration=5, resolution="720p", first=True, frames=1) == pytest.approx(0.50)


def test_cents_minimums_and_the_docs_spelling():
    assert price({"cents_per_second_output": "28", "minimum_cents_per_generation": "56"}, seconds=1)["usd"] == pytest.approx(0.56)
    assert price({"cents_per_second_output": "28", "minimum_cents_per_generation": "56"}, seconds=5)["usd"] == pytest.approx(1.4)
    docs = {"per-video-second": "0.50", "per-video-second-1080p": "0.75"}  # the docs' own example
    assert price(docs, seconds=4, resolution="1080p")["usd"] == pytest.approx(3.0)
    assert price(docs, seconds=4, resolution="720p")["usd"] == pytest.approx(2.0)


def test_an_unknown_key_is_never_guessed_and_is_logged(caplog):
    with caplog.at_level(logging.WARNING):
        got = price({"duration_seconds": "0.1", "per_vibe_unit": "3"}, seconds=5)
    assert got == {"kind": "unknown", "reason": "unknown_sku", "keys": ["per_vibe_unit"]}
    assert any("per_vibe_unit" in r.getMessage() for r in caplog.records)
    assert price({}, seconds=5)["reason"] == "no_price"
    assert price({"video_tokens": "0.000007"}, seconds=5)["reason"] == "per_token"


def test_the_estimate_is_always_approximate():
    f = _row("alibaba/wan-3.0")
    est = estimate(f["pricing"], f["video_params"], duration=5, resolution="480p", audio=None, first_frame=False, frames=0)
    assert est["basis"] == "per_second" and est["approx"] is True and est["per_second"] == pytest.approx(0.05)
    tok = _row("bytedance/seedance-2.5")
    est = estimate(tok["pricing"], tok["video_params"], duration=5, resolution="480p", audio=None, first_frame=False, frames=0)
    assert est["basis"] == "per_token" and est["usd"] is None


# ------------------------------------------------------------------ what a request may ask for
def test_check_request_names_what_the_model_takes():
    vp = video_params(BY_ID["x-ai/grok-imagine-video"])
    assert vp["durations"] == list(range(1, 16)) and vp["frames"] == ["first_frame"] and vp["audio"] is None
    assert default_duration(vp) == 5
    assert "Supported durations: 1-15" in check_request(vp, duration=99)
    assert "480p, 720p" in check_request(vp, duration=5, resolution="4K")
    assert "not a last frame" in check_request(vp, duration=5, first_frame=True, last_frame=True)
    assert check_request(vp, duration=5, resolution="480P", aspect_ratio="1:1", first_frame=True) is None
    gen45 = video_params(BY_ID["runway/gen-4.5"])
    assert "without sound" in check_request(gen45, duration=5, audio=True)
    assert check_request(gen45, duration=5, audio=False) is None
    veo = video_params(BY_ID["google/veo-3.1-lite"])
    assert default_duration(veo) == 4 and "4, 6, 8" in check_request(veo, duration=5)
    sora = video_params(BY_ID["openai/sora-2-pro"])
    assert "no first frame" in check_request(sora, duration=4, first_frame=True)


# ------------------------------------------------------------------ the vendor's closures and the catalog rows
def test_closed_models_carry_the_vendors_status():
    assert _row("openai/sora-2-pro")["status"] == "retired" and not _row("openai/sora-2-pro")["implemented"]
    veo = _row("google/veo-3.1-fast")
    assert (veo["status"], veo["shutdown"], veo["replacement"]) == ("deprecated", "2026-10-22", "gemini-omni-1.1-flash")
    assert veo["implemented"] and "2026-10-22" in veo["note"]
    for mid, vs in VENDOR_STATUS.items():
        assert vs["source"].startswith("https://") and mid in BY_ID


def test_the_catalog_lists_them_as_video_rows(tmp_path):
    cat = ModelCatalog()
    cat._merge_videos(DiscoveryResult(provider="openrouter-videos", models=from_listing(RAW), fetched_at=0))
    listed = {e.id for e in cat.models(modality="video", provider="openrouter")}
    assert len(listed) == 25  # 26 generation models, Sora (retired) hidden by default
    assert "openai/sora-2-pro" in {e.id for e in cat.models(modality="video", include_retired=True)}
    assert "runway/aleph-2" not in listed and cat.openrouter_video("runway/aleph-2").unlisted == "not_generation"
    wan = cat.openrouter_video("alibaba/wan-3.0")
    d = wan.to_dict()
    assert d["vendor_label"] == "Alibaba" and d["video_params"]["frames"] == ["first_frame"] and d["pricing"]["unit"] == "openrouter_video"
    assert cat.get("alibaba/wan-3.0", modality="video") is wan
    # an image roster that does not list the video models leaves them online, and the reverse
    cat._merge_images(DiscoveryResult(provider="openrouter-images", models=[], fetched_at=0, error="x"))
    from omniapi_mcp.catalog.openrouter_images import sandbox_listing as images
    cat._merge_images(DiscoveryResult(provider="openrouter-images", models=images(), fetched_at=0))
    assert wan.online is True


def test_the_sandbox_roster_covers_each_case():
    rows = {dm.id: entry_fields(dm) for dm in sandbox_listing()}
    assert rows["x-ai/grok-imagine-video"]["video_params"]["frames"] == ["first_frame"]
    assert rows["kwaivgi/kling-v3.0-std"]["video_params"]["frames"] == ["first_frame", "last_frame"]
    assert rows["runway/gen-4.5"]["video_params"]["audio"] is False
    assert price_class(estimate(rows["bytedance/seedance-2.0-fast"]["pricing"], rows["bytedance/seedance-2.0-fast"]["video_params"],
                                duration=5, resolution="480p", audio=None, first_frame=False, frames=0)) == "unknown"
    assert rows["google/veo-3.1-lite"]["status"] == "deprecated" and rows["openai/sora-2-pro"]["status"] == "retired"
    assert rows["black-forest-labs/flux-video-edit"]["unlisted"] == "not_generation"
    assert "alibaba/wan-3.0" in rows  # the built-in default video model


def test_sora_and_veo_are_no_longer_ignored_when_a_vendor_lists_them():
    cat = ModelCatalog()
    assert cat.classify("openai", "sora-2") == "video"
    assert cat.classify("google", "veo-3.1-generate-preview") == "video"
    assert cat.classify("google", "gemini-omni-1.1-flash") is None  # ignored by discovery: the curated row is the one
    omni = cat.get("gemini-omni-1.1-flash")
    assert omni and omni.modality == "video" and omni.implemented and omni.video_route == "google"  # 1.4-M5: direct


def test_direct_vendor_video_ids_are_not_listed():
    """1.4-M4: Sora / Veo ids a direct vendor's listing turns up stay known to the
    catalog (classified as video) but are not on the video list or the models page:
    Sora's API is closed and video goes through OpenRouter in this version."""
    cat = ModelCatalog()
    cat._merge_videos(DiscoveryResult(provider="openrouter-videos", models=from_listing(RAW), fetched_at=0))
    found = [DiscoveredModel(id=i, display_name=i) for i in ("sora-2", "sora-2-pro")]
    cat._merge("openai", DiscoveryResult(provider="openai", models=found, fetched_at=0))
    cat._merge("google", DiscoveryResult(provider="google", models=[DiscoveredModel(id="veo-3.1-generate-preview", display_name="Veo")], fetched_at=0))
    listed = {e.id for e in cat.models(modality="video", include_retired=True)}
    assert not listed & {"sora-2", "sora-2-pro", "veo-3.1-generate-preview"}
    assert "gemini-omni-1.1-flash" in listed and "alibaba/wan-3.0" in listed
    assert not {"sora-2", "veo-3.1-generate-preview"} & {m["id"] for m in cat.snapshot(include_retired=True)["models"].get("video", [])}
    # still known when asked for everything
    assert "sora-2" in {e.id for e in cat.models(modality="video", include_unlisted=True)}


# ------------------------------------------------------------------ the provider, against a stand-in transport
def _provider(handler) -> OpenRouterVideoProvider:
    return OpenRouterVideoProvider("fake-openrouter-key", "https://or.test/api/v1", transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_submit_sends_the_frames_inline_and_reads_the_job_id():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"], seen["body"] = str(req.url), json.loads(req.content)
        seen["auth"] = req.headers["authorization"]
        return httpx.Response(202, json={"id": "job-1", "polling_url": "https://or.test/api/v1/videos/job-1", "status": "pending"})

    p = _provider(handler)
    got = await p.submit(VideoRequest(model="x-ai/grok-imagine-video", prompt="a boat", duration=1, resolution="480p",
                                      aspect_ratio="1:1", first_frame="data:image/jpeg;base64,AAAA"))
    await p.close()
    assert got.remote_id == "job-1" and got.status == "pending"
    assert seen["url"] == "https://or.test/api/v1/videos" and seen["auth"].startswith("Bearer ")
    assert seen["body"] == {"model": "x-ai/grok-imagine-video", "prompt": "a boat", "duration": 1, "resolution": "480p",
                            "aspect_ratio": "1:1", "frame_images": [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"},
                                                                     "frame_type": "first_frame"}]}


@pytest.mark.asyncio
async def test_a_400_is_passed_on_as_it_is_and_costs_nothing():
    said = "Duration 99s is not supported for this model. Supported durations: 1, 2, 3"

    def handler(req):
        return httpx.Response(400, json={"error": {"message": said, "metadata": {"failed_routing_step": "Validate Video Parameters"}}})

    p = _provider(handler)
    with pytest.raises(VideoProviderError) as e:
        await p.submit(VideoRequest(model="m", prompt="x", duration=99))
    await p.close()
    assert e.value.said == said and str(e.value).startswith("[openrouter] " + said)
    assert e.value.charged == "no" and e.value.error_kind == "invalid"


@pytest.mark.asyncio
async def test_status_and_download():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/content"):
            assert req.url.params["index"] == "0"
            return httpx.Response(200, content=b"\x00\x00\x00\x18ftypmp42" + b"x" * 100)
        if req.url.path.endswith("/gone"):
            return httpx.Response(404, json={"error": {"message": "Video job gone not found"}})
        return httpx.Response(200, json={"id": "job-1", "status": "completed", "unsigned_urls": ["u"],
                                         "usage": {"cost": 0.2125, "is_byok": False}})

    p = _provider(handler)
    st = await p.status("job-1")
    assert (st.status, st.cost_usd, st.outputs, st.done) == ("completed", 0.2125, 1, True)
    with pytest.raises(VideoProviderError) as e:
        await p.status("gone")
    assert e.value.transient is False and e.value.error_kind == "lost"
    import tempfile

    dest = Path(tempfile.mkdtemp()) / "v.mp4"
    n = await p.download("job-1", dest)
    await p.close()
    assert n == dest.stat().st_size == 112 and not dest.with_name("v.mp4.part").exists()
