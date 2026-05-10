"""Per-wave token-budget policy + service surface.

Public API:

* :func:`policy.classify` — pure threshold check.
* :func:`service.set_budget` — assign a budget to a wave.
* :func:`service.record_consumption` — accumulate tokens and classify.
* :func:`service.check_budget` — read-only classify of the current wave state.

The library is I/O-free; the persistence path lives in the lifecycle CLI
(``eawf wave budget set|consume|show``), which wraps these calls in the
canonical state-locked mutation transaction.
"""

from __future__ import annotations

from eawf.budget.policy import BLOCK_FRACTION, WARN_FRACTION, classify
from eawf.budget.service import check_budget, record_consumption, set_budget

__all__ = [
    "BLOCK_FRACTION",
    "WARN_FRACTION",
    "check_budget",
    "classify",
    "record_consumption",
    "set_budget",
]
