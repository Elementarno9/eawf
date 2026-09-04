"""Fixtures shared by the REL-024 crash-and-recovery modules.

The four modules filed here all drive the same setup: a method context
bound to a throwaway state root, an approved ``0.7.0.dev1`` record, and
a probe registry patched green because the default producers all report
``unavailable`` and would otherwise deny ``approval_stale`` before any
external effect is reached.

The two older modules in this directory carry their own copies of these
fixtures and keep them: a module-level fixture shadows the conftest one,
so nothing here changes what they exercise.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from eawf.kernel.release.signals import (
    ReleaseSignalContext,
    ReleaseSignalName,
    ReleaseSignalOutcome,
    ReleaseSignalStatus,
)
from eawf.kernel.spec.publication import PublicationOperation, latest_attempt
from eawf.kernel.spec.release import (
    Release,
    ReleaseChannel,
    ReleaseStatus,
    ReleaseTargetStatus,
)
from eawf.kernel.spec.release_config import ReleaseConfig, load_release_config
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.methods import MethodContext
from eawf.workflow.release.ledger import record_operation, request_fingerprint
from eawf.workflow.release.observation import FrozenManifest
from eawf.workflow.release.target_machine import advance_target_attempt
from eawf.workflow.release.train import DEV1_RELEASE_CONFIG_YAML, V07_TRAIN

#: Recorded registry answers and the frozen manifest they are judged
#: against, shared with the unit-tier adapter fixtures.
FIXTURES = Path(__file__).parents[4] / "fixtures" / "release" / "observations"

#: The recorded-response stem of each configured leg's adapter.
ADAPTER_STEMS = {"pypi": "package_index", "npm": "npm_registry", "github": "source_host_release"}

SOURCE_SHA = "a" * 40
TREE_SHA = "b" * 40
PROOF_DIGEST = f"sha256:{'1' * 64}"
EFFECT = "receipt://target/effect"
RELEASE_KEY = "REL-0.7.0.dev1"

#: Revision the approved record starts at, and therefore the
#: ``expected_revision`` the first publication verb must present.
APPROVED_REVISION = 4


def run_async(body: Callable[[], Coroutine[Any, Any, None]]) -> None:
    """Drive one coroutine test body to completion."""
    asyncio.run(body())


def dev1_config() -> ReleaseConfig:
    """Return the authored dev1 checkpoint configuration."""
    return load_release_config(DEV1_RELEASE_CONFIG_YAML, train=V07_TRAIN)


def manifest_payload() -> dict[str, Any]:
    """Return the committed ``0.7.0.dev1`` frozen manifest as JSON."""
    decoded: dict[str, Any] = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    return decoded


def manifest_digest() -> str:
    """Return the digest the frozen manifest recomputes to."""
    return FrozenManifest.model_validate(manifest_payload()).digest


def response_payload(target_id: str, case: str) -> dict[str, Any]:
    """Return the recorded registry answer for *target_id* in *case*.

    Args:
        target_id: Publication target whose adapter recorded it.
        case: Fixture case stem, e.g. ``match`` or ``mismatch``.

    Returns:
        The recorded answer, as JSON.
    """
    path = FIXTURES / f"{ADAPTER_STEMS[target_id]}-{case}.json"
    decoded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return decoded


def approved_payload(**overrides: Any) -> dict[str, Any]:
    """Return a serialized APPROVED ``0.7.0.dev1`` record."""
    record = Release(
        uid=UUID(int=33),
        key=RELEASE_KEY,
        version="0.7.0.dev1",
        channel=ReleaseChannel.DEV,
        authority_epoch=1,
        status=ReleaseStatus.APPROVED,
        approval_ref="receipt://approval/dev1",
        source_sha=SOURCE_SHA,
        source_tree_sha=TREE_SHA,
        manifest_ref="artifact://release/manifest",
        manifest_digest=manifest_digest(),
        revision=APPROVED_REVISION,
    )
    return {**record.model_dump(mode="json"), **overrides}


def publish_params(**overrides: Any) -> dict[str, Any]:
    """Return well-formed ``release.publish`` params."""
    params: dict[str, Any] = {
        "release": approved_payload(),
        "expected_revision": APPROVED_REVISION,
        "idempotency_key": "publish-0.7.0.dev1-33",
        "approved_manifest_digest": manifest_digest(),
        "proof_digest": PROOF_DIGEST,
    }
    params.update(overrides)
    return params


def moved(payload: dict[str, Any], status: ReleaseStatus) -> dict[str, Any]:
    """Return *payload* at *status*, one revision on.

    Args:
        payload: A serialized release record.
        status: The status the caller drives it to out of band.

    Returns:
        The successor payload, so the next verb's ``expected_revision``
        is the one the record now carries.
    """
    return {**payload, "status": status.value, "revision": payload["revision"] + 1}


def record_snapshot(
    ctx: MethodContext,
    operation: PublicationOperation,
    *,
    key: str,
) -> PublicationOperation:
    """Make *operation* the current snapshot of the release in the ledger.

    Args:
        ctx: Method context bound to the state root.
        operation: The snapshot to append.
        key: Idempotency key, which becomes the envelope id.

    Returns:
        The snapshot as recorded.
    """
    return record_operation(
        Path(str(ctx.state_path)),
        operation,
        idempotency_key=key,
        fingerprint=request_fingerprint("test.seed", {"key": key}),
        recorded_at=datetime.now(UTC),
        summary=f"seed {key}",
    )


class CountingPublisher:
    """A publisher stand-in that counts the external calls made to it.

    No adapter uploads anything in this distribution, so the external
    effect a duplicate publication would repeat is modelled here: one
    call per dispatched attempt row. ``__call__`` returns the running
    count, so a second call for a row cannot answer what the first did.

    Attributes:
        dispatched: The ``(target_id, attempt)`` pairs called, in order.
    """

    def __init__(self) -> None:
        """Start with nothing dispatched."""
        self.dispatched: list[tuple[str, int]] = []

    def __call__(self, *, target_id: str, attempt: int) -> int:
        """Record one external call and return the running total."""
        self.dispatched.append((target_id, attempt))
        return len(self.dispatched)

    def calls_for(self, target_id: str) -> int:
        """Return how many external calls *target_id* has taken."""
        return sum(1 for called, _attempt in self.dispatched if called == target_id)

    def per_target(self) -> dict[str, int]:
        """Return the call count of every target dispatched so far."""
        return {called: self.calls_for(called) for called, _attempt in self.dispatched}


def dispatch_queued(
    operation: PublicationOperation,
    publisher: CountingPublisher,
) -> PublicationOperation:
    """Call *publisher* once per queued leg and move each into flight.

    The ledger is the deduplication: a row already in flight or settled
    is not re-dispatched, so a resume that replays the ledger makes no
    second external call. Nothing here consults a side table of "what
    did we already send" -- that table is the thing that goes stale.

    Args:
        operation: The snapshot replayed from the ledger.
        publisher: The counting stand-in for the external call.

    Returns:
        The successor with every queued leg dispatched.
    """
    for target in dev1_config().targets:
        row = latest_attempt(operation, target.target_id)
        if row is None or row.status is not ReleaseTargetStatus.QUEUED:
            continue
        publisher(target_id=target.target_id, attempt=row.attempt)
        operation = advance_target_attempt(
            operation, target=target, to=ReleaseTargetStatus.IN_FLIGHT, now=row.started_at
        )
    return operation


@pytest.fixture
def green_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every readiness signal pass so the chokepoint recomputes green."""

    def passing(context: ReleaseSignalContext) -> ReleaseSignalOutcome:
        return ReleaseSignalOutcome(status=ReleaseSignalStatus.PASS, remediation="")

    monkeypatch.setattr(
        "eawf.workflow.verify.release_readiness.DEFAULT_RELEASE_PROBES",
        dict.fromkeys(ReleaseSignalName, passing),
    )


@pytest.fixture
def ctx(tmp_path: Path) -> MethodContext:
    """Return a method context bound to a tmp state root."""
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    state_path = state_dir / "state.json"
    state_path.write_text(json.dumps({}), encoding="utf-8")
    return MethodContext(
        started_at=datetime.now(UTC).isoformat(),
        pid=4242,
        protocol_version=PROTOCOL_VERSION,
        version="test",
        state_path=state_path,
    )
