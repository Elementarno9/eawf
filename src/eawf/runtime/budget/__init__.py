"""Per-wave token-budget policy + service surface.

Public API:

* :func:`policy.classify` — pure advisory threshold check (warn/block).
* :func:`policy.classify_enforcement` — typed soft/hard enforce verdict
  against the multiplier-scaled cap.
* :func:`policy.effective_cap` — multiplier-scaled cap helper.
* :func:`service.set_budget` — assign a budget to a wave.
* :func:`service.record_consumption` — accumulate tokens and classify.
* :func:`service.check_budget` — read-only classify of the current wave state.
* :func:`service.terminate_with_grace` — SIGTERM -> grace -> SIGKILL
  process-termination ladder used by ``hard`` budget enforcement.

The library is I/O-free apart from the termination ladder's process
signalling; the persistence path lives in the lifecycle CLI
(``eawf wave budget set|consume|show``), which wraps the state-mutating
calls in the canonical state-locked mutation transaction.
"""

from __future__ import annotations

from eawf.runtime.budget.policy import (
    BLOCK_FRACTION,
    DEFAULT_ENFORCE,
    DEFAULT_MULTIPLIER,
    WARN_FRACTION,
    BudgetAction,
    BudgetDecision,
    EnforceMode,
    classify,
    classify_enforcement,
    effective_cap,
)
from eawf.runtime.budget.service import (
    DEFAULT_GRACE_SECONDS,
    TerminableProcess,
    TerminationResult,
    check_budget,
    record_consumption,
    set_budget,
    terminate_with_grace,
)

__all__ = [
    "BLOCK_FRACTION",
    "DEFAULT_ENFORCE",
    "DEFAULT_GRACE_SECONDS",
    "DEFAULT_MULTIPLIER",
    "WARN_FRACTION",
    "BudgetAction",
    "BudgetDecision",
    "EnforceMode",
    "TerminableProcess",
    "TerminationResult",
    "check_budget",
    "classify",
    "classify_enforcement",
    "effective_cap",
    "record_consumption",
    "set_budget",
    "terminate_with_grace",
]
