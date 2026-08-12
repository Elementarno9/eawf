"""Cross-session runtime rebasing.

Runtime counters are cumulative *within a session*: a wave claimed in session A
and closed in session B would otherwise difference B's counters (which start at
zero) against A's baseline, so the delta goes negative and ``_counter_delta``
raises ``LifecycleError`` -- the close fails outright. These tests pin the fix:
the first capture from a new session folds the finished session's total into
``Wave.runtime_carry`` and rebases the baseline onto the new session's origin, so
a wave spanning N sessions sums N per-session runtimes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.migrations.v1_14_to_v1_15 import MigrationV114ToV115
from eawf.kernel.migrations.v1_17_to_v1_18 import MigrationV117ToV118
from eawf.kernel.state.enums import WaveStatus
from eawf.kernel.state.models import RuntimeBaseline, RuntimeCarry, RuntimeLatest, Wave
from eawf.runtime.daemon.methods.state import (
    _counters_incomparable,
    _rebase_for_session,
    _reorigin_on_reset,
    _upsert_interactive_session_attempt,
)
from eawf.workflow.lifecycle.wave import compute_runtime_delta

_TS = datetime(2026, 7, 13, tzinfo=UTC)
_EU_MINUTES = 30.0

_CRITERION: dict[str, Any] = {
    "id": "CR-01",
    "text": "the wave records a captured runtime at close",
    "kind": "legacy",
    "acceptance_style": "binary",
    "evidence_kind": "attested",
    "quality_dimension": "functional_suitability",
    "measurable_signal": "the wave records a captured runtime at close",
}


def _wave(**overrides: Any) -> Wave:
    payload: dict[str, Any] = {
        "id": "P00-I01-W01",
        "iter_id": "P00-I01",
        "title": "Wave one",
        "status": WaveStatus.CLAIMED.value,
        "success_criteria": [_CRITERION],
        "opened_at": _TS,
    }
    payload.update(overrides)
    return Wave.model_validate(payload)


def _baseline(session_id: str | None, **counters: Any) -> RuntimeBaseline:
    return RuntimeBaseline(session_id=session_id, captured_at=_TS, **counters)


def _latest(session_id: str | None, **counters: Any) -> RuntimeLatest:
    return RuntimeLatest(session_id=session_id, captured_at=_TS, **counters)


def test_same_session_capture_does_not_rebase() -> None:
    wave = _wave(
        runtime_baseline=_baseline("sess-a", api_duration_ms=1_000),
        runtime_latest=_latest("sess-a", api_duration_ms=5_000),
    )

    _rebase_for_session(wave, _latest("sess-a"), "sess-a")

    assert wave.runtime_carry is None
    assert wave.runtime_baseline is not None
    assert wave.runtime_baseline.api_duration_ms == 1_000
    assert wave.runtime_latest is not None


def test_new_session_folds_prior_total_and_rebases() -> None:
    wave = _wave(
        runtime_baseline=_baseline(
            "sess-a", api_duration_ms=1_000, total_duration_ms=1_000, input_tokens=100
        ),
        runtime_latest=_latest(
            "sess-a", api_duration_ms=5_000, total_duration_ms=5_000, input_tokens=400
        ),
    )

    _rebase_for_session(wave, _latest("sess-b"), "sess-b")

    assert wave.runtime_carry is not None
    # Session A spent 4_000 ms and 300 input tokens; that total is carried.
    assert wave.runtime_carry.api_duration_ms == 4_000
    assert wave.runtime_carry.input_tokens == 300
    assert wave.runtime_carry.sessions_folded == 1
    # The baseline is now session B's origin -- B's counters as of this capture,
    # NOT zero (a zero origin would charge the wave for everything B had already
    # done before the wave was resumed, and would double-count B on a return).
    assert wave.runtime_baseline is not None
    assert wave.runtime_baseline.session_id == "sess-b"
    assert wave.runtime_baseline.api_duration_ms == 0  # this stub capture is empty
    assert wave.runtime_latest is None


def test_cross_session_close_sums_both_sessions_instead_of_raising() -> None:
    wave = _wave(
        runtime_baseline=_baseline("sess-a", api_duration_ms=1_000, input_tokens=100),
        runtime_latest=_latest("sess-a", api_duration_ms=5_000, input_tokens=400),
    )

    # Session B starts fresh: its cumulative counters begin near zero. Without
    # the rebase this is a backwards counter and the close raises.
    _rebase_for_session(wave, _latest("sess-b"), "sess-b")
    wave.runtime_latest = _latest("sess-b", api_duration_ms=2_000, input_tokens=50)

    delta = compute_runtime_delta(
        wave.runtime_baseline,
        wave.runtime_latest,
        carry=wave.runtime_carry,
        eu_minutes=_EU_MINUTES,
    )

    assert delta is not None
    # 4_000 ms carried from session A + 2_000 ms from session B.
    assert delta.api_duration_ms == 6_000
    assert delta.actual_tokens == 350
    assert delta.elapsed_eu == pytest.approx(6_000 / (_EU_MINUTES * 60_000.0))


def test_second_capture_in_the_new_session_keeps_the_carry() -> None:
    wave = _wave(
        runtime_baseline=_baseline("sess-a", api_duration_ms=1_000),
        runtime_latest=_latest("sess-a", api_duration_ms=5_000),
    )
    _rebase_for_session(wave, _latest("sess-b"), "sess-b")
    wave.runtime_latest = _latest("sess-b", api_duration_ms=2_000)

    # A later Stop in the SAME session must not fold again -- the carry is a
    # per-finished-session accumulator, not a per-capture one.
    _rebase_for_session(wave, _latest("sess-b"), "sess-b")
    wave.runtime_latest = _latest("sess-b", api_duration_ms=3_000)

    delta = compute_runtime_delta(
        wave.runtime_baseline,
        wave.runtime_latest,
        carry=wave.runtime_carry,
        eu_minutes=_EU_MINUTES,
    )

    assert wave.runtime_carry is not None
    assert wave.runtime_carry.sessions_folded == 1
    assert delta is not None
    assert delta.api_duration_ms == 7_000


def test_three_sessions_sum() -> None:
    wave = _wave(runtime_baseline=_baseline("sess-a"), runtime_latest=None)
    wave.runtime_latest = _latest("sess-a", api_duration_ms=1_000)

    _rebase_for_session(wave, _latest("sess-b"), "sess-b")
    wave.runtime_latest = _latest("sess-b", api_duration_ms=2_000)

    _rebase_for_session(wave, _latest("sess-c"), "sess-c")
    wave.runtime_latest = _latest("sess-c", api_duration_ms=4_000)

    delta = compute_runtime_delta(
        wave.runtime_baseline,
        wave.runtime_latest,
        carry=wave.runtime_carry,
        eu_minutes=_EU_MINUTES,
    )

    assert wave.runtime_carry is not None
    assert wave.runtime_carry.sessions_folded == 2
    assert delta is not None
    assert delta.api_duration_ms == 7_000


def test_unstamped_baseline_adopts_the_capturing_session() -> None:
    # A wave claimed before the v1.15 session stamp, with nothing captured yet.
    wave = _wave(runtime_baseline=_baseline(None, api_duration_ms=1_000))

    _rebase_for_session(wave, _latest("sess-a"), "sess-a")

    assert wave.runtime_carry is None
    assert wave.runtime_baseline is not None
    assert wave.runtime_baseline.session_id == "sess-a"
    assert wave.runtime_baseline.api_duration_ms == 1_000


def test_capture_without_a_session_id_leaves_snapshots_alone() -> None:
    wave = _wave(
        runtime_baseline=_baseline("sess-a", api_duration_ms=1_000),
        runtime_latest=_latest("sess-a", api_duration_ms=5_000),
    )

    _rebase_for_session(wave, _latest(None), None)

    assert wave.runtime_carry is None
    assert wave.runtime_baseline is not None
    assert wave.runtime_baseline.session_id == "sess-a"


def test_regressed_session_contributes_no_negative_carry() -> None:
    # A counter source that reset mid-session: latest < baseline. The fold clamps
    # at zero rather than carrying a negative total into the next session.
    wave = _wave(
        runtime_baseline=_baseline("sess-a", api_duration_ms=5_000),
        runtime_latest=_latest("sess-a", api_duration_ms=1_000),
    )

    _rebase_for_session(wave, _latest("sess-b"), "sess-b")

    assert wave.runtime_carry is not None
    assert wave.runtime_carry.api_duration_ms == 0


def test_each_session_is_folded_at_its_own_concurrency() -> None:
    """W46: the carry holds the wave's SHARE of each finished session.

    Session A was shared by four waves, session B by none. Folding A's raw total
    and dividing it later by B's concurrency would split each session by whatever
    concurrency the wave happened to END under -- so the division happens as the
    session's total is finalised, at the concurrency that session actually had.
    """
    wave = _wave(
        runtime_baseline=_baseline("sess-a", api_duration_ms=1_000, shared_wave_count=4),
        runtime_latest=_latest("sess-a", api_duration_ms=9_000, shared_wave_count=4),
    )

    _rebase_for_session(wave, _latest("sess-b", shared_wave_count=1), "sess-b")

    assert wave.runtime_carry is not None
    # Session A spent 8_000 ms across four waves; this wave's share is 2_000.
    assert wave.runtime_carry.api_duration_ms == 2_000
    assert wave.runtime_carry.sessions_folded == 1


def test_a_shared_session_delta_is_the_waves_share_not_the_whole(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The close-time delta divides by the concurrency the counters were captured under."""
    del caplog
    baseline = _baseline("sess-a", api_duration_ms=1_000, cost_usd=1.0, shared_wave_count=2)
    latest = _latest("sess-a", api_duration_ms=5_000, cost_usd=3.0, shared_wave_count=2)

    delta = compute_runtime_delta(baseline, latest, eu_minutes=_EU_MINUTES)

    assert delta is not None
    assert delta.shared_wave_count == 2
    assert delta.api_duration_ms == 2_000
    assert delta.actual_cost_usd == pytest.approx(1.0)


def test_carry_defaults_are_the_delta_identity() -> None:
    baseline = _baseline("sess-a", api_duration_ms=1_000)
    latest = _latest("sess-a", api_duration_ms=3_000)

    with_carry = compute_runtime_delta(
        baseline, latest, carry=RuntimeCarry(), eu_minutes=_EU_MINUTES
    )
    without_carry = compute_runtime_delta(baseline, latest, eu_minutes=_EU_MINUTES)

    assert with_carry == without_carry


def test_carry_alone_survives_a_session_that_captured_nothing() -> None:
    # Session B closed the wave without capturing a duration of its own; the
    # runtime session A spent is still the wave's measured effort.
    baseline = _baseline("sess-b", api_duration_ms=0)
    latest = _latest("sess-b")
    carry = RuntimeCarry(api_duration_ms=4_000, input_tokens=300, sessions_folded=1)

    delta = compute_runtime_delta(baseline, latest, carry=carry, eu_minutes=_EU_MINUTES)

    assert delta is not None
    assert delta.api_duration_ms == 4_000
    assert delta.elapsed_eu > 0.0


# --- v1.14 -> v1.15 migration ---------------------------------------------


def _state_v1_14(*, with_snapshots: bool) -> dict[str, Any]:
    wave: dict[str, Any] = {
        "id": "P00-I01-W01",
        "iter_id": "P00-I01",
        "title": "Wave one",
        "status": WaveStatus.PENDING.value,
        "success_criteria": [_CRITERION],
        "opened_at": "2026-07-13T00:00:00Z",
    }
    if with_snapshots:
        wave["runtime_baseline"] = {"api_duration_ms": 10, "captured_at": "2026-07-13T00:00:00Z"}
        wave["runtime_latest"] = {"api_duration_ms": 90, "captured_at": "2026-07-13T00:00:00Z"}
    return {"schema_version": "1.14", "waves": {"P00-I01-W01": wave}}


def test_migration_backfills_session_id_and_carry() -> None:
    out = MigrationV114ToV115().apply(_state_v1_14(with_snapshots=True))

    assert out["schema_version"] == "1.15"
    wave = out["waves"]["P00-I01-W01"]
    assert wave["runtime_baseline"]["session_id"] is None
    assert wave["runtime_latest"]["session_id"] is None
    assert wave["runtime_carry"] is None
    # The historical counters are untouched.
    assert wave["runtime_baseline"]["api_duration_ms"] == 10


def test_migration_handles_waves_without_snapshots() -> None:
    out = MigrationV114ToV115().apply(_state_v1_14(with_snapshots=False))

    assert out["waves"]["P00-I01-W01"]["runtime_carry"] is None


def test_migration_does_not_mutate_input() -> None:
    src = _state_v1_14(with_snapshots=True)
    out = MigrationV114ToV115().apply(src)

    assert out["schema_version"] == "1.15"
    assert src["schema_version"] == "1.14"
    assert "runtime_carry" not in src["waves"]["P00-I01-W01"]


def test_migration_pre_post_version_guards() -> None:
    step = MigrationV114ToV115()
    with pytest.raises(Exception, match="schema_version"):
        step.check_pre({"schema_version": "1.13"})
    with pytest.raises(Exception, match="schema_version"):
        step.check_post({"schema_version": "1.14"})


# --- interactive attempt row: wave delta, not session totals ---------------


def test_interactive_attempt_carries_the_wave_delta_not_session_totals() -> None:
    """The attempt row is per-wave: the snapshot it comes from is per-session.

    Stamping the cumulative snapshot verbatim charged every active wave with the
    whole session's spend and left a zero-length span, which the wave-detail
    metrics tab (EU derived from attempt spans) rendered as 0.00 EU.
    """
    claimed_at = datetime(2026, 7, 13, 1, 0, tzinfo=UTC)
    captured_at = datetime(2026, 7, 13, 1, 30, tzinfo=UTC)
    wave = _wave(
        runtime_baseline=RuntimeBaseline(
            session_id="sess-a",
            api_duration_ms=600_000,
            cost_usd=20.0,
            input_tokens=300,
            output_tokens=90_000,
            cache_creation_input_tokens=200_000,
            cache_read_input_tokens=30_000_000,
            captured_at=claimed_at,
        )
    )
    latest = RuntimeLatest(
        session_id="sess-a",
        api_duration_ms=2_400_000,
        cost_usd=25.0,
        input_tokens=350,
        output_tokens=100_000,
        cache_creation_input_tokens=210_000,
        cache_read_input_tokens=34_000_000,
        captured_at=captured_at,
    )
    wave.runtime_latest = latest

    _upsert_interactive_session_attempt(wave, latest=latest, session_id="sess-a")

    attempt = wave.sessions[1]
    # The wave's own spend, not the session's $25 / 34M cumulative totals.
    assert attempt.cost_usd == pytest.approx(5.0)
    assert attempt.output_tokens == 10_000
    assert attempt.cache_read_input_tokens == 4_000_000
    # A real span: claim -> capture, so the detail tab derives nonzero EU.
    assert attempt.started_at == claimed_at
    assert attempt.ended_at == captured_at
    assert attempt.ended_at > attempt.started_at


def test_second_wave_in_the_same_session_records_only_its_own_delta() -> None:
    """A wave claimed later in a session must not inherit the earlier wave's spend."""
    first_claim = datetime(2026, 7, 13, 1, 0, tzinfo=UTC)
    second_claim = datetime(2026, 7, 13, 1, 20, tzinfo=UTC)
    captured_at = datetime(2026, 7, 13, 1, 30, tzinfo=UTC)

    def _wave_claimed_at(wave_id: str, when: datetime, *, output_tokens: int) -> Wave:
        return _wave(
            id=wave_id,
            runtime_baseline=RuntimeBaseline(
                session_id="sess-a",
                api_duration_ms=0,
                cost_usd=1.0,
                output_tokens=output_tokens,
                captured_at=when,
            ),
        )

    latest = RuntimeLatest(
        session_id="sess-a",
        api_duration_ms=1_800_000,
        cost_usd=9.0,
        output_tokens=100_000,
        captured_at=captured_at,
    )
    early = _wave_claimed_at("P00-I01-W01", first_claim, output_tokens=10_000)
    late = _wave_claimed_at("P00-I01-W02", second_claim, output_tokens=80_000)
    for wave in (early, late):
        wave.runtime_latest = latest
        _upsert_interactive_session_attempt(wave, latest=latest, session_id="sess-a")

    # The late wave saw 20k output tokens of work, not the session's 100k.
    assert early.sessions[1].output_tokens == 90_000
    assert late.sessions[1].output_tokens == 20_000
    assert late.sessions[1].started_at == second_claim


def test_interactive_attempt_is_a_no_op_without_a_baseline() -> None:
    wave = _wave(runtime_baseline=None)
    latest = _latest("sess-a", api_duration_ms=1_000, cost_usd=2.0)

    _upsert_interactive_session_attempt(wave, latest=latest, session_id="sess-a")

    # No baseline means no wave-scoped delta to record; the snapshot stays the
    # only record rather than the attempt claiming the whole session.
    assert wave.sessions == {}


# --- P30-I25-W36: the rebase must not double-count or lose runtime ----------


def test_returning_to_an_earlier_session_does_not_double_count_it() -> None:
    """Sessions interleave. A -> B -> A must charge A's early work exactly once.

    The shipped rebase re-originated on ZERO, so returning to session A made the
    next delta A's ENTIRE cumulative -- including the work already folded into the
    carry when A was first left. Every alternation charged it again, without
    bound. (Found by the P30-I25 iter audit; the happy-path tests never
    interleaved.)
    """
    wave = _wave(
        runtime_baseline=_baseline("sess-a", api_duration_ms=0),
        runtime_latest=_latest("sess-a", api_duration_ms=1_000),
    )

    # Leave A for B: A's 1_000 ms is folded.
    _rebase_for_session(wave, _latest("sess-b", api_duration_ms=0), "sess-b")
    wave.runtime_latest = _latest("sess-b", api_duration_ms=500)

    # Back to A, whose cumulative counter has kept growing (it is one session's
    # transcript): 1_000 already folded, now 1_200 total.
    _rebase_for_session(wave, _latest("sess-a", api_duration_ms=1_200), "sess-a")
    wave.runtime_latest = _latest("sess-a", api_duration_ms=1_200)

    delta = compute_runtime_delta(
        wave.runtime_baseline,
        wave.runtime_latest,
        carry=wave.runtime_carry,
        eu_minutes=_EU_MINUTES,
    )

    assert delta is not None
    # A's 1_000 (folded) + B's 500 (folded) + A's new work since the return (0).
    # The old zero-origin code returned 2_700 -- A's 1_200 counted a second time.
    assert delta.api_duration_ms == 1_500


def test_new_session_origin_excludes_work_the_wave_did_not_do() -> None:
    """A session's pre-resume work must not be charged to the wave it resumes.

    Session B's counters cover everything the operator did in B. A zero origin
    charged the wave for all of it; the origin is B's counters at the moment the
    wave is picked up again.
    """
    wave = _wave(
        runtime_baseline=_baseline("sess-a", api_duration_ms=0),
        runtime_latest=_latest("sess-a", api_duration_ms=1_000),
    )

    # The operator opens B and spends 10 minutes on something else entirely,
    # THEN the wave's first capture in B fires.
    _rebase_for_session(wave, _latest("sess-b", api_duration_ms=600_000), "sess-b")
    wave.runtime_latest = _latest("sess-b", api_duration_ms=660_000)

    delta = compute_runtime_delta(
        wave.runtime_baseline,
        wave.runtime_latest,
        carry=wave.runtime_carry,
        eu_minutes=_EU_MINUTES,
    )

    assert delta is not None
    # A's 1_000 ms + the 60_000 ms B spent on the wave -- not B's other 600_000.
    assert delta.api_duration_ms == 61_000


def test_a_session_that_captured_nothing_is_not_counted_as_folded() -> None:
    """Folding nothing must not claim a session's runtime was accounted for."""
    wave = _wave(runtime_baseline=_baseline("sess-a", api_duration_ms=5_000))

    _rebase_for_session(wave, _latest("sess-b", api_duration_ms=0), "sess-b")

    assert wave.runtime_carry is not None
    # Nothing was ever captured against session A, so nothing is folded -- and the
    # count must not pretend otherwise.
    assert wave.runtime_carry.api_duration_ms == 0
    assert wave.runtime_carry.sessions_folded == 0


# --- P30-I25-W37: a backwards counter must never strand a wave --------------


def test_regressed_counters_reorigin_instead_of_stranding_the_wave() -> None:
    """A counter that goes backwards in the SAME session is a source reset.

    P30-I25 hit this for real: W34 changed the duration basis (wall clock ->
    agent work time) while W35 and W36 were CLAIMED against baselines recorded
    under the old basis. Their next capture read ~18M ms against a ~66M ms
    baseline -- a backwards counter, which the close path raised on. The baseline
    lives on disk, so every retry raised again: the waves were unclosable forever,
    by their own fix.
    """
    wave = _wave(
        runtime_baseline=_baseline("sess-a", api_duration_ms=66_000_000, output_tokens=250_000),
    )
    # The basis changes under the wave: the same session now measures far less.
    incoming = _latest("sess-a", api_duration_ms=18_000_000, output_tokens=250_000)

    assert _counters_incomparable(wave.runtime_baseline, incoming) is True
    _reorigin_on_reset(wave, incoming)

    assert wave.runtime_baseline is not None
    # The origin moves to the regressed counters; the wave keeps measuring forward.
    assert wave.runtime_baseline.api_duration_ms == 18_000_000
    assert wave.runtime_latest is None


def test_a_wave_stranded_by_a_basis_change_still_closes() -> None:
    """The close must degrade to a zero delta, never raise and strand the wave."""
    wave = _wave(
        runtime_baseline=_baseline("sess-a", api_duration_ms=66_000_000),
        runtime_latest=_latest("sess-a", api_duration_ms=18_000_000),
    )

    delta = compute_runtime_delta(
        wave.runtime_baseline,
        wave.runtime_latest,
        carry=wave.runtime_carry,
        eu_minutes=_EU_MINUTES,
    )

    # A bad measurement (zero runtime recorded) beats a broken workflow (a wave
    # that can never be closed by any means).
    assert delta is not None
    assert delta.api_duration_ms == 0
    assert delta.elapsed_eu == 0.0


def test_forward_counters_are_untouched_by_the_reset_guard() -> None:
    baseline = _baseline("sess-a", api_duration_ms=1_000, output_tokens=10)
    incoming = _latest("sess-a", api_duration_ms=5_000, output_tokens=40)

    assert _counters_incomparable(baseline, incoming) is False


# --- P30-I25-W42: a changed measure is not work, in EITHER direction --------


def test_a_rising_measure_change_is_not_banked_as_runtime() -> None:
    """A redefinition that RAISES the figure looks exactly like a productive week.

    This is the half the direction heuristic misses. When the duration measure
    changed from a gap heuristic to per-turn spans, three claimed waves' baselines
    (22,600,279 ms under the old measure) met a capture of 69,676,393 ms under the
    new one. Nothing went backwards, so nothing tripped -- and the delta was 13
    HOURS of fabricated runtime, about 26 EU per wave, silently.

    The snapshots carry the measure that produced them, so the change is a fact
    rather than an inference from which way the number moved.
    """
    baseline = _baseline("sess-a", api_duration_ms=22_600_279)
    baseline = baseline.model_copy(update={"measure_version": 2})
    incoming = _latest("sess-a", api_duration_ms=69_676_393)
    incoming = incoming.model_copy(update={"measure_version": 3})

    # The counters ROSE -- a regression check sees nothing wrong at all.
    assert incoming.api_duration_ms > baseline.api_duration_ms
    assert _counters_incomparable(baseline, incoming) is True


def test_a_falling_measure_change_is_still_caught() -> None:
    baseline = _baseline("sess-a", api_duration_ms=65_988_667).model_copy(
        update={"measure_version": 1}
    )
    incoming = _latest("sess-a", api_duration_ms=19_284_744).model_copy(
        update={"measure_version": 3}
    )

    assert _counters_incomparable(baseline, incoming) is True


def test_the_same_measure_growing_is_ordinary_work() -> None:
    """The guard must not fire on a wave that simply did some work."""
    baseline = _baseline("sess-a", api_duration_ms=1_000).model_copy(update={"measure_version": 3})
    incoming = _latest("sess-a", api_duration_ms=5_000).model_copy(update={"measure_version": 3})

    assert _counters_incomparable(baseline, incoming) is False


def test_an_unversioned_baseline_is_not_a_matching_one() -> None:
    """An unversioned baseline came from SOME earlier measure -- which one is unknown.

    Treating unknown as "same" is exactly what let the gap-heuristic baselines
    survive the turn-span change: they carried no version, the new capture carried
    3, the numbers rose, and 13 hours per wave was banked as work. Unknown is the
    one thing that is definitely not known to match.
    """
    baseline = _baseline("sess-a", api_duration_ms=22_600_279)
    incoming = _latest("sess-a", api_duration_ms=69_676_393).model_copy(
        update={"measure_version": 3}
    )

    assert baseline.measure_version is None
    assert incoming.api_duration_ms > baseline.api_duration_ms  # nothing "went backwards"
    assert _counters_incomparable(baseline, incoming) is True


def test_two_unversioned_snapshots_fall_back_to_the_numbers() -> None:
    """The statusline path declares no measure at all -- compare as before."""
    baseline = _baseline("sess-a", api_duration_ms=1_000)
    forward = _latest("sess-a", api_duration_ms=5_000)
    backward = _latest("sess-a", api_duration_ms=500)

    assert _counters_incomparable(baseline, forward) is False
    assert _counters_incomparable(baseline, backward) is True


def test_a_flip_between_counter_sources_is_a_known_change() -> None:
    """Each writer declares its own measure, so a source flip is identified.

    The transcript aggregator, the statusline parser, and the headless spawn all
    measure different quantities. When the transcript is unreadable the runner falls
    back to the statusline -- and if that snapshot declared NO measure, the flip
    looked like an unknown source: it re-originated the wave, recorded a reset, and
    the reset then excused a zero-EU close. Two repairs composing into a way to
    launder a silent capture failure into a clean one.
    """
    from eawf.runtime.runtimes.claude.runtime_counters import STATUSLINE_MEASURE_VERSION
    from eawf.runtime.runtimes.claude.transcript_counters import MEASURE_VERSION

    assert STATUSLINE_MEASURE_VERSION != MEASURE_VERSION

    transcript_baseline = _baseline("sess-a", api_duration_ms=1_000).model_copy(
        update={"measure_version": MEASURE_VERSION}
    )
    statusline_capture = _latest("sess-a", api_duration_ms=9_000).model_copy(
        update={"measure_version": STATUSLINE_MEASURE_VERSION}
    )

    # The numbers ROSE, so nothing looks wrong -- but they measure different things.
    assert _counters_incomparable(transcript_baseline, statusline_capture) is True


# --- v1.17 -> v1.18 migration ----------------------------------------------


def _state_v1_17(*, with_snapshots: bool) -> dict[str, Any]:
    wave: dict[str, Any] = {
        "id": "P00-I01-W01",
        "iter_id": "P00-I01",
        "title": "Wave one",
        "status": WaveStatus.PENDING.value,
        "success_criteria": [_CRITERION],
        "opened_at": "2026-07-13T00:00:00Z",
    }
    if with_snapshots:
        wave["runtime_baseline"] = {"api_duration_ms": 10, "captured_at": "2026-07-13T00:00:00Z"}
        wave["runtime_latest"] = {"api_duration_ms": 90, "captured_at": "2026-07-13T00:00:00Z"}
    return {"schema_version": "1.17", "waves": {"P00-I01-W01": wave}}


def test_migration_backfills_shared_wave_count() -> None:
    out = MigrationV117ToV118().apply(_state_v1_17(with_snapshots=True))

    assert out["schema_version"] == "1.18"
    wave = out["waves"]["P00-I01-W01"]
    # A null count reads as a divisor of one -- what the delta effectively used
    # before the field existed, so no historical figure changes.
    assert wave["runtime_baseline"]["shared_wave_count"] is None
    assert wave["runtime_latest"]["shared_wave_count"] is None


def test_migration_v118_handles_waves_without_snapshots() -> None:
    out = MigrationV117ToV118().apply(_state_v1_17(with_snapshots=False))

    assert out["schema_version"] == "1.18"
    assert "runtime_baseline" not in out["waves"]["P00-I01-W01"]


def test_migration_v118_does_not_mutate_input() -> None:
    payload = _state_v1_17(with_snapshots=True)

    MigrationV117ToV118().apply(payload)

    assert payload["schema_version"] == "1.17"
    assert "shared_wave_count" not in payload["waves"]["P00-I01-W01"]["runtime_baseline"]


def test_migration_v118_pre_post_version_guards() -> None:
    step = MigrationV117ToV118()
    payload = _state_v1_17(with_snapshots=False)

    step.check_pre(payload)
    step.check_post(step.apply(payload))

    with pytest.raises(ValidationError):
        step.check_pre({"schema_version": "1.16"})


def test_a_session_that_never_captured_says_its_runtime_was_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """W48: a dropped session's runtime is announced, not swallowed.

    A session that ended without ever capturing has nothing to fold, so the carry
    is right to stay empty -- but the wave really did spend runtime in it, and
    that runtime is now gone. Dropping it in silence is exactly how a capture path
    that DIED passes for one that had nothing to do, which is the confusion this
    whole iter exists to make impossible.
    """
    wave = _wave(
        runtime_baseline=_baseline("sess-a", api_duration_ms=1_000),
        runtime_latest=None,
    )

    with caplog.at_level("WARNING"):
        _rebase_for_session(wave, _latest("sess-b"), "sess-b")

    assert wave.runtime_carry is not None
    assert wave.runtime_carry.sessions_folded == 0
    assert "never-captured" in caplog.text
    assert "dropped" in caplog.text
