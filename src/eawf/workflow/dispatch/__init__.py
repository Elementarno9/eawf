"""Subagent prompt-rendering package (B025) + dispatch envelope (P10 W03).

Public API:

- :func:`build_subagent_spec` — project a validated
  :class:`~eawf.kernel.state.models.State` snapshot + wave id into a typed
  :class:`~eawf.workflow.agents.specs.models.SubagentSpec` by walking the
  wave → iter → phase → scope chain and collecting attached decisions,
  hypotheses, and recent audits.
- :func:`render_wave_prompt` — build the :class:`SubagentSpec` and
  render it to a self-contained Markdown prompt for one wave.
- :func:`render_dispatch_envelope` — wrap the wave prompt in a typed
  :class:`DispatchEnvelope` for either the ``claude-code`` or
  ``claude-agent-sdk`` runtime. The SDK branch projects
  :attr:`State.mcp_servers` and :attr:`State.mcp_grants` into
  ``mcp_servers`` and ``allowed_tools`` allow-lists.
- :class:`DispatchEnvelope` — frozen dataclass return type for the
  dispatch adapter.
- :func:`resolve_routing` — pure ``(agent_role, effort_bucket) ->
  (model, runtime)`` lookup, backed by :data:`DEFAULT_ROUTING_TABLE`.
- :func:`assist_with_schema` — the bounded re-ask loop that validates a
  spawn's answer text against the forced ``agent_end`` schema into a typed
  :class:`LLMAssistResult`, or raises :class:`LLMAssistError` once the retry
  ceiling is exhausted.
- :func:`spawn_with_retry` — the bounded spawn-retry loop over the CLI failure
  taxonomy: it classifies a :class:`RuntimeSpawnError` into the V5 ladder action
  (retry-same / switch-runtime / halt), respawns or switches accordingly, and on
  a terminal failure raises :class:`RetryExhaustedError` carrying every attempt
  plus a tiered :class:`FailureNotice` (``transient_retryable`` / ``switched`` /
  ``fatal_halt``).

Both renderers are pure functions — no I/O, no logging side-effects
beyond the module-level ``logger``. The CLI handlers in
:mod:`eawf.surfaces.cli.commands.lifecycle` own all stdout / file writes.
"""

from __future__ import annotations

from eawf.workflow.agents.specs.models import RoleTierBudgetError
from eawf.workflow.dispatch.clarity_judge import (
    CLARITY_DESCRIPTION_SURFACE,
    DEFAULT_CLARITY_JUROR_COUNT,
    PASS_DIMENSION_SCORE,
    ClarityBallotFn,
    ClarityJudgeResult,
    build_clarity_judge_prompt,
    clarity_criteria,
    juror_verdict_from_criteria,
    parse_clarity_judge_body,
    rollup_clarity_judges,
)
from eawf.workflow.dispatch.cost_ab import (
    DEFAULT_FLIP_THRESHOLD,
    DEFAULT_PASS_REGRESSION_THRESHOLD,
    MIN_COST_AB_N,
    CostABReport,
    CostABRow,
    CostABStatus,
    CostObservation,
    TierRecommendation,
    VerdictObservation,
    compute_cost_ab,
    recommend_tier,
    summarize_cost_ab,
)
from eawf.workflow.dispatch.llm_assist import (
    DEFAULT_MAX_ATTEMPTS,
    LLMAssistError,
    LLMAssistResult,
    SchemaAttemptFailure,
    assist_with_schema,
)
from eawf.workflow.dispatch.renderer import (
    DISPATCH_RUNTIMES,
    DispatchEnvelope,
    build_role_contract,
    build_subagent_spec,
    render_dispatch_envelope,
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
    "CLARITY_DESCRIPTION_SURFACE",
    "DEFAULT_CLARITY_JUROR_COUNT",
    "DEFAULT_FLIP_THRESHOLD",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_PASS_REGRESSION_THRESHOLD",
    "DEFAULT_ROUTING_TABLE",
    "DISPATCH_RUNTIMES",
    "MIN_COST_AB_N",
    "PASS_DIMENSION_SCORE",
    "TOP_TIER_INDEX",
    "ClarityBallotFn",
    "ClarityJudgeResult",
    "CostABReport",
    "CostABRow",
    "CostABStatus",
    "CostObservation",
    "DispatchEnvelope",
    "FailureNotice",
    "FailureTier",
    "LLMAssistError",
    "LLMAssistResult",
    "RetryExhaustedError",
    "RoleTierBudgetError",
    "RoutingDecision",
    "SchemaAttemptFailure",
    "SpawnAttemptFailure",
    "TierRecommendation",
    "VerdictObservation",
    "assist_with_schema",
    "build_clarity_judge_prompt",
    "build_role_contract",
    "build_subagent_spec",
    "clarity_criteria",
    "compute_cost_ab",
    "failure_tier_for_action",
    "juror_verdict_from_criteria",
    "model_for_runtime",
    "parse_clarity_judge_body",
    "recommend_tier",
    "render_dispatch_envelope",
    "render_wave_prompt",
    "resolve_routing",
    "rollup_clarity_judges",
    "seed_interim_verdict",
    "spawn_with_retry",
    "summarize_cost_ab",
    "tier_for_model",
]
