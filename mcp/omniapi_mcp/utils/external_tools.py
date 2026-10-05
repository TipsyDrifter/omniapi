"""What this computer has of the outside programs OmniAPI leans on (1.3-M4,
``GET /api/tools``): found or not, where, the version when it is cheap to
ask, what stops working without it, and how to install it.

Read-only: nothing is installed, removed or configured. A version is asked
for only from programs that answer at once and do nothing else (``node
--version``, ``ffmpeg -version``), with a short timeout; the npm-installed
CLIs (Codex, Gemini CLI) start a whole Node program to say their version and
Claude Code may look for updates, so those are not run at all.

Lookups use the same functions the features themselves use (``which_cli``,
``find_claude_cli``, ``ffmpeg_path``), so "found" here means "the feature
will find it". On Windows a program installed after the service started is
not on the service's PATH yet: a re-check also reads the PATH stored in the
registry and reports such a program as ``installed_after_start`` (a restart
of the service picks it up).

Install instructions and their sources follow
``docs/research/2026-10-04-取得key的位置與外部工具安裝方式查證.md``.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Optional

VERSION_TIMEOUT = 3.0

Which = Callable[[str], Optional[str]]

#: per tool: name, what is lost without it, the official way(s) to install, the source
TOOLS: dict[str, dict[str, Any]] = {
    "node": {
        "name": "Node.js（npx）",
        "affects": "派工時 agent 不能上網搜尋（搜尋工具要用 npx 啟動）",
        "features": ["dispatch.search"],
        "install": [{"method": "winget", "command": "winget install OpenJS.NodeJS.LTS"}],
        "source": "https://nodejs.org/en/download",
    },
    "ffmpeg": {
        "name": "ffmpeg",
        "affects": "Gemini 的語音不能轉成 MP3（會存成 WAV）",
        "features": ["speech.gemini_mp3"],
        "install": [
            {"method": "download", "command": None, "note": "FFmpeg 官網列的 Windows 版來源是 gyan.dev 與 BtbN"},
            {"method": "winget", "command": "winget install Gyan.FFmpeg",
             "note": "官網沒有提 winget；這是 Gyan（官網推薦的 Windows 版提供者）的套件，不是 FFmpeg 專案自己維護的"},
        ],
        "source": "https://ffmpeg.org/download.html",
    },
    "claude_code": {
        "name": "Claude Code",
        "affects": "派工時不能選 Claude Code（Claude、DeepSeek、OpenRouter 的模型都靠它派工）",
        "features": ["dispatch.claude"],
        "install": [
            {"method": "powershell", "command": "irm https://claude.ai/install.ps1 | iex", "note": "官方推薦的原生安裝程式"},
            {"method": "winget", "command": "winget install Anthropic.ClaudeCode", "note": "不會自動更新"},
        ],
        "source": "https://code.claude.com/docs/en/setup",
    },
    "claude_login": {
        "name": "Claude Code 的登入",
        "affects": "沒有 Anthropic 的 key 時，Claude 模型不能用訂閱派工",
        "features": ["dispatch.claude_subscription"],
        "install": [{"method": "login", "command": "claude", "note": "在終端機執行後照畫面登入（這一步查證報告沒有另外核對）"}],
        "source": "https://code.claude.com/docs/en/setup",
    },
    "codex": {
        "name": "Codex CLI",
        "affects": "派工時不能選 Codex（OpenAI 模型的派工）",
        "features": ["dispatch.codex"],
        "install": [
            {"method": "powershell", "command": "irm https://chatgpt.com/codex/install.ps1 | iex", "note": "官方 README 列的第一種方式"},
            {"method": "npm", "command": "npm install -g @openai/codex", "note": "要先有 Node.js"},
        ],
        "source": "https://github.com/openai/codex",
    },
    "gemini_cli": {
        "name": "Gemini CLI",
        "affects": "派工時不能選 Gemini CLI（Gemini 模型的派工）",
        "features": ["dispatch.gemini"],
        "install": [{"method": "npm", "command": "npm install -g @google/gemini-cli", "note": "要先有 Node.js"}],
        "source": "https://github.com/google-gemini/gemini-cli",
    },
}


# ---------------------------------------------------------------- PATH from the registry
def registry_path() -> Optional[str]:
    """The PATH a newly started program would get (user + machine, from the
    registry), or ``None`` off Windows / when it cannot be read."""
    if os.name != "nt":
        return None
    try:
        import winreg
    except ImportError:  # pragma: no cover
        return None
    parts: list[str] = []
    for root, sub in ((winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                      (winreg.HKEY_CURRENT_USER, "Environment")):
        try:
            with winreg.OpenKey(root, sub) as k:
                value, _ = winreg.QueryValueEx(k, "Path")
        except OSError:
            continue
        if isinstance(value, str):
            parts.append(os.path.expandvars(value))
    return os.pathsep.join(parts) if parts else None


def _which_on(path: Optional[str]) -> Optional[Which]:
    """A ``which`` that looks only on ``path`` (same name variants as ``which_cli``)."""
    if not path:
        return None

    def which(name: str) -> Optional[str]:
        for cand in (name, f"{name}.cmd", f"{name}.exe"):
            hit = shutil.which(cand, path=path)
            if hit:
                return hit
        return None

    return which


# ---------------------------------------------------------------- versions
def _run_version(args: list[str], timeout: float = VERSION_TIMEOUT) -> Optional[str]:
    """First line of a quick ``--version`` style command, or ``None``."""
    kw: dict[str, Any] = {}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console flash from a background service
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                           encoding="utf-8", errors="replace", **kw)
    except (OSError, subprocess.SubprocessError):
        return None
    line = (p.stdout or p.stderr or "").strip().splitlines()
    return line[0].strip()[:120] if p.returncode == 0 and line else None


def node_version(exe: str, run: Callable[[list[str]], Optional[str]] = _run_version) -> Optional[str]:
    out = run([exe, "--version"])
    return out.lstrip("v") if out and re.match(r"^v?\d+\.\d+", out) else None


def ffmpeg_version(exe: str, run: Callable[[list[str]], Optional[str]] = _run_version) -> Optional[str]:
    out = run([exe, "-hide_banner", "-version"]) or ""
    m = re.match(r"^ffmpeg version (\S+)", out)
    return m.group(1) if m else None


# ---------------------------------------------------------------- detect
def detect(
    settings: Any = None,
    *,
    which: Optional[Which] = None,
    fresh_path: Optional[str] = "registry",
    run: Callable[[list[str]], Optional[str]] = _run_version,
    home: Optional[Path] = None,
    versions: bool = True,
    bundled: Optional[Callable[[], Optional[Path]]] = None,
) -> dict[str, Any]:
    """Look for every tool. ``which`` replaces the PATH lookup (tests);
    ``fresh_path`` is the PATH to try for programs the service's own PATH
    misses (``"registry"`` = read it, ``None`` = don't)."""
    from ..harness.base import which_cli
    from ..harness.claude import find_claude_cli
    from .audio import ffmpeg_path

    w: Which = which or which_cli
    fresh = _which_on(registry_path() if fresh_path == "registry" else fresh_path)
    home = home or Path.home()
    found: dict[str, dict[str, Any]] = {}

    def item(tid: str, path: Optional[str], *, version: Optional[str] = None, detail: Optional[str] = None,
             late: Optional[str] = None, **extra: Any) -> None:
        spec = TOOLS[tid]
        found[tid] = {
            "id": tid,
            "name": spec["name"],
            "found": path is not None,
            "path": path,
            "version": version,
            "installed_after_start": late is not None,
            "detail": detail if path is not None or late is None else "裝好了，但服務是在那之前啟動的：重開服務才用得到",
            "affects": spec["affects"],
            "features": spec["features"],
            "install": spec["install"],
            "source": spec["source"],
            **({"found_path": late} if late else {}),
            **extra,
        }

    def late_hit(name: str) -> Optional[str]:
        return fresh(name) if fresh else None

    # Node / npx: the search tools of agent runs are started with npx
    node = w("node")
    npx = w("npx")
    if npx:
        item("node", node or npx, version=node_version(node, run) if (versions and node) else None, npx_path=npx)
    else:
        item("node", None, late=late_hit("npx"), detail="找不到 npx" if node else None, npx_path=None)

    # ffmpeg: the Gemini TTS MP3 transcode (utils/audio.py)
    ff = ffmpeg_path() if which is None else w("ffmpeg")
    if ff:
        item("ffmpeg", ff, version=ffmpeg_version(ff, run) if versions else None)
    else:
        item("ffmpeg", None, late=late_hit("ffmpeg"))

    # Claude Code: the very lookup agent runs use (harness/claude.py)
    extra_kw: dict[str, Any] = {"bundled": bundled} if bundled is not None else {}
    cli = find_claude_cli(settings, home=home, **({"which": which} if which else {}), **extra_kw)
    if cli.path:
        item("claude_code", cli.path, detail={"setting": "照設定（HARNESS__CLAUDE_CLI）", "path": "在 PATH 上", "local-bin": "在 ~/.local/bin",
                                              "sdk-bundled": "用的是 SDK 內附的那一份"}.get(cli.source), cli_source=cli.source)
    else:
        again = find_claude_cli(settings, home=home, which=fresh, **extra_kw) if fresh else None
        item("claude_code", None, late=again.path if again and again.path else None, detail=cli.reason, cli_source=None)

    # the Claude Code login (subscription runs; harness/registry.py looks at the same file)
    cred = home / ".claude" / ".credentials.json"
    item("claude_login", str(cred) if cred.is_file() else None,
         detail="有登入憑證（沒有檢查是否過期）" if cred.is_file() else "找不到登入憑證（~/.claude/.credentials.json）")

    # Codex / Gemini CLI: the same lookup their harnesses use
    for tid, exe in (("codex", "codex"), ("gemini_cli", "gemini")):
        hit = w(exe)
        if hit:
            item(tid, hit)
        else:
            item(tid, None, late=late_hit(exe))

    tools = [found[t] for t in TOOLS]
    return {
        "checked_at": time.time(),
        "platform": os.name,
        "tools": tools,
        "summary": {"found": sum(1 for t in tools if t["found"]), "missing": sum(1 for t in tools if not t["found"])},
    }
