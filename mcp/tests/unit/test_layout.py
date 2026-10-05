"""1.3-M2: where things are (repo layout unchanged, installed layout relocatable)."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from omniapi_mcp import layout
from omniapi_mcp.config.settings import Settings


def _pkg(tmp_path: Path, *, repo: bool) -> Path:
    """A fake package folder; ``repo`` puts a pyproject.toml beside it and a built GUI two levels up."""
    root = tmp_path / ("repo" if repo else "app")
    pkg = root / "mcp" / "omniapi_mcp"
    pkg.mkdir(parents=True)
    if repo:
        (root / "mcp" / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        dist = root / "gui" / "dist"
        dist.mkdir(parents=True)
        (dist / "index.html").write_text("<html>repo</html>", encoding="utf-8")
    return pkg


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIAPI_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(layout.GUI_DIST_ENV, raising=False)


def test_this_checkout_is_a_repo_layout():
    # the tests run from mcp/ in a checkout: nothing about the running service changes
    assert layout.is_repo_layout()
    assert layout.project_dir() == Path(layout.__file__).resolve().parent.parent
    assert layout.service_workdir() == layout.project_dir()
    assert layout.default_storage_dir() == Path("./storage")


def test_repo_vs_installed_detection(tmp_path):
    repo, app = _pkg(tmp_path, repo=True), _pkg(tmp_path, repo=False)
    assert layout.layout_name(repo) == "repo" and layout.layout_name(app) == "installed"
    assert layout.service_workdir(repo) == repo.parent
    assert layout.service_workdir(app) == tmp_path / "home"


def test_gui_dist_env_first_then_repo_then_none(tmp_path, monkeypatch):
    repo, app = _pkg(tmp_path, repo=True), _pkg(tmp_path, repo=False)
    assert layout.gui_dist(repo) == repo.parent.parent / "gui" / "dist"
    assert layout.gui_dist(app) is None  # installed without OMNIAPI_GUI_DIST: the status page

    own = tmp_path / "elsewhere" / "dist"
    own.mkdir(parents=True)
    (own / "index.html").write_text("<html>own</html>", encoding="utf-8")
    monkeypatch.setenv(layout.GUI_DIST_ENV, str(own))
    assert layout.gui_dist(app) == own.resolve()
    assert layout.gui_dist(repo) == own.resolve()  # the variable wins over the repo's build


def test_desktop_install_finds_gui_next_to_its_python(tmp_path, monkeypatch):
    # 1.3-M6: <install>\python is sys.prefix, <install>\gui the GUI (omni.cmd's `omni serve` has no OMNIAPI_GUI_DIST)
    app = _pkg(tmp_path, repo=False)
    install = tmp_path / "Omni App"
    (install / "python").mkdir(parents=True)
    monkeypatch.setattr(layout.sys, "prefix", str(install / "python"))
    assert layout.gui_dist(app) is None
    (install / "gui").mkdir()
    (install / "gui" / "index.html").write_text("<html>installed</html>", encoding="utf-8")
    assert layout.gui_dist(app) == (install / "gui").resolve()
    repo = _pkg(tmp_path, repo=True)
    assert layout.gui_dist(repo) == repo.parent.parent / "gui" / "dist"  # a checkout never looks there

    monkeypatch.setenv(layout.GUI_DIST_ENV, str(tmp_path / "missing"))
    assert layout.gui_dist(repo) == repo.parent.parent / "gui" / "dist"  # a bad value falls through


def test_env_file_candidates_are_one_list_for_both_entry_points(tmp_path, monkeypatch):
    repo, app = _pkg(tmp_path, repo=True), _pkg(tmp_path, repo=False)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    home = tmp_path / "home"
    assert layout.env_file_candidates(repo) == [repo.parent / ".env", cwd / ".env", home / ".env"]
    assert layout.env_file_candidates(app) == [cwd / ".env", home / ".env"]

    assert layout.env_file(repo) is None
    (home / ".env").write_text("X=1\n", encoding="utf-8")
    assert layout.env_file(repo) == home / ".env"
    (cwd / ".env").write_text("X=2\n", encoding="utf-8")  # what the stdio server always read
    assert layout.env_file(repo) == cwd / ".env"
    (repo.parent / ".env").write_text("X=3\n", encoding="utf-8")  # what the daemon always read first
    assert layout.env_file(repo) == repo.parent / ".env"


def test_env_file_candidates_drop_duplicates(tmp_path, monkeypatch):
    repo = _pkg(tmp_path, repo=True)
    monkeypatch.chdir(repo.parent)  # the daemon's own cwd in a repo is mcp/
    assert layout.env_file_candidates(repo) == [repo.parent / ".env", tmp_path / "home" / ".env"]


def test_installed_works_folder_defaults_to_documents(tmp_path, monkeypatch):
    repo, app = _pkg(tmp_path, repo=True), _pkg(tmp_path, repo=False)
    docs = tmp_path / "Docs"
    monkeypatch.setattr(layout, "documents_dir", lambda: docs)
    monkeypatch.delenv("STORAGE__BASE_PATH", raising=False)

    s = Settings(_env_file=None)
    assert layout.apply_storage_default(s, app) == str(docs / "OmniAPI")
    assert s.storage.base_path == str(docs / "OmniAPI")

    s = Settings(_env_file=None)
    assert layout.apply_storage_default(s, repo) is None  # repo: ./storage as always
    assert s.storage.base_path == "./storage"

    monkeypatch.setenv("STORAGE__BASE_PATH", str(tmp_path / "mine"))
    s = Settings(_env_file=None)
    assert layout.apply_storage_default(s, app) is None  # an explicit choice is kept
    assert Path(s.storage.base_path) == (tmp_path / "mine").resolve()


def test_documents_dir_comes_from_the_os():
    d = layout.documents_dir()
    assert isinstance(d, Path) and d.is_absolute()
    if os.name == "nt":
        assert d.exists()


def test_runtime_applies_the_installed_default_before_the_sandbox_redirect(tmp_path, monkeypatch):
    # an offline sandbox still writes under its own data home, whatever the layout
    import asyncio

    from omniapi_mcp import runtime as runtime_mod
    from omniapi_mcp.runtime import Runtime

    monkeypatch.setenv("OMNIAPI_OFFLINE", "1")
    monkeypatch.setenv("OMNIAPI_DEV", "1")
    monkeypatch.setattr(layout, "PACKAGE_DIR", _pkg(tmp_path, repo=False))
    monkeypatch.setattr(layout, "documents_dir", lambda: tmp_path / "Docs")
    monkeypatch.setattr(runtime_mod.catalog, "refresh", lambda *a, **k: asyncio.sleep(0, result={}))
    monkeypatch.delenv("STORAGE__BASE_PATH", raising=False)

    async def go():
        rt = Runtime()
        ctx = await rt.acquire(Settings(_env_file=None))
        try:
            return ctx.settings.storage.base_path
        finally:
            await rt.release()

    assert asyncio.run(go()) == str(tmp_path / "home" / "storage")


def test_describe_reports_the_layout(monkeypatch):
    info = layout.describe()
    assert info["layout"] == "repo" and info["package_dir"] == str(layout.PACKAGE_DIR)
    assert set(info) >= {"gui_dist", "env_file", "workdir"}


def test_apply_storage_default_tolerates_test_doubles(tmp_path, monkeypatch):
    monkeypatch.setattr(layout, "documents_dir", lambda: tmp_path / "Docs")
    s = NS(storage=NS(base_path="./storage"))
    assert layout.apply_storage_default(s, _pkg(tmp_path, repo=False)) == str(tmp_path / "Docs" / "OmniAPI")
