"""CI snapshot-pairing gate for managed golden-surface mutations.

Per the C09 §5.6 snapshot-update flow: an operator regenerates a golden
surface with ``eawf snapshot update --kind <surface>``, diffs the tree,
and commits the rewritten bytes as ``[P##-W##] test: snapshot update
<kind>``. This gate enforces the contract from the CI side — *every*
commit in the PR range that mutates a managed golden surface MUST carry
a wave-form ``test:`` subject so a golden change can never sneak in under
an unrelated ``feat:`` / ``fix:`` commit.

The watched directories are sourced from the same C09 §5.6 surface
inventory the CLI drives (:data:`eawf.surfaces.cli.commands.snapshot.SNAPSHOT_SURFACES`)
so the gate and ``eawf snapshot update --kind`` cannot drift. Most
surfaces live under ``tests/golden/<kind>/`` but the watch set follows
each surface's declared ``golden_dir`` verbatim, so a surface whose
bytes live elsewhere (e.g. the Textual ``tui`` surface under
``tests/snapshots/tui/golden/``) is guarded too. Golden trees *not*
in the inventory (e.g. ``tests/golden/cli/`` help-panel snapshots,
which refresh as a side-effect of any wave that adds a CLI command)
are deliberately out of scope — they have their own per-wave refresh
path and need no paired ``test:`` commit.

The gate walks the commits between the PR base and head in a **single**
``git log --name-status -z`` pass (see :func:`scan_range`): the whole
range, its subjects, and every commit's changed-file statuses come back
from one subprocess, so the gate stays fast (~0.1s) over a phase-sized
range instead of shelling ``git`` once per commit. The contract targets
*mutations* of already-committed goldens (status ``M`` / ``D`` / ``R``)
— silently rewriting golden bytes is exactly what must ride a paired
``test:`` commit. Pure *additions* (status ``A``) are exempt: a brand-new
surface ships its fixtures alongside the ``feat:`` wave that introduces
it.

The ``M`` / ``D`` / ``R`` filter is applied **in Python** over the parsed
records, never as a ``git`` ``--diff-filter``: a ``--diff-filter`` prunes
the commits with no matching file from the log output entirely, which
would starve :func:`range_spans_multiple_iters` (it needs *every* commit's
subject to tell a phase PR apart from a single-iter one). Rename (``R``)
and copy (``C``) entries arrive from ``--name-status -z`` in the three-token
``<status>\\0<old-path>\\0<new-path>`` form and are matched on the
**destination** (new) path.

For each commit that *modifies / deletes / renames* a managed golden
file, the subject must match one of:

- ``[P##-W##] test: <summary>`` (planned wave deliverable);
- ``[P##-I##-W##] test: <summary>`` (iter >= I02 variant);
- ``[P##(-I##)?-CORE] test: <summary>`` (legacy bookkeeping alias).

``W00`` / ``I00`` are rejected — wave / iter indices are 1-based. A
commit mutating golden fixtures with any other subject (wrong type,
missing wave suffix) fails the gate.

Per-commit pairing is the right contract for managed small-CL PRs. Under
the one-PR-per-phase model the whole phase ships as a single reviewed unit
and the snapshot test suite already asserts every committed golden matches
current-code output, so per-commit ``test:`` pairing is redundant. When the
PR range spans more than one iter (the phase-PR signal) the gate therefore
lists the bundled golden-touching commits for reviewer visibility and exits
``0`` instead of failing. Single-iter ranges keep the hard per-commit gate.

Invocation (GitHub Actions):

    python3 tools/snapshot_pairing_gate.py <base-sha> <head-sha>

When run outside a PR (e.g. a push to ``main`` with no base) the gate
no-ops with exit ``0`` — the pairing contract is a PR-review gate.

Exit codes:
- ``0`` — every golden-touching commit is correctly paired (or no
  golden files changed).
- ``1`` — at least one golden-touching commit is unpaired (offending
  commits printed to stderr).
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass

from eawf.surfaces.cli.commands.snapshot import SNAPSHOT_SURFACES

# Managed golden directories, sourced from the C09 §5.6 surface inventory
# so the gate's watch set and ``eawf snapshot update --kind`` share one
# source of truth. Each entry has a trailing slash so ``startswith`` only
# matches files *inside* the directory, never a sibling prefix.
_WATCHED_DIRS: tuple[str, ...] = tuple(
    sorted(f"{surface.golden_dir}/" for surface in SNAPSHOT_SURFACES.values())
)

# ``test:`` subject forms accepted by the commit-prefix grammar. Planned
# work uses a wave/CORE scope tag; out-of-phase work uses a bare conventional
# subject while no phase is active. The commit-prefix lint owns that lifecycle
# distinction, so this gate only needs to recognise both valid test forms.
# The ``(?!00)`` lookaheads reject ``I00`` / ``W00`` (1-based indices), and
# the digit-width remains ``\d{2,}`` for 3-digit ids.
_PAIRED_SUBJECT_RE = re.compile(
    r"^(?:\[P\d{2,}(-I(?!00)\d{2,})?(-W(?!00)\d{2,}|-CORE)\]\s+)?test:\s+\S.*$"
)

# Phase/iter scope key at the head of a commit subject, e.g. ``[P27-I04-W04]``
# -> ``P27-I04`` and the bare pre-I02 form ``[P27-W19]`` -> ``P27``. Used to
# tell a multi-iter phase-PR range apart from a single-iter small-CL range.
_ITER_KEY_RE = re.compile(r"^\[(P\d{2,}(?:-I\d{2,})?)")

# Sentinel token that heads each commit's ``git log`` record. It cannot
# collide with a ``--name-status`` status token (single letter + optional
# similarity score) and never reaches the boundary check as a path or
# subject, which are consumed positionally — see :func:`_parse_log`.
_RECORD_SENTINEL = "COMMIT"

# ``git log --format`` string that prints, per commit, the sentinel, the
# full SHA, and the subject, each field NUL-separated (``%x00``). The
# ``-z`` flag then NUL-terminates the header and NUL-delimits the trailing
# ``--name-status`` file entries so the whole stream parses in one pass.
_LOG_FORMAT = f"--format={_RECORD_SENTINEL}%x00%H%x00%s"

# Status codes that count as a golden *mutation* — the Python-side
# equivalent of the old ``git diff-tree --diff-filter=MDR``. ``A`` (add)
# and ``C`` (copy) are intentionally excluded; a rename shows as ``R`` and
# is matched on its destination path.
_MUTATION_CODES = frozenset({"M", "D", "R"})


@dataclass(frozen=True)
class CommitRecord:
    """A single commit parsed from the ``git log --name-status -z`` stream.

    Attributes:
        sha: The full 40-hex commit SHA.
        subject: The commit subject (``%s`` — first line only, no newline).
        changed: ``(status_code, path)`` pairs for the commit's changed
            files. ``status_code`` is the leading letter of the raw status
            (``M`` / ``A`` / ``D`` / ``R`` / ``C`` / ...); for rename and
            copy entries ``path`` is the *destination* (new) path.
    """

    sha: str
    subject: str
    changed: tuple[tuple[str, str], ...]


def _run_git(args: list[str]) -> str:
    """Return stdout of ``git <args>``; raise on failure.

    Raises:
        subprocess.CalledProcessError: If ``git`` exits non-zero.
    """
    proc = subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout


def _parse_log(raw: str) -> list[CommitRecord]:
    """Parse a ``git log --name-status -z`` stream into :class:`CommitRecord`s.

    The stream is a flat NUL-delimited token list. Each commit opens with
    the :data:`_RECORD_SENTINEL` token, followed by its SHA and subject;
    then come the ``--name-status`` file entries. A plain entry is two
    tokens (``<status>``, ``<path>``); a rename / copy entry is three
    (``<status>``, ``<old-path>``, ``<new-path>``) and is recorded against
    its destination path. The first status token of each commit carries a
    leading ``\\n`` (git's header/diff separator under ``-z``), stripped
    here; empty tokens (the trailing separator) are skipped. Paths and
    subjects are consumed positionally, so a file literally named
    ``COMMIT`` never trips the sentinel check.

    Args:
        raw: The raw stdout of the single ``git log`` pass.

    Returns:
        One record per commit, in ``git log`` order (newest first).
    """
    tokens = raw.split("\x00")
    records: list[CommitRecord] = []
    header: tuple[str, str] | None = None
    changed: list[tuple[str, str]] = []
    index = 0
    total = len(tokens)
    while index < total:
        token = tokens[index]
        if token == _RECORD_SENTINEL:
            if header is not None:
                records.append(CommitRecord(header[0], header[1], tuple(changed)))
            header = (tokens[index + 1], tokens[index + 2])
            changed = []
            index += 3
            continue
        status = token.strip()
        if not status:
            index += 1
            continue
        code = status[0]
        if code in ("R", "C"):
            # ``<status>\0<old-path>\0<new-path>`` — match the destination.
            changed.append((code, tokens[index + 2]))
            index += 3
        else:
            changed.append((code, tokens[index + 1]))
            index += 2
    if header is not None:
        records.append(CommitRecord(header[0], header[1], tuple(changed)))
    return records


def scan_range(base: str, head: str) -> list[CommitRecord]:
    """Return the parsed commits in ``base..head`` from one ``git`` subprocess.

    This is the gate's only git-invoking function on the ``main`` path: a
    single ``git log --name-status -z`` pass yields every commit's SHA,
    subject, and changed-file statuses at once, replacing the former
    per-commit ``rev-list`` + ``diff-tree`` + ``log`` fan-out.

    Args:
        base: The PR base SHA (merge-base side).
        head: The PR head SHA.

    Returns:
        One :class:`CommitRecord` per commit reachable from *head* but not
        *base*, in ``git log`` order (newest first).
    """
    raw = _run_git(["log", "--name-status", "-z", _LOG_FORMAT, f"{base}..{head}"])
    return _parse_log(raw)


def _is_managed_golden(path: str) -> bool:
    """Return whether *path* lives inside a managed snapshot surface dir."""
    return any(path.startswith(prefix) for prefix in _WATCHED_DIRS)


def commit_mutates_golden(record: CommitRecord) -> bool:
    """Return whether *record* modifies / deletes / renames a managed golden.

    "Managed" means a file under one of the C09 §5.6 surface directories
    (:data:`_WATCHED_DIRS`). Pure additions (status ``A``) and copies
    (status ``C``) are intentionally excluded: a new surface ships its
    fixtures with the ``feat:`` wave that introduces them. Only mutations
    of already-committed bytes (``M`` / ``D`` / ``R``) require a paired
    ``test:`` subject; a rename is matched on its destination path.
    """
    return any(
        code in _MUTATION_CODES and _is_managed_golden(path) for code, path in record.changed
    )


def is_paired(subject: str) -> bool:
    """Return whether *subject* satisfies a scoped or bare ``test:`` grammar."""
    return bool(_PAIRED_SUBJECT_RE.match(subject))


def iter_key(subject: str) -> str | None:
    """Return the phase-or-iter scope key (``P27-I04`` or ``P27``) from *subject*.

    Pre-I02 commits carry a bare ``[P##-W##]`` tag with no iter segment;
    those map onto the bare phase key ``P##``. Returns ``None`` when the
    subject has no recognisable scope tag.
    """
    match = _ITER_KEY_RE.match(subject)
    return match.group(1) if match else None


def range_spans_multiple_iters(records: list[CommitRecord]) -> bool:
    """Return whether *records* reference more than one distinct phase/iter scope.

    A phase PR (the one-PR-per-phase model) bundles commits from every iter
    of the phase, so its range yields multiple distinct iter keys; a managed
    small-CL PR stays within a single iter. The per-commit pairing contract
    is enforced only for the latter — phase PRs defer to wholesale diff review
    plus the snapshot test suite, which already pins golden freshness.
    """
    keys = {key for record in records if (key := iter_key(record.subject))}
    return len(keys) > 1


def _unpaired_in_records(records: list[CommitRecord]) -> list[tuple[str, str]]:
    """Return ``(short_sha, subject)`` for every unpaired golden-mutating record.

    A commit is *unpaired* when it modifies / deletes / renames a managed
    golden file but its subject does not match the wave-form ``test:``
    grammar. Pure in-memory pass over already-parsed records — no git.
    """
    offenders: list[tuple[str, str]] = []
    for record in records:
        if not commit_mutates_golden(record):
            continue
        if not is_paired(record.subject):
            offenders.append((record.sha[:9], record.subject))
    return offenders


def find_unpaired(base: str, head: str) -> list[tuple[str, str]]:
    """Return ``(short_sha, subject)`` for every unpaired golden-mutating commit.

    Convenience wrapper: scans ``base..head`` in one git pass
    (:func:`scan_range`) and applies :func:`_unpaired_in_records`.

    Args:
        base: The PR base SHA.
        head: The PR head SHA.

    Returns:
        The offending commits as ``(short_sha, subject)`` tuples; empty
        when every golden-mutating commit is correctly paired.
    """
    return _unpaired_in_records(scan_range(base, head))


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(
            "usage: snapshot_pairing_gate.py <base-sha> <head-sha>",
            file=sys.stderr,
        )
        return 1
    base, head = argv[1], argv[2]
    if not base or not head:
        # No PR context (push build) — pairing is a PR-review gate.
        print("snapshot pairing gate: no base/head — skipping (not a PR)")
        return 0

    # The single git subprocess for the whole gate run.
    records = scan_range(base, head)
    offenders = _unpaired_in_records(records)
    if not offenders:
        print("snapshot pairing gate: ok (all golden changes paired)")
        return 0

    if range_spans_multiple_iters(records):
        # Phase-PR model (one PR per phase): the range bundles commits from
        # multiple iters and ships as a single reviewed unit, and the snapshot
        # test suite already asserts every committed golden matches current-
        # code output. Per-commit ``test:``-subject pairing is a small-CL
        # review proxy that adds nothing here, so surface the bundled golden
        # commits for reviewer visibility without blocking the merge.
        print(
            "snapshot pairing gate: phase PR detected (range spans multiple iters); "
            "golden changes are reviewed wholesale and pinned by the snapshot test "
            "suite, so per-commit pairing is not enforced. Bundled golden-touching "
            "commits:"
        )
        for short_sha, subject in offenders:
            print(f"  {short_sha} {subject!r}")
        return 0

    print(
        "snapshot pairing gate: unpaired golden-surface mutation(s) detected.\n"
        "Every commit that touches a managed golden fixture must carry a valid\n"
        "'test:' subject, e.g. '[P27-W19] test: snapshot update <kind>' or\n"
        "'test: snapshot update <kind>' while no phase is active\n"
        "(regenerate with `eawf snapshot update --kind <kind>` first).\n"
        "Offending commits:",
        file=sys.stderr,
    )
    for short_sha, subject in offenders:
        print(f"  {short_sha} {subject!r}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
