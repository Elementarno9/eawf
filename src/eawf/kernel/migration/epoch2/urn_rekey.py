"""Plan the re-key of three-slot URNs onto the single project key.

The cutover addressed every record as
``eawf://<workspace>/<project>/<repository-or-_>/<kind>/<key>`` and filled
all three container slots with the project code, so every URN of a tree
reads ``eawf://EAWF/EAWF/EAWF/...``. The slots collapse into one project
key, ``eawf://<project-key>/<kind>/<key>``.

A ledger line is never edited, so the re-key is planned as one appended
row per effective line that holds an old URN: the row keeps the line's
collection, key, status and every non-URN payload value, rewrites the
URNs, and names the old line's digest in ``supersedes`` so the old line
drops out of the effective view. The generation document is rewritten
whole, because it is replaced atomically rather than appended to.

Planning reads and never writes; applying the plan is the caller's
transaction. A value is an old URN only when the whole string is one,
so prose that merely mentions the old shape is left byte for byte.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from eawf.kernel.identity.keys import EntityKind, validate_symbol_key
from eawf.kernel.identity.urn import URN_SCHEME, parse_qualified_urn
from eawf.kernel.store.compaction import read_document
from eawf.kernel.store.ledger import (
    LedgerRecord,
    line_digest,
    render_ledger_line,
    split_ledger_lines,
)
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection, StorageTier, tier_for

#: The single project key every three-slot URN collapses onto by default.
DEFAULT_PROJECT_KEY: Final = "EAWF"

# Shape only: five slash-separated segments after the scheme. Whether the
# segments are valid is the qualified-URN parser's call, so a value with
# the old shape but a broken slot fails loudly instead of being skipped.
_OLD_SHAPE_RE: Final = re.compile(r"^eawf://(?:[^/#]+/){4}[^/#]+(?:#[^#]*)?$")

# A container addresses itself by its own key, so once the three
# container slots collapse its entity key is the project key too.
_CONTAINER_KINDS: Final = frozenset(
    {EntityKind.WORKSPACE, EntityKind.PROJECT, EntityKind.REPOSITORY}
)


@dataclass(frozen=True, slots=True)
class RekeyRow:
    """One superseding row the re-key appends to a ledger.

    Attributes:
        ledger: The ledger file the row is appended to.
        record: The superseding record, its ``supersedes`` naming the
            digest of the line it replaces.
    """

    ledger: Path
    record: LedgerRecord


@dataclass(frozen=True, slots=True)
class UrnRekeyPlan:
    """Everything one re-key of a generation would write.

    Attributes:
        project_key: The key every old URN is re-keyed onto.
        rows: The superseding rows, grouped by ledger in collection
            order and in file order within a ledger.
        ledger_appends: The exact bytes to append to each ledger that
            gains rows, keyed by ledger path.
        document: The rewritten generation document, or ``None`` when it
            holds no old URN.
    """

    project_key: str
    rows: tuple[RekeyRow, ...]
    ledger_appends: Mapping[Path, bytes]
    document: dict[str, Any] | None


def rekey_urn(value: str, project_key: str) -> str | None:
    """Return the single-key form of an old three-slot URN.

    Args:
        value: Any string.
        project_key: The validated key the URN collapses onto.

    Returns:
        ``eawf://<project-key>/<kind>/<key>[#rung-N]`` when *value* is a
        whole three-slot URN, ``None`` when it is anything else.

    Raises:
        IdentityError: *value* has the three-slot shape but a slot, kind,
            key or fragment the qualified-URN grammar refuses.
    """
    if not _OLD_SHAPE_RE.fullmatch(value):
        return None
    urn = parse_qualified_urn(value)
    entity_key = project_key if urn.kind in _CONTAINER_KINDS else urn.entity_key
    rekeyed = f"{URN_SCHEME}{project_key}/{urn.kind.value}/{entity_key}"
    if urn.rung is None:
        return rekeyed
    return f"{rekeyed}#rung-{urn.rung}"


def rekey_value(value: Any, project_key: str) -> Any:
    """Return *value* with every whole-string old URN re-keyed, recursively.

    Args:
        value: Any JSON value.
        project_key: The validated key old URNs collapse onto.

    Returns:
        A copy of *value* in which each whole-string three-slot URN is its
        single-key form; every other value is kept as it was.

    Raises:
        IdentityError: A string has the three-slot shape but does not parse.
    """
    if isinstance(value, str):
        rekeyed = rekey_urn(value, project_key)
        return value if rekeyed is None else rekeyed
    if isinstance(value, dict):
        return {key: rekey_value(item, project_key) for key, item in value.items()}
    if isinstance(value, list):
        return [rekey_value(item, project_key) for item in value]
    return value


def _ledger_rows(path: Path, project_key: str, recorded_at: datetime) -> tuple[RekeyRow, ...]:
    """Plan the superseding rows for one ledger file."""
    if not path.is_file():
        return ()
    lines = split_ledger_lines(path.read_bytes())
    records = [LedgerRecord.model_validate_json(line) for line in lines]
    superseded = {record.supersedes for record in records if record.supersedes is not None}
    rows: list[RekeyRow] = []
    for line, record in zip(lines, records, strict=True):
        digest = line_digest(line)
        if digest in superseded:
            continue
        payload = rekey_value(record.payload, project_key)
        if payload == record.payload:
            continue
        rows.append(
            RekeyRow(
                ledger=path,
                record=LedgerRecord(
                    collection=record.collection,
                    record_key=record.record_key,
                    status=record.status,
                    recorded_at=recorded_at,
                    supersedes=digest,
                    payload=payload,
                ),
            )
        )
    return tuple(rows)


def plan_urn_rekey(
    state_path: Path,
    *,
    recorded_at: datetime,
    project_key: str = DEFAULT_PROJECT_KEY,
) -> UrnRekeyPlan:
    """Plan the URN re-key of the generation whose document is *state_path*.

    Args:
        state_path: The generation's ``state.json``; its ledgers are the
            live ledger of every ledger-tier collection.
        recorded_at: The UTC instant every superseding row records.
        project_key: The single key old URNs collapse onto.

    Returns:
        The plan; an already re-keyed generation plans no row and no
        document.

    Raises:
        IdentityError: *project_key* is not a valid symbol key, or a
            stored value has the three-slot shape but does not parse.
        FileNotFoundError: The generation document does not exist.
        LedgerTornTailError: A ledger ends mid-line.
        ValidationError: A ledger line is not a ledger record, or
            *recorded_at* is not timezone-aware UTC.
    """
    validate_symbol_key(project_key, slot="project")
    rows: list[RekeyRow] = []
    for collection in Epoch2Collection:
        if tier_for(collection) is StorageTier.LEDGER:
            rows.extend(_ledger_rows(ledger_path(state_path, collection), project_key, recorded_at))
    appends: dict[Path, bytes] = {}
    for row in rows:
        line = f"{render_ledger_line(row.record)}\n".encode()
        appends[row.ledger] = appends.get(row.ledger, b"") + line
    original = read_document(state_path)
    rewritten = rekey_value(original, project_key)
    return UrnRekeyPlan(
        project_key=project_key,
        rows=tuple(rows),
        ledger_appends=appends,
        document=None if rewritten == original else rewritten,
    )


__all__ = [
    "DEFAULT_PROJECT_KEY",
    "RekeyRow",
    "UrnRekeyPlan",
    "plan_urn_rekey",
    "rekey_urn",
    "rekey_value",
]
