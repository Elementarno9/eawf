"""Doctor check comparing the committed canonical sequence with its own tier.

Kept out of :mod:`eawf.observability.doctor.checks` so that module stays
under the EAWF010 line budget; ``checks.run_all`` imports and registers it.

The selected generation's ``state.json`` carries ``canonical_sequence`` as
of its last committed write. A write that touches only local status (a Run
row, a Run ledger line) moves the live cursor in the machine-local status
projection without changing a tracked file, so a committed sequence behind
the live one is the healthy state of a tree, not drift. Drift is the
committed file behind a change its own tier records: the committed change
feed filed a change at a sequence the committed file never reached, so the
tracked document lost a write the tracked history kept.
"""

from __future__ import annotations

import json
from pathlib import Path

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.store.changes import committed_high_water
from eawf.kernel.store.compaction import CANONICAL_SEQUENCE_KEY
from eawf.observability.doctor.models import CheckResult

CHECK_NAME = "canonical_sequence_drift"


def check_canonical_sequence_drift(*, workspace: Path | None) -> CheckResult:
    """Report whether the committed ``canonical_sequence`` lags its committed tier.

    Args:
        workspace: The workspace anchor (the ``.ea/`` parent), already
            resolved by the caller; ``None`` when no anchor exists.

    Returns:
        ``ok`` when no anchor or epoch-2 generation exists, when the
        committed change feed records nothing, or when the committed
        sequence reaches its newest change; ``warn`` naming both values
        when the committed sequence is behind it, and ``warn`` naming the
        document when it cannot be read.
    """
    if workspace is None:
        return CheckResult(name=CHECK_NAME, status="ok", detail="no workspace anchor")
    authority = resolve_authority(Path(workspace) / ".ea")
    if authority.gap is not None or authority.target is None or authority.generation_id is None:
        return CheckResult(name=CHECK_NAME, status="ok", detail="no epoch-2 generation")
    document = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    if not document.is_file():
        return CheckResult(name=CHECK_NAME, status="ok", detail="no generation document")
    try:
        body = json.loads(document.read_bytes())
    except (OSError, ValueError) as exc:
        return CheckResult(
            name=CHECK_NAME,
            status="warn",
            detail=f"generation {authority.generation_id} document is unreadable: {exc}",
        )
    if not isinstance(body, dict):
        return CheckResult(
            name=CHECK_NAME,
            status="warn",
            detail=f"generation {authority.generation_id} document is unreadable: not an object",
        )
    committed = body.get(CANONICAL_SEQUENCE_KEY)
    high_water = committed_high_water(document)
    if high_water is None:
        return CheckResult(name=CHECK_NAME, status="ok", detail="no committed change recorded yet")
    if committed is not None and committed >= high_water:
        return CheckResult(
            name=CHECK_NAME,
            status="ok",
            detail=(
                f"committed canonical_sequence {committed} reaches the newest committed "
                f"change {high_water}"
            ),
        )
    return CheckResult(
        name=CHECK_NAME,
        status="warn",
        detail=(
            f"committed canonical_sequence {'absent' if committed is None else committed} "
            f"is behind the committed change feed at {high_water}; "
            "the tracked document lost a write its tracked history kept"
        ),
    )


__all__ = ["CHECK_NAME", "check_canonical_sequence_drift"]
