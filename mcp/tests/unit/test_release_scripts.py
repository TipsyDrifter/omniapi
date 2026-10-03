"""scripts/build_release.py 的單元測試：版本一致性判斷、三個發布包的內容清單、gui/dist 新舊檢查。

全部在 tmp 目錄造迷你 repo 跑，不碰真的 dist/、gui/dist/；不呼叫 mirror-publish.sh（那支有自己的
scripts/test-mirror-publish.sh 自我測試）。
"""

from __future__ import annotations

import importlib.util
import json
import os
import time
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "build_release.py"
if not SCRIPT.is_file():  # 例如只拿到 mcp/ 的安裝（dxt）——沒有 scripts/ 就沒得測
    pytest.skip(f"{SCRIPT} not found", allow_module_level=True)

_spec = importlib.util.spec_from_file_location("build_release", SCRIPT)
br = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(br)


# ---------------------------------------------------------------- 迷你 repo


def _write(p: Path, text: str = "x") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def uv_lock(version: str) -> str:
    """A trimmed uv.lock: the project's own entry sits between dependencies, with its
    dependency list mentioning other names and a metadata sub-table after it."""
    return (
        'version = 1\nrevision = 3\nrequires-python = ">=3.10"\n\n'
        '[[package]]\nname = "httpx"\nversion = "0.28.1"\nsource = { registry = "https://pypi.org/simple" }\n\n'
        f'[[package]]\nname = "omniapi-mcp"\nversion = "{version}"\nsource = {{ editable = "." }}\n'
        'dependencies = [\n    { name = "httpx" },\n]\n\n'
        '[package.metadata]\nrequires-dist = [\n    { name = "httpx", specifier = ">=0.27" },\n]\n\n'
        '[[package]]\nname = "pypdf"\nversion = "6.1.0"\nsource = { registry = "https://pypi.org/simple" }\n'
    )


def make_repo(root: Path, py="1.0.0", init="1.0.0", manifest="1.0.0", pkg="1.0.0", lock=None) -> Path:
    m = root / "mcp"
    _write(m / "pyproject.toml", f'[tool.hatch]\nversion = "9.9.9"\n\n[project]\nname = "omniapi-mcp"\nversion = "{py}"\n')
    _write(m / "omniapi_mcp" / "__init__.py", f'"""pkg"""\n\n__version__ = "{init}"\n')
    _write(m / "manifest.json", json.dumps({"name": "omniapi-mcp", "version": manifest}))
    _write(m / "uv.lock", uv_lock(py if lock is None else lock))  # uv writes the PEP 440 form
    _write(m / "README.md", "# mcp")
    _write(m / ".python-version", "3.10")
    _write(m / "omniapi_mcp" / "server.py", "print('hi')")
    _write(m / "omniapi_mcp" / "daemon" / "app.py", "app = 1")
    _write(m / "omniapi_mcp" / "__pycache__" / "server.cpython-310.pyc", "bytecode")
    _write(m / "omniapi_mcp" / "stray.pyc", "bytecode")
    # 不該進 dxt 的：內部指示、真 .env、測試、儲存區
    _write(m / "CLAUDE.md", "internal")
    _write(m / ".env", "# pretend this holds real keys")  # 內容刻意不寫 KEY=值，免得鏡像安全網把這個測試檔當成外洩
    _write(m / "tests" / "test_x.py", "def test(): pass")
    _write(m / "storage" / "img.png", "png")

    g = root / "gui"
    _write(g / "package.json", json.dumps({"name": "omniapi-gui", "version": pkg}))
    _write(g / "index.html", "<html></html>")
    _write(g / "src" / "main.tsx", "export {}")

    s = root / "skill" / "omniapi"
    _write(s / "SKILL.md", "---\nname: omniapi\n---\n")
    _write(s / "references" / "image.md", "# image")
    _write(s / "references" / "__pycache__" / "junk.pyc", "junk")
    return root


# ---------------------------------------------------------------- 版本號


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("1.0.0", (1, 0, 0, "", 0)),
        ("1.0.0a1", (1, 0, 0, "a", 1)),
        ("1.0.0-alpha.1", (1, 0, 0, "a", 1)),
        ("1.0.0-a1", (1, 0, 0, "a", 1)),
        ("1.2.3rc2", (1, 2, 3, "rc", 2)),
        ("1.2.3-rc.2", (1, 2, 3, "rc", 2)),
        ("1.0.0b3", (1, 0, 0, "b", 3)),
        ("1.0.0-beta.3", (1, 0, 0, "b", 3)),
        ("v1.0.0", (1, 0, 0, "", 0)),
        ("1.0", None),
        ("banana", None),
        (None, None),
    ],
)
def test_normalize_version(raw, expected):
    assert br.normalize_version(raw) == expected


def test_prerelease_pep440_and_semver_are_consistent(tmp_path):
    root = make_repo(tmp_path, py="1.0.0a1", init="1.0.0a1", manifest="1.0.0-alpha.1", pkg="1.0.0-alpha.1")
    versions = br.read_versions(root)
    assert versions == {
        "mcp/pyproject.toml": "1.0.0a1",
        "mcp/omniapi_mcp/__init__.py": "1.0.0a1",
        "mcp/manifest.json": "1.0.0-alpha.1",
        "gui/package.json": "1.0.0-alpha.1",
        "mcp/uv.lock": "1.0.0a1",
    }
    assert br.check_versions(versions) == []


def test_lock_version_is_the_projects_own_entry(tmp_path):
    root = make_repo(tmp_path, lock="1.2.0")
    assert br.read_versions(root)["mcp/uv.lock"] == "1.2.0"  # not httpx's or pypdf's


def test_stale_lock_is_a_mismatch(tmp_path):
    # pyproject and the rest bumped, uv.lock never re-locked
    root = make_repo(tmp_path, py="1.2.0", init="1.2.0", manifest="1.2.0", pkg="1.2.0", lock="1.1.0")
    problems = br.check_versions(br.read_versions(root))
    assert any("不一致" in p for p in problems)


def test_lock_without_the_project_entry_is_reported(tmp_path):
    root = make_repo(tmp_path)
    (root / "mcp" / "uv.lock").write_text('version = 1\n\n[[package]]\nname = "httpx"\nversion = "0.28.1"\n', encoding="utf-8")
    problems = br.check_versions(br.read_versions(root))
    assert any("mcp/uv.lock" in p and "讀不到" in p for p in problems)


def test_missing_lock_is_reported(tmp_path):
    root = make_repo(tmp_path)
    (root / "mcp" / "uv.lock").unlink()
    assert any("mcp/uv.lock" in p and "讀不到" in p for p in br.check_versions(br.read_versions(root)))


def test_the_repos_own_versions_agree():
    # the real tree: catches a bump that missed one of the five places before anyone runs a build
    real = SCRIPT.parents[1]
    if not (real / "gui" / "package.json").is_file():
        pytest.skip("not a full checkout")
    assert br.check_versions(br.read_versions(real)) == []


def test_all_equal_release_is_consistent(tmp_path):
    root = make_repo(tmp_path)
    assert br.check_versions(br.read_versions(root)) == []


def test_pyproject_version_read_from_project_section_only(tmp_path):
    # [tool.hatch] 裡的 version = "9.9.9" 不能被當成專案版本
    root = make_repo(tmp_path, py="1.0.0")
    assert br.read_versions(root)["mcp/pyproject.toml"] == "1.0.0"


def test_mismatch_is_reported(tmp_path):
    root = make_repo(tmp_path, pkg="1.0.1")
    problems = br.check_versions(br.read_versions(root))
    assert any("不一致" in p for p in problems)


def test_alpha_vs_final_is_a_mismatch(tmp_path):
    root = make_repo(tmp_path, py="1.0.0a1", init="1.0.0a1", manifest="1.0.0", pkg="1.0.0")
    assert any("不一致" in p for p in br.check_versions(br.read_versions(root)))


def test_manifest_must_be_semver(tmp_path):
    # PEP 440 寫法跟其他三處「一致」，但 dxt 的 manifest 要 semver
    root = make_repo(tmp_path, py="1.0.0a1", init="1.0.0a1", manifest="1.0.0a1", pkg="1.0.0-alpha.1")
    problems = br.check_versions(br.read_versions(root))
    assert problems and all("semver" in p for p in problems)


def test_missing_version_is_reported(tmp_path):
    root = make_repo(tmp_path)
    (root / "gui" / "package.json").write_text(json.dumps({"name": "x"}), encoding="utf-8")
    problems = br.check_versions(br.read_versions(root))
    assert any("gui/package.json" in p and "讀不到" in p for p in problems)


# ---------------------------------------------------------------- 打包內容


def test_dxt_contents(tmp_path):
    root = make_repo(tmp_path / "repo")
    out = tmp_path / "out" / "omniapi-mcp.dxt"
    names = br.build_dxt(root / "mcp", out)
    with zipfile.ZipFile(out) as zf:
        assert sorted(zf.namelist()) == sorted(names)
    assert sorted(names) == sorted(
        [
            ".python-version",
            "README.md",
            "manifest.json",
            "pyproject.toml",
            "uv.lock",
            "omniapi_mcp/__init__.py",
            "omniapi_mcp/daemon/app.py",
            "omniapi_mcp/server.py",
        ]
    )


def test_dxt_requires_manifest(tmp_path):
    root = make_repo(tmp_path / "repo")
    (root / "mcp" / "manifest.json").unlink()
    with pytest.raises(SystemExit):
        br.build_dxt(root / "mcp", tmp_path / "x.dxt")


def test_skill_zip_contents(tmp_path):
    root = make_repo(tmp_path / "repo")
    out = tmp_path / "out" / "omniapi-skill.zip"
    names = br.build_skill_zip(root / "skill" / "omniapi", out)
    assert sorted(names) == ["omniapi/SKILL.md", "omniapi/references/image.md"]
    with zipfile.ZipFile(out) as zf:
        assert sorted(zf.namelist()) == sorted(names)


def test_rebuild_overwrites_instead_of_appending(tmp_path):
    root = make_repo(tmp_path / "repo")
    out = tmp_path / "out" / "omniapi-skill.zip"
    br.build_skill_zip(root / "skill" / "omniapi", out)
    br.build_skill_zip(root / "skill" / "omniapi", out)
    with zipfile.ZipFile(out) as zf:
        assert len(zf.namelist()) == 2


def test_bundle_has_versioned_top_and_gui_dist(tmp_path):
    export = tmp_path / "export"
    _write(export / "README.md", "# OmniAPI")
    _write(export / "mcp" / "pyproject.toml", "[project]\nversion = \"1.0.0\"\n")
    gui_dist = tmp_path / "gui_dist"
    _write(gui_dist / "index.html", "<html></html>")
    _write(gui_dist / "assets" / "index-abc.js", "js")
    out = tmp_path / "omniapi-v1.0.0.zip"
    names = br.build_bundle(export, gui_dist, out, "1.0.0")
    assert all(n.startswith("omniapi-v1.0.0/") for n in names)
    assert "omniapi-v1.0.0/README.md" in names
    assert "omniapi-v1.0.0/gui/dist/index.html" in names
    assert "omniapi-v1.0.0/gui/dist/assets/index-abc.js" in names


def test_bundle_refuses_export_that_already_has_gui_dist(tmp_path):
    export = tmp_path / "export"
    _write(export / "gui" / "dist" / "index.html", "stale")
    gui_dist = tmp_path / "gui_dist"
    _write(gui_dist / "index.html", "<html></html>")
    with pytest.raises(SystemExit):
        br.build_bundle(export, gui_dist, tmp_path / "b.zip", "1.0.0")


# ---------------------------------------------------------------- gui/dist 新舊


def test_gui_dist_missing(tmp_path):
    root = make_repo(tmp_path)
    assert "不存在" in br.check_gui_dist(root / "gui")


def test_gui_dist_older_than_src(tmp_path):
    root = make_repo(tmp_path)
    index = _write(root / "gui" / "dist" / "index.html", "<html></html>")
    past = time.time() - 3600
    os.utime(index, (past, past))
    problem = br.check_gui_dist(root / "gui")
    assert problem and "比原始碼舊" in problem


def test_gui_dist_fresh(tmp_path):
    root = make_repo(tmp_path)
    past = time.time() - 3600
    for p in (root / "gui" / "src").rglob("*"):
        os.utime(p, (past, past))
    os.utime(root / "gui" / "index.html", (past, past))
    _write(root / "gui" / "dist" / "index.html", "<html></html>")
    assert br.check_gui_dist(root / "gui") is None


def test_sha256_matches_hashlib(tmp_path):
    import hashlib

    p = _write(tmp_path / "f.bin", "hello")
    assert br.sha256(p) == hashlib.sha256(b"hello").hexdigest()
