"""Commit-msg + diff scope linter for eawf phase-bundled commits.

Enforces:

1. Subject line must match
   ``^\\[P\\d{2}(-W\\d{2}|-CORE)?\\]\\s+(feat|fix|chore|docs|refactor|test|build|perf|ci|revert):``.
2. ``[P##-CORE]`` commits MUST touch only state-bookkeeping paths
   (``.ea/state.json``, ``.ea/store/event.jsonl``, and per-wave spec
   files under ``.ea/specs/``). Touching anything else is rejected.
3. The canonical ``Co-Authored-By: Claude <noreply@anthropic.com>``
   trailer MUST be present (the ``prepare-commit-msg`` stage hook
   auto-inserts it; this backstop rejects commits where the trailer
   was hand-deleted).

All checks run as a ``commit-msg``-stage pre-commit hook. The first
argument is the commit-message file path (pre-commit passes it). The
linter consults ``git diff --cached --name-only`` for staged paths.

Exit codes:
- ``0`` — accepted.
- ``1`` — rejected (message printed to stderr).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

_SUBJECT_RE = re.compile(
    r"^\[P\d{2}(-W\d{2}|-CORE)?\]\s+"
    r"(feat|fix|chore|docs|refactor|test|build|perf|ci|revert|state):\s+\S.*$"
)
_CORE_TAG_RE = re.compile(r"^\[P\d{2}-CORE\]\s+")
_TRAILER: str = "Co-Authored-By: Claude <noreply@anthropic.com>"
_STATE_ONLY_ALLOWED = (
    ".ea/state.json",
    ".ea/store/event.jsonl",
    # ``.secrets.baseline`` auto-tracks state.json line numbers; the
    # detect-secrets pre-commit hook regenerates it whenever state.json
    # mutates, and refuses to commit when baseline is left unstaged.
    # CORE commits therefore always need it riding along.
    ".secrets.baseline",
)
_STATE_ONLY_PREFIXES = (".ea/specs/",)


def _staged_paths() -> list[str]:
    """Return paths reported by ``git diff --cached --name-only``.

    Empty list when there are no staged changes (e.g. an amend in the
    working copy, or the hook is invoked outside a commit context).
    """
    proc = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _is_state_only_path(path: str) -> bool:
    if path in _STATE_ONLY_ALLOWED:
        return True
    return any(path.startswith(p) for p in _STATE_ONLY_PREFIXES)


def lint(message_path: Path, staged: list[str]) -> tuple[int, str]:
    """Run both checks against *message_path* + *staged* paths.

    Returns ``(exit_code, diagnostic)``.
    """
    text = message_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    subject = ""
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        subject = stripped
        break
    if not subject:
        return 1, "empty commit subject"
    if not _SUBJECT_RE.match(subject):
        return 1, (
            f"commit subject rejected: {subject!r}\n"
            "expected '[P##[-W##|-CORE]] <type>: <summary>' "
            "(type ∈ feat|fix|chore|docs|refactor|test|build|perf|ci|revert|state)"
        )
    if _CORE_TAG_RE.match(subject):
        bad = [p for p in staged if not _is_state_only_path(p)]
        if bad:
            return 1, (
                f"[P##-CORE] commit touches non-state paths: {bad}\n"
                "CORE commits must mutate only .ea/state.json, "
                ".ea/store/event.jsonl, or .ea/specs/**"
            )
    if "Co-Authored-By: Claude" not in text:
        return 1, (
            f"missing canonical trailer: {_TRAILER!r}\n"
            "the prepare-commit-msg hook should auto-insert it; "
            "re-run `git commit` to retry or paste the trailer manually"
        )
    return 0, ""


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: commit_prefix_lint.py <commit-msg-path>", file=sys.stderr)
        return 1
    message_path = Path(argv[1])
    if not message_path.exists():
        print(f"commit message file missing: {message_path}", file=sys.stderr)
        return 1
    exit_code, diag = lint(message_path, _staged_paths())
    if exit_code != 0:
        print(diag, file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
