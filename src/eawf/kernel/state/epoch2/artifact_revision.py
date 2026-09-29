"""ArtifactRevision: what an artifact card names about a revision without opening it.

A Campaign lists the artifact revisions it produced and each plan step
lists what it produced. Every such revision resolves to this record, which
carries beside the content reference the facts the artifact card prints:
the file name, its media kind, its size, when and by which Run and step it
was written, the digest of its content and the Campaign that keeps it. A
card that renders content whose size or digest disagrees with the record
is rendering some other revision, which :func:`verify_revision_content`
refuses.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, ConfigDict, StringConstraints

from eawf.kernel.state.epoch2.base import (
    Epoch2Model,
    Sha256DigestStr,
    StrictNonNegativeInt,
    StrictPositiveInt,
)
from eawf.kernel.state.epoch2.urns import CampaignUrn, RunUrn
from eawf.kernel.state.models import UrnStr
from eawf.kernel.state.types import UtcDatetime


def _stays_in_the_repository(value: str) -> str:
    if ".." in value.split("/"):
        raise ValueError(f"file name {value!r} walks out of the repository")
    return value


#: A bare file name or a repo-relative path. An absolute path, a backslash
#: or a parent traversal would name a file outside the repository the
#: revision is kept in.
FileName = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=255, pattern=r"^[^/\\][^\\\x00]*$"),
    AfterValidator(_stays_in_the_repository),
]


class MediaKind(StrEnum):
    """How an artifact's content is drawn."""

    MARKDOWN = "markdown"
    TABLE = "table"
    PLAIN = "plain"
    BINARY = "binary"


class RevisionWriter(Epoch2Model):
    """The Run that wrote a revision, and its step when a step wrote it.

    Attributes:
        run_ref: The writing Run.
        step_ordinal: The producing plan step, from one, when a step wrote it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_ref: RunUrn
    step_ordinal: StrictPositiveInt | None = None


class ArtifactRevision(Epoch2Model):
    """One immutable revision of a Campaign artifact.

    Attributes:
        artifact_ref: The artifact this is a revision of.
        revision: The revision number, from one.
        content_ref: Where the content is stored.
        file_name: The file name the card prints.
        media_kind: How the content is drawn.
        size_bytes: The content's size.
        written_at: When it was written.
        written_by: The Run, and step, that wrote it.
        digest: The ``sha256`` of the content.
        kept_with: The Campaign that keeps it.

    Raises:
        pydantic.ValidationError: A missing digest, a media kind outside the
            four, or a file name that leaves the repository.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_ref: UrnStr
    revision: StrictPositiveInt
    content_ref: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500)]
    file_name: FileName
    media_kind: MediaKind
    size_bytes: StrictNonNegativeInt
    written_at: UtcDatetime
    written_by: RevisionWriter
    digest: Sha256DigestStr
    kept_with: CampaignUrn


def verify_revision_content(revision: ArtifactRevision, content: bytes) -> None:
    """Refuse *content* unless it is the content *revision* records.

    Args:
        revision: The record the card names.
        content: The bytes the card is about to render.

    Raises:
        ValueError: The content's size or digest disagrees with the record.
    """
    if len(content) != revision.size_bytes:
        raise ValueError(
            f"{revision.file_name} r{revision.revision} records {revision.size_bytes} bytes; "
            f"the content has {len(content)}"
        )
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    if digest != revision.digest:
        raise ValueError(
            f"{revision.file_name} r{revision.revision} records {revision.digest}; "
            f"the content digests to {digest}"
        )


__all__ = ["ArtifactRevision", "MediaKind", "RevisionWriter", "verify_revision_content"]
