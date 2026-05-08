"""Sibling lockfile path helper.

Computes the conventional `.lock` sibling path for any target file.
"""

from __future__ import annotations

from pathlib import Path


def lock_path(target: Path) -> Path:
    """Return the sibling lockfile path for *target*.

    The lockfile is placed in the same directory as the target, with
    ``".lock"`` appended to the target's full name.

    Examples::

        lock_path(Path("/tmp/state.json"))   -> Path("/tmp/state.json.lock")
        lock_path(Path("/tmp/memory.jsonl")) -> Path("/tmp/memory.jsonl.lock")
    """
    target = Path(target)
    return target.with_name(target.name + ".lock")
