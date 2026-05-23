"""Tests for the ``/filter`` + ``/sort`` pane verbs (P26-I02-W03).

``/filter backlog <needle>`` drives
:meth:`~eawf.tui.widgets.backlog_table.BacklogTable.apply_filter` and
``/sort backlog`` drives
:meth:`~eawf.tui.widgets.backlog_table.BacklogTable.cycle_sort` on the
mounted backlog pane. The repo scope mounts the ``BacklogTable``, so these
Pilot tests host the real :class:`~eawf.tui.app.EaApp` and observe the
widget's reactive state change (the visible effect of the call) plus a
direct call-spy. Unknown panes and scopes that do not mount the pane
degrade to a warning toast and mutate nothing.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from eawf.tui.app import EaApp
from eawf.tui.palette.verbs import _handle_filter, _handle_sort
from eawf.tui.widgets.backlog_table import SORT_KEYS, BacklogTable

from ._palette_harness import PaletteHarnessApp

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"


class _NoBacklogHarness(PaletteHarnessApp):
    """A host that mounts no backlog pane (exercises the absent-pane path)."""


def _backlog_table(app: EaApp) -> BacklogTable:
    return app.screen.query(BacklogTable).first()


# --------------------------------------------------------------------------
# /filter backlog — drives apply_filter
# --------------------------------------------------------------------------


def test_handle_filter_backlog_calls_apply_filter() -> None:
    async def body() -> None:
        calls: list[str] = []
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            table = _backlog_table(app)
            table.apply_filter = calls.append  # type: ignore[assignment]
            _handle_filter(app, "backlog metrics")
            await pilot.pause()
            assert calls == ["metrics"]

    asyncio.run(body())


def test_handle_filter_backlog_sets_filter_text_reactive() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            table = _backlog_table(app)
            _handle_filter(app, "backlog wire")
            await pilot.pause()
            assert table.filter_text == "wire"

    asyncio.run(body())


def test_handle_filter_backlog_empty_needle_clears_filter() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            table = _backlog_table(app)
            table.filter_text = "stale"
            _handle_filter(app, "backlog")
            await pilot.pause()
            assert table.filter_text == ""

    asyncio.run(body())


def test_handle_filter_unknown_pane_warns_no_change() -> None:
    async def body() -> None:
        notices: list[tuple[str, str | None]] = []
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            table = _backlog_table(app)
            before = table.filter_text
            app.notify = lambda message, *_a, **kw: notices.append(  # type: ignore[method-assign]
                (message, kw.get("severity"))
            )
            _handle_filter(app, "roadmap something")
            await pilot.pause()
            assert table.filter_text == before
            assert notices and notices[-1][1] == "warning"
            assert "roadmap" in notices[-1][0]

    asyncio.run(body())


def test_handle_filter_missing_pane_warns() -> None:
    """A host that mounts no backlog pane degrades to a warning, not a crash."""

    async def body() -> None:
        notices: list[tuple[str, str | None]] = []
        app = _NoBacklogHarness()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert all(not screen.query(BacklogTable) for screen in app.screen_stack)
            app.notify = lambda message, *_a, **kw: notices.append(  # type: ignore[method-assign]
                (message, kw.get("severity"))
            )
            _handle_filter(app, "backlog metrics")
            await pilot.pause()
            assert notices and notices[-1][1] == "warning"

    asyncio.run(body())


# --------------------------------------------------------------------------
# /sort backlog — drives cycle_sort
# --------------------------------------------------------------------------


def test_handle_sort_backlog_calls_cycle_sort() -> None:
    async def body() -> None:
        calls: list[int] = []
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            table = _backlog_table(app)
            table.cycle_sort = lambda: calls.append(1)  # type: ignore[method-assign]
            _handle_sort(app, "backlog")
            await pilot.pause()
            assert calls == [1]

    asyncio.run(body())


def test_handle_sort_backlog_advances_sort_key() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            table = _backlog_table(app)
            assert table.sort_key == SORT_KEYS[0]
            _handle_sort(app, "backlog")
            await pilot.pause()
            assert table.sort_key == SORT_KEYS[1]

    asyncio.run(body())


def test_handle_sort_unknown_pane_warns_no_change() -> None:
    async def body() -> None:
        notices: list[tuple[str, str | None]] = []
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            table = _backlog_table(app)
            before = table.sort_key
            app.notify = lambda message, *_a, **kw: notices.append(  # type: ignore[method-assign]
                (message, kw.get("severity"))
            )
            _handle_sort(app, "roadmap")
            await pilot.pause()
            assert table.sort_key == before
            assert notices and notices[-1][1] == "warning"
            assert "roadmap" in notices[-1][0]

    asyncio.run(body())


def test_handle_sort_empty_pane_warns() -> None:
    async def body() -> None:
        notices: list[tuple[str, str | None]] = []
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.notify = lambda message, *_a, **kw: notices.append(  # type: ignore[method-assign]
                (message, kw.get("severity"))
            )
            _handle_sort(app, "")
            await pilot.pause()
            assert notices and notices[-1][1] == "warning"

    asyncio.run(body())
