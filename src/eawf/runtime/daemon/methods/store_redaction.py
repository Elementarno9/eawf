"""``runtime.store.redact_content``: withhold leaked lines from content no commit holds yet.

Captured content is scrubbed when it is filed, so a stored row carries a leak
only when the leak scan learned a shape after the row was written. This verb
plans the correction with :func:`~eawf.kernel.runtime.content_redaction.plan_content_redaction`
against the artifact ledger's bytes in the repository's ``HEAD`` commit, and
replaces only the lines after them. Committed rows stay as they are.

A second call finds nothing left to withhold and writes nothing; a retry under
the same idempotency key replays the first answer.

A leaking unpublished row that is not captured content is left as it is and
answered with a remediation naming ``runtime.store.drop_unpublished_row``,
which removes that one line under the same guards while nothing else names it.
A ledger append files no change-feed row, so the generation's history holds
nothing of such a line; a record compacted out of the document does have
history there, and the drop refuses it.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from eawf.kernel.runtime.content_redaction import (
    ContentRedaction,
    ContentRedactionError,
    plan_content_redaction,
    plan_row_drop,
)
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import PrincipalKey
from eawf.kernel.store.ledger import (
    LedgerAppendOnlyError,
    LedgerError,
    LedgerRecord,
    LedgerTornTailError,
    read_ledger_records,
    replace_unpublished_suffix,
)
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import LEDGER_COLLECTIONS, Epoch2Collection
from eawf.observability.logging.state_leak import default_allowed_emails
from eawf.platform.subprocess_detach import no_window_kwargs
from eawf.runtime.daemon.epoch2_recovery import LEDGER_LINE_KEY
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext, RootSession
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.delivery import IdempotencyKey, file_keyed_answer, keyed_answer
from eawf.runtime.daemon.methods.doctor import project_lock_urn
from eawf.runtime.daemon.native_guard import native_mutator, native_params
from eawf.runtime.daemon.wal import WalStatus, list_records, read_record

logger = logging.getLogger(__name__)

#: The verb that withholds leaked lines from the artifact ledger's unpublished content rows.
STORE_REDACT_CONTENT_METHOD: Final = "runtime.store.redact_content"

#: The verb that removes one unpublished non-content line from the artifact ledger.
STORE_DROP_UNPUBLISHED_ROW_METHOD: Final = "runtime.store.drop_unpublished_row"

#: The command a remediation names, with the caller's own key and principal left to fill.
DROP_UNPUBLISHED_ROW_COMMAND: Final = "eawf store drop-unpublished-row"

#: Refusal: the repository has no ``HEAD`` commit to tell published lines from new ones.
REDACTION_NO_COMMIT: Final = "redaction_no_commit"

#: Refusal: a WAL record not yet durable journals an artifact-ledger line the
#: boot replay would append again by digest, undoing a redaction of it.
REDACTION_WAL_UNFINISHED: Final = "redaction_wal_unfinished"


class RedactContentParams(BaseModel):
    """Params of :data:`STORE_REDACT_CONTENT_METHOD`.

    Attributes:
        actor: Who asked.
        idempotency_key: The client's name for this request.
    """

    model_config = ConfigDict(extra="forbid")

    actor: PrincipalKey
    idempotency_key: IdempotencyKey


class LeakRemediation(BaseModel):
    """The step that clears one leaking unpublished row redaction left as it is.

    Attributes:
        record_key: The row's record key.
        payload_kind: The ``payload_kind`` its payload names, or ``None``.
        command: The command that removes the row, with the caller's
            idempotency key and principal left as placeholders, or ``None``
            when no verb can remove it yet.
        refusal: The ``REDACTION_*`` code the drop answers such a row with,
            set exactly when ``command`` is ``None``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    record_key: str
    payload_kind: str | None
    command: str | None
    refusal: str | None = None

    @model_validator(mode="after")
    def _one_way(self) -> LeakRemediation:
        """Carry exactly one of a command and the refusal that rules it out.

        Returns:
            The remediation.

        Raises:
            ValueError: Both or neither are set.
        """
        if (self.command is None) == (self.refusal is None):
            raise ValueError("a remediation names exactly one of a command and a refusal")
        return self


class RedactContentAnswer(BaseModel):
    """What one redaction answers with.

    Attributes:
        ledger: The artifact ledger, relative to the repository root.
        unpublished_rows: How many of its lines no commit holds.
        rows: How many content rows were rewritten.
        withheld_lines: How many kept lines were newly withheld.
        record_keys: The record key of each rewritten row, in file order.
        non_content_leak_keys: The record key of each unpublished row that is
            not captured content yet still leaks, in file order; the verb
            leaves those rows as they are.
        remediations: One step per entry of ``non_content_leak_keys``, in
            the same order.
        replayed: Whether the answer is the stored one of an earlier call.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ledger: str
    unpublished_rows: int
    rows: int
    withheld_lines: int
    record_keys: list[str]
    non_content_leak_keys: list[str]
    remediations: list[LeakRemediation]
    replayed: bool = False


class DropUnpublishedRowParams(BaseModel):
    """Params of :data:`STORE_DROP_UNPUBLISHED_ROW_METHOD`.

    Attributes:
        actor: Who asked.
        idempotency_key: The client's name for this request.
        record_key: The record key of the artifact-ledger line to remove.
    """

    model_config = ConfigDict(extra="forbid")

    actor: PrincipalKey
    idempotency_key: IdempotencyKey
    record_key: str = Field(min_length=1, max_length=128)


class DropUnpublishedRowAnswer(BaseModel):
    """What one drop answers with.

    Attributes:
        ledger: The artifact ledger, relative to the repository root.
        record_key: The record key of the line removed.
        payload_kind: The ``payload_kind`` its payload named, or ``None``.
        unpublished_rows: How many of its lines no commit holds, after the removal.
        replayed: Whether the answer is the stored one of an earlier call.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ledger: str
    record_key: str
    payload_kind: str | None
    unpublished_rows: int
    replayed: bool = False


def _refused(code: str, detail: str) -> DaemonValidationError:
    """Return the wire form of one redaction refusal."""
    return DaemonValidationError(f"validation_failed: {code}: {detail}")


def _git_bytes(repository: Path, *args: str) -> bytes | None:
    """Return the raw output of one read-only git command, or ``None`` on failure."""
    done = subprocess.run(
        ["git", "-C", str(repository), *args],
        capture_output=True,
        check=False,
        **no_window_kwargs(),
    )
    return done.stdout if done.returncode == 0 else None


def _published_bytes(repository: Path, relative: str) -> bytes:
    """Return the ledger's bytes in ``HEAD``; empty when ``HEAD`` does not hold it.

    Raises:
        DaemonValidationError: ``HEAD`` cannot be read, so which lines are
            published is unknown.
    """
    if _git_bytes(repository, "rev-parse", "--verify", "--quiet", "HEAD^{commit}") is None:
        raise _refused(REDACTION_NO_COMMIT, "the repository has no HEAD commit to compare against")
    listed = _git_bytes(repository, "ls-tree", "--name-only", "HEAD", "--", relative)
    if listed is None:
        raise _refused(REDACTION_NO_COMMIT, f"HEAD cannot be listed for {relative}")
    if not listed.strip():
        return b""
    published = _git_bytes(repository, "show", f"HEAD:{relative}")
    if published is None:
        raise _refused(REDACTION_NO_COMMIT, f"HEAD:{relative} cannot be read")
    return published


def _unfinished_ledger_records(wal_dir: Path, ledger: Path) -> list[str]:
    """Return the id of each pending or applied WAL record journalling a line of *ledger*.

    The boot replay appends such a record's line unless the ledger holds its
    exact bytes, so rewriting that line now would bring the original back.
    A record that cannot be read is skipped: the replay poisons it instead
    of appending its line.

    Args:
        wal_dir: The root's WAL namespace.
        ledger: The ledger the redaction would rewrite.

    Returns:
        The record ids, pending ones first, each in written order.

    Raises:
        ValidationError: A readable record journals a line that is not a
            ledger record, which the replay also refuses loudly.
    """
    found: list[str] = []
    for status in (WalStatus.PENDING, WalStatus.APPLIED):
        for path in list_records(wal_dir, status=status):
            try:
                record = read_record(path)
            except ValueError, OSError:
                continue
            line = record.envelope.payload.get(LEDGER_LINE_KEY)
            if not isinstance(line, str) or record.state_path is None:
                continue
            journalled = LedgerRecord.model_validate_json(line)
            if ledger_path(Path(record.state_path), journalled.collection) == ledger:
                found.append(record.record_id)
    return found


def _rewritable_bytes(
    context: Epoch2RootContext, repository: Path, path: Path, relative: str
) -> tuple[bytes, bytes]:
    """Return the artifact ledger's bytes in ``HEAD`` and now, once a rewrite is safe.

    Args:
        context: The native context whose WAL is checked.
        repository: The repository root.
        path: The artifact ledger.
        relative: *path* relative to *repository*.

    Returns:
        The published bytes, then the live bytes.

    Raises:
        DaemonValidationError: The WAL holds an unfinished line of the
            ledger, or ``HEAD`` cannot be read.
    """
    unfinished = _unfinished_ledger_records(context.wal_dir, path)
    if unfinished:
        raise _refused(
            REDACTION_WAL_UNFINISHED,
            f"WAL records {', '.join(unfinished)} still journal lines of {relative}; "
            "restart the daemon so recovery finishes them, then retry",
        )
    published = _published_bytes(repository, relative)
    return published, path.read_bytes() if path.exists() else b""


def redact_content(
    context: Epoch2RootContext, args: RedactContentParams, *, now: datetime
) -> RedactContentAnswer:
    """Withhold every flagged line in the artifact ledger's unpublished content rows.

    Args:
        context: The native context of the tree whose ledger is redacted.
        args: The validated request.
        now: The instant the keyed answer records.

    Returns:
        The rows rewritten; none when nothing unpublished is flagged.

    Raises:
        DaemonValidationError: The key already answered other parameters,
            ``HEAD`` cannot be read, the WAL holds an unfinished line of the
            artifact ledger, or the plan is refused because the
            ledger moved off its commit, a line is not canonical or is torn,
            or a line supersedes a row the plan would change. Nothing was written.
    """
    params = args.model_dump(mode="json")
    replayed = keyed_answer(
        context, method=STORE_REDACT_CONTENT_METHOD, key=args.idempotency_key, params=params
    )
    if replayed is not None:
        return RedactContentAnswer.model_validate({**replayed, "replayed": True})
    repository = context.identity.tree_root.parent
    with context.session([project_lock_urn(context)]) as session:
        path = session.ledger_path(Epoch2Collection.ARTIFACT)
        relative = path.relative_to(repository).as_posix()
        published, live = _rewritable_bytes(context, repository, path, relative)
        try:
            plan = plan_content_redaction(published, live, allowed_emails=default_allowed_emails())
            if plan.record_keys:
                replace_unpublished_suffix(
                    path, published=published, expected=live, content=plan.content
                )
        except ContentRedactionError as error:
            raise _refused(error.code, str(error)) from error
        except (LedgerAppendOnlyError, LedgerTornTailError) as error:
            raise _refused("redaction_ledger_refused", str(error)) from error
        try:
            refusals = _drop_refusals(session, published, plan)
        except (LedgerError, ValidationError) as error:
            raise _refused("redaction_ledger_refused", str(error)) from error
    answer = RedactContentAnswer(
        ledger=relative,
        unpublished_rows=plan.unpublished_rows,
        rows=len(plan.record_keys),
        withheld_lines=plan.withheld_lines,
        record_keys=list(plan.record_keys),
        non_content_leak_keys=[leak.record_key for leak in plan.non_content_leaks],
        remediations=[
            LeakRemediation(
                record_key=leak.record_key,
                payload_kind=leak.payload_kind,
                command=(
                    None
                    if refusals[leak.record_key] is not None
                    else (
                        f"{DROP_UNPUBLISHED_ROW_COMMAND} {leak.record_key} "
                        "--idempotency-key <key> --actor <principal>"
                    )
                ),
                refusal=refusals[leak.record_key],
            )
            for leak in plan.non_content_leaks
        ],
    )
    file_keyed_answer(
        context,
        method=STORE_REDACT_CONTENT_METHOD,
        key=args.idempotency_key,
        params=params,
        answer=answer.model_dump(mode="json"),
        at=now,
    )
    logger.info(
        f"redact_content ledger={relative} rows={answer.rows} withheld={answer.withheld_lines} "
        f"non_content_leaks={len(answer.non_content_leak_keys)}"
    )
    return answer


def _drop_refusals(
    session: RootSession, published: bytes, plan: ContentRedaction
) -> dict[str, str | None]:
    """Dry-run the drop of every leaking non-content row the redaction left.

    The redaction names the drop for a row only when the drop's own plan
    would remove it, over the redacted ledger and the same outside records
    the drop reads, so the advice never leads to a refusal.

    Args:
        session: The open root session holding the project lock.
        published: The artifact ledger's bytes in ``HEAD``.
        plan: The redaction just applied.

    Returns:
        Each leaking row's record key with the code its drop refuses with,
        or ``None`` when the drop would remove it.

    Raises:
        LedgerError: Another ledger is torn or its snapshot does not match.
        ValidationError: A record of another ledger does not validate.
    """
    if not plan.non_content_leaks:
        return {}
    elsewhere = _elsewhere(session)
    refusals: dict[str, str | None] = {}
    for leak in plan.non_content_leaks:
        try:
            plan_row_drop(published, plan.content, record_key=leak.record_key, elsewhere=elsewhere)
        except ContentRedactionError as error:
            refusals[leak.record_key] = error.code
        else:
            refusals[leak.record_key] = None
    return refusals


def _elsewhere(session: RootSession) -> list[object]:
    """Return what beyond the artifact ledger may name one of its rows.

    That is the generation document and every record of the other ledgers,
    each as decoded JSON.

    Raises:
        LedgerError: Another ledger is torn or its snapshot does not match.
        ValidationError: A line of another ledger is not a ledger record.
    """
    found: list[object] = [session.read_document()]
    for collection in LEDGER_COLLECTIONS:
        if collection is Epoch2Collection.ARTIFACT:
            continue
        records = read_ledger_records(ledger_path(session.document_path, collection))
        found.extend(record.model_dump(mode="json") for record in records)
    return found


def drop_unpublished_row(
    context: Epoch2RootContext, args: DropUnpublishedRowParams, *, now: datetime
) -> DropUnpublishedRowAnswer:
    """Remove the one unpublished non-content line filed under ``args.record_key``.

    Args:
        context: The native context of the tree whose artifact ledger is changed.
        args: The validated request.
        now: The instant the keyed answer records.

    Returns:
        The line removed.

    Raises:
        DaemonValidationError: The key already answered other parameters,
            ``HEAD`` cannot be read, the WAL holds an unfinished line of the
            artifact ledger, a ledger cannot be read, or the plan is refused:
            the ledger moved off its commit, a line is not canonical, the row
            is published, missing, ambiguous, captured content, a campaign
            artifact revision or a compacted record, it supersedes another
            line, or something else names it. Nothing was written.
    """
    params = args.model_dump(mode="json")
    replayed = keyed_answer(
        context, method=STORE_DROP_UNPUBLISHED_ROW_METHOD, key=args.idempotency_key, params=params
    )
    if replayed is not None:
        return DropUnpublishedRowAnswer.model_validate({**replayed, "replayed": True})
    repository = context.identity.tree_root.parent
    with context.session([project_lock_urn(context)]) as session:
        path = session.ledger_path(Epoch2Collection.ARTIFACT)
        relative = path.relative_to(repository).as_posix()
        published, live = _rewritable_bytes(context, repository, path, relative)
        try:
            plan = plan_row_drop(
                published, live, record_key=args.record_key, elsewhere=_elsewhere(session)
            )
            replace_unpublished_suffix(
                path, published=published, expected=live, content=plan.content
            )
        except ContentRedactionError as error:
            raise _refused(error.code, str(error)) from error
        except (LedgerError, ValidationError) as error:
            raise _refused("redaction_ledger_refused", str(error)) from error
    answer = DropUnpublishedRowAnswer(
        ledger=relative,
        record_key=plan.record_key,
        payload_kind=plan.payload_kind,
        unpublished_rows=plan.unpublished_rows,
    )
    file_keyed_answer(
        context,
        method=STORE_DROP_UNPUBLISHED_ROW_METHOD,
        key=args.idempotency_key,
        params=params,
        answer=answer.model_dump(mode="json"),
        at=now,
    )
    logger.info(
        f"drop_unpublished_row ledger={relative} record_key={answer.record_key!r} "
        f"unpublished={answer.unpublished_rows}"
    )
    return answer


@native_mutator(STORE_DROP_UNPUBLISHED_ROW_METHOD)
async def _drop_unpublished_row(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Remove one unpublished non-content line from the artifact ledger."""
    args = native_params(DropUnpublishedRowParams, params)
    context = ctx.native_root_context(authority.root)
    answer = await asyncio.to_thread(drop_unpublished_row, context, args, now=datetime.now(UTC))
    return answer.model_dump(mode="json")


@native_mutator(STORE_REDACT_CONTENT_METHOD)
async def _redact_content(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Withhold leaked lines from the artifact ledger's unpublished content rows."""
    args = native_params(RedactContentParams, params)
    context = ctx.native_root_context(authority.root)
    answer = await asyncio.to_thread(redact_content, context, args, now=datetime.now(UTC))
    return answer.model_dump(mode="json")


__all__ = [
    "DROP_UNPUBLISHED_ROW_COMMAND",
    "REDACTION_NO_COMMIT",
    "REDACTION_WAL_UNFINISHED",
    "STORE_DROP_UNPUBLISHED_ROW_METHOD",
    "STORE_REDACT_CONTENT_METHOD",
    "DropUnpublishedRowAnswer",
    "DropUnpublishedRowParams",
    "LeakRemediation",
    "RedactContentAnswer",
    "RedactContentParams",
    "drop_unpublished_row",
    "redact_content",
]
