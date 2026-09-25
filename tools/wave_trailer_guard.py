"""Refuse a history rewrite that drops a wave's ``Eawf-Wave`` trailer.

Wave pins are recovered after a rewrite by trailer (see the lifecycle
``wave_trailer_repin`` module), so a rewrite that loses a trailer loses the
only identity that survives it. A squash that folds several waves into one
commit keeps them all, one trailer line each, and passes; a squash or rebase
that drops a line does not.

What the check sees is two tips of the same ref: *old* (what the remote holds)
and *new* (what is about to replace it). A fast-forward, where *old* is an
ancestor of *new*, rewrites nothing and passes without reading any message.
Otherwise both sides are read from their merge-base, and every wave named by
a trailer in ``base..old`` must still be named by a trailer in ``base..new``.

``commit_prefix_lint.py`` dispatches here for its ``--check-rewrite OLD NEW``
and ``--pre-push`` modes; the latter reads the tips from the
``PRE_COMMIT_FROM_REF`` / ``PRE_COMMIT_TO_REF`` variables pre-commit exports
to ``pre-push`` hooks.

Exit codes: ``0`` accepted, ``1`` refused or unreadable history.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path

from commit_prefix_lint import _WAVE_TRAILER_RE

_GIT_TIMEOUT_SECONDS = 20.0
_REC_SEP = "\x00"
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
    if not dropped:
        return 0, ""
    return 1, (
        f"wave-trailer guard: this rewrite drops the Eawf-Wave trailer of {', '.join(dropped)}. "
        "Every wave must keep a trailer so its pin can be recovered after the rewrite; "
        "restore the dropped lines (a squashed commit carries one per wave), or land by "
        "fast-forward instead."
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
