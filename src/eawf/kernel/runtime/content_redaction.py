"""Withholding leaked lines from content rows the last commit does not hold yet.

Captured content is scrubbed when it is filed (:func:`~eawf.kernel.runtime.content.bound_content`),
so a row carries a leak only when the scan learned a shape after the row was
written. Rows already committed stay as they are: append-only protects what a
clone may hold. Rows past the committed prefix are on this machine alone, and
this module plans their correction: each kept line the scan now flags becomes
:data:`~eawf.kernel.runtime.content.WITHHELD_LINE`. An unpublished row that is
not captured content has no withheld marker to take: several such kinds carry a
digest over their own text that an approval or citation binds, so rewriting
the text would break them. A leak there is reported by record key and kind,
and :func:`plan_row_drop` plans removing that one line instead, while nothing
else names it.

A row keeps its ``artifact_ref``. Receipts and transcripts cite content by that
reference, so it keeps naming the content as first captured; the lines it
resolves to are the redacted ones.

The planner reads bytes and returns bytes; the caller holds the locks and does
the write.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from eawf.kernel.runtime.content import CONTENT_PAYLOAD_KIND, StoredContent, scrub_line
from eawf.kernel.state.epoch2.artifact_revision import ARTIFACT_REVISION_PAYLOAD_KIND
from eawf.kernel.store.ledger import (
    LedgerRecord,
    line_digest,
    render_ledger_line,
    split_ledger_lines,
)
from eawf.observability.logging.state_leak import diff_state_leaks

logger = logging.getLogger(__name__)

#: Refusal: the ledger no longer starts with the bytes its last commit holds.
REDACTION_PUBLISHED_CHANGED: Final = "redaction_published_changed"

#: Refusal: an unpublished line does not read back as the line its record renders.
REDACTION_NONCANONICAL_LINE: Final = "redaction_noncanonical_line"

#: Refusal: a line supersedes a content row the redaction would change, or
#: something else names the row a drop would remove.
REDACTION_ROW_CITED: Final = "redaction_row_cited"

#: Refusal: the row a drop names is held by the last commit.
REDACTION_ROW_PUBLISHED: Final = "redaction_row_published"

#: Refusal: no unpublished line carries the record key a drop names.
REDACTION_ROW_NOT_FOUND: Final = "redaction_row_not_found"

#: Refusal: more than one unpublished line carries the record key a drop names.
REDACTION_ROW_AMBIGUOUS: Final = "redaction_row_ambiguous"

#: Refusal: the row a drop names is captured content, which redaction corrects in place.
REDACTION_ROW_CONTENT: Final = "redaction_row_content"

#: Refusal: the row a drop names is a record compacted out of the document; readers
#: take it as that record's standing state and the change feed holds its history.
REDACTION_ROW_COMPACTED: Final = "redaction_row_compacted"

#: Refusal: the row a drop names is a campaign artifact revision. A later revision
#: reuses the highest artifact key, so a reference to a dropped one would resolve
#: to different content.
REDACTION_ROW_REVISION: Final = "redaction_row_revision"

#: Refusal: the row a drop names supersedes another line, which removing it would revive.
REDACTION_ROW_SUPERSEDES: Final = "redaction_row_supersedes"


class ContentRedactionError(ValueError):
    """A redaction was refused before anything was written.

    Attributes:
        code: The refusal constant naming why.
    """

    def __init__(self, code: str, detail: str) -> None:
        """Build the refusal.

        Args:
            code: One of the ``REDACTION_*`` constants.
            detail: What was found, without the leaked text.
        """
        super().__init__(f"{code}: {detail}")
        self.code = code


@dataclass(frozen=True)
class NonContentLeak:
    """One unpublished row that is not captured content and still leaks.

    Attributes:
        record_key: The row's record key.
        payload_kind: The ``payload_kind`` its payload names, or ``None``
            when it names none.
        drop_refusal: The ``REDACTION_*`` code a drop of the row refuses
            with whatever cites it, or ``None`` when a drop can remove it.
    """

    record_key: str
    payload_kind: str | None
    drop_refusal: str | None = None


@dataclass(frozen=True)
class ContentRedaction:
    """The planned correction of one ledger's unpublished content rows.

    Attributes:
        content: The whole proposed file: the published bytes unchanged,
            then every unpublished line, redacted where planned.
        record_keys: The record key of each content row that changes, in
            file order.
        withheld_lines: How many kept lines the plan newly withholds.
        unpublished_rows: How many lines follow the published prefix.
        non_content_leaks: Each unpublished line that is not captured
            content and still carries a leak shape, in file order; the plan
            leaves those lines unchanged.
    """

    content: bytes
    record_keys: tuple[str, ...]
    withheld_lines: int
    unpublished_rows: int
    non_content_leaks: tuple[NonContentLeak, ...]


def plan_content_redaction(
    published: bytes, live: bytes, *, allowed_emails: frozenset[str]
) -> ContentRedaction:
    """Plan the withholding of every flagged line in unpublished content rows.

    Args:
        published: The ledger's bytes in the last commit; empty when no
            commit holds the file.
        live: The ledger's bytes now.
        allowed_emails: Casefolded addresses the leak scan lets through.

    Returns:
        The plan; its ``content`` equals *live* when no content line is
        flagged.

    Raises:
        ContentRedactionError: *live* does not start with *published*, an
            unpublished line is not canonical, or a line supersedes a row
            the plan changes.
        LedgerTornTailError: Either byte string ends mid-line.
    """
    if not live.startswith(published):
        raise ContentRedactionError(
            REDACTION_PUBLISHED_CHANGED, "the ledger does not start with its committed bytes"
        )
    published_lines = split_ledger_lines(published)
    unpublished = split_ledger_lines(live[len(published) :])
    planned: list[str] = []
    changed: dict[str, str] = {}
    withheld = 0
    leaking: list[NonContentLeak] = []
    for line, record in zip(unpublished, _canonical_records(unpublished), strict=True):
        kind = _payload_kind(record)
        if kind != CONTENT_PAYLOAD_KIND:
            # Scanned as decoded strings: in the rendered line a JSON escape
            # such as ``\n`` hides where a path starts.
            if diff_state_leaks(
                None, record.model_dump(mode="json"), allowed_emails=allowed_emails
            ):
                leaking.append(
                    NonContentLeak(
                        record_key=record.record_key,
                        payload_kind=kind,
                        drop_refusal=_drop_refusal(record, kind),
                    )
                )
            planned.append(line)
            continue
        stored = StoredContent.model_validate(record.payload)
        kept: list[str] = []
        newly = 0
        for text in stored.lines:
            scrubbed, flagged = scrub_line(text, allowed_emails=allowed_emails)
            newly += int(flagged)
            kept.append(scrubbed if flagged else text)
        if not newly:
            planned.append(line)
            continue
        payload = {
            **record.payload,
            "lines": kept,
            "withheld_lines": stored.withheld_lines + newly,
        }
        StoredContent.model_validate(payload)
        redacted = render_ledger_line(record.model_copy(update={"payload": payload}))
        changed[line_digest(line)] = record.record_key
        withheld += newly
        planned.append(redacted)
    for line in (*published_lines, *planned):
        cited = LedgerRecord.model_validate_json(line).supersedes
        if cited in changed:
            raise ContentRedactionError(
                REDACTION_ROW_CITED, f"a line supersedes content row {changed[cited]}"
            )
    content = published + "".join(f"{line}\n" for line in planned).encode("utf-8")
    logger.debug(
        f"plan_content_redaction unpublished={len(unpublished)} rows={len(changed)} "
        f"withheld={withheld} non_content_leaks={len(leaking)}"
    )
    return ContentRedaction(
        content=content,
        record_keys=tuple(changed.values()),
        withheld_lines=withheld,
        unpublished_rows=len(unpublished),
        non_content_leaks=tuple(leaking),
    )


@dataclass(frozen=True)
class RowDrop:
    """The planned removal of one unpublished non-content line.

    Attributes:
        content: The whole proposed file: the published bytes unchanged,
            then every other unpublished line as it was.
        record_key: The record key of the line removed.
        payload_kind: The ``payload_kind`` its payload named, or ``None``.
        unpublished_rows: How many lines follow the published prefix after
            the removal.
    """

    content: bytes
    record_key: str
    payload_kind: str | None
    unpublished_rows: int


def plan_row_drop(
    published: bytes, live: bytes, *, record_key: str, elsewhere: Sequence[object]
) -> RowDrop:
    """Plan removing the one unpublished non-content line filed under *record_key*.

    No commit holds the line, so no clone can hold it either; removing it
    leaves the ledger as if the row had never been filed. That holds only
    while nothing else names the row and the row replaces nothing, so any
    line of this ledger that supersedes it or carries its key as a whole
    token, any value in *elsewhere* that does, a row that itself supersedes
    a line, and a campaign artifact revision refuse the plan.

    Args:
        published: The ledger's bytes in the last commit; empty when no
            commit holds the file.
        live: The ledger's bytes now.
        record_key: The record key of the line to remove.
        elsewhere: Decoded JSON values beyond this ledger that may name the
            row: the generation document and the other ledgers' records.

    Returns:
        The plan.

    Raises:
        ContentRedactionError: *live* does not start with *published*, an
            unpublished line is not canonical, the key is published, absent
            or on several unpublished lines, the row is captured content, a
            campaign artifact revision or a compacted record, it supersedes
            another line, or something else names it.
        LedgerTornTailError: Either byte string ends mid-line.
    """
    if not live.startswith(published):
        raise ContentRedactionError(
            REDACTION_PUBLISHED_CHANGED, "the ledger does not start with its committed bytes"
        )
    published_lines = split_ledger_lines(published)
    published_records = [LedgerRecord.model_validate_json(line) for line in published_lines]
    if any(record.record_key == record_key for record in published_records):
        raise ContentRedactionError(
            REDACTION_ROW_PUBLISHED, f"the last commit holds a line filed under {record_key}"
        )
    unpublished = split_ledger_lines(live[len(published) :])
    records = _canonical_records(unpublished)
    matches = [index for index, record in enumerate(records) if record.record_key == record_key]
    if not matches:
        raise ContentRedactionError(
            REDACTION_ROW_NOT_FOUND, f"no unpublished line is filed under {record_key}"
        )
    if len(matches) > 1:
        raise ContentRedactionError(
            REDACTION_ROW_AMBIGUOUS,
            f"{len(matches)} unpublished lines are filed under {record_key}",
        )
    [target] = matches
    record = records[target]
    kind = _payload_kind(record)
    _refuse_undroppable(record, kind)
    digest = line_digest(unpublished[target])
    token = _token_pattern(record_key)
    others = [*published_records, *(item for index, item in enumerate(records) if index != target)]
    for other in others:
        if other.supersedes == digest or _names(other.model_dump(mode="json"), token):
            raise ContentRedactionError(
                REDACTION_ROW_CITED, f"line {other.record_key} names {record_key}"
            )
    if any(_names(value, token) for value in elsewhere):
        raise ContentRedactionError(
            REDACTION_ROW_CITED, f"a record outside the artifact ledger names {record_key}"
        )
    kept = [line for index, line in enumerate(unpublished) if index != target]
    logger.debug(f"plan_row_drop record_key={record_key} unpublished={len(unpublished)}")
    return RowDrop(
        content=published + "".join(f"{line}\n" for line in kept).encode("utf-8"),
        record_key=record_key,
        payload_kind=kind,
        unpublished_rows=len(kept),
    )


def _refuse_undroppable(record: LedgerRecord, kind: str | None) -> None:
    """Refuse a drop whose target row cannot leave the ledger whatever cites it.

    Args:
        record: The unpublished row a drop names.
        kind: The ``payload_kind`` its payload names, or ``None``.

    Raises:
        ContentRedactionError: The row is captured content, a campaign
            artifact revision or a compacted record, or it supersedes
            another line.
    """
    record_key = record.record_key
    if kind == CONTENT_PAYLOAD_KIND:
        raise ContentRedactionError(
            REDACTION_ROW_CONTENT,
            f"{record_key} is captured content; eawf store redact-content withholds its lines",
        )
    if kind == ARTIFACT_REVISION_PAYLOAD_KIND:
        raise ContentRedactionError(
            REDACTION_ROW_REVISION,
            f"{record_key} is a campaign artifact revision; its key would be filed again",
        )
    if record.payload.get("key") == record_key:
        raise ContentRedactionError(
            REDACTION_ROW_COMPACTED, f"{record_key} is a record compacted out of the document"
        )
    if record.supersedes is not None:
        raise ContentRedactionError(
            REDACTION_ROW_SUPERSEDES,
            f"{record_key} supersedes another line, which dropping it would revive",
        )


def _drop_refusal(record: LedgerRecord, kind: str | None) -> str | None:
    """Return the code a drop of *record* refuses with whatever cites it, if any.

    A redaction answers a leaking row with the drop only when the drop can
    remove it; a row the drop always refuses would send the operator to a
    command that cannot succeed.

    Args:
        record: An unpublished row that leaks.
        kind: The ``payload_kind`` its payload names, or ``None``.

    Returns:
        The ``REDACTION_*`` code, or ``None`` when only a citation could stop
        the drop.
    """
    try:
        _refuse_undroppable(record, kind)
    except ContentRedactionError as error:
        return error.code
    return None


def _canonical_records(lines: Sequence[str]) -> list[LedgerRecord]:
    """Return the record of each unpublished line, refusing one that is not canonical.

    Raises:
        ContentRedactionError: A line does not read back as the line its record renders.
    """
    records: list[LedgerRecord] = []
    for index, line in enumerate(lines):
        record = LedgerRecord.model_validate_json(line)
        if render_ledger_line(record) != line:
            raise ContentRedactionError(
                REDACTION_NONCANONICAL_LINE, f"unpublished line {index} is not canonical"
            )
        records.append(record)
    return records


def _payload_kind(record: LedgerRecord) -> str | None:
    """Return the ``payload_kind`` a record's payload names, or ``None``."""
    kind = record.payload.get("payload_kind")
    return kind if isinstance(kind, str) else None


def _token_pattern(record_key: str) -> re.Pattern[str]:
    """Return the pattern matching *record_key* as a whole token inside a string.

    References carry a key inside a longer string, such as an artifact URN
    ending in the key, so an exact match misses them. The key must start the
    string or follow a character that is not alphanumeric, and must end it or
    precede a character that cannot continue a key, so ``EVD-0001`` is not
    found inside ``EVD-00010``.

    Args:
        record_key: The record key to find.

    Returns:
        The compiled pattern.
    """
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(record_key)}(?![A-Za-z0-9_-])")


def _names(value: object, token: re.Pattern[str]) -> bool:
    """Return whether *value* holds *token* in a string or a mapping key, at any depth.

    Args:
        value: A decoded JSON value.
        token: The pattern from :func:`_token_pattern`.

    Returns:
        ``True`` when some string or mapping key contains the token.
    """
    if isinstance(value, str):
        return token.search(value) is not None
    if isinstance(value, Mapping):
        return any(
            (isinstance(key, str) and token.search(key) is not None) or _names(item, token)
            for key, item in value.items()
        )
    if isinstance(value, list | tuple):
        return any(_names(item, token) for item in value)
    return False


__all__ = [
    "REDACTION_NONCANONICAL_LINE",
    "REDACTION_PUBLISHED_CHANGED",
    "REDACTION_ROW_AMBIGUOUS",
    "REDACTION_ROW_CITED",
    "REDACTION_ROW_COMPACTED",
    "REDACTION_ROW_CONTENT",
    "REDACTION_ROW_NOT_FOUND",
    "REDACTION_ROW_PUBLISHED",
    "REDACTION_ROW_REVISION",
    "REDACTION_ROW_SUPERSEDES",
    "ContentRedaction",
    "ContentRedactionError",
    "NonContentLeak",
    "RowDrop",
    "plan_content_redaction",
    "plan_row_drop",
]
