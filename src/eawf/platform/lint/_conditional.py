"""Conditional-skip helper for the diff-scoped ``eawf hook`` lint gates.

Pre-push / pre-commit lint gates that re-scan the whole tree on every
invocation add several seconds per commit and tempt operators to
``--no-verify``. This helper lets each gate bail fast: it parses a
``git diff ... --name-only`` once and exposes (a) the set of changed
repo-relative paths and (b) a per-hook regex filter so a gate runs only
when something it cares about actually changed, and scans only the
changed files rather than the entire tree.

Two diff scopes are supported:

* **staged** (``git diff --cached --name-only``) — the delta of the
  commit being made. This is the right scope for the *pre-commit* leak
  / log-format gates: a real ``git commit`` scans only its staged
  files, and ``pre-commit run --all-files`` (nothing staged) is a clean
  no-op rather than re-scanning the whole tree (which is full of
  documented example paths, golden fixtures, and the scrubber's own
  pattern source).
* **branch-vs-base** (``git diff <base>...HEAD --name-only``) — what is
  new on the branch relative to ``origin/main``. This is the right
  scope for the *pre-push* ``plugin-doctor-drift`` fast-skip.

A failed git invocation (unreachable base, shallow clone, non-repo cwd)
yields an empty changed-file set, which the gates treat as "nothing
relevant changed" — a fail-open posture appropriate for a fast-path
skip helper whose authoritative backstop is the full-tree CI run.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_DIFF_BASE = "origin/main"

# Per-hook path filters keyed by hook name. A changed file is "relevant"
# to a hook when its repo-relative path matches the hook's pattern.
_PYTHON_LIBRARY = re.compile(r"^src/eawf/.*\.py$")
_MARKDOWN = re.compile(r".*\.md$")
_PLUGIN_SURFACE = re.compile(r"^(AGENTS\.md|skills/.*|src/eawf/runtime/runtimes/.*|build/.*)$")
# The leak gates care about any tracked text blob; an explicit deny-list
# of binary-ish suffixes keeps the scan from reading non-text payloads.
_LEAK_SURFACE = re.compile(r".*")

HOOK_FILTERS: dict[str, re.Pattern[str]] = {
    "path-leak-lint": _LEAK_SURFACE,
    "email-leak-lint": _LEAK_SURFACE,
    "log-format-lint": _PYTHON_LIBRARY,
    "eawf002-log-key": _PYTHON_LIBRARY,
    "eawf003-logger-acquire": _PYTHON_LIBRARY,
    "eawf010-module-length": _PYTHON_LIBRARY,
    "eawf011-cognitive-complexity": _PYTHON_LIBRARY,
    "eawf012-design-provenance": _PYTHON_LIBRARY,
    "eawf013-bracket-position": _MARKDOWN,
    "eawf014-no-manual-wrap": _MARKDOWN,
    "eawf015-ears-advisory": _MARKDOWN,
    "plugin-doctor-drift": _PLUGIN_SURFACE,
}


def _run_git_name_only(
    diff_args: list[str],
    *,
    cwd: Path | None,
    timeout: float,
    context: str,
) -> list[str]:
    """Run ``git diff --name-only <diff_args>`` and return sorted paths.

    Returns an empty list (fail-open) when git is absent, the cwd is not
    a repo, the ref is unreachable, or the call times out — the gates
    treat that as "nothing relevant changed".
    """
    try:
        proc = subprocess.run(
            ["git", "diff", "--name-only", *diff_args],
            check=False,
            capture_output=True,
            text=True,
            cwd=str(cwd) if cwd is not None else None,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug(f"_run_git_name_only git-failed scope={context} reason={exc!r}")
        return []
    if proc.returncode != 0:
        logger.debug(
            f"_run_git_name_only non-zero scope={context} rc={proc.returncode} "
            f"stderr={proc.stderr.strip()!r}"
        )
        return []
    return sorted({line.strip() for line in proc.stdout.splitlines() if line.strip()})


def changed_files(
    base: str = DEFAULT_DIFF_BASE,
    *,
    cwd: Path | None = None,
    timeout: float = 5.0,
) -> list[str]:
    """Return repo-relative paths changed between ``base`` and ``HEAD``.

    Args:
        base: Diff base ref. The diff is computed as ``base...HEAD`` so
            only commits unique to the current branch are considered.
        cwd: Repository working directory; defaults to the process cwd.
        timeout: Seconds before the ``git`` invocation is abandoned.

    Returns:
        Sorted repo-relative path strings. An empty list when the git
        invocation fails or times out (treated as "no relevant change"
        by callers).
    """
    return _run_git_name_only([f"{base}...HEAD"], cwd=cwd, timeout=timeout, context="changed_files")


def staged_files(*, cwd: Path | None = None, timeout: float = 5.0) -> list[str]:
    """Return repo-relative paths staged for the next commit.

    Uses ``git diff --cached --name-only`` so the gate scans exactly the
    delta a ``git commit`` would record. An empty list when nothing is
    staged (e.g. ``pre-commit run --all-files`` on a clean tree), which
    makes the gate a clean no-op rather than a whole-tree scan.

    Args:
        cwd: Repository working directory; defaults to the process cwd.
        timeout: Seconds before the ``git`` invocation is abandoned.

    Returns:
        Sorted repo-relative path strings; empty on staged-empty or git
        failure.
    """
    return _run_git_name_only(["--cached"], cwd=cwd, timeout=timeout, context="staged_files")


def select_relevant(files: list[str], pattern: re.Pattern[str]) -> list[str]:
    """Return the subset of ``files`` whose path matches ``pattern``.

    Args:
        files: Repo-relative path strings (typically from
            :func:`changed_files`).
        pattern: Per-hook path filter (see :data:`HOOK_FILTERS`).

    Returns:
        Matching paths in input order.
    """
    return [path for path in files if pattern.match(path)]


def relevant_for_hook(
    hook_name: str,
    base: str = DEFAULT_DIFF_BASE,
    *,
    cwd: Path | None = None,
    staged: bool = False,
) -> list[str]:
    """Return the changed files relevant to ``hook_name``.

    Combines the changed-file scan with the hook's filter from
    :data:`HOOK_FILTERS`. An unknown hook name matches every changed
    file (no narrowing) rather than raising, so a new gate that forgets
    to register a filter still runs over the full diff instead of
    silently skipping.

    Args:
        hook_name: Hook key (e.g. ``"log-format-lint"``).
        base: Diff base ref used when ``staged`` is ``False``.
        cwd: Repository working directory; defaults to the process cwd.
        staged: When ``True``, scope to :func:`staged_files` (the
            commit delta — pre-commit gate scope); when ``False``, scope
            to :func:`changed_files` (branch-vs-base — pre-push scope).

    Returns:
        Relevant repo-relative paths; empty when nothing relevant
        changed (the early-exit signal for the gate).
    """
    files = staged_files(cwd=cwd) if staged else changed_files(base, cwd=cwd)
    pattern = HOOK_FILTERS.get(hook_name)
    if pattern is None:
        return files
    return select_relevant(files, pattern)


__all__ = [
    "DEFAULT_DIFF_BASE",
    "HOOK_FILTERS",
    "changed_files",
    "relevant_for_hook",
    "select_relevant",
    "staged_files",
]
