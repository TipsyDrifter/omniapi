"""Popularity (Artificial Analysis ranks, ``catalog/popularity.json``): the file
is sound, its ids reach real models, model rows carry ``rank`` and the badge
never shows a guessed mapping."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from omniapi_mcp.catalog import openrouter_images as ORI
from omniapi_mcp.catalog import openrouter_videos as ORV
from omniapi_mcp.catalog.catalog import OR_IMAGES, OR_VIDEOS, ModelCatalog
from omniapi_mcp.catalog.discovery import DiscoveryResult
from omniapi_mcp.catalog.popularity import BOARDS, Popularity, norm_id, popularity, validate

MCP = Path(__file__).resolve().parents[2]
FIX = MCP / "tests" / "fixtures" / "openrouter"
SNAPSHOT = MCP.parent / "docs" / "research" / "2026-10-06-artificial-analysis排行榜快照.json"


def _fixture_ids() -> set[str]:
    """Every model id the saved OpenRouter listings (text, image, video) know."""
    ids = {m["id"] for m in json.loads((FIX / "text_models.json").read_text(encoding="utf-8"))["data"]}
    ids |= {m["id"] for m in json.loads((FIX / "images_models.json").read_text(encoding="utf-8"))["data"]}
    ids |= {m["id"] for m in json.loads((FIX / "videos_models.json").read_text(encoding="utf-8"))["data"]}
    return ids


@pytest.fixture
def cat() -> ModelCatalog:
    """The catalog with the saved OpenRouter image and video rosters merged in."""
    c = ModelCatalog()
    images = ORI.from_listing(json.loads((FIX / "images_models.json").read_text(encoding="utf-8")),
                              json.loads((FIX / "images_endpoints.json").read_text(encoding="utf-8")))
    c._merge_images(DiscoveryResult(provider=OR_IMAGES, models=images, fetched_at=time.time()))
    c._merge_videos(DiscoveryResult(provider=OR_VIDEOS, models=ORV.from_listing(json.loads((FIX / "videos_models.json").read_text(encoding="utf-8"))), fetched_at=time.time()))
    return c


# ---------------------------------------------------------------- the file
def test_the_file_is_sound_and_names_curated_or_roster_ids():
    c = ModelCatalog()
    assert [p for p in c.problems if p.startswith("popularity")] == []
    assert set(popularity.doc["boards"]) == set(BOARDS)


def test_validate_catches_what_would_mislead():
    doc = {"boards": {
        "text": {"modality": "text", "rows": [
            {"rank": 2, "ids": [{"id": "no-such-model", "confidence": "sure"}]},
            {"rank": 1, "ids": [{"id": "Anthropic/Claude Opus", "confidence": "maybe"}]},
        ]},
        "chess": {"modality": "text", "rows": []},
    }}
    problems = validate(doc, {"text": {"claude-opus-5-5"}})
    assert any("not a curated text model" in p for p in problems)
    assert any("not a whole number above 2" in p for p in problems)
    assert any("not a vendor/name roster id" in p for p in problems)
    assert any("confidence 'maybe'" in p for p in problems)
    assert any("unknown board" in p for p in problems)


def test_every_id_reaches_a_model_we_know(cat):
    """Each sure / likely id of popularity.json is a catalog model or one of the
    saved OpenRouter listings. A miss is printed, not failed: a roster moves."""
    known = {e.id for e in cat._entries.values()} | _fixture_ids()
    known_n = {norm_id(i) for i in known}
    misses = []
    for key, b in popularity.doc["boards"].items():
        for r in b["rows"]:
            for i in r["ids"]:
                if i["confidence"] != "guess" and norm_id(i["id"]) not in known_n:
                    misses.append(f"{key} #{r['rank']} {i['id']}")
    if misses:
        print("popularity ids not found in the catalog or the saved listings:", *misses, sep="\n  ")
    # the leaders of each board are not allowed to be among the misses
    for key, b in popularity.doc["boards"].items():
        top = b["rows"][0]
        assert any(norm_id(i["id"]) in known_n for i in top["ids"]), key


@pytest.mark.skipif(not SNAPSHOT.exists(), reason="research snapshot not in this checkout")
def test_every_sure_id_of_the_snapshot_reaches_a_model(cat):
    """The research snapshot's own mapping, the ``確定`` ones: printed when missing."""
    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    known_n = {norm_id(i) for i in {e.id for e in cat._entries.values()} | _fixture_ids()}
    misses, n = [], 0
    for key, b in snap["boards"].items():
        rows = b.get("rows_by_model_family") or b.get("rows") or []
        rows = rows + ((b.get("also") or {}).get("instrumental") or {}).get("rows", [])
        for r in rows:
            for det in r.get("id_details") or []:
                if det.get("confidence") == "確定":
                    n += 1
                    if norm_id(det["id"]) not in known_n:
                        misses.append(f"{key} #{r['rank']} {det['id']}")
    print(f"{n - len(misses)}/{n} sure ids found", *misses, sep="\n  ")
    assert n and len(misses) < n


@pytest.mark.skipif(not SNAPSHOT.exists(), reason="research snapshot not in this checkout")
def test_the_file_is_what_the_script_makes():
    from scripts.refresh_popularity import build

    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    or_ids = [m["id"] for m in json.loads((FIX / "text_models.json").read_text(encoding="utf-8"))["data"]]
    doc, _ = build(snap, or_ids)
    assert doc == popularity.doc


def test_text_openrouter_ids_are_real_ones_not_the_naming_rule():
    text = {i["id"] for r in popularity.doc["boards"]["text"]["rows"] for i in r["ids"]}
    assert "anthropic/claude-opus-5.5" in text and "openai/gpt-6.1-sol" in text and "google/gemini-3.8-flash" in text
    assert "anthropic/claude-opus-5-5" not in text
    roster = {m["id"] for m in json.loads((FIX / "text_models.json").read_text(encoding="utf-8"))["data"]}
    assert {i for i in text if "/" in i} <= roster


# ---------------------------------------------------------------- matching
@pytest.mark.parametrize("a, b", [
    ("claude-opus-5-5", "claude-opus-5.5"),
    ("anthropic/claude-opus-5.5:batch", "anthropic/claude-opus-5-5"),
    ("~Anthropic/Claude-Opus-5.5", "anthropic/claude-opus-5-5"),
])
def test_spellings_of_one_model_meet(a, b):
    assert norm_id(a) == norm_id(b)


def test_a_pro_variant_is_another_model():
    assert popularity.lookup("openai/gpt-6.1-sol-pro", "text") is None
    assert popularity.lookup("openai/gpt-6.1-sol:batch", "text")["rank"] == 11


def test_the_board_is_the_modalitys_own():
    # the same id on a text board says nothing about an image list
    assert popularity.lookup("claude-opus-5-5", "image") is None
    assert popularity.lookup("gpt-image-2.5-sunburst", "image")["popularity"] == {"image_t2i": 1, "image_edit": 1}


def test_a_guess_orders_but_never_shows_a_badge(tmp_path):
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"boards": {
        "video_t2v": {"modality": "video", "rows": [
            {"rank": 1, "score": 1200, "ids": [{"id": "fal/h3-max", "confidence": "guess"}]},
            {"rank": 4, "score": 1100, "ids": [{"id": "acme/v", "confidence": "guess"}]},
            {"rank": 6, "score": 1000, "ids": [{"id": "acme/v", "confidence": "likely"}]},
        ]},
        "video_i2v": {"modality": "video", "rows": [{"rank": 2, "score": 1150, "ids": [{"id": "acme/v", "confidence": "sure"}]}]},
    }}), encoding="utf-8")
    pop = Popularity(p)
    assert pop.lookup("fal/h3-max", "video") == {"popularity": {"video_t2v": 1}, "rank": 1}
    v = pop.lookup("acme/v", "video")
    assert v["popularity"] == {"video_t2v": 4, "video_i2v": 2} and v["rank"] == 2
    assert v["rank_badge"] == {"board": "video_i2v", "label": BOARDS["video_i2v"]["label"], "rank": 2, "score": 1150}


def test_a_missing_file_costs_only_the_order(tmp_path):
    pop = Popularity(tmp_path / "none.json")
    assert pop.lookup("claude-opus-5-5", "text") is None


# ---------------------------------------------------------------- model rows
def test_model_rows_carry_rank_and_badge(cat):
    opus = cat.get("claude-opus-5-5").to_dict()
    assert opus["rank"] == 1 and opus["popularity"] == {"text": 1}
    assert opus["rank_badge"]["label"] == BOARDS["text"]["label"] and opus["rank_badge"]["score"] == 57.62
    old = cat.get("gpt-4o").to_dict()
    assert not {"rank", "popularity", "rank_badge"} & set(old)
    grok = cat.openrouter_image("x-ai/grok-imagine-image-2.0").to_dict()
    assert grok["rank"] == 4 and grok["rank_badge"]["board"] == "image_t2i"
    wan = cat.openrouter_video("alibaba/wan-3.0").to_dict()
    assert wan["rank"] == 1 and wan["popularity"]["video_t2v"] == 1


def test_the_service_does_not_reorder_lists(cat):
    """Sorting is the page's; the catalog keeps its own order."""
    rows = cat.models(modality="text")
    assert [e.id for e in rows] == [e.id for e in sorted(rows, key=lambda e: (e.modality, e.provider, e.id))]
