"""Golden ASCII snapshot tests for the C06 ``tui`` operator surface.

Drives each key screen / overlay to a known position via Textual's
``App.run_test()`` Pilot and asserts the rendered terminal (captured as
plain ASCII text per the C06 Q-new1 OVERRIDE) byte-matches a committed
golden under ``tests/snapshots/tui/golden/``.

Why a focused set rather than the brief's full ~16-fixture inventory:
each fixture spins a fresh Textual app under ``run_test`` (≈40-80 ms of
mount + settle), and the band's plan flagged that the full ~16-fixture
matrix risks the CI runtime budget. This suite therefore covers the
three scope screens (repo / workspace / user) plus two representative
overlays (help + a destructive confirm) — enough to catch a layout or
chrome regression on every screen *kind* and the overlay capture path,
without paying the per-fixture spin-up cost 16 times. The harness
(:func:`eawf.surfaces.tui.snapshot.assert_screen_snapshot`) is reusable, so
expanding the set later is one ``assert`` per added golden.

Determinism note: the repo / workspace screens carry a ``GitPane`` that
probes the **live** working tree (``git status`` count, branch, recent
commit subjects, ahead/behind). That output varies run-to-run and would
leak the real branch / commit text into a golden. The autouse
:func:`_isolated_cwd` fixture stubs ``git_pane._git_run`` to ``None`` so
every git field resolves to a deterministic dash regardless of cwd,
platform, or ``pytest -n auto`` worker — stable *and* scrub-safe. (The
chdir into a non-git temp dir stays as a belt-and-suspenders guard; the
stub is what makes the dashes immune to a cross-worker cwd leak.) The
fixture state itself comes from the absolute fixture paths above,
unaffected by the cwd switch.

Regenerate the goldens after an intentional layout change with::

    EAWF_SNAPSHOT_REGEN=1 uv run pytest tests/snapshots/tui/
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import orjson
import pytest
from textual.widgets import TabbedContent

from eawf.kernel.config.registry import registry_lookup
from eawf.kernel.state.enums import EffortBucket
from eawf.kernel.state.models import State
from eawf.kernel.store.envelope import Envelope
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modes.doctor import DoctorHealth, DoctorModeScreen, HealthRow
from eawf.surfaces.tui.screens.overlays.config_modal import ConfigModal
from eawf.surfaces.tui.screens.overlays.confirm import ConfirmModal
from eawf.surfaces.tui.screens.overlays.cross_repo_pr import CrossRepoGroup, CrossRepoPrModal
from eawf.surfaces.tui.screens.overlays.detail import DetailModal, resolve_detail
from eawf.surfaces.tui.screens.overlays.edit_field import EditFieldModal
from eawf.surfaces.tui.screens.overlays.events import EventRow, EventsModal, _row_from_envelope
from eawf.surfaces.tui.screens.overlays.pr_list import PrFetchStatus, PrRow
from eawf.surfaces.tui.snapshot import assert_screen_snapshot, settle_screen


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the git pane to dashes + disable Textual animations.

    Three determinism guards for the goldens:

    * The git pane shells out via ``git_pane._git_run``; stubbing it to
      return ``None`` makes ``gather_git_fields`` resolve every field to a
      dash regardless of cwd, platform, or ``pytest -n auto`` worker. This
      is the load-bearing guard — a parallel test can leak the repo cwd
      into the worker, and probing a real repo would otherwise render the
      live branch / status into the golden and drift it (the ubuntu CI
      symptom). The stub keeps the dashes deterministic everywhere.
    * Chdir into a fresh non-git temp dir as belt-and-suspenders: the pane
      probes ``Path.cwd()`` on construction, so even if the stub were
      bypassed the cwd points away from any repo.
    * Disabling animations settles time-driven chrome (notably the
      ``TabbedContent`` underline marker the config overlay uses) to its
      final position immediately, so a capture under scheduler load cannot
      catch a mid-animation frame and flake the snapshot. Textual reads
      ``constants.TEXTUAL_ANIMATIONS`` once at import, so the env var is
      already cached by test time — patch the constant directly (the App
      copies it into ``self.animation_level`` at construction).
    """
    import textual.constants as _tc

    monkeypatch.setattr(_tc, "TEXTUAL_ANIMATIONS", "none")
    monkeypatch.setattr("eawf.surfaces.tui.widgets.git_pane._git_run", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    # Point registry resolution at an empty ``tmp_path`` home so the
    # workspace REGISTRY pane + the user PORTFOLIO table render their
    # honest-empty surface deterministically -- and never leak the
    # operator's real ``~/.eawf/registry.json`` repo paths into a golden.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # The Doctor-mode mount runs the instrument probe, which writes a cache
    # to ``<workspace>/.ea/instrument-probe.json`` -- the workspace resolves
    # to the fixture tree, so redirect the cache into tmp_path to keep a
    # stray probe file out of ``tests/fixtures/``.
    monkeypatch.setenv("EA_INSTRUMENT_PROBE", str(tmp_path / "instrument-probe.json"))


#: Fixed terminal geometry for every snapshot — wide enough for the 2x2
#: quadrant + overlay boxes, matching the asciinema cast viewport so
#: screen + cast frames line up.
_SIZE = (120, 40)

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "states" / "valid"
_REPO_STATE = _FIXTURES / "03-phase-iter-wave-active.json"
_WORKSPACE_STATE = _FIXTURES / "05-workspace-state.json"
_PORTFOLIO_STATE = _FIXTURES / "07-decisions-and-backlog.json"
#: A repo with no phases / iters / waves — the truly data-starved case the
#: Trust pane renders as an honest-negative ("insufficient data") banner.
_EMPTY_STATE = _FIXTURES / "01-empty-repo.json"

# Fail loudly on a path mistake: the read-only binder returns ``None`` for
# a missing file (degrading to an empty-scope placeholder), so a wrong
# fixture path would silently snapshot the placeholder rather than the
# populated screen. Guard at import so the breakage is unmissable.
assert _REPO_STATE.is_file(), f"missing snapshot fixture: {_REPO_STATE}"
assert _WORKSPACE_STATE.is_file(), f"missing snapshot fixture: {_WORKSPACE_STATE}"
assert _PORTFOLIO_STATE.is_file(), f"missing snapshot fixture: {_PORTFOLIO_STATE}"
assert _EMPTY_STATE.is_file(), f"missing snapshot fixture: {_EMPTY_STATE}"

_GOLDEN = Path(__file__).resolve().parent / "golden"


# --------------------------------------------------------------------------
# Scope screens — one golden per screen kind
# --------------------------------------------------------------------------


def test_repo_screen_snapshot() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "repo_screen.txt")

    asyncio.run(body())


def test_workspace_screen_snapshot() -> None:
    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "workspace_screen.txt")

    asyncio.run(body())


def test_repo_git_pane_dashes_from_repo_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    """The git-pane stub — not the chdir — is what guarantees the DASH golden.

    Regression for the ubuntu ``pytest -n auto`` drift: a parallel test can
    leak the repo cwd into the worker, so the repo git pane probes a real
    repo and renders the live branch instead of dashes. This test undoes the
    autouse fixture's chdir (pointing cwd back at the repo root, a genuine
    git work tree) while keeping the ``_git_run`` stub active, and asserts
    the GIT pane still resolves to dashes. With the stub gone the pane would
    render the real branch — so a green assertion here proves the stub
    neutralizes the cwd leak.

    Targets the repo screen: the workspace screen now surfaces git in a
    per-repo :class:`~eawf.surfaces.tui.widgets.workspace_table.WorkspaceTable` column
    (no standalone ``GitPane`` in its table-browse mode), so the
    standalone-pane cwd-leak guard rides the repo quadrant's git pane.

    The function-scoped ``monkeypatch.chdir`` is unwound at teardown, so the
    test does not itself leak the repo cwd into a sibling worker.
    """
    from eawf.surfaces.tui.widgets.git_pane import DASH as GIT_DASH
    from eawf.surfaces.tui.widgets.git_pane import GitPane

    repo_root = Path(__file__).resolve().parents[3]
    assert (repo_root / ".git").exists(), f"expected a git work tree at {repo_root}"
    monkeypatch.chdir(repo_root)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            pane = app.screen.query_one(GitPane)
            assert pane._fields is not None
            assert pane._fields.branch == GIT_DASH
            assert pane._fields.dirty == GIT_DASH
            assert pane._fields.ahead_behind == GIT_DASH
            assert pane._fields.recent_commits == ()

    asyncio.run(body())


def test_user_screen_snapshot() -> None:
    async def body() -> None:
        app = EaApp(scope="user", state_path=_PORTFOLIO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "user_screen.txt")

    asyncio.run(body())


# --------------------------------------------------------------------------
# Feed mode (digit 5) — live event-feed pane, empty + populated
# --------------------------------------------------------------------------


def _feed_event(event_id: str, summary: str) -> Envelope:
    """Build a fixed-timestamp event envelope for the Feed-pane goldens."""
    return Envelope(
        id=event_id,
        kind="event",  # type: ignore[arg-type]
        scope_id="urn:eawf:v1:state:QR",
        created_at=datetime(2026, 5, 27, 9, 30, 15, tzinfo=UTC),
        updated_at=None,
        summary=summary,
        payload={"event_type": "test", "status": "ok"},
    )


def test_feed_mode_empty_snapshot() -> None:
    """The Feed pane renders an honest-empty live notice before any event."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press("7")  # -> Feed mode
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "feed_mode_empty.txt")

    asyncio.run(body())


def test_feed_mode_populated_snapshot() -> None:
    """The Feed pane renders buffered events newest-first on switch-in."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            # Seed deterministic events into the App buffer before switching;
            # the Feed pane seeds from the buffer on mount (newest-first).
            await app._on_event(_feed_event("EV-1", "wave P01-I01-W01 claimed"))
            await app._on_event(_feed_event("EV-2", "wave P01-I01-W01 closed"))
            await settle_screen(pilot)
            await pilot.press("7")  # -> Feed mode
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "feed_mode_populated.txt")

    asyncio.run(body())


def _make_wave(wave_id: str, title: str) -> dict[str, object]:
    """Build a minimal pending-wave payload dict for fixture composition."""
    return {
        "id": wave_id,
        "iter_id": "P01-I01",
        "title": title,
        "status": "pending",
        "deps": [],
        "blocks": [],
        "file_scopes": [],
        "success_criteria": [],
        "opened_at": "2026-05-08T00:00:00Z",
        "closed_at": None,
    }


def _long_iter_many_waves_state(tmp_path: Path) -> Path:
    """Write the repo fixture mutated to a long iter title + 39 waves.

    The committed goldens carry short titles + a single wave, so no
    scrollbar ever appears and the row budget never had to reserve its
    gutter. This case forces the vertical scrollbar (39 waves overflow the
    40-row pane) and a long iter title, so the iter row's trailing
    completion-bar count lands at the right edge — exactly where a missing
    gutter would clip it.
    """
    payload = orjson.loads(_REPO_STATE.read_bytes())
    ids = [f"P01-I01-W{n:02d}" for n in range(1, 40)]
    payload["iters"]["P01-I01"]["wave_ids"] = ids
    payload["iters"]["P01-I01"]["title"] = (
        "First iteration with an extremely long descriptive title that overflows"
    )
    payload["waves"] = {wid: _make_wave(wid, f"wave {n} title") for n, wid in enumerate(ids, 1)}
    State.model_validate(payload)  # fail fast on a malformed mutation
    out = tmp_path / "long_iter_state.json"
    out.write_bytes(orjson.dumps(payload))
    return out


def test_roadmap_scrolled_long_iter_snapshot(tmp_path: Path) -> None:
    """A scrolled roadmap keeps the iter row's completion count on screen.

    Regression golden for the W12 review issue: with the vertical scrollbar
    showing, the iter row's ``0/39`` count must render in full — a missing
    scrollbar gutter in the row budget clipped the trailing digits under
    ``overflow-x: hidden``.
    """

    async def body() -> None:
        state_path = _long_iter_many_waves_state(tmp_path)
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "roadmap_scrolled_long_iter.txt")

    asyncio.run(body())


# --------------------------------------------------------------------------
# Trust mode pane — populated vs honest-negative (data-starved)
# --------------------------------------------------------------------------


def _trust_populated_state(tmp_path: Path) -> Path:
    """Write a repo state with one CLOSED wave plus a verified-evidence row.

    The Trust pane computes its scorecard through
    ``compute_trust_scorecard`` -> ``read_store_projection``, so a populated
    golden needs both a closed wave (to label) and a deterministic-pass
    evidence row under ``store/evidence.jsonl`` (to lift the label to the
    ``verified`` tier). Built off the committed repo fixture so the chrome
    matches the other goldens.
    """
    payload = orjson.loads(_REPO_STATE.read_bytes())
    payload["waves"]["P01-I01-W01"]["status"] = "closed"
    payload["waves"]["P01-I01-W01"]["closed_at"] = "2026-05-08T01:00:00Z"
    payload["iters"]["P01-I01"]["status"] = "closed"
    payload["iters"]["P01-I01"]["closed_at"] = "2026-05-08T01:00:00Z"
    payload["phases"]["P01"]["status"] = "closed"
    payload["phases"]["P01"]["closed_at"] = "2026-05-08T01:00:00Z"
    payload["current"]["phase_id"] = None
    payload["current"]["iter_id"] = None
    payload["current"]["active_wave_ids"] = []
    State.model_validate(payload)  # fail fast on a malformed mutation
    ea_dir = tmp_path / ".ea"
    ea_dir.mkdir(parents=True, exist_ok=True)
    state_path = ea_dir / "state.json"
    state_path.write_bytes(orjson.dumps(payload))
    record = {
        "id": "EV-aaaaaaaaaaaa",
        "scope_id": "P01-I01-W01",
        "produced_by": "tool",
        "evidence_kind": "deterministic",
        "status": "pass",
        "summary": "pytest gate passed",
        "created_at": "2026-05-08T01:00:00Z",
    }
    envelope = {
        "schema_version": "1.0",
        "id": "EV-aaaaaaaaaaaa",
        "kind": "evidence",
        "scope_id": "P01-I01-W01",
        "created_at": "2026-05-08T01:00:00Z",
        "updated_at": "2026-05-08T01:00:00Z",
        "summary": "evidence P01-I01-W01",
        "payload": record,
    }
    store_dir = ea_dir / "store"
    store_dir.mkdir(parents=True, exist_ok=True)
    (store_dir / "evidence.jsonl").write_bytes(orjson.dumps(envelope) + b"\n")
    return state_path


def test_trust_pane_populated_snapshot(tmp_path: Path) -> None:
    """The Trust pane (digit 4) over a closed+verified repo renders its scorecard."""

    async def body() -> None:
        state_path = _trust_populated_state(tmp_path)
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press("4")  # -> trust mode
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "trust_pane_populated.txt")

    asyncio.run(body())


def test_trust_pane_data_starved_snapshot() -> None:
    """The Trust pane over an empty repo renders the honest-negative banner."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_EMPTY_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press("4")  # -> trust mode
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "trust_pane_data_starved.txt")

    asyncio.run(body())


# --------------------------------------------------------------------------
# Overlays — exercise the topmost-modal capture path
# --------------------------------------------------------------------------


def test_help_overlay_snapshot() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            app.action_open_help()
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "help_overlay.txt")

    asyncio.run(body())


def test_confirm_overlay_snapshot() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            app.push_modal(ConfirmModal("Abandon wave P01-I01-W01?"))
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "confirm_overlay.txt")

    asyncio.run(body())


def test_detail_overlay_snapshot() -> None:
    """The wave detail tab renders its NarrativeBundle preview.

    Lands on the default ``d`` (detail) tab — the enlarged box + the tab
    strip frame the rendered What/Why/Validation/Risks preview.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            state = app.state
            assert state is not None
            wave_id = next(iter(state.waves))
            app.push_modal(DetailModal(resolve_detail(state, wave_id)))
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "detail_overlay.txt")

    asyncio.run(body())


def _bucketed_wave_state() -> tuple[State, str]:
    """Return the repo-fixture state with its wave's effort bucket set to ``L``.

    The committed fixtures leave ``effort_bucket`` unset, so the size-bar
    golden needs a bucket injected (the frozen model is rebuilt via
    ``model_copy``).
    """
    state = State.model_validate(orjson.loads(_REPO_STATE.read_bytes()))
    wave_id = next(iter(state.waves))
    bucketed = state.waves[wave_id].model_copy(update={"effort_bucket": EffortBucket.L})
    new_waves = dict(state.waves)
    new_waves[wave_id] = bucketed
    return state.model_copy(update={"waves": new_waves}), wave_id


def test_detail_overlay_iter_metrics_snapshot() -> None:
    """The enlarged iter modal on its ``m`` tab shows the completion bar."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            state = app.state
            assert state is not None
            iter_id = next(iter(state.iters))
            modal = DetailModal(resolve_detail(state, iter_id))
            app.push_modal(modal)
            await settle_screen(pilot)
            modal.query_one(TabbedContent).active = "detail-tab-m"
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "detail_overlay_iter_metrics.txt")

    asyncio.run(body())


def test_detail_overlay_wave_size_snapshot() -> None:
    """The enlarged wave modal on its ``m`` tab shows the effort-size bar."""

    async def body() -> None:
        state, wave_id = _bucketed_wave_state()
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            modal = DetailModal(resolve_detail(state, wave_id))
            app.push_modal(modal)
            await settle_screen(pilot)
            modal.query_one(TabbedContent).active = "detail-tab-m"
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "detail_overlay_wave_size.txt")

    asyncio.run(body())


def _open_config(app: EaApp) -> ConfigModal:
    """Push a ConfigModal with a fixed repo anchor and clear tab-bar focus.

    The ``_isolated_cwd`` fixture chdir's into a fresh non-git temp dir, so
    the layered merge resolves to the built-in defaults — the rendered
    field values are therefore deterministic (the registry defaults), and
    no machine path or YAML-layer value leaks into the golden.
    """
    modal = ConfigModal(workspace=None, repo=Path("/repo"))
    app.push_screen(modal)
    return modal


def test_config_modal_audit_tab_snapshot() -> None:
    """Default tab (audit): a bool + a ranged int row, plus the layer line."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            _open_config(app)
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "config_modal_audit.txt")

    asyncio.run(body())


def test_config_modal_planning_tab_snapshot() -> None:
    """Planning tab exercises a choice + bool + int mix in one pane."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            modal = _open_config(app)
            await settle_screen(pilot)
            modal.query_one("#config-tabs").active = modal._tab_pane_id("planning")  # type: ignore[attr-defined]
            modal.set_focus(None)
            modal.field_index = 0
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "config_modal_planning.txt")

    asyncio.run(body())


def test_config_modal_runtime_single_field_snapshot() -> None:
    """The single-field ``runtime`` tab: its lone choice row carries the caret.

    The lone ``runtime.default`` choice carries the ``>`` cursor caret
    (field 0 of the tab), showing it is keyboard-reachable; ``←`` / ``→``
    leave this tab and ``Enter`` cycles the choice in place.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            modal = _open_config(app)
            await settle_screen(pilot)
            modal.query_one("#config-tabs").active = modal._tab_pane_id("runtime")  # type: ignore[attr-defined]
            modal.set_focus(None)
            modal.field_index = 0
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "config_modal_runtime.txt")

    asyncio.run(body())


def test_config_modal_preferences_tab_snapshot() -> None:
    """The ``preferences`` tab renders its three curated choice rows.

    ``solution_bias`` / ``scope_size`` / ``auto_choose`` surface as choice
    fields with their default values; field 0 carries the ``>`` cursor caret.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            modal = _open_config(app)
            await settle_screen(pilot)
            modal.query_one("#config-tabs").active = modal._tab_pane_id("preferences")  # type: ignore[attr-defined]
            modal.set_focus(None)
            modal.field_index = 0
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "config_modal_preferences.txt")

    asyncio.run(body())


def test_config_modal_research_tab_snapshot() -> None:
    """The ``research`` tab renders its curated choice / int / bool row mix."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            modal = _open_config(app)
            await settle_screen(pilot)
            modal.query_one("#config-tabs").active = modal._tab_pane_id("research")  # type: ignore[attr-defined]
            modal.set_focus(None)
            modal.field_index = 0
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "config_modal_research.txt")

    asyncio.run(body())


def test_config_modal_dirty_layer_snapshot() -> None:
    """After an ``Enter`` toggle, the layer line shows the unsaved-count + marker."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            _open_config(app)
            await settle_screen(pilot)
            await pilot.press("enter")  # toggle the first bool — stages a dirty edit
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "config_modal_dirty.txt")

    asyncio.run(body())


def test_config_modal_dirty_discard_prompt_snapshot() -> None:
    """Esc on a dirty modal raises the V15 discard confirm prompt."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            _open_config(app)
            await settle_screen(pilot)
            await pilot.press("enter")  # toggle the first bool — stages a dirty edit
            await settle_screen(pilot)
            await pilot.press("escape")
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "config_modal_discard_prompt.txt")

    asyncio.run(body())


def test_config_modal_inline_edit_int_snapshot() -> None:
    """``Enter`` on the int field mounts the inline editor in the row.

    The inline input replaces the static ``audit.flaky_retry_count`` row
    with a meta line (key + type + range) and the seeded buffer, and the
    footer hint flips to the commit / cancel form.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            modal = _open_config(app)
            await settle_screen(pilot)
            modal.field_index = 1  # audit.flaky_retry_count (int)
            await settle_screen(pilot)
            await pilot.press("enter")  # open the inline editor
            await settle_screen(pilot)
            assert modal._editing_key == "audit.flaky_retry_count"  # type: ignore[attr-defined]
            assert_screen_snapshot(app, _GOLDEN / "config_modal_inline_int.txt")

    asyncio.run(body())


def test_edit_field_modal_int_snapshot() -> None:
    """The scalar editor seeds the input + shows the type/range meta line."""

    async def body() -> None:
        entry = registry_lookup("audit.flaky_retry_count")
        assert entry is not None
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            app.push_screen(EditFieldModal(entry, 2))
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "edit_field_int.txt")

    asyncio.run(body())


def _event_row(timestamp: str, event_type: str, status: str, summary: str) -> EventRow:
    """Build an :class:`EventRow` through the real envelope-flattening path.

    Routing the raw (possibly mixed-format) *timestamp* through
    :func:`_row_from_envelope` exercises the UTC normalization the overlay
    applies, so the golden captures the normalized + aligned columns.
    """
    row = _row_from_envelope(
        {
            "id": "EV",
            "summary": summary,
            "payload": {"timestamp": timestamp, "event_type": event_type, "status": status},
        }
    )
    assert row is not None
    return row


def test_events_overlay_snapshot() -> None:
    """Events rows render one UTC timestamp format, columns aligned.

    The seeded rows deliberately mix the two on-disk ISO spellings
    (trailing ``Z`` and a ``+00:00`` offset, with fractional seconds); the
    golden must show them collapsed to a single ``...Z`` form with the
    following columns lined up.
    """

    async def body() -> None:
        rows = (
            _event_row("2026-05-10T12:49:10.250985Z", "wave close", "ok", "closed W01"),
            _event_row("2026-05-10T12:49:25.190872+00:00", "dispatch cost", "fail", "boom"),
            _event_row("2026-05-10T12:49:35.093906Z", "executor report", "ok", "report body"),
        )
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            app.push_modal(EventsModal(rows))
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "events_overlay.txt")

    asyncio.run(body())


def test_cross_repo_pr_overlay_snapshot() -> None:
    """The advisory ``/prs`` cross-repo PR view: grouped repos + a degraded one.

    The seeded groups are fixed (no live ``gh`` / registry read) so the
    golden is deterministic + scrub-safe: two healthy repos (ABC / DEF)
    with abstract PR rows over one repo whose fetch was UNAVAILABLE (GHI),
    so the golden captures the per-repo headers, the open-PR rows, and the
    honest ``(unavailable)`` degraded header in one frame. Repo codes +
    PR titles are abstract placeholders, never real project / PR data.
    """

    async def body() -> None:
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
            CrossRepoGroup(
                "DEF",
                "DEF repo",
                (PrRow(20, "add the widget", "carol", "OPEN", "https://example.test/20"),),
                PrFetchStatus.OK,
            ),
            CrossRepoGroup("GHI", "GHI repo", (), PrFetchStatus.UNAVAILABLE),
        )
        app = EaApp(scope="workspace", state_path=_WORKSPACE_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            app.push_modal(CrossRepoPrModal(groups))
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "cross_repo_pr_overlay.txt")

    asyncio.run(body())


def test_doctor_mode_snapshot() -> None:
    """The Doctor-mode health pane: mixed check statuses + a DRIFT block.

    The mode's live mount gathers env-dependent doctor checks (probe
    results, config merge, manifest sync), so to keep the golden
    deterministic + scrub-safe the screen is painted with a **fixed**
    :class:`DoctorHealth` -- the same fixed-input stance the events golden
    uses. The fixture covers the mixed-status render (OK / WARN / FAIL
    glyphs), the rolled-up degraded title, and the DRIFT count + kinds
    block so a layout regression on any of those is caught.
    """

    health = DoctorHealth(
        rows=[
            HealthRow("tools_available", "ok", "3 probes ok"),
            HealthRow("state_present", "ok", "state.json found"),
            HealthRow("config_resolves", "ok", "2 profile(s) enabled"),
            HealthRow("manifest_in_sync", "warn", "drift: AGENTS.md::core=hand-edited"),
            HealthRow(
                "git_state_drift", "warn", "2 drift(s); kinds: closed_no_pin, pinned_mismatch"
            ),
            HealthRow("recent_events", "ok", "12 recent event(s); 1 error(s) in window"),
        ],
        overall="warn",
        drift_count=2,
        drift_kinds=["closed_no_pin", "pinned_mismatch"],
        event_error_count=1,
    )

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press("5")  # -> doctor
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, DoctorModeScreen)
            # Repaint with the fixed health so the golden is deterministic.
            screen._paint_health(health)  # type: ignore[attr-defined]
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "doctor_mode.txt")

    asyncio.run(body())
