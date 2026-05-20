"""Tests for the C06 ``PrListModal`` ``/pr`` overlay (P26-W21).

Two layers: the pure ``gh pr list --json`` parser (:func:`parse_pr_rows`,
including the tolerant author-login extraction + partial-record skip)
without Textual, and Pilot-driven mounting + row-selection movement of the
overlay through the ``/pr`` palette verb + the modal-stack cap.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from rich.text import Text
from textual.widgets import Static

from eawf.tui_v2.app import EaApp
from eawf.tui_v2.screens.overlays.pr_list import (
    GH_PR_FIELDS,
    PR_CACHE_TTL_S,
    PrListModal,
    PrRow,
    parse_pr_rows,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"


def _text(widget: Static) -> str:
    rendered = widget.render()
    return rendered.plain if isinstance(rendered, Text) else str(rendered)


# --------------------------------------------------------------------------
# parse_pr_rows — gh pr list --json decode
# --------------------------------------------------------------------------


def test_parse_pr_rows_decodes_full_record() -> None:
    rows = parse_pr_rows(
        [
            {
                "number": 42,
                "title": "fix the thing",
                "author": {"login": "alice"},
                "state": "OPEN",
                "url": "https://example.test/pr/42",
            }
        ]
    )
    assert rows == (
        PrRow(
            number=42,
            title="fix the thing",
            author="alice",
            state="OPEN",
            url="https://example.test/pr/42",
        ),
    )


def test_parse_pr_rows_empty_input_is_empty() -> None:
    assert parse_pr_rows([]) == ()


def test_parse_pr_rows_skips_record_without_number() -> None:
    rows = parse_pr_rows([{"title": "no number"}, {"number": 7, "title": "ok"}])
    assert [r.number for r in rows] == [7]


def test_parse_pr_rows_skips_non_int_number() -> None:
    rows = parse_pr_rows([{"number": "abc", "title": "bad"}, {"number": 9}])
    assert [r.number for r in rows] == [9]


def test_parse_pr_rows_author_bare_string() -> None:
    rows = parse_pr_rows([{"number": 1, "author": "bob"}])
    assert rows[0].author == "bob"


def test_parse_pr_rows_author_missing_is_empty() -> None:
    rows = parse_pr_rows([{"number": 1, "title": "t"}])
    assert rows[0].author == ""


def test_parse_pr_rows_preserves_input_order() -> None:
    rows = parse_pr_rows([{"number": 3}, {"number": 1}, {"number": 2}])
    assert [r.number for r in rows] == [3, 1, 2]


def test_gh_pr_fields_include_number_and_url() -> None:
    assert "number" in GH_PR_FIELDS
    assert "url" in GH_PR_FIELDS


def test_pr_cache_ttl_is_sixty_seconds() -> None:
    assert PR_CACHE_TTL_S == 60.0


# --------------------------------------------------------------------------
# PrListModal — mounting + selection (Pilot)
# --------------------------------------------------------------------------


_ROWS = (
    PrRow(12, "fix one", "alice", "OPEN", "https://example.test/12"),
    PrRow(13, "fix two", "bob", "OPEN", "https://example.test/13"),
    PrRow(14, "fix three", "carol", "OPEN", "https://example.test/14"),
)


def test_pr_modal_mounts_rows() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(PrListModal(_ROWS))
            await pilot.pause()
            assert isinstance(app.screen, PrListModal)
            rows = app.screen.query_one("#pr-list").query(".pr-row")
            assert len(rows) == 3
            assert "#12" in _text(app.screen.query_one("#pr-row-0", Static))

    asyncio.run(body())


def test_pr_modal_selection_starts_at_first_row() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(PrListModal(_ROWS))
            await pilot.pause()
            assert app.screen.selected == 0
            assert app.screen.query_one("#pr-row-0", Static).has_class("-selected")

    asyncio.run(body())


def test_pr_modal_down_moves_selection() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(PrListModal(_ROWS))
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause()
            assert app.screen.selected == 1
            assert app.screen.query_one("#pr-row-1", Static).has_class("-selected")

    asyncio.run(body())


def test_pr_modal_selection_clamps_at_bottom() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(PrListModal(_ROWS))
            await pilot.pause()
            for _ in range(5):  # over-scroll past the last row
                await pilot.press("down")
                await pilot.pause()
            assert app.screen.selected == 2

    asyncio.run(body())


def test_pr_modal_empty_shows_placeholder() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(PrListModal(()))
            await pilot.pause()
            assert app.screen.query_one("#pr-list").query(".pr-empty")
            assert app.screen.selected == -1

    asyncio.run(body())


def test_pr_verb_opens_modal_through_cap() -> None:
    async def body() -> None:
        from eawf.tui_v2.palette.verbs import VERBS

        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            handler = next(v.handler for v in VERBS if v.name == "/pr")
            handler(app, "")
            await pilot.pause()
            assert isinstance(app.screen, PrListModal)
            assert app.modal_depth() == 1

    asyncio.run(body())


def test_pr_modal_esc_closes() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(PrListModal(_ROWS))
            await pilot.pause()
            assert app.modal_depth() == 1
            await pilot.press("escape")
            await pilot.pause()
            assert app.modal_depth() == 0

    asyncio.run(body())


def test_pr_modal_open_web_noop_on_empty() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.pause()
            app.push_modal(PrListModal(()))
            await pilot.pause()
            # Enter on an empty list is a no-op (no crash, modal stays).
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, PrListModal)

    asyncio.run(body())
