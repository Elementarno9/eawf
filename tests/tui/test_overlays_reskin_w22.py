"""Pilot tests for the multichoice / confirm / init / PR-grid reskin (P30-I02-W22).

The cosmic-terminal reskin lands the shared chrome sigils on five overlays:

- ``MultichoiceChecklist`` -- the filled ``check_on`` / empty ``check_off``
  toggle marks replace the hardcoded ``[X]`` / ``[ ]``;
- ``ConfirmModal`` -- the destructive prompt wears the ``attention`` sigil
  plus the key-hint chord vocab;
- ``InitWizardModal`` -- the title wears the ``dispatch`` sigil plus the
  shared key-hint vocab;
- ``PrListModal`` / ``CrossRepoPrModal`` -- the narrow-sigil PR grid leads
  each row with a single-cell ``dispatch`` sigil so no glyph strands
  against the ``-selected`` reverse rectangle.

These tests mount each overlay IN ISOLATION through a Pilot harness and
assert the rendered glyph vocab plus ``affordance_parity`` -- the
toggle / confirm keys resolve to live ``Binding`` actions that fire. The
overlays mount under :class:`~eawf.surfaces.tui.app.EaApp`, whose default
``render_mode`` is the unicode column, so the asserted glyphs are the
unicode marks; a bare standalone harness exposing no ``render_mode``
resolves the same unicode fallback.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Static

from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.screens.overlays.confirm import ConfirmModal
from eawf.surfaces.tui.screens.overlays.cross_repo_pr import CrossRepoGroup, CrossRepoPrModal
from eawf.surfaces.tui.screens.overlays.init_wizard import InitWizardContext, InitWizardModal
from eawf.surfaces.tui.screens.overlays.multichoice_checklist import MultichoiceChecklist
from eawf.surfaces.tui.screens.overlays.pr_list import PrFetchStatus, PrListModal, PrRow
from eawf.surfaces.tui.snapshot import assert_screen_snapshot, settle_screen
from eawf.surfaces.tui.widgets.sigils import chrome

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"

#: Golden home for the isolated-overlay reskin snapshots this wave adds --
#: a fresh dir local to this test, never the full-app screen-snapshot
#: goldens under ``tests/snapshots/tui/golden/``.
_GOLDEN = Path(__file__).resolve().parent / "golden" / "reskin_w22"


@pytest.fixture(autouse=True)
def _settle_determinism(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Settle animations + neutralize the live git pane for stable captures.

    Two determinism guards mirroring the full-app snapshot suite:

    * Textual reads ``constants.TEXTUAL_ANIMATIONS`` once at import, so the
      App copies it into ``self.animation_level`` at construction; patch the
      constant directly to settle the ``-selected`` reverse fill + any
      time-driven chrome to its final position before the snapshot capture.
    * The repo screen's ``GitPane`` probes the live work tree (branch +
      dirty count + recent commits), which drifts run-to-run and leaks the
      real branch into a golden. The multichoice checklist mounts onto the
      live repo dashboard (it is a ``Static``, not a modal), so stub
      ``git_pane._git_run`` to ``None`` -- every git field then resolves to
      a deterministic dash -- and chdir into a fresh non-git temp dir.
    """
    import textual.constants as _tc

    monkeypatch.setattr(_tc, "TEXTUAL_ANIMATIONS", "none")
    monkeypatch.setattr("eawf.surfaces.tui.widgets.git_pane._git_run", lambda *a, **k: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)


# The overlays mount under EaApp, whose default render_mode is the unicode
# column -- so the asserted glyphs are the unicode marks.
_CHECK_ON = chrome("check_on", mode="unicode")
_CHECK_OFF = chrome("check_off", mode="unicode")
_ATTENTION = chrome("attention", mode="unicode")
_DISPATCH = chrome("dispatch", mode="unicode")
_OVERVIEW = chrome("overview", mode="unicode")

_MC_PREFIX = "   ui.dashboard_panes [multichoice] "
_MC_CHOICES = ("state", "roadmap", "backlog", "events", "git", "registry", "trust")


def _binding_for(screen: object, key: str) -> Binding:
    """Return the live ``Binding`` a *key* resolves to on *screen*.

    Args:
        screen: The mounted overlay whose ``BINDINGS`` to scan.
        key: The bound key string (e.g. ``"enter"``).

    Returns:
        The first ``Binding`` declared for *key*.

    Raises:
        AssertionError: When *key* has no live binding on *screen*.
    """
    for binding in screen.BINDINGS:  # type: ignore[attr-defined]
        if isinstance(binding, Binding) and binding.key == key:
            return binding
    raise AssertionError(f"no live binding for key {key!r}")


# --------------------------------------------------------------------------
# MultichoiceChecklist -- filled / empty toggle glyphs via chrome
# --------------------------------------------------------------------------


def test_multichoice_toggle_glyphs_are_chrome_check_marks() -> None:
    """Each option row renders the filled / empty chrome toggle, not ``[X]``/``[ ]``."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            checklist = MultichoiceChecklist(
                choices=_MC_CHOICES,
                selected=["backlog"],
                prefix=_MC_PREFIX,
                id="mc",
            )
            await app.mount(checklist)
            await pilot.pause()
            lines = str(checklist.render()).splitlines()
            # The legacy hardcoded marks are gone everywhere in the render.
            assert "[X]" not in str(checklist.render())
            # The pre-selected choice carries the filled check_on lozenge...
            backlog_line = next(line for line in lines if "backlog" in line)
            assert _CHECK_ON in backlog_line
            # ...and a cleared choice carries the hollow check_off square.
            state_line = next(line for line in lines if line.rstrip().endswith("state"))
            assert _CHECK_OFF in state_line

    asyncio.run(body())


def test_multichoice_affordance_parity_toggle_keys_fire() -> None:
    """affordance_parity: space toggles + enter commits resolve to live Bindings + fire."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            checklist = MultichoiceChecklist(
                choices=_MC_CHOICES,
                selected=[],
                prefix=_MC_PREFIX,
                id="mc",
            )
            await app.mount(checklist)
            await pilot.pause()
            # affordance_parity: the toggle + commit keys resolve to live
            # Bindings naming callable action_* handlers on the widget.
            assert _binding_for(checklist, "space").action == "toggle_item"
            assert _binding_for(checklist, "enter").action == "commit"
            for action in ("action_toggle_item", "action_commit"):
                assert callable(getattr(checklist, action))

            checklist.focus()
            # Fire the toggle key: the focused choice flips to the filled mark.
            await pilot.press("space")
            await pilot.pause()
            assert checklist.selected_items() == ["state"]
            first_option = str(checklist.render()).splitlines()[1]
            assert _CHECK_ON in first_option

    asyncio.run(body())


def test_multichoice_commit_key_posts_selected_items() -> None:
    """The enter binding fires action_commit, posting the toggled selection.

    Mounts the checklist under a bare ``App[None]`` (which carries the
    ``Committed`` handler) rather than a :class:`~eawf.surfaces.tui.app.EaApp`
    subclass: subclassing ``EaApp`` reanchors its ``CSS_PATH`` to this test
    module's directory, where ``theme.tcss`` does not live. The bare harness
    exposes no ``render_mode``, so the checklist resolves the unicode toggle
    fallback -- which is all the commit-fire semantics need.
    """
    captured: list[list[str]] = []

    class _CaptureHarness(App[None]):
        def compose(self) -> ComposeResult:
            yield MultichoiceChecklist(
                choices=_MC_CHOICES,
                selected=[],
                prefix=_MC_PREFIX,
                id="mc",
            )

        def on_multichoice_checklist_committed(
            self, message: MultichoiceChecklist.Committed
        ) -> None:
            captured.append(message.selected)

    async def body() -> None:
        app = _CaptureHarness()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            checklist = app.query_one("#mc", MultichoiceChecklist)
            checklist.focus()
            await pilot.press("space")  # toggle "state" ON
            await pilot.press("enter")  # commit
            await pilot.pause()

    asyncio.run(body())
    assert captured == [["state"]]


# --------------------------------------------------------------------------
# ConfirmModal -- attention sigil + key-hint vocab
# --------------------------------------------------------------------------


def test_confirm_prompt_wears_attention_sigil_and_hint() -> None:
    """The destructive prompt leads with the attention sigil; a key-hint footer renders."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = ConfirmModal("Drop wave P01-I01-W01?")
            app.push_screen(modal)
            await pilot.pause()
            prompt = str(modal.query_one(".confirm-prompt", Static).render())
            assert _ATTENTION in prompt
            assert "Drop wave P01-I01-W01?" in prompt
            hint = str(modal.query_one(".confirm-hint", Static).render())
            assert "Enter confirm" in hint
            assert "Esc cancel" in hint

    asyncio.run(body())


def test_confirm_affordance_parity_keys_fire() -> None:
    """affordance_parity: the confirm keys resolve to live Bindings + fire to a result."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        sink: list[bool | None] = []
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = ConfirmModal("Drop wave?")
            app.push_screen(modal, callback=sink.append)
            await pilot.pause()
            # affordance_parity: the move / confirm / cancel keys resolve to
            # live Bindings naming callable action_* handlers.
            assert _binding_for(modal, "right").action == "move(1)"
            assert _binding_for(modal, "enter").action == "confirm"
            assert _binding_for(modal, "escape").action == "cancel"
            for action in ("action_move", "action_confirm", "action_cancel"):
                assert callable(getattr(modal, action))

            # Fire the right + confirm keys: the highlighted "Yes" returns True.
            await pilot.press("right")
            await pilot.pause()
            assert modal.selected == 1
            await pilot.press("enter")
            await pilot.pause()
        assert sink == [True]

    asyncio.run(body())


# --------------------------------------------------------------------------
# InitWizardModal -- dispatch sigil + key-hint vocab
# --------------------------------------------------------------------------


def test_init_wizard_header_wears_brand_sigil_and_footer_hint() -> None:
    """The stepped wizard header wears the brand sigil; the footer carries the chord vocab.

    Updated for the W09 stepped wizard (the W08 redesign replaced the
    three-action chooser): the dispatch / brand sigil now leads the header +
    path rows rather than a ``.init-title`` row, and the per-step footer (not a
    fixed ``.init-hint``) carries the key-hint chord vocab.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = InitWizardModal(
                InitWizardContext(scope="repo", target_dir=Path("/abs/path/repo"))
            )
            app.push_screen(modal)
            await pilot.pause()
            header = str(modal.query_one("#init-header", Static).render())
            assert chrome("brand", mode="unicode") in header
            assert "Eä" in header
            foot = str(modal.query_one("#init-foot", Static).render())
            assert "Enter preview" in foot
            assert "Esc cancel" in foot

    asyncio.run(body())


def test_init_wizard_affordance_parity_keys_fire() -> None:
    """affordance_parity: the stepped-wizard keys resolve to live Binding actions."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = InitWizardModal(
                InitWizardContext(scope="repo", target_dir=Path("/abs/path/repo"))
            )
            app.push_screen(modal)
            await pilot.pause()
            assert _binding_for(modal, "enter").action == "advance"
            assert _binding_for(modal, "escape").action == "cancel"
            assert _binding_for(modal, "space").action == "toggle_chip"
            for action in (
                "action_move",
                "action_advance",
                "action_cancel",
                "action_toggle_chip",
                "action_select_all",
                "action_path",
            ):
                assert callable(getattr(modal, action))

    asyncio.run(body())


# --------------------------------------------------------------------------
# Narrow-sigil PR grid -- no glyph strands against the selection rectangle
# --------------------------------------------------------------------------


def test_pr_list_rows_lead_with_narrow_dispatch_sigil() -> None:
    """Each PR row leads with the single-cell dispatch sigil + carries the state."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        rows = (
            PrRow(12, "fix one", "alice", "OPEN", "https://example.test/12"),
            PrRow(13, "fix two", "bob", "OPEN", "https://example.test/13"),
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = PrListModal(rows, PrFetchStatus.OK)
            app.push_screen(modal)
            await pilot.pause()
            # The title leads with the overview sigil.
            title = str(modal.query_one(".pr-title", Static).render())
            assert _OVERVIEW in title
            # Each row leads with the narrow dispatch sigil and carries the
            # number, title, author, and state count.
            row0 = str(modal.query_one("#pr-row-0", Static).render())
            assert _DISPATCH in row0
            assert "#12" in row0
            assert "fix one" in row0
            assert "@alice" in row0
            assert "OPEN" in row0
            # The leading sigil is a single cell, so it never strands: the
            # row text starts with the one-cell glyph then a space.
            assert row0.startswith(f"{_DISPATCH} ")

    asyncio.run(body())


def test_pr_list_affordance_parity_keys_fire() -> None:
    """affordance_parity: the PR-grid nav keys resolve to live Bindings + fire."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        rows = (
            PrRow(12, "fix one", "alice", "OPEN", "https://example.test/12"),
            PrRow(13, "fix two", "bob", "OPEN", "https://example.test/13"),
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = PrListModal(rows, PrFetchStatus.OK)
            app.push_screen(modal)
            await pilot.pause()
            assert _binding_for(modal, "down").action == "move(1)"
            assert _binding_for(modal, "enter").action == "open_web"
            assert _binding_for(modal, "escape").action == "close"
            for action in ("action_move", "action_open_web", "action_close"):
                assert callable(getattr(modal, action))

            # Fire the down key: the highlight moves onto the second row.
            assert modal.query_one("#pr-row-0", Static).has_class("-selected")
            await pilot.press("down")
            await pilot.pause()
            assert modal.query_one("#pr-row-1", Static).has_class("-selected")

    asyncio.run(body())


def test_cross_repo_pr_grid_rows_lead_with_narrow_dispatch_sigil() -> None:
    """The cross-repo grid leads its title + each PR row with the shared sigils."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        groups = (
            CrossRepoGroup(
                "ABC",
                "ABC repo",
                (
                    PrRow(11, "tidy the docs", "alice", "OPEN", "https://example.test/11"),
                    PrRow(12, "fix the parser", "bob", "OPEN", "https://example.test/12"),
                ),
                PrFetchStatus.OK,
            ),
            CrossRepoGroup("GHI", "GHI repo", (), PrFetchStatus.UNAVAILABLE),
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = CrossRepoPrModal(groups)
            app.push_screen(modal)
            await pilot.pause()
            title = str(modal.query_one(".xpr-title", Static).render())
            assert _OVERVIEW in title
            row0 = str(modal.query_one("#xpr-row-0", Static).render())
            assert _DISPATCH in row0
            assert "#11" in row0
            assert "tidy the docs" in row0
            assert "OPEN" in row0
            # The narrow sigil sits one cell in (after the two-space group
            # indent), so it never strands against the selection rectangle.
            assert row0.lstrip().startswith(f"{_DISPATCH} ")

    asyncio.run(body())


def test_cross_repo_pr_affordance_parity_keys_fire() -> None:
    """affordance_parity: the cross-repo nav keys resolve to live Bindings + fire."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        groups = (
            CrossRepoGroup(
                "ABC",
                "ABC repo",
                (
                    PrRow(11, "tidy the docs", "alice", "OPEN", "https://example.test/11"),
                    PrRow(12, "fix the parser", "bob", "OPEN", "https://example.test/12"),
                ),
                PrFetchStatus.OK,
            ),
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            modal = CrossRepoPrModal(groups)
            app.push_screen(modal)
            await pilot.pause()
            assert _binding_for(modal, "down").action == "move(1)"
            assert _binding_for(modal, "enter").action == "open_web"
            assert _binding_for(modal, "escape").action == "close"
            for action in ("action_move", "action_open_web", "action_close"):
                assert callable(getattr(modal, action))

            # Fire the down key: the highlight moves onto the second PR row.
            assert modal.query_one("#xpr-row-0", Static).has_class("-selected")
            await pilot.press("down")
            await pilot.pause()
            assert modal.query_one("#xpr-row-1", Static).has_class("-selected")

    asyncio.run(body())


# --------------------------------------------------------------------------
# Isolated-overlay snapshot goldens -- the reskin glyph vocab in one frame
#
# These mount each overlay IN ISOLATION (no full-app screen) and byte-match
# a golden under this test's local ``golden/reskin_w22/`` dir, so the filled
# / empty toggle marks, the confirm / init sigil + hint vocab, and the
# narrow-sigil PR grid are pinned without touching the forbidden full-app
# ``confirm_overlay.txt`` / ``cross_repo_pr_overlay.txt`` goldens.
#
# Regenerate after an intentional layout change with::
#
#     EAWF_SNAPSHOT_REGEN=1 EAWF_DAEMONLESS=1 \
#         uv run pytest tests/tui/test_overlays_reskin_w22.py
# --------------------------------------------------------------------------


def test_multichoice_checklist_snapshot() -> None:
    """The checklist frame pins the filled / empty chrome toggle marks."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            checklist = MultichoiceChecklist(
                choices=_MC_CHOICES,
                selected=["roadmap", "trust"],
                prefix=_MC_PREFIX,
                id="mc",
            )
            await app.mount(checklist)
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "multichoice_checklist.txt")

    asyncio.run(body())


def test_confirm_overlay_reskin_snapshot() -> None:
    """The confirm overlay frame pins the attention sigil + key-hint vocab."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            app.push_screen(ConfirmModal("Abandon wave P01-I01-W01?"))
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "confirm_overlay_reskin.txt")

    asyncio.run(body())


# The init-wizard frame snapshot moved to the dedicated W09 suite
# ``tests/tui/test_init_wizard_snapshots.py`` when the stepped wizard replaced
# the W22-era three-action chooser: that suite pins every journey state
# (J1 hero / J2 configure-preview-execute-error / J3 select / J4 done) +
# the 80-column narrow variants in isolation (no full-app daemon banner). The
# brand-sigil + footer-vocab assertions for the wizard live in
# ``test_init_wizard_header_wears_brand_sigil_and_footer_hint`` above.


def test_pr_list_grid_snapshot() -> None:
    """The PR-list frame pins the narrow-sigil grid (no glyph stranding)."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        rows = (
            PrRow(11, "tidy the docs", "alice", "OPEN", "https://example.test/11"),
            PrRow(12, "fix the parser", "bob", "OPEN", "https://example.test/12"),
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            app.push_screen(PrListModal(rows, PrFetchStatus.OK))
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "pr_list_grid.txt")

    asyncio.run(body())
