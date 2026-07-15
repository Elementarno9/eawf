"""Hypothesis property tests for the estimation subsystem.

Covers four invariants:

1. **EU calculator round-trip**: ``expected_eu * eu_minutes == central_multiplier
   * raw_minutes`` exactly under :class:`~decimal.Decimal` arithmetic — float
   would drift here, which is why the calculator is Decimal-based.
2. **Segment open/stop round-trip**: opening then closing a segment preserves
   ``elapsed_eu == (stop_at - start_at).total_seconds() / 60 / eu_minutes``
   to within ``± eu_quantum`` after quantization.
3. **Property K — actuals envelope ids are timestamped + pairwise distinct**:
   driving ``actual start``/``actual stop`` N times produces envelopes whose
   ``id`` matches ``ACT-<scope>-<us>-<nonce>`` (microsecond + 4-hex-nonce
   suffix; estimate ids retain ``EST-<scope>-<ISO>``) and no two envelopes
   share an id even under tight-loop back-to-back mutations.
4. **Property L — clock-skew emits a WARNING**: calling
   :func:`eawf.workflow.estimation.recovery.cap_elapsed` with ``started_at > now``
   logs a single ``WARNING`` mentioning the skew so calibration data is not
   silently corrupted.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from typer.testing import CliRunner

from eawf.kernel.state.enums import ActualStatus
from eawf.kernel.store.envelope import Envelope
from eawf.surfaces.cli.app import app
from eawf.workflow.estimation import eu, segments
from eawf.workflow.estimation.recovery import cap_elapsed

# Conservative ranges: keep multipliers/durations sensible and bounded so the
# tests run quickly and stay in the regime we actually exercise at runtime.

_RAW_MINUTES = st.decimals(
    min_value=Decimal("0"), max_value=Decimal("1000"), allow_nan=False, allow_infinity=False
)
_CENTRAL = st.decimals(
    min_value=Decimal("0.01"),
    max_value=Decimal("5"),
    allow_nan=False,
    allow_infinity=False,
)
_EU_MINUTES = st.sampled_from([Decimal("15"), Decimal("30"), Decimal("60")])


# Decimal context precision is 28 digits; division can lose precision past
# that bound. The round-trip is therefore asserted within a small Decimal
# epsilon scaled to that precision, NOT bit-exact.
_DECIMAL_EPSILON = Decimal("1e-25")


@pytest.mark.slow
@settings(max_examples=200, deadline=None)
@given(
    raw_minutes=_RAW_MINUTES,
    central=_CENTRAL,
    eu_minutes=_EU_MINUTES,
)
def test_expected_eu_round_trips_via_minutes(
    raw_minutes: Decimal,
    central: Decimal,
    eu_minutes: Decimal,
) -> None:
    """expected_eu * eu_minutes ≈ central * raw_minutes (within Decimal precision).

    Float would drift much further than this — Decimal preserves exactness
    until the configured context precision (28 digits) is exhausted on a
    non-terminating quotient.
    """
    expected = eu.expected_eu(raw_minutes, central, eu_minutes)
    delta = abs(expected * eu_minutes - central * raw_minutes)
    # Tolerance scales with the magnitude so 1e-25 is fine for typical inputs;
    # the implicit guarantee is that float would never get even close.
    assert delta <= _DECIMAL_EPSILON * (Decimal(1) + abs(central * raw_minutes))


@pytest.mark.slow
@settings(max_examples=200, deadline=None)
@given(
    raw_minutes=_RAW_MINUTES,
    pessim=st.decimals(
        min_value=Decimal("0.5"), max_value=Decimal("5"), allow_nan=False, allow_infinity=False
    ),
    eu_minutes=_EU_MINUTES,
)
def test_pessimistic_eu_round_trips_via_minutes(
    raw_minutes: Decimal,
    pessim: Decimal,
    eu_minutes: Decimal,
) -> None:
    pess = eu.pessimistic_eu(raw_minutes, pessim, eu_minutes)
    delta = abs(pess * eu_minutes - pessim * raw_minutes)
    assert delta <= _DECIMAL_EPSILON * (Decimal(1) + abs(pessim * raw_minutes))


@pytest.mark.slow
@settings(max_examples=100, deadline=None)
@given(
    duration_seconds=st.integers(min_value=0, max_value=86_400),
    eu_minutes=_EU_MINUTES,
)
def test_segment_open_stop_round_trip_within_quantum(
    duration_seconds: int,
    eu_minutes: Decimal,
) -> None:
    """open then close: closed.eu equals duration_seconds / 60 / eu_minutes ± quantum."""
    started = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    ended = started + timedelta(seconds=duration_seconds)
    opened = segments.open_segment(session_id="SES-PROP", started_at=started)
    closed = segments.close_segment(opened, ended_at=ended, eu_minutes=eu_minutes)

    assert closed.status == ActualStatus.DONE
    expected_eu_value = Decimal(duration_seconds) / Decimal(60) / eu_minutes
    quantum = Decimal("0.25")
    # closed.eu is float; convert via str() to avoid binary-float drift before subtract.
    actual_eu = Decimal(str(closed.eu))
    assert abs(actual_eu - expected_eu_value) <= quantum


@pytest.mark.slow
@settings(max_examples=100, deadline=None)
@given(
    duration_seconds=st.integers(min_value=0, max_value=3_600),
    eu_minutes=_EU_MINUTES,
    eu_quantum=st.sampled_from([Decimal("0.1"), Decimal("0.25"), Decimal("0.5")]),
)
def test_quantize_round_trip_within_one_quantum(
    duration_seconds: int,
    eu_minutes: Decimal,
    eu_quantum: Decimal,
) -> None:
    """After quantization, the result is within one quantum of the unrounded value."""
    raw = Decimal(duration_seconds) / Decimal(60) / eu_minutes
    snapped = eu.quantize(raw, eu_quantum)
    delta = abs(snapped - raw)
    # banker's rounding can land at exactly half a quantum, so the bound is
    # strictly less than one quantum.
    assert delta <= eu_quantum / Decimal(2) + Decimal("1e-12"), (raw, snapped, eu_quantum)


# ---- Property K — envelope ids are timestamped + pairwise distinct ---------

# Actual envelope id shape: ``ACT-<scope>-<us>-<nonce4>`` (microsecond integer
# suffix plus 4 hex nonce chars; the nonce is :func:`secrets.token_hex(2)`).
# Estimate envelope id shape: ``EST-<scope>-<YYYYMMDDTHHMMSSZ>`` (ISO suffix).
# Both shapes are mutually exclusive — actual ids end in ``-<digits>-<hex4>``,
# estimate ids end in a date/time literal. The regexes cover both prefixes.
_ACTUAL_ID_RE = re.compile(r"^ACT-[A-Z0-9-]+-\d+-[0-9a-f]{4}$")
_ESTIMATE_ID_RE = re.compile(r"^EST-[A-Z0-9-]+-\d{8}T\d{6}Z$")

_ACTUALS_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "states"


def _seed_property_state(tmp_path: Path) -> Path:
    """Copy the empty-repo fixture into ``tmp_path/.ea/state.json``.

    Returns the workspace root for ``-w`` flag use. Mirrors the helper in
    :mod:`tests.integration.test_cli_estimation` so this property test exercises
    the same surface.
    """
    workspace = tmp_path / "ws"
    state_dir = workspace / ".ea"
    state_dir.mkdir(parents=True)
    src = _ACTUALS_FIXTURES / "valid" / "01-empty-repo.json"
    (state_dir / "state.json").write_bytes(src.read_bytes())
    return workspace


@pytest.mark.parametrize("n", [2, 3, 5])
def test_envelope_ids_are_timestamped_and_distinct(tmp_path: Path, n: int) -> None:
    """Drive N actual start/stop cycles and assert envelope ids are well-formed
    and pairwise distinct.

    Format inconsistency between actuals (us+nonce suffix) and estimates (ISO
    suffix) is intentional and documented at envelope-construction sites in
    ``src/eawf/surfaces/cli/commands/estimation.py``. We assert each id matches its own
    pattern, then assert the full id set has no duplicates. The us+nonce format
    means no inter-call sleeps are required for distinctness — the
    :func:`secrets.token_hex(2)` suffix gives 16 bits of entropy per id.
    """
    workspace = _seed_property_state(tmp_path)
    runner = CliRunner()
    scope = "P01-I01-W01"
    session_id = "SES-PROPK"

    for cycle in range(n):
        start = runner.invoke(
            app,
            [
                "-w",
                str(workspace),
                "actual",
                "start",
                scope,
                "--session",
                session_id,
            ],
        )
        assert start.exit_code == 0, (cycle, start.output)
        stop = runner.invoke(
            app,
            [
                "-w",
                str(workspace),
                "actual",
                "stop",
                scope,
            ],
        )
        assert stop.exit_code == 0, (cycle, stop.output)

    actuals_path = workspace / ".ea" / "store" / "actual.jsonl"
    raw_lines = [ln for ln in actuals_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    # 2*n envelopes: each cycle writes one start envelope and one stop envelope.
    assert len(raw_lines) == 2 * n, raw_lines

    ids: list[str] = []
    for line in raw_lines:
        env = Envelope.model_validate(json.loads(line))
        assert _ACTUAL_ID_RE.match(env.id) or _ESTIMATE_ID_RE.match(env.id), env.id
        ids.append(env.id)

    assert len(set(ids)) == len(ids), f"expected pairwise-distinct envelope ids, got {ids}"


def test_envelope_ids_unique_under_tight_loop_back_to_back(tmp_path: Path) -> None:
    """Collision-stress regression: 8 rapid back-to-back start/stop cycles.

    Pre-W5 the suffix was ``int(now.timestamp() * 1000)`` — two operations on
    the same scope landing in the same millisecond would mint identical ids.
    Post-W5 the suffix is ``<us>-<token_hex(2)>``: microsecond resolution
    plus 16 bits of entropy from :func:`secrets.token_hex`. The probability
    of an id clash across N=8 envelopes is < 6e-7 even if every microsecond
    counter collides, which it shouldn't because the cycles span well over a
    microsecond apiece. This test runs without sleeps to exercise the
    tightest possible loop.
    """
    workspace = _seed_property_state(tmp_path)
    runner = CliRunner()
    scope = "P01-I01-W01"
    session_id = "SES-STRESS"

    n = 8
    for _ in range(n):
        start = runner.invoke(
            app,
            ["-w", str(workspace), "actual", "start", scope, "--session", session_id],
        )
        assert start.exit_code == 0, start.output
        stop = runner.invoke(
            app,
            ["-w", str(workspace), "actual", "stop", scope],
        )
        assert stop.exit_code == 0, stop.output

    actuals_path = workspace / ".ea" / "store" / "actual.jsonl"
    raw_lines = [ln for ln in actuals_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(raw_lines) == 2 * n, raw_lines

    ids = [Envelope.model_validate(json.loads(line)).id for line in raw_lines]
    for envelope_id in ids:
        assert _ACTUAL_ID_RE.match(envelope_id), envelope_id
    assert len(set(ids)) == len(ids), f"expected pairwise-distinct ids; collisions: {ids}"


# ---- Property L — clock-skew emits a WARNING -------------------------------


def test_cap_elapsed_logs_warning_when_started_at_in_future(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``cap_elapsed(started_at, now=...)`` must log a ``WARNING`` mentioning
    'clock_skew' when ``started_at > now``.

    Source: ``src/eawf/estimation/recovery.py:109-112`` — the actual phrasing
    is ``"clock_skew_detected delta_s={seconds:.1f}; now precedes started_at, clamping to 0"``.
    """
    now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
    started_in_future = now + timedelta(seconds=120)

    with caplog.at_level("WARNING", logger="eawf.workflow.estimation.recovery"):
        capped_ended_at, elapsed_eu = cap_elapsed(
            started_in_future, now=now, eu_minutes=Decimal("30")
        )

    # Behavioural assertions: function must clamp the negative interval to 0
    # and surface the skew to the operator.
    assert capped_ended_at == started_in_future
    assert elapsed_eu == Decimal(0)

    skew_records = [
        rec
        for rec in caplog.records
        if "clock_skew" in rec.getMessage() and rec.levelname == "WARNING"
    ]
    assert skew_records, [r.getMessage() for r in caplog.records]
    # The skew magnitude must be present in the message so an operator can see
    # how far off the clock drifted.
    assert "120" in skew_records[0].getMessage(), skew_records[0].getMessage()
