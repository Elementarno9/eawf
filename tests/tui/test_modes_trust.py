"""Tests for the Trust mode pane over ``compute_trust_scorecard`` (W20).

The Trust mode (digit ``4``) fills the W16 chassis seam with an honest
provenance pane. These tests pin the two halves:

* the pure render helpers (one per scorecard section) and the
  :func:`~eawf.surfaces.tui.modes.trust.is_data_starved` predicate, tested
  against directly-constructed :class:`TrustScorecard` objects so the
  composition is verified without mounting Textual; and
* the mounted pane under a Pilot, computing the scorecard end-to-end
  through ``compute_trust_scorecard`` -> ``read_store_projection``: a
  POPULATED repo (a closed wave with a deterministic-pass evidence row)
  surfaces its tier counts / sample sizes / per-output labels, while a
  DATA-STARVED repo (no closed waves, empty stores) renders the
  honest-negative banner and never a fabricated green score.

The honesty requirement is the load-bearing assertion: the starved frame
must show the "insufficient data for a trust signal" banner and must NOT
show a ``verified``-tier count line, so a data-starved project reads as
"no signal yet", not "trusted".

Determinism follows the project Pilot-worker rule: each Pilot body drains
workers via :func:`~eawf.surfaces.tui.snapshot.settle_screen` before
asserting.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eawf.kernel.state.enums import (
    IterStatus,
    PhaseStatus,
    ProjectStatus,
    ScopeKind,
    StoreKind,
    WaveStatus,
)
from eawf.kernel.state.models import (
    CurrentPointers,
    Iter,
    Phase,
    Project,
    State,
    Wave,
)
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.evidence import EvidenceRecord
from eawf.kernel.store.paths import store_path
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modes.trust import (
    DATA_STARVED_NOTICE,
    NO_DATA,
    TrustModeScreen,
    is_data_starved,
    render_eu_calibration,
    render_output_labels,
    render_overview,
    render_store_counts,
    render_tier_counts,
    render_verifier_reliability,
)
from eawf.surfaces.tui.snapshot import (
    capture_screen_text,
    normalize_snapshot,
    settle_screen,
)
from eawf.workflow.estimation.trust_scorecard import (
    EuCalibrationMetric,
    OutputTrustLabel,
    TrustScorecard,
    TrustTierCounts,
    VerifierReliabilityMetric,
    compute_trust_scorecard,
)

_T0 = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Scorecard builders -- directly-constructed, no compute path
# --------------------------------------------------------------------------


def _starved_scorecard() -> TrustScorecard:
    """A scorecard with no signal: no labels, empty stores, no samples."""
    return TrustScorecard(
        window="all",
        eu_calibration=EuCalibrationMetric(
            sample_count=0,
            nudged_bucket_count=0,
            max_drift_pct=None,
            bucket_drift=False,
            drift_badge="no-data",
        ),
        store_record_counts={
            StoreKind.ESTIMATE.value: 0,
            StoreKind.ACTUAL.value: 0,
            StoreKind.AUDIT.value: 0,
            StoreKind.EVIDENCE.value: 0,
        },
        output_labels=[],
        tier_counts=TrustTierCounts(),
        verifier_reliability=VerifierReliabilityMetric(
            status="deferred_v0.4.1",
            sample_count=0,
            pass_rate=None,
            note="no deterministic verifier evidence in window",
        ),
    )


def _populated_scorecard() -> TrustScorecard:
    """A scorecard backed by labels, store rows, samples, and residuals."""
    return TrustScorecard(
        window="all",
        eu_calibration=EuCalibrationMetric(
            sample_count=4,
            nudged_bucket_count=1,
            max_drift_pct=42.5,
            bucket_drift=True,
            drift_badge="bucket-drift",
        ),
        store_record_counts={
            StoreKind.ESTIMATE.value: 3,
            StoreKind.ACTUAL.value: 2,
            StoreKind.AUDIT.value: 1,
            StoreKind.EVIDENCE.value: 5,
        },
        output_labels=[
            OutputTrustLabel(
                urn="urn:eawf:v1:wave:QR/P01-I01-W01",
                scope_id="P01-I01-W01",
                tier="verified",
                evidence_refs=["EV-aaaaaaaaaaaa", "EV-bbbbbbbbbbbb"],
                reason="verified by evidence record",
            ),
            OutputTrustLabel(
                urn="urn:eawf:v1:wave:QR/P01-I01-W02",
                scope_id="P01-I01-W02",
                tier="deferred_outcome",
                evidence_refs=[],
                reason="outcome or actual store row not available yet",
            ),
        ],
        tier_counts=TrustTierCounts(verified=1, deferred_outcome=1),
        verifier_reliability=VerifierReliabilityMetric(
            status="computed",
            sample_count=4,
            pass_rate=0.75,
            note="pass-rate over deterministic evidence rows",
        ),
    )


# --------------------------------------------------------------------------
# is_data_starved -- the honesty predicate (boundary + error-free cases)
# --------------------------------------------------------------------------


def test_is_data_starved_true_when_no_signal_at_all() -> None:
    """An empty scorecard (no labels, stores, samples) is data-starved."""
    assert is_data_starved(_starved_scorecard()) is True


def test_is_data_starved_false_when_labels_present() -> None:
    """A scorecard with at least one output label is not starved."""
    assert is_data_starved(_populated_scorecard()) is False


def test_is_data_starved_false_when_only_store_rows_present() -> None:
    """A single store row (no labels, no samples) lifts the starved verdict."""
    scorecard = _starved_scorecard()
    scorecard.store_record_counts[StoreKind.EVIDENCE.value] = 1
    assert is_data_starved(scorecard) is False


def test_is_data_starved_false_when_only_calibration_samples_present() -> None:
    """A calibration sample alone (no labels, no store rows) is not starved."""
    scorecard = _starved_scorecard()
    scorecard.eu_calibration = EuCalibrationMetric(
        sample_count=1,
        nudged_bucket_count=0,
        max_drift_pct=10.0,
        bucket_drift=False,
        drift_badge="ok",
    )
    assert is_data_starved(scorecard) is False


# --------------------------------------------------------------------------
# Pure render helpers -- starved renders honest-negative, no fake score
# --------------------------------------------------------------------------


def test_render_overview_starved_shows_honest_negative_banner() -> None:
    """The starved overview leads with the insufficient-data banner."""
    body = render_overview(_starved_scorecard())
    assert DATA_STARVED_NOTICE in body
    # No fabricated labelled-output count on the starved path.
    assert "labelled outputs" not in body


def test_render_overview_populated_shows_window_and_label_count() -> None:
    """The populated overview reports the window and the label count."""
    body = render_overview(_populated_scorecard())
    assert "window all" in body
    assert "labelled outputs 2" in body
    assert DATA_STARVED_NOTICE not in body


def test_render_tier_counts_starved_renders_no_data_not_zeroes() -> None:
    """A starved scorecard shows the no-data sentinel, not a zero verdict row."""
    body = render_tier_counts(_starved_scorecard())
    assert NO_DATA in body
    # The honesty requirement: no fabricated tier row on no data.
    assert "verified" not in body


def test_render_tier_counts_populated_shows_each_tier_count() -> None:
    """A populated scorecard renders each tier with its count."""
    body = render_tier_counts(_populated_scorecard())
    assert "verified" in body
    assert "deferred" in body
    # The verified tier carries its real count (1).
    assert "verified[/] 1" in body


def test_render_store_counts_starved_renders_no_data() -> None:
    """All-zero store counts render the no-data sentinel."""
    assert NO_DATA in render_store_counts(_starved_scorecard())


def test_render_store_counts_populated_surfaces_each_n() -> None:
    """Populated store counts surface the per-store sample size (n)."""
    body = render_store_counts(_populated_scorecard())
    assert "evidence n=5" in body
    assert "estimate n=3" in body
    assert "actual n=2" in body


def test_render_eu_calibration_starved_renders_no_data() -> None:
    """A zero-sample calibration metric renders the no-data sentinel."""
    assert NO_DATA in render_eu_calibration(_starved_scorecard())


def test_render_eu_calibration_populated_surfaces_residual_and_n() -> None:
    """Populated calibration surfaces the drift residual and the sample size."""
    body = render_eu_calibration(_populated_scorecard())
    assert "samples 4" in body
    assert "max drift 42.5%" in body
    assert "bucket-drift" in body


def test_render_verifier_reliability_starved_shows_note_not_rate() -> None:
    """A deferred verifier metric shows its note, never a fabricated rate."""
    body = render_verifier_reliability(_starved_scorecard())
    assert "no deterministic verifier evidence" in body
    assert "pass-rate" not in body


def test_render_verifier_reliability_populated_surfaces_pass_rate_and_n() -> None:
    """A computed verifier metric surfaces the pass-rate and the sample size."""
    body = render_verifier_reliability(_populated_scorecard())
    assert "pass-rate 75%" in body
    assert "samples 4" in body


def test_render_output_labels_starved_renders_no_data() -> None:
    """An empty label list renders the no-data sentinel."""
    assert NO_DATA in render_output_labels(_starved_scorecard())


def test_render_output_labels_populated_surfaces_tier_reason_and_refs() -> None:
    """Each output label surfaces its scope, tier, reason, and evidence refs."""
    body = render_output_labels(_populated_scorecard())
    assert "P01-I01-W01" in body
    assert "verified" in body
    assert "verified by evidence record" in body
    # The evidence refs that back the tier are surfaced inline.
    assert "EV-aaaaaaaaaaaa" in body


def test_render_output_labels_caps_rows_with_overflow_count() -> None:
    """A label list past the cap renders a ``+N more`` overflow line."""
    scorecard = _populated_scorecard()
    scorecard.output_labels = [
        OutputTrustLabel(
            urn=f"urn:eawf:v1:wave:QR/P01-I01-W{index:02d}",
            scope_id=f"P01-I01-W{index:02d}",
            tier="unavailable",
            evidence_refs=[],
            reason="no verifier or attestation evidence",
        )
        for index in range(1, 20)
    ]
    body = render_output_labels(scorecard)
    assert "+7 more" in body  # 19 labels, cap 12 -> 7 overflow


# --------------------------------------------------------------------------
# Populated + starved repo fixtures for the mounted-pane Pilot tests
# --------------------------------------------------------------------------


def _project_state(*, with_closed_wave: bool) -> State:
    """Build a minimal repo state, optionally with a closed wave."""
    waves: dict[str, Any] = {}
    iters: dict[str, Any] = {}
    phases: dict[str, Any] = {}
    if with_closed_wave:
        waves["P01-I01-W01"] = Wave(
            id="P01-I01-W01",
            iter_id="P01-I01",
            title="Closed wave",
            status=WaveStatus.CLOSED,
            opened_at=_T0,
            closed_at=_T0,
        )
        iters["P01-I01"] = Iter(
            id="P01-I01",
            phase_id="P01",
            title="Closed iter",
            status=IterStatus.CLOSED,
            wave_ids=["P01-I01-W01"],
            opened_at=_T0,
            closed_at=_T0,
        )
        phases["P01"] = Phase(
            id="P01",
            scope_id="QR",
            title="Closed phase",
            status=PhaseStatus.CLOSED,
            iter_ids=["P01-I01"],
            opened_at=_T0,
            closed_at=_T0,
        )
    return State.model_validate(
        {
            "schema_version": "1.0",
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
            "phases": {pid: p.model_dump(mode="json") for pid, p in phases.items()},
            "iters": {iid: it.model_dump(mode="json") for iid, it in iters.items()},
            "waves": {wid: w.model_dump(mode="json") for wid, w in waves.items()},
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


def _append_evidence(state_path: Path, *, scope_id: str) -> None:
    """Append a deterministic-pass evidence row for *scope_id* to the store."""
    record = EvidenceRecord(
        id="EV-aaaaaaaaaaaa",
        scope_id=scope_id,
        produced_by="tool",
        evidence_kind="deterministic",
        status="pass",
        summary="pytest gate passed",
        created_at=_T0,
    )
    envelope = Envelope(
        id="EV-aaaaaaaaaaaa",
        kind=StoreKind.EVIDENCE,
        scope_id=scope_id,
        created_at=_T0,
        updated_at=_T0,
        summary=f"evidence {scope_id}",
        payload=record.model_dump(mode="json"),
    )
    path = store_path(state_path, StoreKind.EVIDENCE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        handle.write(envelope.model_dump_json().encode("utf-8") + b"\n")


# --------------------------------------------------------------------------
# Mounted pane -- end-to-end through compute_trust_scorecard
# --------------------------------------------------------------------------


def test_compute_trust_scorecard_populated_repo_is_not_starved(tmp_path: Path) -> None:
    """The populated repo fixture computes a non-starved scorecard.

    Guards the fixture itself: the Pilot populated-frame test below is only
    meaningful if ``compute_trust_scorecard`` over this repo actually
    yields a labelled, verified scorecard.
    """
    state = _project_state(with_closed_wave=True)
    state_path = _write_state(tmp_path, state)
    _append_evidence(state_path, scope_id="P01-I01-W01")
    scorecard = compute_trust_scorecard(state, state_path=state_path)
    assert is_data_starved(scorecard) is False
    assert scorecard.tier_counts.verified == 1
    assert scorecard.store_record_counts[StoreKind.EVIDENCE.value] == 1


def test_trust_pane_renders_populated_scorecard_fields(tmp_path: Path) -> None:
    """The mounted Trust pane surfaces the populated scorecard's fields."""
    state = _project_state(with_closed_wave=True)
    state_path = _write_state(tmp_path, state)
    _append_evidence(state_path, scope_id="P01-I01-W01")

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("4")  # -> trust
            await settle_screen(pilot)
            assert isinstance(app.screen, TrustModeScreen)
            assert app.screen.starved is False
            frame = normalize_snapshot(capture_screen_text(app))
            # Section headings + the populated signal are visible.
            assert "TRUST" in frame
            assert "verified" in frame  # tier count line
            assert "P01-I01-W01" in frame  # per-output label
            assert "n=1" in frame  # store sample size surfaced
            # The evidence ref that backs the verified tier survives the
            # render (Textual content markup must not swallow the id -- a
            # square-bracketed ref would parse as a markup tag and vanish).
            assert "EV-aaaaaaaaaaaa" in frame
            # The verifier residual (pass-rate over the single det. row) shows.
            assert "pass-rate" in frame
            # Honest-negative banner is absent on the populated path.
            assert DATA_STARVED_NOTICE not in frame

    asyncio.run(body())


def test_trust_pane_data_starved_renders_honest_negative(tmp_path: Path) -> None:
    """The mounted Trust pane renders the honest-negative banner, no fake score.

    The load-bearing honesty assertion: a data-starved repo (no closed
    waves, empty stores) must show "insufficient data for a trust signal"
    and must NOT show a fabricated ``verified``-tier count.
    """
    state = _project_state(with_closed_wave=False)
    state_path = _write_state(tmp_path, state)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("4")  # -> trust
            await settle_screen(pilot)
            assert isinstance(app.screen, TrustModeScreen)
            assert app.screen.starved is True
            frame = normalize_snapshot(capture_screen_text(app))
            assert DATA_STARVED_NOTICE in frame
            # No fabricated green score / tier line from no data.
            assert "verified" not in frame

    asyncio.run(body())


def test_trust_pane_starved_then_keeps_chassis_brand(tmp_path: Path) -> None:
    """Even data-starved, the Trust pane keeps the shared chassis brand row."""
    from eawf.surfaces.tui.widgets.header import BRAND

    state = _project_state(with_closed_wave=False)
    state_path = _write_state(tmp_path, state)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press("4")  # -> trust
            await settle_screen(pilot)
            frame = normalize_snapshot(capture_screen_text(app))
            # Brand outside-left of the breadcrumb; the Trust mode title trails
            # (the breadcrumb is scope > code > phase > iter > mode).
            header_row = frame.splitlines()[0]
            assert BRAND in header_row
            assert "Trust" in header_row

    asyncio.run(body())
