"""Tests for the fleet verdict-rollup pane in the Watch mode (P30-I07-W09).

The Watch mode leads its body with a fleet verdict-rollup pane: one row per
wave that has an auditor verdict, each tinted by its outcome (the
:class:`~eawf.surfaces.tui.widgets.sigils.status_sigil` resolver paints a
``pass`` green, a ``fail`` red). A fleet with zero verdict rows renders the
honest-empty
:data:`~eawf.surfaces.tui.modes.agent_watch.ROLLUP_EMPTY_NOTICE` line rather
than a fabricated pass / rollup.

These tests pin the two halves:

* the pure render helpers --
  :func:`~eawf.surfaces.tui.modes.agent_watch.verdict_sigil_markup` and
  :func:`~eawf.surfaces.tui.modes.agent_watch.render_verdict_rollup_row` --
  tested against directly-built rows so the outcome tint is verified without
  mounting Textual; and
* the mounted :class:`~eawf.surfaces.tui.modes.agent_watch.VerdictRollupPane`
  and the full :class:`~eawf.surfaces.tui.modes.agent_watch.AgentWatchModeScreen`
  reading a seeded AUDITOR store off disk: two waves with one verdict each (one
  pass, one fail) render BOTH verdicts tinted by outcome; a store with zero
  verdict rows renders the honest-empty line.

Determinism follows the project Pilot-worker rule: each Pilot body drains
workers via :func:`~eawf.surfaces.tui.snapshot.settle_screen` before asserting.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.reactive import reactive
from textual.widgets import Static

from eawf.kernel.state.enums import (
    AgentReportVerdict,
    AgentSessionRole,
    Confidence,
    ProjectStatus,
    ReportSource,
    ScopeKind,
)
from eawf.kernel.state.models import (
    CurrentPointers,
    Project,
    State,
)
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.agent_report import (
    AgentReportHeader,
    AgentReportPayload,
    AuditorReportBody,
    report_record_id,
    store_kind_for_role,
)
from eawf.kernel.store.paths import store_path
from eawf.observability.eval.reputation import FleetVerdictRow
from eawf.surfaces.tui.modes.agent_watch import (
    ROLLUP_EMPTY_NOTICE,
    SYNTHESIZED_BADGE,
    WATCH_ROLLUP_EMPTY_ID,
    WATCH_ROLLUP_ID,
    WATCH_ROLLUP_ROW_CLASS,
    WATCH_SYNTHESIZED_REPORT_BANNER,
    AgentWatchModeScreen,
    VerdictRollupPane,
    frame_replay_lines,
    render_verdict_rollup_row,
    synthesized_marker_markup,
    verdict_sigil_markup,
)
from eawf.surfaces.tui.snapshot import settle_screen
from eawf.surfaces.tui.theme import EA_THEMES, LOGICAL_THEMES
from eawf.surfaces.tui.widgets.eu_bar import RenderMode
from eawf.surfaces.tui.widgets.sigils import chrome, status_sigil
from eawf.surfaces.tui.widgets.status_tint import BAND_HEX

_THEME = Path(__file__).resolve().parents[2] / "src" / "eawf" / "surfaces" / "tui" / "theme.tcss"
_T0 = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)

#: A wide terminal so the rollup rows lay out unwrapped.
_SIZE = (160, 40)

#: The two waves the seeded verdicts are about.
_WAVE_A = "P01-I01-W01"
_WAVE_B = "P01-I01-W02"

assert _THEME.is_file(), f"missing theme: {_THEME}"


def _row(
    wave_id: str,
    verdict: AgentReportVerdict,
    runtime: str = "claude",
    *,
    report_source: ReportSource = ReportSource.AUTHORED,
) -> FleetVerdictRow:
    """Build a directly-constructed fleet verdict row for the render helpers."""
    return FleetVerdictRow(
        wave_id=wave_id, verdict=verdict, runtime=runtime, report_source=report_source
    )


def _append_auditor_verdict(
    state_path: Path,
    *,
    base_id: str,
    attempt: int = 1,
    verdict: AgentReportVerdict,
    runtime: str = "claude",
    report_source: ReportSource = ReportSource.AUTHORED,
) -> None:
    """Append one AUDITOR verdict envelope at ``base_id`` to the on-disk store."""
    role = AgentSessionRole.AUDITOR
    report_id = report_record_id(role=role, base_id=base_id, attempt=attempt)
    moment = _T0 + timedelta(minutes=attempt)
    body = AuditorReportBody(
        verdict=verdict,
        confidence=Confidence.HIGH,
        summary="recorded auditor verdict",
        target_id=base_id,
        report_source=report_source,
    )
    header = AgentReportHeader(
        report_id=report_id,
        role=role,
        session_id=f"S{attempt:02d}",
        scope_id=f"{base_id}::audit",
        base_id=base_id,
        attempt=attempt,
        runtime=runtime,
        generated_at=moment,
        summary="recorded auditor verdict",
    )
    payload = AgentReportPayload(header=header, body=body)
    envelope = Envelope(
        id=report_id,
        kind=store_kind_for_role(role),
        scope_id=base_id,
        created_at=moment,
        updated_at=None,
        summary="recorded auditor verdict",
        payload=payload.model_dump(mode="json"),
    )
    append_envelope(store_path(state_path, store_kind_for_role(role)), envelope)


def _state() -> State:
    """Build a minimal repo state (the rollup reads the store, not the state)."""
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
            "waves": {},
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )


def _write_state(tmp_path: Path, state: State) -> Path:
    """Write *state* to ``<tmp>/.ea/state.json`` and return the path."""
    ea_dir = tmp_path / ".ea"
    ea_dir.mkdir(parents=True, exist_ok=True)
    state_path = ea_dir / "state.json"
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    return state_path


class _PaneHostApp(App[None]):
    """Bare themed host carrying just the render mode the rollup pane reads."""

    CSS_PATH = str(_THEME)
    render_mode: reactive[RenderMode] = reactive[RenderMode]("unicode")

    def __init__(self, *, pane: VerdictRollupPane) -> None:
        super().__init__()
        for theme in EA_THEMES:
            self.register_theme(theme)
        self.theme = LOGICAL_THEMES["dark"]
        self._pane = pane

    def compose(self) -> ComposeResult:
        yield self._pane


class _ScreenHostApp(App[None]):
    """Bare themed host carrying the read-only surface the Watch screen reads.

    The screen reads ``state`` (for the executor sessions) and ``_state_path``
    (for the AUDITOR report store the rollup rolls up) off ``self.app``. No
    daemon socket is exposed, so the screen never reaches a live daemon.
    """

    CSS_PATH = str(_THEME)
    render_mode: reactive[RenderMode] = reactive[RenderMode]("unicode")
    state: reactive[State | None] = reactive(None)

    def __init__(self, *, state: State | None, state_path: Path | None) -> None:
        super().__init__()
        for theme in EA_THEMES:
            self.register_theme(theme)
        self.theme = LOGICAL_THEMES["dark"]
        self.state = state
        self._state_path = state_path

    def compose(self) -> ComposeResult:
        yield from ()

    def on_mount(self) -> None:
        self.push_screen(AgentWatchModeScreen())

    def _daemon_socket_available(self) -> bool:
        """No daemon under the bare host."""
        return False


# --------------------------------------------------------------------------
# Pure render helpers -- outcome tint per verdict
# --------------------------------------------------------------------------


def test_verdict_sigil_markup_tints_pass_and_fail_differently() -> None:
    """A pass and a fail sigil carry visibly different outcome tints."""
    passed = verdict_sigil_markup(AgentReportVerdict.PASS, mode="unicode")
    failed = verdict_sigil_markup(AgentReportVerdict.FAIL, mode="unicode")
    pass_hex = status_sigil(AgentReportVerdict.PASS).tint_hex
    fail_hex = status_sigil(AgentReportVerdict.FAIL).tint_hex
    assert pass_hex is not None and fail_hex is not None
    assert pass_hex != fail_hex
    assert f"[{pass_hex}]" in passed
    assert f"[{fail_hex}]" in failed


def test_verdict_sigil_markup_renders_the_resolved_glyph() -> None:
    """The sigil markup carries the verdict's ratified glyph, not its raw word."""
    markup = verdict_sigil_markup(AgentReportVerdict.PASS, mode="unicode")
    assert status_sigil(AgentReportVerdict.PASS).render(mode="unicode") in markup
    assert AgentReportVerdict.PASS.value not in markup


def test_render_verdict_rollup_row_names_wave_verdict_runtime() -> None:
    """A rollup row names the wave, the verdict word, and the runtime."""
    row = _row(_WAVE_A, AgentReportVerdict.FAIL, "codex")
    rendered = render_verdict_rollup_row(row, mode="unicode")
    assert _WAVE_A in rendered
    assert AgentReportVerdict.FAIL.value in rendered
    assert "codex" in rendered
    # The verdict word is tinted by its outcome, not left bare.
    fail_hex = status_sigil(AgentReportVerdict.FAIL).tint_hex
    assert fail_hex is not None
    assert f"[{fail_hex}]" in rendered


# --------------------------------------------------------------------------
# Synthesized-report marker -- the T8 degrade badge (P30-I26-W07)
# --------------------------------------------------------------------------


def test_synthesized_marker_markup_authored_is_empty() -> None:
    """An authored report renders no synthesized badge -- an empty marker string."""
    assert synthesized_marker_markup(ReportSource.AUTHORED, mode="unicode") == ""


def test_synthesized_marker_markup_synthesized_warns() -> None:
    """A synthesized report renders the warn-tinted attention mark + synth label.

    The daemon minted this body after the agent's output failed validation, so
    the marker flags a degrade: the warn-band attention triangle plus the
    ``synth`` label, so the operator never reads it as an authored verdict.
    """
    marker = synthesized_marker_markup(ReportSource.SYNTHESIZED, mode="unicode")
    assert marker != ""
    assert SYNTHESIZED_BADGE in marker
    # The attention-triangle chrome mark carries the degrade shape.
    assert chrome("attention", mode="unicode") in marker
    # Painted in the warn band, not the closed / pass tint.
    assert f"[{BAND_HEX['warn']}]" in marker


def test_render_verdict_rollup_row_flags_synthesized_report() -> None:
    """Watch: a synthesized row wears the synth badge; an authored row does not.

    The falsifiable half of criterion 3: the Watch verdict-rollup row surfaces
    a synthesized report so an operator sees the report was not authored, and an
    authored row carries no such badge (no false degrade signal).
    """
    synth = _row(_WAVE_A, AgentReportVerdict.BLOCKED, report_source=ReportSource.SYNTHESIZED)
    authored = _row(_WAVE_B, AgentReportVerdict.PASS, report_source=ReportSource.AUTHORED)
    synth_rendered = render_verdict_rollup_row(synth, mode="unicode")
    authored_rendered = render_verdict_rollup_row(authored, mode="unicode")
    assert SYNTHESIZED_BADGE in synth_rendered
    assert chrome("attention", mode="unicode") in synth_rendered
    assert SYNTHESIZED_BADGE not in authored_rendered


def test_frame_replay_lines_prepends_synthesized_banner() -> None:
    """The report replay: a synthesized report prepends the degrade banner.

    The report-modal half of criterion 3: when the watched wave's latest report
    was synthesized, the output-tail replay is framed with the
    :data:`WATCH_SYNTHESIZED_REPORT_BANNER` so the agent's self-claimed pass in
    the replayed stdout never reads as the recorded outcome. An authored report
    (``report_source=None``) on a non-terminal wave leaves the lines unframed.
    """
    lines = ["the agent's replayed stdout", "PASS: wave done"]
    framed = frame_replay_lines(lines, None, report_source=ReportSource.SYNTHESIZED)
    assert framed[0] == WATCH_SYNTHESIZED_REPORT_BANNER
    assert framed[1:] == lines
    # An authored report on a non-terminal wave leaves the replay unframed.
    unframed = frame_replay_lines(lines, None, report_source=ReportSource.AUTHORED)
    assert unframed == lines
    # The default (unknown provenance) is also unframed on a non-terminal wave.
    assert frame_replay_lines(lines, None) == lines


# --------------------------------------------------------------------------
# Mounted pane -- two verdicts tinted, honest-empty
# --------------------------------------------------------------------------


def test_rollup_pane_renders_both_verdicts_tinted_by_outcome() -> None:
    """Two rows (one pass, one fail) render, each tinted by its own outcome.

    The success-criterion render half: a pass wave and a fail wave both paint a
    row, and the two rows carry visibly different outcome tints (green vs red).
    """
    rows = [_row(_WAVE_A, AgentReportVerdict.PASS), _row(_WAVE_B, AgentReportVerdict.FAIL)]
    pane = VerdictRollupPane(rows, mode="unicode")

    async def body() -> None:
        app = _PaneHostApp(pane=pane)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            rendered = [
                str(r.render()) for r in pane.query(f".{WATCH_ROLLUP_ROW_CLASS}").results(Static)
            ]
            assert len(rendered) == 2
            blob = "\n".join(rendered)
            # Both waves render with their verdict words.
            assert _WAVE_A in blob
            assert _WAVE_B in blob
            assert AgentReportVerdict.PASS.value in blob
            assert AgentReportVerdict.FAIL.value in blob
            # The honest-empty line is NOT painted when real verdicts exist.
            assert ROLLUP_EMPTY_NOTICE not in blob
            assert not pane.query(f"#{WATCH_ROLLUP_EMPTY_ID}")

    asyncio.run(body())


def test_rollup_pane_zero_rows_renders_honest_empty() -> None:
    """A fleet with zero verdict rows renders the honest-empty line, no rollup."""
    pane = VerdictRollupPane([], mode="unicode")

    async def body() -> None:
        app = _PaneHostApp(pane=pane)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            # No fabricated verdict rows.
            assert len(pane.query(f".{WATCH_ROLLUP_ROW_CLASS}")) == 0
            notice = pane.query_one(f"#{WATCH_ROLLUP_EMPTY_ID}", Static)
            rendered = str(notice.render())
            assert ROLLUP_EMPTY_NOTICE in rendered
            # No fabricated pass leaks into the empty line.
            assert AgentReportVerdict.PASS.value not in rendered

    asyncio.run(body())


# --------------------------------------------------------------------------
# Mounted screen -- rollup over a seeded report store
# --------------------------------------------------------------------------


def test_watch_screen_rollup_reads_seeded_verdicts(tmp_path: Path) -> None:
    """The mounted Watch screen rolls up the seeded AUDITOR store off disk.

    End-to-end through the live pane: two waves seeded with one verdict each
    (one pass, one fail) both render in the rollup, each tinted by its outcome.
    """
    state = _state()
    state_path = _write_state(tmp_path, state)
    _append_auditor_verdict(state_path, base_id=_WAVE_A, verdict=AgentReportVerdict.PASS)
    _append_auditor_verdict(state_path, base_id=_WAVE_B, verdict=AgentReportVerdict.FAIL)

    async def body() -> None:
        app = _ScreenHostApp(state=state, state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, AgentWatchModeScreen)
            pane = screen.query_one(f"#{WATCH_ROLLUP_ID}", VerdictRollupPane)
            rendered = [
                str(r.render()) for r in pane.query(f".{WATCH_ROLLUP_ROW_CLASS}").results(Static)
            ]
            assert len(rendered) == 2
            blob = "\n".join(rendered)
            assert _WAVE_A in blob and AgentReportVerdict.PASS.value in blob
            assert _WAVE_B in blob and AgentReportVerdict.FAIL.value in blob
            # Each row carries its verdict's outcome-distinct sigil glyph (the
            # green pass circle vs the red fail cross), so the two outcomes are
            # visibly distinguished; the colour tint itself is pinned by the
            # pure render-helper test (markup is consumed on mounted render).
            assert status_sigil(AgentReportVerdict.PASS).render(mode="unicode") in blob
            assert status_sigil(AgentReportVerdict.FAIL).render(mode="unicode") in blob
            assert ROLLUP_EMPTY_NOTICE not in blob

    asyncio.run(body())


def test_watch_screen_rollup_flags_synthesized_verdict(tmp_path: Path) -> None:
    """End-to-end: a synthesized verdict on disk surfaces the synth badge in Watch.

    The full Watch wiring for criterion 3: a seeded verdict whose body was
    synthesized (``report_source=synthesized``) flows store -> FleetVerdictRow ->
    ``render_verdict_rollup_row``, and the mounted rollup row shows the ``synth``
    degrade badge so the operator sees the report was not authored. The authored
    verdict on the sibling wave carries no such badge.
    """
    state = _state()
    state_path = _write_state(tmp_path, state)
    _append_auditor_verdict(
        state_path,
        base_id=_WAVE_A,
        verdict=AgentReportVerdict.BLOCKED,
        report_source=ReportSource.SYNTHESIZED,
    )
    _append_auditor_verdict(state_path, base_id=_WAVE_B, verdict=AgentReportVerdict.PASS)

    async def body() -> None:
        app = _ScreenHostApp(state=state, state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, AgentWatchModeScreen)
            pane = screen.query_one(f"#{WATCH_ROLLUP_ID}", VerdictRollupPane)
            rows = [
                str(r.render()) for r in pane.query(f".{WATCH_ROLLUP_ROW_CLASS}").results(Static)
            ]
            blob = "\n".join(rows)
            # The synthesized verdict surfaces the degrade badge in the row.
            assert SYNTHESIZED_BADGE in blob
            assert chrome("attention", mode="unicode") in blob

    asyncio.run(body())


def test_watch_screen_rollup_empty_store_renders_honest_empty(tmp_path: Path) -> None:
    """A Watch screen over an empty store renders the honest-empty rollup line.

    The load-bearing honesty assertion: zero verdict rows on disk means the
    rollup pane shows the honest-empty line, never a fabricated pass / rollup.
    """
    state = _state()
    state_path = _write_state(tmp_path, state)  # no auditor verdicts written

    async def body() -> None:
        app = _ScreenHostApp(state=state, state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, AgentWatchModeScreen)
            pane = screen.query_one(f"#{WATCH_ROLLUP_ID}", VerdictRollupPane)
            assert len(pane.query(f".{WATCH_ROLLUP_ROW_CLASS}")) == 0
            notice = pane.query_one(f"#{WATCH_ROLLUP_EMPTY_ID}", Static)
            rendered = str(notice.render())
            assert ROLLUP_EMPTY_NOTICE in rendered
            assert AgentReportVerdict.PASS.value not in rendered

    asyncio.run(body())


@pytest.fixture(autouse=True)
def _no_daemon(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Never resolve a real daemon socket / registry under the bare host."""
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
