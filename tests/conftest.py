from __future__ import annotations

import functools
import os
import shutil
import sys
import tempfile
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import hypothesis
import pytest

from eawf.kernel.spec.common import (
    CriterionSpec,
    ObserveVerb,
    ProofLocus,
    QualityDimension,
    ResponseClause,
)
from eawf.kernel.spec.intent import IntentBrief
from eawf.kernel.state.models import CriteriaFloorWaiver
from eawf.platform.lint.kind_taxonomy import kind_for_test_path, marker_conflict

# --- Hypothesis CI example-budget profile --------------------
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

# --- test-kind auto-marking ----------------------------------
#
# A test's kind is its directory: everything under ``tests/<kind>/`` wears
# ``<kind>`` as a marker, applied here rather than by hand, so ``-m tui``
# selects the whole TUI kind without depending on 200 authors remembering
# a decorator. ``TestKind`` is the single declaration and every kind's
# marker is registered in ``[tool.pytest.ini_options]``, which
# ``--strict-markers`` then enforces.
#
# The reverse case -- a file that declares a DIFFERENT kind's marker than
# its directory implies -- is a contradiction, not a preference: the file
# is either misfiled or mislabelled, and silently honouring one side hides
# it. Collection aborts with a usage error naming both sides. The
# pre-taxonomy files that already disagree are grandfathered in
# ``kind_taxonomy.GRANDFATHERED_MARKER_CONFLICTS`` so the contract binds
# new tests without a tree-wide re-file first.


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Apply each item's directory kind as a marker; reject a contradiction.

    Args:
        items: The collected items, mutated in place.

    Raises:
        pytest.UsageError: at least one collected file declares a kind
            marker that contradicts the kind its directory implies.
    """
    conflicts: dict[str, str] = {}
    for item in items:
        path = str(item.path)
        kind = kind_for_test_path(path)
        if kind is None:
            continue
        conflict = marker_conflict(
            path=path,
            marker_names=frozenset(mark.name for mark in item.iter_markers()),
        )
        if conflict is not None:
            conflicts[conflict.path] = conflict.render()
            continue
        item.add_marker(kind.value)
    if conflicts:
        joined = "\n".join(conflicts[path] for path in sorted(conflicts))
        raise pytest.UsageError(f"test-kind marker conflict:\n{joined}")


# --- suite daemon runtime-dir isolation ----------------------
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


# --- repository .ea resolution guard -------------------------
#
# Both state resolvers fall back to a pwd-upward walk, and pytest runs with
# cwd at the repo root -- so a test that reaches a resolver without an
# ``EA_STATE`` override or a ``-w`` workspace gets the REPOSITORY's own
# ``.ea/state.json`` on the first hop and operates on the project's live
# state under the operator's feet. The guard below turns that silent hit
# into a raise at the resolution site.
#
# Only the pwd-upward branch is guarded. An explicit ``EA_STATE`` or an
# explicit workspace argument is a test SAYING it wants that tree, which is
# how the repo-census family (``eawf decision list`` over the committed
# state) legitimately reads real state; the accident this guard exists to
# catch is the fall-through, where nobody chose the path at all.
#
# Rebinding is done by identity sweep over ``sys.modules`` rather than by
# patching the two defining modules: ~50 call sites do
# ``from ... import resolve_state_path`` at module scope, so a
# defining-module patch reaches none of them. The sweep is session-scoped,
# so the per-test cost is zero.
#
# The guard deliberately does NOT witness writes to ``.ea`` on disk: a live
# daemon rewrites ``state.json`` from its own process while the suite runs,
# so a before/after mutation check would red the suite for work no test did.

REPO_ROOT: Path = Path(__file__).resolve().parents[1]
REPO_EA_DIR: Path = REPO_ROOT / ".ea"


class RepoStateAccessError(RuntimeError):
    """Raised when a test resolves a path inside the repository's ``.ea``."""


def guard_repo_ea_path(path: Path, *, origin: str) -> Path:
    """Return *path* unless it resolves inside the repository ``.ea`` tree.

    Containment is tested against fully resolved paths and by exact path
    component equality, so a sibling whose name merely starts with ``.ea``
    -- ``<repo>/.eawf/`` is a real one -- is never flagged.

    Args:
        path: The candidate path a state resolver produced.
        origin: Name of the resolver (or env var) being guarded; quoted in
            the failure message so the offending call site is obvious.

    Returns:
        *path* unchanged when it lies outside the repository ``.ea`` tree.

    Raises:
        TypeError: *path* is not path-like.
        RepoStateAccessError: *path* is the repository ``.ea`` or below it.
    """
    candidate = Path(path)
    resolved = candidate.expanduser().resolve()
    if resolved == REPO_EA_DIR or REPO_EA_DIR in resolved.parents:
        raise RepoStateAccessError(
            f"{origin} resolved to the repository's own state tree "
            f"({resolved}); tests must run against a tmp_path repo. Set "
            f"EA_STATE or pass an explicit workspace."
        )
    return candidate


def _rebind_everywhere(
    monkeypatch: pytest.MonkeyPatch,
    *,
    name: str,
    original: object,
    replacement: object,
) -> None:
    """Rebind every module-level alias of *original* named *name*.

    Walks the imported module table and replaces the attribute wherever it
    is still the identical original object, which covers both the defining
    module and every ``from ... import <name>`` alias.
    """
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, name, None) is original:
            monkeypatch.setattr(module, name, replacement, raising=False)


@pytest.fixture(scope="session", autouse=True)
def repo_ea_guard() -> Iterator[None]:
    """Make either state resolver raise when it lands on the repo's ``.ea``.

    Session-scoped: the guard is stateless, so one rebinding sweep covers
    every test in the worker at no per-test cost.
    """
    from eawf.kernel.state import resolve as state_resolve
    from eawf.surfaces.cli import scope as cli_scope

    inner_with_reason = state_resolve.resolve_with_reason
    inner_state_path = cli_scope.resolve_state_path

    @functools.wraps(inner_with_reason)
    def guarded_with_reason(
        workspace: Path | None,
        env: os._Environ[str] | None = None,
    ) -> tuple[Path, str]:
        path, reason = inner_with_reason(workspace, env)
        if reason == state_resolve.REASON_PWD_UPWARD:
            guard_repo_ea_path(path, origin="resolve_with_reason")
        return path, reason

    @functools.wraps(inner_state_path)
    def guarded_state_path(workspace: Path | None) -> Path:
        path = inner_state_path(workspace)
        # Mirrors the resolver's own precedence: an env override or an
        # explicit workspace means the caller chose this tree on purpose.
        if workspace is None and not os.environ.get("EA_STATE"):
            guard_repo_ea_path(path, origin="resolve_state_path")
        return path

    monkeypatch = pytest.MonkeyPatch()
    _rebind_everywhere(
        monkeypatch,
        name="resolve_with_reason",
        original=inner_with_reason,
        replacement=guarded_with_reason,
    )
    _rebind_everywhere(
        monkeypatch,
        name="resolve_state_path",
        original=inner_state_path,
        replacement=guarded_state_path,
    )
    try:
        yield
    finally:
        monkeypatch.undo()


@pytest.fixture(autouse=True)
def own_runtime_dir(runtime_dir_isolation: RuntimeDirIsolation) -> None:
    """Assert this test still runs under its own isolated ``EAWF_RUNTIME_DIR``.

    Runs at setup, so a predecessor that cleared or repointed the variable
    outside ``monkeypatch`` is caught before the next test can bind a
    daemon socket in the operator's live runtime dir.

    Raises:
        RepoStateAccessError: ``EAWF_RUNTIME_DIR`` is unset, empty, or points
            inside the repository ``.ea`` tree.
    """
    configured = os.environ.get("EAWF_RUNTIME_DIR")
    if not configured:
        raise RepoStateAccessError(
            "EAWF_RUNTIME_DIR is unset: the runtime-dir isolation fixture was "
            "overridden, so this test would bind a daemon in the live ~/.eawfd"
        )
    guard_repo_ea_path(Path(configured), origin="EAWF_RUNTIME_DIR")


@pytest.fixture(scope="session", autouse=True)
def global_config_isolation(runtime_dir_isolation: RuntimeDirIsolation) -> Iterator[Path]:
    """Keep tests independent from the operator's real global EAWF config.

    Individual config-layer tests may monkeypatch ``global_config_path`` again;
    pytest restores those local overrides to this session-level fake.
    """
    from eawf.kernel.config import layered

    isolated_path = runtime_dir_isolation.runtime_dir / "global-config.yaml"
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(layered, "global_config_path", lambda: isolated_path)
    try:
        yield isolated_path
    finally:
        monkeypatch.undo()


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


def make_claim_criterion(criterion_id: str = "CR-CLAIM") -> CriterionSpec:
    """Build one real typed criterion for fixtures that execute a wave."""
    return CriterionSpec(
        id=criterion_id,
        text="the focused lifecycle operation exits with the expected status",
        kind="attested",
        acceptance_style="binary",
        evidence_kind="attested",
        gate_ids=[],
        quality_dimension=QualityDimension.FUNCTIONAL_SUITABILITY,
        measurable_signal="the focused test observes the expected state transition",
        response=ResponseClause(
            observe=ObserveVerb.JUDGED,
            object="the focused lifecycle operation",
            locus=ProofLocus.HUMAN,
            jury_reason="fixture criterion is satisfied by the focused test assertion",
        ),
    )


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """Skeleton fixture for a throwaway repository directory.

    Phase 1+ tests will populate this with the canonical .ea/ skeleton via
    eawf.platform.install. For now it returns a bare temp directory.
    """
    return tmp_path


def make_floor_waiver() -> CriteriaFloorWaiver:
    """Build a typed criteria-floor waiver for legacy-criterion fixtures.

    The plan-time typed-criteria floor rejects a wave
    authored with grandfathered legacy rows; fixtures that deliberately
    model migration-era legacy waves attach this waiver so the modelled
    state stays constructible while the floor stays on for real authoring.
    """
    from datetime import UTC, datetime

    return CriteriaFloorWaiver(
        reason="test fixture models a migration-era legacy wave",
        waived_at=datetime(2026, 7, 2, tzinfo=UTC),
    )
