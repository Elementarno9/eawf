"""Per-wave token-budget policy + service surface.

Public API:

* :func:`policy.classify` — pure advisory threshold check (warn/block).
* :func:`policy.classify_enforcement` — typed soft/hard enforce verdict
  against the multiplier-scaled cap.
* :func:`policy.effective_cap` — multiplier-scaled cap helper.
* :class:`policy.BudgetConfig` / :class:`policy.PromptBudgetCeiling` — the
  validated ``flow.budget`` table and the one ceiling it derives.
* :func:`service.load_budget_config` — read that table from layered config.
* :func:`service.set_budget` — assign a budget to a wave.
* :func:`service.record_consumption` — accumulate tokens and classify.
* :func:`service.check_budget` — read-only classify of the current wave state.
* :func:`service.emit_budget_notice` — upsert the scope's non-blocking
  threshold notice (:mod:`eawf.runtime.budget.notices`).
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
    SEALED_BUDGET,
    WARN_FRACTION,
    BudgetAction,
    BudgetConfig,
    BudgetDecision,
    DuplicateCeilingError,
    EnforceMode,
    PromptBudgetCeiling,
    budget_config_from,
    classify,
    classify_enforcement,
    effective_cap,
)
from eawf.runtime.budget.service import (
    DEFAULT_GRACE_SECONDS,
    TerminableProcess,
    TerminationResult,
    check_budget,
    emit_budget_notice,
    load_budget_config,
    record_consumption,
    set_budget,
    terminate_with_grace,
)

__all__ = [
    "BLOCK_FRACTION",
    "DEFAULT_ENFORCE",
    "DEFAULT_GRACE_SECONDS",
    "DEFAULT_MULTIPLIER",
    "SEALED_BUDGET",
    "WARN_FRACTION",
    "BudgetAction",
    "BudgetConfig",
    "BudgetDecision",
    "DuplicateCeilingError",
    "EnforceMode",
    "PromptBudgetCeiling",
    "TerminableProcess",
    "TerminationResult",
    "budget_config_from",
    "check_budget",
    "classify",
    "classify_enforcement",
    "effective_cap",
    "emit_budget_notice",
    "load_budget_config",
    "record_consumption",
    "set_budget",
    "terminate_with_grace",
]
