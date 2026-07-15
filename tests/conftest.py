from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import hypothesis
import pytest

from eawf.kernel.spec.intent import IntentBrief

# --- Hypothesis CI example-budget profile (P30-I26-W22) --------------------
#
# ``register_profile("ci", ...)`` gives CI a low ``max_examples`` budget so
# the Hypothesis-governed property tests -- the ones that do NOT pin their own
# ``@settings(max_examples=...)`` -- run far fewer examples and the property
# suite's wall-clock drops. Tests that DO pin an example count keep it; the
# ``slow`` pytest marker is what deselects those heavy property tests under CI
# instead. The ``dev`` profile is a stock-default copy so local ``pytest``
# behaviour is unchanged beyond the profile registration itself.
# ``HYPOTHESIS_PROFILE`` overrides the auto-selection when explicitly set.

hypothesis.settings.register_profile("dev", hypothesis.settings())
hypothesis.settings.register_profile("ci", hypothesis.settings(max_examples=25))
hypothesis.settings.load_profile(
    os.environ.get("HYPOTHESIS_PROFILE") or ("ci" if os.environ.get("CI") else "dev")
)

# --- suite daemon runtime-dir isolation (P30-I23-W14) ----------------------
#
# The suite must never bind a daemon socket in -- or mutate state through --
# the operator's live ``~/.eawfd`` runtime dir: a shared-daemon run once
# flipped live ``dispatch_paused`` and seeded a cluster of ``-n auto``
# config-validate / surfaces-smoke flakes. ``runtime_dir()`` already honours
# ``EAWF_RUNTIME_DIR`` as an explicit override, so a session- and
# worker-scoped autouse fixture points every worker at its own short tmp dir.
#
# The dir is rooted directly under ``$TMPDIR`` (the tmp-tree root), not the
# pytest basetemp: on macOS the basetemp lives under
# ``/private/var/folders/.../pytest-of-<user>/pytest-N/popen-gwN`` which
# pushes ``<dir>/eawfd.sock`` to ~106 bytes, past the 104-byte AF_UNIX
# ``sun_path`` cap, so a real socket bind under it would fail. A short
# per-worker stem under ``$TMPDIR`` stays well inside the cap.

_HOME_RUNTIME_DIR: Path = Path.home() / ".eawfd"


def home_runtime_dir_signature() -> tuple[bool, int]:
    """Return an ``(exists, mtime_ns)`` signature for the live ``~/.eawfd``.

    The directory mtime moves only when its entry set changes (a socket,
    PID, lock, or WAL segment created / removed / renamed), not when an
    already-open log is appended to, so an idle live daemon does not bump
    it. That makes the signature a low-noise witness that a suite run never
    spawned or rebound a daemon in the operator's live runtime dir.

    Returns:
        ``(False, 0)`` when ``~/.eawfd`` is absent, else ``(True,
        st_mtime_ns)`` of the directory.
    """
    try:
        stat = _HOME_RUNTIME_DIR.stat()
    except FileNotFoundError:
        return (False, 0)
    return (True, stat.st_mtime_ns)


def _isolated_runtime_dir() -> Path:
    """Return a per-worker runtime dir short enough for the AF_UNIX cap.

    Rooted under ``$TMPDIR`` with an ``eawf-rt-<worker>-<rand>`` stem so
    concurrent xdist workers -- and concurrent pytest sessions -- never
    share a daemon socket. See the module note above for why the pytest
    basetemp is unusable on macOS.
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    return Path(tempfile.gettempdir()) / f"eawf-rt-{worker}-{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True)
class RuntimeDirIsolation:
    """Handle the runtime-dir isolation fixture hands to the guard test.

    Attributes:
        runtime_dir: The per-worker tmp dir ``EAWF_RUNTIME_DIR`` points at.
        home_signature_before: ``~/.eawfd`` signature captured before any
            test ran, for the guard test's before/after comparison.
    """

    runtime_dir: Path
    home_signature_before: tuple[bool, int]


@pytest.fixture(scope="session", autouse=True)
def runtime_dir_isolation() -> Iterator[RuntimeDirIsolation]:
    """Redirect the daemon runtime dir off the live ``~/.eawfd`` for the suite.

    Autouse + session-scoped, so it runs once per worker process before the
    first test and stays active for every test in that worker. Captures the
    live ``~/.eawfd`` signature up front (the before/after baseline), points
    ``EAWF_RUNTIME_DIR`` at a fresh per-worker tmp dir, and restores the
    prior env value and removes the tmp dir on teardown.

    Yields:
        The :class:`RuntimeDirIsolation` handle the guard test asserts on.
    """
    home_signature_before = home_runtime_dir_signature()
    isolated = _isolated_runtime_dir()
    isolated.mkdir(parents=True, exist_ok=True)
    previous = os.environ.get("EAWF_RUNTIME_DIR")
    os.environ["EAWF_RUNTIME_DIR"] = str(isolated)
    try:
        yield RuntimeDirIsolation(
            runtime_dir=isolated,
            home_signature_before=home_signature_before,
        )
    finally:
        if previous is None:
            os.environ.pop("EAWF_RUNTIME_DIR", None)
        else:
            os.environ["EAWF_RUNTIME_DIR"] = previous
        shutil.rmtree(isolated, ignore_errors=True)


def make_intent(
    problem: str = "test wave lacks a typed intent",
    desired_outcome: str = "the test wave carries a populated IntentBrief",
) -> IntentBrief:
    """Build a fully-populated :class:`IntentBrief` for plan_wave call sites.

    The authoring guard on :func:`eawf.workflow.lifecycle.wave.plan_wave`
    rejects an intent of ``None``, so every test that stages a wave needs a
    populated brief. This shared factory keeps the 150+ call sites DRY and
    returns a brief that also carries a non-empty ``priority_rationale``,
    one ``planned_steps`` entry, and one ``risks`` entry so the fixture
    survives a future authoring gate that requires non-blank body fields.

    Args:
        problem: The brief's ``problem`` line (1-200 chars).
        desired_outcome: The brief's ``desired_outcome`` line (1-200 chars).

    Returns:
        A populated :class:`IntentBrief`.
    """
    return IntentBrief(
        problem=problem,
        desired_outcome=desired_outcome,
        priority_rationale="exercises the plan_wave authoring path under test",
        planned_steps=["stage the wave with a populated intent"],
        risks=["none material for the test fixture"],
    )


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """Skeleton fixture for a throwaway repository directory.

    Phase 1+ tests will populate this with the canonical .ea/ skeleton via
    eawf.platform.install. For now it returns a bare temp directory.
    """
    return tmp_path


def make_floor_waiver():
    """Build a typed criteria-floor waiver for legacy-criterion fixtures.

    The plan-time typed-criteria floor (P30-I23-W26) rejects a wave
    authored with grandfathered legacy rows; fixtures that deliberately
    model migration-era legacy waves attach this waiver so the modelled
    state stays constructible while the floor stays on for real authoring.
    """
    from datetime import UTC, datetime

    from eawf.kernel.state.models import CriteriaFloorWaiver

    return CriteriaFloorWaiver(
        reason="test fixture models a migration-era legacy wave",
        waived_at=datetime(2026, 7, 2, tzinfo=UTC),
    )
