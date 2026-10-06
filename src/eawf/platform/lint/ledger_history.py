"""The ledger history gates: committed ledger lines stay put, and the SHAs they bind stay live.

Two defects have reached this repository's committed ledgers. A history rewrite
remapped commit SHAs inside committed ledger lines in place, so bytes a reader had
already relied on changed under it. And rows kept naming commits that no longer
descend from the default branch -- squashed away, amended by a lint, or never pushed
-- so the binding they record points at nothing a fresh clone can reach.

Two checks hold those lines, over every committed ledger file (the LEDGER tier of
:data:`~eawf.kernel.store.commit_policy.EA_PATH_CLASSES`):

- **append-only** -- for each ledger file the range changes, the bytes at the range's
  base must be a byte prefix of the bytes at its tip (the rule
  :func:`~eawf.kernel.store.ledger.verify_append_only` enforces on a live write).
  A finding names the file and the byte offset the two diverge at.
- **reachability** -- every 40-hex string in an effective row (one no later row
  supersedes) that names a commit must be an ancestor of the range's base or tip.
  A row may declare a SHA it knowingly keeps in ``unreachable_shas``, each entry
  naming the archive ref that preserves the commit and the reason; that row's
  mention of the SHA is exempt.

The tip counts as live alongside the base because the branch lands by fast-forward,
so an ancestor of the tip becomes an ancestor of the default branch on landing.

The pure checks take bytes; :func:`scan_ledger_history` collects those bytes from git.
The production call-site is ``eawf hook ledger-history``.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Annotated, Any, Final

import orjson
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from eawf.kernel.store.commit_policy import (
    CENSUS_SURFACE_PREFIX,
    CommitPolicy,
    UndeclaredPathError,
    classify_path,
)
from eawf.kernel.store.ledger import line_digest, split_ledger_lines
from eawf.kernel.store.tiers import StorageTier
from eawf.platform.subprocess_detach import no_window_kwargs

#: A full commit SHA, delimited so a 64-hex digest never yields a 40-hex match.
SHA_RE: Final = re.compile(r"(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])")

#: The row key under which a row declares the SHAs it knowingly keeps unreachable.
UNREACHABLE_KEY: Final = "unreachable_shas"

#: Where every generation's ledgers live; the tier check below decides which count.
_GENERATIONS_DIR: Final = ".ea/generations"


class LedgerHistoryError(ValueError):
    """The gate cannot run: a malformed range or a git command that failed."""


class UnreachableSha(BaseModel):
    """One SHA a row declares it keeps although no live branch reaches it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sha: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    archive_ref: Annotated[str, Field(min_length=1)]
    reason: Annotated[str, Field(min_length=1)]


_UNREACHABLE_LIST: Final = TypeAdapter(list[UnreachableSha])


class AppendOnlyFinding(BaseModel):
    """A committed ledger file whose tip does not extend its base byte for byte."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    offset: int
    detail: str

    def render(self) -> str:
        """Return the one-line report of this finding."""
        return f"  {self.path}:byte {self.offset}: {self.detail}"


class ShaMention(BaseModel):
    """One 40-hex string an effective ledger row carries without exempting it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    line: int
    record_key: str | None
    sha: str


class UnreachableFinding(BaseModel):
    """A row binding a commit that neither end of the range reaches."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    line: int
    record_key: str | None
    sha: str

    def render(self) -> str:
        """Return the one-line report of this finding."""
        key = f" {self.record_key}" if self.record_key else ""
        return f"  {self.path}:{self.line}{key}: {self.sha} is unreachable"


class MalformedDeclaration(BaseModel):
    """A row whose ``unreachable_shas`` declaration does not hold its shape."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    line: int
    detail: str

    def render(self) -> str:
        """Return the one-line report of this finding."""
        return f"  {self.path}:{self.line}: malformed {UNREACHABLE_KEY}: {self.detail}"


class LedgerHistoryReport(BaseModel):
    """Everything one run of the gate found over one range."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    range: str
    base: str
    tip: str
    ledgers_scanned: int
    append_only: tuple[AppendOnlyFinding, ...]
    unreachable: tuple[UnreachableFinding, ...]
    malformed: tuple[MalformedDeclaration, ...]

    @property
    def clean(self) -> bool:
        """Whether neither check found anything."""
        return not (self.append_only or self.unreachable or self.malformed)


def is_ledger_path(path: str) -> bool:
    """Whether *path* is a committed append-only ledger file.

    Args:
        path: A repo-relative path, as git names it.

    Returns:
        True for a path the commit policy files in the committed LEDGER tier.
    """
    if not path.startswith(CENSUS_SURFACE_PREFIX):
        return False
    try:
        row = classify_path(path)
    except UndeclaredPathError:
        # A range can delete or carry epoch-1 paths no row declares any more;
        # they are no ledger of this tree.
        return False
    return row.tier is StorageTier.LEDGER and row.policy is CommitPolicy.COMMITTED


def append_only_finding(path: str, before: bytes, after: bytes) -> AppendOnlyFinding | None:
    """Check that *after* extends *before* byte for byte.

    Args:
        path: The ledger file the two contents belong to, for the report.
        before: The file at the range's base.
        after: The file at the range's tip; empty for a deleted file.

    Returns:
        A finding at the first byte the contents diverge at, or ``None``
        when *after* is an extension of *before*.
    """
    if after.startswith(before):
        return None
    offset = len(os.path.commonprefix([before, after]))
    if offset == len(after):
        detail = f"drops {len(before) - len(after)} committed bytes"
    else:
        detail = "rewrites a committed line"
    return AppendOnlyFinding(path=path, offset=offset, detail=detail)


def _declared(value: Any) -> Iterator[Any]:
    """Yield every ``unreachable_shas`` value nested anywhere in *value*."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key == UNREACHABLE_KEY:
                yield item
            else:
                yield from _declared(item)
    elif isinstance(value, list):
        for item in value:
            yield from _declared(item)


def _record_key(row: Any) -> str | None:
    """Return the row's ``record_key`` (ledger) or ``change_id`` (history), if any."""
    if not isinstance(row, dict):
        return None
    for field in ("record_key", "change_id"):
        value = row.get(field)
        if isinstance(value, str):
            return value
    return None


def sha_mentions(
    path: str, content: bytes
) -> tuple[tuple[ShaMention, ...], tuple[MalformedDeclaration, ...]]:
    """Collect the 40-hex strings the effective rows of one ledger file carry.

    A row is effective when no line of the same file names its digest in
    ``supersedes``. A SHA the row itself declares in ``unreachable_shas`` is
    left out; a declaration that does not hold its shape exempts nothing.

    Args:
        path: The ledger file, for the report.
        content: The whole file.

    Returns:
        The non-exempt mentions, one per distinct SHA per row, and the rows
        whose declaration is malformed.

    Raises:
        LedgerTornTailError: The content ends mid-line.
        orjson.JSONDecodeError: A line is not JSON.
    """
    lines = split_ledger_lines(content)
    rows = [orjson.loads(line) for line in lines]
    superseded = {row.get("supersedes") for row in rows if isinstance(row, dict)}
    mentions: list[ShaMention] = []
    malformed: list[MalformedDeclaration] = []
    for number, (line, row) in enumerate(zip(lines, rows, strict=True), start=1):
        if line_digest(line) in superseded:
            continue
        exempt: set[str] = set()
        for declaration in _declared(row):
            try:
                exempt.update(entry.sha for entry in _UNREACHABLE_LIST.validate_python(declaration))
            except ValidationError as exc:
                malformed.append(
                    MalformedDeclaration(
                        path=path, line=number, detail=f"{exc.error_count()} invalid field(s)"
                    )
                )
        key = _record_key(row)
        for sha in sorted(set(SHA_RE.findall(line)) - exempt):
            mentions.append(ShaMention(path=path, line=number, record_key=key, sha=sha))
    return tuple(mentions), tuple(malformed)


def unreachable_findings(
    mentions: Iterable[ShaMention],
    *,
    commits: frozenset[str],
    reachable: frozenset[str],
) -> tuple[UnreachableFinding, ...]:
    """Report each mention that names a commit no live branch reaches.

    Args:
        mentions: The candidate mentions.
        commits: Which of the mentioned strings name a commit in the repository;
            a 40-hex string naming nothing (a digest prefix, a tree) is no binding.
        reachable: Every commit an end of the range reaches.

    Returns:
        One finding per mention of a commit outside *reachable*.
    """
    return tuple(
        UnreachableFinding(path=m.path, line=m.line, record_key=m.record_key, sha=m.sha)
        for m in mentions
        if m.sha in commits and m.sha not in reachable
    )


def parse_range(spec: str) -> tuple[str, str | None]:
    """Split a range spec into its base and tip.

    Args:
        spec: ``<base>..<tip>``, or a single ``<base>`` to compare against the
            working tree.

    Returns:
        ``(base, tip)``; *tip* is ``None`` for the working tree.

    Raises:
        LedgerHistoryError: An end of the range is empty, or the spec uses
            the three-dot form.
    """
    if "..." in spec:
        raise LedgerHistoryError(f"range {spec!r}: use <base>..<tip>, not the three-dot form")
    base, sep, tip = spec.partition("..")
    if not base or (sep and not tip):
        raise LedgerHistoryError(f"range {spec!r}: expected <base>..<tip> or <base>")
    return base, tip if sep else None


def _git(repo: Path, *args: str, stdin: bytes | None = None) -> bytes:
    """Run one git command in *repo* and return its stdout.

    Raises:
        LedgerHistoryError: git exited non-zero.
    """
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        input=stdin,
        capture_output=True,
        check=False,
        **no_window_kwargs(),
    )
    if proc.returncode != 0:
        message = proc.stderr.decode("utf-8", "replace").strip()
        raise LedgerHistoryError(f"git {' '.join(args)} failed: {message}")
    return proc.stdout


def _blob(repo: Path, rev: str | None, path: str) -> bytes:
    """Return *path* at *rev* (the working tree when ``None``); empty when absent."""
    if rev is None:
        target = repo / path
        return target.read_bytes() if target.is_file() else b""
    proc = subprocess.run(
        ["git", "cat-file", "blob", f"{rev}:{path}"],
        cwd=repo,
        capture_output=True,
        check=False,
        **no_window_kwargs(),
    )
    return proc.stdout if proc.returncode == 0 else b""


def _ledger_paths_at(repo: Path, tip: str | None) -> list[str]:
    """Return every tracked ledger file at *tip* (the index when ``None``)."""
    if tip is None:
        raw = _git(repo, "ls-files", "-z", "--", _GENERATIONS_DIR)
    else:
        raw = _git(repo, "ls-tree", "-r", "-z", "--name-only", tip, "--", _GENERATIONS_DIR)
    return sorted(p for p in raw.decode("utf-8").split("\0") if p and is_ledger_path(p))


def _changed_ledger_paths(repo: Path, old: str, tip: str | None) -> list[str]:
    """Return the ledger files whose bytes differ between *old* and *tip*."""
    ends = [old] if tip is None else [old, tip]
    raw = _git(repo, "diff", "--name-only", "-z", "--no-renames", *ends, "--", _GENERATIONS_DIR)
    return sorted(p for p in raw.decode("utf-8").split("\0") if p and is_ledger_path(p))


def _resolve_commits(repo: Path, shas: Iterable[str]) -> frozenset[str]:
    """Return the subset of *shas* that name a commit in *repo*."""
    ordered = sorted(set(shas))
    if not ordered:
        return frozenset()
    query = "".join(f"{sha}^{{commit}}\n" for sha in ordered).encode("ascii")
    out = _git(repo, "cat-file", "--batch-check=%(objectname) %(objecttype)", stdin=query)
    # One output line per query line, in order; a missing object prints
    # "<query> missing" instead of the oid and type.
    found: set[str] = set()
    for sha, row in zip(ordered, out.decode("utf-8").splitlines(), strict=True):
        if row.endswith(" commit"):
            found.add(sha)
    return frozenset(found)


def scan_ledger_history(repo: Path, spec: str) -> LedgerHistoryReport:
    """Run both checks over the committed ledgers for the range *spec*.

    The append-only check compares each changed ledger file at the merge
    base of the range with the file at its tip, so a base that moved on
    after the branch forked does not read as a rewrite. The reachability
    check reads every ledger file at the tip.

    Args:
        repo: The repository's working directory.
        spec: ``<base>..<tip>``, or ``<base>`` for the working tree.

    Returns:
        The findings of both checks.

    Raises:
        LedgerHistoryError: The range is malformed or names an unknown rev.
    """
    base, tip = parse_range(spec)
    base_sha = _git(repo, "rev-parse", "--verify", f"{base}^{{commit}}").decode().strip()
    tip_rev = tip if tip is not None else "HEAD"
    tip_sha = _git(repo, "rev-parse", "--verify", f"{tip_rev}^{{commit}}").decode().strip()
    old = _git(repo, "merge-base", base_sha, tip_sha).decode().strip()
    tip_ref = tip_sha if tip is not None else None

    checked = (
        append_only_finding(path, _blob(repo, old, path), _blob(repo, tip_ref, path))
        for path in _changed_ledger_paths(repo, old, tip_ref)
    )
    append_only = tuple(finding for finding in checked if finding is not None)

    mentions: list[ShaMention] = []
    malformed: list[MalformedDeclaration] = []
    ledgers = _ledger_paths_at(repo, tip_ref)
    for path in ledgers:
        found, bad = sha_mentions(path, _blob(repo, tip_ref, path))
        mentions.extend(found)
        malformed.extend(bad)
    commits = _resolve_commits(repo, (m.sha for m in mentions))
    reachable = frozenset(_git(repo, "rev-list", base_sha, tip_sha).decode("ascii").split())
    return LedgerHistoryReport(
        range=spec,
        base=base_sha,
        tip=tip_sha if tip is not None else "WORKTREE",
        ledgers_scanned=len(ledgers),
        append_only=append_only,
        unreachable=unreachable_findings(mentions, commits=commits, reachable=reachable),
        malformed=tuple(malformed),
    )


__all__ = [
    "SHA_RE",
    "UNREACHABLE_KEY",
    "AppendOnlyFinding",
    "LedgerHistoryError",
    "LedgerHistoryReport",
    "MalformedDeclaration",
    "ShaMention",
    "UnreachableFinding",
    "UnreachableSha",
    "append_only_finding",
    "is_ledger_path",
    "parse_range",
    "scan_ledger_history",
    "sha_mentions",
    "unreachable_findings",
]
