"""Unit tests for :mod:`eawf.workflow.dispatch.routing`.

Exercises the pure ``(agent_role, effort_bucket) -> (model, runtime)``
routing table and its resolver, plus the ``dispatch.routing`` config leaf
registration.

Coverage:

- Every ``AgentSessionRole`` x ``EffortBucket`` pair maps to a
  :class:`RoutingDecision` (the built-in table is total over the enums).
- A known pair resolves to its expected tier row.
- The effort gradient (lighter -> cheaper, heavier -> costlier) holds for
  a non-bumped role.
- The reasoning-heavy role bump lifts the model one tier at the mid band.
- Every routed model id is one of the canonical pricing-table ids the
  cost ledger prices through.
- The runtime is the short ``RuntimeTriple`` spelling.
- A sparse operator-supplied override table shadows the built-in for a
  named pair and falls through for an unnamed one (non-raising fallback).
- ``RoutingDecision`` is frozen.
- The ``dispatch.routing`` leaf resolves through ``leaf_key_lookup`` with
  its empty-mapping default.
"""

from __future__ import annotations

import dataclasses

import pytest

from eawf.kernel.config.registry import LEAF_KEY_REGISTRY, leaf_key_lookup
from eawf.kernel.state.enums import AgentSessionRole, EffortBucket
from eawf.observability.telemetry.pricing import lookup_pricing
from eawf.workflow.dispatch import (
    DEFAULT_ROUTING_TABLE,
    RoutingDecision,
    model_for_runtime,
    resolve_routing,
)

#: Short RuntimeTriple spelling the routing default targets.
_RUNTIME_TRIPLE = {"claude", "codex", "opencode"}

#: Canonical model ids the routing table emits. These are the pricing-table
#: keys the cost ledger prices through; ``claude-opus-4-8`` lands in the
#: pricing snapshot via a sibling P29-I01 wave, so this test pins the id
#: shape the routing table commits to rather than the (not-yet-landed)
#: pricing row.
_ROUTED_MODELS = {"claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-8"}


# --- Total coverage over the enum product ------------------------------------


def test_default_table_covers_every_role_x_effort_pair() -> None:
    """Every (agent_role, effort_bucket) pair has a built-in row."""
    expected = {(role, effort) for role in AgentSessionRole for effort in EffortBucket}
    assert set(DEFAULT_ROUTING_TABLE) == expected


def test_resolve_routing_maps_every_pair() -> None:
    """resolve_routing returns a decision for every enum pair, no raise."""
    for role in AgentSessionRole:
        for effort in EffortBucket:
            decision = resolve_routing(role, effort)
            assert isinstance(decision, RoutingDecision)
            assert decision.model
            assert decision.runtime in _RUNTIME_TRIPLE


# --- Known-pair rows ----------------------------------------------------------


def test_resolve_routing_known_executor_xs_is_cheap_tier() -> None:
    """A light executor task routes to the cheapest (Haiku) model."""
    decision = resolve_routing(AgentSessionRole.EXECUTOR, EffortBucket.XS)
    assert decision.model == "claude-haiku-4-5"
    assert decision.runtime == "claude"


def test_resolve_routing_known_executor_xl_is_top_tier() -> None:
    """A heavy executor task routes to the most capable (Opus) model."""
    decision = resolve_routing(AgentSessionRole.EXECUTOR, EffortBucket.XL)
    assert decision.model == "claude-opus-4-8"
    assert decision.runtime == "claude"


# --- Effort gradient (non-bumped role) ---------------------------------------


def test_effort_gradient_is_monotone_for_executor() -> None:
    """Heavier effort never routes to a cheaper tier than lighter effort."""
    ladder = ("claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-8")
    ordered = [
        resolve_routing(AgentSessionRole.EXECUTOR, effort).model
        for effort in (
            EffortBucket.XS,
            EffortBucket.S,
            EffortBucket.M,
            EffortBucket.L,
            EffortBucket.XL,
        )
    ]
    tiers = [ladder.index(model) for model in ordered]
    assert tiers == sorted(tiers)


# --- Reasoning-heavy role bump -----------------------------------------------


def test_reviewer_medium_effort_bumps_one_tier_above_executor() -> None:
    """A reasoning-heavy role (reviewer) at M effort outranks the executor."""
    executor = resolve_routing(AgentSessionRole.EXECUTOR, EffortBucket.M)
    reviewer = resolve_routing(AgentSessionRole.REVIEWER, EffortBucket.M)
    assert executor.model == "claude-sonnet-4-6"
    assert reviewer.model == "claude-opus-4-8"


@pytest.mark.parametrize(
    "role",
    [
        AgentSessionRole.PLANNER,
        AgentSessionRole.AUDITOR,
        AgentSessionRole.REVIEWER,
    ],
)
def test_bump_roles_never_route_below_executor(role: AgentSessionRole) -> None:
    """A bumped role's tier is >= the executor's tier at the same effort."""
    ladder = ("claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-8")
    for effort in EffortBucket:
        executor_tier = ladder.index(resolve_routing(AgentSessionRole.EXECUTOR, effort).model)
        role_tier = ladder.index(resolve_routing(role, effort).model)
        assert role_tier >= executor_tier


# --- Every routed model is a canonical pricing-table id ----------------------


def test_every_routed_model_is_a_canonical_id() -> None:
    """Each emitted model is one of the canonical pricing-table ids.

    The cost ledger prices a routed wave through ``lookup_pricing`` on
    these exact ids; the ``claude-opus-4-8`` pricing row lands via a
    sibling P29-I01 wave, so this test pins the id the routing table emits
    rather than the not-yet-present pricing row.
    """
    for decision in DEFAULT_ROUTING_TABLE.values():
        assert decision.model in _ROUTED_MODELS, decision.model


# --- Override table behaviour -------------------------------------------------


def test_override_table_shadows_builtin_for_named_pair() -> None:
    """A supplied override map wins for the pair it names."""
    override = {
        (AgentSessionRole.EXECUTOR, EffortBucket.XS): RoutingDecision(
            model="claude-opus-4-8", runtime="codex"
        )
    }
    decision = resolve_routing(AgentSessionRole.EXECUTOR, EffortBucket.XS, table=override)
    assert decision == RoutingDecision(model="claude-opus-4-8", runtime="codex")


def test_override_table_falls_through_to_builtin_for_unnamed_pair() -> None:
    """An override that omits a pair falls through to the built-in row."""
    override = {
        (AgentSessionRole.EXECUTOR, EffortBucket.XS): RoutingDecision(
            model="claude-opus-4-8", runtime="codex"
        )
    }
    # The override names XS only; XL must still resolve via the built-in.
    decision = resolve_routing(AgentSessionRole.EXECUTOR, EffortBucket.XL, table=override)
    assert decision == DEFAULT_ROUTING_TABLE[(AgentSessionRole.EXECUTOR, EffortBucket.XL)]


def test_empty_override_table_uses_builtin() -> None:
    """An empty override map is a no-op — built-in resolution stands."""
    decision = resolve_routing(AgentSessionRole.PLANNER, EffortBucket.L, table={})
    assert decision == DEFAULT_ROUTING_TABLE[(AgentSessionRole.PLANNER, EffortBucket.L)]


# --- RoutingDecision model contract ------------------------------------------


def test_routing_decision_is_frozen() -> None:
    """RoutingDecision is an immutable dataclass."""
    decision = RoutingDecision(model="claude-opus-4-8", runtime="claude")
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.model = "claude-haiku-4-5"  # type: ignore[misc]


# --- model_for_runtime: per-runtime vendor model resolution ------------------


def test_model_for_runtime_claude_is_byte_identical_to_resolve_routing() -> None:
    """The claude path returns the exact model resolve_routing already emits.

    Cross-vendor model resolution must not perturb the claude lane: every
    (role, effort) pair resolves to the same claude model id as before.
    """
    for role in AgentSessionRole:
        for effort in EffortBucket:
            assert model_for_runtime(role, effort, "claude") == resolve_routing(role, effort).model


def test_model_for_runtime_codex_routes_bare_openai_ids() -> None:
    """A codex spawn resolves its OWN vendor model (bare OpenAI id) per tier.

    The default codex ladder is the set a ChatGPT-account codex serves:
    ``gpt-5.3-codex-spark`` for the cheap tier and the ``gpt-5.5`` flagship for
    the mid + top tiers (the API-key-only ``gpt-5*`` ids that account rejects
    are no longer the default).
    """
    assert (
        model_for_runtime(AgentSessionRole.EXECUTOR, EffortBucket.XS, "codex")
        == "gpt-5.3-codex-spark"
    )
    assert model_for_runtime(AgentSessionRole.EXECUTOR, EffortBucket.M, "codex") == "gpt-5.5"
    assert model_for_runtime(AgentSessionRole.EXECUTOR, EffortBucket.XL, "codex") == "gpt-5.5"


def test_model_for_runtime_opencode_routes_provider_model_form() -> None:
    """An opencode spawn resolves the ``provider/model`` form its CLI expects."""
    assert (
        model_for_runtime(AgentSessionRole.EXECUTOR, EffortBucket.XS, "opencode")
        == "anthropic/claude-haiku-4-5"
    )
    assert (
        model_for_runtime(AgentSessionRole.EXECUTOR, EffortBucket.XL, "opencode")
        == "anthropic/claude-opus-4-8"
    )


def test_model_for_runtime_preserves_tier_across_vendors() -> None:
    """Each runtime resolves its own model at the SAME capability tier.

    Boundary: a reasoning-heavy role at M effort bumps to the top tier; that
    bump must carry across every vendor (opus / gpt-5.5 / anthropic-opus).
    """
    role, effort = AgentSessionRole.REVIEWER, EffortBucket.M
    assert model_for_runtime(role, effort, "claude") == "claude-opus-4-8"
    assert model_for_runtime(role, effort, "codex") == "gpt-5.5"
    assert model_for_runtime(role, effort, "opencode") == "anthropic/claude-opus-4-8"


@pytest.mark.parametrize("runtime", ["claude", "codex", "opencode"])
def test_every_per_runtime_model_is_priced(runtime: str) -> None:
    """Each runtime's per-tier model id resolves to a pricing row (priced).

    The cross-vendor metering floor: a juror spawn on any of the three runtimes
    must price honestly, so every model the resolver can emit is a pricing-table
    key. Covers the whole role x effort grid for the runtime.
    """
    for role in AgentSessionRole:
        for effort in EffortBucket:
            model = model_for_runtime(role, effort, runtime)
            assert lookup_pricing(model) is not None, (runtime, model)


def test_model_for_runtime_unknown_runtime_raises() -> None:
    """Error path: an unknown runtime spelling raises ValueError."""
    with pytest.raises(ValueError, match="unknown runtime"):
        model_for_runtime(AgentSessionRole.EXECUTOR, EffortBucket.M, "gemini")


def test_model_for_runtime_forwards_override_table_tier() -> None:
    """An operator override table re-tiers the resolution for every vendor.

    The override pins XS-executor to the Opus (top) tier; the per-runtime
    resolver must then return each vendor's top-tier model for that pair.
    """
    override = {
        (AgentSessionRole.EXECUTOR, EffortBucket.XS): RoutingDecision(
            model="claude-opus-4-8", runtime="claude"
        )
    }
    assert (
        model_for_runtime(AgentSessionRole.EXECUTOR, EffortBucket.XS, "codex", table=override)
        == "gpt-5.5"
    )
    assert (
        model_for_runtime(AgentSessionRole.EXECUTOR, EffortBucket.XS, "opencode", table=override)
        == "anthropic/claude-opus-4-8"
    )


def test_model_for_runtime_off_tier_override_falls_back_to_mid_tier() -> None:
    """A sparse override whose model is off the tier ladder degrades to mid.

    Error-adjacent boundary: an override carrying a model id the tier index
    does not know resolves to the runtime's mid (sonnet-equivalent) tier rather
    than raising, so the resolver still returns a priced row.
    """
    override = {
        (AgentSessionRole.EXECUTOR, EffortBucket.S): RoutingDecision(
            model="some-unranked-model", runtime="codex"
        )
    }
    model = model_for_runtime(AgentSessionRole.EXECUTOR, EffortBucket.S, "codex", table=override)
    assert model == "gpt-5.5"
    assert lookup_pricing(model) is not None


def test_model_for_runtime_runtime_models_override_wins() -> None:
    """A ``runtime.models`` override swaps the codex ladder for the given ids.

    The per-runtime tier-model override (the ``runtime.models`` config block)
    re-points the codex lane at an API-key account's ids; a runtime absent from
    the override (claude here) still resolves the built-in ladder.
    """
    override = {"codex": ("gpt-5-mini", "gpt-5", "gpt-5-codex")}
    assert (
        model_for_runtime(
            AgentSessionRole.EXECUTOR, EffortBucket.XS, "codex", runtime_models=override
        )
        == "gpt-5-mini"
    )
    assert (
        model_for_runtime(
            AgentSessionRole.EXECUTOR, EffortBucket.XL, "codex", runtime_models=override
        )
        == "gpt-5-codex"
    )
    # A runtime the override omits falls back to the built-in ladder.
    assert (
        model_for_runtime(
            AgentSessionRole.EXECUTOR, EffortBucket.XS, "claude", runtime_models=override
        )
        == "claude-haiku-4-5"
    )


# --- dispatch.routing config leaf --------------------------------------------


def test_dispatch_routing_leaf_registered() -> None:
    """The dispatch.routing leaf is in the catalog registry."""
    assert "dispatch.routing" in LEAF_KEY_REGISTRY


def test_dispatch_routing_leaf_resolves_with_default() -> None:
    """leaf_key_lookup resolves dispatch.routing with its empty-mapping default."""
    entry = leaf_key_lookup("dispatch.routing")
    assert entry.domain == "dispatch"
    assert entry.type == "mapping"
    assert entry.default == {}
