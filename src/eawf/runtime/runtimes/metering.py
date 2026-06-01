"""Metering writer — prices a live :class:`SpawnResult` into a cost row.

Today a dispatch is billed at the cost the *caller* hands the daemon: the
daemon's :func:`eawf.runtime.daemon.dispatch_runner.emit_dispatch_cost` takes a
``cost_usd`` parameter, and nothing upstream derives that figure from the
tokens a spawn actually burned. Left unwired, callers pass ``cost_usd=0``
and the cost ledger reports ``$0`` regardless of real token consumption.

This module closes that gap. :func:`price_spawn_result` is the metering
writer: it takes the transient
:class:`~eawf.runtime.runtimes.adapter.SpawnResult` a live spawn (W21)
returns, prices its token classes through the embedded Decimal pricing
snapshot (W20, :func:`~eawf.observability.telemetry.pricing.lookup_pricing`),
and returns a typed :class:`MeteredCost` carrying the priced ``cost_usd``,
the snapshot ``pricing_version``, the model the cost was priced against,
and the per-class token tallies the ``dispatch_cost`` payload records. The
real, token-derived cost replaces the ``$0`` placeholder so the cost
ledger — and the verdict-hold-rate / cost-A/B surfaces fed from it — read
true spend.

Pricing precision
-----------------

Every rate is a :class:`~decimal.Decimal` in USD *per token* (per the W20
snapshot), so the per-class products and their sum never accumulate the
binary-floating-point drift a ``float`` ledger would over a long horizon.
The two prompt-cache *write* tiers price independently: the spawn result
splits cache-creation into a 5-minute-TTL tally and a 1-hour-TTL tally
(:attr:`SpawnResult.cache_creation_5m_input_tokens` /
:attr:`SpawnResult.cache_creation_1h_input_tokens`), and each is multiplied
by its own rate (``cache_write_5m_per_token`` is ``1.25x`` base input,
``cache_write_1h_per_token`` is ``2x``). The single
``cache_creation_input_tokens`` total the ``dispatch_cost`` payload records
is the *sum* of the two tiers (the payload does not carry the TTL split),
but the **cost** honours the split so a 1-hour write is not under-billed at
the 5-minute rate.

Model resolution
----------------

The cost is priced against :attr:`SpawnResult.resolved_model` when the
runtime disclosed the billed model id, falling back to the *requested*
:attr:`SpawnResult.model` otherwise — so a short alias the caller passed to
``--model`` (e.g. ``opus``) still resolves to a priced row via the
snapshot's longest-prefix alias fallback. When *neither* id matches any
pricing row (a genuinely unknown model), the writer does not raise and does
not silently bill ``$0`` as if priced: it returns a :class:`MeteredCost`
with ``cost_usd == 0`` and ``priced is False`` and logs a ``WARNING``, so an
unpriceable spawn is observable (distinct from a real zero-token spawn,
which is ``priced is True`` with a genuine ``$0`` cost) and a follow-up can
add the missing rate row rather than the gap hiding in the ledger.

Session correlation
--------------------

The W02 spike pinned that the ``dispatch_cost`` event payload
(:class:`~eawf.kernel.store.kinds.events.dispatch_cost.DispatchCostPayload`)
carries **no** ``session_id`` field — its correlation keys are ``wave_id``
plus a per-dispatch ``attempt_id`` UUID, and that ``attempt_id`` is *not*
written back into :attr:`eawf.kernel.state.models.SessionAttempt.session_id`.
This writer does not invent a 1:1 ``session_id`` join that the schema does
not support: :func:`meter_and_emit` carries the session cost forward under
the same ``wave_id`` + ``attempt_id`` correlation the spike ratified and the
daemon's emit path already keys on. The runtime-disclosed
:attr:`SpawnResult.session_id` is surfaced on :class:`MeteredCost` for the
caller's own logging / trace correlation, but it deliberately does not feed
the emitted payload's keys.

Layering
--------

The writer lives in the runtime-adapter layer and prices a
:class:`SpawnResult` (an adapter type) through the telemetry pricing
snapshot; it does **not** import the daemon. :func:`meter_and_emit` accepts
the daemon's emit step as an injected ``emit`` callback so the daemon wires
the real, token-derived cost into
:func:`eawf.runtime.daemon.dispatch_runner.emit_dispatch_cost` without this
module taking an upward dependency on the daemon (which already imports the
adapter layer — a static import here would invert that edge).
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict, Field

from eawf.observability.telemetry.pricing import PRICING_VERSION, ModelPricing, lookup_pricing

if TYPE_CHECKING:
    from eawf.runtime.runtimes.adapter import SpawnResult

logger = logging.getLogger(__name__)

__all__ = [
    "DispatchCostEmitter",
    "MeteredCost",
    "meter_and_emit",
    "price_spawn_result",
]


class MeteredCost(BaseModel):
    """Priced outcome of metering one :class:`SpawnResult`.

    The typed product of :func:`price_spawn_result`: the token-derived
    ``cost_usd`` plus the per-class token tallies + the pinning
    ``pricing_version`` the ``dispatch_cost`` event records. Frozen — a
    metered cost is an immutable fact about a completed spawn.

    Attributes:
        session_id: Runtime-disclosed session id from the spawn. Surfaced
            for the caller's trace correlation only; it does **not** feed
            the emitted ``dispatch_cost`` payload keys (the W02 spike: the
            payload keys on ``wave_id`` + ``attempt_id``, never a
            ``session_id`` join).
        model: Model id the cost was priced against (``resolved_model`` when
            the runtime disclosed it, else the requested ``model``). Matches
            the ``model`` the emitted payload records.
        input_tokens: Non-cached input tokens billed this spawn.
        output_tokens: Output tokens billed this spawn.
        cache_creation_input_tokens: Total prompt-cache write tokens across
            both TTL tiers (the figure the payload records). The *cost*
            honours the per-tier 5m / 1h split; this scalar does not.
        cache_read_input_tokens: Prompt-cache read tokens billed this spawn.
        cost_usd: Token-derived cost in USD (exact :class:`~decimal.Decimal`).
            ``Decimal("0")`` for a genuine zero-token spawn (``priced`` is
            ``True``) or an unpriceable model (``priced`` is ``False``).
        pricing_version: ``PRICING`` snapshot tag the cost was priced under.
        priced: ``True`` when a pricing row resolved for ``model`` and the
            cost is a real token-derived figure (including a genuine ``$0``
            for a zero-token spawn). ``False`` when no row matched and the
            ``$0`` is an unpriced fallback, not a billed zero.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    model: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_creation_input_tokens: int = Field(ge=0)
    cache_read_input_tokens: int = Field(ge=0)
    cost_usd: Decimal = Field(ge=0)
    pricing_version: str = Field(min_length=1)
    priced: bool


def _price_tokens(result: SpawnResult, pricing: ModelPricing) -> Decimal:
    """Sum the per-class token costs for *result* under *pricing*.

    Each token class is multiplied by its own per-token rate and the
    products are summed as :class:`~decimal.Decimal` so no float drift
    enters the ledger. The two cache-write TTL tiers price independently
    (5-minute vs 1-hour rate); the non-cached input, output, and cache-read
    classes price against their single rates.

    Args:
        result: The completed spawn whose token tallies are priced.
        pricing: The resolved per-token rate row for the spawn's model.

    Returns:
        The exact token-derived cost in USD.
    """
    return (
        result.input_tokens * pricing.input_per_token
        + result.output_tokens * pricing.output_per_token
        + result.cache_creation_5m_input_tokens * pricing.cache_write_5m_per_token
        + result.cache_creation_1h_input_tokens * pricing.cache_write_1h_per_token
        + result.cache_read_input_tokens * pricing.cache_read_per_token
    )


def price_spawn_result(result: SpawnResult) -> MeteredCost:
    """Price a completed :class:`SpawnResult` into a :class:`MeteredCost`.

    The metering writer. Resolves the pricing row for
    ``result.resolved_model or result.model`` via
    :func:`~eawf.observability.telemetry.pricing.lookup_pricing` (exact match,
    then longest-prefix alias fallback), then sums the per-class token costs
    (:func:`_price_tokens`) into an exact :class:`~decimal.Decimal`
    ``cost_usd``. The returned :class:`MeteredCost` carries the real,
    token-derived cost the ``dispatch_cost`` event records in place of the
    ``$0`` placeholder.

    A genuine zero-token spawn prices to ``Decimal("0")`` with ``priced`` set
    (a real billed zero). A model that matches no pricing row prices to
    ``Decimal("0")`` with ``priced`` cleared and a logged ``WARNING`` — the
    cost cannot be derived, but the writer neither raises nor pretends the
    ``$0`` is billed.

    Args:
        result: The transient outcome of one live runtime spawn (W21).

    Returns:
        The priced :class:`MeteredCost` for the spawn.
    """
    model = result.resolved_model or result.model
    pricing = lookup_pricing(model)
    cache_creation_total = (
        result.cache_creation_5m_input_tokens + result.cache_creation_1h_input_tokens
    )
    if pricing is None:
        logger.warning(
            f"price_spawn_result model={model!r} runtime={result.runtime!r} "
            f"session={result.session_id!r} pricing=unresolved cost_usd=0 priced=false"
        )
        return MeteredCost(
            session_id=result.session_id,
            model=model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cache_creation_input_tokens=cache_creation_total,
            cache_read_input_tokens=result.cache_read_input_tokens,
            cost_usd=Decimal("0"),
            pricing_version=PRICING_VERSION,
            priced=False,
        )
    cost_usd = _price_tokens(result, pricing)
    logger.info(
        f"price_spawn_result model={model!r} runtime={result.runtime!r} "
        f"session={result.session_id!r} cost_usd={cost_usd} "
        f"pricing_version={pricing.pricing_version}"
    )
    return MeteredCost(
        session_id=result.session_id,
        model=model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cache_creation_input_tokens=cache_creation_total,
        cache_read_input_tokens=result.cache_read_input_tokens,
        cost_usd=cost_usd,
        pricing_version=pricing.pricing_version,
        priced=True,
    )


class DispatchCostEmitter(Protocol):
    """The emit step :func:`meter_and_emit` drives with a priced cost.

    A flattened, daemon-free keyword surface for persisting one
    ``dispatch_cost`` event, declared here so this adapter-layer module
    drives emission through an injected callback rather than importing the
    daemon (which already imports the adapter layer — a static import here
    would invert that edge).

    The daemon adapts its
    :func:`eawf.runtime.daemon.dispatch_runner.emit_dispatch_cost` onto this
    surface with a thin closure that binds the
    :class:`~eawf.runtime.daemon.methods.MethodContext` and folds the four
    token kwargs into a ``DispatchTokens`` (a daemon type this module does
    not depend on); a test passes a recording stub. Token classes are
    flattened (not a ``DispatchTokens`` object) precisely so the adapter
    layer needs no daemon import.

    Returns the id of the appended ``dispatch_cost`` envelope.
    """

    def __call__(
        self,
        *,
        wave_id: str | None,
        attempt_id: str | None,
        runtime: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_creation_input_tokens: int,
        cache_read_input_tokens: int,
        cost_usd: Decimal,
        pricing_version: str,
    ) -> str: ...


def meter_and_emit(
    result: SpawnResult,
    *,
    wave_id: str | None,
    attempt_id: str | None,
    emit: DispatchCostEmitter,
) -> MeteredCost:
    """Price *result* and emit its ``dispatch_cost`` with the real cost.

    Bridges the metering writer to the emit seam: prices *result* via
    :func:`price_spawn_result`, then calls *emit* with the token-derived
    ``cost_usd`` + ``pricing_version`` so the persisted ``dispatch_cost``
    event carries the real spend rather than a ``$0`` placeholder. The
    correlation keys are the dispatch's ``wave_id`` + ``attempt_id`` — the
    pairing the W02 spike ratified — *not* the runtime ``session_id`` (which
    the payload does not carry and the daemon never reconciles to the
    attempt id).

    *emit* is injected (the daemon's
    :func:`~eawf.runtime.daemon.dispatch_runner.emit_dispatch_cost`) so this
    module stays in the adapter layer without an upward daemon import.

    Args:
        result: The completed spawn to meter.
        wave_id: ``W<NN>`` wave the dispatch served, or ``None`` for an
            interactive (non-wave) session.
        attempt_id: The serving attempt's id (a per-dispatch UUID), or
            ``None`` for an interactive session. This — with *wave_id* — is
            the correlation the emitted payload keys on.
        emit: The dispatch-cost emit step to drive with the priced cost.

    Returns:
        The :class:`MeteredCost` priced for *result* (after *emit* has
        persisted the event).
    """
    metered = price_spawn_result(result)
    envelope_id = emit(
        wave_id=wave_id,
        attempt_id=attempt_id,
        runtime=result.runtime,
        model=metered.model,
        input_tokens=metered.input_tokens,
        output_tokens=metered.output_tokens,
        cache_creation_input_tokens=metered.cache_creation_input_tokens,
        cache_read_input_tokens=metered.cache_read_input_tokens,
        cost_usd=metered.cost_usd,
        pricing_version=metered.pricing_version,
    )
    logger.info(
        f"meter_and_emit wave={wave_id} attempt={attempt_id} "
        f"runtime={result.runtime!r} cost_usd={metered.cost_usd} "
        f"priced={metered.priced} envelope_id={envelope_id!r}"
    )
    return metered
