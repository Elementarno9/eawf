"""Lane resolution for a wave close issued with no interactive session.

A close that no agent session is attached to used to have exactly one route
past a gate-bearing wave's falsifiers: the daemonless bypass, which skips the
gates entirely behind an operator waiver. The daemon-hosted close is that
route's replacement -- the daemon runs the same gates, in the same
out-of-process sandboxed runner, that an interactive close runs.

This module answers how many waivers a scope's closes cost. A hosted close
costs zero, which is what makes it a replacement for the bypass rather than
another one.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def count_scope_waivers(store_dir: Path, *, scope_id: str) -> int:
    """Return how many persisted waiver rows *scope_id* carries.

    The bypass lane is auditable precisely because every waiver it takes lands
    as an evidence row; counting them is how a caller proves a hosted close
    took none.

    Args:
        store_dir: ``<state_dir>/store/`` -- the JSONL store root. A directory
            that does not exist yet counts as zero, because the evidence store
            is created lazily on first append.
        scope_id: Wave id whose waiver rows are counted.

    Returns:
        The number of ``status="waived"`` evidence rows for *scope_id*.

    Raises:
        ValueError: *scope_id* is empty, or *store_dir* exists but is not a
            directory.
    """
    if not scope_id:
        raise ValueError("scope_id must be a non-empty wave id")
    if store_dir.exists() and not store_dir.is_dir():
        raise ValueError(f"evidence store root is not a directory: {str(store_dir)!r}")
    from eawf.workflow.verify.readiness import (
        _filter_evidence_for_scope,
        _read_evidence_rows,
    )

    rows = _filter_evidence_for_scope(_read_evidence_rows(store_dir), scope_id=scope_id)
    count = sum(1 for row in rows if row.status == "waived")
    logger.debug(f"count_scope_waivers scope_id={scope_id!r} waivers={count}")
    return count


__all__ = [
    "count_scope_waivers",
]
