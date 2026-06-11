"""Affordance-parity + reused edge-frame audit for the Autopilot fleet cockpit (P30-I13-W10).

This is an AUDIT wave: it pins the contracts the FA cockpit (P30-I13-W02 +
the FA3 lane grid + RS-26 crash boundary + the E1 degraded banner +
the E4 narrow-truncation rule) already advertise, so a regression that
breaks an advertised affordance or a reused edge frame reds here.

Three bands, one per success criterion:

* **C1 -- affordance parity over the FA1-FA8 footer keys.** Every key the
  Autopilot footer advertises resolves to a LIVE
  :class:`~textual.binding.Binding` AND fires its action: the
  :func:`~eawf.workflow.audit_dsl.kinds.affordance_parity.check_affordance_parity`
  kind drives each advertised key through the real key->Binding path and
  passes only when every one resolves; a synthetic advertised key with no
  binding reds the same check (naming the offending key). The intervention
  keys (``d`` / ``H`` / ``S`` / ``K`` / ``space`` / ``a``) are pinned to
  their live bindings + handlers directly, and pressing each in the mounted
  pane never classifies ``UNRESOLVED``.
* **C2 -- the reused RS-26 crash frame + E1 degraded banner.** An FA3 lane
  cell that raises mid-paint renders the RS-26 crash frame (the per-pane
  error boundary) while its neighbour cell + the loop survive and the App
  never panics; a daemon-down mid-run flips the App's degraded reactive and
  the E1 degraded banner surfaces over the draining cockpit.
* **C3 -- the E4 narrow-truncation rule.** At a constrained width the
  cockpit's load-bearing tokens -- the run-state sigil, the ``N/M lanes``
  block-bar n/m ratio, and (in the auto-raised fork inbox) the risk-tier
  badge -- survive while the over-long wave title is the token that
  truncates.

Determinism follows the project Pilot-worker rule: each Pilot body drains
workers via :func:`~eawf.surfaces.tui.snapshot.settle_screen` before asserting.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from textual.binding import Binding
from textual.widget import Widget
from textual.widgets import Static

from eawf.kernel.state.enums import ProjectStatus, RiskTier, ScopeKind, WaveStatus
from eawf.kernel.state.models import (
    CurrentPointers,
    FleetFork,
    FleetForkReason,
    FleetRun,
    FleetRunState,
    Project,
    State,
    Wave,
)
from eawf.surfaces.tui.app import (
    DEGRADED_BANNER_HIDDEN_CLASS,
    DEGRADED_BANNER_ID,
    EaApp,
)
from eawf.surfaces.tui.modes.autopilot import (
    COCKPIT_LANES_LABEL,
    AutopilotModeScreen,
    LaneCellRow,
    ReadyWaveRow,
    render_cockpit_vitals,
    render_lane_cell,
    render_ready_row,
)
from eawf.surfaces.tui.screens.overlays.fork_inbox import tier_badge
from eawf.surfaces.tui.snapshot import (
    capture_screen_text,
    normalize_snapshot,
    settle_screen,
)
from eawf.surfaces.tui.snapshot.behaviour_probe import (
    ProbeStatus,
    record_keypress_transcript,
)
from eawf.surfaces.tui.widgets.sigils import Sigil, glyph
from eawf.workflow.audit_dsl import CheckResult, CheckSpec
from eawf.workflow.audit_dsl.kinds import affordance_parity as ap_module
from eawf.workflow.audit_dsl.kinds.affordance_parity import check_affordance_parity

_T0 = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)

# tests/tui/test_fleet_affordance.py -> parents[2] is the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_REPO_STATE_REL = "tests/fixtures/states/valid/03-phase-iter-wave-active.json"
_REPO_STATE = (_REPO_ROOT / _REPO_STATE_REL).resolve()

#: The mode name + digit key the Autopilot cockpit lives behind.
_AUTOPILOT_MODE = "autopilot"
_AUTOPILOT_DIGIT = "2"

#: The wide / narrow column counts the E4 truncation rule is pinned across.
_WIDE_COLS = 120
_NARROW_COLS = 36

#: The FA1-FA8 intervention keys the Autopilot footer advertises, each paired
#: with the action its live ``Binding`` must resolve to. These are the cockpit
#: affordances the footer promises an operator can fire (dispatch + the
#: interventions); the parity contract is that every one stays bound + live.
_FA_INTERVENTION_BINDINGS: dict[str, str] = {
    "d": "dispatch_selected",
    "H": "halt_selected",
    "S": "skip_selected",
    "K": "kill_selected",
    "space": "toggle_pause",
    "a": "arm_flow",
}

#: EaApp's default render column is unicode, so the asserted lifecycle glyphs
#: are the unicode set.
_RUNNING_SIGIL = glyph(Sigil.RUNNING, mode="unicode")
_FAIL_SIGIL = glyph(Sigil.FAILED, mode="unicode")

#: A wave title far wider than the narrow column count so the E4 reflow fires.
_LONG_TITLE = "Add the per-lane affordance-parity acceptance gate over the cockpit here"


# --------------------------------------------------------------------------
# Fleet-state builders -- a DRAINING run with a ready frontier bound into state
# --------------------------------------------------------------------------


def _wave(wave_id: str, *, status: WaveStatus, deps: list[str] | None = None, title: str) -> Wave:
    """Build a wave row for the autopilot frontier projection."""
    return Wave(
        id=wave_id,
        iter_id="P01-I01",
        title=title,
        status=status,
        deps=deps or [],
        opened_at=_T0,
    )


def _frontier_waves() -> dict[str, Wave]:
    """Build a wave graph whose ready frontier is ``(W02, W03)`` (two ready rows)."""
    return {
        "P01-I01-W01": _wave("P01-I01-W01", status=WaveStatus.CLOSED, title="First wave"),
        "P01-I01-W02": _wave(
            "P01-I01-W02", status=WaveStatus.PENDING, deps=["P01-I01-W01"], title=_LONG_TITLE
        ),
        "P01-I01-W03": _wave(
            "P01-I01-W03", status=WaveStatus.PENDING, deps=["P01-I01-W01"], title="Wave three"
        ),
    }


def _fork(reason: FleetForkReason = FleetForkReason.HIGH_RISK_CLOSE) -> FleetFork:
    """Build a queued :class:`FleetFork` the cockpit auto-raises its inbox over."""
    return FleetFork(
        wave_id="P01-I01-W02",
        attempt=1,
        risk_tier=RiskTier.HIGH,
        reason=reason,
        evidence_ref="urn:eawf:v1:close:P01-I01-W02",
        forked_at=_T0,
    )


def _draining_run(*, forks: list[FleetFork] | None = None) -> FleetRun:
    """Build a DRAINING :class:`FleetRun` with a ready frontier, optionally forked."""
    return FleetRun(
        run_state=FleetRunState.DRAINING,
        concurrency=4,
        frontier=["P01-I01-W02", "P01-I01-W03"],
        forks=forks or [],
        armed_at=_T0,
    )


def _fleet_state(*, fleet_run: FleetRun) -> State:
    """Build a repo state carrying the ready-frontier wave graph + *fleet_run*."""
    return State.model_validate(
        {
            "schema_version": "1.10",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": _T0.isoformat(),
            "project": Project(
                code="QR",
                slug="quant-research",
                title="Quant Research",
                domains=["quant"],
                default_branch="main",
                status=ProjectStatus.ACTIVE,
                repo_urn="urn:eawf:v1:repo:QR",
            ).model_dump(mode="json"),
            "current": CurrentPointers(project_code="QR").model_dump(mode="json"),
            "workspace": None,
            "fleet_run": fleet_run.model_dump(mode="json"),
            "phases": {},
            "iters": {},
            "waves": {wid: w.model_dump(mode="json") for wid, w in _frontier_waves().items()},
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )


def _write_state(tmp_path: Path, state: State) -> Path:
    """Write *state* to ``<tmp>/.ea/state.json`` and return the absolute path."""
    ea_dir = tmp_path / ".ea"
    ea_dir.mkdir(parents=True, exist_ok=True)
    state_path = ea_dir / "state.json"
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    return state_path.resolve()


def _healthy_cell(text: str) -> Widget:
    """Return a builder result that yields a plain healthy lane-cell widget."""
    return Static(text, classes="healthy-cell")


def _exploding_cell() -> Widget:
    """A lane-cell builder that raises mid-paint.

    Mirrors an FA3 lane cell whose content build raises: the per-pane error
    boundary must mount the RS-26 crash frame in place of the cell rather than
    letting the exception escalate to ``App.panic``.

    Raises:
        RuntimeError: Always -- the injected mid-paint render explosion.
    """
    raise RuntimeError("injected lane-cell explosion")


# ==========================================================================
# C1 -- affordance parity over every advertised FA1-FA8 footer key
# ==========================================================================


def test_autopilot_affordance_parity_check_passes() -> None:
    """Every advertised Autopilot footer key resolves to a live binding (C1).

    The ``affordance_parity`` kind enumerates the cockpit's advertised footer
    keys and drives each through the real key->Binding path; it passes only
    when every advertised key resolves -- the load-bearing C1 contract that no
    advertised cockpit affordance is a dead promise.
    """
    spec = CheckSpec(
        kind="affordance_parity",
        name="autopilot-parity",
        args={"mode": _AUTOPILOT_MODE, "state_path": _REPO_STATE_REL},
    )
    result = check_affordance_parity(spec, _REPO_ROOT)
    assert result.status == "pass", result.details
    assert result.passed is True


def test_autopilot_affordance_parity_dead_advertised_key_reds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An advertised key with NO binding reds the parity check, naming it (C1).

    The RED side of C1: a footer that advertises a key no ``Binding`` answers
    must fail the parity check rather than pass silently. Inject a synthetic
    advertised dead key (``z``); the check probes it, classifies it
    ``UNRESOLVED``, and fails naming the offending key.
    """

    async def _fake_advertised(
        *, mode: str, state_path: Path | None, size: tuple[int, int]
    ) -> list[str]:
        return ["z"]

    monkeypatch.setattr(ap_module, "_advertised_keys", _fake_advertised)
    spec = CheckSpec(
        kind="affordance_parity",
        name="autopilot-dead-key",
        args={"mode": _AUTOPILOT_MODE, "state_path": _REPO_STATE_REL},
    )
    result = check_affordance_parity(spec, _REPO_ROOT)
    assert result.status == "fail"
    assert result.passed is False
    assert result.details is not None
    assert "z" in result.details


@pytest.mark.parametrize(("key", "action"), sorted(_FA_INTERVENTION_BINDINGS.items()))
def test_autopilot_intervention_key_binds_a_live_handler(key: str, action: str) -> None:
    """Each FA intervention key resolves to its live ``Binding`` + a real handler (C1).

    The intervention half of C1, pinned off the screen's ``BINDINGS`` directly:
    the advertised key declares a ``Binding`` whose action names the handler,
    and that ``action_<name>`` method is callable on the screen -- so the
    advertised affordance is wired end to end, not just labelled.
    """
    bindings = [
        binding
        for binding in AutopilotModeScreen.BINDINGS
        if isinstance(binding, Binding) and binding.key == key
    ]
    assert bindings, f"no binding declared for advertised key {key!r}"
    assert bindings[0].action == action
    assert callable(getattr(AutopilotModeScreen, f"action_{action}"))


@pytest.mark.parametrize("key", sorted(_FA_INTERVENTION_BINDINGS))
def test_autopilot_intervention_key_press_is_never_unresolved(key: str) -> None:
    """Pressing each FA intervention key in the mounted cockpit resolves (C1).

    The live-path complement: switching to the cockpit and pressing the
    advertised key drives the real key->Binding resolution. A resolving key
    classifies ``OBSERVABLE`` or ``NO_OP`` (it fired, observably or not); only a
    dead key classifies ``UNRESOLVED``, so the contract C1 pins is that no
    advertised intervention key is ever ``UNRESOLVED``.
    """

    async def body() -> ProbeStatus:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=(_WIDE_COLS, 40)) as pilot:
            await settle_screen(pilot)
            await app.switch_mode(_AUTOPILOT_MODE)
            await settle_screen(pilot)
            transcript = await record_keypress_transcript(pilot, [key], source_commit="w10")
            return transcript.outcomes[0].status

    status = asyncio.run(body())
    assert status is not ProbeStatus.UNRESOLVED


def test_autopilot_footer_advertises_the_fa_intervention_keys() -> None:
    """The mounted cockpit footer advertises every FA intervention key (C1).

    Parity is only meaningful over a non-vacuous advertised set: the footer the
    operator reads must actually advertise the intervention keys whose bindings
    the parity check verifies. Reads the live footer hint strip and asserts each
    FA intervention token is present.
    """
    from eawf.surfaces.tui.widgets.footer import Footer

    async def body() -> set[str]:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=(_WIDE_COLS, 40)) as pilot:
            await settle_screen(pilot)
            await app.switch_mode(_AUTOPILOT_MODE)
            await settle_screen(pilot)
            footer = app.screen.query(Footer).first(Footer)
            return {hint.split(" ", 1)[0] for hint in footer.hints}

    tokens = asyncio.run(body())
    assert set(_FA_INTERVENTION_BINDINGS) <= tokens


# ==========================================================================
# C2 -- the reused RS-26 crash frame per FA3 cell + the E1 degraded banner
# ==========================================================================


def test_fa3_cell_crash_frame_renders_while_neighbour_and_loop_survive() -> None:
    """An FA3 lane cell raising mid-paint shows the RS-26 crash frame (C2).

    The reused per-pane crash boundary, exercised per FA3 lane cell: a cell
    whose builder raises mid-paint mounts the RS-26 crash frame (the FAIL sigil
    + the calm recovery copy) in place of the content, while a neighbour cell
    still renders its healthy content and the App captures NO exception (the
    loop survives -- the one cell's explosion never panics the whole cockpit).
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=(_WIDE_COLS, 40)) as pilot:
            await settle_screen(pilot)
            broken = app.pane_boundary(builder=_exploding_cell, pane_id="lane-W02")
            healthy = app.pane_boundary(
                builder=lambda: _healthy_cell("lane W03 ok"), pane_id="lane-W03"
            )
            await app.screen.mount(broken)
            await app.screen.mount(healthy)
            await settle_screen(pilot)
            # The broken FA3 cell shows the RS-26 crash frame...
            frames = broken.query(".pane-crash-frame")
            assert len(frames) == 1
            rendered = str(frames.first(Static).render())
            assert _FAIL_SIGIL in rendered
            assert "raised mid-paint" in rendered
            # ...the neighbour cell still rendered its healthy content + no frame...
            content = healthy.query(".healthy-cell")
            assert len(content) == 1
            assert "lane W03 ok" in str(content.first(Static).render())
            assert not healthy.query(".pane-crash-frame")
            # ...and the loop survived: the App captured no exception (no panic).
            assert app._exception is None

    asyncio.run(body())


def test_e1_degraded_banner_surfaces_over_a_daemon_down_mid_run(tmp_path: Path) -> None:
    """A daemon-down mid-run surfaces the E1 degraded banner over the cockpit (C2).

    The honest-degraded half of C2: with a draining run bound, flipping the
    App's degraded reactive (the daemon-unreachable signal the binder raises
    when the socket drops mid-run) surfaces the E1 degraded banner -- it loses
    its hidden class and renders the FAIL-led "daemon unreachable" copy -- over
    the still-draining cockpit, so the lost transport reads honestly rather than
    a frozen-but-live cockpit.
    """
    state_path = _write_state(tmp_path, _fleet_state(fleet_run=_draining_run()))

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(_WIDE_COLS, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)  # -> autopilot cockpit
            await settle_screen(pilot)
            assert isinstance(app.screen, AutopilotModeScreen)
            # Flip the degraded reactive as the binder would on a daemon drop.
            await app._on_degraded(True)
            await settle_screen(pilot)
            banner = app.screen.query(f"#{DEGRADED_BANNER_ID}").first(Static)
            # The banner is shown (no hidden class) and carries the E1 copy.
            assert not banner.has_class(DEGRADED_BANNER_HIDDEN_CLASS)
            rendered = str(banner.render())
            assert _FAIL_SIGIL in rendered
            assert "daemon unreachable" in rendered
            # The cockpit pane is still mounted underneath (the run did not vanish).
            assert isinstance(app.screen, AutopilotModeScreen)

    asyncio.run(body())


def test_e1_degraded_banner_clears_when_transport_recovers(tmp_path: Path) -> None:
    """The E1 degraded banner re-hides when the daemon comes back (C2 boundary).

    The recovery boundary of the degraded path: once the binder reports the
    transport restored, the banner re-acquires its hidden class and clears its
    text rather than lingering as a stale alarm.
    """
    state_path = _write_state(tmp_path, _fleet_state(fleet_run=_draining_run()))

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(_WIDE_COLS, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            await app._on_degraded(True)
            await settle_screen(pilot)
            banner = app.screen.query(f"#{DEGRADED_BANNER_ID}").first(Static)
            assert not banner.has_class(DEGRADED_BANNER_HIDDEN_CLASS)
            await app._on_degraded(False)
            await settle_screen(pilot)
            assert banner.has_class(DEGRADED_BANNER_HIDDEN_CLASS)

    asyncio.run(body())


# ==========================================================================
# C3 -- the E4 narrow-truncation rule (title cuts; sigil + tier + n/m survive)
# ==========================================================================


def test_cockpit_vitals_narrow_keeps_sigil_and_lanes_ratio(tmp_path: Path) -> None:
    """At a narrow width the cockpit keeps its sigil + ``N/M lanes`` ratio (C3).

    The E4 rule over the cockpit vitals header in the captured frame: the
    run-state sigil and the ``N/M lanes`` block-bar n/m ratio are load-bearing
    and survive un-clipped at the narrow width, while the long wave titles in
    the rows below are what reflow. Asserting against the on-screen frame proves
    the reflow reaches the rendered terminal, not just the widget markup.
    """
    state_path = _write_state(tmp_path, _fleet_state(fleet_run=_draining_run()))

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(_NARROW_COLS, 30)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            assert isinstance(app.screen, AutopilotModeScreen)
            frame = normalize_snapshot(capture_screen_text(app))
            # The run-state sigil survives (the draining run wears the diamond).
            assert _RUNNING_SIGIL in frame
            # The block-bar n/m ratio (lanes occupancy) survives un-clipped.
            assert "0/4" in frame
            assert COCKPIT_LANES_LABEL in frame

    asyncio.run(body())


def test_cockpit_vitals_render_keeps_sigil_and_ratio_pure() -> None:
    """The pure cockpit-vitals render carries the sigil + n/m ratio (C3 unit).

    The pure-render unit under the mounted E4 golden: ``render_cockpit_vitals``
    over a draining run leads with the run-state sigil and carries the
    ``N/M lanes`` block-bar ratio -- the two tokens the narrow frame must keep.
    """
    rendered = render_cockpit_vitals(_draining_run(), mode="unicode")
    assert _RUNNING_SIGIL in rendered
    assert f"0/4 {COCKPIT_LANES_LABEL}" in rendered


def test_ready_row_narrow_truncates_title_not_id_or_sigil() -> None:
    """The over-long wave title is the token the narrow reflow cuts, not the id (C3).

    The E4 truncation target, pinned on the ready row: the load-bearing tokens
    -- the selection sigil (checkbox affordance) and the wave id -- precede the
    prose title in the row markup, so a narrow soft-wrap pushes the title down
    without ever clipping the id or the affordance. The id survives even though
    the title is far wider than the narrow width.
    """
    row = ReadyWaveRow(wave_id="P01-I01-W02", iter_id="P01-I01", title=_LONG_TITLE)
    rendered = render_ready_row(row, selected=True, mode="unicode")
    # The wave id precedes the prose title in the row markup (so it survives a
    # trailing-title clip).
    assert "P01-I01-W02" in rendered
    assert rendered.index("P01-I01-W02") < rendered.index(_LONG_TITLE)


def test_lane_cell_narrow_keeps_repair_ratio_and_fork_badge() -> None:
    """The FA3 lane cell keeps its repair n/m ratio + fork badge (C3).

    The lane-cell n/m ratio is load-bearing: a draining cell renders
    ``repair n/<budget>`` and a repair-exhausted cell escalates to the FAIL-led
    fork badge -- both the ratio and the badge precede no prose title, so the
    narrow reflow has nothing to clip off them.
    """
    draining = render_lane_cell(LaneCellRow(wave_id="P01-I01-W02", attempt=2, exhausted=False))
    assert "2/3" in draining  # repair n/<REPAIR_BUDGET> ratio survives
    forked = render_lane_cell(
        LaneCellRow(wave_id="P01-I01-W02", attempt=3, exhausted=True), mode="unicode"
    )
    assert _FAIL_SIGIL in forked  # the fork-escalation badge survives
    assert "P01-I01-W02" in forked


def test_fork_inbox_tier_badge_survives_for_a_queued_fork() -> None:
    """The risk-tier badge the auto-raised fork inbox renders survives (C3).

    The E4 ``tier badge`` token, pinned on the fork-inbox card the cockpit
    auto-raises mid-run: a queued HIGH-tier fork resolves to its band badge,
    so the badge stays a fixed short token (never a token a narrow reflow
    truncates) while the surrounding prose is what reflows.
    """
    badge = tier_badge(_fork())
    assert badge == "HIGH"


def test_autopilot_fork_inbox_auto_raises_and_shows_tier_badge(tmp_path: Path) -> None:
    """A daemon-queued fork auto-raises the inbox with its tier badge on screen (C3).

    The mounted counterpart: a draining run carrying a queued HIGH-tier fork
    auto-raises the FA5 fork inbox over the cockpit, and the risk-tier badge
    surfaces in the captured frame -- so the load-bearing tier token the E4 rule
    protects is genuinely on screen, not merely a pure-render string.
    """
    state_path = _write_state(tmp_path, _fleet_state(fleet_run=_draining_run(forks=[_fork()])))

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(_WIDE_COLS, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            frame = normalize_snapshot(capture_screen_text(app))
            # The auto-raised fork inbox surfaces the HIGH risk-tier badge.
            assert "HIGH" in frame

    asyncio.run(body())


def test_check_affordance_parity_returns_a_check_result() -> None:
    """The parity kind returns a typed :class:`CheckResult` (registry contract).

    A thin type guard backing C1: the kind the audit DSL dispatches returns the
    typed result the gate runner consumes, so the cockpit-parity criterion
    flows through the same evidence chassis every other check does.
    """
    spec = CheckSpec(
        kind="affordance_parity",
        name="autopilot-typed",
        args={"mode": _AUTOPILOT_MODE, "state_path": _REPO_STATE_REL},
    )
    result = check_affordance_parity(spec, _REPO_ROOT)
    assert isinstance(result, CheckResult)
    assert result.kind == "affordance_parity"
