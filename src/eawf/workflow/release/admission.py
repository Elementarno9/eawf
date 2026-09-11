"""REL-037: nothing is built for a checkpoint until it was measured.

Three surfaces of the v0.7 train were probed before any of them was
designed -- the epoch-2 importer, the cross-provider dispatch surface,
and the daemon's RPC under concurrent dispatch -- and each probe is
promoted as a citable
:class:`~eawf.kernel.spec.measured_contract.MeasuredContract` record.
This module is the admission edge that makes the citation *load-bearing*
rather than decorative: opening a checkpoint record is refused unless
its contracts resolve, and the refusal names each one that does not.

Each leg is assigned to the rung that builds the surface it measures.
``dev2`` builds the importer, so it asserts over the importer contract
alone. ``dev3`` is the first rung that dispatches natively in parallel
and the first with a projection seam over the daemon's RPC, so the other
two legs land there. Demanding all three a rung early would have blocked
``dev2`` on evidence it never reads, which teaches an operator that the
table is ceremony.

The refusal is deliberately per-contract and exhaustive. A single
"measured contracts missing" error would let an operator promote one,
retry, and be refused again with the same opaque message; naming every
missing contract plus the exact promotion command turns the refusal into
an instruction.

:data:`CHECKPOINT_MEASURED_CONTRACTS` is the admission table. A version
absent from it requires nothing, which is the honest reading for the
``dev1`` rung: it predates the measured-before-build rule and has no
probes to cite.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Final
from uuid import UUID

from eawf.kernel.spec.release import Release, ReleaseTrain, validate_release_against_train
from eawf.kernel.state.models import Artifact, State
from eawf.surfaces.cli.errors import UserError
from eawf.workflow.evidence.measured_contract import resolve_contract_citation
from eawf.workflow.release.advance import draft_release_for

logger = logging.getLogger(__name__)

#: The measured contracts each checkpoint may not be built without,
#: keyed by normalized version. ``dev2`` is the first rung the rule
#: applies to, and it carries the importer leg alone: the importer is
#: what ``dev2`` builds, so it is the one surface ``dev2`` asserts over.
#: The other two legs land at ``dev3``, which is the first rung that
#: dispatches natively in parallel and the first with a projection seam
#: over the daemon's RPC -- so those are the contracts it may not be
#: built without, and demanding them a rung early would have blocked
#: ``dev2`` on evidence it never uses.
CHECKPOINT_MEASURED_CONTRACTS: Final[Mapping[str, tuple[str, ...]]] = {
    "0.7.0.dev2": ("MCT-26091101",),
    "0.7.0.dev3": ("MCT-26081302", "MCT-26081303"),
}

#: The checkpoint-facing name of each contract. Shorter than the
#: contract's own ``surface`` field, which describes the probed system
#: boundary in full; this is the phrase the checkpoint plan uses, so the
#: refusal reads in the operator's vocabulary rather than the probe's.
CONTRACT_LABELS: Final[Mapping[str, str]] = {
    "MCT-26081301": "importer at fixture scale",
    "MCT-26081302": "cross-provider conformance",
    "MCT-26081303": "daemon RPC under concurrent dispatch",
    "MCT-26091101": "importer over the production corpus",
}

#: The CLI verb that promotes a measured contract. Quoted verbatim in
#: every refusal so the operator's next action is a command rather than
#: a description of one.
PROMOTION_COMMAND: Final[str] = "eawf artifact promote-contract"


def required_contract_ids(version: str) -> tuple[str, ...]:
    """Return the contracts *version* may not be built without.

    Args:
        version: Normalized checkpoint version, e.g. ``0.7.0.dev2``.

    Returns:
        The required contract ids in declaration order; empty for a
        checkpoint the rule does not cover.
    """
    return CHECKPOINT_MEASURED_CONTRACTS.get(version, ())


def _missing_clause(contract_id: str) -> str:
    """Return the refusal clause naming one unpromoted contract.

    Args:
        contract_id: The contract that did not resolve.

    Returns:
        The id, its checkpoint-facing label, and the exact command that
        promotes it.
    """
    label = CONTRACT_LABELS.get(contract_id, contract_id)
    return f"{contract_id} ({label}), promote with: {PROMOTION_COMMAND} {contract_id}"


def assert_measured_contracts(state: State, version: str) -> tuple[Artifact, ...]:
    """Return the promoted contracts backing *version*, or refuse.

    Every required contract is resolved before the refusal is raised, so
    the message names *all* of them rather than the first. A rung that
    asserts over two surfaces would otherwise send the operator back to
    the same wall after each partial fix -- which is exactly the
    round-trip the per-contract naming exists to remove.

    Args:
        state: State the citations resolve against.
        version: Normalized checkpoint version.

    Returns:
        The resolved artifact rows, in declaration order. Empty for a
        checkpoint the admission table does not cover.

    Raises:
        UserError: ``kind="measured_contract_missing"`` when any required
            contract does not resolve. The message names every missing
            contract and the command that promotes each; the ones that
            did resolve stay out of it.
    """
    required = required_contract_ids(version)
    resolved: list[Artifact] = []
    missing: list[str] = []
    for contract_id in required:
        try:
            resolved.append(resolve_contract_citation(state, contract_id))
        except UserError:
            missing.append(contract_id)
    if missing:
        logger.warning(
            f"assert_measured_contracts missing version={version!r} "
            f"contract_ids={missing} required={len(required)}"
        )
        raise UserError(
            f"checkpoint {version} cannot be created: {len(missing)} of {len(required)} "
            f"measured contract(s) are not promoted, so nothing measured backs the "
            f"build -- {'; '.join(_missing_clause(row) for row in missing)}",
            kind="measured_contract_missing",
        )
    logger.debug(f"assert_measured_contracts version={version!r} contracts={len(resolved)}")
    return tuple(resolved)


def create_checkpoint_release(
    state: State,
    *,
    train: ReleaseTrain,
    version: str,
    uid: UUID,
    membership_refs: Sequence[str] = (),
) -> Release:
    """Open the DRAFT record of checkpoint *version*, after admission.

    Admission runs before the record is built, not after: a refused
    checkpoint leaves no half-created record behind for a later caller to
    find and mistake for an admitted one.

    Args:
        state: State the measured-contract citations resolve against.
        train: Train that declares the rung.
        version: Normalized checkpoint version to open.
        uid: Identity for the new record.
        membership_refs: Milestone acceptance bundles, for the rungs that
            require them.

    Returns:
        The DRAFT :class:`~eawf.kernel.spec.release.Release`.

    Raises:
        UserError: ``kind="measured_contract_missing"`` when a required
            measured contract is not promoted.
        KeyError: When *train* declares no rung for *version*.
        ValueError: When *version* is not a train version, or the rung
            requires membership bundles the caller did not supply.
        ValidationError: When the rung forbids the supplied membership
            bundles.
    """
    rung = train.checkpoint_for_version(version)
    assert_measured_contracts(state, version)
    record = draft_release_for(rung, uid=uid, membership_refs=membership_refs)
    validate_release_against_train(record, train)
    logger.info(
        f"create_checkpoint_release key={record.key!r} train_id={train.train_id!r} "
        f"contracts={len(required_contract_ids(version))}"
    )
    return record


__all__ = [
    "CHECKPOINT_MEASURED_CONTRACTS",
    "CONTRACT_LABELS",
    "PROMOTION_COMMAND",
    "assert_measured_contracts",
    "create_checkpoint_release",
    "required_contract_ids",
]
