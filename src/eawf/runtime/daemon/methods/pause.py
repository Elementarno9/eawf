"""The ``runtime.pause.*`` verbs: an operational pause the daemon observed, as a record.

A pause lives on the run ledger beside the work it holds, one line per revision under its
own ``PAU-####`` key, as a provider permission and a host question do. The daemon opens
one when it observes the work waiting: a Run whose host asked its operator a question
waits on a person until that question is answered, so the pause names the question as the
record whose answer ends it, and the answer resolves it in the same transaction. A Run the
stall sweep finds quiet is lost: its pause cites the stall fact and waits on the Run to
answer again, and ends when it does or when the Run ends.

``read`` reports every pause of the tree with the situation it projects to, computed at
every read and never stored, and the state of each Run a pause holds.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict

from eawf.kernel.identity import EntityKind, QualifiedUrn
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.epoch2.pause import (
    OpenPause,
    PauseReason,
    PauseSituation,
    PauseStatus,
    ResumePredicate,
    project_pause,
)
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.ledger import LedgerRecord, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.epoch2_root import RootSession
from eawf.runtime.daemon.epoch2_transaction import commit_ledger_append
from eawf.runtime.daemon.methods import MethodContext, register
from eawf.runtime.daemon.native_guard import native_params, require_native_call

logger = logging.getLogger(__name__)

#: The verb every pause of a tree is read through.
PAUSE_READ_METHOD: Final = "runtime.pause.read"

#: The discriminator a pause line carries on the run ledger.
_PAYLOAD_KIND: Final = "open_pause"

#: The Run state a lost Run is reported in, which leaves a control's outcome unknown.
_LOST: Final = "LOST"

#: What resumes a Run that went quiet, and what observes it.
_ACTIVE: Final = ResumePredicate(
    description="the Run produces activity again", evaluator="the daemon's stall sweep"
)

#: What resumes work that waits on a person, and what observes it.
_ANSWERED: Final = ResumePredicate(
    description="the question it waits on is answered", evaluator="the daemon's answer path"
)


class PauseLine(BaseModel):
    """One revision of a pause on the run ledger.

    Attributes:
        payload_kind: The line's discriminator.
        pause: The pause record itself.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    payload_kind: Literal["open_pause"] = _PAYLOAD_KIND
    pause: OpenPause


class PauseItem(BaseModel):
    """One pause as the read reports it.

    Attributes:
        pause: The record, at its latest revision.
        situation: The situation it projects to now.
        ends_when: What would end it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    pause: OpenPause
    situation: PauseSituation
    ends_when: str


class PausesAnswer(BaseModel):
    """What :data:`PAUSE_READ_METHOD` answers.

    Attributes:
        pauses: Every pause of the tree, by key.
        run_states: The state of each Run a pause holds, by Run key; a Run the document
            no longer holds is absent.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    pauses: tuple[PauseItem, ...] = ()
    run_states: dict[str, str] = {}


class _ReadParams(BaseModel):
    """Params of :data:`PAUSE_READ_METHOD`: none beyond the tree."""

    model_config = ConfigDict(extra="forbid")


def latest_pauses(records: Iterable[LedgerRecord]) -> dict[str, OpenPause]:
    """Return the latest revision of every pause on the run ledger, by key."""
    latest: dict[str, OpenPause] = {}
    for item in records:
        if item.payload.get("payload_kind") == _PAYLOAD_KIND:
            latest[item.record_key] = PauseLine.model_validate(item.payload).pause
    return dict(sorted(latest.items()))


def _append(session: RootSession, pause: OpenPause, *, now: datetime) -> None:
    """Append one revision of *pause* as a line of the run ledger."""
    commit_ledger_append(
        session,
        LedgerRecord(
            collection=Epoch2Collection.RUN,
            record_key=pause.key,
            status=pause.status.value.lower(),
            recorded_at=now,
            payload=PauseLine(pause=pause).model_dump(mode="json"),
        ),
    )


def _next_key(held: dict[str, OpenPause]) -> str:
    """Return the ``PAU-####`` key after every pause the run ledger holds."""
    return f"PAU-{max((int(key.split('-', 1)[1]) for key in held), default=0) + 1:04d}"


def stall_of(pause: OpenPause) -> str | None:
    """Return the stall fact key a pause over a quiet Run cites, or ``None`` for any other."""
    return next((ref for ref in pause.health_evidence_refs if isinstance(ref, str)), None)


def open_stall_pause(
    session: RootSession, *, run_ref: QualifiedUrn, stall_key: str, now: datetime
) -> OpenPause:
    """Open the provider pause over a Run that went quiet, citing its stall fact.

    One quiet episode opens one pause: a pause already citing *stall_key* is returned
    rather than opened twice.

    Returns:
        The pause citing the stall.
    """
    held = latest_pauses(read_ledger_records(session.ledger_path(Epoch2Collection.RUN)))
    standing = next((p for p in held.values() if stall_of(p) == stall_key), None)
    if standing is not None:
        return standing
    pause = OpenPause(
        key=_next_key(held),
        scope_ref=run_ref,
        reason=PauseReason.PROVIDER,
        health_evidence_refs=(stall_key,),
        resume_predicate=_ACTIVE,
        status=PauseStatus.OPEN,
        opened_at=now,
    )
    _append(session, pause, now=now)
    logger.info(f"open_stall_pause key={pause.key} run={run_ref.entity_key} stall={stall_key}")
    return pause


def end_stall_pause(
    session: RootSession, pause: OpenPause, *, cancelled: bool, now: datetime
) -> None:
    """End an open stall pause: resolved when its Run answered, cancelled with its Run."""
    if cancelled:
        ended = pause.model_copy(update={"status": PauseStatus.CANCELLED})
    else:
        observed = pause.resume_predicate.model_copy(
            update={"last_evaluated_at": now, "last_result": True}
        )
        ended = pause.model_copy(
            update={
                "status": PauseStatus.RESOLVED,
                "resume_predicate": observed,
                "resolved_at": now,
            }
        )
    _append(session, ended, now=now)
    logger.info(f"end_stall_pause key={pause.key} status={ended.status.value}")


def open_person_pause(
    session: RootSession,
    *,
    scope_ref: QualifiedUrn,
    waiting_on_ref: QualifiedUrn,
    now: datetime,
) -> OpenPause:
    """Open a pause over *scope_ref* that waits on a person answering *waiting_on_ref*.

    The question the pause waits on is also what the daemon observed opening it. A
    pause already open on that question is returned rather than opened twice, so a
    retried raise opens one pause.

    Returns:
        The open pause.
    """
    held = latest_pauses(read_ledger_records(session.ledger_path(Epoch2Collection.RUN)))
    standing = next(
        (
            pause
            for pause in held.values()
            if pause.waiting_on_ref == waiting_on_ref and pause.status is PauseStatus.OPEN
        ),
        None,
    )
    if standing is not None:
        return standing
    pause = OpenPause(
        key=_next_key(held),
        scope_ref=scope_ref,
        reason=PauseReason.USER,
        health_evidence_refs=(waiting_on_ref,),
        resume_predicate=_ANSWERED,
        waiting_on_ref=waiting_on_ref,
        status=PauseStatus.OPEN,
        opened_at=now,
    )
    _append(session, pause, now=now)
    logger.info(f"open_person_pause key={pause.key} scope={scope_ref.entity_key}")
    return pause


def resolve_person_pauses(
    session: RootSession, answered: Iterable[QualifiedUrn], *, now: datetime
) -> tuple[str, ...]:
    """Resolve every open pause waiting on one of the *answered* records.

    Returns:
        The keys of the pauses resolved.
    """
    refs = set(answered)
    held = latest_pauses(read_ledger_records(session.ledger_path(Epoch2Collection.RUN)))
    resolved: list[str] = []
    for pause in held.values():
        if pause.status is not PauseStatus.OPEN or pause.waiting_on_ref not in refs:
            continue
        observed = _ANSWERED.model_copy(update={"last_evaluated_at": now, "last_result": True})
        _append(
            session,
            pause.model_copy(
                update={
                    "status": PauseStatus.RESOLVED,
                    "resume_predicate": observed,
                    "resolved_at": now,
                }
            ),
            now=now,
        )
        resolved.append(pause.key)
    return tuple(resolved)


def read_pauses(document_path: Path) -> PausesAnswer:
    """Report every pause of the tree whose document is at *document_path*.

    Args:
        document_path: The selected generation's document.

    Returns:
        Each pause with its situation, and the state of each Run a pause holds.
    """
    runs = document_rows(read_document(document_path), Epoch2Collection.RUN)
    held = latest_pauses(read_ledger_records(ledger_path(document_path, Epoch2Collection.RUN)))
    run_states = {
        pause.scope_ref.entity_key: str(runs[pause.scope_ref.entity_key].get("status"))
        for pause in held.values()
        if pause.scope_ref.kind is EntityKind.RUN and pause.scope_ref.entity_key in runs
    }
    # a Run an open stall pause stands over is lost: stored running, its outcome unknown
    run_states.update(
        {
            pause.scope_ref.entity_key: _LOST
            for pause in held.values()
            if pause.status is PauseStatus.OPEN and stall_of(pause) is not None
        }
    )
    items = []
    for pause in held.values():
        lost = run_states.get(pause.scope_ref.entity_key) == _LOST
        projection = project_pause(pause, affected_run_lost=lost)
        items.append(
            PauseItem(pause=pause, situation=projection.situation, ends_when=projection.ends_when)
        )
    return PausesAnswer(pauses=tuple(items), run_states=run_states)


@register(PAUSE_READ_METHOD)
async def _read_pauses(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Report every pause of the tree with the situation it projects to."""
    authority = require_native_call(ctx, params)
    native_params(_ReadParams, params)
    assert authority.target is not None and authority.generation_id is not None
    path = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    answer = await asyncio.to_thread(read_pauses, path)
    return answer.model_dump(mode="json")


__all__ = [
    "PAUSE_READ_METHOD",
    "PauseItem",
    "PauseLine",
    "PausesAnswer",
    "end_stall_pause",
    "latest_pauses",
    "open_person_pause",
    "open_stall_pause",
    "read_pauses",
    "resolve_person_pauses",
    "stall_of",
]
