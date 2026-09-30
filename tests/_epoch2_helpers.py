"""Test helpers that lay down a tree the way ``eawf init`` leaves it after the flag day.

A plain epoch-1 tree refuses every mutating verb, so a test that drives a
mutating verb needs a tree born at epoch 2. These helpers bear one through
the same library writer ``eawf init`` uses, rather than a second writer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson

from eawf.platform.install.epoch2_birth import bear_epoch2_tree


def lay_epoch2_tree(
    repo_root: Path, *, state: dict[str, Any] | None = None, project_code: str = "QR"
) -> Path:
    """Write ``state`` as the tree's document, bear the tree at epoch 2, return its path.

    Args:
        repo_root: The repository root; the tree is ``repo_root/.ea``.
        state: The epoch-1 document to write first; empty when omitted.
        project_code: The code the generation is named for.

    Returns:
        The tree's ``state.json``.
    """
    state_path = repo_root / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(orjson.dumps(state if state is not None else {}))
    bear_epoch2_tree(state_path, project_code=project_code, born_at=datetime.now(UTC))
    return state_path
