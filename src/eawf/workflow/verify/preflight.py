"""Shared wave-close pre-flight verifier bundle.

One canonical home for the ORDER of the three pre-apply close checks --
the structural criterion/gate-ref validation, the enforcing close gate
(ordered oracle), and the floor-pack readiness compute -- so every close
path (daemon mutate pipeline today; the lock-split, fleet clean-close,
and daemonless paths that follow) runs the same sequence instead of
re-plaiting it inline.

The bundle is a pure read with respect to persisted state: nothing here
writes ``state.json`` or a store file. The daemon-only concerns (jury
spawn factories, block-authority resolution, high-risk verdict
production) stay behind the injected callables, so this module depends
only on the kernel + verify layers.

The (potentially minutes-long) readiness compute shells out to pytest /
pre-commit / mypy; it is offloaded via :func:`asyncio.to_thread` so an
async caller's event loop is not starved while it runs -- the same
offload the daemon close path used before the extraction.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eawf.kernel.state.models import State
    from eawf.kernel.state.mutations import Mutation
    from eawf.kernel.store.kinds.evidence import EvidenceRecord
    from eawf.workflow.verify.models import CloseReadiness

#: The structural criterion/gate-ref validation seam. Raises on a
#: malformed spec (orphan gate ref, author-set oracle tier); returns
#: ``None`` on the grandfathered common case.
ValidateGateRefs = Callable[["State", "Mutation"], None]

#: The enforcing close-gate seam (ordered oracle). Returns the
#: deterministic-pass evidence rows the caller persists only after the
#: close mutation commits; raises to refuse the close.
EnforceCloseGate = Callable[..., Awaitable[list["EvidenceRecord"]]]

#: The floor-pack readiness seam. Returns ``None`` when no active
#: profile enforces verify; raises to refuse a not-ready close.
ComputeReadiness = Callable[..., "CloseReadiness | None"]


@dataclass(frozen=True)
class ClosePreflight:
    """Result of the wave-close pre-flight verification bundle.

    Attributes:
        evidence: Deterministic-pass evidence rows minted by the
            enforcing close gate. The caller appends them to the
            evidence store only AFTER the close mutation commits, so a
            refused close never leaves a stray pass row behind.
        readiness: The enforcing pre-close readiness view, or ``None``
            when no active profile enforces verify.
    """

    evidence: list[EvidenceRecord]
    readiness: CloseReadiness | None


async def run_close_preflight(
    state: State,
    mutation: Mutation,
    *,
    state_path: Path,
    repo_root: Path,
    validate_gate_refs: ValidateGateRefs,
    enforce_close_gate: EnforceCloseGate,
    compute_readiness: ComputeReadiness,
) -> ClosePreflight:
    """Run the three pre-apply close checks in their canonical order.

    The sequence is load-bearing and byte-identical to the pre-extraction
    daemon close pipeline:

    1. *validate_gate_refs* -- structural criterion/gate referential
       integrity, checked REGARDLESS of ``verify.enforce`` (a malformed
       spec must never reach apply);
    2. *enforce_close_gate* -- the enforcing ordered-oracle gate,
       awaited so a refusal aborts the close before any readiness spend;
    3. *compute_readiness* -- the floor-pack readiness compute, offloaded
       to a worker thread because it shells out to pytest / pre-commit /
       mypy and would otherwise starve the caller's event loop.

    Args:
        state: Loaded, validated state the closing wave is read from.
        mutation: The wave-close mutation naming the wave.
        state_path: Path to ``state.json``; stores resolve under its
            sibling ``store/`` directory.
        repo_root: Repository root anchoring config + gate execution.
        validate_gate_refs: The structural-validation seam.
        enforce_close_gate: The enforcing close-gate seam.
        compute_readiness: The floor-pack readiness seam.

    Returns:
        The :class:`ClosePreflight` bundle: deterministic-pass evidence
        rows plus the readiness view.

    Raises:
        Exception: Whatever the injected seams raise -- the bundle adds
            no handling so each close path keeps its own error surface.
    """
    validate_gate_refs(state, mutation)
    evidence = await enforce_close_gate(
        state,
        mutation,
        state_path=state_path,
        repo_root=repo_root,
    )
    readiness = await asyncio.to_thread(
        compute_readiness,
        state,
        mutation,
        state_path=state_path,
        repo_root=repo_root,
    )
    return ClosePreflight(evidence=evidence, readiness=readiness)


__all__ = [
    "ClosePreflight",
    "ComputeReadiness",
    "EnforceCloseGate",
    "ValidateGateRefs",
    "run_close_preflight",
]
