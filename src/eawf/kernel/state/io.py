"""Epoch markers, the legacy-write fence and the state digest.

- :func:`epoch_marker_present` says whether a tree carries the epoch-2
  marker, so its generation, not ``state.json``, is the authority.
- :func:`refuse_legacy_write` is the fence every remaining ``state.json``
  writer calls first; a marked tree refuses the write.
- :func:`state_version` is the stable digest the daemon's transaction and
  recovery paths stamp on a document.
- :func:`fallback_wal_dir` names the repo-local write-ahead log directory
  the install helpers keep out of git.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, ClassVar, Final

import orjson

from eawf.kernel.migration.epoch2.canary import GENERATIONS_DIRNAME, MARKER_FILENAME

logger = logging.getLogger(__name__)

#: The stable code an epoch-1 write refused on a cut-over tree carries.
LEGACY_OPERATION_REMOVED: Final = "legacy_operation_removed"

#: The epoch-1 authority document's file name; only a write to it is fenced.
STATE_FILENAME: Final = "state.json"


class StateValidationError(ValueError):
    """A ``state.json`` write was refused.

    Subclasses :class:`ValueError` so callers that have no CLI-error context
    can still catch it; the CLI boundary maps it onto
    :class:`eawf.surfaces.cli.errors.ValidationError` (exit code 2).
    """


class LegacyOperationRemovedError(StateValidationError):
    """An epoch-1 write was aimed at a tree that carries the epoch marker.

    A :class:`StateValidationError`, so every caller that already maps a
    refused state write onto the validation bucket reports this one too.

    Attributes:
        code: The stable refusal code.
    """

    code: ClassVar[str] = LEGACY_OPERATION_REMOVED

    def __init__(self, *, ea_dir: Path) -> None:
        """Build the refusal for the tree rooted at ``ea_dir``.

        Args:
            ea_dir: The tree root. Only its directory name reaches the
                message, so a pasted refusal carries no home directory.
        """
        super().__init__(
            f"{LEGACY_OPERATION_REMOVED}: {ea_dir.name}/{GENERATIONS_DIRNAME}/{MARKER_FILENAME} "
            f"exists, so {STATE_FILENAME} is frozen and this epoch-1 mutation was refused; "
            "nothing was written"
        )


def epoch_marker_present(ea_dir: Path) -> bool:
    """Return whether the tree rooted at ``ea_dir`` carries the epoch marker.

    Only the marker's presence is asked, not whether the tree was granted
    epoch 2: a marker on an undeclared tree is a half-done cutover, and an
    epoch-1 writer landing there would still be a second writer. The
    answer is re-read on every call because a rollback removes the marker
    and must give the epoch-1 writers their tree back at once; one ``stat``
    is noise beside the fsynced write it guards.

    Args:
        ea_dir: The tree root, the directory that holds ``state.json``.

    Returns:
        ``True`` when ``generations/EPOCH2_ACTIVE.json`` exists under it.
    """
    return (ea_dir / GENERATIONS_DIRNAME / MARKER_FILENAME).exists()


def refuse_legacy_write(state_path: Path) -> None:
    """Refuse an epoch-1 write to ``state_path`` on a tree that carries the marker.

    No writer is let through on a marked tree, a native handler included:
    the selected generation is the only authority there, so a second copy
    of any record in the v1 document would be a second writer.

    Args:
        state_path: The file about to be written. A file not named
            ``state.json`` is never refused, so the generic JSON writer can
            call this for every target.

    Raises:
        LegacyOperationRemovedError: ``state_path`` is a ``state.json``
            whose directory carries the epoch marker.
    """
    path = Path(state_path)
    if path.name != STATE_FILENAME or not epoch_marker_present(path.parent):
        return
    logger.info(f"refuse_legacy_write refused tree={path.parent.name}")
    raise LegacyOperationRemovedError(ea_dir=path.parent)


def state_version(payload: dict[str, Any]) -> str:
    """Return a stable 16-hex-char digest of a state payload.

    Args:
        payload: JSON-mode state payload.

    Returns:
        The first 16 hex chars of the sorted-key sha256 digest.
    """
    raw = orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)
    return hashlib.sha256(raw).hexdigest()[:16]


def fallback_wal_dir(state_path: Path) -> Path:
    """Return the in-process fallback WAL directory for *state_path*.

    The fallback writer is the V1 carve-out (CI / one-shot / recovery shell)
    — it does not share the daemon's per-user ``<runtime>/wal``. Instead it
    keeps a repo-local WAL under ``.ea/locks/wal/`` so a crash mid-write is
    reconcilable on the next mutation against the same repo's ``state.json``
    + ``event.jsonl``. ``.ea/locks/`` is gitignored machine-local scratch,
    the right home for a transient recovery aid.

    Args:
        state_path: Path to the active ``state.json``.

    Returns:
        The repo-local ``locks/wal`` directory beside the state file.
    """
    return state_path.parent / "locks" / "wal"
