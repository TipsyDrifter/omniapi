"""The works wall (v1.1, 1.1-M4): filters, facets, lineage, stepping through
the wall, ledger rows that point at their works, and ticketed calls getting
their real cost."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import pytest

from omniapi_mcp.bus import EventBus
from omniapi_mcp.core.job_manager import JobManager
from omniapi_mcp.recorder import extract_call_meta, make_recorded
from omniapi_mcp.store.db import Store


@pytest.fixture
async def store(tmp_path):
    s = Store(tmp_path / "w.db")
    await s.open()
    yield s
    await s.close()


async def _seed(s: Store) -> dict[str, dict]:
    rows = {}
    for name, kw in {
        "img1": dict(kind="image", model="gpt-image-2", source="backfill", prompt="a red cat", created_at=100, call_id="c1"),
        "img2": dict(kind="image", model="gpt-image-2", source="gui", prompt="a blue dog", created_at=200, call_id="c2"),
        "song": dict(kind="music", model=None, source="backfill", created_at=300),
        "lyr": dict(kind="lyrics", model=None, source="backfill", text="la la", created_at=400),
        "talk": dict(kind="speech", model="eleven_v3", source="mcp", prompt="早安", created_at=500, call_id="c2"),
    }.items():
        rows[name] = await s.add_artifact(file_path=f"C:/w/{name}_file.bin", **kw)
    return rows


async def test_wall_filters(store):
    r = await _seed(store)
    ids = lambda rows: [x["id"] for x in rows]  # noqa: E731
    assert ids(await store.artifacts(kind="music,lyrics")) == [r["lyr"]["id"], r["song"]["id"]]
    assert ids(await store.artifacts(model="-")) == [r["lyr"]["id"], r["song"]["id"]]  # works that carry no model
    assert ids(await store.artifacts(since=200, until=400)) == [r["song"]["id"], r["img2"]["id"]]
    assert ids(await store.artifacts(call_id="c2")) == [r["talk"]["id"], r["img2"]["id"]]
    assert ids(await store.artifacts(q="song_file")) == [r["song"]["id"]]  # backfilled audio is found by its file name
    assert ids(await store.artifacts(q="eleven")) == [r["talk"]["id"]]
    # "does this one belong on the wall as filtered?" — asked by id; transcript text counts, which a pushed event does not carry
    assert ids(await store.artifacts(artifact_id=r["lyr"]["id"], q="la la")) == [r["lyr"]["id"]]
    assert ids(await store.artifacts(artifact_id=r["lyr"]["id"], kind="image")) == []
    await store.set_artifact_hidden(r["img1"]["id"], True)
    assert ids(await store.artifacts(only_hidden=True)) == [r["img1"]["id"]]
    assert r["img1"]["id"] not in ids(await store.artifacts())
    assert await store.artifact_counts() == {"image": 1, "music": 1, "lyrics": 1, "speech": 1}
    assert await store.artifact_counts(source="backfill") == {"music": 1, "lyrics": 1}  # the tabs follow the other filters


async def test_facets_list_what_actually_occurs(store):
    r = await _seed(store)
    await store.set_artifact_hidden(r["talk"]["id"], True)
    f = await store.artifact_facets()
    assert {m["value"]: m["n"] for m in f["models"]} == {"gpt-image-2": 2, None: 2}
    assert {m["value"]: m["n"] for m in f["sources"]} == {"backfill": 3, "gui": 1}
    assert f["hidden"] == 1 and (f["oldest"], f["newest"]) == (100, 400)


async def test_lineage_and_stepping_through_the_wall(store):
    r = await _seed(store)
    child = await store.add_artifact(kind="image", file_path="C:/w/edit.png", parent_id=r["img1"]["id"], created_at=600)
    assert [c["id"] for c in await store.artifact_children(r["img1"]["id"])] == [child["id"]]
    n = await store.artifact_neighbours(r["img2"]["created_at"])
    assert n == {"newer": r["song"]["id"], "older": r["img1"]["id"]}
    n = await store.artifact_neighbours(r["img2"]["created_at"], kind="image")  # stepping stays inside the filter
    assert n == {"newer": child["id"], "older": r["img1"]["id"]}
    assert (await store.artifact_neighbours(100))["older"] is None


async def test_ledger_rows_name_their_works(store):
    r = await _seed(store)
    assert await store.artifact_ids_by_call(["c1", "c2", "nope"]) == {"c1": [r["img1"]["id"]], "c2": [r["img2"]["id"], r["talk"]["id"]]}
    a = await store.call_started("generate_image", {})
    b = await store.call_started("complete_text", {})
    c = await store.call_started("generate_music", {})
    assert {x["id"] for x in await store.calls(tool="generate_image,generate_music")} == {a, c}
    assert b in {x["id"] for x in await store.calls()}


async def test_a_ticketed_call_gets_its_real_cost_and_fetching_does_not_bill_twice(store):
    """Before: a generation that returned a ticket cost nothing on the ledger
    unless someone fetched it — and then the cost sat on the fetch's row."""
    ctx = NS(store=store, bus=EventBus(), jobs=JobManager())
    release = asyncio.Event()

    async def late(result):
        from omniapi_mcp.artifacts import current_call

        call = current_call.get()
        meta = extract_call_meta(result)
        await store.settle_ticket_call(call.call_id, model=meta.get("model"), provider=meta.get("provider"), cost_usd=meta.get("cost_usd"))

    ctx.jobs.on_late_result = late
    recorded = make_recorded(lambda: ctx)

    async def slow():
        await release.wait()
        return {"model": "V6", "provider": "kie", "cost_usd": 0.06}

    @recorded
    async def generate_music(prompt: str = "") -> dict:
        return await ctx.jobs.run("generate_music", slow(), soft_timeout=0.05)

    @recorded
    async def get_job_result(task_id: str = "") -> dict:
        return await ctx.jobs.get(task_id)

    ticket = await generate_music(prompt="x")
    (row,) = await store.calls(tool="generate_music")
    assert row["status"] == "ticket" and row["cost_usd"] is None
    release.set()
    for _ in range(50):
        await asyncio.sleep(0.01)
        (row,) = await store.calls(tool="generate_music")
        if row["status"] == "ok":
            break
    assert (row["status"], row["cost_usd"], row["model"]) == ("ok", 0.06, "V6")  # settled without anyone fetching

    assert (await get_job_result(task_id=ticket["task_id"]))["cost_usd"] == 0.06  # the caller still sees the cost
    (fetch,) = await store.calls(tool="get_job_result")
    assert fetch["cost_usd"] is None  # ...but the ledger counts it once
    assert (await store.cost_summary(days=1))["total"]["cost"] == pytest.approx(0.06)
    await ctx.jobs.close()
