"""The console performance harness: latency distributions, and the cold-paint clock.

Every console budget before this module was unfalsifiable: the probes measured
correctness only, so a render that got ten times slower still passed. This harness
records distributions off the shipped render path, so a budget is read off the console
an operator drives rather than off a private one.

Three things are recorded.

:meth:`PerformanceHarness.key_latency` records the key-to-painted-frame distribution at
one fleet size, and :meth:`PerformanceHarness.profile` records one distribution per size
in :data:`FLEET_SIZES`. The fleet register is the input that scales with a real
workspace, so it is the axis: the Activity route scans every run on every render, which
is where a per-row cost shows.

A sample is the console's own round trip -- dispatch the key, compose the frame, paint
it through the shipped :func:`~eawf.surfaces.tui.console.harness.capture` primitive --
and not the toolkit's simulated key press. The toolkit's press costs about seventy
milliseconds of scheduler sleep that no operator pays, and its pause about twenty more,
against a frame that composes and paints in half a millisecond. Sampling through them
measures the test harness, and swamps a per-row cost of well under a microsecond in
noise, which is exactly the unfalsifiable budget this module exists to replace. The
shipped :func:`~eawf.surfaces.tui.console.harness.settle` primitive is what proves the
shortcut sound: it runs after the sample loop and reports the cycles the screen still
needed, and one cycle means every captured frame was already the settled frame.

:meth:`ConsoleLatencyProfile.key_cost` splits the profile into the two figures a budget
gates apart. The **floor** is what one key costs when the fleet cannot be the reason: the
median at the smallest recorded fleet. The **increment** is what one extra run adds: the
median slope across the recorded span. They are budgeted apart because they regress for
different reasons -- a heavier dispatch path lifts the floor, an accidental per-row scan
lifts the increment -- and one number lets either hide inside the other.

Both gated figures are medians. The tail of a sub-millisecond operation on a host running
anything else is a scheduler spike: measured here, the median held inside a five per cent
band across repeats while the p99 of the same runs swung from 0.8ms to 24ms. A gate on
that tail needs a ceiling so generous it stops discriminating, so the tail is recorded
(:attr:`KeyCost.floor_tail_ms`, and every percentile of :class:`Distribution`) and the
ceilings bind the medians, which a real regression moves and a busy host does not.

:meth:`PerformanceHarness.cold_paint` starts its clock at process start. First meaningful
paint is not the time to compose a frame: it is the time from the operator asking for the
console to the first painted frame, and on a Python surface most of that is the import
graph. So the measurement runs in a child process whose clock origin is the moment the
parent asked for it, and it reports the import graph as a named component beside the
spawn, the fixture load and the paint itself. The child is launched with
``sys.executable`` rather than through a launcher, so the spawn component measures the
interpreter and not a resolver in front of it.

Every reading is taken through the one console clock, like every other timed behaviour in
this package. The cold-paint child is the single unavoidable exception: it reads the
clock before it has imported anything, because the console clock is on the far side of
the import graph it is there to measure. It reads the same monotonic source the console
clock wraps, so the parent's origin and the child's entry are two readings of one clock.

Nothing here is persisted: a measurement is a reading of one machine at one moment, and a
stored reading would later be quoted as a property of the code.
"""

from __future__ import annotations

import logging
import math
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eawf.surfaces.tui.console.app import ConsoleApp
from eawf.surfaces.tui.console.clock import Clock, FakeClock
from eawf.surfaces.tui.console.fixture import Detail, Fixture, FleetRow, load_fixture
from eawf.surfaces.tui.console.harness import capture, settle
from eawf.surfaces.tui.console.session import SIZES, SessionSetup

logger = logging.getLogger(__name__)

#: The fleet sizes a profile records, smallest first.
FLEET_SIZES: tuple[int, ...] = (1, 10, 100, 150)

#: The route the measurement drives: the one whose body scales with the fleet.
PERF_ROUTE = "activity"

#: The key dispatched per sample. It moves the Activity cursor, so the table re-renders.
PERF_KEY = "ArrowDown"

#: The frame size every measurement runs at, so a reading is comparable across machines.
PERF_SIZE: tuple[int, int] = SIZES[0]

#: Samples per fleet size when the caller does not reduce them. A gate passes fewer to
#: stay inside its wall-clock budget; below 100 samples a p99 degrades to the maximum.
DEFAULT_KEY_SAMPLES = 100

#: The per-key floor budget in milliseconds: what one key may cost at the median when the
#: fleet is too small to be the reason. Measured at about 0.5ms on a host running several
#: other jobs, with the median steady inside a five per cent band across repeats, so this
#: ceiling carries roughly twenty times headroom and still discriminates: a per-keystroke
#: regression of the kind the P20 postmortem recorded -- re-reading the whole workspace
#: state on every key -- lands on the median, not on one sample, and trips this.
KEY_FLOOR_CEILING_MS = 10.0

#: The per-row budget in microseconds: what one extra run may add to a keypress. Measured
#: between 0.4 and 0.7 microseconds per run, the cost of the Activity route's fixed number
#: of scans over the fleet register. This ceiling carries roughly eight times headroom: a
#: second scan per render stays under it, a per-row scan nested inside another does not.
ROW_INCREMENT_CEILING_US = 5.0

#: How far the named components of a cold paint may sum away from its total, in
#: milliseconds. They are boundary differences on one clock, so the slack is float noise.
COMPONENT_SUM_TOLERANCE_MS = 1e-3

#: The environment key carrying the parent's clock reading into the cold-paint child.
COLD_PAINT_T0_ENV = "EAWF_CONSOLE_COLD_PAINT_T0"

#: How long the cold-paint child may take before it is killed, in seconds.
_COLD_PAINT_TIMEOUT_S = 180.0

#: The cold-paint child. Its first statement reads the clock, before one eawf module is
#: imported, so the import graph lands inside a measured component instead of in front of
#: the clock. That is why it reaches for the monotonic source directly rather than for the
#: console clock that wraps it: the console clock is behind the import being measured. It
#: writes its reading to a file rather than to stdout, so nothing a library prints on the
#: way up can be mistaken for the measurement.
_COLD_PAINT_CHILD = r"""
import json
import os
import sys
import time

t_entry = time.monotonic()
requested = float(os.environ["EAWF_CONSOLE_COLD_PAINT_T0"])
fixture_dir, width, height = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
out_path = sys.argv[4]

import asyncio
from pathlib import Path

from eawf.surfaces.tui.console.app import ConsoleApp
from eawf.surfaces.tui.console.clock import FakeClock
from eawf.surfaces.tui.console.fixture import load_fixture
from eawf.surfaces.tui.console.harness import settle

t_imported = time.monotonic()
fixture = load_fixture(Path(fixture_dir))
t_loaded = time.monotonic()


async def paint() -> str:
    app = ConsoleApp(fixture, FakeClock())
    async with app.run_test(size=(width, height)) as pilot:
        text, _cycles = await settle(pilot)
    return text


frame = asyncio.run(paint())
t_painted = time.monotonic()
Path(out_path).write_text(
    json.dumps(
        {
            "spawn_ms": (t_entry - requested) * 1000.0,
            "import_ms": (t_imported - t_entry) * 1000.0,
            "fixture_ms": (t_loaded - t_imported) * 1000.0,
            "paint_ms": (t_painted - t_loaded) * 1000.0,
            "total_ms": (t_painted - requested) * 1000.0,
            "painted_rows": len([row for row in frame.split("\n") if row.strip()]),
        }
    ),
    encoding="utf-8",
)
"""


class Distribution(BaseModel):
    """One latency distribution in milliseconds, with the percentiles a budget reads.

    Attributes:
        count: How many samples the distribution was built from.
        min_ms: The fastest sample.
        p50_ms: The median sample.
        p90_ms: The 90th percentile by nearest rank.
        p99_ms: The 99th percentile by nearest rank; the maximum below 100 samples.
        max_ms: The slowest sample.
        mean_ms: The arithmetic mean.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    count: int = Field(ge=1)
    min_ms: float = Field(ge=0.0)
    p50_ms: float = Field(ge=0.0)
    p90_ms: float = Field(ge=0.0)
    p99_ms: float = Field(ge=0.0)
    max_ms: float = Field(ge=0.0)
    mean_ms: float = Field(ge=0.0)

    @classmethod
    def of(cls, samples: Sequence[float]) -> Distribution:
        """Return the distribution of ``samples``.

        Args:
            samples: Latencies in milliseconds, in any order.

        Raises:
            ValueError: ``samples`` is empty, so no percentile exists.
        """
        if not samples:
            raise ValueError("a distribution needs at least one sample")
        ordered = sorted(samples)
        return cls(
            count=len(ordered),
            min_ms=ordered[0],
            p50_ms=_percentile(ordered, 0.50),
            p90_ms=_percentile(ordered, 0.90),
            p99_ms=_percentile(ordered, 0.99),
            max_ms=ordered[-1],
            mean_ms=sum(ordered) / len(ordered),
        )


class FleetLatency(BaseModel):
    """The key latency recorded at one fleet size.

    Attributes:
        runs: How many runs the fleet register held.
        key: The key-to-painted-frame distribution at that size.
        settle_cycles: The cycles the screen still needed once the samples were taken.
            One means every sampled frame was already the settled frame.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    runs: int = Field(ge=1)
    key: Distribution
    settle_cycles: int = Field(ge=1)


class KeyCost(BaseModel):
    """The two figures a key budget gates apart.

    Attributes:
        floor_runs: The fleet size the floor was read at.
        floor_ms: The per-key floor: the median at ``floor_runs``, where the fleet is too
            small to be the reason a key is slow.
        floor_tail_ms: The p99 at ``floor_runs``. Recorded, not gated: at this scale the
            tail is a scheduler spike rather than a property of the console.
        span_runs: How many runs separate the smallest and largest recorded fleets.
        increment_us: The per-row increment: the median slope in microseconds per run.
            Negative when the larger fleet measured no slower than the smaller one, which
            is what a console doing no per-row work looks like under noise.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    floor_runs: int = Field(ge=1)
    floor_ms: float = Field(ge=0.0)
    floor_tail_ms: float = Field(ge=0.0)
    span_runs: int = Field(ge=1)
    increment_us: float


class ConsoleLatencyProfile(BaseModel):
    """Key latency across every recorded fleet size.

    Attributes:
        fleet: One record per fleet size, in the order they were measured.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    fleet: tuple[FleetLatency, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_sizes_are_distinct(self) -> ConsoleLatencyProfile:
        """Reject a fleet size recorded twice: its two records cannot both be the size."""
        sizes = [record.runs for record in self.fleet]
        if len(set(sizes)) != len(sizes):
            raise ValueError(f"a profile records each fleet size once, not {sizes}")
        return self

    def by_runs(self) -> dict[int, Distribution]:
        """Return each recorded distribution by its fleet size."""
        return {record.runs: record.key for record in self.fleet}

    def key_cost(self) -> KeyCost:
        """Return the per-key floor and the per-row increment.

        Raises:
            ValueError: the profile records one fleet size only, so no slope exists.
        """
        smallest = min(self.fleet, key=lambda record: record.runs)
        largest = max(self.fleet, key=lambda record: record.runs)
        span = largest.runs - smallest.runs
        if span <= 0:
            raise ValueError("a key-cost split needs two different fleet sizes")
        rise_ms = largest.key.p50_ms - smallest.key.p50_ms
        return KeyCost(
            floor_runs=smallest.runs,
            floor_ms=smallest.key.p50_ms,
            floor_tail_ms=smallest.key.p99_ms,
            span_runs=span,
            increment_us=rise_ms * 1000.0 / span,
        )


class ColdPaint(BaseModel):
    """First meaningful paint from process start, broken into named components.

    Attributes:
        spawn_ms: From the parent asking for the process to the child's first statement:
            process creation plus interpreter start-up.
        import_ms: The console import graph, the toolkit underneath it included.
        fixture_ms: Loading and validating the fixture registers.
        paint_ms: Mounting the console and settling its first frame.
        total_ms: From the parent asking for the process to that settled frame.
        painted_rows: How many non-blank rows the first frame carried; a paint with none
            is not meaningful.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    spawn_ms: float = Field(ge=0.0)
    import_ms: float = Field(ge=0.0)
    fixture_ms: float = Field(ge=0.0)
    paint_ms: float = Field(ge=0.0)
    total_ms: float = Field(ge=0.0)
    painted_rows: int = Field(ge=1)

    @model_validator(mode="after")
    def _check_components_account_for_the_total(self) -> ColdPaint:
        """Reject components that do not account for the total they were cut from."""
        drift = abs(sum(self.components().values()) - self.total_ms)
        if drift > COMPONENT_SUM_TOLERANCE_MS:
            raise ValueError(f"components miss the total by {drift:.6f}ms")
        return self

    def components(self) -> dict[str, float]:
        """Return each named component of the paint, in the order they happen."""
        return {
            "spawn": self.spawn_ms,
            "import": self.import_ms,
            "fixture": self.fixture_ms,
            "paint": self.paint_ms,
        }


def _percentile(ordered: Sequence[float], quantile: float) -> float:
    """Return the nearest-rank percentile of an already-sorted, non-empty sequence."""
    rank = max(1, math.ceil(quantile * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def _scaled_rows(rows: Sequence[FleetRow], runs: int) -> tuple[FleetRow, ...]:
    """Return ``runs`` fleet rows cycled from ``rows``, each under its own run id.

    Raises:
        ValueError: ``runs`` is below one, or ``rows`` is empty so nothing can be cycled.
    """
    if runs < 1:
        raise ValueError(f"a fleet holds at least one run, not {runs}")
    if not rows:
        raise ValueError("an empty fleet register cannot be scaled")
    return tuple(
        rows[index % len(rows)].model_copy(update={"run": f"RUN-{index:06d}"})
        for index in range(runs)
    )


def _scaled_fixture(fixture: Fixture, runs: int) -> Fixture:
    """Return ``fixture`` with its fleet register scaled to ``runs`` rows.

    Every other register is shared. The fleet is the axis under measurement, and
    rebuilding the rest would put the copy inside the reading rather than the render.
    """
    proto = fixture.proto.model_copy(update={"fleet": _scaled_rows(fixture.proto.fleet, runs)})
    return Fixture(proto, Detail(dict(fixture.detail)), fixture.registers, fixture.settings)


def _run_child(argv: Sequence[str], env: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
    """Run the cold-paint child, turning a hang into a failed run rather than a wedge.

    Raises:
        RuntimeError: the child outlived :data:`_COLD_PAINT_TIMEOUT_S`.
    """
    try:
        return subprocess.run(
            list(argv),
            env=dict(env),
            capture_output=True,
            text=True,
            check=False,
            timeout=_COLD_PAINT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"the cold-paint child outlived {_COLD_PAINT_TIMEOUT_S:.0f}s") from exc


class PerformanceHarness:
    """Record console latency over one fixture, at whatever fleet sizes are asked for.

    Args:
        fixture_dir: The fixture registers directory the console renders from.

    Raises:
        FileNotFoundError: a register file is missing.
        pydantic.ValidationError: a register carries an unknown key or a malformed row.
    """

    def __init__(self, fixture_dir: Path) -> None:
        self.fixture_dir = fixture_dir
        self.fixture = load_fixture(fixture_dir)
        self.clock = Clock()

    async def key_latency(
        self,
        runs: int,
        *,
        samples: int = DEFAULT_KEY_SAMPLES,
        key: str = PERF_KEY,
    ) -> FleetLatency:
        """Return the key-to-painted-frame record at a fleet of ``runs``.

        One console is mounted for the whole measurement, so what is timed is a key on a
        warm console rather than a mount. Each sample runs from the dispatch to the
        painted frame, which is the work the console owns.

        Args:
            runs: How many runs the fleet register holds.
            samples: How many keys are dispatched; fewer keys, a coarser tail.
            key: The dispatcher's name for the key pressed per sample.

        Raises:
            ValueError: ``samples`` or ``runs`` is below one, or the console painted an
                empty frame, which would make every reading meaningless.
        """
        if samples < 1:
            raise ValueError(f"a distribution needs at least one sample, not {samples}")
        app = ConsoleApp(_scaled_fixture(self.fixture, runs), FakeClock())
        latencies: list[float] = []
        async with app.run_test(size=PERF_SIZE) as pilot:
            app.reset(SessionSetup(route=PERF_ROUTE))
            app.render_frame()
            await settle(pilot)
            for _ in range(samples):
                start = self.clock.now()
                app.press_key(key)
                capture(app)
                latencies.append((self.clock.now() - start) * 1000.0)
            painted, cycles = await settle(pilot)
        if not painted.strip():
            raise ValueError(f"the console painted an empty frame at a fleet of {runs}")
        record = FleetLatency(runs=runs, key=Distribution.of(latencies), settle_cycles=cycles)
        logger.info(
            "console key latency recorded",
            extra={"runs": runs, "samples": record.key.count, "p50_ms": record.key.p50_ms},
        )
        return record

    async def profile(
        self,
        *,
        fleet_sizes: Sequence[int] = FLEET_SIZES,
        samples: int = DEFAULT_KEY_SAMPLES,
    ) -> ConsoleLatencyProfile:
        """Return one key-latency record per fleet size.

        Args:
            fleet_sizes: The fleet sizes to record, each measured in its own console.
            samples: How many keys are dispatched at each size.

        Raises:
            ValueError: ``fleet_sizes`` is empty, or names a size twice.
        """
        if not fleet_sizes:
            raise ValueError("a profile records at least one fleet size")
        records = [await self.key_latency(runs, samples=samples) for runs in fleet_sizes]
        return ConsoleLatencyProfile(fleet=tuple(records))

    def cold_paint(self, *, size: tuple[int, int] = PERF_SIZE) -> ColdPaint:
        """Return first meaningful paint measured from process start.

        The clock starts here, in the parent, at the moment the child is asked for, so the
        reading covers everything an operator waits through: process creation, interpreter
        start-up, the import graph, the fixture load and the paint.

        Args:
            size: The frame size the child paints at.

        Raises:
            RuntimeError: the child failed, timed out, or wrote no reading.
            pydantic.ValidationError: the child's reading does not account for its total.
        """
        with tempfile.TemporaryDirectory() as workspace:
            reading = Path(workspace) / "cold-paint.json"
            argv = [
                sys.executable,
                "-c",
                _COLD_PAINT_CHILD,
                str(self.fixture_dir),
                str(size[0]),
                str(size[1]),
                str(reading),
            ]
            env = dict(os.environ)
            env[COLD_PAINT_T0_ENV] = repr(self.clock.now())
            done = _run_child(argv, env)
            if done.returncode != 0 or not reading.exists():
                raise RuntimeError(
                    f"the cold-paint child exited {done.returncode} without a reading: "
                    f"{done.stderr.strip()[-2000:]}"
                )
            paint = ColdPaint.model_validate_json(reading.read_text(encoding="utf-8"))
        logger.info(
            "console cold paint recorded",
            extra={"total_ms": paint.total_ms, "import_ms": paint.import_ms},
        )
        return paint
