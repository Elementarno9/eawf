"""Tests for the raw agent-output tail widget (P30-I13-W04, FA4 session zoom).

The :class:`~eawf.surfaces.tui.widgets.output_tail.OutputTail` is a ``tail -f``
view of a spawned agent's raw stdout: it appends each line at the bottom,
auto-scrolls so the newest line stays in view, and -- before any output lands
(or while a stream is stalled) -- shows the pinned literal
``waiting for output...`` rather than a frozen blank pane.

These tests mount the widget alone under a minimal Textual ``App`` + ``Pilot``
(no daemon, no state) and pin:

* the pinned waiting notice on a freshly-mounted (silent) tail;
* a single :meth:`OutputTail.append_line` dropping the notice + rendering the
  line + flipping :attr:`OutputTail.has_output`;
* a many-line :meth:`OutputTail.extend` replay landing every line in order;
* a literal ``[`` in agent output rendering escaped (never a parsed style tag);
* the stalled-stream invariant: a tail that received one line then went quiet
  keeps its line on screen, never reverting to the waiting notice.

Determinism follows the project Pilot-worker rule: each body drains workers via
:func:`~eawf.surfaces.tui.snapshot.settle_screen` before asserting.
"""

from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult

from eawf.surfaces.tui.snapshot import settle_screen
from eawf.surfaces.tui.widgets.output_tail import (
    OUTPUT_TAIL_ROW_CLASS,
    OUTPUT_TAIL_WAITING_ID,
    WAITING_NOTICE,
    OutputTail,
)


class _TailApp(App[None]):
    """A minimal host App mounting one :class:`OutputTail` for the Pilot tests."""

    def compose(self) -> ComposeResult:
        """Yield the single output-tail under test."""
        yield OutputTail(id="tail-under-test")

    @property
    def tail(self) -> OutputTail:
        """Return the mounted tail."""
        return self.query_one("#tail-under-test", OutputTail)


def test_output_tail_waiting_notice_on_fresh_mount() -> None:
    """A freshly-mounted tail shows the pinned waiting notice, no rows.

    The not-yet-spoken surface: before any output the tail renders the pinned
    ``waiting for output...`` notice rather than a blank pane, and reports no
    output yet.
    """

    async def body() -> None:
        app = _TailApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await settle_screen(pilot)
            tail = app.tail
            assert tail.query(f"#{OUTPUT_TAIL_WAITING_ID}")
            assert not tail.has_output
            assert not tail.query(f".{OUTPUT_TAIL_ROW_CLASS}")
            assert WAITING_NOTICE == "waiting for output…"  # the real ellipsis

    asyncio.run(body())


def test_output_tail_append_line_drops_notice_and_renders() -> None:
    """A single appended line drops the notice, renders, and flips has_output."""

    async def body() -> None:
        app = _TailApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await settle_screen(pilot)
            tail = app.tail
            tail.append_line("first agent line")
            await settle_screen(pilot)
            assert not tail.query(f"#{OUTPUT_TAIL_WAITING_ID}")  # notice dropped
            assert tail.has_output
            rows = [str(r.render()) for r in tail.query(f".{OUTPUT_TAIL_ROW_CLASS}").results()]
            assert rows == ["first agent line"]

    asyncio.run(body())


def test_output_tail_append_preserves_append_order() -> None:
    """Lines append at the bottom in arrival order (a tail, not newest-first)."""

    async def body() -> None:
        app = _TailApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await settle_screen(pilot)
            tail = app.tail
            for line in ("line A", "line B", "line C"):
                tail.append_line(line)
            await settle_screen(pilot)
            rows = [str(r.render()) for r in tail.query(f".{OUTPUT_TAIL_ROW_CLASS}").results()]
            assert rows == ["line A", "line B", "line C"]

    asyncio.run(body())


def test_output_tail_extend_lands_every_line_in_order() -> None:
    """A buffer-replay extend lands every line in order, dropping the notice."""

    async def body() -> None:
        app = _TailApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await settle_screen(pilot)
            tail = app.tail
            tail.extend(["seed 1", "seed 2", "seed 3"])
            await settle_screen(pilot)
            assert not tail.query(f"#{OUTPUT_TAIL_WAITING_ID}")
            assert tail.has_output
            rows = [str(r.render()) for r in tail.query(f".{OUTPUT_TAIL_ROW_CLASS}").results()]
            assert rows == ["seed 1", "seed 2", "seed 3"]

    asyncio.run(body())


def test_output_tail_extend_empty_keeps_waiting_notice() -> None:
    """An empty extend leaves the pane (and its waiting notice) untouched.

    Boundary case: a zero-line replay is a no-op -- the notice stays and no row
    mounts, so a quiescent buffer never flips the pane to a spoken-but-blank one.
    """

    async def body() -> None:
        app = _TailApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await settle_screen(pilot)
            tail = app.tail
            tail.extend([])
            await settle_screen(pilot)
            assert tail.query(f"#{OUTPUT_TAIL_WAITING_ID}")
            assert not tail.has_output
            assert not tail.query(f".{OUTPUT_TAIL_ROW_CLASS}")

    asyncio.run(body())


def test_output_tail_escapes_bracket_in_output() -> None:
    """A literal ``[`` in agent output renders escaped, not as a style tag.

    Error-path / robustness: agent stdout often carries bracketed prefixes such
    as ``[P30-I13-W04]``; the tail must render them literally rather than letting
    Textual parse them as a (dropped) style tag.
    """

    async def body() -> None:
        app = _TailApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await settle_screen(pilot)
            tail = app.tail
            tail.append_line("[P30-I13-W04] building")
            await settle_screen(pilot)
            rows = [str(r.render()) for r in tail.query(f".{OUTPUT_TAIL_ROW_CLASS}").results()]
            assert rows == ["[P30-I13-W04] building"]  # bracket survived literally

    asyncio.run(body())


def test_output_tail_stalled_keeps_last_line_not_waiting_notice() -> None:
    """A tail that spoke once then went quiet keeps its line, never the notice.

    The stalled-stream invariant: once the first line lands the waiting notice
    is gone for good, so a stream that stops mid-flight renders its last lines
    rather than reverting to the (misleading) ``waiting for output...`` notice.
    """

    async def body() -> None:
        app = _TailApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await settle_screen(pilot)
            tail = app.tail
            tail.append_line("only line before stall")
            await settle_screen(pilot)
            # The stream goes quiet -- no further appends. Settle again.
            await settle_screen(pilot)
            assert not tail.query(f"#{OUTPUT_TAIL_WAITING_ID}")  # never reverts
            rows = [str(r.render()) for r in tail.query(f".{OUTPUT_TAIL_ROW_CLASS}").results()]
            assert rows == ["only line before stall"]
            assert tail.has_output

    asyncio.run(body())


def test_output_tail_append_before_mount_is_noop() -> None:
    """Appending to an unmounted tail is a quiet no-op (no raise).

    Boundary case: a push scheduled before the widget mounts (or after teardown)
    must not raise -- the append is dropped and ``has_output`` stays ``False``.
    """
    tail = OutputTail(id="unmounted")
    assert not tail.is_mounted
    tail.append_line("dropped")
    tail.extend(["also dropped"])
    assert not tail.has_output
