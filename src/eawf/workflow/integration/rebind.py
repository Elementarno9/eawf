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

A history rewrite in place -- author fields rewritten, every tree kept -- gives
every commit a new hash. Its :class:`RewriteMap`, read from a git filter-repo
commit map, is planned the same way, commit by commit: it names no Batch commit
to rebind a row through.

Planning reads and never writes; the daemon verb applying the plan verifies
the rewritten commits first.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError, field_validator

from eawf.kernel.runtime.content import CONTENT_LINE_CHARS
from eawf.kernel.state.models import ShaStr
from eawf.kernel.store.commit_policy import CommitPolicy, ledger_row_policy
from eawf.kernel.store.ledger import (
    LedgerRecord,
    line_digest,
    read_ledger_content,
    split_ledger_lines,
)
from eawf.kernel.store.paths import LEDGER_DIRNAME, LOCAL_DIRNAME, seed_ledger_path
from eawf.kernel.store.tiers import (
    STATUS_PROJECTION_COLLECTIONS,
    Epoch2Collection,
    StorageTier,
    tier_for,
)
from eawf.workflow.integration.squash import SquashMap

#: A full commit SHA, delimited so a 64-hex digest never yields a match.
_SHA_RE: Final = re.compile(r"(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])")

#: A field value that binds a commit: the bare SHA, or a ``commit:`` reference to it.
_BINDING_RE: Final = re.compile(r"^(?:commit:)?(?P<sha>[0-9a-f]{40})$")

#: What a line quoting a dropped commit says in its place, before the drop's reason.
DROPPED_LINE: Final = "this line named a commit no published branch carries and is withheld"

#: The member every exact revision binding carries beside the head it binds,
#: a Task's integrated binding included, which may omit its digests.
_EXACT_BINDING_MARK: Final = "policy_revision"

#: Refusal: a commit-map file is not a git filter-repo commit map.
REWRITE_MAP_INVALID: Final = "rewrite_map_invalid"

#: What git filter-repo writes for a commit it pruned; no commit has this hash.
_ZERO_SHA: Final = "0" * 40

_FULL_SHA_RE: Final = re.compile(r"[0-9a-f]{40}")


class RewriteMapError(ValueError):
    """A commit-map file is not a git filter-repo commit map.

    Attributes:
        line: The 1-based line the file goes wrong on.
    """

    def __init__(self, line: int, reason: str) -> None:
        """Name the line and why it is refused.

        Args:
            line: The 1-based line the file goes wrong on.
            reason: What is wrong with it.
        """
        super().__init__(f"{REWRITE_MAP_INVALID}: line {line}: {reason}")
        self.line = line


class RewriteMap(BaseModel):
    """Which commit each commit became when a history was rewritten in place.

    Several old commits may name one new commit, where the rewrite made them
    identical, and a commit the rewrite left alone names itself.

    Attributes:
        commits: Each old full commit hash mapped to its new full commit hash.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    commits: dict[ShaStr, ShaStr]

    @field_validator("commits")
    @classmethod
    def _no_pruned_commit(cls, commits: dict[str, str]) -> dict[str, str]:
        """Refuse an old commit mapped to the zero hash.

        Args:
            commits: Each old commit mapped to its new commit.

        Returns:
            *commits* unchanged.

        Raises:
            ValueError: A commit maps to the zero hash, so no commit stands for it.
        """
        pruned = sorted(old for old, new in commits.items() if new == _ZERO_SHA)
        if pruned:
            raise ValueError(f"{pruned[0]} maps to the zero hash; no commit stands for it")
        return commits


def load_rewrite_map(path: Path) -> RewriteMap:
    """Read a git filter-repo commit map.

    Args:
        path: The file: a header line ``old`` and ``new`` separated by
            whitespace, then one ``<old> <new>`` pair of full lowercase
            commit hashes per line.

    Returns:
        The map.

    Raises:
        OSError: The file cannot be read.
        RewriteMapError: The header is missing, a line is not two full hashes,
            an old hash repeats, or a new hash is the zero hash.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].split() != ["old", "new"]:
        raise RewriteMapError(1, "the header is not 'old new'")
    commits: dict[str, str] = {}
    for number, line in enumerate(lines[1:], start=2):
        pair = line.split()
        if len(pair) != 2 or not all(_FULL_SHA_RE.fullmatch(sha) for sha in pair):
            raise RewriteMapError(number, "the line is not two full lowercase commit hashes")
        old, new = pair
        if old in commits:
            raise RewriteMapError(number, f"old commit {old} repeats an earlier line")
        if new == _ZERO_SHA:
            raise RewriteMapError(number, f"{old} maps to the zero hash of a pruned commit")
        commits[old] = new
    return RewriteMap(commits=commits)


class RewritePair(BaseModel):
    """One line of the committed record of applied rewrite pairs.

    Attributes:
        old: The commit the rewrite replaced.
        new: The commit that replaced it.
        applied_at: When the rebind that applied the pair ran.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    old: ShaStr
    new: ShaStr
    applied_at: AwareDatetime


def rewrite_map_path(tree_root: Path) -> Path:
    """Return the committed record of applied rewrite pairs of the ``.ea`` tree *tree_root*."""
    return tree_root / "store" / "rewrite_map.jsonl"


def parse_rewrite_pairs(content: bytes) -> tuple[RewritePair, ...]:
    """Return every pair the record's bytes *content* hold, in file order.

    Args:
        content: The record's bytes; empty bytes hold no pair.

    Returns:
        The pairs.

    Raises:
        RewriteMapError: A line is not a rewrite pair: torn, not JSON, not
            UTF-8, or missing, adding or mistyping a field.
    """
    pairs: list[RewritePair] = []
    for number, line in enumerate(content.splitlines(), start=1):
        try:
            pairs.append(RewritePair.model_validate_json(line))
        except ValidationError as exc:
            raise RewriteMapError(
                number, f"the line is not a rewrite pair ({exc.error_count()} error(s))"
            ) from exc
    return tuple(pairs)


def read_rewrite_pairs(path: Path) -> tuple[RewritePair, ...]:
    """Return every pair the record at *path* holds, in file order.

    Args:
        path: The record; a missing file reads as empty.

    Returns:
        The pairs.

    Raises:
        OSError: The record exists but cannot be read.
        RewriteMapError: A line is not a rewrite pair.
    """
    if not path.is_file():
        return ()
    return parse_rewrite_pairs(path.read_bytes())


def resolve_rewrite_pairs(pairs: Sequence[RewritePair]) -> dict[str, str]:
    """Return each old commit of *pairs* mapped to the commit it finally became.

    A commit rewritten twice is recorded twice, the second pair's old
    commit being the first pair's new one, so each chain is followed to its
    end. Where one old commit is recorded with two new commits, the later
    pair wins, as the later rebind applied it.

    Args:
        pairs: The pairs in record order.

    Returns:
        The resolved map; empty for no pairs.

    Raises:
        RewriteMapError: The pairs form a cycle, so a commit has no end;
            the line named is the 1-based position of the last pair whose
            old commit is on the cycle.
    """
    direct = {pair.old: pair.new for pair in pairs}
    line_of = {pair.old: number for number, pair in enumerate(pairs, start=1)}
    resolved: dict[str, str] = {}
    for old in direct:
        chain, current = [old], direct[old]
        while current in direct and current not in resolved:
            if current in chain:
                raise RewriteMapError(
                    line_of[current], f"commit {current} is rewritten back into itself"
                )
            chain.append(current)
            current = direct[current]
        end = resolved.get(current, current)
        resolved.update(dict.fromkeys(chain, end))
    return resolved


def rewritten_commits(tree_root: Path) -> dict[str, str]:
    """Return each recorded old commit mapped to the commit it finally became.

    Args:
        tree_root: The repository's ``.ea`` directory.

    Returns:
        The map :func:`resolve_rewrite_pairs` resolves from the record;
        empty when no record exists.

    Raises:
        OSError: The record exists but cannot be read.
        RewriteMapError: A line is not a rewrite pair, or the pairs form a cycle.
    """
    return resolve_rewrite_pairs(read_rewrite_pairs(rewrite_map_path(tree_root)))


def resolve_rewritten(tree_root: Path, sha: str) -> str:
    """Return the commit *sha* finally became, or *sha* when no pair records it.

    Args:
        tree_root: The repository's ``.ea`` directory.
        sha: A full commit hash.

    Returns:
        The end of *sha*'s chain of recorded pairs, or *sha* unchanged.

    Raises:
        OSError: The record exists but cannot be read.
        RewriteMapError: A line is not a rewrite pair, or the pairs form a cycle.
    """
    return rewritten_commits(tree_root).get(sha, sha)


def pairs_to_record(
    rewrite_map: RewriteMap, recorded: Iterable[RewritePair], *, at: datetime
) -> tuple[RewritePair, ...]:
    """Return the pairs of *rewrite_map* the record does not hold yet.

    A commit mapped to itself was not rewritten, so it is no pair to record.

    Args:
        rewrite_map: The map a rebind applied.
        recorded: The pairs already recorded.
        at: The instant each new pair records.

    Returns:
        The new pairs, ordered by old commit.
    """
    held = {(pair.old, pair.new) for pair in recorded}
    return tuple(
        RewritePair(old=old, new=new, applied_at=at)
        for old, new in sorted(rewrite_map.commits.items())
        if old != new and (old, new) not in held
    )


def append_rewrite_pairs(path: Path, pairs: Iterable[RewritePair]) -> None:
    """Append one JSON line per pair to the record at *path* and flush it to disk.

    Args:
        path: The record; it and its directory are created when missing.
        pairs: The pairs to append.

    Raises:
        OSError: The record cannot be written.
    """
    text = "".join(f"{pair.model_dump_json()}\n" for pair in pairs)
    if not text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


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
    commits: Mapping[str, str | None],
    batches: Mapping[str, str],
    dropped: Mapping[str, str],
    task_batches: Mapping[str, str],
    *,
    trees: Mapping[str, str],
    parents: Mapping[str, str],
    recorded_at: datetime,
    keep_exhaust: bool,
) -> list[RebindRow]:
    """Plan the superseding rows of one ledger.

    With *keep_exhaust*, a Run exhaust line the commit policy keeps local is
    left as it is.

    Every effective line naming a source commit is superseded. When a key's
    earlier line is, and its last effective line is not, the last line is
    appended again too, so the line that stands stays the key's last.
    """
    effective = _effective_lines(path)
    rows: list[RebindRow] = []
    rebound_keys: set[str] = set()
    for digest, record in effective:
        if (
            keep_exhaust
            and ledger_row_policy(record.collection, record.payload) is CommitPolicy.NOT_COMMITTED
        ):
            # Run exhaust, such as a content-addressed capture of tool output,
            # keeps a rewritten hash: its key is the digest of its text, and a
            # line superseding it would land in the local ledger, not beside it.
            continue
        # A squash that writes a commit again byte for byte maps it onto itself;
        # such a commit is still on the branch and needs no rebinding.
        named = {
            sha for sha in _SHA_RE.findall(record.model_dump_json()) if commits.get(sha, sha) != sha
        }
        if not named and not _stale_parent(record.payload, parents):
            continue
        batch = row_batch(record, task_batches)
        target = batches.get(batch) if batch is not None else None
        shas = {
            sha: None if commits[sha] is None else target or commits[sha] for sha in sorted(named)
        }
        rows.append(
            RebindRow(
                record=_superseding(record, digest, recorded_at, shas, dropped, trees, parents),
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
    commit_map: SquashMap | RewriteMap,
    task_batches: Mapping[str, str],
    *,
    trees: Mapping[str, str],
    parents: Mapping[str, str],
    recorded_at: datetime,
) -> tuple[RebindRow, ...]:
    """Plan one superseding row per effective ledger line naming a source commit.

    Args:
        state_path: The generation's ``state.json``; its ledgers are read.
        commit_map: The map the squash answered, or a history-rewrite map,
            which names no Batch commit and drops no commit, so its rows are
            rebound commit by commit.
            For a history-rewrite map, a Run exhaust line the commit policy
            keeps local, such as a content-addressed capture of tool output,
            is left as it is; a squash still withholds a dropped commit there.
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
    batches, dropped = (
        (commit_map.batches, commit_map.dropped) if isinstance(commit_map, SquashMap) else ({}, {})
    )
    rows: list[RebindRow] = []
    for path in _ledger_paths(state_path):
        rows.extend(
            _ledger_rows(
                path,
                commit_map.commits,
                batches,
                dropped,
                task_batches,
                trees=trees,
                parents=parents,
                recorded_at=recorded_at,
                keep_exhaust=isinstance(commit_map, RewriteMap),
            )
        )
    return tuple(rows)


def _ledger_paths(state_path: Path) -> Iterator[Path]:
    """Yield the ledger of every ledger-tier collection, in collection order.

    Each is the file whose lines are in effect even before
    ``settle_status_ledgers`` has run, so a plan that must not write reads
    what a settled tree would: an unseeded local status ledger reads as its
    committed seed, and a Task ledger stranded under ``local/`` stands in
    for the committed one it extends.

    Args:
        state_path: The generation's ``state.json``.

    Yields:
        One ledger path per ledger-tier collection.
    """
    held = state_path.parent / LOCAL_DIRNAME / LEDGER_DIRNAME
    for collection in Epoch2Collection:
        if tier_for(collection) is not StorageTier.LEDGER:
            continue
        seed = seed_ledger_path(state_path, collection)
        settles = collection in STATUS_PROJECTION_COLLECTIONS or collection is Epoch2Collection.TASK
        local = held / seed.name
        yield local if settles and local.is_file() else seed


def recorded_hashes(state_path: Path) -> frozenset[str]:
    """Return every full hash an effective line of the generation's ledgers names.

    Args:
        state_path: The generation's ``state.json``; its ledgers are read.

    Returns:
        The distinct 40-hex strings, commits and trees alike.

    Raises:
        LedgerTornTailError: A ledger ends mid-line.
        LedgerSnapshotError: A ledger's sealed snapshot does not match its digest.
        ValidationError: A ledger line is not a ledger record.
    """
    return frozenset(
        sha
        for path in _ledger_paths(state_path)
        for _, record in _effective_lines(path)
        for sha in _SHA_RE.findall(record.model_dump_json())
    )


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
    "REWRITE_MAP_INVALID",
    "RebindDropError",
    "RebindRow",
    "RewriteMap",
    "RewriteMapError",
    "RewritePair",
    "append_rewrite_pairs",
    "document_mentions",
    "load_rewrite_map",
    "only_commits_rebound",
    "pairs_to_record",
    "parse_rewrite_pairs",
    "plan_rebind",
    "read_rewrite_pairs",
    "recorded_hashes",
    "resolve_rewrite_pairs",
    "resolve_rewritten",
    "rewrite_map_path",
    "rewritten_commits",
    "row_batch",
]
