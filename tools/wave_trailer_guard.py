"""Refuse a history rewrite that drops an ``Eawf-Wave`` or ``Task`` trailer.

Wave pins are recovered after a rewrite by trailer (see the lifecycle
``wave_trailer_repin`` module), so a rewrite that loses a trailer loses the
only identity that survives it. A squash that folds several waves into one
commit keeps them all, one trailer line each, and passes; a squash or rebase
that drops a line does not.

What the check sees is two tips of the same ref: *old* (what the remote holds)
and *new* (what is about to replace it). A fast-forward, where *old* is an
ancestor of *new*, rewrites nothing and passes without reading any message.
Otherwise both sides are read from their merge-base, and every wave named by
a trailer in ``base..old`` must still be named by a trailer in ``base..new``,
and so must every native Task a ``Task`` trailer names, since that
trailer is the only identity a native Task commit carries.

The same push also has each pushed commit linted under the per-Batch regime
(:func:`check_pushed_commits`), so scratch commits stay free locally and are
refused only when they would leave the machine.

``commit_prefix_lint.py`` dispatches here for its ``--check-rewrite OLD NEW``,
``--pre-push`` and ``--pre-push-lint`` modes; the last two read the tips from
the ``PRE_COMMIT_FROM_REF`` / ``PRE_COMMIT_TO_REF`` variables pre-commit
exports to ``pre-push`` hooks.

Exit codes: ``0`` accepted, ``1`` refused or unreadable history.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path

from commit_prefix_lint import (
    _TASK_TRAILER_RE,
    _WAVE_TRAILER_RE,
    _canonical_state_path,
    load_native_records,
    pushed_commit_error,
)
from epoch2_lifecycle_view import epoch2_generation

_GIT_TIMEOUT_SECONDS = 20.0
_REC_SEP = "\x00"
_COMMIT_SEP = "\x1e"
_ZERO_SHA_CHAR = "0"


def trailer_wave_ids(messages: Iterable[str]) -> set[str]:
    """Return every full wave id named on an ``Eawf-Wave`` line of *messages*.

    A short ``P##-W##`` value names the ``I01`` iter, as it does for the
    commit-msg check, so the short and full spellings of one wave compare equal.
    """
    wave_ids: set[str] = set()
    for message in messages:
        for match in _WAVE_TRAILER_RE.finditer(message):
            parts = match.group("wave").split("-")
            wave_ids.add("-".join(parts) if len(parts) == 3 else f"{parts[0]}-I01-{parts[1]}")
    return wave_ids


def trailer_task_keys(messages: Iterable[str]) -> set[str]:
    """Return every native Task key named on a ``Task`` line of *messages*."""
    return {
        match.group("key") for message in messages for match in _TASK_TRAILER_RE.finditer(message)
    }


def dropped_wave_ids(old_messages: Iterable[str], new_messages: Iterable[str]) -> list[str]:
    """Return the wave ids trailered in *old_messages* but in none of *new_messages*."""
    return sorted(trailer_wave_ids(old_messages) - trailer_wave_ids(new_messages))


def _git(args: list[str], *, repo_root: Path | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo_root) if repo_root else None,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
        check=False,
        stdin=subprocess.DEVNULL,
    )


def _range_messages(rev_range: str, *, repo_root: Path | None) -> list[str] | None:
    out = _git(["log", "--format=%B%x00", rev_range, "--"], repo_root=repo_root)
    if out.returncode != 0:
        return None
    return [message for message in out.stdout.split(_REC_SEP) if message.strip()]


def check_rewrite(old: str, new: str, *, repo_root: Path | None = None) -> tuple[int, str]:
    """Refuse replacing tip *old* with tip *new* when a wave trailer goes missing.

    Args:
        old: The tip being replaced (the remote's current commit).
        new: The tip replacing it.
        repo_root: Repository working directory; ``None`` uses the cwd.

    Returns:
        ``(exit_code, diagnostic)``; ``1`` when a trailer is dropped or
        either tip cannot be read, ``0`` otherwise.
    """
    ancestor = _git(["merge-base", "--is-ancestor", old, new], repo_root=repo_root)
    if ancestor.returncode == 0:
        return 0, ""
    base = _git(["merge-base", old, new], repo_root=repo_root)
    if ancestor.returncode != 1 or base.returncode not in (0, 1):
        return 1, (
            f"wave-trailer guard: cannot compare {old} with {new}; "
            "fetch the remote tip first so both commits are local"
        )
    # Unrelated histories share no base, so each side is read whole.
    base_sha = base.stdout.strip()
    old_messages = _range_messages(f"{base_sha}..{old}" if base_sha else old, repo_root=repo_root)
    new_messages = _range_messages(f"{base_sha}..{new}" if base_sha else new, repo_root=repo_root)
    if old_messages is None or new_messages is None:
        return 1, f"wave-trailer guard: cannot read the commits between {old} and {new}"
    dropped = dropped_wave_ids(old_messages, new_messages)
    dropped_tasks = sorted(trailer_task_keys(old_messages) - trailer_task_keys(new_messages))
    if not dropped and not dropped_tasks:
        return 0, ""
    losses = [
        f"the {name} trailer of {', '.join(ids)}"
        for name, ids in (("Eawf-Wave", dropped), ("Task", dropped_tasks))
        if ids
    ]
    return 1, (
        f"wave-trailer guard: this rewrite drops {' and '.join(losses)}. "
        "Every wave and Task must keep a trailer so its pin can be recovered after the "
        "rewrite; restore the dropped lines (a squashed commit carries one per wave or "
        "Task), or land by fast-forward instead."
    )


def check_pre_push(env: Mapping[str, str], *, repo_root: Path | None = None) -> tuple[int, str]:
    """Run :func:`check_rewrite` on the tips pre-commit exports to a pre-push hook.

    A push that creates or deletes a ref replaces no history, so it passes, as
    does a run without the variables (a manual ``--hook-stage pre-push``).
    """
    old = env.get("PRE_COMMIT_FROM_REF", "")
    new = env.get("PRE_COMMIT_TO_REF", "")
    if not old.strip(_ZERO_SHA_CHAR) or not new.strip(_ZERO_SHA_CHAR):
        return 0, ""
    return check_rewrite(old, new, repo_root=repo_root)


def check_pushed_commits(
    env: Mapping[str, str], *, repo_root: Path | None = None
) -> tuple[int, str]:
    """Lint every non-merge commit a push adds under the per-Batch regime.

    The pushed range is ``PRE_COMMIT_FROM_REF..PRE_COMMIT_TO_REF``; for a new
    branch pre-commit sets the first variable to the parent of the oldest
    commit no remote holds, so the range is still exactly what the push adds.
    A deletion, or a run without the variables, adds nothing and passes. The
    Batch and Task records are read from the canonical main-worktree
    generation, so a worktree cannot prove a commit against a stale copy.

    Args:
        env: The hook environment.
        repo_root: Repository working directory; ``None`` uses the cwd.

    Returns:
        ``(exit_code, diagnostic)``; ``1`` naming each refused commit, or when
        the range or the generation cannot be read; ``0`` otherwise.
    """
    old = env.get("PRE_COMMIT_FROM_REF", "")
    new = env.get("PRE_COMMIT_TO_REF", "")
    if not old.strip(_ZERO_SHA_CHAR) or not new.strip(_ZERO_SHA_CHAR):
        return 0, ""
    log = _git(
        ["log", "--no-merges", "--format=%H%x00%B%x1e", f"{old}..{new}", "--"],
        repo_root=repo_root,
    )
    if log.returncode != 0:
        return 1, f"commit lint: cannot read the commits between {old} and {new}"
    canonical = _canonical_state_path(repo_root or Path.cwd())
    generation = None if canonical is None else epoch2_generation(canonical.parent)
    try:
        records = None if generation is None else load_native_records(generation)
    except (OSError, ValueError) as exc:
        return 1, f"commit lint: managed state decode failed: {exc}"
    refused: list[str] = []
    for record in log.stdout.split(_COMMIT_SEP):
        sha, _, message = record.strip("\n").partition(_REC_SEP)
        if not sha:
            continue
        changed = _git(
            ["diff-tree", "--no-commit-id", "--name-only", "-r", "--root", sha],
            repo_root=repo_root,
        )
        if changed.returncode != 0:
            return 1, f"commit lint: cannot read the paths of {sha[:12]}"
        error = pushed_commit_error(message, changed.stdout.splitlines(), records)
        if error is not None:
            refused.append(f"{sha[:12]} {error}")
    if not refused:
        return 0, ""
    return 1, "\n".join(
        [
            "commit lint: this push carries commits outside the per-Batch regime:",
            *refused,
            "Squash each Batch's scratch work into one '<type>: <summary>' commit with "
            "one 'Batch: BAT-<NNNN>' trailer and a 'Task: <KEY>' trailer per Task it "
            "delivers, ending with one 'state: <summary>' commit, then push again.",
        ]
    )
