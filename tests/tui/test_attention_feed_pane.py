"""Pilot tests for the Home attention-feed band (P29-I02-W19).

The Home overview band (:class:`~eawf.surfaces.tui.widgets.attention_feed.AttentionFeed`)
leads every scope screen's body, rendering the ranked attention feed above
the scope view. These tests drive it under a real :class:`EaApp`:

* the band mounts on every scope screen and renders the ranked feed;
* selecting (``Enter``) a needs_user row opens that pause's
  :class:`NeedsUserModal` through the shared ``open_needs_user_pause``;
* an empty feed renders the honest-empty
  :data:`~eawf.surfaces.tui.attention.EMPTY_FEED_TEXT` note;
* the orthogonal scope axis is intact -- ``w`` / ``r`` / ``u`` still swap
  the scope screen beneath the band (the W16 chassis contract), and the
  band rebuilds on the new scope screen.

Determinism follows the Pilot-worker rule: each body drains workers via
:func:`~eawf.surfaces.tui.snapshot.settle_screen` before asserting. The
autouse ``_isolate_registry`` fixture redirects ``Path.home`` so a ``u``
switch never reads the operator's real registry.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest
from textual.widgets import Static

from eawf.kernel.state.enums import Urgency
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.attention import EMPTY_FEED_TEXT, AttentionKind
from eawf.surfaces.tui.scopes import RepoScreen, UserScreen, WorkspaceScreen
from eawf.surfaces.tui.screens.overlays.needs_user import NeedsUserModal
from eawf.surfaces.tui.snapshot import settle_screen
from eawf.surfaces.tui.widgets.attention_feed import AttentionFeed
from eawf.workflow.skills.bodies.user_question import UserQuestion, UserQuestionOption
from eawf.workflow.skills.needs_user import record_pause

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"
_WORKSPACE = _FIXTURES / "05-workspace-state.json"
_SCOPE = "urn:eawf:v1:state:QR"  # matches the fixture's State.urn
_SESSION = "urn:eawf:v1:session:cli/SES-tui"


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point registry resolution at an empty ``tmp_path`` home for ``u``."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


def _question(text: str) -> UserQuestion:
    return UserQuestion(
        question=text,
        options=[UserQuestionOption(label="apply"), UserQuestionOption(label="cancel")],
    )


def _temp_state(tmp_path: Path) -> Path:
    ea = tmp_path / ".ea"
    ea.mkdir()
    path = ea / "state.json"
    shutil.copyfile(_PHASE_ITER_WAVE, path)
    return path


async def _dismiss_autoopen(pilot: object) -> None:
    """Clear any auto-opened needs_user / init modal off the stack."""
    app = pilot.app  # type: ignore[attr-defined]
    while app.screen_stack and app.screen.__class__.__name__ in (
        "NeedsUserModal",
        "InitWizardModal",
    ):
        await pilot.press("escape")  # type: ignore[attr-defined]
        await settle_screen(pilot)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Band mounts on every scope screen
# --------------------------------------------------------------------------


def test_attention_band_mounts_on_repo_scope() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            assert isinstance(app.screen, RepoScreen)
            assert app.screen.query_one(AttentionFeed)

    asyncio.run(body())


def test_attention_band_renders_honest_empty_when_no_signals() -> None:
    async def body() -> None:
        # The base fixture has one IN_PROGRESS wave + no pauses -> empty feed.
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            feed = app.screen.query_one(AttentionFeed)
            assert feed.items() == ()
            empties = feed.query(".attention-empty")
            assert empties
            assert EMPTY_FEED_TEXT in str(empties.first(Static).render())

    asyncio.run(body())


# --------------------------------------------------------------------------
# Feed renders ranked rows off seeded pauses
# --------------------------------------------------------------------------


def test_attention_band_renders_seeded_pause_rows_ranked(tmp_path: Path) -> None:
    async def body() -> None:
        state_path = _temp_state(tmp_path)
        record_pause(
            state_path,
            scope_id=_SCOPE,
            session=_SESSION,
            question=_question("the low pause"),
            urgency=Urgency.LOW,
        )
        record_pause(
            state_path,
            scope_id=_SCOPE,
            session=_SESSION,
            question=_question("the urgent pause"),
            urgency=Urgency.URGENT,
        )
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await _dismiss_autoopen(pilot)
            feed = app.screen.query_one(AttentionFeed)
            items = feed.items()
            # Two needs_user rows, urgent ranked first.
            kinds = [i.kind for i in items]
            assert kinds.count(AttentionKind.NEEDS_USER) == 2
            assert items[0].urgency is Urgency.URGENT
            assert "the urgent pause" in items[0].title

    asyncio.run(body())


# --------------------------------------------------------------------------
# Enter on a pause row opens its modal
# --------------------------------------------------------------------------


def test_attention_band_enter_opens_pause_modal(tmp_path: Path) -> None:
    async def body() -> None:
        state_path = _temp_state(tmp_path)
        record_pause(
            state_path,
            scope_id=_SCOPE,
            session=_SESSION,
            question=_question("answer me from the band"),
            urgency=Urgency.URGENT,
        )
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await _dismiss_autoopen(pilot)
            assert not any(isinstance(s, NeedsUserModal) for s in app.screen_stack)
            feed = app.screen.query_one(AttentionFeed)
            feed.focus()
            await settle_screen(pilot)
            # Activate the highlighted (urgent) row -> its modal opens.
            feed.action_activate()
            await settle_screen(pilot)
            assert isinstance(app.screen, NeedsUserModal)
            question_cell = app.screen.query_one(".needs-user-question", Static)
            assert "answer me from the band" in str(question_cell.render())

    asyncio.run(body())


def test_attention_band_activate_informational_row_opens_no_modal(tmp_path: Path) -> None:
    async def body() -> None:
        # A ready-to-claim wave is informational, not actionable: activating
        # it posts no PauseSelected and opens no modal.
        state_path = _temp_state(tmp_path)
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await _dismiss_autoopen(pilot)
            feed = app.screen.query_one(AttentionFeed)
            # Inject an informational (ready-wave) item directly and activate.
            from eawf.surfaces.tui.attention import AttentionItem

            feed._items = (
                AttentionItem(
                    urgency=Urgency.NORMAL,
                    kind=AttentionKind.READY_WAVE,
                    title="P01-I01-W02 ready",
                    detail="ready to claim",
                ),
            )
            feed.action_activate()
            await settle_screen(pilot)
            assert app.modal_depth() == 0

    asyncio.run(body())


# --------------------------------------------------------------------------
# Orthogonal scope axis intact (W16 chassis contract preserved)
# --------------------------------------------------------------------------


def test_attention_band_scope_switch_still_works() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            assert app.current_mode == "home"
            assert isinstance(app.screen, RepoScreen)
            # The band rides the repo scope.
            assert app.screen.query_one(AttentionFeed)
            # Scope switch within Home: screen swaps, band rebuilds on it.
            await pilot.press("w")
            await settle_screen(pilot)
            assert app.current_mode == "home"
            assert isinstance(app.screen, WorkspaceScreen)
            assert app.screen.query_one(AttentionFeed)
            await pilot.press("u")
            await settle_screen(pilot)
            await _dismiss_autoopen(pilot)
            assert app.current_mode == "home"
            assert isinstance(app.screen, UserScreen)
            assert app.screen.query_one(AttentionFeed)
            # Back to repo: scope axis fully reversible with the band present.
            await pilot.press("r")
            await settle_screen(pilot)
            assert isinstance(app.screen, RepoScreen)
            assert app.screen.query_one(AttentionFeed)

    asyncio.run(body())


def test_attention_band_survives_zoom_on_workspace() -> None:
    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, WorkspaceScreen)
            # The band is a sibling of the browse pane, so a zoom (which hides
            # only #pane-repos) leaves the band mounted.
            from eawf.surfaces.tui.widgets.workspace_table import WorkspaceTable

            await screen.on_workspace_table_row_zoomed(WorkspaceTable.RowZoomed("QR"))
            await settle_screen(pilot)
            assert screen.zoomed
            assert screen.query_one(AttentionFeed)

    asyncio.run(body())
