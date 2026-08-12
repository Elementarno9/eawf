"""Tests for the workspace ``RegistryPane`` read-only registry listing.

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
from eawf.surfaces.tui.widgets.eu_bar import DEFAULT_RENDER_MODE
from eawf.surfaces.tui.widgets.git_pane import GitFields
from eawf.surfaces.tui.widgets.markup import escape_markup
from eawf.surfaces.tui.widgets.registry_pane import (
    CHIP_ACCENT,
    REGISTRY_EMPTY_CELL,
    REGISTRY_HINT_LINE,
    REGISTRY_UNAVAILABLE_CELL,
    RegistryPane,
    format_registry_lines,
    format_registry_markup_lines,
    load_registry_markup_rows,
    load_registry_rows,
    registry_line_sigil,
)
from eawf.surfaces.tui.widgets.sigils import Sigil, glyph, tint

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
# registry_line_sigil -- lifecycle -> sigil mapping (P30-I08-W01 reskin)
# --------------------------------------------------------------------------


def test_registry_line_sigil_active_is_running() -> None:
    """The active repo's line leads with the RUNNING diamond."""
    assert registry_line_sigil(is_active=True, is_stale=False) is Sigil.RUNNING


def test_registry_line_sigil_stale_is_abandoned() -> None:
    """A stale (non-active) entry leads with the ABANDONED circled-slash."""
    assert registry_line_sigil(is_active=False, is_stale=True) is Sigil.ABANDONED


def test_registry_line_sigil_plain_is_closed() -> None:
    """A registered-and-fresh entry leads with the CLOSED filled circle."""
    assert registry_line_sigil(is_active=False, is_stale=False) is Sigil.CLOSED


def test_registry_line_sigil_active_wins_over_stale() -> None:
    """The active flag wins over stale: an active-and-stale repo reads in-flight."""
    assert registry_line_sigil(is_active=True, is_stale=True) is Sigil.RUNNING


# --------------------------------------------------------------------------
# format_registry_markup_lines -- leading sigil + green chips
# --------------------------------------------------------------------------


def _leading_sigil_span(sigil: Sigil) -> str:
    """Return the leading tinted-sigil span a line for *sigil* opens with.

    Mirrors the pane's composition: the lifecycle glyph (from the shared
    sigils source) wrapped in its lifecycle-tint span. Built off the canonical
    :func:`glyph` / :func:`tint` helpers so the test never hard-codes a glyph
    or a hex of its own.
    """
    hue = tint(sigil)
    mark = glyph(sigil, mode=DEFAULT_RENDER_MODE)
    return f"[{hue}]{mark}[/]" if hue is not None else f"[$muted]{mark}[/]"


def _markup_by_code(lines: list[str]) -> dict[str, str]:
    """Index *lines* by repo code (the token after the leading ``[/] `` span)."""
    return {line.split("[/] ", 1)[1].split("  ", 1)[0]: line for line in lines}


def test_format_registry_markup_lines_leads_with_lifecycle_sigil() -> None:
    """Each repo line leads with the I02 lifecycle sigil span for its state.

    The active repo wears the RUNNING glyph, the stale entry the ABANDONED
    glyph, and a plain entry the CLOSED glyph -- each drawn from the shared
    sigils source (not a glyph invented in this pane).
    """
    registry = _registry(["ABC", "DEF", "GHI"], active_code="ABC")
    lines = format_registry_markup_lines(
        registry,
        is_stale_at={"DEF": True},
        mode=DEFAULT_RENDER_MODE,
    )
    by_code = _markup_by_code(lines)
    assert by_code["ABC"].startswith(_leading_sigil_span(Sigil.RUNNING))
    assert by_code["DEF"].startswith(_leading_sigil_span(Sigil.ABANDONED))
    assert by_code["GHI"].startswith(_leading_sigil_span(Sigil.CLOSED))


def test_format_registry_markup_lines_tints_sigil_with_lifecycle_hue() -> None:
    """The leading sigil is wrapped in its lifecycle tint span from the COLOUR layer."""
    registry = _registry(["ABC"])
    (line,) = format_registry_markup_lines(registry, is_stale_at={}, mode=DEFAULT_RENDER_MODE)
    hue = tint(Sigil.CLOSED)
    assert hue is not None
    assert line.startswith(f"[{hue}]")


def test_format_registry_markup_lines_active_chip_is_green_accent() -> None:
    """The ``(active)`` chip renders in the green accent palette span."""
    registry = _registry(["ABC", "DEF"], active_code="DEF")
    lines = format_registry_markup_lines(registry, is_stale_at={}, mode=DEFAULT_RENDER_MODE)
    by_code = _markup_by_code(lines)
    assert f"[{CHIP_ACCENT}](active)[/]" in by_code["DEF"]
    assert "(active)" not in by_code["ABC"]


def test_format_registry_markup_lines_stale_chip_is_green_accent() -> None:
    """The ``(stale)`` chip renders in the green accent palette span."""
    registry = _registry(["ABC", "DEF"])
    lines = format_registry_markup_lines(
        registry, is_stale_at={"ABC": True, "DEF": False}, mode=DEFAULT_RENDER_MODE
    )
    by_code = _markup_by_code(lines)
    assert f"[{CHIP_ACCENT}](stale)[/]" in by_code["ABC"]
    assert "(stale)" not in by_code["DEF"]


def test_format_registry_markup_lines_escapes_path_brackets() -> None:
    """A path with a ``[`` is backslash-escaped, never parsed as a style tag."""
    registry = Registry(
        version="1",
        repos={"ABC": RegistryRepoEntry(code="ABC", path="/abs/[x]/abc", title="ABC repo")},
    )
    (line,) = format_registry_markup_lines(registry, is_stale_at={}, mode=DEFAULT_RENDER_MODE)
    assert "/abs/\\[x]/abc" in line


def test_format_registry_markup_lines_honest_empty_is_byte_identical() -> None:
    """The honest-empty markup lines are byte-identical to the escaped plain lines."""
    registry = Registry(version="1", repos={})
    plain = format_registry_lines(registry, is_stale_at={})
    markup = format_registry_markup_lines(registry, is_stale_at={}, mode=DEFAULT_RENDER_MODE)
    assert markup == [escape_markup(line) for line in plain]
    assert markup == [REGISTRY_EMPTY_CELL, REGISTRY_HINT_LINE]


def test_format_registry_markup_lines_unavailable_is_byte_identical() -> None:
    """The unavailable markup line is byte-identical to the escaped plain line."""
    plain = format_registry_lines(None, is_stale_at={})
    markup = format_registry_markup_lines(None, is_stale_at={}, mode=DEFAULT_RENDER_MODE)
    assert markup == [escape_markup(line) for line in plain]
    assert markup == [REGISTRY_UNAVAILABLE_CELL]


def test_load_registry_markup_rows_reads_explicit_registry(tmp_path: Path) -> None:
    """The markup loader leads each on-disk entry with its lifecycle sigil + green chip."""
    _write_registry(
        tmp_path,
        {"ABC": {"code": "ABC", "path": "/abs/path/abc", "title": "ABC repo"}},
        active_code="ABC",
    )
    (line,) = load_registry_markup_rows(home=tmp_path)
    assert line.startswith(f"[{tint(Sigil.RUNNING)}]")
    assert f"[{CHIP_ACCENT}](active)[/]" in line


def test_load_registry_markup_rows_unavailable_is_escaped_plain(tmp_path: Path) -> None:
    """No registry under *home* yields the escaped unavailable placeholder, unchanged."""
    assert load_registry_markup_rows(home=tmp_path) == [escape_markup(REGISTRY_UNAVAILABLE_CELL)]


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


def test_workspace_dashboard_registry_pane_paints_sigils_and_chips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mounted pane paints the lifecycle sigil glyphs + the chip text.

    Asserts the painted content (the markup tags consumed by the renderer)
    carries the active repo's RUNNING glyph and a stale repo's ABANDONED
    glyph, plus the ``(active)`` / ``(stale)`` chip text -- the
    cosmic-terminal reskin landing on the live surface, not just in the pure
    formatter. The fixture's home has no per-repo ``.ea/state.json`` for
    either path, so both entries flag stale; ABC is also active.
    """
    _write_registry(
        tmp_path,
        {
            "ABC": {"code": "ABC", "path": str(tmp_path / "abc"), "title": "ABC repo"},
            "DEF": {"code": "DEF", "path": str(tmp_path / "def"), "title": "DEF repo"},
        },
        active_code="ABC",
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            pane = app.screen.query_one(RegistryPane)
            mode = pane._render_mode()
            painted = str(pane.render())
            # ABC is active -> RUNNING glyph; DEF is stale-only -> ABANDONED glyph.
            assert glyph(Sigil.RUNNING, mode=mode) in painted
            assert glyph(Sigil.ABANDONED, mode=mode) in painted
            assert "(active)" in painted
            assert "(stale)" in painted

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
