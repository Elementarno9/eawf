"""Doctor check comparing the committed canonical sequence with the live one.

Kept out of :mod:`eawf.observability.doctor.checks` so that module stays
under the EAWF010 line budget; ``checks.run_all`` imports and registers it.

The selected generation's ``state.json`` carries ``canonical_sequence`` as
of its last committed write, while the daemon's cursor lives in the
machine-local status projection beside it. A write that touches only local
status (a Run row, a Run ledger line) moves the cursor without changing a
tracked file, so the two may differ; a caller that reads the committed
value and sends it as the expected sequence is then refused with a
revision conflict. The check names both values so the operator sees the
gap instead of discovering it through a retry.
"""

from __future__ import annotations

import json
from pathlib import Path

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.store.compaction import CANONICAL_SEQUENCE_KEY, read_document
from eawf.observability.doctor.models import CheckResult

CHECK_NAME = "canonical_sequence_drift"


def check_canonical_sequence_drift(*, workspace: Path | None) -> CheckResult:
    """Report whether the committed ``canonical_sequence`` lags the live one.

    Args:
        workspace: The workspace anchor (the ``.ea/`` parent), already
            resolved by the caller; ``None`` when no anchor exists.

    Returns:
        ``ok`` when no anchor or epoch-2 generation exists, when the
        document carries no sequence, or when the two values are equal;
        ``warn`` naming both values when the committed one is behind, and
        ``warn`` naming the document when it cannot be read.
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
        # read_document refuses a committed file that is not a JSON object,
        # so the second decode below always yields one.
        live = read_document(document).get(CANONICAL_SEQUENCE_KEY)
        committed = json.loads(document.read_bytes()).get(CANONICAL_SEQUENCE_KEY)
    except (OSError, ValueError) as exc:
        return CheckResult(
            name=CHECK_NAME,
            status="warn",
            detail=f"generation {authority.generation_id} document is unreadable: {exc}",
        )
    if live is None:
        return CheckResult(name=CHECK_NAME, status="ok", detail="no canonical sequence yet")
    if committed == live:
        return CheckResult(
            name=CHECK_NAME,
            status="ok",
            detail=f"committed canonical_sequence {committed} matches the live {live}",
        )
    return CheckResult(
        name=CHECK_NAME,
        status="warn",
        detail=(
            f"committed canonical_sequence {'absent' if committed is None else committed} "
            f"is behind the live {live}; "
            f"a mutating verb expects {live}"
        ),
    )


__all__ = ["CHECK_NAME", "check_canonical_sequence_drift"]
