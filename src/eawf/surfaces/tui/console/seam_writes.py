"""The seam's write path: the one way a console verb reaches the daemon, and back.

A verb is addressed from the projection rows the seam holds, so a write names the revision
the operator was shown, and is sent through the same binding every read uses. Each sent
operation is opened in a ledger under its own id and settled by the daemon's answer; one
whose answer never arrived stays outstanding for the reconnect to reconcile, asked again
under the same id so the daemon answers with what the first send did. An answer cites the
operator's evidence receipt, and a receipt records one answer: once it has been sent for
one pending action, an answer to any other is refused unsent rather than filed under a
receipt that already speaks for something else.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Protocol

from eawf.kernel.projection.compute import KeyedPatch, ProjectionRow
from eawf.kernel.projection.settings import EffectiveSettingsView
from eawf.runtime.budget.notices import BudgetThresholdNotice
from eawf.surfaces.cli._daemon_client import DaemonRpcError
from eawf.surfaces.tui.console.bulk import (
    BULK_CONTROL_METHOD,
    BULK_PREVIEW_METHOD,
    BULK_RECONCILE_METHOD,
    BulkRequest,
    bulk_params,
    bulk_results,
    refused_bulk,
    unanswered_bulk,
)
from eawf.surfaces.tui.console.operations import (
    NO_PRINCIPAL_REASON,
    AnswerRequest,
    ConsoleOperation,
    DispatchRequest,
    LifecycleRequest,
    NoticeRequest,
    OperationLedger,
    OperationResult,
    OperationStatus,
    Operator,
    SettingRequest,
    VerbRequest,
    address,
    address_dispatch,
    address_lifecycle,
    address_notice,
    address_setting,
    exhausted,
    not_sent,
    refused,
    rereads,
    settled,
    unanswered,
)

logger = logging.getLogger(__name__)


class WriteHost(Protocol):
    """What the write path needs of the seam: who acts, the rows it holds, and a way to ask."""

    @property
    def operator(self) -> Operator | None:
        """Return who the console's writes are attributed to; ``None`` when nobody."""
        ...

    @property
    def notices(self) -> tuple[BudgetThresholdNotice, ...]:
        """Return the open budget notices held for the operator's inbox."""
        ...

    def held_row(self, key: str) -> ProjectionRow | None:
        """Return the held row keyed *key*, if any."""
        ...

    async def call(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """Send one call to the daemon for the seam's tree and return its answer."""
        ...

    async def load_settings(self) -> EffectiveSettingsView:
        """Read the effective settings again and hold them."""
        ...

    async def load_notices(self) -> tuple[BudgetThresholdNotice, ...]:
        """Read the operator's notice inbox again and hold it."""
        ...

    async def reload_holding(self, key: str) -> None:
        """Re-read every held route that holds *key*."""
        ...


class SeamWrites:
    """The operations a seam sent, and the verbs that send them.

    The seam owns one and hands it every write; the ledger of what was sent and not yet
    answered lives here, beside the receipts already spent on an answer.
    """

    def __init__(self, host: WriteHost) -> None:
        """Bind the write path to the seam it sends for.

        Args:
            host: The seam, which addresses each write and carries it to the daemon.
        """
        self._host = host
        self._operations = OperationLedger()
        self._answered_under: dict[str, str] = {}

    @property
    def outstanding(self) -> tuple[ConsoleOperation, ...]:
        """Return the operations sent and not yet answered, oldest first."""
        return self._operations.outstanding()

    async def request(self, request: VerbRequest) -> OperationResult:
        """Send one console verb to the daemon, addressed from the rows the seam holds.

        The target is addressed by the URN and revision of the row the operator was shown,
        so a write never names a revision the console did not draw. Nothing held changes here;
        the daemon's commit arrives as a patch like any other.

        Args:
            request: What the operator asked for.

        Returns:
            What became of it: applied, superseded, refused with the daemon's reason,
            or outstanding when the answer never arrived. A request that cannot be
            addressed is refused without being sent.
        """
        if isinstance(request, SettingRequest | NoticeRequest | DispatchRequest):
            # no row addresses these: a layer write, the notice ledger, the tree's one queue
            return await (
                self._write_setting(request)
                if isinstance(request, SettingRequest)
                else self._unrowed(request)
            )
        if self._host.operator is None:
            return not_sent(request.target, f"{NO_PRINCIPAL_REASON} (and --receipt-ref to answer)")
        row = self._host.held_row(request.target)
        if row is None:
            return not_sent(
                request.target, f"{request.target} is in no projection the console holds"
            )
        if isinstance(request, LifecycleRequest):
            return await self._move(request, urn=row.urn)
        spent = self._spent_receipt(request)
        if spent is not None:
            return spent
        addressed = address(
            request, urn=row.urn, revision=int(row.revision), operator=self._host.operator
        )
        if isinstance(addressed, OperationResult):
            return addressed
        # only an answer to a pending action is recorded under an evidence receipt
        receipt = self._host.operator.receipt_ref if isinstance(request, AnswerRequest) else None
        if receipt is not None:
            self._answered_under[receipt] = request.target
        self._operations.open(addressed)
        result = await self._send(addressed)
        if result.status is OperationStatus.REFUSED and receipt is not None:
            # a refusal wrote nothing, so the receipt still records no answer
            self._answered_under.pop(receipt, None)
        if rereads(request, result):
            await self._host.reload_holding(request.target)
        return result

    async def _unrowed(self, request: NoticeRequest | DispatchRequest) -> OperationResult:
        """Send one dispatch request, or one notice disposition and re-read the inbox it changed.

        An unheld notice was never shown, so it is refused unsent. The inbox is re-read whatever
        the answer: an applied disposition moved the notice, a refused one may have escalated.
        """
        if self._host.operator is None:
            return not_sent(request.target, NO_PRINCIPAL_REASON)
        if isinstance(request, DispatchRequest):
            self._operations.open(sent := address_dispatch(request, operator=self._host.operator))
            return await self._send(sent)
        if not any(notice.notice_key == request.target for notice in self._host.notices):
            return not_sent(request.target, f"{request.target} is in no inbox the console holds")
        operation = address_notice(request, operator=self._host.operator)
        self._operations.open(operation)
        result = await self._send(operation)
        try:
            await self._host.load_notices()
        except (DaemonRpcError, OSError, ValueError) as exc:
            logger.warning(f"_unrowed reread_failed id={operation.operation_id} cause={exc!r}")
        return result

    async def _move(self, request: LifecycleRequest, *, urn: str) -> OperationResult:
        """Send one confirmed lifecycle move under the id its card minted.

        A move already outstanding under that id is asked again rather than opened twice,
        which is how a repeated confirmation of the same card at the same revision is one
        write: the daemon answers the second ask with what the first one did.
        """
        assert self._host.operator is not None, "only called with an operator"
        held = next(
            (
                op
                for op in self._operations.outstanding()
                if op.operation_id == request.operation_id
            ),
            None,
        )
        operation = held or address_lifecycle(request, urn=urn, operator=self._host.operator)
        if held is None:
            self._operations.open(operation)
        return await self._send(operation)

    async def bulk(self, request: BulkRequest) -> tuple[OperationResult, ...]:
        """Send one confirmed card's targets as one daemon bulk operation.

        The preview is asked first, and the operation opens under the confirmation
        digest it answers, anchored at the revisions the card showed; a reconcile asks
        again under the same id for every target not yet settled. The results are the
        daemon's, one per target; nothing the console holds changes here.

        Args:
            request: The confirmed targets.

        Returns:
            One result per target: the item's own outcome, ``rejected`` for every target
            of an operation refused whole, or ``unknown`` for every target when no answer
            arrived. A console acting as nobody, or holding no row for a target, sends
            nothing.
        """
        if self._host.operator is None:
            return tuple(not_sent(key, NO_PRINCIPAL_REASON) for key in request.targets)
        urns = {
            key: row.urn for key in request.targets if (row := self._host.held_row(key)) is not None
        }
        if len(urns) != len(request.targets):
            return tuple(
                not_sent(key, f"{key} is in no projection the console holds")
                for key in request.targets
            )
        items = {"verb": request.verb.value, "item_refs": list(urns.values())}
        try:
            shown = await self._host.call(BULK_PREVIEW_METHOD, items)
            params = bulk_params(
                request,
                urns,
                actor=self._host.operator.principal,
                digest=shown["confirmation_digest"],
            )
            method = BULK_RECONCILE_METHOD if request.reconcile else BULK_CONTROL_METHOD
            answer = await self._host.call(method, params)
        except DaemonRpcError as error:
            return refused_bulk(request, error.message)
        except Exception as exc:
            logger.warning(f"bulk unanswered id={request.operation_id} cause={exc!r}")
            return unanswered_bulk(request, "no answer arrived · reconcile asks again")
        logger.info(f"bulk id={request.operation_id} items={len(urns)} method={method}")
        return bulk_results(request, urns, answer)

    async def _write_setting(self, request: SettingRequest) -> OperationResult:
        """Send one settings edit to the layered-config verbs, then re-read the settings.

        Config carries no ordinal and no patch arrives for it, so the effective value an
        operator sees next is the daemon's fresh answer rather than the value the console
        asked for: a write that a higher layer shadows reads as unchanged, as it is.

        Args:
            request: The edit, carrying the key, the lens layer and the typed value.

        Returns:
            What the daemon answered; the held view is replaced only after an applied
            write, and a failed re-read leaves the previous view held and says why.
        """
        operation = address_setting(request)
        self._operations.open(operation)
        result = await self._send(operation)
        if result.status is not OperationStatus.APPLIED:
            return result
        try:
            await self._host.load_settings()
        except Exception as exc:
            logger.warning(
                f"_write_setting reread_failed id={operation.operation_id} cause={exc!r}"
            )
            return OperationResult(
                operation_id=result.operation_id,
                target=result.target,
                status=result.status,
                detail=f"{result.target} written · the re-read failed, so the frame is stale",
                disposition=result.disposition,
            )
        return result

    def _spent_receipt(self, request: VerbRequest) -> OperationResult | None:
        """Refuse an answer whose receipt already records the answer to another action.

        Returns:
            The refusal, or ``None`` when the request is not an answer or its receipt is
            unspent or was spent on this same action, which a retry answers again.
        """
        receipt = self._host.operator.receipt_ref if self._host.operator is not None else None
        if not isinstance(request, AnswerRequest) or receipt is None:
            return None
        answered = self._answered_under.get(receipt)
        if answered is None or answered == request.target:
            return None
        return not_sent(
            request.target,
            f"receipt {receipt} already records the answer to {answered} — each answer "
            "needs an evidence receipt of its own",
        )

    async def _send(self, operation: ConsoleOperation) -> OperationResult:
        """Send *operation* under its own id and settle the ledger with the answer.

        A refusal the daemon answered with wrote nothing, so it closes the operation.
        Any other failure leaves the outcome unknown, so the operation stays
        outstanding for the reconnect to reconcile.
        """
        try:
            answer = await self._host.call(operation.method, operation.params)
        except DaemonRpcError as error:
            result = refused(operation, error.message)
        except Exception as exc:
            logger.warning(
                f"operation unanswered id={operation.operation_id} "
                f"method={operation.method} cause={exc!r}"
            )
            result = unanswered(operation)
        else:
            result = settled(operation, answer)
        self._operations.settle(result)
        logger.info(
            f"operation id={operation.operation_id} method={operation.method} "
            f"status={result.status.value}"
        )
        return result

    async def reconcile(self) -> tuple[OperationResult, ...]:
        """Ask the daemon again for every outstanding operation, by its own id.

        The reconnect is the one automatic reconciliation, so an operation whose answer
        is lost again comes back in ``recovery`` rather than as a second ``unknown``:
        nothing further will ask on the operator's behalf until the next reconnect.
        """
        results: list[OperationResult] = []
        for operation in self._operations.outstanding():
            result = await self._send(operation)
            if result.status is OperationStatus.OUTSTANDING:
                result = exhausted(operation)
            results.append(result)
        return tuple(results)

    def settle_from_patches(self, patches: tuple[KeyedPatch, ...]) -> tuple[OperationResult, ...]:
        """Settle each outstanding Run control whose line the replay carries, from that line.

        A control is sent under its request id, and the daemon marks each line it commits
        for that request with the id and the disposition it reached, so the latest such
        line is the answer the console lost. Nothing is sent for it.
        """
        reached: dict[str, str] = {}
        for patch in patches:
            for entry in patch.entries:
                if entry.control is not None:
                    reached[entry.control.control_request_ref] = entry.control.disposition
        results: list[OperationResult] = []
        for operation in self._operations.outstanding():
            disposition = reached.get(operation.operation_id)
            if disposition is None:
                continue
            result = settled(operation, {"disposition": disposition})
            self._operations.settle(result)
            results.append(result)
        if results:
            logger.info(f"reconnect settled from the replay operations={len(results)}")
        return tuple(results)


__all__ = ["SeamWrites", "WriteHost"]
