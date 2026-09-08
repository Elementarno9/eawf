"""Lane resolution for a wave close issued with no interactive session.

A close that no agent session is attached to used to have exactly one route
past a gate-bearing wave's falsifiers: the daemonless bypass, which skips the
gates entirely behind an operator waiver. The daemon-hosted close is that
route's replacement -- the daemon runs the same gates, in the same
out-of-process sandboxed runner, that an interactive close runs.

This module answers the two questions the close surfaces ask about that
choice: which lane a given close takes, and how many waivers the lane cost.
A hosted close costs zero, which is what makes it a replacement for the
bypass rather than another one.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from eawf.kernel.spec.common import _StrictModel

logger = logging.getLogger(__name__)

#: Whether a close carries an interactive agent session.
#:
#: * ``interactive`` -- an operator or agent session issued the close and is
#:   attached to it for its lifetime.
#: * ``hosted`` -- no session is attached; the daemon owns the close from
#:   submission to verdict.
CloseSessionMode = Literal["interactive", "hosted"]


class HostedCloseDecision(_StrictModel):
    """Which lane one close takes, and what that lane costs in waivers.

    Attributes:
        mode: The issuing session mode -- see :data:`CloseSessionMode`.
        hosted: Whether the daemon hosts the close and therefore runs every
            gate out of process. ``False`` means the close falls to the
            daemonless lane.
        waiver_required: Whether the lane needs an explicit operator waiver
            before it will let a gate-bearing wave close. Only the daemonless
            lane ever does; a hosted close runs the gates instead of waiving
            them.
        reason: Operator-facing prose naming why the lane was chosen. Carried
            so the CLI can quote one sentence rather than re-deriving it.
    """

    mode: CloseSessionMode
    hosted: bool
    waiver_required: bool
    reason: str


def resolve_hosted_close(
    *,
    mode: CloseSessionMode,
    daemon_available: bool,
    gate_bearing: bool,
) -> HostedCloseDecision:
    """Return the lane one close takes and whether it needs a waiver.

    A reachable daemon hosts the close whatever the issuing mode, because the
    daemon runs the gates in the crash-isolated out-of-process runner either
    way -- that is the whole point of hosting, and it is why a headless close
    stops being a degraded mode. Only an unreachable daemon drops the close to
    the daemonless lane, where a gate-bearing wave has no way to run its
    falsifiers and therefore needs the operator's explicit waiver.

    Args:
        mode: Whether an interactive session issued the close.
        daemon_available: Whether the daemon answered. The caller probes; this
            function performs no I/O.
        gate_bearing: Whether the closing wave attaches typed gates. A wave
            with no gates has nothing to falsify, so the daemonless lane
            carries it with no waiver.

    Returns:
        The :class:`HostedCloseDecision` for this close.
    """
    if daemon_available:
        return HostedCloseDecision(
            mode=mode,
            hosted=True,
            waiver_required=False,
            reason=(
                "the daemon hosts the close and runs every gate out of process; "
                "no bypass waiver applies"
            ),
        )
    if not gate_bearing:
        return HostedCloseDecision(
            mode=mode,
            hosted=False,
            waiver_required=False,
            reason="the daemon is unavailable and the wave attaches no gate to run",
        )
    return HostedCloseDecision(
        mode=mode,
        hosted=False,
        waiver_required=True,
        reason=(
            "the daemon is unavailable, so a gate-bearing close can only take the "
            "daemonless bypass lane, which records and counts an operator waiver"
        ),
    )


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
    "CloseSessionMode",
    "HostedCloseDecision",
    "count_scope_waivers",
    "resolve_hosted_close",
]
