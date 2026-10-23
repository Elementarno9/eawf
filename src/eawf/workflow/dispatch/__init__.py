"""Subagent prompt-rendering package.

Public API:

- :func:`build_subagent_spec` — project a validated
  :class:`~eawf.kernel.migration.v1_state.State` snapshot + wave id into a typed
  :class:`~eawf.workflow.agents.specs.models.SubagentSpec` by walking the
  wave → iter → phase → scope chain and collecting attached decisions,
  hypotheses, and recent audits.
- :func:`render_wave_prompt` — build the :class:`SubagentSpec` and
  render it to a self-contained Markdown prompt for one wave.
- :func:`resolve_routing` — pure ``(agent_role, effort_bucket) ->
  (model, runtime)`` lookup, backed by :data:`DEFAULT_ROUTING_TABLE`.
- :func:`spawn_with_retry` — the bounded spawn-retry loop over the CLI failure
  taxonomy: it classifies a :class:`RuntimeSpawnError` into the V5 ladder action
  (retry-same / switch-runtime / halt), respawns or switches accordingly, and on
  a terminal failure raises :class:`RetryExhaustedError` carrying every attempt
  plus a tiered :class:`FailureNotice` (``transient_retryable`` / ``switched`` /
  ``fatal_halt``).

The renderer is a pure function — no I/O, no logging side-effects
beyond the module-level ``logger``. The CLI handlers in
:mod:`eawf.surfaces.cli.commands.lifecycle` own all stdout / file writes.
"""

from __future__ import annotations

from eawf.workflow.agents.specs.models import RoleTierBudgetError
from eawf.workflow.dispatch.renderer import (
    build_role_contract,
    build_subagent_spec,
    render_wave_prompt,
)
from eawf.workflow.dispatch.retry import (
    FailureNotice,
    FailureTier,
    RetryExhaustedError,
    SpawnAttemptFailure,
    failure_tier_for_action,
    spawn_with_retry,
)
from eawf.workflow.dispatch.routing import (
    DEFAULT_ROUTING_TABLE,
    TOP_TIER_INDEX,
    RoutingDecision,
    model_for_runtime,
    resolve_routing,
    tier_for_model,
)
from eawf.workflow.dispatch.seed import seed_interim_verdict

__all__ = [
    "DEFAULT_ROUTING_TABLE",
    "TOP_TIER_INDEX",
    "FailureNotice",
    "FailureTier",
    "RetryExhaustedError",
    "RoleTierBudgetError",
    "RoutingDecision",
    "SpawnAttemptFailure",
    "build_role_contract",
    "build_subagent_spec",
    "failure_tier_for_action",
    "model_for_runtime",
    "render_wave_prompt",
    "resolve_routing",
    "seed_interim_verdict",
    "spawn_with_retry",
    "tier_for_model",
]
