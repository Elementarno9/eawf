"""Lint the subject and trailer grammar of commits already written.

History is never rewritten to follow a grammar change, so the grammar
must keep reading what it was once written in. A commit that predates
the switch to the epoch-2 lifecycle -- an ancestor of the commit that
first added the opt-in declaration -- may carry any epoch-1 bracket
scope, including the retired ``-CORE`` suffix and types the grammar no
longer lists, and is accepted without a warning. A commit after the
switch is held to the grammar the commit-msg hook enforces today: one of
the accepted subject forms, and a ``Task`` trailer in its fixed place.
The deprecated bracket-wave and task-prefix forms warn there.

Only the message is read. State is not: a Task that was live when its
commit landed has long since completed, so proving it again here would
reject every commit ever made under it.

``commit_prefix_lint.py`` dispatches here for ``--check-history RANGE``.

Exit codes: ``0`` accepted (warnings may print), ``1`` a commit's grammar
is invalid or the history could not be read.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from commit_prefix_lint import (
    _SUBJECT_BARE_CONVENTIONAL_RE,
    _SUBJECT_BARE_RE,
    _SUBJECT_TASK_PREFIX_RE,
    _SUBJECT_WAVE_RE,
    _extract_subject,
    task_trailer_position_error,
)

#: The declaration whose first commit is the switch to the epoch-2 lifecycle.
SWITCH_MARKER = ".ea/epoch2-opt-in.json"

#: Any epoch-1 bracket scope the grammar was written in before the switch,
#: the retired ``-CORE`` suffix and the scopeless ``[CORE]`` included.
_HISTORICAL_SUBJECT_RE = re.compile(r"^\[(?:P\d{2,}(?:-I\d{2,})?(?:-W\d{2,}|-CORE)?|CORE)\]\s+\S")

_GIT_TIMEOUT_SECONDS = 60.0
# The separators the ``--format`` below spells as ``%x1e`` and ``%x00``.
# The separators the ``--format`` below spells as ``%x1e`` and ``%x00``.
_RECORD_SEP = "\x1e"
_FIELD_SEP = "\x00"


def history_verdict(message: str, *, predates_switch: bool) -> tuple[int, str]:
    """Return the grammar verdict for one commit message already written.

    Args:
        message: The full commit message.
        predates_switch: Whether the commit is an ancestor of the switch.

    Returns:
        ``(1, diagnostic)`` for a subject no grammar of its era accepts or
        a misplaced ``Task`` trailer after the switch; ``(0, warning)`` for
        a deprecated form after the switch; ``(0, "")`` otherwise.
    """
    subject = _extract_subject(message)
    task_prefix = _SUBJECT_TASK_PREFIX_RE.match(subject)
    wave_form = _SUBJECT_WAVE_RE.match(subject)
    current = (
        wave_form
        or task_prefix
        or _SUBJECT_BARE_RE.match(subject)
        or _SUBJECT_BARE_CONVENTIONAL_RE.match(subject)
    )
    if predates_switch:
        if current is not None or _HISTORICAL_SUBJECT_RE.match(subject):
            return 0, ""
        return 1, f"subject matches no epoch-1 commit grammar: {subject!r}"
    if current is None:
        return 1, f"subject matches no commit grammar: {subject!r}"
    position = task_trailer_position_error(message)
    if position is not None:
        return 1, position
    if task_prefix is not None:
        return 0, f"deprecated task-prefix subject: {subject!r}"
    if wave_form is not None:
        return 0, f"deprecated bracket-prefix wave subject: {subject!r}"
    return 0, ""


def _git(repo_root: Path | None, *args: str) -> str:
    """Run one read-only git command and return its stdout.

    Raises:
        subprocess.CalledProcessError: git exits non-zero.
        subprocess.TimeoutExpired: git does not answer in time.
    """
    proc = subprocess.run(
        ["git", *args],
        cwd=None if repo_root is None else str(repo_root),
        check=True,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    return proc.stdout


def pre_switch_commits(repo_root: Path | None) -> frozenset[str]:
    """Return every commit that predates the switch, or all of history when none happened.

    Raises:
        subprocess.CalledProcessError: git cannot read the history.
    """
    added = _git(repo_root, "log", "--diff-filter=A", "--format=%H", "--", SWITCH_MARKER).split()
    if not added:
        return frozenset(_git(repo_root, "rev-list", "--all").split())
    switch = added[-1]
    return frozenset(_git(repo_root, "rev-list", switch).split()) - {switch}


def check_history(rev_range: str, *, repo_root: Path | None = None) -> tuple[int, str]:
    """Lint the grammar of every non-merge commit in *rev_range*.

    Args:
        rev_range: A revision range git understands (``base..head``).
        repo_root: The repository to read; ``None`` uses the cwd.

    Returns:
        ``(exit_code, report)``: one line per rejected or warned commit,
        and a non-zero code when any commit was rejected or the history
        could not be read.
    """
    try:
        early = pre_switch_commits(repo_root)
        log = _git(repo_root, "log", "--no-merges", "--format=%H%x00%B%x1e", rev_range)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"commit history unreadable for {rev_range!r}: {exc}"
    rejected: list[str] = []
    warned: list[str] = []
    for record in log.split(_RECORD_SEP):
        sha, _, message = record.strip("\n").partition(_FIELD_SEP)
        if not sha:
            continue
        code, diag = history_verdict(message, predates_switch=sha in early)
        if diag:
            (rejected if code else warned).append(f"{sha[:12]} {diag.splitlines()[0]}")
    lines = [f"rejected: {line}" for line in rejected] + [f"warning: {line}" for line in warned]
    return (1 if rejected else 0), "\n".join(lines)
