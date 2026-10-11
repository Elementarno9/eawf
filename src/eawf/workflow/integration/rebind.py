"""Plan the ledger rows that rebind a squashed branch's source commits.

A squash writes a branch's commits again as one commit per Batch, so every
ledger row naming a source commit -- a Batch's pinned head, a reconciliation
row's matched head, a Task's integrated binding, a proof receipt's revision --
names a commit no branch will reach once the squashed history replaces it. A
ledger line is never edited, so each such line gains one appended row that
supersedes it by digest and names the rewritten commit instead.

A row that belongs to a Batch -- through a ``batch_ref``, its own Batch key, or
the Batch of the Task it names -- is rebound to the last rewritten commit
carrying that Batch, the commit its Task or Batch trailers now live on; any
other row to the rewritten commit that absorbed the source commit. Where a
rebound head sits beside the binding's other members, they follow it: a
binding's ``parent_sha`` becomes the new head's parent, and an exact revision
binding's ``tree_sha`` its tree. A proof receipt keeps its ``tree_sha``,
because the freshness key covers it and the gate ran on that tree.

Planning reads and never writes; the daemon verb applying the plan verifies
the rewritten commits first.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from eawf.kernel.runtime.content import CONTENT_LINE_CHARS
from eawf.kernel.store.ledger import (
    LedgerRecord,
    line_digest,
    read_ledger_content,
    split_ledger_lines,
)
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection, StorageTier, tier_for
from eawf.workflow.integration.squash import SquashMap

#: A full commit SHA, delimited so a 64-hex digest never yields a match.
_SHA_RE: Final = re.compile(r"(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])")

#: A field value that binds a commit: the bare SHA, or a ``commit:`` reference to it.
_BINDING_RE: Final = re.compile(r"^(?:commit:)?(?P<sha>[0-9a-f]{40})$")

#: What a line quoting a dropped commit says in its place, before the drop's reason.
DROPPED_LINE: Final = "this line named a commit no published branch carries and is withheld"

#: The member an exact revision binding carries beside the head it binds.
_EXACT_BINDING_MARK: Final = "contract_digest"


@dataclass(frozen=True, slots=True)
class RebindRow:
    """One superseding row a rebind appends.

    Attributes:
        record: The superseding record, its ``supersedes`` naming the line it replaces.
        batch_key: The Batch the row was rebound through, or ``None`` when it
            was rebound commit by commit.
        task_key: The Task the row is, for a Task row, else ``None``.
        rebinds: Each source commit the line named, mapped to its new commit, or
            to ``None`` where the reference was dropped.
    """

    record: LedgerRecord
    batch_key: str | None
    task_key: str | None
    rebinds: Mapping[str, str | None]


class RebindDropError(ValueError):
    """A dropped commit is the whole value of a field, which cannot be left empty."""


def _key_of(ref: object) -> str | None:
    """Return the record key a URN names in its last segment."""
    return ref.rsplit("/", 1)[-1] if isinstance(ref, str) and ref else None


def _find(value: Any, field: str) -> str | None:
    """Return the first string *field* holds anywhere in *value*, depth first."""
    if isinstance(value, dict):
        found = value.get(field)
        if isinstance(found, str):
            return found
        for item in value.values():
            nested = _find(item, field)
            if nested is not None:
                return nested
    if isinstance(value, list):
        for item in value:
            nested = _find(item, field)
            if nested is not None:
                return nested
    return None


def row_batch(record: LedgerRecord, task_batches: Mapping[str, str]) -> str | None:
    """Return the Batch a ledger row belongs to, or ``None`` when it names none.

    Args:
        record: The ledger line.
        task_batches: Each Task key mapped to its Batch key.

    Returns:
        The Batch its ``batch_ref`` names, its own key for a Batch row, or the
        Batch of the Task it is or names.
    """
    payload = record.payload
    batch = _key_of(_find(payload, "batch_ref"))
    if batch is not None:
        return batch
    if record.collection is Epoch2Collection.BATCH and payload.get("key") == record.record_key:
        return record.record_key
    task = record.record_key if record.collection is Epoch2Collection.TASK else None
    task = task or _key_of(_find(payload, "task_ref"))
    return None if task is None else task_batches.get(task)


def _dropped_line(reason: str) -> str:
    """Return the line that stands where a line quoting a dropped commit was."""
    return f"{DROPPED_LINE}: {reason}"[:CONTENT_LINE_CHARS]


def _rebound_text(text: str, shas: Mapping[str, str | None], reasons: Mapping[str, str]) -> str:
    """Return *text* with each line quoting a dropped commit withheld and other SHAs rebound."""
    lines = text.split("\n")
    for index, line in enumerate(lines):
        gone = [sha for sha in _SHA_RE.findall(line) if sha in shas and shas[sha] is None]
        if gone:
            lines[index] = _dropped_line(reasons[gone[0]])
    return _SHA_RE.sub(lambda match: shas.get(match.group(0)) or match.group(0), "\n".join(lines))


def _rebound(
    value: Any,
    shas: Mapping[str, str | None],
    *,
    reasons: Mapping[str, str],
    trees: Mapping[str, str],
    parents: Mapping[str, str],
) -> Any:
    """Return *value* with every source SHA replaced, its binding members following.

    Raises:
        RebindDropError: A dropped commit is the whole value of a field.
    """
    if isinstance(value, str):
        return _rebound_text(value, shas, reasons)
    if isinstance(value, list):
        return [
            _rebound(item, shas, reasons=reasons, trees=trees, parents=parents) for item in value
        ]
    if not isinstance(value, dict):
        return value
    for key, item in value.items():
        if isinstance(item, str) and item in shas and shas[item] is None:
            raise RebindDropError(
                f"{key} names dropped commit {item}; map it to a published equivalent instead"
            )
    out = {
        key: _rebound(item, shas, reasons=reasons, trees=trees, parents=parents)
        for key, item in value.items()
    }
    lines, kept = value.get("lines"), out.get("lines")
    if (
        isinstance(lines, list)
        and isinstance(kept, list)
        and isinstance(out.get("withheld_lines"), int)
    ):
        out["withheld_lines"] += sum(
            1
            for old, new in zip(lines, kept, strict=True)
            if old != new and str(new).startswith(DROPPED_LINE)
        )
    head = out.get("head_sha")
    if isinstance(head, str) and head in parents and "parent_sha" in out:
        # A binding's parent is its head's parent, whichever SHAs the line named.
        out["parent_sha"] = parents[head]
    if (
        isinstance(head, str)
        and head != value.get("head_sha")
        and "tree_sha" in out
        and _EXACT_BINDING_MARK in out
    ):
        out["tree_sha"] = trees[head]
    return out


def _stale_parent(value: Any, parents: Mapping[str, str]) -> bool:
    """Return whether a binding in *value* heads at a squashed commit but names another parent."""
    if isinstance(value, list):
        return any(_stale_parent(item, parents) for item in value)
    if not isinstance(value, dict):
        return False
    head, parent = value.get("head_sha"), value.get("parent_sha")
    if (
        isinstance(head, str)
        and head in parents
        and isinstance(parent, str)
        and parent != parents[head]
    ):
        return True
    return any(_stale_parent(item, parents) for item in value.values())


def _effective_lines(path: Path) -> list[tuple[str, LedgerRecord]]:
    """Return each line no later line supersedes, with its digest, in file order."""
    lines = split_ledger_lines(read_ledger_content(path))
    records = [LedgerRecord.model_validate_json(line) for line in lines]
    superseded = {record.supersedes for record in records if record.supersedes is not None}
    return [
        (line_digest(line), record)
        for line, record in zip(lines, records, strict=True)
        if line_digest(line) not in superseded
    ]


def _ledger_rows(
    path: Path,
    squash_map: SquashMap,
    task_batches: Mapping[str, str],
    *,
    trees: Mapping[str, str],
    parents: Mapping[str, str],
    recorded_at: datetime,
) -> list[RebindRow]:
    """Plan the superseding rows of one ledger.

    Every effective line naming a source commit is superseded. When a key's
    earlier line is, and its last effective line is not, the last line is
    appended again too, so the line that stands stays the key's last.
    """
    effective = _effective_lines(path)
    rows: list[RebindRow] = []
    rebound_keys: set[str] = set()
    for digest, record in effective:
        # A squash that writes a commit again byte for byte maps it onto itself;
        # such a commit is still on the branch and needs no rebinding.
        named = {
            sha
            for sha in _SHA_RE.findall(record.model_dump_json())
            if squash_map.commits.get(sha, sha) != sha
        }
        if not named and not _stale_parent(record.payload, parents):
            continue
        batch = row_batch(record, task_batches)
        target = squash_map.batches.get(batch) if batch is not None else None
        shas = {
            sha: None if squash_map.commits[sha] is None else target or squash_map.commits[sha]
            for sha in sorted(named)
        }
        rows.append(
            RebindRow(
                record=_superseding(
                    record, digest, recorded_at, shas, squash_map.dropped, trees, parents
                ),
                batch_key=batch if target is not None and named else None,
                task_key=record.record_key if record.collection is Epoch2Collection.TASK else None,
                rebinds=shas,
            )
        )
        rebound_keys.add(record.record_key)
    last: dict[str, tuple[str, LedgerRecord]] = {}
    for digest, record in effective:
        last[record.record_key] = (digest, record)
    planned = {row.record.supersedes for row in rows}
    for key in sorted(rebound_keys):
        digest, record = last[key]
        if digest not in planned:
            rows.append(
                RebindRow(
                    record=_superseding(record, digest, recorded_at, {}, {}, trees, parents),
                    batch_key=None,
                    task_key=None,
                    rebinds={},
                )
            )
    return rows


def _superseding(
    record: LedgerRecord,
    digest: str,
    recorded_at: datetime,
    shas: Mapping[str, str | None],
    reasons: Mapping[str, str],
    trees: Mapping[str, str],
    parents: Mapping[str, str],
) -> LedgerRecord:
    """Return the row that supersedes *record*'s line, its source SHAs rebound or dropped."""
    return LedgerRecord(
        collection=record.collection,
        record_key=record.record_key,
        status=record.status,
        recorded_at=recorded_at,
        supersedes=digest,
        payload=_rebound(record.payload, shas, reasons=reasons, trees=trees, parents=parents),
    )


def plan_rebind(
    state_path: Path,
    squash_map: SquashMap,
    task_batches: Mapping[str, str],
    *,
    trees: Mapping[str, str],
    parents: Mapping[str, str],
    recorded_at: datetime,
) -> tuple[RebindRow, ...]:
    """Plan one superseding row per effective ledger line naming a source commit.

    Args:
        state_path: The generation's ``state.json``; its ledgers are read.
        squash_map: The map the squash answered.
        task_batches: Each Task key mapped to its Batch key.
        trees: Each rewritten commit mapped to its tree.
        parents: Each rewritten commit mapped to its parent.
        recorded_at: The UTC instant every row records.

    Returns:
        The rows, by ledger in collection order and in file order within one;
        none once every line already names a rewritten commit.

    Raises:
        LedgerTornTailError: A ledger ends mid-line.
        LedgerSnapshotError: A ledger's sealed snapshot does not match its digest.
        ValidationError: A ledger line is not a ledger record.
    """
    rows: list[RebindRow] = []
    for collection in Epoch2Collection:
        if tier_for(collection) is StorageTier.LEDGER:
            rows.extend(
                _ledger_rows(
                    ledger_path(state_path, collection),
                    squash_map,
                    task_batches,
                    trees=trees,
                    parents=parents,
                    recorded_at=recorded_at,
                )
            )
    return tuple(rows)


def only_commits_rebound(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    """Return whether *after* is *before* with nothing but commit SHAs rewritten.

    A rebind changes a superseded line's commit SHAs and the binding members that
    follow them, which are SHAs too; a row differing anywhere else is not a rebind.

    Args:
        before: The superseded line's payload.
        after: The superseding line's payload.

    Returns:
        ``True`` when the two payloads match once every commit SHA is masked.
    """

    def masked(payload: Mapping[str, Any]) -> str:
        return _SHA_RE.sub("", json.dumps(payload, sort_keys=True))

    return masked(before) == masked(after)


def _bound_shas(value: Any) -> Iterator[str]:
    """Yield every value in *value* that is a commit binding: a bare SHA or ``commit:<sha>``."""
    if isinstance(value, str):
        match = _BINDING_RE.match(value)
        if match is not None:
            yield match.group("sha")
    elif isinstance(value, list):
        for item in value:
            yield from _bound_shas(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _bound_shas(item)


def document_mentions(document: Mapping[str, Any], squash_map: SquashMap) -> tuple[str, ...]:
    """Return the source commits a live row of the generation document is bound to.

    A live row is rewritten in place by its own verbs, never superseded, so a
    rebind refuses a document binding a source commit rather than guessing. A
    SHA quoted inside prose, such as a research brief, binds nothing and is
    passed over.
    """
    return tuple(sorted(set(_bound_shas(document)) & set(squash_map.commits)))


__all__ = [
    "DROPPED_LINE",
    "RebindDropError",
    "RebindRow",
    "document_mentions",
    "only_commits_rebound",
    "plan_rebind",
    "row_batch",
]
