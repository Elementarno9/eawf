"""REL-024: a crash on either side of every registered durable boundary.

Under test: for each boundary in
:data:`~eawf.workflow.release.boundaries.IN_PACKAGE_BOUNDARIES`, a crash
injected *before* the durable write leaving no effect and a crash
injected *after* it leaving exactly one; the resume replaying from the
ledger rather than repeating the call; and the same pair around the one
external boundary, ``tag_push``, driven against a real local remote.

The proof that a resume is not a second publication is a counting
publisher: it records every ``(target_id, attempt)`` it is called with,
and the tests assert no pair is ever called twice. That is the property
a duplicate publication violates, and it is checked rather than argued
-- a dispatcher that consulted a side table of "already sent" would pass
an argument and fail this.

The crash is injected at the ledger append itself
(:func:`~eawf.workflow.release.ledger.record_operation` is the shared
durable write of the four in-package boundaries), so "before" and
"after" are the two sides of the exact instant the effect becomes
durable, not two sides of an approximation of it.
"""

from __future__ import annotations

import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.spec.publication import PublicationOperation, require_attempt
from eawf.kernel.spec.release import ReleaseStatus, ReleaseTargetStatus
from eawf.kernel.store.append import append_envelope
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.release import observe, publish, reconcile, retry_target
from eawf.surfaces.cli.commands.release import _create_and_push_tag
from eawf.workflow.release.boundaries import IN_PACKAGE_BOUNDARIES, PublicationBoundary
from eawf.workflow.release.ledger import current_operation, ledger_path
from eawf.workflow.release.observation import configured_target
from eawf.workflow.release.target_machine import advance_target_attempt
from tests.integration.runtime.daemon.methods.conftest import (
    EFFECT,
    RELEASE_KEY,
    CountingPublisher,
    dev1_config,
    dispatch_queued,
    manifest_payload,
    moved,
    publish_params,
    record_snapshot,
    response_payload,
    run_async,
)

pytestmark = pytest.mark.integration

TARGET = "pypi"
TAG = "v0.7.0.dev1"


class SimulatedCrashError(RuntimeError):
    """The process dying at a durable-effect boundary."""


# --- the in-package boundaries -------------------------------------------


@dataclass(frozen=True)
class Scenario:
    """One boundary's RPC, set up so a crash can land on either side.

    Attributes:
        verb: The ``release.*`` handler that crosses the boundary.
        params: Params to present, unchanged, on both the crashed call
            and the resume -- a resume that changed them would be a
            different request wearing the same idempotency key.
    """

    verb: Callable[[MethodContext, dict[str, Any]], Awaitable[dict[str, Any]]]
    params: dict[str, Any]


class LedgerCrasher:
    """Kills the process at the ledger append, on either side of it.

    Attributes:
        when: ``"before"``, ``"after"`` or ``None`` to pass through.
        appended: How many envelopes actually reached the file.
    """

    def __init__(self, real: Callable[..., None]) -> None:
        """Wrap the real append, disarmed."""
        self.real = real
        self.when: str | None = None
        self.appended = 0

    def __call__(self, path: Path, envelope: Any) -> None:
        """Append, crash, or crash after appending."""
        if self.when == "before":
            raise SimulatedCrashError(f"crash before the durable write to {path.name}")
        self.real(path, envelope)
        self.appended += 1
        if self.when == "after":
            raise SimulatedCrashError(f"crash after the durable write to {path.name}")


@pytest.fixture
def crash(monkeypatch: pytest.MonkeyPatch) -> LedgerCrasher:
    """Return the disarmed crasher, installed over the ledger append."""
    crasher = LedgerCrasher(append_envelope)
    monkeypatch.setattr("eawf.workflow.release.ledger.append_envelope", crasher)
    return crasher


def ledger_lines(ctx: MethodContext) -> int:
    """Return how many envelopes the publication ledger carries."""
    path = ledger_path(Path(str(ctx.state_path)))
    if not path.exists():
        return 0
    return len([line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()])


def _dispatched(result: dict[str, Any], publisher: CountingPublisher) -> PublicationOperation:
    """Dispatch every queued leg of the just-published operation."""
    return dispatch_queued(PublicationOperation.model_validate(result["operation"]), publisher)


def _settled(
    operation: PublicationOperation,
    status: ReleaseTargetStatus,
) -> PublicationOperation:
    """Settle *TARGET* on *status* at its own deadline."""
    return advance_target_attempt(
        operation,
        target=configured_target(dev1_config(), TARGET),
        to=status,
        now=require_attempt(operation, TARGET).deadline_at,
        effect_receipt_ref=None if status is ReleaseTargetStatus.UNKNOWN else EFFECT,
    )


async def _operation_open(ctx: MethodContext, publisher: CountingPublisher) -> Scenario:
    """The episode opening: nothing durable exists yet."""
    return Scenario(publish, publish_params(idempotency_key="crash-publish-33"))


async def _target_dispatch(ctx: MethodContext, publisher: CountingPublisher) -> Scenario:
    """A timed-out leg being re-queued for a second attempt."""
    result = await publish(ctx, publish_params())
    operation = _settled(_dispatched(result, publisher), ReleaseTargetStatus.UNKNOWN)
    record_snapshot(ctx, operation, key="seed-unknown-crash-33")
    recovering = moved(result["release"], ReleaseStatus.RECOVERING)
    return Scenario(
        retry_target,
        {
            "release": recovering,
            "expected_revision": recovering["revision"],
            "idempotency_key": "crash-retry-33",
            "target_id": TARGET,
            "proof_digest": operation.proof_digest,
        },
    )


async def _effect_receipt_write(ctx: MethodContext, publisher: CountingPublisher) -> Scenario:
    """An adapter's own word arriving late on a dispatched leg."""
    result = await publish(ctx, publish_params())
    record_snapshot(ctx, _dispatched(result, publisher), key="seed-in-flight-crash-33")
    return Scenario(
        reconcile,
        {
            "release": result["release"],
            "expected_revision": result["release"]["revision"],
            "idempotency_key": "crash-reconcile-33",
            "target_id": TARGET,
            "status": ReleaseTargetStatus.REPORTED_SUCCESS.value,
            "effect_receipt_ref": EFFECT,
        },
    )


async def _observed(
    ctx: MethodContext,
    publisher: CountingPublisher,
    *,
    case: str,
    key: str,
) -> Scenario:
    """A read-back settling a leg that already reported success."""
    result = await publish(ctx, publish_params())
    operation = _settled(_dispatched(result, publisher), ReleaseTargetStatus.REPORTED_SUCCESS)
    record_snapshot(ctx, operation, key=f"seed-reported-{case}-33")
    return Scenario(
        observe,
        {
            "release": result["release"],
            "expected_revision": result["release"]["revision"],
            "idempotency_key": key,
            "target_id": TARGET,
            "manifest": manifest_payload(),
            "response": response_payload(TARGET, case),
        },
    )


async def _observation_receipt_write(ctx: MethodContext, publisher: CountingPublisher) -> Scenario:
    """A matching read-back writing its receipt onto the leg."""
    return await _observed(ctx, publisher, case="match", key="crash-observe-33")


async def _transition_apply(ctx: MethodContext, publisher: CountingPublisher) -> Scenario:
    """A contradicted read-back moving the release into RECOVERING."""
    return await _observed(ctx, publisher, case="mismatch", key="crash-recover-33")


#: The RPC that crosses each in-package boundary. Parametrizing the
#: crash tests over this mapping is what makes "for every registered
#: boundary" a claim the suite checks rather than one it asserts: a
#: boundary added to the enum with no entry here reds
#: :func:`test_every_in_package_boundary_has_a_crash_scenario`.
SCENARIOS: dict[
    PublicationBoundary,
    Callable[[MethodContext, CountingPublisher], Awaitable[Scenario]],
] = {
    PublicationBoundary.OPERATION_OPEN: _operation_open,
    PublicationBoundary.TARGET_DISPATCH: _target_dispatch,
    PublicationBoundary.EFFECT_RECEIPT_WRITE: _effect_receipt_write,
    PublicationBoundary.OBSERVATION_RECEIPT_WRITE: _observation_receipt_write,
    PublicationBoundary.TRANSITION_APPLY: _transition_apply,
}

BOUNDARIES = sorted(IN_PACKAGE_BOUNDARIES)


def test_every_in_package_boundary_has_a_crash_scenario() -> None:
    assert set(SCENARIOS) == IN_PACKAGE_BOUNDARIES


@pytest.mark.parametrize("boundary", BOUNDARIES)
def test_a_crash_before_the_durable_write_leaves_no_effect(
    ctx: MethodContext,
    green_probes: None,
    crash: LedgerCrasher,
    boundary: PublicationBoundary,
) -> None:
    async def body() -> None:
        publisher = CountingPublisher()
        scenario = await SCENARIOS[boundary](ctx, publisher)
        before = ledger_lines(ctx)

        crash.when = "before"
        with pytest.raises(SimulatedCrashError):
            await scenario.verb(ctx, scenario.params)
        assert ledger_lines(ctx) == before

        crash.when = None
        result = await scenario.verb(ctx, scenario.params)
        assert result["replayed"] is False
        assert ledger_lines(ctx) == before + 1

    run_async(body)


@pytest.mark.parametrize("boundary", BOUNDARIES)
def test_a_crash_after_the_durable_write_leaves_exactly_one_effect(
    ctx: MethodContext,
    green_probes: None,
    crash: LedgerCrasher,
    boundary: PublicationBoundary,
) -> None:
    async def body() -> None:
        publisher = CountingPublisher()
        scenario = await SCENARIOS[boundary](ctx, publisher)
        before = ledger_lines(ctx)

        crash.when = "after"
        with pytest.raises(SimulatedCrashError):
            await scenario.verb(ctx, scenario.params)
        assert ledger_lines(ctx) == before + 1

        crash.when = None
        result = await scenario.verb(ctx, scenario.params)
        assert result["replayed"] is True
        assert ledger_lines(ctx) == before + 1

    run_async(body)


@pytest.mark.parametrize("boundary", BOUNDARIES)
def test_resume_from_the_ledger_completes_the_leg_without_a_duplicate_call(
    ctx: MethodContext,
    green_probes: None,
    crash: LedgerCrasher,
    boundary: PublicationBoundary,
) -> None:
    async def body() -> None:
        publisher = CountingPublisher()
        scenario = await SCENARIOS[boundary](ctx, publisher)

        crash.when = "after"
        with pytest.raises(SimulatedCrashError):
            await scenario.verb(ctx, scenario.params)
        crash.when = None
        replayed = await scenario.verb(ctx, scenario.params)
        assert replayed["replayed"] is True

        state_path = Path(str(ctx.state_path))
        recovered = current_operation(state_path, RELEASE_KEY)
        assert recovered is not None
        resumed = dispatch_queued(recovered, publisher)
        record_snapshot(ctx, resumed, key=f"resume-{boundary.value}-33")
        after_resume = publisher.per_target()

        replayed_again = current_operation(state_path, RELEASE_KEY)
        assert replayed_again is not None
        dispatch_queued(replayed_again, publisher)

        assert publisher.per_target() == after_resume
        assert len(set(publisher.dispatched)) == len(publisher.dispatched)

    run_async(body)


@pytest.mark.parametrize("boundary", BOUNDARIES)
def test_the_crash_is_injected_at_the_durable_write_itself(
    ctx: MethodContext,
    green_probes: None,
    crash: LedgerCrasher,
    boundary: PublicationBoundary,
) -> None:
    async def body() -> None:
        publisher = CountingPublisher()
        scenario = await SCENARIOS[boundary](ctx, publisher)
        appended = crash.appended

        crash.when = "before"
        with pytest.raises(SimulatedCrashError, match="crash before the durable write"):
            await scenario.verb(ctx, scenario.params)
        assert crash.appended == appended

        crash.when = "after"
        with pytest.raises(SimulatedCrashError, match="crash after the durable write"):
            await scenario.verb(ctx, scenario.params)
        assert crash.appended == appended + 1

    run_async(body)


# --- the external boundary: the tag push ---------------------------------


def _git(args: list[str], cwd: Path) -> str:
    """Run one git command in *cwd* and return its stdout."""
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


class PushCrasher:
    """Kills the process at ``git push``, on either side of it.

    Attributes:
        when: ``"before"``, ``"after"`` or ``None`` to pass through.
        pushes: How many pushes actually reached the remote.
    """

    def __init__(self, real: Callable[..., Any]) -> None:
        """Wrap the real runner, disarmed."""
        self.real = real
        self.when: str | None = None
        self.pushes = 0

    def __call__(self, argv: list[str], *args: Any, **kwargs: Any) -> Any:
        """Pass non-push git commands through; crash around a push."""
        if argv[:2] != ["git", "push"]:
            return self.real(argv, *args, **kwargs)
        if self.when == "before":
            raise SimulatedCrashError("crash before the tag reaches the remote")
        result = self.real(argv, *args, **kwargs)
        self.pushes += 1
        if self.when == "after":
            raise SimulatedCrashError("crash after the tag reached the remote")
        return result


@pytest.fixture
def tagged_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Return a work repo tracking a bare remote, with the cwd set to it."""
    work = tmp_path / "work"
    work.mkdir()
    _git(["init", "-b", "main"], work)
    _git(["config", "user.email", "test@example.invalid"], work)
    _git(["config", "user.name", "Test"], work)
    (work / "f.txt").write_text("x\n", encoding="utf-8")
    _git(["add", "f.txt"], work)
    _git(["commit", "-m", "init"], work)
    bare = tmp_path / "remote.git"
    _git(["init", "--bare", str(bare)], tmp_path)
    _git(["remote", "add", "origin", str(bare)], work)
    _git(["push", "origin", "main"], work)
    monkeypatch.chdir(work)
    return work, bare


def remote_tags(bare: Path) -> list[str]:
    """Return every tag the remote carries."""
    return _git(["tag", "-l"], bare).split()


@pytest.fixture
def push_crash(tagged_repo: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> PushCrasher:
    """Return the disarmed push crasher, installed over ``subprocess.run``.

    Ordered after :func:`tagged_repo` so the repo's own setup commands
    run against the real runner, and installed once so re-arming it does
    not stack a second crasher on top of the first.
    """
    crasher = PushCrasher(subprocess.run)
    monkeypatch.setattr(subprocess, "run", crasher)
    return crasher


def test_a_crash_before_the_tag_push_leaves_no_tag_on_the_remote(
    tagged_repo: tuple[Path, Path], push_crash: PushCrasher
) -> None:
    work, bare = tagged_repo
    push_crash.when = "before"
    with pytest.raises(SimulatedCrashError, match="before the tag reaches"):
        _create_and_push_tag(tag=TAG, remote="origin", push=True, force=False)
    assert push_crash.pushes == 0
    assert remote_tags(bare) == []
    assert _git(["tag", "-l"], work).split() == [TAG]


def test_a_crash_after_the_tag_push_leaves_exactly_one_tag(
    tagged_repo: tuple[Path, Path], push_crash: PushCrasher
) -> None:
    _work, bare = tagged_repo
    push_crash.when = "after"
    with pytest.raises(SimulatedCrashError, match="after the tag reached"):
        _create_and_push_tag(tag=TAG, remote="origin", push=True, force=False)
    assert push_crash.pushes == 1
    assert remote_tags(bare) == [TAG]


def test_resuming_the_tag_push_leaves_the_remote_with_exactly_one_tag(
    tagged_repo: tuple[Path, Path], push_crash: PushCrasher
) -> None:
    _work, bare = tagged_repo
    push_crash.when = "after"
    with pytest.raises(SimulatedCrashError):
        _create_and_push_tag(tag=TAG, remote="origin", push=True, force=False)

    push_crash.when = None
    assert _create_and_push_tag(tag=TAG, remote="origin", push=True, force=True) is True
    assert push_crash.pushes == 2
    assert remote_tags(bare) == [TAG]


def test_a_tag_that_is_never_pushed_leaves_the_remote_empty(
    tagged_repo: tuple[Path, Path], push_crash: PushCrasher
) -> None:
    work, bare = tagged_repo
    assert _create_and_push_tag(tag=TAG, remote="origin", push=False, force=False) is False
    assert push_crash.pushes == 0
    assert remote_tags(bare) == []
    assert _git(["tag", "-l"], work).split() == [TAG]


def test_re_creating_an_existing_tag_without_force_is_refused(
    tagged_repo: tuple[Path, Path], push_crash: PushCrasher
) -> None:
    _work, bare = tagged_repo
    _create_and_push_tag(tag=TAG, remote="origin", push=True, force=False)
    with pytest.raises(subprocess.CalledProcessError):
        _create_and_push_tag(tag=TAG, remote="origin", push=True, force=False)
    assert push_crash.pushes == 1
    assert remote_tags(bare) == [TAG]
