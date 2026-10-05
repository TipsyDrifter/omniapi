"""What the settings page, the models page and the first-run guide read
beyond 1.3-M2 (1.3-M4 backend), against an OFFLINE sandbox daemon (no vendor call):

    # a fresh data home with a .env holding a fake kie key (to be shadowed),
    # and a stand-in for ~/.claude.json — the real one must never be touched
    set OMNIAPI_HOME=<empty dir>
    echo PROVIDERS__KIE__API_KEY=kie-e2e-fake-env-key-00003a90> %OMNIAPI_HOME%\\.env
    set OMNIAPI_CLAUDE_CONFIG=<temp dir>\\claude.json
    set OMNIAPI_DEV=1 & set OMNIAPI_OFFLINE=1
    omni serve --port 7830
    python -m tests.e2e.settings_m4_e2e 7830 one     (from mcp/; ends by shutting the sandbox down)
    omni serve --port 7830                            (same environment)
    python -m tests.e2e.settings_m4_e2e 7830 two     (after the restart; shuts down again)

Phase one: the service section of /api/status; every provider carries
``key.shadowed``, ``health``, ``get_key``, ``suggested_tiers``; a settings key
names the .env key it hides; the key test in the sandbox checks shapes without
prefix rules and files nothing; a connection result filed for the key in
effect shows on both pages; /api/tools and its re-check; Claude Code's MCP
entry is read and written in the stand-in file only.
Phase two: the connection result is still there after a restart.
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 7830
PHASE = sys.argv[2] if len(sys.argv) > 2 else "one"
BASE = f"http://127.0.0.1:{PORT}"
KEY = "sk-e2e-m4-fake-deepseek-key-WXYZ"  # never valid anywhere; the sandbox sends nothing
KIE_SETTINGS_KEY = "kie-e2e-fake-settings-key-0000-QRST"
LOCAL = {"Origin": f"http://127.0.0.1:{PORT}"}


def check(label: str, ok: bool, detail: object = "") -> None:
    if not ok:
        print("FAIL " + label, detail)
        raise SystemExit(1)
    print("PASS " + label)


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def real_claude_json_untouched_by_us() -> bool:
    """The owner's ~/.claude.json must not carry an entry pointing at this sandbox."""
    p = Path.home() / ".claude.json"
    if not p.exists():
        return True
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return True  # not ours to judge; Claude Code may be writing it right now
    entry = (data.get("mcpServers") or {}).get("omniapi-mcp") or {}
    return f":{PORT}/" not in str(entry.get("url", ""))


async def shutdown(http: httpx.AsyncClient, pid: int) -> None:
    r = await http.post("/api/shutdown")
    check("POST /api/shutdown accepted", r.status_code == 200, r.text)
    t0 = time.monotonic()
    while time.monotonic() - t0 < 20 and pid_alive(pid):
        await asyncio.sleep(0.3)
    check("the sandbox exited", not pid_alive(pid))


async def phase_one() -> None:
    async with httpx.AsyncClient(base_url=BASE, timeout=60) as http:
        st = (await http.get("/api/status")).json()
        check("the daemon is an offline dev sandbox", st["offline"] and st["dev"], st)
        pid, data_home = st["pid"], Path(st["data_home"])
        os.environ["OMNIAPI_HOME"] = str(data_home)  # this script files a result in the sandbox's home below

        # ---------------------------------------------------------- service info
        svc = st.get("service") or {}
        check("status has the service section", {"version", "layout", "data_home", "storage", "env_file", "env_file_suggested", "settings_file", "logs"} <= set(svc), svc)
        check("…paths are the sandbox's", svc["data_home"] == str(data_home) and svc["settings_file"] == str(data_home / "settings.json")
              and Path(svc["storage"]).is_absolute(), svc)
        check("…the .env it found is the sandbox's", svc["env_file"] == str(data_home / ".env"), svc)

        # ---------------------------------------------------------- settings: new fields
        s0 = (await http.get("/api/settings")).json()
        for slot, p in s0["providers"].items():
            check(f"{slot}: key.shadowed / health / get_key / suggested_tiers present",
                  {"shadowed"} <= set(p["key"]) and {"health", "get_key", "suggested_tiers"} <= set(p), p)
        check("anthropic's key page is platform.claude.com", s0["providers"]["anthropic"]["get_key"]["url"] == "https://platform.claude.com/settings/keys")
        check("openai has no key page address, only its docs", s0["providers"]["openai"]["get_key"]["url"] is None and s0["providers"]["openai"]["get_key"]["docs"])
        tiers = {slot: p["suggested_tiers"] for slot, p in s0["providers"].items()}
        check("suggestions for the five text providers but OpenRouter", all(tiers[s] for s in ("openai", "anthropic", "gemini", "deepseek"))
              and tiers["openrouter"] is None and tiers["elevenlabs"] is None and tiers["kie"] is None, tiers)
        kie = s0["providers"]["kie"]
        check("kie's key comes from the .env, nothing shadowed", kie["key"]["source"] == "env" and kie["key"]["last4"] == "3a90" and kie["key"]["shadowed"] is None, kie["key"])
        check("…untested so far", kie["health"]["state"] == "untested", kie["health"])
        check("a provider without a key has no health", s0["providers"]["deepseek"]["health"] is None)

        # ---------------------------------------------------------- shadowing
        r = await http.patch("/api/settings", json={"providers": {"kie": {"api_key": KIE_SETTINGS_KEY}}})
        check("PATCH a kie key over the .env one", r.status_code == 200 and KIE_SETTINGS_KEY not in r.text, r.text)
        k = r.json()["providers"]["kie"]["key"]
        check("…it names the .env key it hides (last four, where)", k["source"] == "settings" and k["last4"] == "QRST"
              and k["shadowed"] == {"set": True, "last4": "3a90", "source": "env", "where": "env_file"}, k)
        r = await http.patch("/api/settings", json={"providers": {"kie": {"api_key": None}}})
        k = r.json()["providers"]["kie"]["key"]
        check("removing it brings the .env key back", k["source"] == "env" and k["last4"] == "3a90" and k["shadowed"] is None, k)

        # ---------------------------------------------------------- test-key: shapes only, no prefix rules, nothing filed
        r = (await http.post("/api/settings/test-key", json={"provider": "gemini", "api_key": "AQ.new-style-auth-key-0123456789"})).json()
        check("a Gemini key without AIza passes the sandbox's shape check", r["ok"] is True and r["simulated"] is True, r)
        r = (await http.post("/api/settings/test-key", json={"provider": "kie", "api_key": "abc123"})).json()
        check("a too-short key fails the shape check", r["ok"] is False and r["reason"] == "format", r)
        r = await http.post("/api/settings/test-key", json={"provider": "kie", "api_key": "two words-0123456789"})
        check("a key with a space inside is refused", r.status_code == 400, r.text)

        # ---------------------------------------------------------- health of the key in effect
        r = await http.patch("/api/settings", json={"providers": {"deepseek": {"api_key": KEY}}})
        check("PATCH a deepseek key", r.status_code == 200)
        ds = r.json()["providers"]["deepseek"]
        check("…not tested yet", ds["health"]["state"] == "untested", ds["health"])
        t = (await http.post("/api/settings/test-key", json={"provider": "deepseek"})).json()
        check("a simulated test is not a connection result", t["simulated"] and (await http.get("/api/settings")).json()["providers"]["deepseek"]["health"]["state"] == "untested")

        from omniapi_mcp.catalog import health  # the same store the daemon reads, in the sandbox's home

        health.record("deepseek", KEY, ok=False, source="discovery", reason="rejected", status=401, message="the provider rejected the key")
        s1 = (await http.get("/api/settings")).json()["providers"]["deepseek"]["health"]
        check("a filed result shows on the settings page", s1["state"] == "failed" and s1["reason"] == "rejected" and s1["status"] == 401, s1)
        m1 = (await http.get("/api/models")).json()
        check("…and on the models page", m1["providers"]["deepseek"]["health"]["state"] == "failed" and m1["providers"]["deepseek"]["get_key"]["url"], m1["providers"]["deepseek"])
        check("…no key in either", KEY not in json.dumps(s1) and KEY not in json.dumps(m1))
        check("…filed without the key", KEY not in health.health_path().read_text(encoding="utf-8"))

        # ---------------------------------------------------------- outside programs
        t1 = (await http.get("/api/tools")).json()
        ids = [x["id"] for x in t1["tools"]]
        check("/api/tools lists the six programs", ids == ["node", "ffmpeg", "claude_code", "claude_login", "codex", "gemini_cli"], ids)
        for x in t1["tools"]:
            check(f"  {x['id']}: found={x['found']} version={x['version']} — has affects/install/source",
                  x["affects"] and x["install"] and x["source"].startswith("https://") and isinstance(x["found"], bool))
        t2 = (await http.get("/api/tools")).json()
        check("kept between calls", t2["checked_at"] == t1["checked_at"])
        t3 = (await http.get("/api/tools?refresh=true")).json()
        check("?refresh=true looks again", t3["checked_at"] > t1["checked_at"])

        # ---------------------------------------------------------- Claude Code's MCP entry (stand-in file only)
        cm = (await http.get("/api/claude-mcp")).json()
        stand_in = Path(os.environ.get("OMNIAPI_CLAUDE_CONFIG") or cm["path"])
        check("the daemon reads the stand-in file, not ~/.claude.json", cm["path"] == str(stand_in) and Path(cm["path"]) != Path.home() / ".claude.json", cm["path"])
        check("no file yet: no_config", cm["state"] == "no_config", cm)
        r = await http.post("/api/claude-mcp", headers=LOCAL)
        check("connecting without a file is refused (409), nothing created", r.status_code == 409 and not stand_in.exists(), r.text)
        stand_in.write_text(json.dumps({"numStartups": 3, "mcpServers": {"omniapi-mcp": {"type": "http", "url": "http://127.0.0.1:7788/mcp"}, "x": {"type": "http", "url": "u"}}}), encoding="utf-8")
        cm = (await http.get("/api/claude-mcp")).json()
        check("an entry pointing elsewhere reads as other", cm["state"] == "other" and cm["entry"]["url"] == "http://127.0.0.1:7788/mcp", cm)
        r = await http.post("/api/claude-mcp", headers={"Origin": "https://evil.example"})
        check("a foreign page cannot connect it (403)", r.status_code == 403)
        r = await http.post("/api/claude-mcp", headers=LOCAL)
        res = r.json()
        check("connect: 200, connected, changed, backed up", r.status_code == 200 and res["state"] == "connected" and res["changed"] and res["backed_up"], res)
        data = json.loads(stand_in.read_text(encoding="utf-8"))
        check("the stand-in file points at this service and keeps the rest",
              data["mcpServers"]["omniapi-mcp"] == {"type": "http", "url": f"http://127.0.0.1:{PORT}/mcp"} and data["numStartups"] == 3 and data["mcpServers"]["x"], data)
        check("the old entry is in the data home", json.loads((data_home / "claude-mcp-entry.backup.json").read_text(encoding="utf-8"))["url"] == "http://127.0.0.1:7788/mcp")
        check("the owner's real ~/.claude.json has no entry for this sandbox", real_claude_json_untouched_by_us())

        await shutdown(http, pid)
    print("PHASE ONE PASS — restart the sandbox with the same environment, then run phase two")


async def phase_two() -> None:
    async with httpx.AsyncClient(base_url=BASE, timeout=60) as http:
        st = (await http.get("/api/status")).json()
        check("a restarted offline sandbox", st["offline"])
        ds = (await http.get("/api/settings")).json()["providers"]["deepseek"]
        check("the key is still there", ds["key"]["last4"] == "WXYZ", ds["key"])
        check("…and so is its last connection result", ds["health"]["state"] == "failed" and ds["health"]["reason"] == "rejected", ds["health"])
        r = await http.patch("/api/settings", json={"providers": {"deepseek": {"api_key": "sk-e2e-m4-another-fake-key-ABCD"}}})
        check("a different key reads as not tested", r.json()["providers"]["deepseek"]["health"]["state"] == "untested", r.json()["providers"]["deepseek"]["health"])
        check("the owner's real ~/.claude.json has no entry for this sandbox", real_claude_json_untouched_by_us())
        await shutdown(http, st["pid"])
    print("ALL PASS")


if __name__ == "__main__":
    asyncio.run(phase_one() if PHASE == "one" else phase_two())
