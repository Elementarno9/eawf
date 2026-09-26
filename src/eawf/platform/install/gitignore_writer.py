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
from eawf.platform.install.managed_block import render_managed_block, splice_managed_block
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
    # The rendered policy file, its per-runtime views and the role carriers
    # regenerate from a repository's own .ea/rules.yaml on `eawf sync`, so
    # committing them would create a second copy of the same content that
    # drifts from the source -- and for AGENTS.override.md, would commit the
    # machine-local workspace layer it composes in.
    "AGENTS.override.md",
    ".ea/rules/views/",
    ".claude/skills/eawf-rules-*/",
    # Per-spawn claude config homes: eawf.runtime.runtimes.claude.managed_run
    # creates one under <cwd>/.eawf-mcp/spawn-<uuid>/ for every headless spawn
    # and removes it when the spawn ends, so it never belongs in the repo.
    ".eawf-mcp/",
    ".ea/locks/",
    ".ea/**/*.lock",
    ".ea/local/",
    ".ea/worktrees/",
    ".ea/indexes/",
    # Per-wave spec renders: the typed criteria in state.json are the record.
    ".ea/specs/",
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

    Raises:
        ManagedBlockError: When the existing file's markers are not exactly
            one ordered pair; the file is left untouched.
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
    block = render_managed_block(begin=_BEGIN, end=_END, body_lines=patterns)
    path.write_bytes(splice_managed_block(existing, begin=_BEGIN, end=_END, block=block))
    return GitignoreWriteResult(path=path, patterns=patterns)


__all__ = ["GITIGNORE_PATTERNS", "GitignoreWriteResult", "write_gitignore"]
