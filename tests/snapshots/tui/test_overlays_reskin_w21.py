"""Golden snapshots for the four W21 overlay reskins.

Pins the migrated cosmic-terminal sigils + block progress each of the four
overlays now renders, captured from the modal mounted IN ISOLATION on a
bare themed host (mirroring the status-pane / bar-swap reskin suites) so
the frame is a pure function of the constructed payload with no off-disk
daemon read:

* ``plan_preview`` -- the wave-DAG action row marks the selected action
  with the shared dispatch chrome glyph (the cosmic-terminal frontier
  pointer) rather than the old hardcoded right-pointing triangle;
* ``reference`` -- the reference card reuses the detail-chassis look: the
  title carries the overview chrome glyph + the field rows are aligned
  ``label: value`` pairs;
* ``audit_running`` -- the per-check rows tick the running / closed /
  failed lifecycle sigils and a block-progress bar renders the
  reported-check share; and
* ``audit_failed`` -- the failing-check header (failed sigil + gate glyph
  + name) plus a clean ``evidence`` line surfaces above the repair menu.

The host pins the unicode render mode so the sigil + bar columns are
deterministic.

Regenerate the goldens after an intentional layout change with::

    EAWF_DAEMONLESS=1 EAWF_SNAPSHOT_REGEN=1 uv run pytest \
        tests/snapshots/tui/test_overlays_reskin_w21.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from textual.app import App
from textual.reactive import reactive
from textual.screen import ModalScreen

from eawf.surfaces.tui.screens.overlays.audit_failed import AuditFailedModal, FailingCheck
from eawf.surfaces.tui.screens.overlays.audit_running import (
    AuditProgress,
    AuditRunningModal,
    CheckRow,
    CheckState,
)
from eawf.surfaces.tui.screens.overlays.plan_preview import (
    DroppedClause,
    PlanIterRow,
    PlanPreviewModal,
    PlanTree,
    PlanWaveRow,
)
from eawf.surfaces.tui.screens.overlays.reference import ReferenceCard, ReferenceModal
from eawf.surfaces.tui.snapshot import assert_screen_snapshot, settle_screen
from eawf.surfaces.tui.theme import EA_THEMES, LOGICAL_THEMES
from eawf.surfaces.tui.widgets.eu_bar import RenderMode

_THEME = Path(__file__).resolve().parents[3] / "src" / "eawf" / "surfaces" / "tui" / "theme.tcss"
_GOLDEN = Path(__file__).resolve().parent / "golden"

#: A wide terminal so each overlay box lays out without wrapping, anchoring
#: every golden to the unwrapped box.
_SIZE = (120, 40)

assert _THEME.is_file(), f"missing theme: {_THEME}"


class _HostApp(App[None]):
    """Bare themed host carrying the unicode ``render_mode`` overlays read."""

    CSS_PATH = str(_THEME)
    render_mode: reactive[RenderMode] = reactive[RenderMode]("unicode")

    def __init__(self, modal: ModalScreen[object]) -> None:
        super().__init__()
        for theme in EA_THEMES:
            self.register_theme(theme)
        self.theme = LOGICAL_THEMES["dark"]
        self._modal = modal

    def on_mount(self) -> None:
        self.push_screen(self._modal)


_PLAN = PlanTree(
    phase_id="P30",
    title="binding pass",
    iters=(
        PlanIterRow(
            iter_id="P30-I02",
            title="tui reskin",
            waves=(
                PlanWaveRow(wave_id="P30-I02-W20", title="overlay chassis", deps=()),
                PlanWaveRow(
                    wave_id="P30-I02-W21",
                    title="overlay reskins",
                    deps=("P30-I02-W20",),
                ),
            ),
            eu=2.5,
        ),
    ),
    dropped_detail=(
        DroppedClause(
            span_id="U-014",
            reason="brief span dropped with no covering criterion and no deferral",
        ),
    ),
)

_REFERENCE_CARD = ReferenceCard(
    kind="wave",
    target="P30-I02-W21",
    title="wave P30-I02-W21",
    rows=(
        ("id", "P30-I02-W21"),
        ("title", "overlay reskins"),
        ("status", "in_progress"),
        ("role", "executor"),
    ),
)

_PROGRESS = AuditProgress(
    audit_id="A58-P30",
    scope_label="P30-I02-W21",
    checks=(
        CheckRow("file_exists", CheckState.PASS),
        CheckRow("pytest_pass", CheckState.RUNNING),
        CheckRow("coverage_min", CheckState.RUNNING),
    ),
)

_FAILING_CHECK = FailingCheck(name="pytest_pass", evidence="exit=1 (3 failed, 0 passed)")


def test_plan_preview_frontier_marker_snapshot() -> None:
    """The action row marks the selected action with the dispatch chrome glyph."""

    async def body() -> None:
        app = _HostApp(PlanPreviewModal(_PLAN))
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "overlay_plan_preview_frontier.txt")

    asyncio.run(body())


def test_reference_card_chassis_snapshot() -> None:
    """The reference card reuses the detail chassis look (overview glyph title)."""

    async def body() -> None:
        app = _HostApp(ReferenceModal(_REFERENCE_CARD))
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "overlay_reference_chassis.txt")

    asyncio.run(body())


def test_audit_running_sigils_and_bar_snapshot() -> None:
    """Per-check rows tick lifecycle sigils + a block-progress bar renders."""

    async def body() -> None:
        app = _HostApp(AuditRunningModal(_PROGRESS))
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "overlay_audit_running_sigils.txt")

    asyncio.run(body())


def test_audit_failed_check_evidence_snapshot() -> None:
    """The failing-check header + evidence line surfaces above the repair menu."""

    async def body() -> None:
        app = _HostApp(AuditFailedModal("P30-I02-W21", failing_check=_FAILING_CHECK))
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GOLDEN / "overlay_audit_failed_evidence.txt")

    asyncio.run(body())
