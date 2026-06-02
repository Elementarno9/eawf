"""Unit + Pilot tests for the ``MultichoiceChecklist`` overlay widget.

Covers the W09 fix (d): the ``_meta_line`` prefix renders once as a header
row above the checklist (not echoed on every option line), the option rows
carry no prefix and indent-align under the value column, and the cursor /
toggle / selected-items contract is preserved.
"""

from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult

from eawf.surfaces.tui.screens.overlays.multichoice_checklist import MultichoiceChecklist

# A realistic ``ConfigModal._meta_line`` prefix: three-cell lead (caret +
# dirty + separator), the key, then the padded ``[type]`` cell + trailing
# space -- exactly the string the modal passes through.
_PREFIX = "   ui.dashboard_panes [multichoice] "
_CHOICES = ("state", "roadmap", "backlog", "events", "git", "registry", "trust")


class _Harness(App[None]):
    """Bare host mounting one checklist with a config-modal-style prefix."""

    def __init__(self, *, selected: list[str] | None = None) -> None:
        super().__init__()
        self._selected = selected or []

    def compose(self) -> ComposeResult:
        yield MultichoiceChecklist(
            choices=_CHOICES,
            selected=self._selected,
            prefix=_PREFIX,
            id="mc",
        )


def _rendered(checklist: MultichoiceChecklist) -> str:
    return str(checklist.render())


# --------------------------------------------------------------------------
# Header row renders the prefix exactly ONCE (fix d)
# --------------------------------------------------------------------------


def test_prefix_renders_once_as_header_not_per_row() -> None:
    """The meta prefix appears exactly once (a header), not on every option."""

    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            checklist = app.query_one("#mc", MultichoiceChecklist)
            text = _rendered(checklist)
            # The key+type meta token appears once, not once per choice.
            assert text.count("ui.dashboard_panes [multichoice]") == 1
            # Every choice still renders (one option line each).
            for choice in _CHOICES:
                assert choice in text

    asyncio.run(body())


def test_header_is_the_first_line() -> None:
    """The single meta line is the header (first) row, above the options."""

    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            checklist = app.query_one("#mc", MultichoiceChecklist)
            lines = _rendered(checklist).splitlines()
            assert lines[0].strip() == "ui.dashboard_panes [multichoice]"
            # Header carries no checkbox; the option rows do.
            assert "[" not in lines[0].replace("[multichoice]", "")
            assert len(lines) == 1 + len(_CHOICES)  # header + one row per choice

    asyncio.run(body())


def test_option_rows_carry_no_prefix() -> None:
    """Option lines drop the meta prefix and indent under the value column."""

    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            checklist = app.query_one("#mc", MultichoiceChecklist)
            lines = _rendered(checklist).splitlines()
            indent = " " * len(_PREFIX)
            for line in lines[1:]:
                # No echoed key/type token on an option row.
                assert "ui.dashboard_panes" not in line
                assert "[multichoice]" not in line
                # The row indents under the value column the header sets up.
                assert line.startswith(indent)
                assert "[ ]" in line or "[X]" in line

    asyncio.run(body())


def test_caret_marks_focused_option_row_only() -> None:
    """The ``>`` caret sits on the cursor's option row, never on the header."""

    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            checklist = app.query_one("#mc", MultichoiceChecklist)
            lines = _rendered(checklist).splitlines()
            assert ">" not in lines[0]  # header has no caret
            # Line 0 of the options (index 1 overall) is the focused row.
            assert lines[1].lstrip().startswith(">")
            # Exactly one caret across the whole render.
            assert _rendered(checklist).count(">") == 1

    asyncio.run(body())


# --------------------------------------------------------------------------
# Cursor / toggle / selected-items contract preserved (fix d)
# --------------------------------------------------------------------------


def test_cursor_down_moves_caret_and_repaints() -> None:
    """``down`` advances the line cursor; the caret tracks to the new row."""

    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            checklist = app.query_one("#mc", MultichoiceChecklist)
            checklist.focus()
            await pilot.press("down")
            await pilot.pause()
            assert checklist.line_index == 1
            lines = _rendered(checklist).splitlines()
            # Header (0) + first option (1) carry no caret; second option does.
            assert ">" not in lines[1]
            assert lines[2].lstrip().startswith(">")

    asyncio.run(body())


def test_cursor_clamps_at_first_and_last() -> None:
    """``up`` at the top and ``down`` at the bottom clamp (no wrap)."""

    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            checklist = app.query_one("#mc", MultichoiceChecklist)
            checklist.focus()
            await pilot.press("up")  # already at 0
            await pilot.pause()
            assert checklist.line_index == 0
            for _ in range(len(_CHOICES) + 3):
                await pilot.press("down")
            await pilot.pause()
            assert checklist.line_index == len(_CHOICES) - 1  # clamped at last

    asyncio.run(body())


def test_space_toggles_focused_choice_membership() -> None:
    """``space`` flips the focused choice's ``[X]`` mark + selected-items."""

    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            checklist = app.query_one("#mc", MultichoiceChecklist)
            checklist.focus()
            assert checklist.selected_items() == []
            await pilot.press("space")  # toggle the first choice ON
            await pilot.pause()
            assert checklist.selected_items() == ["state"]
            # The first option row now shows the filled mark.
            first_option = _rendered(checklist).splitlines()[1]
            assert "[X]" in first_option
            await pilot.press("space")  # toggle it back OFF
            await pilot.pause()
            assert checklist.selected_items() == []

    asyncio.run(body())


def test_selected_items_returned_in_declaration_order() -> None:
    """``selected_items`` preserves declaration order regardless of toggle order."""

    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            checklist = app.query_one("#mc", MultichoiceChecklist)
            checklist.focus()
            # Toggle a later choice first, then an earlier one.
            await pilot.press("down", "down")  # cursor -> "backlog" (index 2)
            await pilot.press("space")
            await pilot.press("up", "up")  # cursor -> "state" (index 0)
            await pilot.press("space")
            await pilot.pause()
            # Declaration order: state (0) before backlog (2).
            assert checklist.selected_items() == ["state", "backlog"]

    asyncio.run(body())


def test_commit_posts_selected_items() -> None:
    """``enter`` posts ``Committed`` carrying the selected list."""
    captured: list[list[str]] = []

    class _CaptureHarness(_Harness):
        def on_multichoice_checklist_committed(
            self, message: MultichoiceChecklist.Committed
        ) -> None:
            captured.append(message.selected)

    async def body() -> None:
        app = _CaptureHarness(selected=["roadmap"])
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            checklist = app.query_one("#mc", MultichoiceChecklist)
            checklist.focus()
            await pilot.press("enter")
            await pilot.pause()

    asyncio.run(body())
    assert captured == [["roadmap"]]


def test_pre_selected_choice_renders_filled_mark() -> None:
    """A pre-selected choice seeds the ``[X]`` mark on its option row."""

    async def body() -> None:
        app = _Harness(selected=["backlog"])
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            checklist = app.query_one("#mc", MultichoiceChecklist)
            lines = _rendered(checklist).splitlines()
            backlog_line = next(line for line in lines if "backlog" in line)
            assert "[X]" in backlog_line
            assert checklist.selected_items() == ["backlog"]

    asyncio.run(body())
