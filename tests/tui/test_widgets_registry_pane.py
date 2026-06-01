"""Tests for the workspace ``RegistryPane`` read-only registry listing (P29-I02-W24).

The registry pane lists the explicit ``~/.eawf/registry.json`` entries
under the workspace dashboard: one ``CODE  title  path  [chips]`` line per
registered repo, honest-empty when the registry has zero repos, and an
unavailable placeholder when the file is missing / corrupt. The pane reads
ONLY the registry file -- never a filesystem scan/walk -- so the
explicit-registry-only rule is upheld.

Two test tiers:

* Pure helpers (:func:`format_registry_lines`, :func:`load_registry_rows`)
  are unit-tested by feeding a :class:`Registry` / a ``tmp_path`` registry
  directly, no Pilot / app mount.
* A Pilot tier mounts the workspace screen against the workspace fixture
  with a ``tmp_path``-rooted registry and asserts the dashboard surfaces the
  registry entries (and the honest-empty placeholder at N=0). Determinism:
  every git-probing launch awaits ``app.workers.wait_for_complete()`` (the
  project Pilot-worker rule). Repo codes are abstract placeholders
  (ABC / DEF / GHI), never real-looking project names.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import orjson
import pytest

from eawf.platform.registry import Registry, RegistryRepoEntry
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.scopes import WorkspaceScreen
from eawf.surfaces.tui.widgets.git_pane import GitFields
from eawf.surfaces.tui.widgets.registry_pane import (
    REGISTRY_EMPTY_CELL,
    REGISTRY_HINT_LINE,
    REGISTRY_UNAVAILABLE_CELL,
    RegistryPane,
    format_registry_lines,
    load_registry_rows,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_WORKSPACE = _FIXTURES / "05-workspace-state.json"


@pytest.fixture(autouse=True)
def _stub_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the workspace table's git probe to a deterministic clean tree."""
    monkeypatch.setattr(
        "eawf.surfaces.tui.widgets.workspace_table.gather_git_fields",
        lambda _path: GitFields(
            branch="main", dirty="clean", ahead_behind="up-to-date", recent_commits=()
        ),
    )


def _write_registry(
    home: Path, repos: dict[str, dict[str, str]], *, active_code: str | None = None
) -> Path:
    """Write a ``registry.json`` under *home*/.eawf and return its path."""
    ea_dir = home / ".eawf"
    ea_dir.mkdir(parents=True, exist_ok=True)
    path = ea_dir / "registry.json"
    payload: dict[str, object] = {"version": "1", "repos": repos}
    if active_code is not None:
        payload["active_code"] = active_code
    path.write_bytes(orjson.dumps(payload))
    return path


def _registry(codes: list[str], *, active_code: str | None = None) -> Registry:
    """Build a :class:`Registry` over abstract repo *codes*."""
    return Registry(
        version="1",
        active_code=active_code,
        repos={
            code: RegistryRepoEntry(
                code=code, path=f"/abs/path/{code.lower()}", title=f"{code} repo"
            )
            for code in codes
        },
    )


# --------------------------------------------------------------------------
# format_registry_lines -- ordered, chipped listing
# --------------------------------------------------------------------------


def test_format_registry_lines_orders_entries_by_code() -> None:
    """Entries render in code order regardless of insertion order."""
    registry = _registry(["DEF", "ABC", "GHI"])
    lines = format_registry_lines(registry, is_stale_at={})
    codes = [line.split("  ", 1)[0] for line in lines]
    assert codes == ["ABC", "DEF", "GHI"]


def test_format_registry_lines_includes_code_title_and_path() -> None:
    """Each line carries the repo code, its title, and its on-disk path."""
    registry = _registry(["ABC"])
    (line,) = format_registry_lines(registry, is_stale_at={})
    assert line.startswith("ABC")
    assert "ABC repo" in line
    assert "/abs/path/abc" in line


def test_format_registry_lines_marks_active_repo() -> None:
    """The active repo carries an ``(active)`` chip; others do not."""
    registry = _registry(["ABC", "DEF"], active_code="DEF")
    lines = format_registry_lines(registry, is_stale_at={})
    by_code = {line.split("  ", 1)[0]: line for line in lines}
    assert "(active)" in by_code["DEF"]
    assert "(active)" not in by_code["ABC"]


def test_format_registry_lines_marks_stale_repo() -> None:
    """A stale entry carries a ``(stale)`` chip from the passed flags."""
    registry = _registry(["ABC", "DEF"])
    lines = format_registry_lines(registry, is_stale_at={"ABC": True, "DEF": False})
    by_code = {line.split("  ", 1)[0]: line for line in lines}
    assert "(stale)" in by_code["ABC"]
    assert "(stale)" not in by_code["DEF"]


def test_format_registry_lines_falls_back_to_code_when_title_missing() -> None:
    """An entry without a title renders its code in the title slot."""
    registry = Registry(
        version="1",
        repos={"ABC": RegistryRepoEntry(code="ABC", path="/abs/path/abc", title=None)},
    )
    (line,) = format_registry_lines(registry, is_stale_at={})
    assert "ABC  ABC  /abs/path/abc" in line


# --------------------------------------------------------------------------
# format_registry_lines -- honest-empty + unavailable (boundary)
# --------------------------------------------------------------------------


def test_format_registry_lines_zero_repos_is_honest_empty() -> None:
    """A registry with zero repos renders the placeholder + explicit hint."""
    lines = format_registry_lines(Registry(version="1", repos={}), is_stale_at={})
    assert lines == [REGISTRY_EMPTY_CELL, REGISTRY_HINT_LINE]


def test_format_registry_lines_none_registry_is_unavailable() -> None:
    """A ``None`` registry (unavailable) renders the unavailable placeholder."""
    assert format_registry_lines(None, is_stale_at={}) == [REGISTRY_UNAVAILABLE_CELL]


def test_format_registry_lines_single_repo_renders_one_line() -> None:
    """The N=1 case renders exactly one entry line (not a fallback panel)."""
    lines = format_registry_lines(_registry(["ABC"]), is_stale_at={})
    assert len(lines) == 1
    assert lines[0].startswith("ABC")


# --------------------------------------------------------------------------
# load_registry_rows -- read-only resolution over a tmp registry
# --------------------------------------------------------------------------


def test_load_registry_rows_reads_explicit_registry(tmp_path: Path) -> None:
    """The loader renders the entries of an on-disk registry under *home*."""
    _write_registry(
        tmp_path,
        {
            "ABC": {"code": "ABC", "path": "/abs/path/abc", "title": "ABC repo"},
            "DEF": {"code": "DEF", "path": "/abs/path/def", "title": "DEF repo"},
        },
    )
    lines = load_registry_rows(home=tmp_path)
    codes = [line.split("  ", 1)[0] for line in lines]
    assert codes == ["ABC", "DEF"]


def test_load_registry_rows_missing_registry_is_unavailable(tmp_path: Path) -> None:
    """No registry file under *home* yields the unavailable placeholder."""
    assert load_registry_rows(home=tmp_path) == [REGISTRY_UNAVAILABLE_CELL]


def test_load_registry_rows_empty_registry_is_honest_empty(tmp_path: Path) -> None:
    """A present-but-empty registry yields the honest-empty placeholder + hint."""
    _write_registry(tmp_path, {})
    assert load_registry_rows(home=tmp_path) == [REGISTRY_EMPTY_CELL, REGISTRY_HINT_LINE]


def test_load_registry_rows_marks_stale_entry_by_missing_state(tmp_path: Path) -> None:
    """An entry whose repo path has no ``state.json`` resolves as stale.

    The staleness OR-chain treats a missing per-repo ``state.json`` as
    stale (branch (c)), so an entry pointing at a path with no
    ``.ea/state.json`` renders the ``(stale)`` chip -- exercised here
    through the real :func:`~eawf.platform.registry.is_stale` boundary the
    loader uses (no monkeypatching of the staleness call).
    """
    _write_registry(
        tmp_path, {"ABC": {"code": "ABC", "path": str(tmp_path / "abc"), "title": "ABC repo"}}
    )
    (line,) = load_registry_rows(home=tmp_path, now=datetime.now(UTC))
    assert "(stale)" in line


# --------------------------------------------------------------------------
# NO-SCAN invariant -- the pane reads only the registry, never the filesystem
# --------------------------------------------------------------------------


def test_load_registry_rows_does_not_scan_filesystem_for_repos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repo dir physically present but absent from the registry is NOT listed.

    The explicit-registry-only contract: the pane resolves repos solely
    from ``~/.eawf/registry.json``. Here the registry names only ABC, while
    a sibling on-disk ``ghi`` repo (with its own ``.ea/state.json``) sits
    right next to it. If the loader walked the filesystem it would surface
    GHI; reading only the registry must not. We additionally hard-fail on
    any directory-walk syscall to pin the no-scan invariant structurally.
    """
    # A real on-disk repo that is NOT registered.
    ghi_state = tmp_path / "ghi" / ".ea"
    ghi_state.mkdir(parents=True)
    (ghi_state / "state.json").write_bytes(b"{}")
    _write_registry(
        tmp_path, {"ABC": {"code": "ABC", "path": str(tmp_path / "abc"), "title": "ABC repo"}}
    )

    # Trip-wire: any filesystem-walk syscall on the registry read path fails.
    def _no_walk(*_a: object, **_k: object) -> None:
        raise AssertionError("registry resolution must not scan/walk the filesystem")

    monkeypatch.setattr("os.walk", _no_walk)
    monkeypatch.setattr("os.scandir", _no_walk)

    lines = load_registry_rows(home=tmp_path)
    codes = [line.split("  ", 1)[0] for line in lines]
    assert codes == ["ABC"]
    assert "GHI" not in codes


# --------------------------------------------------------------------------
# Pilot tier -- workspace dashboard surfaces the registry pane
# --------------------------------------------------------------------------


def test_workspace_dashboard_renders_registry_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The workspace dashboard's registry pane lists the registered repos."""
    _write_registry(
        tmp_path,
        {
            "ABC": {"code": "ABC", "path": "/abs/path/abc", "title": "ABC repo"},
            "DEF": {"code": "DEF", "path": "/abs/path/def", "title": "DEF repo"},
        },
        active_code="ABC",
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            screen = app.screen
            assert isinstance(screen, WorkspaceScreen)
            pane = screen.query_one(RegistryPane)
            rendered = pane.rendered_text()
            assert "ABC" in rendered
            assert "DEF" in rendered
            assert "(active)" in rendered

    asyncio.run(body())


def test_workspace_dashboard_registry_pane_honest_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no registered repos the registry pane shows the honest-empty line."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            pane = app.screen.query_one(RegistryPane)
            rendered = pane.rendered_text()
            assert REGISTRY_UNAVAILABLE_CELL in rendered

    asyncio.run(body())


def test_workspace_dashboard_registry_pane_lists_only_registry_not_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pane lists the registry's repos, not the bound workspace index's.

    The workspace fixture's ``state.workspace`` carries repo ``QR``; the
    registry under *home* carries only ``ABC``. The registry pane reflects
    the registry (ABC), proving it reads the explicit registry rather than
    re-deriving repos from the bound workspace state (which the per-repo
    table renders separately).
    """
    _write_registry(
        tmp_path, {"ABC": {"code": "ABC", "path": "/abs/path/abc", "title": "ABC repo"}}
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            pane = app.screen.query_one(RegistryPane)
            rendered = pane.rendered_text()
            assert "ABC" in rendered
            assert "QR" not in rendered

    asyncio.run(body())


def test_workspace_dashboard_registry_chip_for_stale_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An entry stale by the OR-chain renders the ``(stale)`` chip in the pane.

    The entry points at a path with no ``.ea/state.json``, so the
    staleness OR-chain's branch (c) (missing per-repo state) fires and the
    pane renders the ``(stale)`` chip.
    """
    _write_registry(
        tmp_path, {"ABC": {"code": "ABC", "path": str(tmp_path / "abc"), "title": "ABC repo"}}
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            pane = app.screen.query_one(RegistryPane)
            assert "(stale)" in pane.rendered_text()

    asyncio.run(body())
