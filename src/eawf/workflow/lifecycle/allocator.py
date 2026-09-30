"""Auto-allocate zero-padded lifecycle IDs over a typed :class:`State`.

The CLI exposes ``--auto`` on ``phase open``/``iter open`` and a default-allocate
path for ``wave plan`` when no explicit wave id collides; in each case we want
the smallest free zero-padded suffix among the existing entries. The
underlying counter helpers live in :mod:`eawf.kernel.state.ids`; this module is the
state-aware adapter that pulls the existing key set and delegates.

All three allocators are pure: they read ``state`` and return a string. The
caller mounts the allocation result back onto a new :class:`Phase`/:class:`Iter`
/:class:`Wave` record.
"""

from __future__ import annotations

import logging

from eawf.kernel.state.models import State

logger = logging.getLogger(__name__)


_GRANT_ID_PREFIX = "GRANT-"


def allocate_grant_id(state: State) -> str:
    """Return the smallest free MCP grant id (``GRANT-<n>``).

    The candidate set is ``state.mcp_grants`` keys; the helper returns
    ``GRANT-<max+1>``, treating any non-numeric suffix as ignorable so a
    custom ``--grant-id`` override never blocks auto-allocation.
    """
    pool = state.mcp_grants or {}
    next_n = 1
    for existing_id in pool:
        if not existing_id.startswith(_GRANT_ID_PREFIX):
            continue
        try:
            n = int(existing_id.removeprefix(_GRANT_ID_PREFIX))
        except ValueError:
            continue
        next_n = max(next_n, n + 1)
    gid = f"{_GRANT_ID_PREFIX}{next_n}"
    logger.debug(f"allocate_grant_id existing={len(pool)} allocated={gid}")
    return gid
