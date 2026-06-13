"""Tests for the FA1 fleet-arm / launch-flow overlay (P30-I13-W01).

The Autopilot ``a`` key opens the
:class:`~eawf.surfaces.tui.screens.overlays.arm.ArmModal` launch form -- a
config form for the five launch dimensions (scope / budget / concurrency / risk
policy / convergence). ``Enter`` submits a typed
:class:`~eawf.surfaces.tui.screens.overlays.arm.ArmSpec` the autopilot pane
folds into a ``fleet.drive`` RPC (flipping the cockpit to ``DRAINING``); ``Esc``
cancels (dismisses ``None``). Over a dry ready frontier the form refuses to arm
and surfaces the honest "nothing to drain" banner.

These tests pin the two halves:

* the pure helper :func:`~eawf.surfaces.tui.screens.overlays.arm.build_arm_spec`
  and the :class:`ArmSpec` model -- tested directly without mounting Textual; and
* the mounted overlay under a Pilot: the five config groups render, ``Enter``
  on a populated frontier submits a typed ``ArmSpec`` to ``fleet.drive`` and
  flips the cockpit to ``DRAINING``, ``Esc`` cancels without arming, and arming
  over an empty frontier shows the honest-empty banner and issues no RPC.

Determinism follows the project Pilot-worker rule: each Pilot body drains
workers via :func:`~eawf.surfaces.tui.snapshot.settle_screen` before asserting.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import ProjectStatus, ScopeKind, WaveStatus
from eawf.kernel.state.models import CurrentPointers, Project, State, Wave
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modes.autopilot import (
    DISPATCH_RESULT_ID,
    AutopilotModeScreen,
)
from eawf.surfaces.tui.screens.overlays.arm import (
    ARM_CANCELLED,
    ARM_DRAINING,
    CAPS_ROW_ID,
    CONCURRENCY_GROUP_ID,
    CONVERGENCE_GROUP_ID,
    HALT_ROW_ID,
    NOTHING_TO_DRAIN,
    RISK_GROUP_ID,
    RISK_ROW_1_ID,
    RISK_ROW_2_ID,
    SCOPE_GROUP_ID,
    ArmModal,
    ArmSpec,
    build_arm_spec,
    render_caps_row,
    render_group_row,
    render_halt_row,
    render_risk_matrix_rows,
)
from eawf.surfaces.tui.snapshot import settle_screen

_T0 = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
_AUTOPILOT_DIGIT = "2"


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point registry resolution at an empty ``tmp_path`` home."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def _wave(
    wave_id: str,
    *,
    status: WaveStatus,
    deps: list[str] | None = None,
    iter_id: str = "P01-I01",
) -> Wave:
    """Build a wave row for the frontier projection."""
    return Wave(
        id=wave_id,
        iter_id=iter_id,
        title=f"Wave {wave_id}",
        status=status,
        deps=deps or [],
        opened_at=_T0,
    )


def _state(*, waves: dict[str, Wave] | None = None) -> State:
    """Build a minimal repo state, optionally with a wave graph."""
    return State.model_validate(
        {
            "schema_version": "1.3",
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
            "phases": {},
            "iters": {},
            "waves": (
                {wid: w.model_dump(mode="json") for wid, w in waves.items()}
                if waves is not None
                else {}
            ),
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )


def _frontier_state() -> State:
    """Build a state whose ready frontier is exactly ``(W02,)``."""
    waves = {
        "P01-I01-W01": _wave("P01-I01-W01", status=WaveStatus.CLOSED),
        "P01-I01-W02": _wave("P01-I01-W02", status=WaveStatus.PENDING, deps=["P01-I01-W01"]),
    }
    return _state(waves=waves)


def _write_state(tmp_path: Path, state: State) -> Path:
    """Write *state* to ``<tmp>/.ea/state.json`` and return the path."""
    ea_dir = tmp_path / ".ea"
    ea_dir.mkdir(parents=True, exist_ok=True)
    state_path = ea_dir / "state.json"
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    return state_path


# --------------------------------------------------------------------------
# build_arm_spec + ArmSpec -- the pure derivation (boundary + error paths)
# --------------------------------------------------------------------------


def test_build_arm_spec_maps_concurrency_option_to_lane_int() -> None:
    """The concurrency option resolves to its integer lane width."""
    spec = build_arm_spec(
        scope="this iter",
        budget="standard",
        concurrency_option="4 lanes",
        risk_policy="auto-close, fork on fail",
        convergence_option="drain to empty",
    )
    assert spec.concurrency == 4


def test_build_arm_spec_drain_option_maps_to_drain_mode() -> None:
    """The ``drain to empty`` option maps to the ``drain`` convergence mode."""
    spec = build_arm_spec(
        scope="this iter",
        budget="standard",
        concurrency_option="1 lane",
        risk_policy="auto-close, fork on fail",
        convergence_option="drain to empty",
    )
    assert spec.convergence == "drain"


def test_build_arm_spec_kclean_option_maps_to_kclean_mode() -> None:
    """The ``K-clean rounds`` option maps to the ``kclean`` convergence mode."""
    spec = build_arm_spec(
        scope="this phase",
        budget="strict",
        concurrency_option="2 lanes",
        risk_policy="auto-close, fork on fail",
        convergence_option="K-clean rounds",
    )
    assert spec.convergence == "kclean"


def test_build_arm_spec_hard_halt_policy_sets_hard_halt_flag() -> None:
    """A risk policy naming ``hard-halt`` sets the hard-halt toggle."""
    spec = build_arm_spec(
        scope="cross-repo",
        budget="lenient",
        concurrency_option="8 lanes",
        risk_policy="auto-close, hard-halt on fail",
        convergence_option="drain to empty",
    )
    assert spec.hard_halt is True


def test_build_arm_spec_fork_policy_clears_hard_halt_flag() -> None:
    """A risk policy without ``hard-halt`` leaves the toggle off."""
    spec = build_arm_spec(
        scope="this iter",
        budget="standard",
        concurrency_option="1 lane",
        risk_policy="auto-close, fork on fail",
        convergence_option="drain to empty",
    )
    assert spec.hard_halt is False


def test_arm_spec_rejects_zero_concurrency() -> None:
    """``ArmSpec`` rejects a sub-1 lane width (the ``fleet.drive`` ge=1 floor)."""
    with pytest.raises(ValidationError):
        ArmSpec(
            scope="this iter",
            budget="standard",
            concurrency=0,
            risk_policy="auto-close, fork on fail",
            hard_halt=False,
            convergence="drain",
        )


def test_arm_spec_forbids_extra_fields() -> None:
    """``ArmSpec`` forbids unknown fields (strict config validation)."""
    with pytest.raises(ValidationError):
        ArmSpec.model_validate(
            {
                "scope": "this iter",
                "budget": "standard",
                "concurrency": 1,
                "risk_policy": "auto-close, fork on fail",
                "hard_halt": False,
                "convergence": "drain",
                "unknown": "x",
            }
        )


def test_render_group_row_focused_shows_caret_and_option() -> None:
    """A focused group row leads with the caret and names its selected option."""
    body = render_group_row("scope", "this iter", focused=True)
    assert "> scope" in body
    assert "this iter" in body


def test_render_group_row_unfocused_omits_caret() -> None:
    """An unfocused group row carries no caret marker."""
    body = render_group_row("budget", "strict", focused=False)
    assert "> budget" not in body
    assert "strict" in body


def test_render_caps_row_names_all_three_budget_axes() -> None:
    """The arm preview row names EU / USD / waves caps."""
    row = render_caps_row("standard")
    assert "EU" in row
    assert "$" in row
    assert "waves" in row
    assert "16" in row
    assert "32" in row
    assert "12" in row


def test_render_halt_row_distinguishes_drain_from_hard_halt() -> None:
    """The budget-stop row distinguishes graceful drain from hard halt."""
    drain = render_halt_row("auto-close, fork on fail")
    hard = render_halt_row("auto-close, hard-halt on fail")
    assert "drain in-flight lanes" in drain
    assert "hard-halt in-flight lanes" in hard


def test_render_risk_matrix_rows_are_two_rows() -> None:
    """The risk matrix renders exactly two rows naming clean and fail paths."""
    row_1, row_2 = render_risk_matrix_rows("auto-close, fork on fail")
    assert "risk matrix" in row_1
    assert "clean" in row_1
    assert "fail" in row_2


# --------------------------------------------------------------------------
# Mounted overlay -- open, five groups, Enter -> fleet.drive -> DRAINING
# --------------------------------------------------------------------------


def test_arm_binding_opens_overlay(tmp_path: Path) -> None:
    """Pressing ``a`` opens the ArmModal launch form over the ready frontier."""
    state_path = _write_state(tmp_path, _frontier_state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            await pilot.press("a")  # open the arm overlay
            await settle_screen(pilot)
            assert isinstance(app.screen, ArmModal)

    asyncio.run(body())


def test_arm_overlay_renders_five_config_groups(tmp_path: Path) -> None:
    """The arm overlay renders all five launch-form config groups.

    The load-bearing C1 half: the overlay must surface scope, budget,
    concurrency, risk policy, and convergence as selectable groups so the
    operator configures the whole launch before arming.
    """
    state_path = _write_state(tmp_path, _frontier_state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            await pilot.press("a")
            await settle_screen(pilot)
            modal = app.screen
            assert isinstance(modal, ArmModal)
            # All five config groups are mounted (one row each).
            for group_id in (
                SCOPE_GROUP_ID,
                "arm-budget",
                CONCURRENCY_GROUP_ID,
                RISK_GROUP_ID,
                CONVERGENCE_GROUP_ID,
            ):
                assert modal.query(f"#{group_id}")
            for row_id in (CAPS_ROW_ID, HALT_ROW_ID, RISK_ROW_1_ID, RISK_ROW_2_ID):
                assert modal.query(f"#{row_id}")
            assert "arm drain over 1 wave" in str(modal.query_one(".arm-title").render())  # type: ignore[attr-defined]
            caps = str(modal.query_one(f"#{CAPS_ROW_ID}").render())  # type: ignore[attr-defined]
            assert "EU" in caps and "$" in caps and "waves" in caps

    asyncio.run(body())


def test_arm_enter_submits_drive_and_flips_to_draining(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Enter`` submits a typed ArmSpec to ``fleet.drive`` and flips to DRAINING.

    The load-bearing C1 success criterion: with a reachable daemon stubbed by a
    fake client, arming the form reaches the daemon with a ``fleet.drive`` call
    carrying the ready frontier + the spec's concurrency + convergence, and the
    cockpit result line reads the DRAINING flip.
    """
    state_path = _write_state(tmp_path, _frontier_state())
    calls: list[tuple[str, dict[str, object]]] = []

    class _FakeClient:
        def __init__(self, *_a: object, **_k: object) -> None:
            return None

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
            calls.append((method, params))
            return {"run_state": "draining", "terminal_reason": None, "counters": {}}

    async def body() -> None:
        from eawf.surfaces.cli import _daemon_client as dc

        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: True)
        monkeypatch.setattr(dc, "DaemonClient", _FakeClient)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            await pilot.press("a")  # open the form
            await settle_screen(pilot)
            assert isinstance(app.screen, ArmModal)
            await pilot.press("enter")  # arm
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AutopilotModeScreen)  # overlay dismissed
            result = pane.query_one(f"#{DISPATCH_RESULT_ID}")
            assert ARM_DRAINING in str(result.render())  # type: ignore[attr-defined]

    asyncio.run(body())
    # The arm reached the daemon with fleet.drive carrying the ready frontier.
    assert calls and calls[0][0] == "fleet.drive"
    assert calls[0][1]["frontier"] == ["P01-I01-W02"]
    assert calls[0][1]["concurrency"] == 1
    assert calls[0][1]["convergence"] == "drain"


def test_arm_esc_cancels_without_arming(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Esc`` on the arm form cancels without firing a ``fleet.drive`` RPC.

    The C1 cancel half: backing out of the launch form must not arm -- the
    daemon is never reached and the result line reads the cancel honestly.
    """
    state_path = _write_state(tmp_path, _frontier_state())
    calls: list[tuple[str, dict[str, object]]] = []

    class _RecordingClient:
        def __init__(self, *_a: object, **_k: object) -> None:
            return None

        def __enter__(self) -> _RecordingClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
            calls.append((method, params))
            return {}

    async def body() -> None:
        from eawf.surfaces.cli import _daemon_client as dc

        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: True)
        monkeypatch.setattr(dc, "DaemonClient", _RecordingClient)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            await pilot.press("a")
            await settle_screen(pilot)
            assert isinstance(app.screen, ArmModal)
            await pilot.press("escape")  # cancel
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AutopilotModeScreen)
            result = pane.query_one(f"#{DISPATCH_RESULT_ID}")
            assert ARM_CANCELLED in str(result.render())  # type: ignore[attr-defined]

    asyncio.run(body())
    assert calls == []  # cancel never reached the daemon


def test_arm_cycle_changes_concurrency_carried_into_drive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cycling the concurrency group carries the new lane width into ``fleet.drive``.

    Proves the form's selections are load-bearing (not cosmetic): moving the
    cursor to the concurrency group, cycling it forward once (1 -> 2 lanes), then
    arming reaches the daemon with ``concurrency=2``.
    """
    state_path = _write_state(tmp_path, _frontier_state())
    calls: list[dict[str, object]] = []

    class _FakeClient:
        def __init__(self, *_a: object, **_k: object) -> None:
            return None

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def call(self, _method: str, params: dict[str, object]) -> dict[str, object]:
            calls.append(params)
            return {"run_state": "draining", "terminal_reason": None, "counters": {}}

    async def body() -> None:
        from eawf.surfaces.cli import _daemon_client as dc

        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: True)
        monkeypatch.setattr(dc, "DaemonClient", _FakeClient)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            await pilot.press("a")
            await settle_screen(pilot)
            modal = app.screen
            assert isinstance(modal, ArmModal)
            await pilot.press("down", "down")  # cursor -> concurrency group
            await pilot.press("right")  # cycle 1 lane -> 2 lanes
            await settle_screen(pilot)
            await pilot.press("enter")  # arm
            await settle_screen(pilot)

    asyncio.run(body())
    assert calls and calls[0]["concurrency"] == 2


def test_arm_empty_frontier_shows_honest_empty_and_issues_no_rpc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Arming over an empty frontier shows the honest-empty banner and arms nothing.

    The load-bearing C2 success criterion: an empty ready frontier renders the
    exact ``nothing to drain`` banner, and pressing ``Enter`` does NOT arm -- the
    daemon is never reached (arming an empty frontier would refuse at the daemon).
    """
    state_path = _write_state(tmp_path, _state())  # empty frontier
    calls: list[tuple[str, dict[str, object]]] = []

    class _ExplodingClient:
        def __init__(self, *_a: object, **_k: object) -> None:
            raise AssertionError("DaemonClient must not be constructed on a dry frontier")

        def __enter__(self) -> _ExplodingClient:  # pragma: no cover - never reached
            return self

        def __exit__(self, *_args: object) -> None:  # pragma: no cover - never reached
            return None

        def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
            calls.append((method, params))  # pragma: no cover - never reached
            return {}

    async def body() -> None:
        from eawf.surfaces.cli import _daemon_client as dc

        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: True)
        monkeypatch.setattr(dc, "DaemonClient", _ExplodingClient)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            await pilot.press("a")  # open over a dry frontier
            await settle_screen(pilot)
            modal = app.screen
            assert isinstance(modal, ArmModal)
            # The honest-empty banner renders the exact literal byte-for-byte.
            banner = str(modal.query_one("#arm-empty").render())  # type: ignore[attr-defined]
            assert NOTHING_TO_DRAIN in banner
            assert not modal.query(f"#{SCOPE_GROUP_ID}")  # close-only: no editable fields
            hint = str(modal.query_one("#arm-hint").render())  # type: ignore[attr-defined]
            assert "close" in hint
            assert "arm" not in hint
            await pilot.press("enter")  # arm refused -> no RPC
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AutopilotModeScreen)  # dismissed without arming

    asyncio.run(body())
    assert calls == []  # the dry-frontier arm never reached the daemon


def test_arm_nothing_to_drain_literal_uses_real_em_dash() -> None:
    """The honest-empty literal carries a real em-dash (the pinned C2 byte form)."""
    assert "—" in NOTHING_TO_DRAIN
    assert NOTHING_TO_DRAIN == "nothing to drain — all ready waves closed or blocked"
