"""Token-budget policy + service surface.

Public API:

* :func:`policy.classify` — pure over-budget check.
* :func:`policy.classify_enforcement` — typed soft/hard enforce verdict
  against the multiplier-scaled cap.
* :func:`policy.effective_cap` — multiplier-scaled cap helper.
* :class:`policy.BudgetConfig` / :class:`policy.PromptBudgetCeiling` — the
  validated ``flow.budget`` table and the one ceiling it derives.
* :func:`service.load_budget_config` — read that table from layered config.
* :func:`service.emit_budget_notice` — upsert the scope's non-blocking
  threshold notice (:mod:`eawf.runtime.budget.notices`).
* :func:`service.emit_termination_notice` — fold a Run's budget-termination
  receipt into that same notice ledger.
* :func:`service.terminate_with_grace` — SIGTERM -> grace -> SIGKILL
  process-termination ladder used by ``hard`` budget enforcement.
"""

from __future__ import annotations

from eawf.runtime.budget.policy import (
    BLOCK_FRACTION,
    DEFAULT_ENFORCE,
    DEFAULT_MULTIPLIER,
    SEALED_BUDGET,
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
    emit_budget_notice,
    emit_termination_notice,
    load_budget_config,
    terminate_with_grace,
)

__all__ = [
    "BLOCK_FRACTION",
    "DEFAULT_ENFORCE",
    "DEFAULT_GRACE_SECONDS",
    "DEFAULT_MULTIPLIER",
    "SEALED_BUDGET",
    "BudgetAction",
    "BudgetConfig",
    "BudgetDecision",
    "DuplicateCeilingError",
    "EnforceMode",
    "PromptBudgetCeiling",
    "TerminableProcess",
    "TerminationResult",
    "budget_config_from",
    "classify",
    "classify_enforcement",
    "effective_cap",
    "emit_budget_notice",
    "emit_termination_notice",
    "load_budget_config",
    "terminate_with_grace",
]
