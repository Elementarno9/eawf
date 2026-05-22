"""CI snapshot-pairing gate for ``tests/golden/**`` mutations.

Per the C09 §5.6 snapshot-update flow: an operator regenerates a golden
surface with ``eawf snapshot update --kind <surface>``, diffs the tree,
and commits the rewritten bytes as ``[P##-W##] test: snapshot update
<kind>``. This gate enforces the contract from the CI side — *every*
commit in the PR range that mutates a managed golden surface MUST carry
a wave-form ``test:`` subject so a golden change can never sneak in under
an unrelated ``feat:`` / ``fix:`` commit.

The watched directories are sourced from the same C09 §5.6 surface
inventory the CLI drives (:data:`eawf.cli.commands.snapshot.SNAPSHOT_SURFACES`)
so the gate and ``eawf snapshot update --kind`` cannot drift. Golden
trees *not* in the inventory (e.g. ``tests/golden/cli/`` help-panel
snapshots, which refresh as a side-effect of any wave that adds a CLI
command) are deliberately out of scope — they have their own per-wave
refresh path and need no paired ``test:`` commit.

The gate walks the commits between the PR base and head. The contract
targets *mutations* of already-committed goldens (status ``M`` / ``D`` /
``R``) — silently rewriting golden bytes is exactly what must ride a
paired ``test:`` commit. Pure *additions* (status ``A``) are exempt: a
brand-new surface ships its fixtures alongside the ``feat:`` wave that
introduces it.

For each commit that *modifies / deletes / renames* a ``tests/golden/**``
file, the subject must match one of:

- ``[P##-W##] test: <summary>`` (planned wave deliverable);
- ``[P##-I##-W##] test: <summary>`` (iter >= I02 variant);
- ``[P##(-I##)?-CORE] test: <summary>`` (legacy bookkeeping alias).

``W00`` / ``I00`` are rejected — wave / iter indices are 1-based. A
commit mutating golden fixtures with any other subject (wrong type,
missing wave suffix) fails the gate.

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

from eawf.cli.commands.snapshot import SNAPSHOT_SURFACES

# Managed golden directories, sourced from the C09 §5.6 surface inventory
# so the gate's watch set and ``eawf snapshot update --kind`` share one
# source of truth. Each entry has a trailing slash so ``startswith`` only
# matches files *inside* the directory, never a sibling prefix.
_WATCHED_DIRS: tuple[str, ...] = tuple(
    sorted(f"{surface.golden_dir}/" for surface in SNAPSHOT_SURFACES.values())
)

# Wave-form ``test:`` subject — mirrors the commit-prefix grammar in
# ``tools/commit_prefix_lint.py`` narrowed to ``type == 'test'``. The
# ``(?!00)`` lookaheads reject ``I00`` / ``W00`` (1-based indices). Both
# the ``-W##`` planned-wave suffix and the legacy ``-CORE`` bookkeeping
# alias satisfy the pairing contract.
_PAIRED_SUBJECT_RE = re.compile(r"^\[P\d{2}(-I(?!00)\d{2})?(-W(?!00)\d{2}|-CORE)\]\s+test:\s+\S.*$")


def _run_git(args: list[str]) -> str:
    """Return stdout of ``git <args>`` (stripped); raise on failure."""
    proc = subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout


def commits_in_range(base: str, head: str) -> list[str]:
    """Return the commit SHAs in ``base..head`` (oldest-last is fine).

    Args:
        base: The PR base SHA (merge-base side).
        head: The PR head SHA.

    Returns:
        The list of commit SHAs reachable from *head* but not *base*.
    """
    out = _run_git(["rev-list", f"{base}..{head}"])
    return [line.strip() for line in out.splitlines() if line.strip()]


def _is_managed_golden(path: str) -> bool:
    """Return whether *path* lives inside a managed snapshot surface dir."""
    return any(path.startswith(prefix) for prefix in _WATCHED_DIRS)


def commit_mutates_golden(sha: str) -> bool:
    """Return whether *sha* modifies / deletes / renames a managed golden.

    "Managed" means a file under one of the C09 §5.6 surface directories
    (:data:`_WATCHED_DIRS`). Pure additions (status ``A``) are
    intentionally excluded: a new surface ships its fixtures with the
    ``feat:`` wave that introduces them. Only mutations of already-
    committed bytes (``M`` / ``D`` / ``R``) require a paired ``test:``
    subject.
    """
    out = _run_git(
        [
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            "--diff-filter=MDR",
            sha,
        ]
    )
    return any(_is_managed_golden(line.strip()) for line in out.splitlines())


def commit_subject(sha: str) -> str:
    """Return the subject (first line) of *sha*'s commit message."""
    return _run_git(["log", "-1", "--format=%s", sha]).strip()


def is_paired(subject: str) -> bool:
    """Return whether *subject* satisfies the wave-form ``test:`` grammar."""
    return bool(_PAIRED_SUBJECT_RE.match(subject))


def find_unpaired(base: str, head: str) -> list[tuple[str, str]]:
    """Return ``(sha, subject)`` for every unpaired golden-mutating commit.

    A commit is *unpaired* when it modifies / deletes / renames a
    ``tests/golden/**`` file but its subject does not match the wave-form
    ``test:`` grammar.

    Args:
        base: The PR base SHA.
        head: The PR head SHA.

    Returns:
        The offending commits as ``(short_sha, subject)`` tuples; empty
        when every golden-mutating commit is correctly paired.
    """
    offenders: list[tuple[str, str]] = []
    for sha in commits_in_range(base, head):
        if not commit_mutates_golden(sha):
            continue
        subject = commit_subject(sha)
        if not is_paired(subject):
            offenders.append((sha[:9], subject))
    return offenders


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

    offenders = find_unpaired(base, head)
    if not offenders:
        print("snapshot pairing gate: ok (all golden changes paired)")
        return 0

    print(
        "snapshot pairing gate: unpaired tests/golden/** mutation(s) detected.\n"
        "Every commit that touches a golden fixture must carry a wave-form\n"
        "'test:' subject, e.g. '[P27-W19] test: snapshot update <kind>'\n"
        "(regenerate with `eawf snapshot update --kind <kind>` first).\n"
        "Offending commits:",
        file=sys.stderr,
    )
    for short_sha, subject in offenders:
        print(f"  {short_sha} {subject!r}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
