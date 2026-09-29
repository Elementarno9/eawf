"""The ``runtime.bulk.*`` verbs: one operator decision over many Runs or Tasks.

``runtime.bulk.preview`` answers what an operation would be before it
opens: the :class:`~eawf.kernel.delivery.bulk.BulkConfirmation` naming the
target count, the effects, the non-effects and the invalidation rule, its
digest, and the revision each item stands at now. It writes nothing.

``runtime.bulk.control`` opens the operation. It refuses outright only
what makes the whole request meaningless -- a request that does not
parse, or a confirmation digest that is not the one the preview answers
for these items -- and otherwise judges every item on its own. An item
whose revision moved since it was confirmed is rejected before anything
is asked for it. Every other item is asked through the same fenced verb
a lone request for it goes through, under the same principal, so an item
gets exactly the authority a lone request for it would get: one refused
item grants the others nothing and aborts nothing. A Run is asked through
``runtime.run.control.request`` and ``runtime.run.control.acknowledge``,
and what it became is read back from the Run's control ledger rather than
taken from the answer. A Task is released through ``domain.task.release``,
whose committed transition is itself the observed effect; its refusal --
another principal's claim, an open Run, a moved revision -- rejects that
Task alone.

The answer is filed under the operation's idempotency key once every
item has been asked. The same key with the same request replays that
answer, the original per-item results unchanged; the same key with a
different item set is refused ``idempotency_conflict``. Each item's
request identity is derived from the operation's key and the item, so an
operation that died half way re-asks its items under the same identities
and each is answered by the fact it already has.

``runtime.bulk.reconcile`` asks again, under those same identities, for
every item that has not settled, and reads each one back. An item moves
only on what the ledger shows: an accepted control whose effect nobody
has recorded stays accepted, and an unknown one stays unknown, however
many times it is reconciled.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import UTC, datetime
from typing import Annotated, Any, Final, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from eawf.kernel.delivery.bulk import (
    BULK_CONTROLS,
    BULK_ITEM_KINDS,
    DISPOSITION_STATES,
    SETTLED_ITEM_STATES,
    BulkConfirmation,
    BulkIdempotencyKey,
    BulkItemResult,
    BulkItemState,
    BulkOperation,
    BulkVerb,
    advance,
    aggregate_of,
    canonical_items,
    confirm_bulk,
    require_item_kinds,
)
from eawf.kernel.identity import EntityKind, QualifiedUrn
from eawf.kernel.runtime.control import ControlDisposition
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import PrincipalKey, Sha256DigestStr, StrictPositiveInt
from eawf.kernel.state.epoch2.task import Task
from eawf.kernel.state.epoch2.urns import BulkItemUrn, RunUrn
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.control.reducer import project_disposition
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext, dispatch, register
from eawf.runtime.daemon.methods.delivery import file_keyed_answer, keyed_answer
from eawf.runtime.daemon.methods.domain_envelope import DomainEnvelope, DomainStatus
from eawf.runtime.daemon.methods.run import (
    RUN_CONTROL_ACKNOWLEDGE_METHOD,
    RUN_CONTROL_REQUEST_METHOD,
)
from eawf.runtime.daemon.native_dispatch import control_facts_of, stored_run
from eawf.runtime.daemon.native_guard import (
    REPO_ROOT_PARAM,
    native_mutator,
    native_params,
    require_native_call,
)
from eawf.runtime.daemon.task_release import TASK_RELEASE_METHOD

logger = logging.getLogger(__name__)


#: The read that answers what an operation would be before it opens.
BULK_PREVIEW_METHOD: Final = "runtime.bulk.preview"

#: The verb that opens one operation over many Runs.
BULK_CONTROL_METHOD: Final = "runtime.bulk.control"

#: The verb that reads every unsettled item of an opened operation again.
BULK_RECONCILE_METHOD: Final = "runtime.bulk.reconcile"

#: The refusal an operation gets when it names a consequence other than
#: the one its items and verb have.
CONFIRMATION_MISMATCH: Final = "confirmation_mismatch"

#: The cause every Task a bulk release frees is recorded under, so a mass
#: release reads apart from a Task released on its own.
BULK_RELEASE_CAUSE: Final = "bulk-release"


#: The items of a request, unique and in canonical order, so two
#: selections of the same items are one request to the idempotency check.
_Items = Annotated[tuple[BulkItemUrn, ...], Field(min_length=1), AfterValidator(canonical_items)]


class BulkPreviewParams(BaseModel):
    """Params of :data:`BULK_PREVIEW_METHOD`.

    Attributes:
        verb: The verb the operation would apply.
        item_refs: The items it would name, in any order.
    """

    model_config = ConfigDict(extra="forbid")

    verb: BulkVerb
    item_refs: _Items

    @model_validator(mode="after")
    def _items_fit_the_verb(self) -> Self:
        """Refuse an item of a kind the verb does not take.

        Raises:
            ValueError: A Run control names a Task, or a release a Run.
        """
        require_item_kinds(self.verb, self.item_refs)
        return self


class BulkPreviewAnswer(BaseModel):
    """What a preview answers with.

    Attributes:
        confirmation: What the operator must be shown before opening.
        confirmation_digest: The digest the opening request must carry.
        expected_revisions: The revision each resolvable item stands at
            now, keyed by URN: the anchors to open with.
        unresolved: The items no record answers for.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    confirmation: BulkConfirmation
    confirmation_digest: Sha256DigestStr
    expected_revisions: dict[str, StrictPositiveInt]
    unresolved: tuple[str, ...] = ()


class BulkControlParams(BaseModel):
    """Params of :data:`BULK_CONTROL_METHOD` and :data:`BULK_RECONCILE_METHOD`.

    Reconcile takes the opening request unchanged, so it addresses the
    operation by the same key and is held to the same item set.

    Attributes:
        verb: The verb every item receives.
        item_refs: The items, in any order; the operation lists them
            canonically.
        expected_revisions: The revision each item was confirmed at.
        actor: Who opens the operation, and who each item is asked by.
        idempotency_key: The client's name for the operation.
        confirmation_digest: The digest of the confirmation the operator
            was shown.
    """

    model_config = ConfigDict(extra="forbid")

    verb: BulkVerb
    item_refs: _Items
    expected_revisions: dict[str, StrictPositiveInt]
    actor: PrincipalKey
    idempotency_key: BulkIdempotencyKey
    confirmation_digest: Sha256DigestStr

    @model_validator(mode="after")
    def _anchors_every_item(self) -> Self:
        """Refuse anchors that do not name exactly the request's items.

        Raises:
            ValueError: An item has no anchor, or an anchor names
                something that is not an item; or an item is of a kind
                the verb does not take.
        """
        require_item_kinds(self.verb, self.item_refs)
        if set(self.expected_revisions) != {str(urn) for urn in self.item_refs}:
            raise ValueError("expected_revisions must name exactly the operation's items")
        return self


def _revision(context: Epoch2RootContext, urn: QualifiedUrn) -> int | None:
    """Return the revision the item stands at, or ``None`` when no record holds it.

    A Task is read from the document only: a Task that left it is
    terminal, and a release has nothing to anchor on one.
    """
    with context.session([urn]) as session:
        if urn.kind is EntityKind.TASK:
            row = document_rows(session.read_document(), Epoch2Collection.TASK).get(urn.entity_key)
            return None if row is None else Task.model_validate(row).revision
        records = read_ledger_records(session.ledger_path(Epoch2Collection.RUN))
        try:
            return stored_run(session, records, urn).revision
        except DaemonValidationError:
            return None


def _observed(context: Epoch2RootContext, urn: RunUrn, ref: str) -> ControlDisposition:
    """Return what the Run's control ledger says became of one request."""
    with context.session([urn]) as session:
        records = read_ledger_records(session.ledger_path(Epoch2Collection.RUN))
    facts = [fact for fact in control_facts_of(records, urn) if fact.control_request_ref == ref]
    return project_disposition(facts)


def _request_ref(context: Epoch2RootContext, key: str, urn: QualifiedUrn) -> str:
    """Return the request identity one item is asked under.

    Derived from the operation's key and the item, so a re-asked item is
    answered by the fact its first ask left rather than opening another. A
    Run's control is filed under it; a Task's release is keyed by it.
    """
    seed = f"{context.idempotency_key(f'{BULK_CONTROL_METHOD}:{key}')}\n{urn}"
    return f"CTL-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:32]}"


def _refusal_code(refusal: DaemonValidationError) -> str:
    """Return the stable code a daemon refusal leads with."""
    return str(refusal).split(": ", 2)[1]


async def _ask(
    ctx: MethodContext,
    context: Epoch2RootContext,
    params: dict[str, Any],
    args: BulkControlParams,
    urn: RunUrn,
    *,
    prior: BulkItemState,
) -> BulkItemResult:
    """Ask for one item's control through the single-item verbs, then read it back.

    Args:
        ctx: Server context the single-item verbs are dispatched through.
        context: The native context of the addressed root.
        params: The raw request, whose routing key the item verbs reuse.
        args: The validated operation.
        urn: The item.
        prior: Where the item stood before this ask.

    Returns:
        The item's result: a refusal the item verbs gave, ``unknown``
        when the ask failed in a way that left its outcome unobserved,
        or else what the Run's control ledger now shows -- a request
        superseded by another principal's lease reads as rejected with
        code ``superseded``.
    """
    ref = _request_ref(context, args.idempotency_key, urn)
    routing = {key: value for key, value in params.items() if key == REPO_ROOT_PARAM}
    asked = {**routing, "urn": str(urn), "control_request_ref": ref, "actor": args.actor}
    code: str | None = None
    detail = ""
    try:
        await dispatch(
            RUN_CONTROL_REQUEST_METHOD,
            ctx,
            {**asked, "control": BULK_CONTROLS[args.verb].value},
        )
        await dispatch(RUN_CONTROL_ACKNOWLEDGE_METHOD, ctx, asked)
    except DaemonValidationError as refusal:
        observed = BulkItemState.REJECTED
        code, detail = _refusal_code(refusal), str(refusal)[:1000]
    except Exception as error:
        # Any other failure leaves the ask's outcome unobserved, and one
        # item's transport trouble must not abort the others.
        observed = BulkItemState.UNKNOWN
        detail = f"the ask for this Run was not answered: {type(error).__name__}"
    else:
        disposition = await asyncio.to_thread(_observed, context, urn, ref)
        observed = DISPOSITION_STATES[disposition]
        # a lease another request holds, or a refusal, is named by its disposition
        code = disposition.value
    state = advance(prior, observed)
    if state is not BulkItemState.REJECTED:
        code = None
    logger.info(f"_ask run={urn.entity_key!r} prior={prior.value} state={state.value}")
    return BulkItemResult(state=state, code=code, detail=detail, control_request_ref=ref)


async def _release(
    ctx: MethodContext,
    context: Epoch2RootContext,
    params: dict[str, Any],
    args: BulkControlParams,
    urn: QualifiedUrn,
    *,
    prior: BulkItemState,
) -> BulkItemResult:
    """Release one Task's lease through the single-item verb, and read its answer.

    The release is a canonical transition, so its committed receipt is the
    observed effect: an ``ok`` answer confirms the item. A refusal names
    the guard that failed where there is one -- another principal holds
    the lease, a Run is open on the Task -- and otherwise the envelope's
    code. A re-ask under the same derived key replays the first commit.

    Args:
        ctx: Server context the single-item verb is dispatched through.
        context: The native context of the addressed root.
        params: The raw request, whose routing key the item verb reuses.
        args: The validated operation.
        urn: The Task.
        prior: Where the item stood before this ask.

    Returns:
        The item's result: ``confirmed`` on a committed release,
        ``rejected`` with the refusal's code, or ``unknown`` when the ask
        failed in a way that left its outcome unobserved.
    """
    routing = {key: value for key, value in params.items() if key == REPO_ROOT_PARAM}
    code: str | None = None
    detail = ""
    try:
        answer = await dispatch(
            TASK_RELEASE_METHOD,
            ctx,
            {
                **routing,
                "urn": str(urn),
                "expected_revision": args.expected_revisions[str(urn)],
                "idempotency_key": _request_ref(context, args.idempotency_key, urn),
                "actor": args.actor,
                "reason_code": BULK_RELEASE_CAUSE,
            },
        )
    except Exception as error:
        # Any failure to answer leaves the release unobserved, and one
        # item's transport trouble must not abort the others.
        state = advance(prior, BulkItemState.UNKNOWN)
        detail = f"the release of this Task was not answered: {type(error).__name__}"
    else:
        envelope = DomainEnvelope.model_validate(answer)
        if envelope.status is DomainStatus.OK:
            state = advance(advance(prior, BulkItemState.ACCEPTED), BulkItemState.CONFIRMED)
        else:
            first = envelope.errors[0]
            state = advance(prior, BulkItemState.REJECTED)
            code, detail = first.guard or first.code.value, first.message[:1000]
    if state is not BulkItemState.REJECTED:
        code = None
    logger.info(f"_release task={urn.entity_key!r} prior={prior.value} state={state.value}")
    return BulkItemResult(state=state, code=code, detail=detail)


async def _ask_item(
    ctx: MethodContext,
    context: Epoch2RootContext,
    params: dict[str, Any],
    args: BulkControlParams,
    urn: QualifiedUrn,
    *,
    prior: BulkItemState,
) -> BulkItemResult:
    """Ask for one item through the single-item verb its verb goes through."""
    if BULK_ITEM_KINDS[args.verb] is EntityKind.TASK:
        return await _release(ctx, context, params, args, urn, prior=prior)
    return await _ask(ctx, context, params, args, urn, prior=prior)


async def _open_item(
    ctx: MethodContext,
    context: Epoch2RootContext,
    params: dict[str, Any],
    args: BulkControlParams,
    urn: QualifiedUrn,
) -> BulkItemResult:
    """Judge one item's anchor, then ask for it if the anchor holds."""
    expected = args.expected_revisions[str(urn)]
    current = await asyncio.to_thread(_revision, context, urn)
    noun = urn.kind.value
    if current is None:
        return BulkItemResult(
            state=BulkItemState.REJECTED,
            code="identity_not_found",
            detail=f"no {noun} record is held under this URN",
        )
    if current != expected:
        return BulkItemResult(
            state=BulkItemState.REJECTED,
            code="revision_conflict",
            detail=f"the {noun} moved from revision {expected} to {current} after it was confirmed",
        )
    return await _ask_item(ctx, context, params, args, urn, prior=BulkItemState.REQUESTED)


def _operation(args: BulkControlParams, results: dict[str, BulkItemResult]) -> BulkOperation:
    """Return the operation *args* opened, carrying *results*."""
    return BulkOperation(
        verb=args.verb,
        item_refs=args.item_refs,
        expected_revisions=args.expected_revisions,
        item_results=results,
        aggregate=aggregate_of(results.values()),
        idempotency_key=args.idempotency_key,
        actor=args.actor,
        confirmation_digest=args.confirmation_digest,
    )


def _preview(context: Epoch2RootContext, args: BulkPreviewParams) -> BulkPreviewAnswer:
    """Return the confirmation and the current anchors of one prospective operation."""
    confirmation = confirm_bulk(args.verb, args.item_refs)
    revisions = {str(urn): _revision(context, urn) for urn in args.item_refs}
    return BulkPreviewAnswer(
        confirmation=confirmation,
        confirmation_digest=confirmation.digest,
        expected_revisions={urn: rev for urn, rev in revisions.items() if rev is not None},
        unresolved=tuple(urn for urn, rev in revisions.items() if rev is None),
    )


@register(BULK_PREVIEW_METHOD)
async def _preview_bulk(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Answer what an operation over these items would be; writes nothing."""
    authority = require_native_call(ctx, params)
    args = native_params(BulkPreviewParams, params)
    context = ctx.native_root_context(authority.root)
    answer = await asyncio.to_thread(_preview, context, args)
    return answer.model_dump(mode="json")


@native_mutator(BULK_CONTROL_METHOD)
async def _open_bulk(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Open one operation, judging and asking for every item on its own.

    Raises:
        DaemonValidationError: The request does not parse, anchors
            included; its confirmation digest is not
            the one these items and this verb confirm to; or its key
            already answered a different request.
    """
    args = native_params(BulkControlParams, params)
    context = ctx.native_root_context(authority.root)
    payload = args.model_dump(mode="json")
    key = args.idempotency_key
    replayed = await asyncio.to_thread(
        keyed_answer, context, method=BULK_CONTROL_METHOD, key=key, params=payload
    )
    if replayed is not None:
        return replayed
    if confirm_bulk(args.verb, args.item_refs).digest != args.confirmation_digest:
        raise DaemonValidationError(
            f"validation_failed: {CONFIRMATION_MISMATCH}: the confirmation digest is not the "
            "one these items and this verb confirm to, so the operator was shown a different "
            "consequence; preview again and open under the digest it answers"
        )
    results = {
        str(urn): await _open_item(ctx, context, params, args, urn) for urn in args.item_refs
    }
    answer = _operation(args, results).model_dump(mode="json")
    await asyncio.to_thread(
        file_keyed_answer,
        context,
        method=BULK_CONTROL_METHOD,
        key=key,
        params=payload,
        answer=answer,
        at=datetime.now(UTC),
    )
    logger.info(f"_open_bulk verb={args.verb.value} items={len(results)}")
    return answer


@native_mutator(BULK_RECONCILE_METHOD)
async def _reconcile_bulk(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Ask again for every unsettled item of an opened operation and read it back.

    The answer is the operation as it stands now. It is not filed: a
    replay of the opening request still answers the original results.

    Raises:
        DaemonValidationError: The request does not parse; its key
            answered a different request; or no operation was opened
            under its key.
    """
    args = native_params(BulkControlParams, params)
    context = ctx.native_root_context(authority.root)
    stored = await asyncio.to_thread(
        keyed_answer,
        context,
        method=BULK_CONTROL_METHOD,
        key=args.idempotency_key,
        params=args.model_dump(mode="json"),
    )
    if stored is None:
        raise DaemonValidationError(
            f"validation_failed: identity_not_found: no bulk operation was opened under "
            f"idempotency key {args.idempotency_key!r}"
        )
    opened = BulkOperation.model_validate(stored)
    results = dict(opened.item_results)
    for urn in opened.item_refs:
        result = results[str(urn)]
        if result.state not in SETTLED_ITEM_STATES:
            results[str(urn)] = await _ask_item(ctx, context, params, args, urn, prior=result.state)
    return _operation(args, results).model_dump(mode="json")


__all__ = [
    "BULK_CONTROL_METHOD",
    "BULK_PREVIEW_METHOD",
    "BULK_RECONCILE_METHOD",
    "BULK_RELEASE_CAUSE",
    "CONFIRMATION_MISMATCH",
    "BulkControlParams",
    "BulkPreviewAnswer",
    "BulkPreviewParams",
]
