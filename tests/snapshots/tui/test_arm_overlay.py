"""Golden snapshot for the FA1 fleet-arm / launch-flow overlay (P30-I13-W01).

The Autopilot ``a`` key opens the
:class:`~eawf.surfaces.tui.screens.overlays.arm.ArmModal` launch form -- a config
form for the five launch dimensions (scope / budget / concurrency / risk policy /
convergence). ``Enter`` folds the typed
:class:`~eawf.surfaces.tui.screens.overlays.arm.ArmSpec` into a ``fleet.drive``
RPC and flips the cockpit to ``DRAINING``; ``Esc`` cancels. Over a dry ready
frontier the form refuses to arm and surfaces the honest "nothing to drain"
banner.

These tests pin two goldens:

* the populated launch form (the five config groups, one row each); and
* the honest-empty form over a dry frontier -- the load-bearing C2 golden, which
  pins the ``nothing to drain — all ready waves closed or blocked`` banner
  byte-for-byte (a REAL em-dash) so a regression that drops or rewords the honest
  refusal is caught.

Regenerate the goldens after an intentional layout change with::

    EAWF_SNAPSHOT_REGEN=1 EAWF_DAEMONLESS=1 \
        uv run pytest tests/snapshots/tui/test_arm_overlay.py -q
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.state.enums import ProjectStatus, ScopeKind, WaveStatus
from eawf.kernel.state.models import CurrentPointers, Project, State, Wave
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.screens.overlays.arm import (
    NOTHING_TO_DRAIN,
    ArmModal,
)
from eawf.surfaces.tui.snapshot import (
    assert_screen_snapshot,
    capture_screen_text,
    normalize_snapshot,
    settle_screen,
)

_T0 = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
_SIZE = (120, 40)
_AUTOPILOT_DIGIT = "2"
_ARM_KEY = "a"
_GOLDEN = Path(__file__).resolve().parent / "golden"


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point registry resolution at an empty ``tmp_path`` home."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


def _wave(
    wave_id: str,
    *,
    status: WaveStatus,
    deps: list[str] | None = None,
) -> Wave:
    """Build a wave row for the frontier projection."""
    return Wave(
        id=wave_id,
        iter_id="P01-I01",
        title=f"Wave {wave_id}",
        status=status,
        deps=deps or [],
        opened_at=_T0,
    )


def _state(*, waves: dict[str, Wave]) -> State:
    """Build a minimal repo state carrying *waves*."""
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
            "waves": {wid: w.model_dump(mode="json") for wid, w in waves.items()},
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )


def _ready_state() -> State:
    """Build a state whose ready frontier is exactly ``(W02,)``."""
    return _state(
        waves={
            "P01-I01-W01": _wave("P01-I01-W01", status=WaveStatus.CLOSED),
            "P01-I01-W02": _wave("P01-I01-W02", status=WaveStatus.PENDING, deps=["P01-I01-W01"]),
        }
    )


def _drained_state() -> State:
    """Build a state with no claim-ready wave (a dry frontier).

    Every wave is CLOSED, so the ready frontier is empty -- arming refuses.
    """
    return _state(waves={"P01-I01-W01": _wave("P01-I01-W01", status=WaveStatus.CLOSED)})


def _write_state(tmp_path: Path, state: State) -> Path:
    """Write *state* to ``<tmp>/.ea/state.json`` and return the path."""
    ea_dir = tmp_path / ".ea"
    ea_dir.mkdir(parents=True, exist_ok=True)
    state_path = ea_dir / "state.json"
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    return state_path


def test_arm_overlay_populated_snapshot(tmp_path: Path) -> None:
    """The arm overlay renders the five-group launch form golden.

    Opens the launch form over a ready frontier and snapshots the frame so a
    layout regression on the config groups (scope / budget / concurrency / risk
    policy / convergence) is caught.
    """
    state_path = _write_state(tmp_path, _ready_state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)  # -> autopilot
            await settle_screen(pilot)
            await pilot.press(_ARM_KEY)  # open the arm overlay
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, ArmModal)
            frame = normalize_snapshot(capture_screen_text(app))
            # The five config-group captions are all present.
            assert "scope" in frame
            assert "budget" in frame
            assert "concurrency" in frame
            assert "risk policy" in frame
            assert "convergence" in frame
            # The dry-frontier banner is NOT shown over a ready frontier.
            assert NOTHING_TO_DRAIN not in frame
            assert_screen_snapshot(app, _GOLDEN / "arm_overlay_populated.txt")

    asyncio.run(body())


def test_arm_overlay_empty_frontier_snapshot(tmp_path: Path) -> None:
    """The arm overlay over a dry frontier renders the honest-empty golden.

    The load-bearing C2 golden: opening the launch form over an empty ready
    frontier surfaces the ``nothing to drain — all ready waves closed or
    blocked`` banner, pinned byte-for-byte (a real em-dash) so a regression that
    drops or rewords the honest refusal is caught.
    """
    state_path = _write_state(tmp_path, _drained_state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)  # -> autopilot
            await settle_screen(pilot)
            await pilot.press(_ARM_KEY)  # open over a dry frontier
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, ArmModal)
            frame = normalize_snapshot(capture_screen_text(app))
            # The honest-empty banner renders byte-for-byte (real em-dash).
            assert NOTHING_TO_DRAIN in frame
            assert "nothing to drain — all ready waves closed or blocked" in frame
            assert_screen_snapshot(app, _GOLDEN / "arm_overlay_empty.txt")

    asyncio.run(body())


@pytest.mark.parametrize("width", [40, 48])
def test_arm_overlay_populated_narrow_snapshot(tmp_path: Path, width: int) -> None:
    """The populated arm overlay stays coherent at 40/48 columns."""
    state_path = _write_state(tmp_path, _ready_state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(width, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            await pilot.press(_ARM_KEY)
            await settle_screen(pilot)
            assert isinstance(app.screen, ArmModal)
            assert_screen_snapshot(app, _GOLDEN / f"arm_overlay_populated_w{width}.txt")

    asyncio.run(body())
