"""Tests for the Doctor-mode health pane (P29-I02-W21, TUI-3).

The Doctor mode (digit ``5``) folds the same health signals ``eawf
doctor`` reports into ONE in-TUI health view -- the doctor check library,
git/state commit drift, and the recent event-store tail -- and rolls them
up to one overall health. These tests pin:

* the pure fold (:func:`~eawf.surfaces.tui.modes.doctor.build_doctor_health`)
  surfaces mixed check statuses (ok / warn / fail), the drift count + the
  distinct kinds, an all-ok / honest-empty clean case, and the correct
  rolled-up overall (the doctor library's own highest-severity reducer);
* the fold is a renderer over the doctor check library -- it reads the
  ``CheckResult`` rows verbatim and never re-runs a check (verified by
  feeding a sentinel ``CheckResult`` and asserting it is passed through
  untouched);
* the recent-events fold surfaces the error count but does not flip the
  rollup (an old log error is not a live install fault);
* the render lines tint the per-status glyph and surface the DRIFT block;
* the registry swaps the ``doctor`` placeholder for the real factory;
* the mode mounts under Pilot and renders the health pane cleanly on the
  honest-empty (no closed waves) and the drift-present live paths.

Determinism follows the project Pilot-worker rule: each Pilot body drains
workers via :func:`~eawf.surfaces.tui.snapshot.settle_screen`
(``pilot.pause()`` is CPU-idle-based, not worker-aware) before asserting.
The autouse ``_isolate_registry`` fixture redirects ``Path.home`` so a
``u`` switch never reads the operator's real registry, and ``_stub_git``
pins the git/state drift probe so the live path is deterministic.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.models import State
from eawf.observability.doctor.checks import CheckResult
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modes.doctor import (
    DoctorHealth,
    DoctorModeScreen,
    HealthRow,
    build_doctor_health,
    doctor_mode_factory,
    gather_doctor_health,
    render_health_lines,
)
from eawf.surfaces.tui.modes.registry import MODE_REGISTRY, mode_for_name
from eawf.surfaces.tui.scopes import RepoScreen
from eawf.surfaces.tui.screens.overlays.events import EventRow
from eawf.surfaces.tui.snapshot import (
    assert_screen_snapshot,
    capture_screen_text,
    normalize_snapshot,
    settle_screen,
)
from eawf.surfaces.tui.widgets.git_pane import GitFields
from eawf.surfaces.tui.widgets.sigils import Sigil, chrome, glyph
from eawf.workflow.lifecycle.wave_sha import Drift

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_REPO = _FIXTURES / "03-phase-iter-wave-active.json"

_SHA_A = "a" * 40
_SHA_B = "b" * 40


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate registry + probe-cache writes into ``tmp_path``.

    Two test-hygiene guards for the live Doctor-mode mount:

    * The ``u`` scope switch reads ``~/.eawf/registry.json``; redirecting
      ``Path.home`` keeps any scope-switch deterministic and reads no real
      registry.
    * The live doctor mount runs the instrument probe, which writes a cache
      to ``<workspace>/.ea/instrument-probe.json``; the workspace anchor
      resolves to the fixture tree, so without a redirect the probe would
      drop a stray cache file under ``tests/fixtures/``. Pointing
      ``EA_INSTRUMENT_PROBE`` at ``tmp_path`` keeps the cache out of the
      repo (and the probe deterministic per-run).
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("EA_INSTRUMENT_PROBE", str(tmp_path / "instrument-probe.json"))


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
# Test data builders
# --------------------------------------------------------------------------


def _event_row(*, status: str, event_type: str = "wave close") -> EventRow:
    """Build an :class:`EventRow` with the given status for the events fold."""
    return EventRow(
        event_id="EV-1",
        timestamp="2026-06-01T00:00:00Z",
        event_type=event_type,
        status=status,
        summary="x",
    )


def _base_state_payload() -> dict[str, Any]:
    """Minimal :class:`State` payload acceptable by Pydantic."""
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:ZZ",
        "updated_at": "2026-05-27T00:00:00Z",
        "project": {
            "code": "ZZ",
            "slug": "zz",
            "title": "ZZ",
            "description": "",
            "domains": [],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:ZZ",
        },
        "current": {
            "project_code": "ZZ",
            "track_id": None,
            "phase_id": None,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _closed_wave_payload(wave_id: str, *, commit: str | None) -> dict[str, Any]:
    """Build a CLOSED wave payload for the drift live path."""
    return {
        "id": wave_id,
        "iter_id": wave_id.rsplit("-", 1)[0],
        "title": f"wave {wave_id}",
        "status": "closed",
        "deps": [],
        "blocks": [],
        "file_scopes": [],
        "claim_session_id": None,
        "worktree_id": None,
        "token_budget": None,
        "tokens_consumed": 0,
        "outcome": None,
        "commit": commit,
        "opened_at": "2026-05-27T00:00:00Z",
        "closed_at": "2026-05-27T00:01:00Z",
    }


def _state_with_closed_waves(tmp_path: Path, waves: list[dict[str, Any]]) -> Path:
    """Write a state.json with the given closed waves; return its path."""
    import orjson

    payload = _base_state_payload()
    payload["waves"] = {w["id"]: w for w in waves}
    State.model_validate(payload)  # fail fast on a malformed payload
    ea_dir = tmp_path / ".ea"
    ea_dir.mkdir(parents=True, exist_ok=True)
    state_path = ea_dir / "state.json"
    state_path.write_bytes(orjson.dumps(payload))
    return state_path


# --------------------------------------------------------------------------
# Pure fold -- build_doctor_health
# --------------------------------------------------------------------------


def test_build_doctor_health_surfaces_mixed_check_statuses() -> None:
    """Every doctor check status (ok / warn / fail) appears as a health row."""
    checks = [
        CheckResult(name="tools_available", status="ok", detail="3 probes ok"),
        CheckResult(name="state_present", status="warn", detail="no state.json"),
        CheckResult(name="manifest_in_sync", status="fail", detail="malformed"),
    ]
    health = build_doctor_health(checks, [], ())
    by_name = {row.name: row for row in health.rows}
    assert by_name["tools_available"].status == "ok"
    assert by_name["state_present"].status == "warn"
    assert by_name["manifest_in_sync"].status == "fail"
    # The folded-in drift + events rows ride alongside the checks.
    assert "git_state_drift" in by_name
    assert "recent_events" in by_name


def test_build_doctor_health_rolls_up_to_highest_severity() -> None:
    """The overall is the highest-severity status across checks + drift."""
    ok_only = build_doctor_health(
        [CheckResult(name="a", status="ok"), CheckResult(name="b", status="ok")], [], ()
    )
    assert ok_only.overall == "ok"
    with_warn = build_doctor_health(
        [CheckResult(name="a", status="ok"), CheckResult(name="b", status="warn")], [], ()
    )
    assert with_warn.overall == "warn"
    with_fail = build_doctor_health(
        [CheckResult(name="a", status="warn"), CheckResult(name="b", status="fail")], [], ()
    )
    assert with_fail.overall == "fail"


def test_build_doctor_health_drift_flips_overall_to_warn() -> None:
    """A drift row lifts an otherwise-ok rollup to warn (matches the CLI)."""
    checks = [CheckResult(name="tools_available", status="ok", detail="ok")]
    drifts = [Drift(wave_id="P29-I02-W01", kind="closed_no_pin")]
    health = build_doctor_health(checks, drifts, ())
    assert health.overall == "warn"
    drift_row = next(r for r in health.rows if r.name == "git_state_drift")
    assert drift_row.status == "warn"


def test_build_doctor_health_surfaces_drift_count_and_kinds() -> None:
    """The drift summary carries the count and the distinct, sorted kinds."""
    drifts = [
        Drift(
            wave_id="P29-I02-W02", kind="pinned_mismatch", state_commit=_SHA_A, git_commit=_SHA_B
        ),
        Drift(wave_id="P29-I02-W01", kind="closed_no_pin"),
        Drift(wave_id="P29-I02-W03", kind="closed_no_pin"),
    ]
    health = build_doctor_health([CheckResult(name="a", status="ok")], drifts, ())
    assert health.drift_count == 3
    # Distinct + sorted (two closed_no_pin collapse to one kind label).
    assert health.drift_kinds == ["closed_no_pin", "pinned_mismatch"]
    drift_row = next(r for r in health.rows if r.name == "git_state_drift")
    assert "3 drift(s)" in drift_row.detail
    assert "closed_no_pin" in drift_row.detail
    assert "pinned_mismatch" in drift_row.detail


def test_build_doctor_health_all_ok_clean_case() -> None:
    """An all-ok check set with no drift / events rolls up to ok (healthy)."""
    checks = [
        CheckResult(name="tools_available", status="ok", detail="3 probes ok"),
        CheckResult(name="state_present", status="ok", detail="found"),
        CheckResult(name="config_resolves", status="ok", detail="2 profiles"),
    ]
    health = build_doctor_health(checks, [], ())
    assert health.overall == "ok"
    assert health.drift_count == 0
    assert health.drift_kinds == []
    assert health.event_error_count == 0
    # The drift + events rows are honest-empty ok rows.
    assert all(
        r.status == "ok" for r in health.rows if r.name in {"git_state_drift", "recent_events"}
    )


def test_build_doctor_health_reuses_check_results_verbatim() -> None:
    """The fold passes each doctor CheckResult through untouched (reuse, not reimplement).

    A sentinel CheckResult with a synthetic name + detail must appear in
    ``health.rows`` with its status + detail intact -- proving the pane is a
    renderer over the doctor check library, not a reimplementation that
    re-derives the status.
    """
    sentinel = CheckResult(name="sentinel_probe", status="warn", detail="SENTINEL-DETAIL-7f")
    health = build_doctor_health([sentinel], [], ())
    row = next(r for r in health.rows if r.name == "sentinel_probe")
    assert row.status == "warn"
    assert row.detail == "SENTINEL-DETAIL-7f"


def test_build_doctor_health_event_errors_do_not_flip_rollup() -> None:
    """A recent-events error is surfaced but never lifts the install rollup.

    An old error already on disk is not a live install fault, so the events
    row stays ``ok`` and the overall stays ``ok`` even with errors in the
    window (the same stance the CLI takes by not folding the event store
    into its checks).
    """
    checks = [CheckResult(name="tools_available", status="ok", detail="ok")]
    events = (_event_row(status="error", event_type="daemon error"), _event_row(status="ok"))
    health = build_doctor_health(checks, [], events)
    assert health.event_error_count == 1
    events_row = next(r for r in health.rows if r.name == "recent_events")
    assert events_row.status == "ok"
    assert "1 error(s)" in events_row.detail
    # Overall stays ok -- the event error did not flip it.
    assert health.overall == "ok"


def test_build_doctor_health_empty_events_is_honest_empty() -> None:
    """An empty event store renders an honest 'no recent events' ok row."""
    health = build_doctor_health([CheckResult(name="a", status="ok")], [], ())
    events_row = next(r for r in health.rows if r.name == "recent_events")
    assert events_row.status == "ok"
    assert events_row.detail == "no recent events"


# --------------------------------------------------------------------------
# Render -- render_health_lines
# --------------------------------------------------------------------------


def test_render_health_lines_titles_the_rollup_word() -> None:
    """The body opens with a ``Health: <word>`` rollup title per status."""
    healthy = DoctorHealth(rows=[HealthRow("a", "ok", "ok")], overall="ok")
    assert "healthy" in render_health_lines(healthy)[0]
    degraded = DoctorHealth(rows=[HealthRow("a", "warn", "w")], overall="warn")
    assert "degraded" in render_health_lines(degraded)[0]
    unhealthy = DoctorHealth(rows=[HealthRow("a", "fail", "f")], overall="fail")
    assert "unhealthy" in render_health_lines(unhealthy)[0]


def test_render_health_lines_shows_each_status_sigil() -> None:
    """Each row renders its status sigil (closed / attention / failed) + name.

    The SHAPE comes from the shared sigils home: ``ok`` wears the CLOSED
    filled circle, ``warn`` wears the ``attention`` triangle, and ``fail``
    wears the FAILED multiplication cross. The check names ride alongside.
    """
    health = DoctorHealth(
        rows=[
            HealthRow("tools_available", "ok", "3 probes ok"),
            HealthRow("state_present", "warn", "no state.json"),
            HealthRow("manifest_in_sync", "fail", "malformed"),
        ],
        overall="fail",
    )
    body = "\n".join(render_health_lines(health, mode="unicode"))
    assert glyph(Sigil.CLOSED, mode="unicode") in body
    assert chrome("attention", mode="unicode") in body
    assert glyph(Sigil.FAILED, mode="unicode") in body
    assert "tools_available" in body
    assert "manifest_in_sync" in body


def test_render_health_lines_warn_sigil_distinct_from_pending() -> None:
    """A warn row wears the attention triangle, NEVER the pending ring.

    The I00 correctness rule: a degraded check must not be shape-identical
    to a not-yet-run one. The warn mark is the ``attention`` chrome triangle
    (distinct shape); the PENDING lifecycle ring must be absent from the
    rendered body so the two never collide.
    """
    health = DoctorHealth(
        rows=[HealthRow("state_present", "warn", "no state.json")],
        overall="warn",
    )
    body = "\n".join(render_health_lines(health, mode="unicode"))
    attention = chrome("attention", mode="unicode")
    pending = glyph(Sigil.PENDING, mode="unicode")
    assert attention != pending  # guards the I00 shape-distinction premise
    assert attention in body
    assert pending not in body


def test_render_health_lines_groups_install_state_drift_sections() -> None:
    """The body groups rows under Install / State / Drift section headers.

    Each doctor signal renders under exactly one section in install ->
    state -> drift order: the toolchain / config / render probes under
    Install, the state-presence / scale / events signals under State, and
    the manifest / MCP / git-state reconciler signals under Drift.
    """
    health = DoctorHealth(
        rows=[
            HealthRow("tools_available", "ok", "3 probes ok"),
            HealthRow("config_resolves", "ok", "2 profile(s)"),
            HealthRow("render_output_roundtrip", "ok", "ok"),
            HealthRow("state_present", "ok", "found"),
            HealthRow("state_scale_ceiling", "ok", "under ceiling"),
            HealthRow("manifest_in_sync", "ok", "synced"),
            HealthRow("mcp_drift", "ok", "no drift"),
            HealthRow("git_state_drift", "ok", "reconciles"),
            HealthRow("recent_events", "ok", "no recent events"),
        ],
        overall="ok",
    )
    body = "\n".join(render_health_lines(health, mode="unicode"))
    # The three section headers all appear, in order.
    assert "Install" in body
    assert "State" in body
    assert "Drift" in body
    install_at = body.index("Install")
    state_at = body.index("State")
    drift_at = body.index("Drift")
    assert install_at < state_at < drift_at
    # An install-section row sits before the State header; a drift-section
    # row sits after the Drift header (the grouping is honoured, not flat).
    assert body.index("tools_available") < state_at
    assert body.index("git_state_drift") > drift_at
    assert body.index("state_present") > state_at
    assert body.index("state_present") < drift_at


def test_render_health_lines_header_reads_n_checks_m_warn() -> None:
    """The rollup title carries an ``N checks · M warn`` summary.

    The count is every rendered signal row; the warn count is the rows whose
    status is ``warn`` (the attention-triangle rows), so the operator reads
    the degraded-check tally at a glance from the header. The two counts are
    joined by the reskin's pinned middle-dot separator, not a comma.
    """
    health = DoctorHealth(
        rows=[
            HealthRow("tools_available", "ok", "ok"),
            HealthRow("state_present", "warn", "no state.json"),
            HealthRow("manifest_in_sync", "warn", "hand-edited"),
            HealthRow("git_state_drift", "ok", "reconciles"),
        ],
        overall="warn",
    )
    header = render_health_lines(health, mode="unicode")[0]
    assert "4 checks · 2 warn" in header


def test_render_health_lines_header_zero_warn_clean_case() -> None:
    """An all-ok health reads ``N checks · 0 warn`` in the header."""
    health = DoctorHealth(
        rows=[HealthRow("tools_available", "ok", "ok"), HealthRow("state_present", "ok", "found")],
        overall="ok",
    )
    header = render_health_lines(health, mode="unicode")[0]
    assert "2 checks · 0 warn" in header


def test_render_health_lines_ascii_mode_uses_ascii_sigil_column() -> None:
    """In ASCII mode each row wears the ASCII sigil column, not unicode.

    The mode threads into the sigil helpers, so an ``ascii`` render draws
    the ASCII fallback glyphs (closed=``@``, warn=``!``, fail=``x``) and the
    unicode marks are absent -- the byte-stable column for a font-poor
    terminal.
    """
    health = DoctorHealth(
        rows=[
            HealthRow("tools_available", "ok", "ok"),
            HealthRow("state_present", "warn", "no state.json"),
            HealthRow("manifest_in_sync", "fail", "bad"),
        ],
        overall="fail",
    )
    body = "\n".join(render_health_lines(health, mode="ascii"))
    assert glyph(Sigil.CLOSED, mode="ascii") in body
    assert chrome("attention", mode="ascii") in body
    assert glyph(Sigil.FAILED, mode="ascii") in body
    # The unicode marks must not bleed into the ASCII column.
    assert glyph(Sigil.CLOSED, mode="unicode") not in body
    assert glyph(Sigil.FAILED, mode="unicode") not in body


def test_render_health_lines_surfaces_drift_block() -> None:
    """A drift-present health renders the DRIFT count + kinds block."""
    health = DoctorHealth(
        rows=[HealthRow("git_state_drift", "warn", "2 drift(s)")],
        overall="warn",
        drift_count=2,
        drift_kinds=["closed_no_pin", "pinned_mismatch"],
    )
    body = "\n".join(render_health_lines(health))
    assert "2 wave(s)" in body
    assert "closed_no_pin" in body
    assert "pinned_mismatch" in body


def test_render_health_lines_no_drift_block_when_clean() -> None:
    """An all-ok health renders no DRIFT block (honest-empty)."""
    health = DoctorHealth(rows=[HealthRow("a", "ok", "ok")], overall="ok", drift_count=0)
    body = "\n".join(render_health_lines(health))
    assert "wave(s)" not in body
    assert "kinds:" not in body


def test_render_health_lines_escapes_bracketed_detail() -> None:
    """A detail carrying a ``[P##-W##]`` bracket renders literally (escaped)."""
    health = DoctorHealth(rows=[HealthRow("recent_events", "ok", "[P29-W01] closed")], overall="ok")
    body = "\n".join(render_health_lines(health))
    # The bracket is backslash-escaped so Textual renders it literally.
    assert "\\[P29-W01]" in body


# --------------------------------------------------------------------------
# Registry wiring
# --------------------------------------------------------------------------


def test_registry_doctor_is_the_real_factory_not_placeholder() -> None:
    """The ``doctor`` registry row builds a DoctorModeScreen, not a placeholder."""
    from eawf.surfaces.tui.modes import PlaceholderModeScreen

    spec = mode_for_name("doctor")
    assert spec is not None
    screen = spec.factory(EaApp(scope="repo", state_path=_REPO))
    assert isinstance(screen, DoctorModeScreen)
    assert not isinstance(screen, PlaceholderModeScreen)


def test_doctor_mode_factory_ignores_app_and_builds_screen() -> None:
    """The factory is scope-independent: any app builds a fresh DoctorModeScreen."""
    screen = doctor_mode_factory(EaApp(scope="workspace", state_path=_REPO))
    assert isinstance(screen, DoctorModeScreen)
    # Doctor keeps its digit + title in the registry.
    spec = next(s for s in MODE_REGISTRY if s.name == "doctor")
    assert (spec.digit, spec.title) == ("5", "Doctor")


# --------------------------------------------------------------------------
# Pilot -- the mode mounts + renders the health pane
# --------------------------------------------------------------------------


def test_doctor_mode_mounts_and_renders_health_view() -> None:
    """Digit ``5`` switches to Doctor; the pane renders a rollup + check rows."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("5")
            await settle_screen(pilot)
            assert app.current_mode == "doctor"
            assert isinstance(app.screen, DoctorModeScreen)
            frame = normalize_snapshot(capture_screen_text(app))
            # The rollup title + at least the tools-available check row paint.
            assert "Health:" in frame
            assert "tools_available" in frame

    asyncio.run(body())


def test_doctor_mode_renders_honest_empty_clean_case() -> None:
    """Against a fixture with no closed waves, the drift fold is honest-empty.

    The drift reconciler finds no closed waves to reconcile, so the
    ``git_state_drift`` row is ``ok`` and no DRIFT block paints -- the pane
    renders cleanly without inventing a problem.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("5")
            await settle_screen(pilot)
            frame = normalize_snapshot(capture_screen_text(app))
            assert "git_state_drift" in frame
            assert "recent_events" in frame
            # No closed waves -> no DRIFT count block.
            assert "wave(s)" not in frame

    asyncio.run(body())


def test_doctor_mode_keeps_shared_chassis_brand() -> None:
    """The Doctor pane keeps the shared chassis: brand + breadcrumb on row 0."""

    async def body() -> None:
        from eawf.surfaces.tui.widgets.header import BRAND

        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("5")
            await settle_screen(pilot)
            header_row = normalize_snapshot(capture_screen_text(app)).splitlines()[0]
            assert BRAND in header_row
            assert "Doctor" in header_row

    asyncio.run(body())


def test_doctor_mode_round_trip_back_to_home() -> None:
    """A Doctor mode flip and back to Home restores the repo scope screen."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("5")
            await settle_screen(pilot)
            assert isinstance(app.screen, DoctorModeScreen)
            await pilot.press("1")
            await settle_screen(pilot)
            assert app.current_mode == "home"
            assert isinstance(app.screen, RepoScreen)

    asyncio.run(body())


def test_gather_doctor_health_surfaces_live_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The impure gather folds a live drift row from a closed-wave state.

    Builds a state with a CLOSED wave pinned to a SHA that ``git`` cannot
    find, stubs the drift probe (git on PATH, derive returns ``None``), and
    asserts the gathered health surfaces the ``pinned_but_missing`` drift
    and lifts the overall to ``warn`` -- the live reconciler path the pane
    renders.
    """
    monkeypatch.setattr("eawf.workflow.lifecycle.wave_sha.shutil.which", lambda _: "/usr/bin/git")
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha.build_wave_sha_index",
        lambda repo_root=None: {},
    )
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha.derive_wave_sha",
        lambda wid, repo_root=None, index=None: None,
    )
    state_path = _state_with_closed_waves(
        tmp_path, [_closed_wave_payload("P29-I02-W01", commit=_SHA_A)]
    )
    workspace = state_path.parent.parent
    health = gather_doctor_health(workspace=workspace, state_path=state_path)
    assert health.drift_count == 1
    assert health.drift_kinds == ["pinned_but_missing"]
    drift_row = next(r for r in health.rows if r.name == "git_state_drift")
    assert drift_row.status == "warn"
    # The drift lifts the overall to at least warn.
    assert health.overall in {"warn", "fail"}


def test_gather_doctor_health_no_state_is_total(tmp_path: Path) -> None:
    """A missing state.json degrades the gather to an honest health (no raise).

    With no state path, the drift fold is empty and the events fold is
    honest-empty; the doctor checks degrade their state-reading rows to
    ``ok`` on their own. The gather must return a DoctorHealth, never raise.
    """
    health = gather_doctor_health(workspace=tmp_path, state_path=None)
    assert isinstance(health, DoctorHealth)
    assert health.drift_count == 0
    events_row = next(r for r in health.rows if r.name == "recent_events")
    assert events_row.detail == "no recent events"


# --------------------------------------------------------------------------
# Worker offload -- the gather runs off the UI thread (the hang fix)
# --------------------------------------------------------------------------


def _sentinel_health(detail: str) -> DoctorHealth:
    """Build a one-row :class:`DoctorHealth` carrying a sentinel detail."""
    return DoctorHealth(rows=[HealthRow("sentinel_probe", "ok", detail)], overall="ok")


def test_doctor_mode_paints_health_via_worker_without_hanging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Doctor mode mounts, the gather worker completes, and real health paints.

    The regression guard for the operator-reported hang: the gather runs
    blocking subprocesses (doctor tool-probes + a per-wave ``git log`` drift
    scan) that, on the UI thread, freeze the whole app. Here the gather is
    stubbed to a sentinel so the assertion is deterministic, but the point
    is structural: ``run_test`` (a real event loop) must reach the painted
    frame -- if the gather still ran on the UI thread the worker drain +
    settle would never resolve. After ``wait_for_complete()`` the pane shows
    the folded health, NOT the mount-time placeholder.
    """
    import eawf.surfaces.tui.modes.doctor as doctor_mod

    def _fake_gather(*, workspace: object, state_path: object) -> DoctorHealth:
        return _sentinel_health("SENTINEL-WORKER-OK")

    monkeypatch.setattr(doctor_mod, "gather_doctor_health", _fake_gather)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("5")
            await pilot.app.workers.wait_for_complete()
            await settle_screen(pilot)
            assert isinstance(app.screen, DoctorModeScreen)
            frame = normalize_snapshot(capture_screen_text(app))
            # The real folded health replaced the placeholder after the
            # worker resolved -- the hang would never have reached this row.
            assert "SENTINEL-WORKER-OK" in frame
            assert "gathering health" not in frame

    asyncio.run(body())


def test_doctor_mode_shows_placeholder_before_worker_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The instant ``gathering health...`` placeholder paints before the gather returns.

    The worker is parked on a :class:`threading.Event` so the gather has
    NOT resolved when the frame is sampled: the pane must already show the
    honest placeholder (proving the pane paints instantly, never frozen).
    Releasing the gate then draining the worker replaces it with the real
    health -- so the placeholder is a transient, not the terminal state.
    """
    import threading

    import eawf.surfaces.tui.modes.doctor as doctor_mod

    gate = threading.Event()

    def _blocking_gather(*, workspace: object, state_path: object) -> DoctorHealth:
        # Park the worker (it runs off-thread via asyncio.to_thread) until
        # the test releases the gate, so the placeholder frame is observable.
        gate.wait(timeout=5.0)
        return _sentinel_health("SENTINEL-AFTER-GATE")

    monkeypatch.setattr(doctor_mod, "gather_doctor_health", _blocking_gather)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("5")
            # Pump the loop WITHOUT draining workers -- the gather is parked,
            # so the pane shows the mount-time placeholder, not real health.
            await pilot.pause()
            frame_before = normalize_snapshot(capture_screen_text(app))
            assert "gathering health" in frame_before
            assert "SENTINEL-AFTER-GATE" not in frame_before
            # Release the gate; the worker resolves and the real health paints.
            gate.set()
            await pilot.app.workers.wait_for_complete()
            await settle_screen(pilot)
            frame_after = normalize_snapshot(capture_screen_text(app))
            assert "SENTINEL-AFTER-GATE" in frame_after
            assert "gathering health" not in frame_after

    asyncio.run(body())


def test_doctor_mode_force_refresh_re_kicks_the_gather_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``F5`` re-gathers via a fresh worker and repaints the pane.

    The first gather paints one sentinel; a force-refresh (``F5``) re-kicks
    the worker (``exclusive`` within the gather group) and the pane repaints
    from the second gather -- proving the force-refresh path runs through
    the worker offload, not a blocking UI-thread call.
    """
    import itertools

    import eawf.surfaces.tui.modes.doctor as doctor_mod

    counter = itertools.count(1)

    def _counting_gather(*, workspace: object, state_path: object) -> DoctorHealth:
        return _sentinel_health(f"SENTINEL-GATHER-{next(counter)}")

    monkeypatch.setattr(doctor_mod, "gather_doctor_health", _counting_gather)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("5")
            await pilot.app.workers.wait_for_complete()
            await settle_screen(pilot)
            assert isinstance(app.screen, DoctorModeScreen)
            first = normalize_snapshot(capture_screen_text(app))
            assert "SENTINEL-GATHER-1" in first
            # Force-refresh -> the gather worker re-kicks and repaints.
            await pilot.press("f5")
            await pilot.app.workers.wait_for_complete()
            await settle_screen(pilot)
            second = normalize_snapshot(capture_screen_text(app))
            assert "SENTINEL-GATHER-2" in second
            assert "SENTINEL-GATHER-1" not in second

    asyncio.run(body())


# --------------------------------------------------------------------------
# Isolated grouped-render golden -- the W14 reskin close-gate bar
# --------------------------------------------------------------------------

#: The W14 isolated golden, distinct from the shared full-app
#: ``doctor_mode.txt`` so this wave owns + regenerates it without touching
#: the coupled screen-snapshot suite. Lives beside the other TUI goldens.
_GROUPED_GOLDEN = (
    Path(__file__).resolve().parents[1] / "snapshots" / "tui" / "golden" / "doctor_mode_grouped.txt"
)


def test_doctor_mode_grouped_snapshot() -> None:
    """The reskinned Doctor pane: install/state/drift groups + status sigils.

    The W14 close-gate golden. The mode's live mount gathers env-dependent
    doctor checks, so -- mirroring the shared snapshot's fixed-input stance
    -- the screen is repainted with a deterministic :class:`DoctorHealth`
    after mount. The fixture pins the install / state / drift section
    grouping, the per-status sigil (ok=closed circle, warn=attention
    triangle distinct from the pending ring, fail=failed cross), and the
    ``N checks · M warn`` header summary, so a layout / glyph regression on
    any of those is caught against a golden this wave owns in isolation.
    """
    health = DoctorHealth(
        rows=[
            HealthRow("tools_available", "ok", "3 probes ok"),
            HealthRow("config_resolves", "ok", "2 profile(s) enabled"),
            HealthRow("render_output_roundtrip", "ok", "round-trips clean"),
            HealthRow("state_present", "ok", "state.json found"),
            HealthRow("state_scale_ceiling", "warn", "approaching 5k-wave ceiling"),
            HealthRow("manifest_in_sync", "warn", "drift: AGENTS.md::core=hand-edited"),
            HealthRow("mcp_drift", "fail", "1 eawf-owned server missing"),
            HealthRow(
                "git_state_drift", "warn", "2 drift(s); kinds: closed_no_pin, pinned_mismatch"
            ),
            HealthRow("recent_events", "ok", "12 recent event(s); 1 error(s) in window"),
        ],
        overall="fail",
        drift_count=2,
        drift_kinds=["closed_no_pin", "pinned_mismatch"],
        event_error_count=1,
    )

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("5")  # -> doctor
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, DoctorModeScreen)
            # Repaint with the fixed health so the golden is deterministic.
            screen._paint_health(health)  # type: ignore[attr-defined]
            await settle_screen(pilot)
            assert_screen_snapshot(app, _GROUPED_GOLDEN)
            # Belt-and-braces text assertions over the same painted frame so
            # the close-gate criteria are pinned independently of the golden.
            frame = normalize_snapshot(capture_screen_text(app))
            # W19: the install / state / drift sections are now two-column card
            # border-titles, rendered uppercase to match the mockup chrome.
            assert "INSTALL" in frame and "STATE" in frame and "DRIFT" in frame
            assert glyph(Sigil.CLOSED, mode="unicode") in frame  # ok sigil
            assert chrome("attention", mode="unicode") in frame  # warn sigil
            assert glyph(Sigil.FAILED, mode="unicode") in frame  # fail sigil
            assert glyph(Sigil.PENDING, mode="unicode") not in frame  # warn != pending
            assert "9 checks · 3 warn" in frame

    asyncio.run(body())
