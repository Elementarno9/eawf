"""Plan the superseding rows that give each cited local artifact its digest.

Artifacts under ``.ea/artifacts/`` stay out of git, so the ledger row that
cites one is the only thing a clone shares about it. A row that cites a
file by path alone cannot tell a reader whether the local copy is the
file that was cited. The repair supersedes each such row with the same
record carrying the file's sha256 and size, computed from the local copy;
a row whose file is absent here is reported and never guessed.
"""

from __future__ import annotations

import copy
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import orjson
from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.migration.epoch2.origins import source_digest
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.store.ledger import (
    LedgerRecord,
    content_digest,
    line_digest,
    split_ledger_lines,
)
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection

#: The action that appends a digest-carrying row for each cited local artifact.
ARTIFACT_DIGEST_ACTION: Final = "ledger.artifact-digests"

#: The URI prefix of a row that cites a file in the local artifact tree.
ARTIFACT_URI_PREFIX: Final = "repo:.ea/artifacts/"

#: The key an imported row keeps its source row's canonical digest under.
_PAYLOAD_DIGEST_KEY: Final = "payload_digest"


class ArtifactDigest(BaseModel):
    """One superseding row planned for one effective artifact ledger line.

    Attributes:
        line: The 1-based line the row supersedes.
        supersedes: The digest of that line.
        path: The cited artifact, repo-relative.
        sha256: The hex sha256 of the local file.
        size_bytes: The local file's size.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    line: int = Field(ge=1)
    supersedes: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)


class ArtifactDigestPlan(BaseModel):
    """The rows to append, and the cited files this checkout does not hold.

    Attributes:
        rows: The planned rows, in ledger order.
        missing: Each cited artifact path with no local file, in ledger
            order; their rows are left as they are.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    rows: tuple[ArtifactDigest, ...] = ()
    missing: tuple[str, ...] = ()


def selected_artifact_ledger(root: Path) -> Path | None:
    """Return the selected generation's artifact ledger, or ``None`` on an epoch-1 tree.

    Args:
        root: The repository's working directory.

    Returns:
        ``<generation>/ledger/artifact.jsonl``, whether or not it exists.
    """
    authority = resolve_authority(root / ".ea")
    if authority.epoch != 2 or authority.target is None or authority.generation_id is None:
        return None
    document = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    return ledger_path(document, Epoch2Collection.ARTIFACT)


def _cited(record: LedgerRecord) -> dict[str, Any] | None:
    """Return the artifact a line records, when it cites a local file without a digest."""
    inner = record.payload.get("payload")
    if not isinstance(inner, dict):
        return None
    uri = inner.get("uri")
    if not isinstance(uri, str) or not uri.startswith(ARTIFACT_URI_PREFIX):
        return None
    return None if inner.get("sha256") else inner


def plan_artifact_digests(root: Path, ledger: Path) -> ArtifactDigestPlan:
    """Plan one digest-carrying row per effective line citing a local artifact without one.

    Args:
        root: The repository's working directory, which cited paths resolve against.
        ledger: The artifact ledger to read; a missing file plans nothing.

    Returns:
        The rows to append and the cited paths with no local file.

    Raises:
        LedgerTornTailError: The ledger ends mid-line.
        ValidationError: A line is not a ledger record.
    """
    if not ledger.is_file():
        return ArtifactDigestPlan()
    lines = split_ledger_lines(ledger.read_bytes())
    records = [LedgerRecord.model_validate_json(text) for text in lines]
    superseded = {record.supersedes for record in records if record.supersedes is not None}
    rows: list[ArtifactDigest] = []
    missing: list[str] = []
    for number, (text, record) in enumerate(zip(lines, records, strict=True), start=1):
        digest = line_digest(text)
        cited = None if digest in superseded else _cited(record)
        if cited is None:
            continue
        path = str(cited["uri"]).removeprefix("repo:")
        local = root / path
        if not local.is_file():
            missing.append(path)
            continue
        content = local.read_bytes()
        rows.append(
            ArtifactDigest(
                line=number,
                supersedes=digest,
                path=path,
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
            )
        )
    return ArtifactDigestPlan(rows=tuple(rows), missing=tuple(missing))


def artifact_digest_preview(ledger: Path, plan: ArtifactDigestPlan) -> str:
    """Return the preview digest of *plan* over the ledger bytes it was planned from.

    Args:
        ledger: The artifact ledger the plan was taken from.
        plan: The plan.

    Returns:
        A ``sha256:`` digest that changes when the ledger, a planned row or
        a cited file's content changes.
    """
    basis = {
        "rows": [row.model_dump(mode="json") for row in plan.rows],
        "ledger": content_digest(ledger.read_bytes()) if ledger.is_file() else None,
    }
    return f"sha256:{hashlib.sha256(orjson.dumps(basis, option=orjson.OPT_SORT_KEYS)).hexdigest()}"


def digested_record(ledger: Path, row: ArtifactDigest, *, recorded_at: datetime) -> LedgerRecord:
    """Build the record that supersedes *row*'s line with the file's digest.

    An imported line also restates the canonical digest of the row it
    carries, so the restated row stays covered by it; its origin keeps the
    digest of the epoch-1 row it was imported from.

    Args:
        ledger: The artifact ledger the row was planned from.
        row: The planned row.
        recorded_at: The UTC instant the new row records.

    Returns:
        The superseded record with ``sha256`` and ``size_bytes`` filled.

    Raises:
        ValidationError: The line is not a ledger record.
    """
    text = split_ledger_lines(ledger.read_bytes())[row.line - 1]
    record = LedgerRecord.model_validate_json(text)
    payload = copy.deepcopy(record.payload)
    inner = payload["payload"]
    inner["sha256"] = row.sha256
    inner["size_bytes"] = row.size_bytes
    if _PAYLOAD_DIGEST_KEY in payload:
        payload[_PAYLOAD_DIGEST_KEY] = source_digest(inner)
    return LedgerRecord(
        collection=record.collection,
        record_key=record.record_key,
        status=record.status,
        recorded_at=recorded_at,
        supersedes=row.supersedes,
        payload=payload,
    )


__all__ = [
    "ARTIFACT_DIGEST_ACTION",
    "ARTIFACT_URI_PREFIX",
    "ArtifactDigest",
    "ArtifactDigestPlan",
    "artifact_digest_preview",
    "digested_record",
    "plan_artifact_digests",
    "selected_artifact_ledger",
]
