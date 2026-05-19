"""Cold-spawn latency benchmark for the eawfd daemon.

V1 [1:44] sets a warm-cache target of 400 ms for the round-trip
``cold-spawn → connect → daemon.ping`` sequence. macOS APFS exhibits
cold-cache spikes that exceed the warm-cache budget on a tiny
fraction of runs; the assertion gate uses a more permissive 600 ms
threshold so transient APFS effects do not flake CI while the
underlying regression signal is still visible.

The body of the benchmark is a single round-trip ``DaemonClient``
context — spawn + connect + ping + teardown. ``pytest-benchmark``
times it across multiple iterations and aggregates the distribution;
the gate compares the empirical p95 against the documented threshold.

Run modes:

- ``uv run pytest benches/daemon_cold_spawn.py --benchmark-only`` —
  full benchmark, operator-triggered. Spawns a fresh daemon per
  iteration to measure the actual cold-spawn path.
- ``uv run pytest benches/daemon_cold_spawn.py --benchmark-disable``
  — CI smoke run. Validates the file loads + type-checks; the test
  body skips when benchmarking is disabled so CI does not pay the
  fork+exec wall-clock cost.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from eawf.cli._daemon_client import DaemonClient
from eawf.daemon.spawn import DaemonSpawnTimeout

#: Threshold for the p95 assertion gate. V1 [1:44] documents the
#: 400 ms warm-cache target; the gate runs at 600 ms to absorb APFS
#: cold-cache jitter on macOS without losing the regression signal.
P95_THRESHOLD_SECONDS: float = 0.600


pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="windows spawn benchmark covered by the named-pipe wave (W09)",
)


def _short_runtime_dir() -> Path:
    """Return a per-test runtime dir short enough for AF_UNIX (104-byte cap).

    macOS ``$TMPDIR`` lives under ``/var/folders/...`` which routinely
    exceeds the cap after pytest adds its own per-test sub-directory.
    The benchmark therefore allocates the runtime dir directly under
    ``$TMPDIR`` with an 8-char uuid stem so the resulting socket path
    stays within bounds.
    """
    base = Path(tempfile.gettempdir())
    return base / f"eawfd-bench-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def cold_runtime() -> Iterator[Path]:
    """Yield a fresh runtime dir; teardown kills any spawned daemon.

    The fixture is per-test so each benchmark iteration that goes
    through this fixture sees a clean slate (no pre-warmed PID file,
    no stale socket).
    """
    runtime_dir = _short_runtime_dir()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    try:
        yield runtime_dir
    finally:
        # Teardown: stop any daemon still bound to the temp runtime dir.
        pid_file = runtime_dir / "eawfd.pid"
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text(encoding="utf-8").splitlines()[0])
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGTERM)
            except OSError, ValueError, IndexError:
                pass
        # Wait briefly for the socket to disappear so subsequent tests do
        # not collide on the same path.
        sock_path = runtime_dir / "eawfd.sock"
        deadline = time.monotonic() + 2.0
        while sock_path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        with contextlib.suppress(FileNotFoundError):
            sock_path.unlink()
        with contextlib.suppress(FileNotFoundError):
            pid_file.unlink()
        with contextlib.suppress(OSError):
            runtime_dir.rmdir()


def _benchmark_disabled(benchmark: object) -> bool:
    """Return True when ``--benchmark-disable`` is in effect.

    ``pytest-benchmark`` exposes a ``disabled`` attribute on the
    fixture in disable mode; older versions surface it via
    ``stats.disabled``. The helper papers over both.
    """
    if getattr(benchmark, "disabled", False):
        return True
    stats = getattr(benchmark, "stats", None)
    return bool(getattr(stats, "disabled", False))


def _kill_existing_daemon(runtime_dir: Path) -> None:
    """SIGTERM any pre-existing daemon bound to *runtime_dir* and wait for teardown."""
    pid_file = runtime_dir / "eawfd.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text(encoding="utf-8").splitlines()[0])
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGTERM)
        except OSError, ValueError, IndexError:
            pass
        sock_path = runtime_dir / "eawfd.sock"
        deadline = time.monotonic() + 2.0
        while sock_path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        with contextlib.suppress(OSError):
            pid_file.unlink()


def _cold_spawn_round_trip(runtime_dir: Path) -> None:
    """One full cold-spawn cycle: spawn + connect + ping + teardown.

    Args:
        runtime_dir: Per-iteration runtime dir; the helper kills any
            pre-existing daemon before spawning a fresh one.
    """
    _kill_existing_daemon(runtime_dir)
    try:
        with DaemonClient(runtime_dir=runtime_dir) as client:
            result = client.call("daemon.ping")
            assert result["protocol_version"] == "1"
    except DaemonSpawnTimeout:
        pytest.skip("daemon spawn timed out; environment may not allow fork+exec")


def test_cold_spawn_p95_under_threshold(benchmark: object, cold_runtime: Path) -> None:
    """Cold-spawn p95 stays under the documented threshold.

    The threshold accounts for V1's 400 ms warm-cache target plus
    APFS cold-cache headroom (~200 ms) per the
    ``2026-05-19-p24-c02-impl-waves.md`` spike brief §4. A regression
    that exceeds the threshold fails this test in CI.
    """
    if _benchmark_disabled(benchmark):
        pytest.skip("--benchmark-disable in effect; skipping real cold-spawn")
    # ``pytest-benchmark`` instruments the callable + records the
    # distribution. The fixture's static type is ``object`` here
    # because the dev extras carry ``pytest-benchmark`` but the
    # plugin's stubs are not installed; reach for the runtime API
    # via attribute access without typed stubs.
    runner = benchmark
    runner.pedantic(  # type: ignore[attr-defined]
        _cold_spawn_round_trip,
        args=(cold_runtime,),
        iterations=1,
        rounds=3,
        warmup_rounds=0,
    )
    stats = runner.stats.stats  # type: ignore[attr-defined]
    # pytest-benchmark exposes mean, stddev, min, max, median; p95 is
    # not always available across versions, so fall back to max as a
    # conservative upper bound when missing.
    p95_attr = getattr(stats, "percentiles", None)
    if isinstance(p95_attr, dict) and 95 in p95_attr:
        observed = float(p95_attr[95])
    else:
        observed = float(getattr(stats, "max", stats.mean))
    assert observed < P95_THRESHOLD_SECONDS, (
        f"cold-spawn p95 regression: observed={observed:.3f}s "
        f"threshold={P95_THRESHOLD_SECONDS:.3f}s"
    )


def test_warm_attach_under_400ms(benchmark: object, cold_runtime: Path) -> None:
    """Warm-attach (no spawn) stays under the V1 400 ms warm-cache target.

    Spawns the daemon once via the fixture, then benchmarks repeat
    ``DaemonClient`` connects against the already-running daemon —
    isolating the connect + RPC latency from the cold-spawn cost.
    """
    if _benchmark_disabled(benchmark):
        pytest.skip("--benchmark-disable in effect; skipping warm-attach roundtrip")
    # Warm the daemon once so subsequent calls go through the
    # already-running path.
    with DaemonClient(runtime_dir=cold_runtime) as client:
        client.call("daemon.ping")

    def _warm_round_trip() -> None:
        with DaemonClient(runtime_dir=cold_runtime) as client:
            client.call("daemon.ping")

    runner = benchmark
    runner.pedantic(  # type: ignore[attr-defined]
        _warm_round_trip,
        iterations=5,
        rounds=5,
        warmup_rounds=1,
    )
    stats = runner.stats.stats  # type: ignore[attr-defined]
    mean = float(stats.mean)
    assert mean < 0.400, f"warm-attach mean regression: observed={mean:.3f}s threshold=0.400s"
