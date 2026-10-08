"""OpenRouter's image models (1.4-M2) end-to-end against an OFFLINE sandbox daemon (no vendor call):

    OMNIAPI_HOME=<empty dir> OMNIAPI_DEV=1 OMNIAPI_OFFLINE=1 omni serve --foreground --port 7873
    python tests/e2e/openrouter_images_e2e.py [port]

The sandbox lists a stand-in OpenRouter roster (ten models copied from the
real listing). Walks: the roster in the model list, the generate options
and MCP's list_available_models → an estimate from the listed price (shown,
never charged) → a generation with a Grok Imagine model from the generate
page → the work on the wall → that work edited with FLUX.3 (via the page and
via MCP's edit_image). Then the requests the listing rules out are refused
before a job exists: editing with a model that takes no reference image,
a resolution the model does not list, too many reference images.
"""

import asyncio
import json
import sys
import time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 7873
BASE = f"http://127.0.0.1:{PORT}"
GROK = "x-ai/grok-imagine-image-2.0"
FLUX = "black-forest-labs/flux-3-image"
MING = "inclusionai/ming-image-0.1-design"  # takes no reference image


def check(label: str, ok: bool, detail: object = "") -> None:
    if not ok:
        print("FAIL " + label, detail)
        raise SystemExit(1)
    print("PASS " + label)


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE, timeout=60) as http:
        st = (await http.get("/api/status")).json()
        check("the daemon is an offline dev sandbox", st["offline"] and st["dev"], st)

        # ---- the roster
        models = (await http.get("/api/models", params={"modality": "image"})).json()["models"]["image"]
        orm = {m["id"]: m for m in models if m["provider"] == "openrouter"}
        check("the model list carries the OpenRouter image models", {GROK, FLUX, MING} <= set(orm), sorted(orm))
        check("each names its maker", orm[GROK]["vendor_label"] == "xAI" and orm[FLUX]["vendor_label"] == "Black Forest Labs", orm[GROK])
        check("…and its request shape", orm[GROK]["image_params"]["qualities"] == ["low", "medium"] and orm[MING]["image_params"]["max_references"] == 0)
        check("…and its listed price lines", any(l.get("variant") == "low_1k" and l["cost_usd"] == 0.04 for l in orm[GROK]["pricing"]["lines"]))
        check("the direct vendors' own rows are still there", any(m["id"] == "gpt-image-2" and m["provider"] == "openai" for m in models))

        opts = (await http.get("/api/generate/options")).json()
        rows = {m["id"]: m for m in opts["kinds"]["image"]["models"]}
        check("the generate page can pick them", rows[GROK]["available"] and rows[FLUX]["available"], rows.get(GROK))
        check("they carry their parameters on the row", rows[FLUX]["image_params"]["resolutions"] == ["768", "1K", "1.5K", "2K", "4K"])

        # ---- the estimate: the sandbox charges nothing but shows the listed price
        est = (await http.post("/api/generate/estimate", json={"kind": "image", "params": {"prompt": "x", "model": GROK, "image_size": "1K", "quality": "low", "aspect_ratio": "1:1"}})).json()
        check("the sandbox estimate is zero…", est["basis"] == "sandbox" and est["usd"] == 0.0, est)
        check("…and says the listed price of low 1K", est.get("listed", {}).get("basis") == "variant" and est["listed"]["usd"] == 0.04, est)
        rng = (await http.post("/api/generate/estimate", json={"kind": "image", "params": {"prompt": "x", "model": GROK, "image_size": "2K"}})).json()
        check("no quality said: a range, not a guess", rng["listed"]["basis"] == "range" and (rng["listed"]["low"], rng["listed"]["high"]) == (0.06, 0.08), rng)

        # ---- generate from the page
        async def finished(gid: str) -> dict:
            t0 = time.time()
            while time.time() - t0 < 60:
                row = (await http.get(f"/api/generations/{gid}")).json()
                if row["status"] != "running":
                    return row
                await asyncio.sleep(0.3)
            raise SystemExit("FAIL a job never finished")

        before = (await http.get("/api/artifacts")).json()["counts"].get("image", 0)
        r = await http.post("/api/generations", json={"kind": "image", "params": {"prompt": "a paper boat at dawn", "model": GROK, "image_size": "1K", "quality": "low", "aspect_ratio": "16:9"}})
        check("a Grok Imagine generation starts", r.status_code == 200, r.text)
        gen = await finished(r.json()["id"])
        check("…and finishes with a work", gen["status"] == "done" and len(gen["artifacts"]) == 1, gen)
        check("the job kept its estimate (listed price in the sandbox)", gen["estimate"]["listed"]["usd"] == 0.04, gen["estimate"])
        work = gen["artifacts"][0]
        check("the work names the model", work["model"] == GROK, work)
        check("the stand-in honoured the ratio", (work.get("width"), work.get("height")) in ((1024, 576), (None, None)), work)

        wall = (await http.get("/api/artifacts")).json()
        check("it is on the wall", wall["counts"].get("image", 0) == before + 1 and wall["items"][0]["id"] == work["id"])

        # ---- edit it with FLUX.3 from the page
        r = await http.post("/api/generations", json={"kind": "image", "params": {"prompt": "make it night", "model": FLUX, "image_size": "1K", "aspect_ratio": "16:9"},
                                                      "sources": {"images": [{"artifact_id": work["id"]}]}})
        check("an edit with FLUX.3 starts", r.status_code == 200, r.text)
        check("the edit is estimated with its reference image", r.json()["estimate"]["listed"]["usd"] == 0.048, r.json()["estimate"])
        edit = await finished(r.json()["id"])
        check("…and finishes", edit["status"] == "done" and edit["tool"] == "edit_image", edit)
        child = edit["artifacts"][0]
        check("the edit is linked to its source", child["parent_id"] == work["id"] and child["model"] == FLUX, child)

        # ---- refused before a job exists
        r = await http.post("/api/generations", json={"kind": "image", "params": {"prompt": "x", "model": MING}, "sources": {"images": [{"artifact_id": work["id"]}]}})
        check("a model without reference images cannot edit", r.status_code == 400 and "takes no reference image" in r.text, r.text)
        r = await http.post("/api/generations", json={"kind": "image", "params": {"prompt": "x", "model": GROK, "image_size": "4K"}})
        check("a resolution the model does not list is refused, naming the ones it does", r.status_code == 400 and "1K, 2K" in r.text, r.text)
        four = [{"artifact_id": work["id"]}, {"artifact_id": child["id"]}, {"artifact_id": work["id"]}, {"artifact_id": child["id"]}]
        r = await http.post("/api/generations", json={"kind": "image", "params": {"prompt": "x", "model": GROK}, "sources": {"images": four}})
        check("more reference images than the model takes are refused", r.status_code == 400 and "at most 3" in r.text, r.text)
        jobs = (await http.get("/api/generations", params={"kind": "image"})).json()
        check("no job was created for them", len([j for j in (jobs if isinstance(jobs, list) else jobs.get("items", [])) if j.get("model") == MING]) == 0)

    # ---- the same through MCP
    async with streamablehttp_client(BASE + "/mcp") as (read, write, _):
        async with ClientSession(read, write) as s:
            await s.initialize()

            async def call(tool: str, args: dict) -> dict:
                res = await s.call_tool(tool, args)
                return json.loads(res.content[0].text) if not res.isError else {"error": res.content[0].text}

            listed = await call("list_available_models", {"modality": "image"})
            ids = {m["id"] for m in listed["models"]["image"]}
            check("list_available_models shows them by their OpenRouter id", {GROK, FLUX} <= ids, sorted(ids))
            img = await call("generate_image", {"prompt": "a lighthouse", "model": FLUX, "image_size": "768", "aspect_ratio": "1:1"})
            check("generate_image takes an OpenRouter id", img.get("metadata", {}).get("model") == FLUX, img)
            ed = await call("edit_image", {"prompt": "add snow", "model": GROK, "image_path": child_path(child)})
            check("edit_image takes an OpenRouter id", ed.get("operation") == "edit" and ed["metadata"]["model"] == GROK, ed)
            bad = await call("edit_image", {"prompt": "x", "model": MING, "image_path": child_path(child)})
            check("edit_image refuses a model without reference images", "takes no reference image" in json.dumps(bad), bad)
    print("ALL PASS")


def child_path(work: dict) -> str:
    row = httpx.get(f"{BASE}/api/artifacts/{work['id']}", timeout=10).json()
    return row.get("file_path") or row["artifact"]["file_path"]


if __name__ == "__main__":
    asyncio.run(main())
