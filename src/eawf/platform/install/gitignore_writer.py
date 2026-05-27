"""Managed ``.gitignore`` writer for ``eawf init``.

The writer appends one managed block to the target repository's
``.gitignore`` and replaces that block on re-run. Existing user lines stay
outside the managed region untouched, while generated EAWF scratch,
runtime-plugin output, and local databases stay untracked by default.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

_BEGIN = "# BEGIN EAWF:gitignore"
_END = "# END EAWF:gitignore"

GITIGNORE_PATTERNS: tuple[str, ...] = (
    "CLAUDE.md",
    ".claude/",
    ".codex/",
    ".opencode/",
    ".ea/locks/",
    ".ea/local/",
    ".ea/worktrees/",
    ".ea/indexes/",
    ".ea/instrument-probe.json",
    ".ea/telemetry.db",
    "*.db",
    ".ea/state.json.bak.*",
)


@dataclass(frozen=True)
class GitignoreWriteResult:
    """Summary of the managed ``.gitignore`` write."""

    path: Path
    patterns: tuple[str, ...]


def _managed_block() -> str:
    lines = [_BEGIN, *GITIGNORE_PATTERNS, _END]
    return "\n".join(lines) + "\n"


def write_gitignore(target_dir: Path) -> GitignoreWriteResult:
    """Write or replace the managed EAWF block in ``target_dir/.gitignore``.

    Args:
        target_dir: Repository root initialised by ``eawf init``.

    Returns:
        :class:`GitignoreWriteResult` with the target path and the exact
        shipped pattern tuple.
    """
    path = (target_dir / ".gitignore").resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    block = _managed_block()
    if _BEGIN in existing and _END in existing:
        before, rest = existing.split(_BEGIN, 1)
        _, after = rest.split(_END, 1)
        new_text = before.rstrip() + "\n\n" + block + after.lstrip()
    else:
        prefix = existing.rstrip()
        new_text = f"{prefix}\n\n{block}" if prefix else block
    path.write_text(new_text, encoding="utf-8")
    return GitignoreWriteResult(path=path, patterns=GITIGNORE_PATTERNS)


__all__ = ["GITIGNORE_PATTERNS", "GitignoreWriteResult", "write_gitignore"]
