"""The worker handshake: what a worker claims, and what it earns by it.

A worker process announces itself with a :class:`WorkerHello` carrying
the digests it believes it is running under. The daemon compares them
against the Run's recorded contract and records the comparison as a
:class:`WorkerHelloFact`. Nothing about the comparison is advisory: a
worker whose compiled spec or authority capsule differs from the one the
Run was bound to is running under authority nobody granted it, and the
only safe reading of that is that the installation drifted.

The order is the point. Acceptance is persisted *before* any tool opens,
so the question "was this Run's worker ever accepted?" is answered by a
durable fact rather than by whatever a live process remembers.
:func:`decide_tool_grant` is therefore a refusal by default: with no
accepted hello on the ledger it grants nothing, and it does not matter
how plausible the request looked.

Mismatch answers :data:`RUNTIME_HANDSHAKE_MISMATCH` and names the fields
that differed. It never names the values: a digest comparison that
printed both sides would put the expected digest in a log a drifted
worker can read.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal

from eawf.kernel.runtime.compiled import BoundedText
from eawf.kernel.runtime.provider import (
    AuthKind,
    Digest,
    ModelId,
    ProviderId,
    RuntimeRecord,
    SemVer,
)
from eawf.kernel.runtime.semantic import SemanticToolId
from eawf.kernel.state.epoch2.base import PrincipalKey, StrictPositiveInt
from eawf.kernel.state.epoch2.urns import RunUrn
from eawf.kernel.state.types import UtcDatetime

logger = logging.getLogger(__name__)

#: The refusal a drifted handshake answers with. No tool grant opens
#: behind it.
RUNTIME_HANDSHAKE_MISMATCH: Final = "runtime_handshake_mismatch"

#: The refusal a tool request answers with when no worker was ever
#: accepted for the Run. It is distinct from a mismatch: nothing drifted,
#: nothing was proven either.
RUNTIME_HANDSHAKE_REQUIRED: Final = "runtime_handshake_required"


class HandshakeDisposition(StrEnum):
    """What the daemon decided about one hello."""

    ACCEPTED = "accepted"
    MISMATCHED = "mismatched"


class WorkerHello(RuntimeRecord):
    """What a worker claims about itself when it announces.

    Attributes:
        run_ref: The Run the worker believes it is serving.
        provider_session_ref: The provider's own session handle, when it
            has one.
        driver_manifest_digest: The installed driver manifest it read.
        provider_id: Which provider the worker is.
        sdk_version: The provider SDK or server version actually loaded.
        auth_kind: How the worker authenticated.
        model_id: The model actually selected.
        os_class: The operating system the worker runs on.
        worker_protocol_version: The worker protocol it speaks.
        event_codec_version: The event codec it emits.
        compiled_spec_digest: The compiled spec it believes it holds.
        authority_capsule_digest: The authority capsule it believes it
            holds.
        capabilities_digest: The capability set it resolved.
        hello_sequence: Which announcement this is, counting from one. A
            hello that does not exceed the last recorded one is a replay
            of an older claim and is refused.
    """

    run_ref: RunUrn
    provider_session_ref: BoundedText | None = None
    driver_manifest_digest: Digest
    provider_id: ProviderId
    sdk_version: SemVer
    auth_kind: AuthKind
    model_id: ModelId
    os_class: Literal["linux", "macos", "windows"]
    worker_protocol_version: SemVer
    event_codec_version: SemVer
    compiled_spec_digest: Digest
    authority_capsule_digest: Digest
    capabilities_digest: Digest
    hello_sequence: StrictPositiveInt


class WorkerHelloFact(RuntimeRecord):
    """One append-only line recording a hello and what it earned.

    Attributes:
        payload_kind: The discriminator separating a handshake line from
            a control fact, a Run event and a contract binding.
        run_ref: The Run the handshake belongs to.
        hello: Exactly what the worker claimed.
        disposition: What the daemon decided.
        mismatched_fields: The contract fields that differed, empty on
            an acceptance. Field names only, never their values.
        actor: The principal the daemon attributed the decision to.
        recorded_at: When the daemon appended the line.
    """

    payload_kind: Literal["worker_hello"] = "worker_hello"
    run_ref: RunUrn
    hello: WorkerHello
    disposition: HandshakeDisposition
    mismatched_fields: tuple[str, ...] = ()
    actor: PrincipalKey
    recorded_at: UtcDatetime


@dataclass(frozen=True, slots=True)
class HandshakeOutcome:
    """The comparison of one hello against the Run's recorded contract.

    Attributes:
        disposition: Accepted or mismatched.
        mismatched_fields: Which contract fields differed, in a stable
            order, empty on an acceptance.
        reason: One sentence an operator reads.
    """

    disposition: HandshakeDisposition
    mismatched_fields: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class ToolGrantDecision:
    """Whether a Run's semantic tools may open.

    Attributes:
        granted: The tools that open, empty when the handshake has not
            earned them.
        refusal_code: Why nothing opened, or ``None`` on a grant.
        reason: One sentence an operator reads.
    """

    granted: tuple[SemanticToolId, ...]
    refusal_code: str | None
    reason: str


def assess_worker_hello(
    *,
    hello: WorkerHello,
    run_ref: RunUrn,
    compiled_spec_digest: Digest,
    authority_capsule_digest: Digest,
) -> HandshakeOutcome:
    """Compare *hello* against the contract the Run was bound under.

    Args:
        hello: What the worker claimed.
        run_ref: The Run the daemon is answering for.
        compiled_spec_digest: The compiled spec the Run's binding names.
        authority_capsule_digest: The capsule the Run's binding names.

    Returns:
        The outcome, naming the fields that differed. The comparison is
        over field names only, so a refusal never repeats a digest.
    """
    compared = (
        ("run_ref", hello.run_ref, run_ref),
        ("compiled_spec_digest", hello.compiled_spec_digest, compiled_spec_digest),
        ("authority_capsule_digest", hello.authority_capsule_digest, authority_capsule_digest),
    )
    mismatched = tuple(field for field, claimed, expected in compared if claimed != expected)
    if mismatched:
        logger.info(
            f"assess_worker_hello disposition=mismatched "
            f"fields={','.join(mismatched)} hello_sequence={hello.hello_sequence}"
        )
        return HandshakeOutcome(
            disposition=HandshakeDisposition.MISMATCHED,
            mismatched_fields=mismatched,
            reason=(
                f"the worker announced a contract that differs from this Run's binding in "
                f"{', '.join(mismatched)}, so no tool grant opens"
            ),
        )
    return HandshakeOutcome(
        disposition=HandshakeDisposition.ACCEPTED,
        mismatched_fields=(),
        reason="the worker announced this Run's recorded contract",
    )


def decide_tool_grant(
    *, requested: Sequence[SemanticToolId], facts: Sequence[WorkerHelloFact]
) -> ToolGrantDecision:
    """Decide which of *requested* may open, given the handshakes so far.

    The latest hello decides. An earlier acceptance does not survive a
    later mismatch, because the later one is the claim the live worker
    made and the earlier one describes a process that is no longer the
    one asking.

    Args:
        requested: The capsule's tool grants, in grant order.
        facts: Every handshake fact of the Run, in ledger order.

    Returns:
        The decision. Nothing opens without an accepted hello.
    """
    if not facts:
        return ToolGrantDecision(
            granted=(),
            refusal_code=RUNTIME_HANDSHAKE_REQUIRED,
            reason="no worker has announced this Run, so no tool grant is issued",
        )
    latest = max(facts, key=lambda fact: fact.hello.hello_sequence)
    if latest.disposition is not HandshakeDisposition.ACCEPTED:
        return ToolGrantDecision(
            granted=(),
            refusal_code=RUNTIME_HANDSHAKE_MISMATCH,
            reason=(
                "this Run's latest worker announcement was refused in "
                f"{', '.join(latest.mismatched_fields)}, so no tool grant is issued"
            ),
        )
    granted = tuple(requested)
    logger.info(
        f"decide_tool_grant granted={len(granted)} hello_sequence={latest.hello.hello_sequence}"
    )
    return ToolGrantDecision(
        granted=granted,
        refusal_code=None,
        reason=f"the accepted worker announcement opens {len(granted)} semantic tool(s)",
    )


__all__ = [
    "RUNTIME_HANDSHAKE_MISMATCH",
    "RUNTIME_HANDSHAKE_REQUIRED",
    "HandshakeDisposition",
    "HandshakeOutcome",
    "ToolGrantDecision",
    "WorkerHello",
    "WorkerHelloFact",
    "assess_worker_hello",
    "decide_tool_grant",
]
