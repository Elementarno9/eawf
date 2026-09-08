"""The twelve preflight signals and the probe protocol that produces them.

Two consumers need this vocabulary and neither may import the other: the
sweep in :mod:`eawf.workflow.verify.release_readiness` computes the rows,
and the probes in :mod:`eawf.workflow.release.signal_probes` fill them in.
Declaring the names, statuses, failure codes and probe signature here
keeps that dependency a Y rather than a cycle.

A signal is the coarsest unit a row is reported at. Some gates read a
*component* of a signal rather than the whole row -- the dependency
inventory and the vulnerability report are two distinct checks that both
land on the ``dependencies`` row -- so :data:`SIGNAL_COMPONENTS` closes
the component vocabulary alongside the signal vocabulary. A component
that is not declared here cannot be bound by a gate, which is what stops
a typo in an authored binding from resolving to nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from eawf.kernel.spec.release_config import ReleaseConfig

#: Separator between a signal name and one of its components in a
#: qualified component reference (``dependencies.inventory``).
COMPONENT_SEPARATOR: Final[str] = "."


class ReleaseSignalName(StrEnum):
    """The twelve preflight signals a release readiness object reports.

    The set is train-wide and fixed: it keeps rows that no checkpoint's
    gates can read yet, so a later checkpoint does not have to invent a
    row when its producer lands. A row no checkpoint requires is
    reported ``unavailable``, not omitted.

    Values:
        VERSION_CONSISTENCY: Package, plugin, lock and tag versions agree.
        CHANGELOG: One non-empty section names migrations and limitations.
        ANCESTRY: Source is reachable from the intended remote branch.
        TREE_CLEANLINESS: No untracked or modified release inputs.
        MEMBERSHIP: Every acceptance bundle is complete and exact.
        MIGRATION: Dry-run, apply, rerun and rollback rehearsals pass.
        ARTIFACTS: A clean rebuild reproduces the same digests.
        DEPENDENCIES: Locked, inventoried, permitted, no blocking CVE.
        PLATFORM: Every advertised platform passes its journey.
        PROVIDER: Every advertised runtime passes its conformance.
        PERFECT_REALIZATION: All realization assertions pass unwaived.
        CREDENTIALS: Required handles are available to the daemon.
    """

    VERSION_CONSISTENCY = "version_consistency"
    CHANGELOG = "changelog"
    ANCESTRY = "ancestry"
    TREE_CLEANLINESS = "tree_cleanliness"
    MEMBERSHIP = "membership"
    MIGRATION = "migration"
    ARTIFACTS = "artifacts"
    DEPENDENCIES = "dependencies"
    PLATFORM = "platform"
    PROVIDER = "provider"
    PERFECT_REALIZATION = "perfect_realization"
    CREDENTIALS = "credentials"


class ReleaseSignalStatus(StrEnum):
    """Outcome of one readiness signal.

    Only :attr:`PASS` clears a required row. The other three are
    distinguished because they call for different operator action: fix
    the release, fix the checker, or wait for the producer.

    Values:
        PASS: The signal's passing condition holds.
        FAIL: The signal ran and its condition does not hold.
        BLOCKED: The probe could not produce a verdict (it raised).
        UNAVAILABLE: No producer exists for this signal at this
            checkpoint, by design.
    """

    PASS = "pass"
    FAIL = "fail"
    BLOCKED = "blocked"
    UNAVAILABLE = "unavailable"


class ReleaseSignalFailureCode(StrEnum):
    """The named failure each signal reports when it does not pass.

    Values:
        VERSION_MISMATCH: Version consistency failed.
        CHANGELOG_MISSING: The changelog section is absent or empty.
        SOURCE_NOT_PUBLISHABLE: Ancestry failed.
        DIRTY_RELEASE_TREE: Tree cleanliness failed.
        MEMBERSHIP_UNACCEPTED: A membership bundle is unaccepted.
        MIGRATION_UNPROVEN: A migration rehearsal leg failed.
        ARTIFACT_NONREPRODUCIBLE: Rebuild digests diverged.
        DEPENDENCY_GATE_FAILED: Inventory or vulnerability check failed.
        PLATFORM_CLAIM_UNPROVEN: An advertised platform is unproven.
        PROVIDER_CLAIM_UNPROVEN: An advertised runtime is unproven.
        PERFECT_REALIZATION_FAILED: A realization assertion failed.
        CREDENTIAL_UNAVAILABLE: A required handle is unavailable.
    """

    VERSION_MISMATCH = "version_mismatch"
    CHANGELOG_MISSING = "changelog_missing"
    SOURCE_NOT_PUBLISHABLE = "source_not_publishable"
    DIRTY_RELEASE_TREE = "dirty_release_tree"
    MEMBERSHIP_UNACCEPTED = "membership_unaccepted"
    MIGRATION_UNPROVEN = "migration_unproven"
    ARTIFACT_NONREPRODUCIBLE = "artifact_nonreproducible"
    DEPENDENCY_GATE_FAILED = "dependency_gate_failed"
    PLATFORM_CLAIM_UNPROVEN = "platform_claim_unproven"
    PROVIDER_CLAIM_UNPROVEN = "provider_claim_unproven"
    PERFECT_REALIZATION_FAILED = "perfect_realization_failed"
    CREDENTIAL_UNAVAILABLE = "credential_unavailable"


#: The failure code each signal reports. Total over
#: :class:`ReleaseSignalName` -- a signal with no declared failure code
#: could go red without naming why.
SIGNAL_FAILURE_CODES: Final[Mapping[ReleaseSignalName, ReleaseSignalFailureCode]] = {
    ReleaseSignalName.VERSION_CONSISTENCY: ReleaseSignalFailureCode.VERSION_MISMATCH,
    ReleaseSignalName.CHANGELOG: ReleaseSignalFailureCode.CHANGELOG_MISSING,
    ReleaseSignalName.ANCESTRY: ReleaseSignalFailureCode.SOURCE_NOT_PUBLISHABLE,
    ReleaseSignalName.TREE_CLEANLINESS: ReleaseSignalFailureCode.DIRTY_RELEASE_TREE,
    ReleaseSignalName.MEMBERSHIP: ReleaseSignalFailureCode.MEMBERSHIP_UNACCEPTED,
    ReleaseSignalName.MIGRATION: ReleaseSignalFailureCode.MIGRATION_UNPROVEN,
    ReleaseSignalName.ARTIFACTS: ReleaseSignalFailureCode.ARTIFACT_NONREPRODUCIBLE,
    ReleaseSignalName.DEPENDENCIES: ReleaseSignalFailureCode.DEPENDENCY_GATE_FAILED,
    ReleaseSignalName.PLATFORM: ReleaseSignalFailureCode.PLATFORM_CLAIM_UNPROVEN,
    ReleaseSignalName.PROVIDER: ReleaseSignalFailureCode.PROVIDER_CLAIM_UNPROVEN,
    ReleaseSignalName.PERFECT_REALIZATION: (ReleaseSignalFailureCode.PERFECT_REALIZATION_FAILED),
    ReleaseSignalName.CREDENTIALS: ReleaseSignalFailureCode.CREDENTIAL_UNAVAILABLE,
}


#: Named components a signal row can be read at a finer grain than the
#: whole row. Only the ``dependencies`` row is decomposed today: the
#: lock-derived inventory and the vulnerability report are separate
#: checks with separate owners that share one row, so two gates bind
#: them independently. A signal absent from this map has no components
#: and can only be bound whole.
SIGNAL_COMPONENTS: Final[Mapping[ReleaseSignalName, frozenset[str]]] = {
    ReleaseSignalName.DEPENDENCIES: frozenset({"inventory", "vulnerability"}),
}


def component_ref(signal: ReleaseSignalName, component: str) -> str:
    """Return the qualified ``<signal>.<component>`` reference.

    Args:
        signal: Signal the component belongs to.
        component: Bare component name, e.g. ``inventory``.

    Returns:
        The dotted reference, e.g. ``dependencies.inventory``.
    """
    return f"{signal.value}{COMPONENT_SEPARATOR}{component}"


def declared_components() -> tuple[str, ...]:
    """Return every declared component reference, sorted.

    Returns:
        The qualified references of every component in
        :data:`SIGNAL_COMPONENTS`, e.g.
        ``("dependencies.inventory", "dependencies.vulnerability")``.
    """
    return tuple(
        sorted(
            component_ref(signal, component)
            for signal, components in SIGNAL_COMPONENTS.items()
            for component in components
        )
    )


@dataclass(frozen=True, slots=True)
class ReleaseSignalContext:
    """What one probe is handed when it runs.

    Attributes:
        config: The loaded checkpoint configuration.
        signal: Which signal the probe is being asked for.
        observed_revision: Source revision the sweep was computed
            against, or ``None`` before the checkpoint is pinned.
    """

    config: ReleaseConfig
    signal: ReleaseSignalName
    observed_revision: str | None


@dataclass(frozen=True, slots=True)
class ReleaseSignalOutcome:
    """What one probe returns.

    Attributes:
        status: The signal's verdict.
        remediation: What the operator should do. Mandatory for any
            non-pass verdict; a red signal with no next action is a
            dead end.
        evidence_refs: References backing the verdict.
    """

    status: ReleaseSignalStatus
    remediation: str = ""
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)


#: A probe: given a context, return one signal's outcome. Probes are
#: injected rather than imported so the observation adapters, the
#: dependency inventory producer and the reproducibility build can each
#: land in their own wave without the sweep importing them.
ReleaseSignalProbe = Callable[[ReleaseSignalContext], ReleaseSignalOutcome]


__all__ = [
    "COMPONENT_SEPARATOR",
    "SIGNAL_COMPONENTS",
    "SIGNAL_FAILURE_CODES",
    "ReleaseSignalContext",
    "ReleaseSignalFailureCode",
    "ReleaseSignalName",
    "ReleaseSignalOutcome",
    "ReleaseSignalProbe",
    "ReleaseSignalStatus",
    "component_ref",
    "declared_components",
]
