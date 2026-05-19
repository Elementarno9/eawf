"""Filesystem-side helpers shared across per-runtime renderers.

See :mod:`eawf.runtimes.helpers` for the module-level KISS-004 LOC
budget. Each function in this file replaces a previously-duplicated
sibling under ``eawf.runtimes.{claude,codex,opencode}.plugin_install``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

DEFAULT_TIMESTAMP: str = "1970-01-01T00:00:00+00:00"
"""Frozen rendered-at timestamp used when the caller does not pin one.

Two installs minutes apart produce byte-identical output because the
timestamp is the only non-deterministic input the renderer would
otherwise pick up. Production callers (the daemon-mediated plugin
sync path) may pass an explicit ``datetime.now(UTC).isoformat()`` when
cohesive timestamps matter to a downstream consumer.
"""


FileAction = Literal["created", "updated", "unchanged"]


@dataclass(frozen=True)
class FileDelta:
    """One file the renderer wrote or would have written.

    Attributes:
        path: Absolute path the file lives at after rendering.
        action: ``"created"`` when the file did not exist prior to
            this run, ``"updated"`` when its bytes changed,
            ``"unchanged"`` when the rendered bytes match the
            existing file (so re-runs report cleanly).
    """

    path: Path
    action: FileAction


def classify_action(path: Path, payload: bytes) -> FileAction:
    """Classify writing *payload* at *path* as create / update / unchanged.

    Args:
        path: Destination path. Need not exist.
        payload: Bytes the caller intends to write.

    Returns:
        ``"created"`` when *path* is absent, ``"updated"`` when its
        current bytes differ from *payload*, ``"unchanged"`` when
        the bytes already match.
    """
    if not path.exists():
        return "created"
    if path.read_bytes() == payload:
        return "unchanged"
    return "updated"


def ensure_dir(path: Path) -> None:
    """Create *path* and any missing parents (no-op if it exists).

    Wrapper over :meth:`pathlib.Path.mkdir` whose name reads as the
    operator-intent verb at the call site.
    """
    path.mkdir(parents=True, exist_ok=True)


__all__ = [
    "DEFAULT_TIMESTAMP",
    "FileAction",
    "FileDelta",
    "classify_action",
    "ensure_dir",
]
