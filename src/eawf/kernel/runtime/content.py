"""The bounded, scrubbed text a transcript block unfolds to.

A tool's output, a diff or a trace can be as long as the process that wrote it and can
carry a home path, an address or a token. The transcript never reads such text raw: it
reads a :class:`StoredContent`, which keeps at most :data:`CONTENT_LINE_LIMIT` lines of
at most :data:`CONTENT_LINE_CHARS` characters each, withholds whole every line the
state-leak scan flags, and states how many lines it did not keep. A withheld value is
never stored, so nothing downstream of the store can leak it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Annotated, Final, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from eawf.kernel.runtime.provider import ArtifactUrn, RuntimeRecord
from eawf.kernel.state.epoch2.base import StrictNonNegativeInt
from eawf.kernel.state.epoch2.urns import RunUrn
from eawf.kernel.state.types import UtcDatetime
from eawf.observability.logging.state_leak import default_allowed_emails, scan_state_leaks

#: The discriminator of a stored content line in the artifact ledger.
CONTENT_PAYLOAD_KIND: Final = "content_artifact"

#: The most lines one stored content keeps.
CONTENT_LINE_LIMIT: Final = 200

#: The longest line one stored content keeps; a longer line is cut.
CONTENT_LINE_CHARS: Final = 500

#: The most characters one stored content keeps across all its lines.
CONTENT_CHAR_LIMIT: Final = 16_384

#: What a withheld line says in place of its text.
WITHHELD_LINE: Final = "this line carries a path, an address or a token shape and is withheld"

ContentLine = Annotated[str, StringConstraints(strict=True, max_length=CONTENT_LINE_CHARS)]


@dataclass(frozen=True, slots=True)
class BoundedContent:
    """Text cut to the store's bounds, with what the cut left out.

    Attributes:
        lines: The lines kept, withheld ones replaced by :data:`WITHHELD_LINE`.
        total_lines: How many lines the source text had.
        withheld_lines: How many kept lines were withheld.
    """

    lines: tuple[str, ...]
    total_lines: int
    withheld_lines: int

    @property
    def digest(self) -> str:
        """Return the hex digest a content-addressed reference is named by."""
        body = json.dumps([self.lines, self.total_lines], separators=(",", ":"))
        return hashlib.sha256(body.encode("utf-8")).hexdigest()


def bound_content(text: str) -> BoundedContent:
    """Return *text* as the lines the store may keep.

    Args:
        text: The raw text, such as a tool's output.

    Returns:
        The kept lines, each cut to :data:`CONTENT_LINE_CHARS`, stopping at
        :data:`CONTENT_LINE_LIMIT` lines or :data:`CONTENT_CHAR_LIMIT` characters, and
        every line the state-leak scan flags replaced by :data:`WITHHELD_LINE`.
    """
    source = text.rstrip("\n").splitlines() if text.strip() else []
    allowed = default_allowed_emails()
    kept: list[str] = []
    withheld = 0
    spent = 0
    for raw in source[:CONTENT_LINE_LIMIT]:
        line = raw.rstrip()[:CONTENT_LINE_CHARS]
        if scan_state_leaks(raw, allowed_emails=allowed):
            line = WITHHELD_LINE
            withheld += 1
        if spent + len(line) > CONTENT_CHAR_LIMIT:
            break
        kept.append(line)
        spent += len(line)
    return BoundedContent(lines=tuple(kept), total_lines=len(source), withheld_lines=withheld)


class StoredContent(RuntimeRecord):
    """One bounded content as the artifact ledger holds it.

    Attributes:
        payload_kind: The discriminator separating it from other artifact lines.
        artifact_ref: The content-addressed reference a transcript names it by.
        run_ref: The Run the content was produced by.
        lines: The kept lines.
        total_lines: How many lines the source had, so a reader can state how many it
            is not showing.
        withheld_lines: How many kept lines were withheld whole.
        recorded_at: When the daemon filed it.
    """

    payload_kind: Literal["content_artifact"] = CONTENT_PAYLOAD_KIND
    artifact_ref: ArtifactUrn
    run_ref: RunUrn
    lines: Annotated[tuple[ContentLine, ...], Field(max_length=CONTENT_LINE_LIMIT)]
    total_lines: StrictNonNegativeInt
    withheld_lines: StrictNonNegativeInt
    recorded_at: UtcDatetime

    @model_validator(mode="after")
    def _counts_cover_the_kept_lines(self) -> Self:
        """Refuse counts that describe fewer lines than are kept.

        Raises:
            ValueError: The source count is below the kept count, or more lines are
                withheld than kept.
        """
        if self.total_lines < len(self.lines) or self.withheld_lines > len(self.lines):
            raise ValueError("a stored content's counts do not cover its kept lines")
        return self


class ResolvedContent(RuntimeRecord):
    """The content one transcript reference resolves to, as a reader receives it.

    Attributes:
        ref: The reference asked for: an artifact, a call or a receipt.
        lines: The kept lines.
        total_lines: How many lines the source had.
        withheld_lines: How many kept lines were withheld whole.
    """

    ref: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256)]
    lines: Annotated[tuple[ContentLine, ...], Field(max_length=CONTENT_LINE_LIMIT)]
    total_lines: StrictNonNegativeInt
    withheld_lines: StrictNonNegativeInt

    @property
    def unkept(self) -> int:
        """Return how many source lines the store did not keep."""
        return max(0, self.total_lines - len(self.lines))


__all__ = [
    "CONTENT_CHAR_LIMIT",
    "CONTENT_LINE_CHARS",
    "CONTENT_LINE_LIMIT",
    "CONTENT_PAYLOAD_KIND",
    "WITHHELD_LINE",
    "BoundedContent",
    "ResolvedContent",
    "StoredContent",
    "bound_content",
]
