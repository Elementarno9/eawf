"""``runtime.store.redact_content``: withhold leaked lines from content no commit holds yet.

Captured content is scrubbed when it is filed, so a stored row carries a leak
only when the leak scan learned a shape after the row was written. This verb
plans the correction with :func:`~eawf.kernel.runtime.content_redaction.plan_content_redaction`
against the artifact ledger's bytes in the repository's ``HEAD`` commit, and
replaces only the lines after them. Committed rows stay as they are.

A second call finds nothing left to withhold and writes nothing; a retry under
the same idempotency key replays the first answer.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from eawf.kernel.runtime.content_redaction import ContentRedactionError, plan_content_redaction
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import PrincipalKey
from eawf.kernel.store.ledger import (
    LedgerAppendOnlyError,
    LedgerRecord,
    LedgerTornTailError,
    replace_unpublished_suffix,
)
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.observability.logging.state_leak import default_allowed_emails
from eawf.platform.subprocess_detach import no_window_kwargs
from eawf.runtime.daemon.epoch2_recovery import LEDGER_LINE_KEY
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.delivery import IdempotencyKey, file_keyed_answer, keyed_answer
from eawf.runtime.daemon.methods.doctor import project_lock_urn
from eawf.runtime.daemon.native_guard import native_mutator, native_params
from eawf.runtime.daemon.wal import WalStatus, list_records, read_record

logger = logging.getLogger(__name__)

#: The verb that withholds leaked lines from the artifact ledger's unpublished content rows.
STORE_REDACT_CONTENT_METHOD: Final = "runtime.store.redact_content"

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
        replayed: Whether the answer is the stored one of an earlier call.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ledger: str
    unpublished_rows: int
    rows: int
    withheld_lines: int
    record_keys: list[str]
    non_content_leak_keys: list[str]
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
        unfinished = _unfinished_ledger_records(context.wal_dir, path)
        if unfinished:
            raise _refused(
                REDACTION_WAL_UNFINISHED,
                f"WAL records {', '.join(unfinished)} still journal lines of {relative}; "
                "restart the daemon so recovery finishes them, then retry",
            )
        published = _published_bytes(repository, relative)
        live = path.read_bytes() if path.exists() else b""
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
    answer = RedactContentAnswer(
        ledger=relative,
        unpublished_rows=plan.unpublished_rows,
        rows=len(plan.record_keys),
        withheld_lines=plan.withheld_lines,
        record_keys=list(plan.record_keys),
        non_content_leak_keys=list(plan.non_content_leak_keys),
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
    "REDACTION_NO_COMMIT",
    "REDACTION_WAL_UNFINISHED",
    "STORE_REDACT_CONTENT_METHOD",
    "RedactContentAnswer",
    "RedactContentParams",
    "redact_content",
]
