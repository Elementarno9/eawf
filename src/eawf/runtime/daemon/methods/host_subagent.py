"""The ``runtime.host.subagent.*`` verbs: a subagent the host harness spawned, as a Run.

The host harness has its own way to fan work out, and a subagent it starts would
otherwise be invisible to Eawf: no Run, no Activity row, no transcript. These two verbs
adopt it. The start and stop hooks the plugin subscribes to call them, so harness-side
and daemon-side fan-out land in one Run register and one Activity view instead of two.

``start`` admits the subagent as a Run and starts it. The scope is typed and is not a
Task's: the subagent was spawned by the harness, not dispatched against a Task, so it
is a repository-scoped Run observing the repository it runs in, and it writes nothing
Eawf integrates. The Run's vendor session is the subagent's own id, so its counters
are read against that session. When the spawning session is itself the vendor session
of exactly one live Run, that Run is recorded as the parent, which is the delegation
lineage a subtree is counted over. The subagent is already running when it is adopted,
so one that takes its subtree past a ``child_runs`` ceiling is admitted and the breach
is filed on the run ledger rather than refused: refusing the record would hide the
subagent, not stop it.

When a parent is recorded, the delegation is also stated on the parent's own stream:
``start`` appends ``child_run_started`` and ``stop`` appends ``child_run_terminal``, both
naming the child Run, so the parent's transcript draws the subagent as work happening
elsewhere with a typed event behind it rather than an inference. The parent's stream has
other writers, so these lines take its tail under the Run's lock.

``stop`` bridges the subagent's transcript into the Run's own stream, then completes
the Run. The stop hook is the harness saying the subagent has returned its final
message, and that message is on the bridged stream, so the completion presents the
bound report the edge requires.

Both verbs are idempotent on the subagent's id. The create, the start and the stop
each carry a key derived from it, a retried hook replays the receipt it already
earned, and a stop whose start never arrived adopts the subagent first.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from eawf.kernel.identity import EntityKind, QualifiedUrn, format_entity_key, parse_qualified_urn
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.runtime.control import TERMINAL_RUN_STATUSES
from eawf.kernel.runtime.events import ChildRunPayload, MessageSummaryPayload, RunEventKind
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import StrictNonNegativeInt
from eawf.kernel.state.epoch2.measurement import VendorSessionRef
from eawf.kernel.state.epoch2.run import (
    RepositoryScope,
    Run,
    RunCreateSpec,
    RunPurpose,
    RunStatus,
)
from eawf.kernel.state.epoch2.transitions import ObservedFact
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.epoch2_create import CreateRequest, run_create
from eawf.runtime.daemon.epoch2_recovery import (
    PROJECTION_DEGRADED,
    publish_projection,
    read_idempotency_receipt,
)
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import (
    CANONICAL_SEQUENCE_KEY,
    MutationReceipt,
    TransactionRefusalCode,
    TransactionRefusedError,
    TransitionRequest,
    run_transaction,
)
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.run import append_run_event
from eawf.runtime.daemon.native_dispatch import run_ledger, stored_run
from eawf.runtime.daemon.native_guard import native_mutator, native_params
from eawf.runtime.daemon.run_capture_updates import bind_run_capture
from eawf.runtime.daemon.run_events import RunEventAppend
from eawf.runtime.runtimes.host_transcript import HostHarness, read_host_transcript
from eawf.runtime.session.vendor_id import hash_vendor_session_id

logger = logging.getLogger(__name__)

#: Adopt a host subagent as a Run and start it.
HOST_SUBAGENT_START_METHOD: Final = "runtime.host.subagent.start"

#: Bridge a host subagent's transcript into its Run and complete it.
HOST_SUBAGENT_STOP_METHOD: Final = "runtime.host.subagent.stop"

#: The principal each harness's adoptions are attributed to.
HARNESS_ACTORS: Final[Mapping[str, str]] = {
    "claude-code": "HARNESS-CLAUDE-CODE",
    "codex": "HARNESS-CODEX",
}

#: How many times a create is re-decided after another writer moved the tree's
#: cursor first. Parallel subagents start together, so losing that race once is
#: ordinary; losing it every time means the tree is busier than a hook should wait.
_CREATE_ATTEMPTS: Final = 3

_RUN_KEY: Final = re.compile(r"^RUN-(\d{8})$")

_HostId = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256)]


class HostSubagentStart(BaseModel):
    """The strict parameters of a subagent start the host reported.

    Attributes:
        harness: The harness that spawned the subagent.
        agent_id: The harness's own id for the subagent.
        host_session_id: The session that spawned it, when the hook named one.
    """

    model_config = ConfigDict(extra="forbid")

    harness: HostHarness
    agent_id: _HostId
    host_session_id: _HostId | None = None


class HostSubagentStop(HostSubagentStart):
    """The strict parameters of a subagent stop the host reported.

    Attributes:
        transcript_path: The subagent's own transcript, when the hook named it.
    """

    transcript_path: Annotated[str, StringConstraints(strict=True, min_length=1)] | None = None


class HostSubagentAnswer(BaseModel):
    """What an adoption verb answers with.

    Attributes:
        run_ref: The Run the subagent is adopted as.
        parent_run_ref: The Run that delegated it, when one was resolved.
        status: The Run's status once the verb is done.
        bridged_events: How many transcript payloads the stop bridged.
        reason: One sentence an operator reads.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_ref: str
    parent_run_ref: str | None
    status: RunStatus
    bridged_events: StrictNonNegativeInt = 0
    reason: str


@dataclasses.dataclass(frozen=True, slots=True)
class _Adoption:
    """The keys every write of one adoption is named by, all derived from the subagent."""

    params: HostSubagentStart
    session: VendorSessionRef
    body: str

    @classmethod
    def of(cls, params: HostSubagentStart) -> _Adoption:
        session = VendorSessionRef(harness=params.harness, session_digest=params.agent_id)
        return cls(
            params=params, session=session, body=session.session_digest.removeprefix("vsid-")
        )

    @property
    def actor(self) -> str:
        return HARNESS_ACTORS[self.params.harness]

    def key(self, step: str) -> str:
        return f"host-subagent-{self.body}-{step}"

    def event_ref(self, index: int | str) -> str:
        return f"EVT-{hashlib.sha256(f'{self.body}:{index}'.encode()).hexdigest()[:32]}"

    @property
    def delegation_ref(self) -> str:
        """Return the delegation this subagent answers, named from the subagent itself."""
        digest = hashlib.sha256(self.params.agent_id.encode()).hexdigest()[:32]
        return f"delegation://{self.params.harness}/{digest}"


def _document_path(authority: RootAuthority) -> Path:
    """Return the selected generation's document of a fence-cleared tree."""
    target, generation_id = authority.target, authority.generation_id
    assert target is not None, "an epoch-2 answer always carries its target"
    assert generation_id is not None, "an epoch-2 answer always names a generation"
    return target.generation_path(generation_id) / GENERATION_DOCUMENT


def _repository_urn(document: dict[str, Any]) -> QualifiedUrn:
    """Return the one repository this tree admits, which a host Run observes.

    Raises:
        DaemonValidationError: The tree admits no repository, or several, so
            which one the harness is running in cannot be read off the tree.
    """
    rows = document_rows(document, Epoch2Collection.REPOSITORY)
    if len(rows) != 1:
        raise DaemonValidationError(
            f"validation_failed: identity_not_found: the tree admits {len(rows)} "
            "repositories, so the repository a host subagent runs in is not one row"
        )
    (row,) = rows.values()
    return parse_qualified_urn(row["urn"])


def _next_run_key(document: dict[str, Any], ledger: Path) -> str:
    """Return the Run key one past every key the document and the run ledger hold."""
    held = set(document_rows(document, Epoch2Collection.RUN))
    held.update(record.record_key for record in read_ledger_records(ledger))
    ordinals = [int(match.group(1)) for key in held if (match := _RUN_KEY.match(key))]
    return format_entity_key(EntityKind.RUN, max(ordinals, default=0) + 1)


def _parent_of(document: dict[str, Any], host_session_id: str | None) -> QualifiedUrn | None:
    """Return the one live Run whose vendor session spawned the subagent, if exactly one.

    Several live Runs may share one vendor session; the spawn cannot then be
    attributed to any one of them, so no parent is recorded rather than a guess.
    """
    if host_session_id is None:
        return None
    digest = hash_vendor_session_id(host_session_id)
    live = [
        row["urn"]
        for row in document_rows(document, Epoch2Collection.RUN).values()
        if row.get("status") not in {status.value for status in TERMINAL_RUN_STATUSES}
        and isinstance(row.get("vendor_session"), Mapping)
        and row["vendor_session"].get("session_digest") == digest
    ]
    return parse_qualified_urn(live[0]) if len(live) == 1 else None


def _adopted_urn(context: Epoch2RootContext, adoption: _Adoption) -> QualifiedUrn | None:
    """Return the Run an earlier call already adopted the subagent as, if one did."""
    namespaced = context.idempotency_key(adoption.key("adopt"))
    stored = read_idempotency_receipt(context, namespaced_key=namespaced)
    if stored is None:
        return None
    return MutationReceipt.model_validate(stored.receipt).entity_ref


def _create(
    context: Epoch2RootContext,
    authority: RootAuthority,
    adoption: _Adoption,
    *,
    now: datetime,
    envelopes: list[Envelope],
) -> QualifiedUrn:
    """Admit the subagent as a queued, repository-scoped Run.

    Raises:
        TransactionRefusedError: The tree kept moving under every attempt, or
            the create was refused for any other reason.
    """
    document_path = _document_path(authority)
    for attempt in range(1, _CREATE_ATTEMPTS + 1):
        document = read_document(document_path)
        repository = _repository_urn(document)
        key = _next_run_key(document, ledger_path(document_path, Epoch2Collection.RUN))
        urn = dataclasses.replace(repository, kind=EntityKind.RUN, entity_key=key)
        parent = _parent_of(document, adoption.params.host_session_id)
        spec = RunCreateSpec(
            key=key,
            scope=RepositoryScope(
                scope_kind="repository",
                repository_ref=repository,
                purpose=RunPurpose.OBSERVE,
            ),
            parent_run_ref=parent,
        )
        request = CreateRequest(
            urn=urn,
            expected_revision=document.get(CANONICAL_SEQUENCE_KEY, 0),
            idempotency_key=adoption.key("adopt"),
            actor=adoption.actor,
            spec=spec.model_dump(mode="json"),
        )
        try:
            committed = run_create(context=context, request=request, now=now, over_ceiling="record")
        except TransactionRefusedError as refusal:
            if refusal.code is not TransactionRefusalCode.REVISION_CONFLICT:
                raise
            logger.info(f"host subagent create raced attempt={attempt} key={key}")
            continue
        if committed.envelope is not None:
            envelopes.append(committed.envelope)
        return urn
    raise TransactionRefusedError(
        code=TransactionRefusalCode.REVISION_CONFLICT,
        detail=f"the tree's cursor moved under {_CREATE_ATTEMPTS} create attempts in a row",
        entity_ref=adoption.key("adopt"),
        remediation="Re-deliver the subagent's start hook once the tree is quieter.",
    )


def _move(
    context: Epoch2RootContext,
    run: Run,
    adoption: _Adoption,
    *,
    to_status: RunStatus,
    step: str,
    updates: dict[str, Any],
    observations: tuple[ObservedFact, ...] = (),
    envelopes: list[Envelope],
) -> None:
    """Take one Run edge, with the counter readings the edge records."""
    captured = bind_run_capture(context, urn=run.urn, to_status=to_status, updates=updates)
    committed = run_transaction(
        context=context,
        request=TransitionRequest(
            urn=run.urn,
            to_status=to_status.value,
            expected_revision=run.revision,
            idempotency_key=adoption.key(step),
            actor=adoption.actor,
            observations=observations,
            updates=captured,
        ),
        now=datetime.now(UTC),
    )
    if committed.envelope is not None:
        envelopes.append(committed.envelope)


def _read_run(context: Epoch2RootContext, urn: QualifiedUrn) -> Run:
    """Return the Run as whichever tier holds it."""
    with context.session([urn]) as session:
        return stored_run(session, read_ledger_records(run_ledger(session)), urn)


def _adopt(
    context: Epoch2RootContext,
    authority: RootAuthority,
    adoption: _Adoption,
    *,
    now: datetime,
    envelopes: list[Envelope],
) -> Run:
    """Return the subagent's Run, admitted and started if it was not already."""
    urn = _adopted_urn(context, adoption) or _create(
        context, authority, adoption, now=now, envelopes=envelopes
    )
    run = _read_run(context, urn)
    _state_delegation(context, run, adoption, phase="started")
    if run.status is RunStatus.QUEUED:
        _move(
            context,
            run,
            adoption,
            to_status=RunStatus.RUNNING,
            step="start",
            updates={
                "started_at": now.isoformat(),
                "vendor_session": adoption.session.model_dump(mode="json"),
            },
            envelopes=envelopes,
        )
        run = _read_run(context, urn)
    return run


def _state_delegation(
    context: Epoch2RootContext,
    run: Run,
    adoption: _Adoption,
    *,
    phase: Literal["started", "terminal"],
) -> None:
    """State one phase of the delegation on the parent Run's stream, when there is a parent.

    The line takes the parent stream's tail, and a re-delivered hook repeats the same
    event id, which the append answers with the line already standing. A parent that has
    already ended keeps the line as a quarantined diagnostic, as any late event is kept.
    """
    parent = run.parent_run_ref
    if parent is None:
        return
    terminal = phase == "terminal"
    payload = ChildRunPayload(
        child_run_ref=run.urn,
        delegation_request_ref=adoption.delegation_ref,
        phase=phase,
        terminal_status=run.status if terminal else None,
    )
    answer = append_run_event(
        context,
        RunEventAppend(
            urn=parent,
            event_ref=adoption.event_ref(f"delegation:{phase}"),
            run_sequence=1,
            event_kind=(
                RunEventKind.CHILD_RUN_TERMINAL if terminal else RunEventKind.CHILD_RUN_STARTED
            ),
            provenance="provider_native",
            payload=payload,
            actor=adoption.actor,
        ),
        now=datetime.now(UTC),
        at_tail=True,
    )
    logger.info(
        f"host subagent delegation phase={phase} child={run.key} parent={parent.entity_key} "
        f"disposition={answer.disposition}"
    )


def _bridge(context: Epoch2RootContext, run: Run, adoption: _Adoption, path: Path) -> int:
    """Append the subagent's transcript to its Run's stream, one event per payload.

    The payloads take the stream's tail, because a subagent that delegated in turn
    already has its children's lines on it. A re-delivered stop repeats the same event
    ids, which the append recognises as duplicates rather than new lines.
    """
    payloads = read_host_transcript(path, harness=adoption.params.harness)
    for index, payload in enumerate(payloads):
        append_run_event(
            context,
            RunEventAppend(
                urn=run.urn,
                event_ref=adoption.event_ref(index),
                run_sequence=index + 1,
                event_kind=(
                    RunEventKind.MESSAGE_SUMMARIZED
                    if isinstance(payload, MessageSummaryPayload)
                    else RunEventKind.CHILD_RUN_REQUESTED
                ),
                provenance="provider_native",
                payload=payload,
                actor=adoption.actor,
            ),
            now=datetime.now(UTC),
            at_tail=True,
        )
    return len(payloads)


def _answer(run: Run, *, bridged: int, reason: str) -> HostSubagentAnswer:
    return HostSubagentAnswer(
        run_ref=str(run.urn),
        parent_run_ref=None if run.parent_run_ref is None else str(run.parent_run_ref),
        status=run.status,
        bridged_events=bridged,
        reason=reason,
    )


def _start(
    context: Epoch2RootContext,
    authority: RootAuthority,
    params: HostSubagentStart,
    *,
    envelopes: list[Envelope],
) -> HostSubagentAnswer:
    run = _adopt(
        context, authority, _Adoption.of(params), now=datetime.now(UTC), envelopes=envelopes
    )
    return _answer(run, bridged=0, reason=f"the host subagent runs as {run.key}")


def _stop(
    context: Epoch2RootContext,
    authority: RootAuthority,
    params: HostSubagentStop,
    *,
    envelopes: list[Envelope],
) -> HostSubagentAnswer:
    adoption = _Adoption.of(params)
    now = datetime.now(UTC)
    run = _adopt(context, authority, adoption, now=now, envelopes=envelopes)
    if run.status in TERMINAL_RUN_STATUSES:
        return _answer(run, bridged=0, reason=f"{run.key} had already stopped")
    bridged = (
        0
        if params.transcript_path is None
        else _bridge(context, run, adoption, Path(params.transcript_path))
    )
    _move(
        context,
        run,
        adoption,
        to_status=RunStatus.COMPLETED,
        step="stop",
        updates={"ended_at": datetime.now(UTC).isoformat()},
        observations=(ObservedFact.RUN_REPORT_BOUND,),
        envelopes=envelopes,
    )
    run = _read_run(context, run.urn)
    _state_delegation(context, run, adoption, phase="terminal")
    return _answer(run, bridged=bridged, reason=f"{run.key} completed with {bridged} bridged")


def _published(
    ctx: MethodContext, envelopes: list[Envelope], answer: HostSubagentAnswer
) -> dict[str, Any]:
    """Publish what the verb committed, once the locks are released, and answer."""
    degraded = [envelope for envelope in envelopes if not publish_projection(ctx.bus, envelope)]
    body = answer.model_dump(mode="json")
    if degraded:
        body["warnings"] = [PROJECTION_DEGRADED]
    return body


@native_mutator(HOST_SUBAGENT_START_METHOD)
async def _start_host_subagent(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Adopt one host subagent as a repository-scoped Run and start it."""
    args = native_params(HostSubagentStart, params)
    context = ctx.native_root_context(authority.root)
    envelopes: list[Envelope] = []
    answer = await asyncio.to_thread(_start, context, authority, args, envelopes=envelopes)
    return _published(ctx, envelopes, answer)


@native_mutator(HOST_SUBAGENT_STOP_METHOD)
async def _stop_host_subagent(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Bridge one host subagent's transcript into its Run and complete the Run."""
    args = native_params(HostSubagentStop, params)
    context = ctx.native_root_context(authority.root)
    envelopes: list[Envelope] = []
    answer = await asyncio.to_thread(_stop, context, authority, args, envelopes=envelopes)
    return _published(ctx, envelopes, answer)


__all__ = [
    "HARNESS_ACTORS",
    "HOST_SUBAGENT_START_METHOD",
    "HOST_SUBAGENT_STOP_METHOD",
    "HostSubagentAnswer",
    "HostSubagentStart",
    "HostSubagentStop",
]
