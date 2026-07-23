"""Managed ``.gitignore`` writer for ``eawf init``.

The writer appends one managed block to the target repository's
``.gitignore`` and replaces that block on re-run. Existing user lines stay
outside the managed region untouched, while generated EAWF scratch,
runtime-plugin output, and local databases stay untracked by default.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.io import fallback_wal_dir
from eawf.kernel.store.paths import store_path
from eawf.runtime.lock import sibling

_BEGIN = "# BEGIN EAWF:gitignore"
_END = "# END EAWF:gitignore"

GITIGNORE_PATTERNS: tuple[str, ...] = (
    "CLAUDE.md",
    ".claude/",
    ".codex/",
    ".opencode/",
    ".mcp.json",
    "opencode.json",
    ".ea/locks/",
    ".ea/**/*.lock",
    ".ea/local/",
    ".ea/worktrees/",
    ".ea/indexes/",
    ".ea/instrument-probe.json",
    ".ea/telemetry.db",
    "*.db",
    ".ea/state.json.bak.*",
    # The event store is the firehose, not the ledger: it accumulates one row
    # per lifecycle mutation plus the raw stdout of every spawned agent, so it
    # grows without bound and carries free text nobody typed. The evidence chain
    # that decisions cite lives in the typed stores next to it (audit, decision,
    # evidence, the agent reports), which stay committed. Tracking this one
    # instead re-stores a multi-megabyte blob on every bookkeeping commit and
    # points a raw-output channel at version control.
    ".ea/store/event.jsonl",
)


@dataclass(frozen=True)
class GitignoreWriteResult:
    """Summary of the managed ``.gitignore`` write."""

    path: Path
    patterns: tuple[str, ...]


def _escape_component(component: str) -> str:
    """Escape one literal path component for a gitignore pattern."""
    if "\r" in component or "\n" in component:
        raise ValueError("gitignore paths cannot contain CR or LF characters")
    special = frozenset({"\\", " ", "!", "#", "*", "?", "[", "]"})
    return "".join(f"\\{char}" if char in special else char for char in component)


def _rooted_pattern(
    target_dir: Path,
    path: Path,
    *,
    controlled_suffix: str = "",
    directory: bool = False,
) -> str:
    """Return a root-anchored pattern for an already-contained path."""
    relative = path.relative_to(target_dir)
    parts = [_escape_component(part) for part in relative.parts]
    if not parts:
        raise ValueError("gitignore path must name an entry below the target directory")
    parts[-1] = f"{parts[-1]}{controlled_suffix}"
    pattern = f"/{'/'.join(parts)}"
    return f"{pattern}/" if directory else pattern


def _dynamic_patterns(target_dir: Path, state_path: Path | None) -> tuple[str, ...]:
    """Derive exact runtime-artifact ignores for a repo-local state layout."""
    if state_path is None:
        return ()
    if "\r" in str(state_path) or "\n" in str(state_path):
        raise ValueError("gitignore paths cannot contain CR or LF characters")

    candidate = state_path if state_path.is_absolute() else target_dir / state_path
    resolved_state = candidate.resolve()
    if "\r" in str(resolved_state) or "\n" in str(resolved_state):
        raise ValueError("gitignore paths cannot contain CR or LF characters")
    try:
        resolved_state.relative_to(target_dir)
    except ValueError:
        return ()
    # The canonical layout is already covered by the shipped .ea patterns.
    # Avoid expanding every fresh init with redundant exact entries.
    if resolved_state == (target_dir / ".ea" / "state.json").resolve():
        return ()

    wal_dir = fallback_wal_dir(resolved_state)
    actual_lock_prefix = wal_dir.parent / "actual-"
    patterns = [
        _rooted_pattern(target_dir, sibling.lock_path(resolved_state)),
        _rooted_pattern(target_dir, resolved_state, controlled_suffix=".bak.*"),
        *(
            _rooted_pattern(
                target_dir,
                sibling.lock_path(store_path(resolved_state, kind)),
            )
            for kind in StoreKind
        ),
        _rooted_pattern(
            target_dir,
            store_path(resolved_state, StoreKind.EVENT),
        ),
        _rooted_pattern(target_dir, wal_dir, directory=True),
        _rooted_pattern(
            target_dir,
            actual_lock_prefix,
            controlled_suffix="*.lock",
        ),
    ]
    return tuple(dict.fromkeys(patterns))


def _managed_block(patterns: tuple[str, ...]) -> str:
    lines = [_BEGIN, *patterns, _END]
    return "\n".join(lines) + "\n"


def _managed_span(existing: bytes) -> tuple[int, int] | None:
    """Return byte offsets covering the first complete managed line span."""
    begin_marker = _BEGIN.encode("utf-8")
    end_marker = _END.encode("utf-8")
    begin_offset: int | None = None
    offset = 0
    for line in existing.splitlines(keepends=True):
        if line.endswith(b"\r\n"):
            body = line[:-2]
        elif line.endswith((b"\n", b"\r")):
            body = line[:-1]
        else:
            body = line
        if begin_offset is None and body == begin_marker:
            begin_offset = offset
        elif begin_offset is not None and body == end_marker:
            return begin_offset, offset + len(line)
        offset += len(line)
    return None


def _append_separator(existing: bytes) -> bytes:
    """Return spacing needed before a newly appended managed block."""
    if not existing or existing.endswith((b"\n\n", b"\r\n\r\n")):
        return b""
    if existing.endswith((b"\n", b"\r")):
        return b"\n"
    return b"\n\n"


def write_gitignore(
    target_dir: Path,
    *,
    state_path: Path | None = None,
) -> GitignoreWriteResult:
    """Write or replace the managed EAWF block in ``target_dir/.gitignore``.

    Args:
        target_dir: Repository root initialised by ``eawf init``.
        state_path: Optional state file path. Relative paths are anchored to
            ``target_dir``. Repo-local paths add exact, root-anchored ignores
            for their persistent lock, store, backup, and fallback-WAL
            artifacts. Outside paths add nothing.

    Returns:
        :class:`GitignoreWriteResult` with the target path and the exact
        managed pattern tuple.
    """
    target_dir = target_dir.resolve()
    patterns = tuple(
        dict.fromkeys(
            (
                *GITIGNORE_PATTERNS,
                *_dynamic_patterns(target_dir, state_path),
            )
        )
    )
    path = (target_dir / ".gitignore").resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_bytes() if path.exists() else b""
    block = _managed_block(patterns).encode("utf-8")
    span = _managed_span(existing)
    if span is not None:
        start, end = span
        new_content = existing[:start] + block + existing[end:]
    else:
        new_content = existing + _append_separator(existing) + block
    path.write_bytes(new_content)
    return GitignoreWriteResult(path=path, patterns=patterns)


__all__ = ["GITIGNORE_PATTERNS", "GitignoreWriteResult", "write_gitignore"]
