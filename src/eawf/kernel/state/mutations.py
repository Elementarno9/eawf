"""Mutation discriminated union — the typed payload for ``state.mutate``.

The daemon's :func:`state.mutate` RPC accepts exactly one :class:`Mutation`
per call; the discriminator :attr:`MutationKind` names which lifecycle
transition (or kindred state edit) the daemon should apply. Each kind
maps onto exactly one apply function inside
:mod:`eawf.daemon.methods.state` so the dispatch is closed + auditable.

Every kind in :class:`MutationKind` resolves to a real apply function in
:mod:`eawf.daemon.methods.state` — the wave / phase / iter kinds delegate
to :mod:`eawf.workflow.lifecycle.transitions`; the roadmap kinds map onto the
planner transitions (``plan_wave`` / ``remove_wave_plan`` /
``set_wave_deps`` / ``edit_wave_plan`` / ``archive_phase``); and
``EVENT_APPEND`` is an append-only audit row with no structural state
change.

Per the spike brief (`.ea/local/research/2026-05-19-p24-c02-impl-waves.md`
§4 "W09") this module deliberately uses a **loose discriminated union**:
the per-variant :attr:`MutationBase.params` field carries the kind-
specific dict. Per-variant Pydantic subclasses (one per
:class:`MutationKind`) land in C03-IMPL when the spec catalogue is
fully enumerated; until then the dict shape is contract-tested in the
daemon apply functions, not by the discriminator.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class MutationKind(StrEnum):
    """Closed enumeration of state-mutation kinds.

    Each variant names exactly one CLI verb that mutates ``state.json``;
    the daemon's :func:`state.mutate` apply table maps each kind onto
    the corresponding :mod:`eawf.workflow.lifecycle.transitions` function. Every
    kind now resolves to a real apply function — the roadmap kinds
    (:attr:`ROADMAP_REVISE`, :attr:`ROADMAP_APPLY`, :attr:`ROADMAP_DROP`)
    dispatch to the planner transitions, :attr:`WAVE_RELEASE` un-claims a
    wave back to PENDING, and :attr:`EVENT_APPEND` records an append-only
    audit row with no structural state change.
    """

    WAVE_CLAIM = "wave_claim"
    WAVE_CLOSE = "wave_close"
    WAVE_FAIL = "wave_fail"
    WAVE_RELEASE = "wave_release"
    PHASE_OPEN = "phase_open"
    PHASE_ACTIVATE = "phase_activate"
    PHASE_CLOSE = "phase_close"
    ITER_OPEN = "iter_open"
    ITER_CLOSE = "iter_close"
    EVENT_APPEND = "event_append"
    ROADMAP_REVISE = "roadmap_revise"
    ROADMAP_APPLY = "roadmap_apply"
    ROADMAP_DROP = "roadmap_drop"


class Mutation(BaseModel):
    """One state-mutation payload sent across the daemon RPC boundary.

    Attributes:
        kind: :class:`MutationKind` discriminator; the daemon dispatches
            to a per-kind apply function.
        scope_id: Canonical scope id (wave id, phase id, iter id, etc.)
            this mutation targets. Carried verbatim into the event
            envelope so subscribers can filter by scope without
            decoding ``params``.
        mutation_id: Stable client-side identifier (typically a fresh
            uuid4 hex). Used by the daemon to correlate the in-flight
            mutation across logs + WAL records.
        idempotency_key: Optional caller-supplied key for the V5
            cross-runtime retry path; a repeat call with the same
            key inside the daemon's idempotency window returns the
            cached envelope with ``idempotent_replay=True``.
        params: Kind-specific parameter dict. Loose-typed in W09; the
            daemon apply functions are the contract. Per-variant
            Pydantic subclasses land in C03-IMPL.
    """

    model_config = ConfigDict(extra="forbid")

    kind: MutationKind
    scope_id: str = Field(min_length=1)
    mutation_id: str = Field(min_length=1)
    idempotency_key: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "Mutation",
    "MutationKind",
]
