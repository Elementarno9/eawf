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
would starve :func:`is_phase_pr` (it needs *every* commit's scope, not
just the golden-touching ones, to tell a phase PR from ordinary work).
Rename (``R``)
and copy (``C``) entries arrive from ``--name-status -z`` in the three-token
``<status>\\0<old-path>\\0<new-path>`` form and are matched on the
**destination** (new) path.

For each commit that *modifies / deletes / renames* a managed golden
file, the subject must match one of:

- ``[P##-W##] test: <summary>`` (planned wave deliverable);
- ``[P##-I##-W##] test: <summary>`` (iter >= I02 variant);
- ``test: <summary>`` (bare conventional, out-of-phase only).

``W00`` / ``I00`` are rejected — wave / iter indices are 1-based. A
commit mutating golden fixtures with any other subject (wrong type,
missing wave suffix) fails the gate.

Per-commit pairing is the right contract for managed small-CL PRs. Under
the one-PR-per-phase model the whole phase ships as a single reviewed unit
and the snapshot test suite already asserts every committed golden matches
current-code output, so per-commit ``test:`` pairing is redundant. The gate
therefore reads the phase membership the commits themselves declare (the
``[P##...]`` subject prefix, or the ``Eawf-Wave: P##-I##-W##`` trailer the
trailer-style convention emits) and corroborates it against the repo's own
``.ea/state.json``: when every phase the range names is one this repo has
actually opened, the range is that phase's PR, so the gate lists the bundled
golden-touching commits for reviewer visibility and exits ``0`` instead of
failing. The determination is independent of how many iters the range
touches -- a phase that ships in a single iter is still a phase PR.

Ordinary work keeps the hard per-commit gate, and cannot reach the bundled
path by accident: a range that declares no phase at all (out-of-phase work,
a hotfix branch off the default branch, an unmanaged fork) names nothing to
corroborate, and a fabricated scope tag buys nothing because the phase it
names must exist in ``state.json`` with a status that says the phase was
opened -- a merely PLANNED phase has no commits yet, so a commit claiming
one is not a phase PR.

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

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from eawf.surfaces.cli.commands.snapshot import SNAPSHOT_SURFACES

# Managed golden directories, sourced from the C09 §5.6 surface inventory
# so the gate's watch set and ``eawf snapshot update --kind`` share one
# source of truth. Each entry has a trailing slash so ``startswith`` only
# matches files *inside* the directory, never a sibling prefix.
_WATCHED_DIRS: tuple[str, ...] = tuple(
    sorted(f"{surface.golden_dir}/" for surface in SNAPSHOT_SURFACES.values())
)

# ``test:`` subject forms accepted by the commit-prefix grammar. Planned
# work uses a wave scope tag; out-of-phase work uses a bare conventional
# subject while no phase is active. The commit-prefix lint owns that lifecycle
# distinction, so this gate only needs to recognise both valid test forms.
# The retired ``-CORE`` alias is not accepted: this gate only ever scans an
# unmerged base..head range, so no already-landed commit is replayed through it.
# The ``(?!00)`` lookaheads reject ``I00`` / ``W00`` (1-based indices), and
# the digit-width remains ``\d{2,}`` for 3-digit ids.
_PAIRED_SUBJECT_RE = re.compile(r"^(?:\[P\d{2,}(-I(?!00)\d{2,})?-W(?!00)\d{2,}\]\s+)?test:\s+\S.*$")

# Phase id carried by a bracket-style subject prefix: ``[P27-I04-W04]``,
# ``[P27-W19]``, ``[P27-I04]`` and the bare bookkeeping form ``[P27]`` all
# name phase ``P27``. Trailing scope segments are matched loosely because
# only the phase segment decides membership.
_SUBJECT_PHASE_RE = re.compile(r"^\[(P\d{2,})[^\]]*\]")

# Phase id carried by the trailer-style convention's ``Eawf-Wave`` line, the
# only phase carrier a bare ``<type>: <summary>`` subject has. Matched over
# the raw body rather than via ``git``'s ``%(trailers)`` because that
# interpolation only sees the message's LAST paragraph, and the co-author
# trailer routinely lands in a paragraph of its own below this one.
_WAVE_TRAILER_RE = re.compile(
    r"^Eawf-Wave:[ \t]*(P\d{2,})-I\d{2,}-W\d{2,}[ \t]*$",
    re.MULTILINE,
)

# Phase statuses that mean "this phase has been opened and can own commits".
# PLANNED is excluded on purpose: a planned phase has no commits, so a range
# whose commits claim one is mislabelled work, not a phase PR.
_OPENED_PHASE_STATUSES = frozenset({"active", "closed", "archived"})

# The committed state document, relative to a project root. Read-only here:
# the gate corroborates commit-declared scope, it never mutates state.
_STATE_RELPATH = Path(".ea") / "state.json"

# Sentinel token that heads each commit's ``git log`` record. It cannot
# collide with a ``--name-status`` status token (single letter + optional
# similarity score) and never reaches the boundary check as a path or
# subject, which are consumed positionally — see :func:`_parse_log`.
_RECORD_SENTINEL = "COMMIT"

# ``git log --format`` string that prints, per commit, the sentinel, the
# full SHA, the subject, and the raw body, each field NUL-separated
# (``%x00``). The ``-z`` flag then NUL-terminates the header and
# NUL-delimits the trailing ``--name-status`` file entries so the whole
# stream parses in one pass. The body rides along because it carries the
# ``Eawf-Wave`` trailer, which is the phase carrier for trailer-style
# commits; a body cannot contain a NUL, so it stays one token.
_LOG_FORMAT = f"--format={_RECORD_SENTINEL}%x00%H%x00%s%x00%b"

# Header field count per commit record: sentinel, SHA, subject, body.
_HEADER_FIELDS = 4

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
        body: The raw commit body (``%b`` — everything below the subject),
            which carries the ``Eawf-Wave`` trailer when the repo is on the
            trailer-style subject convention.
        changed: ``(status_code, path)`` pairs for the commit's changed
            files. ``status_code`` is the leading letter of the raw status
            (``M`` / ``A`` / ``D`` / ``R`` / ``C`` / ...); for rename and
            copy entries ``path`` is the *destination* (new) path.
    """

    sha: str
    subject: str
    body: str
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
    the :data:`_RECORD_SENTINEL` token, followed by its SHA, subject and
    raw body; then come the ``--name-status`` file entries. A plain entry is two
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
    header: tuple[str, str, str] | None = None
    changed: list[tuple[str, str]] = []
    index = 0
    total = len(tokens)
    while index < total:
        token = tokens[index]
        if token == _RECORD_SENTINEL:
            if header is not None:
                records.append(CommitRecord(header[0], header[1], header[2], tuple(changed)))
            header = (tokens[index + 1], tokens[index + 2], tokens[index + 3])
            changed = []
            index += _HEADER_FIELDS
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
        records.append(CommitRecord(header[0], header[1], header[2], tuple(changed)))
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
    """Return whether *path* is golden bytes inside a managed surface dir.

    Python under a watched directory is the suite that produces the goldens,
    not a golden itself — ``tests/golden/scenarios/`` holds its conftest and
    test module beside its fixtures. Requiring a ``test:`` subject to edit
    that code forces a fix to the generator to masquerade as a refresh.
    """
    if path.endswith(".py"):
        return False
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


def phase_key(record: CommitRecord) -> str | None:
    """Return the phase id (``P27``) *record* declares, or ``None``.

    A commit names its phase through exactly one of two carriers: the
    bracket-style subject prefix (``[P27-I04-W04]``, ``[P27-W19]``,
    ``[P27]``), or the ``Eawf-Wave: P##-I##-W##`` body trailer that the
    trailer-style convention emits under a bare ``<type>: <summary>``
    subject. Both are read here so the gate's view of phase membership does
    not depend on which convention the repo is configured for.

    Args:
        record: The parsed commit to read scope from.

    Returns:
        The phase id, or ``None`` when the commit declares no phase (which
        is the honest form for out-of-phase work).
    """
    subject_match = _SUBJECT_PHASE_RE.match(record.subject)
    if subject_match is not None:
        return subject_match.group(1)
    trailer_match = _WAVE_TRAILER_RE.search(record.body)
    return trailer_match.group(1) if trailer_match is not None else None


def opened_phase_ids(start: Path | None = None) -> frozenset[str]:
    """Return the ids of phases the repo has actually opened.

    Reads the committed ``.ea/state.json``, walking up from *start* to find
    the project root the way any tool invoked from a subdirectory must. The
    document is parsed as a plain mapping rather than through the typed
    state model on purpose: a CI gate must not red because the state schema
    moved under it, and only two shallow fields are needed here. Only
    phases whose status says the phase was opened
    (:data:`_OPENED_PHASE_STATUSES`) are returned, so a PLANNED phase id --
    which by definition owns no commits -- corroborates nothing.

    Absence and corruption both read as "no phases known", which fails
    closed: without corroboration the gate keeps its hard per-commit
    contract rather than waving a range through.

    Args:
        start: Directory to begin the upward search from; defaults to the
            current working directory, which is also the repo ``git`` runs
            against.

    Returns:
        The opened phase ids, empty when no readable state document exists.
    """
    origin = Path.cwd() if start is None else start
    for parent in [origin, *origin.parents]:
        candidate = parent / _STATE_RELPATH
        if not candidate.is_file():
            continue
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
            return frozenset()
        if not isinstance(raw, dict):
            return frozenset()
        phases = raw.get("phases")
        if not isinstance(phases, dict):
            return frozenset()
        return frozenset(
            phase_id
            for phase_id, phase in phases.items()
            if isinstance(phase, dict) and phase.get("status") in _OPENED_PHASE_STATUSES
        )
    return frozenset()


def is_phase_pr(records: list[CommitRecord], *, opened_phases: frozenset[str]) -> bool:
    """Return whether *records* form the PR of one or more opened phases.

    Under the one-PR-per-phase model a phase ships as a single reviewed
    unit, so the range that carries it defers to wholesale diff review plus
    the snapshot suite, which already pins golden freshness. The signal is
    the phase membership the commits declare, corroborated against the
    phases *this repo* has opened -- not the number of iters the range
    touches, which says nothing about whether the range is a phase PR (a
    phase that lands in one iter is still a phase PR).

    Ordinary work cannot fall into this path: a range that declares no phase
    contributes no key and is rejected, and a scope tag naming a phase the
    repo never opened is not corroborated.

    Args:
        records: The parsed commits of the ``base..head`` range.
        opened_phases: Phase ids the repo has opened, per
            :func:`opened_phase_ids`.

    Returns:
        ``True`` when the range names at least one phase and every phase it
        names was opened by this repo.
    """
    named = {key for record in records if (key := phase_key(record))}
    return bool(named) and named <= opened_phases


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

    if is_phase_pr(records, opened_phases=opened_phase_ids()):
        # Phase-PR model (one PR per phase): the range carries an opened
        # phase and ships as a single reviewed unit, and the snapshot test
        # suite already asserts every committed golden matches current-code
        # output. Per-commit ``test:``-subject pairing is a small-CL review
        # proxy that adds nothing here, so surface the bundled golden
        # commits for reviewer visibility without blocking the merge.
        print(
            "snapshot pairing gate: phase PR detected (every phase the range names is "
            "an opened phase in .ea/state.json); golden changes are reviewed wholesale "
            "and pinned by the snapshot test suite, so per-commit pairing is not "
            "enforced. Bundled golden-touching commits:"
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
