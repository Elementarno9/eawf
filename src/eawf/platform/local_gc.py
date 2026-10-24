"""Reclaiming expired entries from a repository's machine-local ``.ea/local`` store.

An entry is a directory under ``.ea/local`` holding a ``.retention`` file
whose only content is the date it may be removed after, as ``YYYY-MM-DD``.
Nothing else is ever removed: a directory without a ``.retention``, or with
one that does not hold exactly such a date, stays where it is, and so does
every file beside the entries, research briefs included. An expired entry that
holds an entry not yet expired, or one whose date cannot be read, is kept
whole, since removing it would remove that entry too.

Planning and removal are separate so ``eawf local gc --dry-run`` shows the
exact set the apply step would remove.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

#: The file that marks a directory as reclaimable and names its expiry date.
RETENTION_FILENAME: Final = ".retention"


@dataclass(frozen=True, slots=True)
class LocalGcPlan:
    """The entries one pass over a local store would remove.

    Attributes:
        local_root: The ``.ea/local`` directory the plan was made over.
        expired: Each directory whose ``.retention`` date has passed, outermost
            only, in path order.
        held: Each directory whose date has passed but which holds an entry
            that is unexpired or unreadable; kept.
        unreadable: Each directory whose ``.retention`` holds no date; kept.
    """

    local_root: Path
    expired: tuple[Path, ...] = ()
    held: tuple[Path, ...] = ()
    unreadable: tuple[Path, ...] = ()


def _retention_date(marker: Path) -> date | None:
    """Return the expiry a ``.retention`` file states, or ``None`` when it states none."""
    try:
        return date.fromisoformat(marker.read_text(encoding="utf-8").strip())
    except OSError, UnicodeDecodeError, ValueError:
        return None


def plan_local_gc(local_root: Path, *, today: date) -> LocalGcPlan:
    """Return the entries under *local_root* whose retention has passed.

    The walk does not follow symbolic links, and a ``.retention`` that is a
    link is not read, so an entry is always a real directory inside
    *local_root*. A ``.retention`` on *local_root* itself is ignored: the store
    as a whole is never an entry.

    Args:
        local_root: The repository's ``.ea/local`` directory; absent is empty.
        today: The day judged against. An entry expiring today is kept.

    Returns:
        The plan; an empty one when *local_root* does not exist.
    """
    if not local_root.is_dir():
        return LocalGcPlan(local_root=local_root)
    expiries: dict[Path, date | None] = {}
    for marker in sorted(local_root.rglob(RETENTION_FILENAME)):
        entry = marker.parent
        if entry != local_root and marker.is_file(follow_symlinks=False):
            expiries[entry] = _retention_date(marker)
    kept = [entry for entry, expiry in expiries.items() if expiry is None or expiry >= today]
    removable: list[Path] = []
    held: list[Path] = []
    for entry, expiry in expiries.items():
        if expiry is None or expiry >= today:
            continue
        if any(inner != entry and inner.is_relative_to(entry) for inner in kept):
            held.append(entry)
        else:
            removable.append(entry)
    expired = [
        entry
        for entry in removable
        if not any(outer != entry and entry.is_relative_to(outer) for outer in removable)
    ]
    return LocalGcPlan(
        local_root=local_root,
        expired=tuple(expired),
        held=tuple(held),
        unreadable=tuple(entry for entry, expiry in expiries.items() if expiry is None),
    )


def apply_local_gc(plan: LocalGcPlan) -> None:
    """Remove every expired entry *plan* names.

    Args:
        plan: A plan :func:`plan_local_gc` returned.

    Raises:
        ValueError: An entry does not resolve strictly inside the plan's local
            root; nothing is removed.
        OSError: An entry could not be removed.
    """
    root = plan.local_root.resolve()
    for entry in plan.expired:
        resolved = entry.resolve()
        if resolved == root or not resolved.is_relative_to(root):
            raise ValueError(f"refusing to remove {entry}: it lies outside {plan.local_root}")
    for entry in plan.expired:
        shutil.rmtree(entry)
        logger.info(f"apply_local_gc removed entry={entry}")


__all__ = ["RETENTION_FILENAME", "LocalGcPlan", "apply_local_gc", "plan_local_gc"]
