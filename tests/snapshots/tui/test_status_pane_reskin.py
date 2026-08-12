"""Golden snapshots for the StatusPane cosmic-terminal reskin.

Pins the three reskin behaviours the wave delivers, each captured from the
StatusPane mounted IN ISOLATION under a bare themed host (mirroring the
bar-swap / workspace-chips suites) so the frame is a pure function of the
bound fixture state with no off-disk daemon read:

* the all-absent EFFORT collapse -- before any wave has run, the three
  present-vs-absent metrics (effort / variance / velocity) carry no data, so
  the EFFORT block renders the single dim ``effort . variance . velocity --
  awaiting first wave`` line instead of three stacked dashes; and
* the selective-absence expansion -- the moment ANY metric has data the block
  expands to its per-metric rows, where a selectively-absent metric (here
  variance, with a populated burn day but no estimate baseline) still shows
  its own ``— no data`` dash.

Both frames also exercise the leading live-row sigils: the waves / audits /
worktrees / gate rows each open with the running sigil drawn from the shared
sigils vocabulary, while the static project / phase / iter pointer rows do
not. The host pins the unicode render mode so the sigil + bar columns are
deterministic, and a narrow terminal keeps the pane single-column so the
golden anchors to the flat line set.

Regenerate the goldens after an intentional layout change with::

    EAWF_DAEMONLESS=1 EAWF_SNAPSHOT_REGEN=1 uv run pytest \
        tests/snapshots/tui/test_status_pane_reskin.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import orjson
from textual.app import App, ComposeResult
from textual.reactive import reactive

from eawf.kernel.state.models import State
from eawf.surfaces.tui.snapshot import assert_screen_snapshot, settle_screen
from eawf.surfaces.tui.theme import EA_THEMES, LOGICAL_THEMES
from eawf.surfaces.tui.widgets.eu_bar import RenderMode
from eawf.surfaces.tui.widgets.status_pane import StatusPane

_THEME = Path(__file__).resolve().parents[3] / "src" / "eawf" / "surfaces" / "tui" / "theme.tcss"
_GOLDEN = Path(__file__).resolve().parent / "golden"
_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"

#: A narrow terminal so the pane lays out single-column (its measured content
#: stays below ``TWO_COLUMN_THRESHOLD``), anchoring the golden to the flat
#: line set rather than the two-column composition.
_SIZE = (60, 24)

assert _PHASE_ITER_WAVE.is_file(), f"missing fixture: {_PHASE_ITER_WAVE}"


class _HostApp(App[None]):
    """Bare themed host carrying the unicode ``render_mode`` the pane reads."""

    CSS_PATH = str(_THEME)
    render_mode: reactive[RenderMode] = reactive[RenderMode]("unicode")

    def __init__(self, widget: object) -> None:
        super().__init__()
        for theme in EA_THEMES:
            self.register_theme(theme)
        self.theme = LOGICAL_THEMES["dark"]
        self._widget = widget

    def compose(self) -> ComposeResult:
        yield self._widget  # type: ignore[misc]


def _awaiting_state() -> State:
    """Return the active fixture as-is: a fresh phase with no measured burn.

    Fixture-03's lone wave carries no ``effort_bucket`` and no actual, so all
    three EFFORT metrics are absent and the block collapses to the dim
    awaiting-first-wave line.
    """
    return State.model_validate(orjson.loads(_PHASE_ITER_WAVE.read_bytes()))


def _selective_absence_state() -> State:
    """Return the fixture with an actual but no bucket (velocity present).

    A 1.2-EU actual gives velocity a populated burn day (present) while the
    missing ``effort_bucket`` leaves the estimate at 0 (so variance has no
    baseline -- absent). The presence of velocity expands the EFFORT block;
    variance still shows its own dash.
    """
    payload: dict[str, Any] = orjson.loads(_PHASE_ITER_WAVE.read_bytes())
    payload.setdefault("actuals", {})["ACT-A"] = {
        "id": "ACT-A",
        "scope_id": "P01-I01-W01",
        "status": "active",
        "elapsed_eu": 1.2,
        "attention_eu": None,
        "agent_runtime_eu": None,
        "current_store_record_id": "ACT-A-REC",
        "updated_at": "2026-05-08T09:00:00Z",
    }
    return State.model_validate(payload)


def test_status_pane_awaiting_collapse_snapshot() -> None:
    """The all-absent frame collapses EFFORT to one dim awaiting line + sigils."""

    async def body() -> None:
        pane = StatusPane(id="sp")
        app = _HostApp(pane)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            pane.state = _awaiting_state()
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "status_pane_awaiting.txt")

    asyncio.run(body())


def test_status_pane_selective_absence_snapshot() -> None:
    """A present velocity expands EFFORT; absent variance keeps its own dash."""

    async def body() -> None:
        pane = StatusPane(id="sp")
        app = _HostApp(pane)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            pane.state = _selective_absence_state()
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "status_pane_selective.txt")

    asyncio.run(body())
