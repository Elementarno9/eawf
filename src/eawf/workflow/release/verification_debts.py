"""The durable collection of verification debts a release approval reads.

A fast delivery binding may defer a gate, and the deferred gate is owed
as a :class:`~eawf.kernel.state.epoch2.regime.VerificationDebt` until it
runs and passes. Stable release approval is where that debt is called
in, so the approval reads every recorded debt from this collection
rather than trusting a caller to say none is open.

A debt changes status by successor record, so the collection keeps one
row per revision under the debt key and reads back the newest row of
each key, which is the debt's current state.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import ValidationError

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.epoch2.regime import VerificationDebt
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.paths import store_path

logger = logging.getLogger(__name__)


def read_verification_debts(state_path: Path) -> tuple[VerificationDebt, ...]:
    """Return the current revision of every recorded verification debt.

    Args:
        state_path: Path to ``state.json``.

    Returns:
        The newest row of each debt key, in first-recorded order; empty
        when nothing has been recorded.

    Raises:
        ValueError: When a line is not an envelope of this kind or does
            not carry a debt. A corrupt collection refuses rather than
            skips, because a skipped open debt is an approval it should
            have blocked.
    """
    path = store_path(state_path, StoreKind.VERIFICATION_DEBT)
    if not path.exists():
        return ()
    latest: dict[str, VerificationDebt] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            envelope = Envelope.model_validate_json(line)
            if envelope.kind is not StoreKind.VERIFICATION_DEBT:
                raise ValueError(f"filed under {envelope.kind.value!r}")
            debt = VerificationDebt.model_validate(envelope.payload)
        except (ValidationError, ValueError) as exc:
            raise ValueError(
                f"verification debt collection {path} line {number} is not a debt: {exc}"
            ) from exc
        latest[debt.key] = debt
    logger.debug(f"read_verification_debts path={path} debts={len(latest)}")
    return tuple(latest.values())


__all__ = ["read_verification_debts"]
