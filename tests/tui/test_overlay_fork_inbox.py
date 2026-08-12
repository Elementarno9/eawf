"""Tests for the FA5 blocking-fork interrupt inbox overlay.

When the daemon-owned fleet auto-drain loop pauses a lane to a blocking fork (a
high-risk close, an uncalibrated-jury advisory, or a needs-user split), the
Autopilot pane auto-raises the
:class:`~eawf.surfaces.tui.screens.overlays.fork_inbox.ForkInboxModal` over the
cockpit -- a per-fork decision card that NAMES the forked wave, its
:class:`~eawf.kernel.state.enums.RiskTier` band badge, WHY it forked, the
evidence backing it, and the four resolution option keys. Each option key routes
the operator's choice to the ``fleet.resolve_fork`` RPC.

These tests pin the two halves:

* the pure render helpers (:func:`reason_headline`, :func:`tier_badge`,
  :func:`evidence_line`, :func:`render_options_row`, :func:`issue_resolve`) --
  tested directly against built :class:`FleetFork` rows so the figures are
  verified WITHOUT mounting Textual; and
* the mounted overlay under a Pilot: the card renders the wave / tier / reason /
  evidence + the four option keys, a pressed option key routes
  ``fleet.resolve_fork`` with that resolution, the honest-empty literal renders
  on an empty queue (pinned in a golden), no-daemon resolve keeps the card
  visible, and a multi-fork queue renders a count and cycles + drains without
  losing one.

Determinism follows the project Pilot-worker rule: each Pilot body drains workers
via :func:`~eawf.surfaces.tui.snapshot.settle_screen` before asserting.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.state.enums import ProjectStatus, RiskTier, ScopeKind
from eawf.kernel.state.models import (
    CurrentPointers,
    FleetFork,
    FleetForkReason,
    FleetForkResolution,
    FleetRun,
    FleetRunState,
    Project,
    State,
)
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.screens.overlays.fork_inbox import (
    EVIDENCE_NONE,
    FORK_INBOX_EMPTY,
    FORK_INBOX_EVIDENCE_ID,
    FORK_INBOX_HEADER_ID,
    FORK_INBOX_OPTIONS_ID,
    FORK_INBOX_REASON_ID,
    FORK_INBOX_TITLE,
    FORK_INBOX_WAVE_ID,
    RESOLVE_NO_DAEMON,
    ForkInboxModal,
    evidence_line,
    issue_resolve,
    reason_headline,
    render_options_row,
    tier_badge,
)
from eawf.surfaces.tui.snapshot import assert_screen_snapshot, settle_screen, toast_messages

_T0 = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)

#: Golden home for the isolated fork-inbox snapshot this wave adds -- a fresh dir
#: local to this test, regenerated via ``EAWF_SNAPSHOT_REGEN=1``.
_GOLDEN = Path(__file__).resolve().parent / "golden" / "fork_inbox_w05"


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point registry resolution at an empty ``tmp_path`` home + settle animations."""
    import textual.constants as _tc

    monkeypatch.setattr(_tc, "TEXTUAL_ANIMATIONS", "none")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def _fork(
    *,
    wave_id: str = "P30-I13-W05",
    attempt: int = 1,
    risk_tier: RiskTier = RiskTier.UI,
    reason: FleetForkReason = FleetForkReason.HIGH_RISK_CLOSE,
    evidence_ref: str | None = "urn:eawf:v1:close:P30-I13-W05",
) -> FleetFork:
    """Build a queued :class:`FleetFork` row carrying the figures the card reads."""
    return FleetFork(
        wave_id=wave_id,
        attempt=attempt,
        risk_tier=risk_tier,
        reason=reason,
        evidence_ref=evidence_ref,
        forked_at=_T0,
    )


def _run(*, forks: list[FleetFork] | None = None) -> FleetRun:
    """Build a DRAINING :class:`FleetRun` carrying *forks* on its queue."""
    return FleetRun(
        run_state=FleetRunState.DRAINING,
        concurrency=4,
        frontier=["P30-I13-W08"],
        forks=forks or [],
        armed_at=_T0,
    )


def _state(*, fleet_run: FleetRun | None = None) -> State:
    """Build a minimal repo state, optionally carrying a fleet run with forks."""
    return State.model_validate(
        {
            "schema_version": "1.10" if fleet_run is not None else "1.3",
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
            "fleet_run": fleet_run.model_dump(mode="json") if fleet_run is not None else None,
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


class _RecordingClient:
    """Fake :class:`DaemonClient` that records its calls + returns a canned dict.

    Records every ``(method, params)`` pair so a test can assert the wire shape,
    and returns a run-state so the result line reads the daemon's verdict.
    """

    calls: list[tuple[str, dict[str, object]]]

    def __init__(self, *_a: object, **_k: object) -> None:
        return None

    def __enter__(self) -> _RecordingClient:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
        type(self).calls.append((method, params))
        return {"run_state": "draining", "resolution": params.get("resolution")}


def _make_recording_client(sink: list[tuple[str, dict[str, object]]]) -> type[_RecordingClient]:
    """Build a recording-client class whose ``calls`` log is *sink*."""
    return type("_BoundRecordingClient", (_RecordingClient,), {"calls": sink})


# --------------------------------------------------------------------------
# reason_headline -- naming WHY the lane forked (C1, boundary + error paths)
# --------------------------------------------------------------------------


def test_reason_headline_high_risk_close_names_reason() -> None:
    """A high-risk-close fork names the held-close reason (C1)."""
    headline = reason_headline(_fork(reason=FleetForkReason.HIGH_RISK_CLOSE))
    assert "high-risk" in headline


def test_reason_headline_uncalibrated_jury_names_reason() -> None:
    """An uncalibrated-jury fork names the advisory-authority reason (C1)."""
    headline = reason_headline(_fork(reason=FleetForkReason.UNCALIBRATED_JURY))
    assert "jury" in headline


def test_reason_headline_needs_user_split_names_reason() -> None:
    """A needs-user-split fork names the clarification reason (C1)."""
    headline = reason_headline(_fork(reason=FleetForkReason.NEEDS_USER_SPLIT))
    assert "needs-user" in headline


def test_reason_headline_three_reasons_are_distinct() -> None:
    """The three fork reasons render three DISTINCT headlines (C1)."""
    high = reason_headline(_fork(reason=FleetForkReason.HIGH_RISK_CLOSE))
    jury = reason_headline(_fork(reason=FleetForkReason.UNCALIBRATED_JURY))
    split = reason_headline(_fork(reason=FleetForkReason.NEEDS_USER_SPLIT))
    assert len({high, jury, split}) == 3


# --------------------------------------------------------------------------
# tier_badge / evidence_line -- the band badge + evidence read off the fork
# --------------------------------------------------------------------------


def test_tier_badge_reads_risk_tier_off_fork() -> None:
    """The band badge reads the risk tier STRAIGHT off the fork (C1)."""
    assert tier_badge(_fork(risk_tier=RiskTier.UI)) == "UI"
    assert tier_badge(_fork(risk_tier=RiskTier.HIGH)) == "HIGH"


def test_evidence_line_reads_evidence_ref_off_fork() -> None:
    """The evidence row reads the evidence ref off the fork (C1)."""
    body = evidence_line(_fork(evidence_ref="urn:eawf:v1:close:QR"))
    assert body == "urn:eawf:v1:close:QR"


def test_evidence_line_missing_ref_reads_honest_empty() -> None:
    """A fork with no evidence ref reads the honest-empty marker, not blank."""
    assert evidence_line(_fork(evidence_ref=None)) == EVIDENCE_NONE


def test_render_options_row_lists_all_four_option_keys() -> None:
    """The options row names all four resolution keys + labels (C1)."""
    row = render_options_row()
    for key in ("a", "r", "s", "x"):
        assert key in row
    assert "approve-close" in row
    assert "re-dispatch" in row
    assert "skip" in row
    assert "abort run" in row


# --------------------------------------------------------------------------
# issue_resolve -- routes fleet.resolve_fork with the chosen resolution
# --------------------------------------------------------------------------


def test_issue_resolve_no_daemon_issues_no_rpc() -> None:
    """An unreachable daemon issues no RPC and reads the honest unavailable line."""
    line = issue_resolve(_fork(), FleetForkResolution.APPROVE_CLOSE, daemon_available=False)
    assert RESOLVE_NO_DAEMON in line


def test_issue_resolve_routes_resolve_fork_with_wave_attempt_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reachable daemon routes ``fleet.resolve_fork`` with wave / attempt / resolution."""
    from eawf.surfaces.cli import _daemon_client as dc

    calls: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(dc, "DaemonClient", _make_recording_client(calls))
    line = issue_resolve(
        _fork(wave_id="P30-I13-W05", attempt=2),
        FleetForkResolution.RE_DISPATCH,
        daemon_available=True,
    )
    assert calls and calls[0][0] == "fleet.resolve_fork"
    assert calls[0][1]["wave_id"] == "P30-I13-W05"
    assert calls[0][1]["attempt"] == 2
    assert calls[0][1]["resolution"] == "re_dispatch"
    assert "re_dispatch" in line
    assert "draining" in line  # the run state the daemon returned


# --------------------------------------------------------------------------
# Mounted overlay -- renders the card; honest-empty; queue cycle (C1 + C2)
# --------------------------------------------------------------------------


def test_fork_inbox_card_renders_wave_tier_reason_evidence_and_options(tmp_path: Path) -> None:
    """The mounted card renders the wave, tier badge, reason, evidence + options (C1)."""
    state_path = _write_state(tmp_path, _state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            fork = _fork(
                wave_id="P30-I13-W05",
                risk_tier=RiskTier.UI,
                reason=FleetForkReason.HIGH_RISK_CLOSE,
                evidence_ref="urn:eawf:v1:close:P30-I13-W05",
            )
            await app.push_screen(ForkInboxModal((fork,)))
            await settle_screen(pilot)
            modal = app.screen
            assert isinstance(modal, ForkInboxModal)
            header = str(modal.query_one(f"#{FORK_INBOX_HEADER_ID}").render())  # type: ignore[attr-defined]
            assert FORK_INBOX_TITLE in header
            wave = str(modal.query_one(f"#{FORK_INBOX_WAVE_ID}").render())  # type: ignore[attr-defined]
            assert "P30-I13-W05" in wave
            assert "UI" in wave  # the band badge
            reason = str(modal.query_one(f"#{FORK_INBOX_REASON_ID}").render())  # type: ignore[attr-defined]
            assert "high-risk" in reason
            evidence = str(modal.query_one(f"#{FORK_INBOX_EVIDENCE_ID}").render())  # type: ignore[attr-defined]
            assert "urn:eawf:v1:close:P30-I13-W05" in evidence
            options = str(modal.query_one(f"#{FORK_INBOX_OPTIONS_ID}").render())  # type: ignore[attr-defined]
            for key in ("a", "r", "s", "x"):
                assert key in options

    asyncio.run(body())


def test_fork_inbox_empty_queue_renders_honest_empty_literal(tmp_path: Path) -> None:
    """An empty fork queue renders the honest-empty literal (C2)."""
    state_path = _write_state(tmp_path, _state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await app.push_screen(ForkInboxModal(()))
            await settle_screen(pilot)
            modal = app.screen
            assert isinstance(modal, ForkInboxModal)
            body_line = str(modal.query_one(f"#{FORK_INBOX_REASON_ID}").render())  # type: ignore[attr-defined]
            assert FORK_INBOX_EMPTY in body_line

    asyncio.run(body())


def test_fork_inbox_empty_queue_matches_golden(tmp_path: Path) -> None:
    """The honest-empty fork-inbox card matches the pinned golden (C2).

    Regenerate with ``EAWF_SNAPSHOT_REGEN=1 uv run pytest
    tests/tui/test_overlay_fork_inbox.py``.
    """
    state_path = _write_state(tmp_path, _state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(96, 24)) as pilot:
            await settle_screen(pilot)
            await app.push_screen(ForkInboxModal(()))
            await settle_screen(pilot)
            assert isinstance(app.screen, ForkInboxModal)
            assert_screen_snapshot(app, _GOLDEN / "empty.txt")

    asyncio.run(body())


@pytest.mark.parametrize("width", [40, 48])
def test_fork_inbox_populated_narrow_matches_golden(tmp_path: Path, width: int) -> None:
    """The populated fork-inbox card stays coherent at 40/48 columns."""
    state_path = _write_state(tmp_path, _state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(width, 40)) as pilot:
            await settle_screen(pilot)
            await app.push_screen(ForkInboxModal((_fork(),)))
            await settle_screen(pilot)
            assert isinstance(app.screen, ForkInboxModal)
            assert_screen_snapshot(app, _GOLDEN / f"populated_w{width}.txt")

    asyncio.run(body())


def test_fork_inbox_multiple_forks_render_count(tmp_path: Path) -> None:
    """A multi-fork queue renders an ``i/N`` count in the header (C2)."""
    state_path = _write_state(tmp_path, _state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            forks = (
                _fork(wave_id="P30-I13-W05"),
                _fork(wave_id="P30-I13-W06", reason=FleetForkReason.UNCALIBRATED_JURY),
            )
            await app.push_screen(ForkInboxModal(forks))
            await settle_screen(pilot)
            modal = app.screen
            assert isinstance(modal, ForkInboxModal)
            header = str(modal.query_one(f"#{FORK_INBOX_HEADER_ID}").render())  # type: ignore[attr-defined]
            assert "1/2" in header  # the queue position

    asyncio.run(body())


def test_fork_inbox_tab_cycles_between_queued_forks(tmp_path: Path) -> None:
    """``Tab`` cycles a multi-fork queue without losing one (C2)."""
    state_path = _write_state(tmp_path, _state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            forks = (
                _fork(wave_id="P30-I13-W05"),
                _fork(wave_id="P30-I13-W06"),
            )
            await app.push_screen(ForkInboxModal(forks))
            await settle_screen(pilot)
            modal = app.screen
            assert isinstance(modal, ForkInboxModal)
            wave = str(modal.query_one(f"#{FORK_INBOX_WAVE_ID}").render())  # type: ignore[attr-defined]
            assert "P30-I13-W05" in wave
            await pilot.press("tab")  # cycle to the second fork
            await settle_screen(pilot)
            wave2 = str(modal.query_one(f"#{FORK_INBOX_WAVE_ID}").render())  # type: ignore[attr-defined]
            assert "P30-I13-W06" in wave2

    asyncio.run(body())


def test_fork_inbox_no_daemon_keeps_card_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed resolve surfaces the honest toast and keeps the card visible."""
    state_path = _write_state(tmp_path, _state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: False)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await app.push_screen(ForkInboxModal((_fork(wave_id="P30-I13-W05"),)))
            await settle_screen(pilot)
            assert isinstance(app.screen, ForkInboxModal)
            await pilot.press("a")
            await settle_screen(pilot)
            modal = app.screen
            assert isinstance(modal, ForkInboxModal)
            wave = str(modal.query_one(f"#{FORK_INBOX_WAVE_ID}").render())  # type: ignore[attr-defined]
            assert "P30-I13-W05" in wave
            toasts = "\n".join(toast_messages(app))
            assert RESOLVE_NO_DAEMON in toasts

    asyncio.run(body())


def test_fork_inbox_resolve_advances_then_drains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolving a fork advances to the next; the last resolution dismisses (C2)."""
    state_path = _write_state(tmp_path, _state())
    calls: list[tuple[str, dict[str, object]]] = []

    async def body() -> None:
        from eawf.surfaces.cli import _daemon_client as dc

        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: True)
        monkeypatch.setattr(dc, "DaemonClient", _make_recording_client(calls))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            forks = (
                _fork(wave_id="P30-I13-W05"),
                _fork(wave_id="P30-I13-W06"),
            )
            cockpit = app.screen
            await app.push_screen(ForkInboxModal(forks))
            await settle_screen(pilot)
            assert isinstance(app.screen, ForkInboxModal)
            await pilot.press("a")  # approve-close the first fork
            await settle_screen(pilot)
            modal = app.screen
            assert isinstance(modal, ForkInboxModal)  # one fork remains -> still open
            wave = str(modal.query_one(f"#{FORK_INBOX_WAVE_ID}").render())  # type: ignore[attr-defined]
            assert "P30-I13-W06" in wave  # advanced to the next queued fork
            await pilot.press("s")  # skip the last fork
            await settle_screen(pilot)
            assert app.screen is cockpit  # the drained inbox dismissed

    asyncio.run(body())
    # Both resolutions reached the daemon with the right resolution values.
    assert [c[0] for c in calls] == ["fleet.resolve_fork", "fleet.resolve_fork"]
    assert calls[0][1]["wave_id"] == "P30-I13-W05"
    assert calls[0][1]["resolution"] == "approve_close"
    assert calls[1][1]["wave_id"] == "P30-I13-W06"
    assert calls[1][1]["resolution"] == "skip"
