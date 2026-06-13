"""Golden snapshot + grouping contract for the command palette (P30-I02-W18).

The ``/`` command palette groups its verbs into three sections --
``Recent`` / ``Lifecycle`` / ``All`` -- so the operator scans the
recently-run shortcuts, the stage-mutating lifecycle moves, and the
remaining navigation / chrome verbs as distinct bands. Lifecycle verbs
(open phase / iter / wave, run the verdict step) wear the stage sigil
(the running diamond, resolved against the App's render mode) so they
read as the state-advancing moves.

This module pins:

* the pure :func:`~eawf.surfaces.tui.palette.command_palette.group_verbs`
  partition (Recent / Lifecycle / All; section order; lifecycle membership)
  -- unit-testable without mounting Textual;
* the frozen no-match prompt literal
  (:data:`~eawf.surfaces.tui.palette.command_palette.NO_MATCH_PROMPT`)
  byte-for-byte, including its middle-dot separator and the absence of a
  trailing period; and
* the rendered palette (mounted IN ISOLATION on a bare themed App, NOT
  the full operator surface) as a golden snapshot showing the three
  section headers with lifecycle verbs carrying the stage sigil.

Regenerate the golden after an intentional layout change with::

    EAWF_SNAPSHOT_REGEN=1 uv run pytest tests/snapshots/tui/test_command_palette_sections.py -q
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from eawf.surfaces.tui.palette.command_palette import (
    LIFECYCLE_VERB_NAMES,
    NO_MATCH_PROMPT,
    SECTION_ALL,
    SECTION_LIFECYCLE,
    SECTION_RECENT,
    CommandPalette,
    group_verbs,
)
from eawf.surfaces.tui.palette.verbs import visible_verbs
from eawf.surfaces.tui.snapshot import (
    assert_screen_snapshot,
    capture_screen_text,
    normalize_snapshot,
    settle_screen,
)
from eawf.surfaces.tui.theme import EA_THEMES, LOGICAL_THEMES
from eawf.surfaces.tui.widgets.sigils import Sigil, glyph

_SIZE = (120, 40)
_GOLDEN = Path(__file__).resolve().parent / "golden"

#: The exact frozen literal the no-match path must pin.
_FROZEN_NO_MATCH: str = "no verb matches · Esc cancel"


class _PaletteHostApp(App[None]):
    """Bare themed host App that pushes the palette IN ISOLATION.

    Registers the Eä themes (so the palette's ``$accent`` / ``$surface``
    semantic vars resolve at stylesheet-parse time) and seeds two recent
    verbs so the rendered golden exercises the ``Recent`` section. The
    full operator surface (:class:`~eawf.surfaces.tui.app.EaApp`) is
    deliberately NOT mounted: this wave reskins a single surface, so the
    golden asserts that surface alone.
    """

    #: Seeded recents so the rendered golden shows the Recent band.
    _palette_recents: tuple[str, ...] = ("/theme", "/find")

    #: The resolved render-mode label the real
    #: :class:`~eawf.surfaces.tui.app.EaApp` carries as a reactive; the
    #: bare host pins it to the unicode column so the stage sigil resolves
    #: deterministically in the golden.
    render_mode: str = "unicode"

    def __init__(self) -> None:
        super().__init__()
        for theme in EA_THEMES:
            self.register_theme(theme)
        self.theme = LOGICAL_THEMES["dark"]

    def compose(self) -> ComposeResult:
        yield from ()

    def on_mount(self) -> None:
        self.push_screen(CommandPalette(recents=self._palette_recents))


# --------------------------------------------------------------------------
# Pure grouping -- no Textual mount
# --------------------------------------------------------------------------


def test_group_verbs_orders_recent_lifecycle_all() -> None:
    """The non-empty sections render in Recent -> Lifecycle -> All order."""
    verbs = visible_verbs("repo")
    groups = group_verbs(verbs, recents=("/theme", "/find"))

    assert [section for section, _ in groups] == [
        SECTION_RECENT,
        SECTION_LIFECYCLE,
        SECTION_ALL,
    ]


def test_group_verbs_recent_section_is_seeded_recents_in_order() -> None:
    """The Recent section carries the seeded recents, most-recent first."""
    verbs = visible_verbs("repo")
    groups = dict(group_verbs(verbs, recents=("/theme", "/find")))

    assert [verb.name for verb in groups[SECTION_RECENT]] == ["/theme", "/find"]


def test_group_verbs_lifecycle_section_holds_stage_verbs() -> None:
    """Every Lifecycle-section verb is a registered lifecycle verb name."""
    verbs = visible_verbs("repo")
    groups = dict(group_verbs(verbs, recents=()))

    lifecycle_names = {verb.name for verb in groups[SECTION_LIFECYCLE]}
    assert lifecycle_names
    assert lifecycle_names <= LIFECYCLE_VERB_NAMES


def test_group_verbs_no_section_double_counts_a_verb() -> None:
    """A verb claimed by Recent is not re-listed under Lifecycle / All."""
    verbs = visible_verbs("repo")
    # /audit is a lifecycle verb; seeding it as a recent moves it to Recent.
    groups = dict(group_verbs(verbs, recents=("/audit",)))

    assert "/audit" in {verb.name for verb in groups[SECTION_RECENT]}
    assert "/audit" not in {verb.name for verb in groups.get(SECTION_LIFECYCLE, [])}
    assert "/audit" not in {verb.name for verb in groups.get(SECTION_ALL, [])}


def test_group_verbs_empty_input_yields_no_groups() -> None:
    """No verbs partitions to no sections (no header without rows beneath)."""
    assert group_verbs([], recents=("/theme",)) == []


def test_group_verbs_drops_recent_section_when_no_recents_resolve() -> None:
    """A recents list that matches no visible verb yields no Recent section."""
    verbs = visible_verbs("repo")
    groups = group_verbs(verbs, recents=("/not-a-verb",))

    assert SECTION_RECENT not in {section for section, _ in groups}


# --------------------------------------------------------------------------
# Frozen no-match literal -- byte-for-byte
# --------------------------------------------------------------------------


def test_no_match_prompt_is_frozen_middle_dot_literal() -> None:
    """The no-match prompt pins the frozen middle-dot literal byte-for-byte."""
    assert NO_MATCH_PROMPT == _FROZEN_NO_MATCH
    assert "·" in NO_MATCH_PROMPT
    assert " - " not in NO_MATCH_PROMPT
    assert "--" not in NO_MATCH_PROMPT
    # No trailing period (a prompt is a label, not a sentence).
    assert not NO_MATCH_PROMPT.endswith(".")


# --------------------------------------------------------------------------
# Rendered palette -- mounted IN ISOLATION (golden snapshot)
# --------------------------------------------------------------------------


def test_palette_sections_snapshot() -> None:
    """The mounted palette groups Recent / Lifecycle / All with stage sigils."""

    async def body() -> None:
        app = _PaletteHostApp()
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            assert isinstance(app.screen, CommandPalette)
            option_list = app.screen.query_one("#palette-options", OptionList)

            labels = [
                option_list.get_option_at_index(i).prompt for i in range(option_list.option_count)
            ]
            text = "\n".join(str(label) for label in labels)
            # All three section headers render.
            assert SECTION_RECENT in text
            assert SECTION_LIFECYCLE in text
            assert SECTION_ALL in text
            # Lifecycle verbs wear the stage (running) sigil prefix.
            stage = glyph(Sigil.RUNNING, mode=app.render_mode)
            assert f"{stage} /roadmap" in text
            # Section headers are non-selectable (disabled) rows.
            for index in range(option_list.option_count):
                option = option_list.get_option_at_index(index)
                if str(option.prompt) in (SECTION_RECENT, SECTION_LIFECYCLE, SECTION_ALL):
                    assert option.disabled is True

            assert_screen_snapshot(app, _GOLDEN / "command_palette_sections.txt")

    asyncio.run(body())


def test_palette_no_match_renders_frozen_prompt() -> None:
    """A non-matching query renders the frozen no-match prompt as a disabled row."""

    async def body() -> None:
        app = _PaletteHostApp()
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            for char in "zzzz":
                await pilot.press(char)
            await settle_screen(pilot)
            option_list = app.screen.query_one("#palette-options", OptionList)
            assert option_list.option_count == 1
            prompt = option_list.get_option_at_index(0)
            assert isinstance(prompt, Option)
            assert str(prompt.prompt) == _FROZEN_NO_MATCH
            assert prompt.disabled is True
            # The frozen literal is in the rendered frame byte-for-byte.
            frame = normalize_snapshot(capture_screen_text(app))
            assert _FROZEN_NO_MATCH in frame

    asyncio.run(body())


@pytest.mark.parametrize("query", ["", "/"])
def test_palette_trivial_query_renders_no_no_match_prompt(query: str) -> None:
    """An empty / bare-prefix query never shows the no-match prompt."""

    async def body() -> None:
        app = _PaletteHostApp()
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            palette = app.screen
            assert isinstance(palette, CommandPalette)
            palette._refresh_options(query)
            await settle_screen(pilot)
            option_list = palette.query_one("#palette-options", OptionList)
            labels = [
                str(option_list.get_option_at_index(i).prompt)
                for i in range(option_list.option_count)
            ]
            assert _FROZEN_NO_MATCH not in labels

    asyncio.run(body())
