"""Withholding leaked lines from content rows the last commit does not hold yet.

Captured content is scrubbed when it is filed (:func:`~eawf.kernel.runtime.content.bound_content`),
so a row carries a leak only when the scan learned a shape after the row was
written. Rows already committed stay as they are: append-only protects what a
clone may hold. Rows past the committed prefix are on this machine alone, and
this module plans their correction: each kept line the scan now flags becomes
:data:`~eawf.kernel.runtime.content.WITHHELD_LINE`.

A row keeps its ``artifact_ref``. Receipts and transcripts cite content by that
reference, so it keeps naming the content as first captured; the lines it
resolves to are the redacted ones.

The planner reads bytes and returns bytes; the caller holds the locks and does
the write.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final

from eawf.kernel.runtime.content import CONTENT_PAYLOAD_KIND, StoredContent, scrub_line
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

#: Refusal: an unpublished line that is not captured content carries a leak shape.
REDACTION_LEAK_OUTSIDE_CONTENT: Final = "redaction_leak_outside_content"

#: Refusal: a line supersedes a content row the redaction would change.
REDACTION_ROW_CITED: Final = "redaction_row_cited"


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
class ContentRedaction:
    """The planned correction of one ledger's unpublished content rows.

    Attributes:
        content: The whole proposed file: the published bytes unchanged,
            then every unpublished line, redacted where planned.
        record_keys: The record key of each content row that changes, in
            file order.
        withheld_lines: How many kept lines the plan newly withholds.
        unpublished_rows: How many lines follow the published prefix.
    """

    content: bytes
    record_keys: tuple[str, ...]
    withheld_lines: int
    unpublished_rows: int


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
        The plan; its ``content`` equals *live* when nothing is flagged.

    Raises:
        ContentRedactionError: *live* does not start with *published*, an
            unpublished line is not canonical, a line that is not captured
            content leaks, or a line supersedes a row the plan changes.
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
    for index, line in enumerate(unpublished):
        record = LedgerRecord.model_validate_json(line)
        if render_ledger_line(record) != line:
            raise ContentRedactionError(
                REDACTION_NONCANONICAL_LINE, f"unpublished line {index} is not canonical"
            )
        if record.payload.get("payload_kind") != CONTENT_PAYLOAD_KIND:
            # Scanned as decoded strings: in the rendered line a JSON escape
            # such as ``\n`` hides where a path starts.
            if diff_state_leaks(
                None, record.model_dump(mode="json"), allowed_emails=allowed_emails
            ):
                raise ContentRedactionError(
                    REDACTION_LEAK_OUTSIDE_CONTENT,
                    f"unpublished line {index} ({record.record_key}) leaks outside content lines",
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
        f"withheld={withheld}"
    )
    return ContentRedaction(
        content=content,
        record_keys=tuple(changed.values()),
        withheld_lines=withheld,
        unpublished_rows=len(unpublished),
    )


__all__ = [
    "REDACTION_LEAK_OUTSIDE_CONTENT",
    "REDACTION_NONCANONICAL_LINE",
    "REDACTION_PUBLISHED_CHANGED",
    "REDACTION_ROW_CITED",
    "ContentRedaction",
    "ContentRedactionError",
    "plan_content_redaction",
]
