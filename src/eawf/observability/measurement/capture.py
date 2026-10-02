"""The capture producer: read a Run's counters through its vendor session.

A Run is read twice. :func:`capture_run_start` takes the baseline when the
Run starts, and :func:`capture_run_terminal` takes the stop reading and
differences the two. Both resolve their source only through the Run's
:class:`~eawf.kernel.state.epoch2.measurement.VendorSessionRef`: the
session transcript when one is found, the statusline counter sidecar
otherwise, and neither when neither is -- in which case the Run records
that it has no captured runtime instead of differencing a zero it never
read.

The session id is stored hashed, so both sources are found by hashing
the candidate file names and matching the digest; the raw id is never
needed after the Run was started.

A Run that starts in the middle of a host turn is the one subtle case.
The host writes a turn's duration only when the turn ends, so a baseline
read at the start cannot see the turn it started inside, and the stop
reading would bank that whole turn -- the minutes before the Run existed
included. The stop capture therefore re-reads the transcript as of the
Run's start: a turn straddling it is split in proportion, and the
baseline that results is marked derived.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from eawf.kernel.state.epoch2.measurement import (
    UNKNOWN_ATTRIBUTION,
    CaptureSource,
    CounterName,
    CounterSnapshot,
    ExcludedRuntime,
    MeasuredRuntime,
    Observed,
    SpanSummary,
    UncapturedReason,
    UncapturedRuntime,
    Unobserved,
    VendorSessionRef,
    measure_run,
    summarize_spans,
)
from eawf.kernel.state.epoch2.run import (
    RUNTIME_MODEL_PATTERN,
    RUNTIME_VERSION_PATTERN,
    RunRuntimeTuple,
)
from eawf.observability.measurement.transcript_spans import transcript_spans
from eawf.runtime.runtime_counter_sidecar import RuntimeCounterSidecar
from eawf.runtime.runtimes.claude.runtime_counters import RuntimeCounters
from eawf.runtime.runtimes.claude.statusline import cache_path_for
from eawf.runtime.runtimes.claude.transcript_counters import (
    MEASURE_VERSION,
    TranscriptReading,
    projects_root,
    read_transcript,
)
from eawf.runtime.session.vendor_id import hash_vendor_session_id

logger = logging.getLogger(__name__)

#: The one harness whose transcript and sidecar this producer can read.
CLAUDE_HARNESS: Final = "claude-code"

#: The suffix the statusline gives a session's counter sidecar file.
_SIDECAR_SUFFIX: Final = ".runtime-counters.json"

#: The grammars a version and a model must read in before a Run records them.
_VERSION: Final = re.compile(RUNTIME_VERSION_PATTERN)
_MODEL: Final = re.compile(RUNTIME_MODEL_PATTERN)

#: The directory a session keeps its subagents' transcripts in, and their name prefix.
_SUBAGENT_DIRNAME: Final = "subagents"
_SUBAGENT_PREFIX: Final = "agent-"


def _transcript_session(path: Path) -> str:
    """Return the session id a transcript file is named for.

    A subagent's transcript sits under its spawning session as
    ``subagents/agent-<id>.jsonl``, and the id its Run is adopted under is the
    agent id alone, so the prefix is not part of the session.
    """
    if path.parent.name == _SUBAGENT_DIRNAME:
        return path.stem.removeprefix(_SUBAGENT_PREFIX)
    return path.stem


def _transcript_for(digest: str) -> Path | None:
    """Return the transcript whose session id hashes to *digest*, if any.

    A session's own transcript and a subagent's are separate files, so a
    subagent's Run reads only its own rows and never the session that spawned it.
    """
    root = projects_root()
    try:
        candidates = sorted(
            [
                *root.glob("*/*.jsonl"),
                *root.glob(f"*/*/{_SUBAGENT_DIRNAME}/{_SUBAGENT_PREFIX}*.jsonl"),
            ]
        )
    except OSError as exc:
        logger.debug(f"_transcript_for err={exc!r}")
        return None
    return next(
        (
            path
            for path in candidates
            if hash_vendor_session_id(_transcript_session(path)) == digest
        ),
        None,
    )


def _sidecar_for(digest: str) -> Path | None:
    """Return the counter sidecar whose session id hashes to *digest*, if any."""
    # Every session's cache file sits directly in the statusline cache root.
    root = cache_path_for("unknown").parent
    try:
        candidates = sorted(root.glob(f"*{_SIDECAR_SUFFIX}"))
    except OSError as exc:
        logger.debug(f"_sidecar_for err={exc!r}")
        return None
    return next(
        (
            path
            for path in candidates
            if hash_vendor_session_id(path.name.removesuffix(_SIDECAR_SUFFIX)) == digest
        ),
        None,
    )


def _transcript_readings(reading: TranscriptReading) -> dict[CounterName, Observed | Unobserved]:
    """Return every counter of a transcript reading, unobserved where it is."""
    scan = reading.scan
    tally = scan.tally
    readings: dict[CounterName, Observed | Unobserved] = {}
    # A transcript with no billed message genuinely records no usage yet;
    # past the first message, a class no message reported was not seen.
    for name in (
        CounterName.INPUT_TOKENS,
        CounterName.OUTPUT_TOKENS,
        CounterName.CACHE_CREATION_INPUT_TOKENS,
        CounterName.CACHE_READ_INPUT_TOKENS,
    ):
        if scan.messages and name.value not in tally.observed:
            readings[name] = Unobserved(reason=f"no transcript message reports {name.value}")
        else:
            readings[name] = Observed(value=Decimal(getattr(tally, name.value)))
    if scan.unmeasurable:
        readings[CounterName.DURATION_MS] = Unobserved(
            reason="the turn durations outrun the transcript's own lifetime"
        )
    else:
        readings[CounterName.DURATION_MS] = Observed(value=Decimal(scan.duration_ms))
    cost = None if reading.counters is None else reading.counters.cost_usd
    if cost is not None:
        readings[CounterName.COST_USD] = Observed(value=cost)
    elif not scan.messages:
        readings[CounterName.COST_USD] = Observed(value=Decimal(0))
    else:
        readings[CounterName.COST_USD] = Unobserved(
            reason=f"no price is known for model {scan.model or UNKNOWN_ATTRIBUTION}"
        )
    return readings


def _sidecar_readings(counters: RuntimeCounters) -> dict[CounterName, Observed | Unobserved]:
    """Return every counter of a sidecar reading, unobserved where it is."""
    values: dict[CounterName, int | Decimal | None] = {
        CounterName.DURATION_MS: counters.api_duration_ms,
        CounterName.INPUT_TOKENS: counters.input_tokens,
        CounterName.OUTPUT_TOKENS: counters.output_tokens,
        CounterName.CACHE_CREATION_INPUT_TOKENS: counters.cache_creation_input_tokens,
        CounterName.CACHE_READ_INPUT_TOKENS: counters.cache_read_input_tokens,
        CounterName.COST_USD: counters.cost_usd,
    }
    return {
        name: Unobserved(reason=f"the counter sidecar does not report {name.value}")
        if value is None
        else Observed(value=Decimal(value))
        for name, value in values.items()
    }


def _transcript_snapshot(
    reading: TranscriptReading, *, at: datetime, concurrent_run_count: int
) -> CounterSnapshot:
    """Return the snapshot a transcript reading stands for at *at*."""
    return CounterSnapshot(
        captured_at=at,
        source=CaptureSource.TRANSCRIPT,
        harness=CLAUDE_HARNESS,
        model=reading.scan.model or UNKNOWN_ATTRIBUTION,
        measurement_version=MEASURE_VERSION,
        concurrent_run_count=concurrent_run_count,
        derived=reading.scan.straddled,
        counters=_transcript_readings(reading),
    )


def _read_snapshot(
    ref: VendorSessionRef, *, at: datetime, as_of: datetime, concurrent_run_count: int
) -> tuple[CounterSnapshot, TranscriptReading | None] | None:
    """Return the snapshot the session's best source gives, and its transcript.

    Args:
        ref: The Run's vendor session.
        at: The instant the snapshot is stamped with.
        as_of: The instant the transcript is read as of.
        concurrent_run_count: The divisor the snapshot records.

    Returns:
        The snapshot and, for a transcript source, the reading it came
        from; ``None`` when the harness has no reader or neither source
        yields a reading.
    """
    if ref.harness != CLAUDE_HARNESS:
        return None
    transcript = _transcript_for(ref.session_digest)
    reading = None if transcript is None else read_transcript(transcript, as_of=as_of)
    if reading is not None:
        snapshot = _transcript_snapshot(reading, at=at, concurrent_run_count=concurrent_run_count)
        return snapshot, reading
    sidecar = _sidecar_for(ref.session_digest)
    counters = None if sidecar is None else RuntimeCounterSidecar(sidecar).read()
    if counters is None or counters.measure_version is None:
        return None
    snapshot = CounterSnapshot(
        captured_at=at,
        source=CaptureSource.SIDECAR,
        harness=counters.harness or UNKNOWN_ATTRIBUTION,
        model=counters.model or UNKNOWN_ATTRIBUTION,
        measurement_version=counters.measure_version,
        concurrent_run_count=concurrent_run_count,
        counters=_sidecar_readings(counters),
    )
    return snapshot, None


def capture_run_start(
    ref: VendorSessionRef | None, *, at: datetime, concurrent_run_count: int
) -> CounterSnapshot | UncapturedRuntime:
    """Take a Run's baseline at its own start.

    Args:
        ref: The Run's vendor session, or ``None`` when it names none.
        at: When the Run started; the transcript is read as of it.
        concurrent_run_count: How many active Runs share the session now,
            this one included.

    Returns:
        The baseline, or the record that there is none to take.
    """
    if ref is None:
        return UncapturedRuntime(reason=UncapturedReason.NO_VENDOR_SESSION)
    read = _read_snapshot(ref, at=at, as_of=at, concurrent_run_count=concurrent_run_count)
    if read is None:
        logger.info(f"capture_run_start harness={ref.harness} outcome=uncaptured")
        return UncapturedRuntime(reason=UncapturedReason.NO_SOURCE)
    snapshot, _ = read
    logger.info(
        f"capture_run_start harness={ref.harness} source={snapshot.source.value} "
        f"concurrent_run_count={concurrent_run_count}"
    )
    return snapshot


def _bounded_baseline(
    ref: VendorSessionRef, baseline: CounterSnapshot, *, started_at: datetime
) -> CounterSnapshot:
    """Return the baseline re-read as of the Run's start, when a turn straddles it.

    Only a transcript baseline can be re-read. The re-read keeps the
    divisor recorded at the start, because that is the moment the session
    was shared.
    """
    if baseline.source is not CaptureSource.TRANSCRIPT:
        return baseline
    reread = _read_snapshot(
        ref,
        at=baseline.captured_at,
        as_of=started_at,
        concurrent_run_count=baseline.concurrent_run_count,
    )
    if reread is None or not reread[0].derived:
        return baseline
    return reread[0]


def capture_run_terminal(
    ref: VendorSessionRef | None,
    *,
    baseline: CounterSnapshot | UncapturedRuntime | None,
    started_at: datetime,
    at: datetime,
) -> MeasuredRuntime | ExcludedRuntime | UncapturedRuntime:
    """Take a Run's stop reading and difference it against its baseline.

    Args:
        ref: The Run's vendor session, or ``None`` when it names none.
        baseline: The reading taken at the start, or ``None`` when the Run
            started without one.
        started_at: When the Run started.
        at: When the Run stopped.

    Returns:
        The Run's measured share, the exclusion that explains why there is
        none, or the record that nothing was captured.
    """
    if ref is None:
        return UncapturedRuntime(reason=UncapturedReason.NO_VENDOR_SESSION)
    if not isinstance(baseline, CounterSnapshot):
        return UncapturedRuntime(reason=UncapturedReason.NO_BASELINE)
    read = _read_snapshot(ref, at=at, as_of=at, concurrent_run_count=baseline.concurrent_run_count)
    if read is None:
        return measure_run(baseline, None, spans=Unobserved(reason="no source at stop"))
    terminal, reading = read
    spans: SpanSummary | Unobserved
    if reading is None:
        spans = Unobserved(reason="the counter sidecar records no row timing")
    else:
        tiles = transcript_spans(reading.rows, window_start=started_at, window_end=at)
        spans = summarize_spans(tiles) if tiles else Unobserved(reason="the Run's window is empty")
    captured = measure_run(
        _bounded_baseline(ref, baseline, started_at=started_at),
        terminal.model_copy(update={"derived": False}),
        spans=spans,
    )
    logger.info(f"capture_run_terminal harness={ref.harness} outcome={captured.outcome}")
    return captured


def _stated(value: object, pattern: re.Pattern[str]) -> str | None:
    """Return *value* when it is text in *pattern*'s grammar, else ``None``."""
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _transcript_version(rows: Sequence[dict[str, Any]]) -> str | None:
    """Return the runtime version the newest transcript row was written by."""
    for row in reversed(rows):
        version = _stated(row.get("version"), _VERSION)
        if version is not None:
            return version
    return None


def observe_session_runtime(ref: VendorSessionRef, *, as_of: datetime) -> RunRuntimeTuple:
    """Return the runtime a vendor session shows it ran on, as of *as_of*.

    The harness is the session's own. Claude Code stamps each transcript row
    with the version that wrote it and each billed message with its model,
    so both are read off the transcript, or the model alone off the sidecar
    when no transcript is found. What no source shows stays ``None``.

    Args:
        ref: The Run's vendor session.
        as_of: The instant the transcript is read as of.

    Returns:
        The observed tuple.
    """
    observed = RunRuntimeTuple(harness=ref.harness)
    if ref.harness != CLAUDE_HARNESS:
        return observed
    transcript = _transcript_for(ref.session_digest)
    reading = None if transcript is None else read_transcript(transcript, as_of=as_of)
    if reading is not None:
        return observed.model_copy(
            update={
                "harness_version": _transcript_version(reading.rows),
                "model": _stated(reading.scan.model, _MODEL),
            }
        )
    sidecar = _sidecar_for(ref.session_digest)
    counters = None if sidecar is None else RuntimeCounterSidecar(sidecar).read()
    model = None if counters is None else _stated(counters.model, _MODEL)
    return observed.model_copy(update={"model": model})


__all__ = [
    "CLAUDE_HARNESS",
    "capture_run_start",
    "capture_run_terminal",
    "observe_session_runtime",
]
