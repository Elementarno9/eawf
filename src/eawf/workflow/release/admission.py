"""REL-037: nothing is built for a checkpoint until it was measured.

The ``0.7.0.dev2`` checkpoint carries the epoch-2 importer, the
cross-provider dispatch surface and the daemon under concurrent load.
Each of those three was probed before any of it was designed, and the
probes are promoted as citable
:class:`~eawf.kernel.spec.measured_contract.MeasuredContract` records.
This module is the admission edge that makes the citation *load-bearing*
rather than decorative: opening the dev2 record is refused unless all
three contracts resolve, and the refusal names the one that does not.

The refusal is deliberately per-contract. A single "measured contracts
missing" error would let an operator promote one, retry, and be refused
again with the same opaque message; naming the contract plus the exact
promotion command turns the refusal into an instruction.

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
#: applies to.
CHECKPOINT_MEASURED_CONTRACTS: Final[Mapping[str, tuple[str, ...]]] = {
    "0.7.0.dev2": ("MCT-26081301", "MCT-26081302", "MCT-26081303"),
}

#: The checkpoint-facing name of each contract. Shorter than the
#: contract's own ``surface`` field, which describes the probed system
#: boundary in full; this is the phrase the checkpoint plan uses, so the
#: refusal reads in the operator's vocabulary rather than the probe's.
CONTRACT_LABELS: Final[Mapping[str, str]] = {
    "MCT-26081301": "importer at scale",
    "MCT-26081302": "cross-provider conformance",
    "MCT-26081303": "daemon RPC under concurrent dispatch",
}


def required_contract_ids(version: str) -> tuple[str, ...]:
    """Return the contracts *version* may not be built without.

    Args:
        version: Normalized checkpoint version, e.g. ``0.7.0.dev2``.

    Returns:
        The required contract ids in declaration order; empty for a
        checkpoint the rule does not cover.
    """
    return CHECKPOINT_MEASURED_CONTRACTS.get(version, ())


def assert_measured_contracts(state: State, version: str) -> tuple[Artifact, ...]:
    """Return the promoted contracts backing *version*, or refuse.

    Args:
        state: State the citations resolve against.
        version: Normalized checkpoint version.

    Returns:
        The resolved artifact rows, in declaration order. Empty for a
        checkpoint the admission table does not cover.

    Raises:
        UserError: ``kind="measured_contract_missing"`` when any required
            contract does not resolve. The message names that one
            contract and the command that promotes it.
    """
    resolved: list[Artifact] = []
    for contract_id in required_contract_ids(version):
        try:
            resolved.append(resolve_contract_citation(state, contract_id))
        except UserError as exc:
            label = CONTRACT_LABELS.get(contract_id, contract_id)
            logger.warning(
                f"assert_measured_contracts missing version={version!r} contract_id={contract_id!r}"
            )
            raise UserError(
                f"checkpoint {version} cannot be created: measured contract "
                f"{contract_id} ({label}) is not promoted, so nothing measured backs "
                f"the build; promote it first: eawf artifact promote-contract "
                f"{contract_id}",
                kind="measured_contract_missing",
            ) from exc
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
    "assert_measured_contracts",
    "create_checkpoint_release",
    "required_contract_ids",
]
