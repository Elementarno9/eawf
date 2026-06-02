"""Dispatch routing table — ``(agent_role, effort_bucket) -> (model, runtime)``.

A dispatched wave needs two pieces of vendor wiring the renderer does not
itself decide: which *model* prices and runs the session, and which
*runtime* adapter spawns it. This module owns that decision as a pure,
dict-backed lookup so the dispatch path has a single canonical home for
the model / runtime pairing rather than scattering ``if effort == "XL"``
ladders across the renderer and the spawn seam.

The decision keys on the two canonical wave fields:

- :class:`~eawf.kernel.state.enums.AgentSessionRole` (``Wave.agent_role``).
- :class:`~eawf.kernel.state.enums.EffortBucket` (``Wave.effort_bucket``),
  the closed ``XS|S|M|L|XL`` ladder.

:func:`resolve_routing` is a pure function: same inputs, same
:class:`RoutingDecision`, no hidden state and no I/O. It accepts an
optional ``table`` so an operator-supplied ``dispatch.routing`` config
override (the leaf this wave registers) can layer over
:data:`DEFAULT_ROUTING_TABLE` without the resolver re-reading config.

Model tiers follow the cost gradient: heavier effort routes to the more
capable (and costlier) Opus tier, lighter effort routes to the cheaper
Haiku tier, with Sonnet in the middle. A handful of reasoning-heavy roles
(planner, auditor, reviewer) bump one tier up at the mid band so a
medium-effort review still reasons on the stronger model. Every model id
is a key in :data:`eawf.observability.telemetry.pricing.PRICING` so a
routed wave prices through :func:`~eawf.observability.telemetry.pricing.lookup_pricing`.

The ``runtime`` is the short :data:`~eawf.kernel.store.kinds.events.base.RuntimeTriple`
spelling (``claude`` / ``codex`` / ``opencode``) — the same vocabulary
``runtime.preference`` defaults to (``"claude"``) and the telemetry cost
surface keys on — so a routing decision drops straight onto the
configured adapter ladder.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from eawf.kernel.state.enums import AgentSessionRole, EffortBucket

logger = logging.getLogger(__name__)

#: Canonical model ids (keys of
#: :data:`eawf.observability.telemetry.pricing.PRICING`). Named here so the
#: table reads by tier rather than by bare string literal.
_MODEL_OPUS: str = "claude-opus-4-8"
_MODEL_SONNET: str = "claude-sonnet-4-6"
_MODEL_HAIKU: str = "claude-haiku-4-5"

#: Default runtime adapter — the short ``RuntimeTriple`` spelling matching
#: ``runtime.preference``'s built-in default and the telemetry cost
#: surface's ``RuntimeName`` vocabulary.
_DEFAULT_RUNTIME: str = "claude"

#: Ordered tier index (cheapest -> most capable) the per-runtime model maps
#: key on. The claude model a :class:`RoutingDecision` carries pins the tier;
#: :func:`model_for_runtime` maps that tier onto the queried runtime's own
#: vendor model so a codex / opencode juror spawns its OWN vendor's model at
#: the same capability tier rather than a claude id the foreign CLI rejects.
_TIER_INDEX_BY_MODEL: dict[str, int] = {_MODEL_HAIKU: 0, _MODEL_SONNET: 1, _MODEL_OPUS: 2}

#: Per-runtime model id per tier, indexed by :data:`_TIER_INDEX_BY_MODEL`.
#: The short ``RuntimeTriple`` spelling keys the outer map (the same vocabulary
#: :attr:`RoutingDecision.runtime` carries). ``claude`` mirrors the tier ladder
#: verbatim so the claude spawn model is byte-identical to the pre-W15 surface.
#: ``codex`` routes bare OpenAI ids; ``opencode`` routes the ``provider/model``
#: form the opencode CLI ``-m`` flag expects (anthropic provider, so an
#: OAuth-Claude opencode lane prices at the real anthropic rates). Every id is a
#: key the cost ledger prices through
#: :func:`eawf.observability.telemetry.pricing.lookup_pricing`.
_RUNTIME_TIER_MODEL: dict[str, tuple[str, str, str]] = {
    "claude": (_MODEL_HAIKU, _MODEL_SONNET, _MODEL_OPUS),
    "codex": ("gpt-5-mini", "gpt-5", "gpt-5-codex"),
    "opencode": (
        f"anthropic/{_MODEL_HAIKU}",
        f"anthropic/{_MODEL_SONNET}",
        f"anthropic/{_MODEL_OPUS}",
    ),
}


@dataclass(frozen=True)
class RoutingDecision:
    """The model + runtime a dispatched wave resolves to.

    Attributes:
        model: Model identifier — a key of
            :data:`eawf.observability.telemetry.pricing.PRICING` so the
            decision prices through
            :func:`~eawf.observability.telemetry.pricing.lookup_pricing`.
        runtime: Runtime adapter id in the short
            :data:`~eawf.kernel.store.kinds.events.base.RuntimeTriple`
            spelling (``claude`` / ``codex`` / ``opencode``).
    """

    model: str
    runtime: str


#: Per-effort base model tier. The cost gradient: lighter effort → cheaper
#: model, heavier effort → more capable model. Roles that need stronger
#: reasoning at the mid band are bumped one tier in :data:`_TIER_BUMP_ROLES`.
_EFFORT_MODEL: dict[EffortBucket, str] = {
    EffortBucket.XS: _MODEL_HAIKU,
    EffortBucket.S: _MODEL_HAIKU,
    EffortBucket.M: _MODEL_SONNET,
    EffortBucket.L: _MODEL_OPUS,
    EffortBucket.XL: _MODEL_OPUS,
}

#: Ordered tier ladder (cheapest -> most capable). Used to apply a one-step
#: bump for reasoning-heavy roles without hardcoding the bumped pair per
#: role x effort cell.
_TIER_LADDER: tuple[str, ...] = (_MODEL_HAIKU, _MODEL_SONNET, _MODEL_OPUS)

#: Roles whose reasoning load justifies one tier above the effort base.
#: Planner / auditor / reviewer reason over the whole diff or DAG, so even
#: a medium-effort task wants the stronger model.
_TIER_BUMP_ROLES: frozenset[AgentSessionRole] = frozenset(
    {
        AgentSessionRole.PLANNER,
        AgentSessionRole.AUDITOR,
        AgentSessionRole.REVIEWER,
    }
)


def _bump_one_tier(model: str) -> str:
    """Return the next-more-capable tier, or *model* when already at the top."""
    idx = _TIER_LADDER.index(model)
    return _TIER_LADDER[min(idx + 1, len(_TIER_LADDER) - 1)]


def _build_default_table() -> dict[tuple[AgentSessionRole, EffortBucket], RoutingDecision]:
    """Project the effort gradient + role bump into the full role x effort grid.

    Building the full ``8 x 5`` grid up front (rather than computing on each
    :func:`resolve_routing` call) keeps the resolver a flat dict lookup and
    lets a test assert every ``(role, effort)`` pair maps without exercising
    the bump arithmetic at lookup time.

    Returns:
        A mapping covering every :class:`AgentSessionRole` x
        :class:`EffortBucket` pair to its :class:`RoutingDecision`.
    """
    table: dict[tuple[AgentSessionRole, EffortBucket], RoutingDecision] = {}
    for role in AgentSessionRole:
        for effort in EffortBucket:
            model = _EFFORT_MODEL[effort]
            if role in _TIER_BUMP_ROLES:
                model = _bump_one_tier(model)
            table[(role, effort)] = RoutingDecision(model=model, runtime=_DEFAULT_RUNTIME)
    return table


#: Built-in routing table — every ``(agent_role, effort_bucket)`` pair
#: mapped to its :class:`RoutingDecision`. Built once at import so the
#: resolver stays a flat lookup. The ``dispatch.routing`` config leaf
#: layers operator overrides on top of this baseline.
DEFAULT_ROUTING_TABLE: dict[tuple[AgentSessionRole, EffortBucket], RoutingDecision] = (
    _build_default_table()
)

#: Global fallback when neither the supplied override nor the built-in
#: table names a pair (cannot happen for the built-in table, which is
#: total over the enums, but keeps :func:`resolve_routing` non-raising on
#: a sparse operator-supplied ``table``).
_FALLBACK_DECISION: RoutingDecision = RoutingDecision(model=_MODEL_SONNET, runtime=_DEFAULT_RUNTIME)


def resolve_routing(
    agent_role: AgentSessionRole,
    effort_bucket: EffortBucket,
    *,
    table: dict[tuple[AgentSessionRole, EffortBucket], RoutingDecision] | None = None,
) -> RoutingDecision:
    """Resolve the model + runtime for a dispatched wave.

    Pure function — no I/O, no hidden state. The same ``(agent_role,
    effort_bucket)`` always resolves to the same :class:`RoutingDecision`
    for a given ``table``.

    Lookup order:

    1. Exact ``(agent_role, effort_bucket)`` hit in *table* (when an
       override is supplied).
    2. Exact hit in :data:`DEFAULT_ROUTING_TABLE` (total over the enum
       product, so a valid pair always lands here).
    3. :data:`_FALLBACK_DECISION` — only reachable when a sparse override
       *table* is supplied that omits the pair AND the built-in lookup is
       bypassed; retained so the resolver never raises on a valid enum
       pair.

    Args:
        agent_role: The wave's :class:`~eawf.kernel.state.enums.AgentSessionRole`.
        effort_bucket: The wave's
            :class:`~eawf.kernel.state.enums.EffortBucket`.
        table: Optional operator-supplied override map (e.g. projected
            from the ``dispatch.routing`` config leaf). Entries shadow the
            built-in table; absent pairs fall through to the built-in.

    Returns:
        The resolved :class:`RoutingDecision`.
    """
    key = (agent_role, effort_bucket)
    if table is not None and key in table:
        return table[key]
    decision = DEFAULT_ROUTING_TABLE.get(key, _FALLBACK_DECISION)
    logger.debug(
        f"resolve_routing role={agent_role.value} effort={effort_bucket.value} "
        f"model={decision.model!r} runtime={decision.runtime!r}"
    )
    return decision


def model_for_runtime(
    agent_role: AgentSessionRole,
    effort_bucket: EffortBucket,
    runtime: str,
    *,
    table: dict[tuple[AgentSessionRole, EffortBucket], RoutingDecision] | None = None,
) -> str:
    """Resolve the model id for *runtime* at *agent_role* x *effort_bucket*.

    Pure function -- no I/O, no hidden state. Resolves the routing decision
    via :func:`resolve_routing` (so an operator *table* override still wins),
    reads the capability tier off the decision's claude model, then maps that
    tier onto *runtime*'s own vendor model via :data:`_RUNTIME_TIER_MODEL`.

    This is the per-runtime model the live spawn / cross-vendor juror runs
    against: a codex juror gets a bare OpenAI id, an opencode juror gets the
    ``provider/model`` form its CLI ``-m`` flag expects, and a claude spawn gets
    the same claude id :func:`resolve_routing` already returned (byte-identical
    to the pre-W15 surface). Every returned id is a key the cost ledger prices
    through :func:`eawf.observability.telemetry.pricing.lookup_pricing`.

    Args:
        agent_role: The wave's :class:`~eawf.kernel.state.enums.AgentSessionRole`.
        effort_bucket: The wave's
            :class:`~eawf.kernel.state.enums.EffortBucket`.
        runtime: The short ``RuntimeTriple`` spelling (``claude`` / ``codex`` /
            ``opencode``) the spawn runs on.
        table: Optional operator-supplied override map forwarded to
            :func:`resolve_routing`.

    Returns:
        The per-runtime model id for the tier.

    Raises:
        ValueError: When *runtime* is not one of the three short triple
            spellings, or the resolved tier model is not a known tier.
    """
    decision = resolve_routing(agent_role, effort_bucket, table=table)
    tier_models = _RUNTIME_TIER_MODEL.get(runtime)
    if tier_models is None:
        known = ", ".join(sorted(_RUNTIME_TIER_MODEL))
        raise ValueError(f"unknown runtime: {runtime!r} (known: {known})")
    tier = _TIER_INDEX_BY_MODEL.get(decision.model)
    if tier is None:
        # The default table only ever yields the three tier ids, so this is
        # reachable only via a sparse operator override whose model is off the
        # tier ladder; fall back to the runtime's mid (sonnet-equivalent) tier
        # so the resolver still returns a priced row rather than raising.
        tier = _TIER_INDEX_BY_MODEL[_MODEL_SONNET]
    model = tier_models[tier]
    logger.debug(
        f"model_for_runtime role={agent_role.value} effort={effort_bucket.value} "
        f"runtime={runtime!r} model={model!r}"
    )
    return model


__all__ = [
    "DEFAULT_ROUTING_TABLE",
    "RoutingDecision",
    "model_for_runtime",
    "resolve_routing",
]
