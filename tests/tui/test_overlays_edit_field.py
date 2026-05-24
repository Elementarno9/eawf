"""Pilot + unit tests for the C06 ``EditFieldModal`` scalar editor.

Covers the per-type single-field editor: the input seeds from the current
value, ``Enter`` validates the buffer against the field's declared type /
range (dismissing with the typed value), a validation failure reports
inline below the input and keeps the overlay open, and ``Esc`` cancels
(dismissing ``None``).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from textual.widgets import Input, Static

from eawf.kernel.config.registry import registry_lookup
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.screens.overlays.edit_field import EditFieldModal, seed_input_text

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"

#: An int field with a declared range (audit.flaky_retry_count: 0..5).
_INT_KEY = registry_lookup("audit.flaky_retry_count")
assert _INT_KEY is not None


def _push_edit(app: EaApp, current: Any, sink: list[Any]) -> EditFieldModal:
    modal = EditFieldModal(_INT_KEY, current)
    app.push_screen(modal, callback=lambda result: sink.append(result))
    return modal


def test_seed_input_text_stringifies_value() -> None:
    assert seed_input_text(_INT_KEY, 3) == "3"


def test_seed_input_text_none_is_empty() -> None:
    assert seed_input_text(_INT_KEY, None) == ""


def test_edit_seeds_input_from_current() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_edit(app, 2, [])
            await pilot.pause()
            assert modal.query_one("#edit-field-input", Input).value == "2"

    asyncio.run(body())


def test_edit_enter_returns_coerced_value() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        sink: list[Any] = []
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_edit(app, 1, sink)
            await pilot.pause()
            modal.query_one("#edit-field-input", Input).value = "4"
            await pilot.press("enter")
            await pilot.pause()
        # Returned value is the coerced int, not the raw string.
        assert sink == [4]

    asyncio.run(body())


def test_edit_invalid_value_reports_inline_and_stays_open() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        sink: list[Any] = []
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_edit(app, 1, sink)
            await pilot.pause()
            # Above the declared max of 5 — coercion must fail.
            modal.query_one("#edit-field-input", Input).value = "99"
            await pilot.press("enter")
            await pilot.pause()
            # No dismiss (overlay stays open) and the error row is populated.
            assert app.screen is modal
            error = modal.query_one("#edit-field-error", Static)
            assert "maximum" in str(error.render())
        assert sink == []

    asyncio.run(body())


def test_edit_esc_cancels_with_none() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        sink: list[Any] = []
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _push_edit(app, 1, sink)
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
        assert sink == [None]

    asyncio.run(body())


def test_edit_meta_line_shows_range() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = _push_edit(app, 1, [])
            await pilot.pause()
            meta = modal.query_one(".edit-field-meta", Static)
            text = str(meta.render())
            assert "audit.flaky_retry_count" in text
            assert "range 0..5" in text

    asyncio.run(body())
