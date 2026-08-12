"""Golden snapshot for the wave-keyed Evidence-mode report rollup.

The Evidence mode (digit ``6``) renders the typed agent-report rollup as a
table keyed by the WAVE each report advanced: a ``report`` column carrying the
wave label, a ``role`` column carrying the producing agent role in its own
column, a ``verdict`` column carrying the ratified verdict sigil from the
extended ``status_sigil`` home (pass -> filled circle, fail -> multiplication
cross, blocked -> warn-tinted withheld mark), and an ``eu`` column carrying the
wave's bucket-derived effort-unit estimate.

Two paths are pinned:

* the populated rollup -- a seeded report store yields one row per report,
  showing the wave-keyed report identity, the verdict sigil, and the wave EU;
* the no-reports path -- a scope with no report store on disk renders the
  honest-empty sentinel (:data:`~eawf.surfaces.tui.modes.evidence.EMPTY_NOTICE`)
  framed calmly with a muted ring, fabricating no rows.

The screen mounts in isolation under :class:`~eawf.surfaces.tui.app.EaApp`; the
render seam reads the role-report stores off disk, so the snapshot is
deterministic and subprocess-free.

Regenerate the golden after an intentional layout change with::

    EAWF_SNAPSHOT_REGEN=1 uv run pytest tests/snapshots/tui/test_evidence_rollup.py -q
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import orjson
import pytest
from textual.widgets import DataTable, Static

from eawf.kernel.state.enums import (
    AgentReportVerdict,
    AgentSessionRole,
    Confidence,
    ProjectStatus,
    ScopeKind,
    WaveStatus,
)
from eawf.kernel.state.models import CurrentPointers, Project, State
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.agent_report import (
    AgentReportHeader,
    AgentReportPayload,
    ExecutorReportBody,
    ReviewerReportBody,
    store_kind_for_role,
)
from eawf.kernel.store.paths import store_path
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modes.evidence import (
    EMPTY_NOTICE,
    EvidenceModeScreen,
    EvidenceRow,
    frame_empty_notice,
    verdict_sigil,
)
from eawf.surfaces.tui.snapshot import (
    assert_screen_snapshot,
    capture_screen_text,
    normalize_snapshot,
    settle_screen,
)
from eawf.surfaces.tui.widgets.empty_state import brand_sigil_markup
from eawf.surfaces.tui.widgets.git_pane import GitFields
from eawf.surfaces.tui.widgets.sigils import Sigil, chrome, glyph, status_sigil, tint
from eawf.workflow.lifecycle.transitions import open_iter, open_phase, plan_wave
from tests.conftest import make_intent

_SIZE = (120, 40)
_NOW = datetime(2026, 6, 9, 12, 0, tzinfo=UTC)

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "states" / "valid"
#: A repo fixture with NO sibling ``store/`` -- the honest-empty common path.
_REPO_STATE = _FIXTURES / "03-phase-iter-wave-active.json"

assert _REPO_STATE.is_file(), f"missing snapshot fixture: {_REPO_STATE}"

_GOLDEN = Path(__file__).resolve().parent / "golden"


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point registry resolution at an empty ``tmp_path`` home."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


@pytest.fixture(autouse=True)
def _stub_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the workspace git probe to a deterministic clean tree."""
    monkeypatch.setattr(
        "eawf.surfaces.tui.widgets.workspace_table.gather_git_fields",
        lambda _path: GitFields(
            branch="main", dirty="clean", ahead_behind="up-to-date", recent_commits=()
        ),
    )


# --------------------------------------------------------------------------
# Fixture builders -- state + report-store seeding
# --------------------------------------------------------------------------


def _empty_state() -> State:
    """Build a minimal valid repo state with no phases/iters/waves."""
    return State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": _NOW.isoformat(),
            "project": Project(
                code="QR",
                slug="qr",
                title="QR",
                description=None,
                domains=["x"],
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


def _state_with_wave(
    wave_id: str = "P30-I02-W15",
    title: str = "Reskin Evidence",
    effort_bucket: str = "M",
) -> State:
    """Build a state carrying one CLOSED wave (with a bucket) for the join.

    Uses the lifecycle transition helpers so every phase/iter/wave model
    carries its required fields without a hand-built dict that drifts as the
    schema grows. The effort bucket drives the EU column.
    """
    state = _empty_state()
    open_phase(state, phase_id="P30", title="phase")
    open_iter(state, iter_id="P30-I02", phase_id="P30", title="iter")
    plan_wave(
        state,
        wave_id=wave_id,
        iter_id="P30-I02",
        title=title,
        file_scopes=["x"],
        effort_bucket=effort_bucket,
        intent=make_intent(),
    )
    wave = state.waves[wave_id]
    wave.status = WaveStatus.CLOSED
    wave.closed_at = _NOW
    return state


def _write_state(state: State, tmp_path: Path) -> Path:
    """Write *state* to ``tmp_path/.ea/state.json`` and return the path."""
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(orjson.dumps(state.model_dump(mode="json")))
    return state_path


def _write_executor_report(
    state_path: Path,
    *,
    base_id: str,
    verdict: AgentReportVerdict,
    attempt: int = 1,
) -> None:
    """Append one executor report envelope keyed by ``base_id`` to the store."""
    body = ExecutorReportBody(
        role="executor",
        verdict=verdict,
        confidence=Confidence.HIGH,
        summary="attempt completed",
        wave_id=base_id,
        outcome="done",
        followups=[],
    )
    report_id = f"AR-executor-{base_id}-{attempt:02d}"
    header = AgentReportHeader(
        report_id=report_id,
        role=AgentSessionRole.EXECUTOR,
        session_id=f"SES-{attempt}",
        scope_id=base_id,
        base_id=base_id,
        attempt=attempt,
        runtime="codex",
        generated_at=_NOW,
        summary="attempt completed",
    )
    _append_report(state_path, AgentSessionRole.EXECUTOR, report_id, base_id, body, header)


def _write_reviewer_report(
    state_path: Path,
    *,
    base_id: str,
    verdict: AgentReportVerdict,
    attempt: int = 1,
) -> None:
    """Append one reviewer report envelope keyed by ``base_id`` to the store."""
    body = ReviewerReportBody(
        role="reviewer",
        verdict=verdict,
        confidence=Confidence.HIGH,
        summary="review complete",
        target_id=base_id,
    )
    report_id = f"AR-reviewer-{base_id}-{attempt:02d}"
    header = AgentReportHeader(
        report_id=report_id,
        role=AgentSessionRole.REVIEWER,
        session_id=f"REV-{attempt}",
        scope_id=base_id,
        base_id=base_id,
        attempt=attempt,
        runtime="claude",
        generated_at=_NOW,
        summary="review complete",
    )
    _append_report(state_path, AgentSessionRole.REVIEWER, report_id, base_id, body, header)


def _append_report(
    state_path: Path,
    role: AgentSessionRole,
    report_id: str,
    base_id: str,
    body: ExecutorReportBody | ReviewerReportBody,
    header: AgentReportHeader,
) -> None:
    """Write one report envelope to the role store under *state_path*."""
    payload = AgentReportPayload(header=header, body=body)
    envelope = Envelope(
        id=report_id,
        kind=store_kind_for_role(role),
        scope_id=base_id,
        created_at=_NOW + timedelta(minutes=header.attempt),
        updated_at=None,
        summary=body.summary,
        payload=payload.model_dump(mode="json"),
    )
    path = store_path(state_path, store_kind_for_role(role))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(envelope.model_dump_json() + "\n")


# --------------------------------------------------------------------------
# Pure helpers -- no Textual mount
# --------------------------------------------------------------------------


def test_verdict_sigil_pass_wears_closed_filled_circle() -> None:
    """A clean pass wears the CLOSED lifecycle sigil tinted closed-green."""
    text = verdict_sigil("pass", mode="unicode")
    assert text.plain == glyph(Sigil.CLOSED, mode="unicode")
    assert text.style == tint(Sigil.CLOSED)


def test_verdict_sigil_pass_with_followups_wears_closed_plus_badge() -> None:
    """A pass-with-a-tail reads as a pass (CLOSED shape) trailing its badge."""
    text = verdict_sigil("pass-with-followups", mode="unicode")
    resolved = status_sigil(AgentReportVerdict.PASS_WITH_FOLLOWUPS)
    assert text.plain == resolved.render(mode="unicode")
    assert text.plain.startswith(glyph(Sigil.CLOSED, mode="unicode"))
    assert len(text.plain) > 1  # the follow-up badge trails the shape


def test_verdict_sigil_fail_wears_failed_cross() -> None:
    """A fail wears the FAILED multiplication cross tinted failed-red."""
    text = verdict_sigil("fail", mode="unicode")
    assert text.plain == glyph(Sigil.FAILED, mode="unicode")
    assert text.style == tint(Sigil.FAILED)


def test_verdict_sigil_blocked_wears_warn_withheld_mark() -> None:
    """A withheld blocked verdict wears the ratified warn-tinted withheld mark.

    Resolved through the extended :func:`status_sigil` home -- neither the
    pending ring (blocked is not not-yet-run) nor the fail cross (blocked is
    not a verdict on the work).
    """
    text = verdict_sigil("blocked", mode="unicode")
    resolved = status_sigil(AgentReportVerdict.BLOCKED)
    assert text.plain == resolved.render(mode="unicode")
    assert text.style == (resolved.tint_hex or "")
    assert text.plain != glyph(Sigil.PENDING, mode="unicode")
    assert text.plain != glyph(Sigil.FAILED, mode="unicode")


def test_verdict_sigil_unknown_falls_back_to_pending_ring() -> None:
    """A verdict outside the closed enum falls back to the ring, not raises."""
    text = verdict_sigil("not-a-real-verdict", mode="unicode")
    assert text.plain == glyph(Sigil.PENDING, mode="unicode")


def test_verdict_sigil_ascii_mode_uses_ascii_column() -> None:
    """The ASCII render mode selects the ASCII glyph column."""
    assert verdict_sigil("pass", mode="ascii").plain == glyph(Sigil.CLOSED, mode="ascii")


def test_frame_empty_notice_keeps_sentinel_and_leads_with_brand_sigil() -> None:
    """The framed notice keeps EMPTY_NOTICE verbatim, led by the brand sigil hero."""
    framed = frame_empty_notice(mode="unicode")
    assert EMPTY_NOTICE in framed
    # The honest-empty hero leads with the muted brand sigil (not the
    # pre-reskin PENDING lifecycle ring) so the rollup empty reads as the
    # same centered calm hero the research board + sandbox timeline render.
    assert framed.startswith(brand_sigil_markup(mode="unicode"))


def test_evidence_row_report_label_joins_wave_and_role() -> None:
    """The report column leads with the wave label and trails the role."""
    row = EvidenceRow(
        report_id="AR-executor-P30-I02-W15-01",
        role="executor",
        verdict="pass",
        wave_id="P30-I02-W15",
        wave_title="Reskin Evidence",
        attempt=1,
        summary="s",
        followups=(),
        eu=1.0,
    )
    assert row.report_label == "P30-I02-W15 Reskin Evidence :: executor"


def test_evidence_row_eu_label_formats_two_decimals() -> None:
    """A wave with a bucket renders its EU to two decimals."""
    row = EvidenceRow("r", "executor", "pass", "W1", None, 1, "s", (), eu=2.0)
    assert row.eu_label == "2.00"


def test_evidence_row_eu_label_zero_dashes() -> None:
    """A report joining no wave (zero EU) dashes the EU cell, not '0.00'."""
    row = EvidenceRow("r", "executor", "pass", "P30", None, 1, "s", (), eu=0.0)
    assert row.eu_label == "-"


# --------------------------------------------------------------------------
# Snapshot: the wave-keyed rollup table
# --------------------------------------------------------------------------


def test_evidence_rollup_snapshot(tmp_path: Path) -> None:
    """The Evidence-mode rollup renders a wave-keyed report/verdict/EU table.

    Mounts the evidence screen over a seeded report store and snapshots the
    frame so a layout regression on the wave-keyed table is caught. The
    verdict column renders tinted lifecycle sigils, the report column the
    wave-keyed identity, and the EU column the wave's bucket EU.
    """

    async def body() -> None:
        state = _state_with_wave(wave_id="P30-I02-W15", title="Reskin Evidence")
        state_path = _write_state(state, tmp_path)
        _write_executor_report(state_path, base_id="P30-I02-W15", verdict=AgentReportVerdict.PASS)
        _write_reviewer_report(
            state_path, base_id="P30-I02-W15", verdict=AgentReportVerdict.BLOCKED
        )
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press("6")  # -> evidence mode
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, EvidenceModeScreen)
            table = screen.query_one("#evidence-table", DataTable)
            assert table.display is True
            assert table.row_count == 2
            # The wave-keyed columns split the producing role into its own
            # column: report / role / verdict / eu (no fused ``:: role`` suffix).
            assert [str(key.value) for key in table.columns] == [
                "report",
                "role",
                "verdict",
                "eu",
            ]
            frame = normalize_snapshot(capture_screen_text(app))
            # Report column: the wave label (id + title); the role rides its own
            # column beside it (no fused ``:: role`` suffix).
            assert "P30-I02-W15" in frame
            assert "Reskin Evidence" in frame
            assert "executor" in frame
            assert "reviewer" in frame
            # Verdict column: the ratified sigils (closed pass, warn-tinted
            # withheld block) from the extended status_sigil home.
            assert glyph(Sigil.CLOSED, mode=app.render_mode) in frame
            blocked_mark = status_sigil(AgentReportVerdict.BLOCKED).render(mode=app.render_mode)
            assert blocked_mark in frame
            # EU column: the M bucket's EU.
            assert "1.00" in frame
            assert EMPTY_NOTICE not in frame
            assert_screen_snapshot(app, _GOLDEN / "evidence_rollup.txt")

    asyncio.run(body())


def test_evidence_rollup_no_reports_renders_honest_empty_sentinel() -> None:
    """The no-reports path renders the honest-empty sentinel, no rows.

    A scope with no report store on disk (the common path -- the live
    ``state.json`` is exactly this) renders the calmly-framed
    :data:`~eawf.surfaces.tui.modes.evidence.EMPTY_NOTICE`, fabricating no
    rows, and hides the table.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press("6")
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, EvidenceModeScreen)
            empty = screen.query_one("#evidence-empty", Static)
            table = screen.query_one("#evidence-table", DataTable)
            assert empty.display is True
            assert table.display is False
            assert table.row_count == 0
            frame = normalize_snapshot(capture_screen_text(app))
            assert EMPTY_NOTICE in frame
            # The no-reports rollup now renders the shared centered honest-empty
            # hero: a muted brand sigil over the headline (not the pre-reskin
            # top-left PENDING lifecycle ring).
            assert chrome("brand", mode=app.render_mode) in frame
            assert_screen_snapshot(app, _GOLDEN / "evidence_rollup_empty.txt")

    asyncio.run(body())
