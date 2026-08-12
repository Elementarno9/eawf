"""Tests for the advisory cross-repo PR overlay.

Three layers, mirroring the single-repo ``/pr`` suite:

* the pure cross-repo group builder (:func:`gather_cross_repo_prs`) that
  resolves the repo set read-only from the explicit registry and fetches
  each repo's open PRs through an injected stub -- so the grouping +
  per-repo degradation are unit-testable without a live ``gh`` or the
  network, and the no-scan invariant is pinned structurally;
* Pilot-driven mounting + grouping + row-selection of
  :class:`CrossRepoPrModal` (the read-only guarantee is asserted by
  checking no binding mutates);
* the ``/prs`` palette verb path, where the per-repo ``gh`` shell-out is
  stubbed at the ``subprocess`` boundary and the worker is drained via
  ``app.workers.wait_for_complete()`` (the project Pilot-worker rule).

Repo codes are abstract placeholders (ABC / DEF / GHI), never real-looking
project names, and no real PR data / PII appears in any fixture.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import orjson
import pytest
from rich.text import Text
from textual.widgets import Static

from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.screens.overlays import cross_repo_pr as xpr_mod
from eawf.surfaces.tui.screens.overlays.cross_repo_pr import (
    CrossRepoGroup,
    CrossRepoPrModal,
    gather_cross_repo_prs,
    open_cross_repo_pr,
    total_open_prs,
)
from eawf.surfaces.tui.screens.overlays.pr_list import (
    PrFetch,
    PrFetchStatus,
    PrRow,
    reset_pr_cache,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"
_WORKSPACE = _FIXTURES / "05-workspace-state.json"


@pytest.fixture(autouse=True)
def _clear_pr_cache() -> Any:
    """Reset the single-repo open-PR TTL cache around every test (xdist hygiene)."""
    reset_pr_cache()
    yield
    reset_pr_cache()


def _completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> SimpleNamespace:
    """Build a stand-in ``subprocess.run`` result."""
    return SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)


def _text(widget: Static) -> str:
    rendered = widget.render()
    return rendered.plain if isinstance(rendered, Text) else str(rendered)


def _write_registry(home: Path, repos: dict[str, dict[str, str]]) -> Path:
    """Write a ``registry.json`` under *home*/.eawf and return its path."""
    ea_dir = home / ".eawf"
    ea_dir.mkdir(parents=True, exist_ok=True)
    path = ea_dir / "registry.json"
    path.write_bytes(orjson.dumps({"version": "1", "repos": repos}))
    return path


def _entry(code: str) -> dict[str, str]:
    """Build one registry entry dict for abstract *code*."""
    return {"code": code, "path": f"/abs/path/{code.lower()}", "title": f"{code} repo"}


def _ok(*numbers: int) -> PrFetch:
    """Build an OK :class:`PrFetch` carrying PR rows for *numbers*."""
    return PrFetch(
        rows=tuple(
            PrRow(n, f"fix {n}", "alice", "OPEN", f"https://example.test/{n}") for n in numbers
        ),
        status=PrFetchStatus.OK,
    )


_UNAVAILABLE = PrFetch(rows=(), status=PrFetchStatus.UNAVAILABLE)


# --------------------------------------------------------------------------
# gather_cross_repo_prs -- pure grouping over the explicit registry
# --------------------------------------------------------------------------


def test_gather_cross_repo_prs_groups_n_repos_in_code_order(tmp_path: Path) -> None:
    """N registered repos produce N groups, ordered by repo code."""
    _write_registry(tmp_path, {"DEF": _entry("DEF"), "ABC": _entry("ABC"), "GHI": _entry("GHI")})

    def fetcher(cwd: Path) -> PrFetch:
        return _ok(1)

    groups = gather_cross_repo_prs(home=tmp_path, fetcher=fetcher)
    assert [g.code for g in groups] == ["ABC", "DEF", "GHI"]


def test_gather_cross_repo_prs_fetches_each_repo_path(tmp_path: Path) -> None:
    """Each repo's own on-disk path is passed to the per-repo fetcher."""
    _write_registry(tmp_path, {"ABC": _entry("ABC"), "DEF": _entry("DEF")})
    seen: list[str] = []

    def fetcher(cwd: Path) -> PrFetch:
        seen.append(str(cwd))
        return _ok(1)

    gather_cross_repo_prs(home=tmp_path, fetcher=fetcher)
    assert sorted(seen) == ["/abs/path/abc", "/abs/path/def"]


def test_gather_cross_repo_prs_per_repo_rows_grouped(tmp_path: Path) -> None:
    """Each group carries that repo's own PR rows (per-repo grouping)."""
    _write_registry(tmp_path, {"ABC": _entry("ABC"), "DEF": _entry("DEF")})
    fetches = {"/abs/path/abc": _ok(11, 12), "/abs/path/def": _ok(20)}

    def fetcher(cwd: Path) -> PrFetch:
        return fetches[str(cwd)]

    groups = gather_cross_repo_prs(home=tmp_path, fetcher=fetcher)
    by_code = {g.code: g for g in groups}
    assert [r.number for r in by_code["ABC"].rows] == [11, 12]
    assert [r.number for r in by_code["DEF"].rows] == [20]


def test_gather_cross_repo_prs_failing_repo_is_unavailable_not_fatal(tmp_path: Path) -> None:
    """A repo whose fetch is UNAVAILABLE keeps its group; others still resolve."""
    _write_registry(tmp_path, {"ABC": _entry("ABC"), "DEF": _entry("DEF")})

    def fetcher(cwd: Path) -> PrFetch:
        return _UNAVAILABLE if str(cwd) == "/abs/path/abc" else _ok(20)

    groups = gather_cross_repo_prs(home=tmp_path, fetcher=fetcher)
    by_code = {g.code: g for g in groups}
    assert by_code["ABC"].status is PrFetchStatus.UNAVAILABLE
    assert by_code["ABC"].rows == ()
    assert by_code["DEF"].status is PrFetchStatus.OK
    assert [r.number for r in by_code["DEF"].rows] == [20]


def test_gather_cross_repo_prs_title_falls_back_to_code(tmp_path: Path) -> None:
    """An entry without a title uses its code as the group title."""
    _write_registry(tmp_path, {"ABC": {"code": "ABC", "path": "/abs/path/abc"}})
    groups = gather_cross_repo_prs(home=tmp_path, fetcher=lambda _cwd: _ok(1))
    assert groups[0].title == "ABC"


def test_gather_cross_repo_prs_missing_registry_is_empty(tmp_path: Path) -> None:
    """No registry file under *home* yields no groups (host shows honest-empty)."""
    assert gather_cross_repo_prs(home=tmp_path, fetcher=lambda _cwd: _ok(1)) == ()


def test_gather_cross_repo_prs_zero_repos_is_empty(tmp_path: Path) -> None:
    """A present-but-empty registry yields no groups."""
    _write_registry(tmp_path, {})
    assert gather_cross_repo_prs(home=tmp_path, fetcher=lambda _cwd: _ok(1)) == ()


def test_gather_cross_repo_prs_does_not_scan_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The repo set comes from the registry only -- never a filesystem walk.

    A sibling on-disk repo absent from the registry must NOT be surfaced,
    and any directory-walk syscall on the resolution path hard-fails to pin
    the no-scan invariant structurally.
    """
    ghi_state = tmp_path / "ghi" / ".ea"
    ghi_state.mkdir(parents=True)
    (ghi_state / "state.json").write_bytes(b"{}")
    _write_registry(tmp_path, {"ABC": _entry("ABC")})

    def _no_walk(*_a: object, **_k: object) -> None:
        raise AssertionError("cross-repo PR resolution must not scan/walk the filesystem")

    monkeypatch.setattr("os.walk", _no_walk)
    monkeypatch.setattr("os.scandir", _no_walk)

    groups = gather_cross_repo_prs(home=tmp_path, fetcher=lambda _cwd: _ok(1))
    assert [g.code for g in groups] == ["ABC"]


def test_total_open_prs_sums_across_groups() -> None:
    """The aggregate count sums every group's rows."""
    groups = (
        CrossRepoGroup("ABC", "ABC repo", _ok(1, 2).rows, PrFetchStatus.OK),
        CrossRepoGroup("DEF", "DEF repo", (), PrFetchStatus.UNAVAILABLE),
        CrossRepoGroup("GHI", "GHI repo", _ok(9).rows, PrFetchStatus.OK),
    )
    assert total_open_prs(groups) == 3


def test_cross_repo_group_is_frozen() -> None:
    """A group row is immutable (frozen dataclass)."""
    group = CrossRepoGroup("ABC", "ABC repo", (), PrFetchStatus.OK)
    with pytest.raises((AttributeError, TypeError)):
        group.code = "DEF"  # type: ignore[misc]


# --------------------------------------------------------------------------
# CrossRepoPrModal -- mounting, grouping, selection (Pilot)
# --------------------------------------------------------------------------

_GROUPS = (
    CrossRepoGroup("ABC", "ABC repo", _ok(11, 12).rows, PrFetchStatus.OK),
    CrossRepoGroup("DEF", "DEF repo", _ok(20).rows, PrFetchStatus.OK),
)


def test_cross_repo_modal_mounts_group_headers_per_repo() -> None:
    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(CrossRepoPrModal(_GROUPS))
            await pilot.pause()
            assert isinstance(app.screen, CrossRepoPrModal)
            assert "ABC" in _text(app.screen.query_one("#xpr-group-ABC", Static))
            assert "DEF" in _text(app.screen.query_one("#xpr-group-DEF", Static))

    asyncio.run(body())


def test_cross_repo_modal_lists_prs_across_repos() -> None:
    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(CrossRepoPrModal(_GROUPS))
            await pilot.pause()
            rows = app.screen.query_one("#xpr-list").query(".xpr-row")
            assert len(rows) == 3  # 11, 12 (ABC) + 20 (DEF)
            assert "#11" in _text(app.screen.query_one("#xpr-row-0", Static))
            assert "#20" in _text(app.screen.query_one("#xpr-row-2", Static))

    asyncio.run(body())


def test_cross_repo_modal_unavailable_repo_shows_honest_header() -> None:
    """A failing repo renders an ``(unavailable)`` header + hint, not a crash.

    The OK sibling's PR row must still render -- one bad repo never breaks
    the view.
    """

    async def body() -> None:
        groups = (
            CrossRepoGroup("ABC", "ABC repo", (), PrFetchStatus.UNAVAILABLE),
            CrossRepoGroup("DEF", "DEF repo", _ok(20).rows, PrFetchStatus.OK),
        )
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(CrossRepoPrModal(groups))
            await pilot.pause()
            assert isinstance(app.screen, CrossRepoPrModal)
            assert "unavailable" in _text(app.screen.query_one("#xpr-group-ABC", Static))
            # The healthy repo still lists its PR.
            rows = app.screen.query_one("#xpr-list").query(".xpr-row")
            assert len(rows) == 1
            assert "#20" in _text(app.screen.query_one("#xpr-row-0", Static))

    asyncio.run(body())


def test_cross_repo_modal_selection_starts_at_first_row() -> None:
    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(CrossRepoPrModal(_GROUPS))
            await pilot.pause()
            assert app.screen.selected == 0
            assert app.screen.query_one("#xpr-row-0", Static).has_class("-selected")

    asyncio.run(body())


def test_cross_repo_modal_down_moves_selection_across_repos() -> None:
    """``down`` walks the flattened rows, crossing the repo-group boundary."""

    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(CrossRepoPrModal(_GROUPS))
            await pilot.pause()
            await pilot.press("down")
            await pilot.press("down")  # cross from ABC (#11,#12) into DEF (#20)
            await pilot.pause()
            assert app.screen.selected == 2
            assert app.screen.query_one("#xpr-row-2", Static).has_class("-selected")

    asyncio.run(body())


def test_cross_repo_modal_selection_clamps_at_bottom() -> None:
    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(CrossRepoPrModal(_GROUPS))
            await pilot.pause()
            for _ in range(6):  # over-scroll past the last row
                await pilot.press("down")
                await pilot.pause()
            assert app.screen.selected == 2

    asyncio.run(body())


def test_cross_repo_modal_no_repos_is_honest_empty() -> None:
    """Zero registered repos renders the no-repos placeholder, selection -1."""

    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(CrossRepoPrModal(()))
            await pilot.pause()
            empty = app.screen.query_one("#xpr-list").query(".xpr-empty")
            assert empty
            assert "no repos registered" in _text(empty.first(Static))
            assert app.screen.selected == -1

    asyncio.run(body())


def test_cross_repo_modal_repos_but_no_prs_is_honest_empty() -> None:
    """Registered repos with zero open PRs renders the no-PRs placeholder."""

    async def body() -> None:
        groups = (
            CrossRepoGroup("ABC", "ABC repo", (), PrFetchStatus.OK),
            CrossRepoGroup("DEF", "DEF repo", (), PrFetchStatus.OK),
        )
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(CrossRepoPrModal(groups))
            await pilot.pause()
            empty = app.screen.query_one("#xpr-list").query(".xpr-empty")
            assert empty
            assert "no open pull requests across registered repos" in _text(empty.first(Static))
            assert app.screen.selected == -1

    asyncio.run(body())


def test_cross_repo_modal_esc_closes() -> None:
    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(CrossRepoPrModal(_GROUPS))
            await pilot.pause()
            assert app.modal_depth() == 1
            await pilot.press("escape")
            await pilot.pause()
            assert app.modal_depth() == 0

    asyncio.run(body())


def test_cross_repo_modal_is_read_only_no_mutating_bindings() -> None:
    """The overlay is advisory: no binding maps to a mutating PR action.

    Only navigation (move), open-in-browser (read-only ``gh pr view``), and
    close are bound -- there is no merge / close / comment / approve action.
    """
    from textual.binding import Binding

    bindings = [b for b in CrossRepoPrModal.BINDINGS if isinstance(b, Binding)]
    # Normalize ``move(-1)`` -> ``move`` for the parametrized binding.
    action_names = {b.action.split("(", 1)[0] for b in bindings}
    assert action_names == {"move", "open_web", "close"}
    forbidden = {"merge", "close_pr", "comment", "approve", "request_changes", "delete"}
    assert not (action_names & forbidden)


def test_cross_repo_modal_open_web_noop_on_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enter on an empty view spawns no ``gh`` (no-op, no crash)."""

    async def body() -> None:
        gh_calls = {"n": 0}

        def fake_run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
            if argv and argv[0] == "gh":
                gh_calls["n"] += 1
            return _completed()

        monkeypatch.setattr(subprocess, "run", fake_run)
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(CrossRepoPrModal(()))
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, CrossRepoPrModal)
            assert gh_calls["n"] == 0

    asyncio.run(body())


def test_cross_repo_modal_open_web_spawns_gh_view_for_highlighted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enter opens the highlighted PR via ``gh pr view --web`` (read-only)."""

    async def body() -> None:
        gh_argvs: list[list[str]] = []

        def fake_run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
            if argv and argv[0] == "gh":
                gh_argvs.append(argv)
            return _completed()

        monkeypatch.setattr(subprocess, "run", fake_run)
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(CrossRepoPrModal(_GROUPS))
            await pilot.pause()
            await pilot.press("down")
            await pilot.press("down")  # highlight DEF's #20
            await pilot.pause()
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert gh_argvs == [["gh", "pr", "view", "--web", "20"]]

    asyncio.run(body())


def test_open_cross_repo_pr_pushes_through_cap() -> None:
    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            assert open_cross_repo_pr(app, _GROUPS)
            await pilot.pause()
            assert isinstance(app.screen, CrossRepoPrModal)
            assert app.modal_depth() == 1

    asyncio.run(body())


# --------------------------------------------------------------------------
# /prs palette verb -- worker path with the gh shell-out stubbed
# --------------------------------------------------------------------------


def test_prs_verb_opens_cross_repo_modal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``/prs`` verb sweeps the registry off-thread and opens the overlay.

    The per-repo ``gh`` shell-out is stubbed at the ``subprocess`` boundary
    so no real ``gh`` runs; the registry resolves to a ``tmp_path`` home.
    """
    _write_registry(tmp_path, {"ABC": _entry("ABC"), "DEF": _entry("DEF")})
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    gh_json = json.dumps(
        [{"number": 7, "title": "t", "author": {"login": "a"}, "state": "OPEN", "url": "u"}]
    )

    def fake_run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        if argv and argv[0] == "gh":
            return _completed(stdout=gh_json)
        return _completed()

    monkeypatch.setattr(subprocess, "run", fake_run)

    async def body() -> None:
        from eawf.surfaces.tui.palette.verbs import VERBS

        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            handler = next(v.handler for v in VERBS if v.name == "/prs")
            handler(app, "")
            await app.workers.wait_for_complete()  # /prs sweep runs in a worker
            await pilot.pause()
            assert isinstance(app.screen, CrossRepoPrModal)
            assert "ABC" in _text(app.screen.query_one("#xpr-group-ABC", Static))
            assert "DEF" in _text(app.screen.query_one("#xpr-group-DEF", Static))

    asyncio.run(body())


def test_prs_verb_per_repo_gh_failure_degrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repo whose ``gh`` errors shows the unavailable header via the verb."""
    _write_registry(tmp_path, {"ABC": _entry("ABC")})
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def fake_run(*args: Any, **kwargs: Any) -> SimpleNamespace:
        raise FileNotFoundError("gh")

    monkeypatch.setattr(subprocess, "run", fake_run)

    async def body() -> None:
        from eawf.surfaces.tui.palette.verbs import VERBS

        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            handler = next(v.handler for v in VERBS if v.name == "/prs")
            handler(app, "")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen, CrossRepoPrModal)
            assert "unavailable" in _text(app.screen.query_one("#xpr-group-ABC", Static))

    asyncio.run(body())


def test_prs_verb_uses_default_fetcher_module_path() -> None:
    """The module's default fetcher delegates to the single-repo fetch.

    Verifies the wiring -- :func:`gather_cross_repo_prs` with no injected
    fetcher reuses :func:`pr_list.fetch_open_prs` rather than reimplementing
    the ``gh`` call.
    """
    assert xpr_mod._default_fetcher.__module__ == xpr_mod.__name__
    # The default-arg of the public builder is the module default fetcher.
    import inspect

    sig = inspect.signature(gather_cross_repo_prs)
    assert sig.parameters["fetcher"].default is xpr_mod._default_fetcher
