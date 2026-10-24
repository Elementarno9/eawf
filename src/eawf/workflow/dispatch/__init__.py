"""Subagent dispatch package: routing and spawn retry.

Public API:

- :func:`resolve_routing` — pure ``(agent_role, effort_bucket) ->
  (model, runtime)`` lookup, backed by :data:`DEFAULT_ROUTING_TABLE`.
- :func:`spawn_with_retry` — the bounded spawn-retry loop over the CLI failure
  taxonomy: it classifies a :class:`RuntimeSpawnError` into the V5 ladder action
  (retry-same / switch-runtime / halt), respawns or switches accordingly, and on
  a terminal failure raises :class:`RetryExhaustedError` carrying every attempt
  plus a tiered :class:`FailureNotice` (``transient_retryable`` / ``switched`` /
  ``fatal_halt``).
"""

from __future__ import annotations

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
    "RoutingDecision",
    "SpawnAttemptFailure",
    "failure_tier_for_action",
    "model_for_runtime",
    "resolve_routing",
    "seed_interim_verdict",
    "spawn_with_retry",
    "tier_for_model",
]
