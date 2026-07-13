"""Cross-session runtime rebasing (P30-I25-W27).

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

from eawf.kernel.migrations.v1_14_to_v1_15 import MigrationV114ToV115
from eawf.kernel.state.enums import WaveStatus
from eawf.kernel.state.models import RuntimeBaseline, RuntimeCarry, RuntimeLatest, Wave
from eawf.runtime.daemon.methods.state import _rebase_for_session
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

    _rebase_for_session(wave, "sess-a")

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

    _rebase_for_session(wave, "sess-b")

    assert wave.runtime_carry is not None
    # Session A spent 4_000 ms and 300 input tokens; that total is carried.
    assert wave.runtime_carry.api_duration_ms == 4_000
    assert wave.runtime_carry.input_tokens == 300
    assert wave.runtime_carry.sessions_folded == 1
    # The baseline is now session B's zero origin, and B has captured nothing yet.
    assert wave.runtime_baseline is not None
    assert wave.runtime_baseline.session_id == "sess-b"
    assert wave.runtime_baseline.api_duration_ms == 0
    assert wave.runtime_latest is None


def test_cross_session_close_sums_both_sessions_instead_of_raising() -> None:
    wave = _wave(
        runtime_baseline=_baseline("sess-a", api_duration_ms=1_000, input_tokens=100),
        runtime_latest=_latest("sess-a", api_duration_ms=5_000, input_tokens=400),
    )

    # Session B starts fresh: its cumulative counters begin near zero. Without
    # the rebase this is a backwards counter and the close raises.
    _rebase_for_session(wave, "sess-b")
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
    _rebase_for_session(wave, "sess-b")
    wave.runtime_latest = _latest("sess-b", api_duration_ms=2_000)

    # A later Stop in the SAME session must not fold again -- the carry is a
    # per-finished-session accumulator, not a per-capture one.
    _rebase_for_session(wave, "sess-b")
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

    _rebase_for_session(wave, "sess-b")
    wave.runtime_latest = _latest("sess-b", api_duration_ms=2_000)

    _rebase_for_session(wave, "sess-c")
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

    _rebase_for_session(wave, "sess-a")

    assert wave.runtime_carry is None
    assert wave.runtime_baseline is not None
    assert wave.runtime_baseline.session_id == "sess-a"
    assert wave.runtime_baseline.api_duration_ms == 1_000


def test_capture_without_a_session_id_leaves_snapshots_alone() -> None:
    wave = _wave(
        runtime_baseline=_baseline("sess-a", api_duration_ms=1_000),
        runtime_latest=_latest("sess-a", api_duration_ms=5_000),
    )

    _rebase_for_session(wave, None)

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

    _rebase_for_session(wave, "sess-b")

    assert wave.runtime_carry is not None
    assert wave.runtime_carry.api_duration_ms == 0


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
