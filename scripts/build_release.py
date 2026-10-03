"""build_release.py — 打 OmniAPI 的三個發布包到 dist/（決策記錄 M7-f）。

產出：
  dist/omniapi-mcp.dxt          Claude Desktop 擴充（zip）：mcp/ 的 manifest.json、pyproject.toml、uv.lock、
                                README.md、.python-version、omniapi_mcp/ 原始碼（去掉 __pycache__／.pyc）
  dist/omniapi-skill.zip        repo 裡 skill/omniapi/ 的檔案，zip 內以 omniapi/ 為前綴
                                （來源是 repo，不是 ~/.claude/skills/——安裝好的那份可能比 repo 舊或新）
  dist/omniapi-v<版本>.zip       鏡像內容（scripts/mirror-publish.sh --export-to）＋已經 build 好的 gui/dist，
                                zip 內最上層是 omniapi-v<版本>/；沒有 Node 的人解開就能跑 daemon＋GUI
  dist/SHA256SUMS.txt           三個檔的 SHA-256（sha256sum -c 相容格式）

執行（這台的 python 是 Store 空殼，一律走 uv）：
  cd mcp && uv run python ../scripts/build_release.py

  --allow-version-mismatch  五處版本號不一致時照樣打包（只給「先驗打包機制」用；正式發布不要帶）
  --allow-missing-docs      鏡像內容缺 README.md／LICENSE 時照樣打包（同上）
  --allow-leaks             鏡像安全網命中時照樣打包（傳給 mirror-publish.sh；同上）
  --out <資料夾>            輸出位置，預設 <repo>/dist

版本號五處要一致才打包：mcp/pyproject.toml、mcp/omniapi_mcp/__init__.py、mcp/manifest.json、gui/package.json，
  以及 mcp/uv.lock 裡 omniapi-mcp 自己那一筆（改了 pyproject 沒重鎖，它會停在舊版）。
  manifest.json 必須是 semver 寫法（Claude Desktop 的 dxt 規格）；比對時把 pre-release 正規化，
  所以 pyproject 的 1.0.0a1 與 manifest 的 1.0.0-alpha.1 算一致。

gui/dist 不在這裡 build：gui/dist 是主人正式 daemon 正在提供的檔案，在正式 gui/ 裡 build 會當場換掉它。
  這裡只檢查 gui/dist/index.html 存在、而且比 gui/src 新；不是就停下來請你先跑 `npm run build --prefix gui`。

只用標準庫（Python 3.10：沒有 tomllib，pyproject 的版本用正規表示式讀 [project] 段）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DXT_INCLUDE_TOP = ("manifest.json", "pyproject.toml", "uv.lock", "README.md", ".python-version")
DXT_INCLUDE_DIRS = ("omniapi_mcp",)
EXCLUDE_PARTS = {".venv", ".git", "__pycache__", "node_modules", ".pytest_cache", ".mypy_cache"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}

# ---------------------------------------------------------------- 版本號

_PRE_ALIASES = {"a": "a", "alpha": "a", "b": "b", "beta": "b", "c": "rc", "rc": "rc", "pre": "rc", "preview": "rc"}
# PEP 440 的 1.0.0a1／1.0.0rc2、semver 的 1.0.0-alpha.1／1.0.0-rc.2、npm 常見的 1.0.0-a1 都吃
_VERSION_RE = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)"
    r"(?:[-.]?(alpha|beta|preview|pre|rc|a|b|c)[-.]?(\d+)?)?$",
    re.IGNORECASE,
)
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)


def normalize_version(raw: str | None) -> tuple | None:
    """把各家寫法正規化成 (major, minor, patch, pre_kind, pre_num)；認不得就回 None。

    1.0.0 → (1,0,0,'','0')；1.0.0a1、1.0.0-alpha.1、1.0.0-a1 → (1,0,0,'a',1)。
    """
    if raw is None:
        return None
    m = _VERSION_RE.match(raw.strip())
    if not m:
        return None
    major, minor, patch, kind, num = m.groups()
    if kind is None:
        return (int(major), int(minor), int(patch), "", 0)
    return (int(major), int(minor), int(patch), _PRE_ALIASES[kind.lower()], int(num or 0))


def is_semver(raw: str | None) -> bool:
    return bool(raw) and bool(_SEMVER_RE.match(raw))


def read_versions(root: Path) -> dict[str, str | None]:
    """讀五處版本號；讀不到的值是 None（不猜）。"""
    out: dict[str, str | None] = {}

    pyproject = root / "mcp" / "pyproject.toml"
    v = None
    if pyproject.exists():
        section = None
        for line in pyproject.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("[") and s.endswith("]"):
                section = s.strip("[]").strip()
                continue
            if section == "project":
                m = re.match(r'^version\s*=\s*["\']([^"\']+)["\']', s)
                if m:
                    v = m.group(1)
                    break
    out["mcp/pyproject.toml"] = v

    init = root / "mcp" / "omniapi_mcp" / "__init__.py"
    v = None
    if init.exists():
        m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', init.read_text(encoding="utf-8"), re.M)
        v = m.group(1) if m else None
    out["mcp/omniapi_mcp/__init__.py"] = v

    for rel in ("mcp/manifest.json", "gui/package.json"):
        p = root / rel
        v = None
        if p.exists():
            try:
                v = json.loads(p.read_text(encoding="utf-8")).get("version")
            except (json.JSONDecodeError, AttributeError):
                v = None
        out[rel] = v

    out["mcp/uv.lock"] = read_lock_version(root / "mcp" / "uv.lock")
    return out


def read_lock_version(lock: Path, package: str = "omniapi-mcp") -> str | None:
    """uv.lock 裡專案自己那一筆 [[package]] 的 version。

    pyproject 升了版本、沒有重跑 uv lock／uv sync，這一筆就停在舊版，而鎖檔會跟著 dxt 與發布包出去。"""
    if not lock.exists():
        return None
    name = version = None
    for line in lock.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("["):
            if name == package:
                return version
            name = version = None
            continue
        m = re.match(r'^(name|version)\s*=\s*"([^"]*)"', s)
        if m:
            if m.group(1) == "name":
                name = m.group(2)
            elif version is None:
                version = m.group(2)
    return version if name == package else None


def check_versions(versions: dict[str, str | None]) -> list[str]:
    """回傳問題清單；空清單＝五處一致而且 manifest 是 semver。"""
    problems: list[str] = []
    norm = {k: normalize_version(v) for k, v in versions.items()}
    for k, v in versions.items():
        if v is None:
            problems.append(f"{k}：讀不到版本號")
        elif norm[k] is None:
            problems.append(f"{k}：認不得的版本寫法 {v!r}")
    manifest = versions.get("mcp/manifest.json")
    if manifest is not None and not is_semver(manifest):
        problems.append(f"mcp/manifest.json：{manifest!r} 不是 semver（dxt 規格要求，例如 1.0.0-alpha.1）")
    distinct = {n for n in norm.values() if n is not None}
    if len(distinct) > 1:
        problems.append("五處版本號不一致")
    return problems


# ---------------------------------------------------------------- 打包


def _skip(rel: Path) -> bool:
    return any(part in EXCLUDE_PARTS for part in rel.parts) or rel.suffix in EXCLUDE_SUFFIXES


def _zip_tree(zf: zipfile.ZipFile, src: Path, prefix: str) -> list[str]:
    names: list[str] = []
    for p in sorted(src.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(src)
        if _skip(rel):
            continue
        arc = f"{prefix}{rel.as_posix()}" if prefix else rel.as_posix()
        zf.write(p, arc)
        names.append(arc)
    return names


def build_dxt(mcp_dir: Path, out: Path) -> list[str]:
    """Claude Desktop 擴充：比照舊版 build_omniapi.py 的清單。回傳 zip 內的檔名。"""
    missing = [n for n in ("manifest.json", "pyproject.toml") if not (mcp_dir / n).is_file()]
    if missing:
        raise SystemExit(f"✗ {mcp_dir} 缺 {', '.join(missing)}，dxt 打不起來")
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    names: list[str] = []
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for name in DXT_INCLUDE_TOP:
            p = mcp_dir / name
            if p.is_file():
                zf.write(p, name)
                names.append(name)
        for d in DXT_INCLUDE_DIRS:
            if not (mcp_dir / d).is_dir():
                raise SystemExit(f"✗ {mcp_dir / d} 不存在")
            names += _zip_tree(zf, mcp_dir / d, f"{d}/")
    return names


def build_skill_zip(skill_dir: Path, out: Path) -> list[str]:
    """skill 的所有檔案，zip 內以 omniapi/ 為前綴（claude.ai 上傳 skill 的格式）。"""
    if not (skill_dir / "SKILL.md").is_file():
        raise SystemExit(f"✗ {skill_dir} 裡沒有 SKILL.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        return _zip_tree(zf, skill_dir, "omniapi/")


def check_gui_dist(gui_dir: Path) -> str | None:
    """gui/dist 可以直接打包就回 None；否則回一句給人看的理由。"""
    index = gui_dir / "dist" / "index.html"
    if not index.is_file():
        return f"{index} 不存在"
    built = index.stat().st_mtime
    sources = [p for p in (gui_dir / "src").rglob("*") if p.is_file()]
    sources += [p for p in (gui_dir / "index.html",) if p.is_file()]
    newer = [p for p in sources if p.stat().st_mtime > built]
    if newer:
        sample = ", ".join(str(p.relative_to(gui_dir)) for p in newer[:3])
        return f"gui/dist 比原始碼舊（{len(newer)} 個檔比它新，例如 {sample}）"
    return None


def build_bundle(export_dir: Path, gui_dist: Path, out: Path, version: str) -> list[str]:
    """鏡像內容＋已 build 好的 gui/dist，最上層 omniapi-v<版本>/。"""
    top = f"omniapi-v{version}/"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        names = _zip_tree(zf, export_dir, top)
        # 鏡像匯出本來就不含 gui/dist（未追蹤＋排除規則）；有的話代表匯出壞了，寧可停
        if any(n.startswith(f"{top}gui/dist/") for n in names):
            raise SystemExit("✗ 鏡像匯出裡竟然有 gui/dist/——匯出規則壞了")
        names += _zip_tree(zf, gui_dist, f"{top}gui/dist/")
    return names


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find_bash() -> str:
    """找 Git Bash。不能直接把 "bash" 丟給 subprocess：Windows 的 CreateProcess 會先搜 System32，
    有裝 WSL 的機器會跑到 WSL 的 bash（看不到 Windows 路徑）。OMNIAPI_BASH 可以覆寫。"""
    env = os.environ.get("OMNIAPI_BASH")
    if env:
        return env
    found = shutil.which("bash")
    if found and "system32" not in found.lower():
        return found
    for cand in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"):
        if Path(cand).is_file():
            return cand
    raise SystemExit("✗ 找不到 Git Bash；用 OMNIAPI_BASH=<bash.exe 路徑> 指定")


def export_mirror(root: Path, dest: Path, allow_leaks: bool) -> None:
    cmd = [find_bash(), str(root / "scripts" / "mirror-publish.sh"), "--export-to", str(dest)]
    if allow_leaks:
        cmd.append("--allow-leaks")
    print(f"▸ 鏡像匯出：{' '.join(cmd[1:])}")
    r = subprocess.run(cmd, cwd=root, capture_output=True)
    for stream in (r.stdout, r.stderr):
        text = stream.decode("utf-8", errors="replace").rstrip()
        if text:
            print("    " + text.replace("\n", "\n    "))
    if r.returncode != 0:
        raise SystemExit(f"✗ mirror-publish.sh --export-to 失敗（exit {r.returncode}）")


def dirty_paths(root: Path, paths: list[str]) -> list[str]:
    try:
        r = subprocess.run(["git", "status", "--porcelain", "--", *paths], cwd=root,
                           capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return []
    return [ln for ln in r.stdout.decode("utf-8", errors="replace").splitlines() if ln.strip()]


def main(argv: list[str] | None = None) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="Build OmniAPI release packages into dist/")
    ap.add_argument("--allow-version-mismatch", action="store_true")
    ap.add_argument("--allow-missing-docs", action="store_true")
    ap.add_argument("--allow-leaks", action="store_true")
    ap.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    root: Path = args.root.resolve()
    out_dir: Path = (args.out or root / "dist").resolve()

    versions = read_versions(root)
    problems = check_versions(versions)
    width = max(len(k) for k in versions)
    print("▸ 版本號")
    for k, v in versions.items():
        print(f"    {k:<{width}}  {v}")
    if problems:
        for p in problems:
            print(f"  ✗ {p}")
        if not args.allow_version_mismatch:
            print("✗ 版本號要五處一致才打包（只想驗打包機制：加 --allow-version-mismatch）", file=sys.stderr)
            return 1
        print("  ⚠ --allow-version-mismatch：照樣打包，檔名用 pyproject.toml 的版本——這批產出不能拿去發布")
    version = versions["mcp/pyproject.toml"]
    if not version:
        print("✗ 讀不到 mcp/pyproject.toml 的版本號", file=sys.stderr)
        return 1

    gui_problem = check_gui_dist(root / "gui")
    if gui_problem:
        print(f"✗ {gui_problem}——先跑 `npm run build --prefix gui` 再打包", file=sys.stderr)
        return 1

    dirty = dirty_paths(root, ["mcp", "skill"])
    if dirty:
        print(f"  ⚠ mcp/ 或 skill/ 有 {len(dirty)} 個沒 commit 的改動：dxt／skill zip 取自工作樹，"
              "發布包 zip 取自 git（HEAD）——兩邊會不一樣")

    produced: list[tuple[Path, int]] = []
    with tempfile.TemporaryDirectory(prefix="omniapi-release-") as tmp:
        # 先匯出＋過安全網，全部過了才開始寫 dist/——免得留下「dxt 是新的、zip 是舊的」半套產出
        export_dir = Path(tmp) / "export"
        export_mirror(root, export_dir, args.allow_leaks)
        missing = [n for n in ("README.md", "LICENSE") if not (export_dir / n).is_file()]
        if missing:
            msg = f"鏡像內容缺 {', '.join(missing)}"
            if not args.allow_missing_docs:
                print(f"✗ {msg}（只想驗打包機制：加 --allow-missing-docs）", file=sys.stderr)
                return 1
            print(f"  ⚠ --allow-missing-docs：{msg}，照樣打包——這批產出不能拿去發布")
        # 匯出取自 git（HEAD），版本號卻讀工作樹：改了版本還沒 commit 時，zip 檔名會跟內容對不上
        exported = read_versions(export_dir)["mcp/pyproject.toml"]
        if normalize_version(exported) != normalize_version(version):
            msg = f"匯出內容（git）的版本是 {exported}，工作樹是 {version}——先 commit 版本號"
            if not args.allow_version_mismatch:
                print(f"✗ {msg}", file=sys.stderr)
                return 1
            print(f"  ⚠ --allow-version-mismatch：{msg}")

        out_dir.mkdir(parents=True, exist_ok=True)
        dxt = out_dir / "omniapi-mcp.dxt"
        produced.append((dxt, len(build_dxt(root / "mcp", dxt))))
        skill = out_dir / "omniapi-skill.zip"
        produced.append((skill, len(build_skill_zip(root / "skill" / "omniapi", skill))))
        bundle = out_dir / f"omniapi-v{version}.zip"
        produced.append((bundle, len(build_bundle(export_dir, root / "gui" / "dist", bundle, version))))

    sums = out_dir / "SHA256SUMS.txt"
    lines = []
    print(f"▸ 產出（v{version}）→ {out_dir}")
    for path, count in produced:
        digest = sha256(path)
        lines.append(f"{digest}  {path.name}")
        print(f"    {path.name:<24} {path.stat().st_size:>12,} bytes  {count:>4} 檔  sha256 {digest}")
    sums.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"    {sums.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
