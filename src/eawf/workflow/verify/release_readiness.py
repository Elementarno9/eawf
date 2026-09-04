"""Release preflight readiness: twelve signals, computed without fail-fast.

Close readiness (:mod:`eawf.workflow.verify.readiness`) answers whether
one scope may close. This module answers the release-shaped version of
the same question: is this checkpoint publishable, and if not, what
exactly is wrong with it.

The load-bearing property is that :func:`compute_readiness` **never
fail-fasts**. Every one of the twelve signals gets a row on every run,
even when the first one is red, because an operator repairing a release
needs the whole picture in one pass rather than one failure per
round-trip. A probe that raises becomes a ``blocked`` row naming the
exception; a signal with no probe registered becomes an ``unavailable``
row naming the producer that has not landed yet. Neither aborts the
sweep.

The *required* subset is derived, never authored twice: it is exactly
the rows the checkpoint's ``gates.required`` list binds, plus the rows
the three configuration flags imply (tree cleanliness under
``require_clean_tree``, ancestry under ``require_ancestor_of_remote``,
credentials whenever any target is required). A second authored list
would be free to drift from the first, so there is no such list.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import ConfigDict, Field, model_validator

from eawf.kernel.spec.common import _StrictModel
from eawf.kernel.spec.release import (
    NormalizedVersionStr,
    ReferenceStr,
    ReleaseChannel,
    ReleaseGateProfile,
    ReleaseKeyStr,
)
from eawf.kernel.spec.release_config import ReleaseConfig, ReleaseGateName

logger = logging.getLogger(__name__)

#: Freshness window a readiness row is valid for when the caller does
#: not pin one. One hour is short enough that an approval cannot quote a
#: signal computed against yesterday's tree.
DEFAULT_SIGNAL_TTL_SECONDS: Final[int] = 3600


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


#: The readiness row each declared gate name reads. Total over
#: :class:`ReleaseGateName`; ``None`` marks a gate that reads a proof
#: command run at the pinned revision rather than a signal row, so it
#: contributes no row to the derived required set.
GATE_SIGNAL_BINDINGS: Final[Mapping[ReleaseGateName, ReleaseSignalName | None]] = {
    ReleaseGateName.VERSION_CONSISTENCY: ReleaseSignalName.VERSION_CONSISTENCY,
    ReleaseGateName.CHANGELOG_ENTRY: ReleaseSignalName.CHANGELOG,
    ReleaseGateName.DEPENDENCY_INVENTORY: ReleaseSignalName.DEPENDENCIES,
    ReleaseGateName.SECURITY_REVIEW: ReleaseSignalName.DEPENDENCIES,
    ReleaseGateName.ARTIFACT_REPRODUCIBILITY: ReleaseSignalName.ARTIFACTS,
    ReleaseGateName.EPOCH1_STABILIZATION: None,
    ReleaseGateName.TELEMETRY_PRODUCER: None,
    ReleaseGateName.FRONT_DOOR_JOURNEY: None,
}


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
#: land in their own wave without this module importing them.
ReleaseSignalProbe = Callable[[ReleaseSignalContext], ReleaseSignalOutcome]


class ReleaseSignalRow(_StrictModel):
    """One signal's row in a :class:`ReleaseReadiness` object.

    Attributes:
        signal: Which signal this row reports.
        status: Its verdict.
        failure_code: The named failure; present exactly when the
            verdict is not ``pass``.
        observed_revision: Source revision the row was computed against.
        evidence_refs: References backing the verdict.
        computed_at: When the row was computed (timezone-aware UTC).
        expires_at: When the row goes stale; strictly after
            :attr:`computed_at`.
        remediation: Operator next action; non-empty for any non-pass
            verdict.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    signal: ReleaseSignalName
    status: ReleaseSignalStatus
    failure_code: ReleaseSignalFailureCode | None = None
    observed_revision: str | None = None
    evidence_refs: tuple[ReferenceStr, ...] = ()
    computed_at: datetime
    expires_at: datetime
    remediation: str = ""

    @model_validator(mode="after")
    def _row_is_coherent(self) -> ReleaseSignalRow:
        """Reject a row whose verdict, code, window or remediation disagree.

        Raises:
            ValueError: When a passing row carries a failure code, a
                non-passing row carries none or no remediation, the
                failure code is not the one declared for the signal, or
                the freshness window does not move forward.
        """
        expected = SIGNAL_FAILURE_CODES[self.signal]
        if self.status is ReleaseSignalStatus.PASS:
            if self.failure_code is not None:
                raise ValueError(
                    f"signal {self.signal.value!r} passed but carries failure_code "
                    f"{self.failure_code.value!r}"
                )
        else:
            if self.failure_code is not expected:
                raise ValueError(
                    f"signal {self.signal.value!r} must report {expected.value!r} when not passing"
                )
            if not self.remediation.strip():
                raise ValueError(
                    f"signal {self.signal.value!r} is {self.status.value!r} and "
                    f"must carry remediation"
                )
        if self.expires_at <= self.computed_at:
            raise ValueError(f"signal {self.signal.value!r} expires_at must be after computed_at")
        return self


class ReleaseReadiness(_StrictModel):
    """The full preflight verdict for one checkpoint.

    Attributes:
        schema_version: Record schema tag.
        release_key: ``REL-<version>`` the sweep was computed for.
        version: Normalized version.
        channel: Channel derived from the version.
        gate_profile: Profile the checkpoint ran under.
        signals: Exactly one row per :class:`ReleaseSignalName`, in
            declaration order.
        required_signals: The derived required subset. Never authored;
            see :func:`derive_required_signals`.
        waiver_count: Gate waivers recorded against this checkpoint.
            A field beside the signals, never a thirteenth signal row.
        computed_at: When the sweep ran.
        ready: Whether the checkpoint may be approved -- every required
            row passes and no waiver is outstanding.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["release-readiness/v1"] = "release-readiness/v1"
    release_key: ReleaseKeyStr
    version: NormalizedVersionStr
    channel: ReleaseChannel
    gate_profile: ReleaseGateProfile
    signals: Annotated[tuple[ReleaseSignalRow, ...], Field(min_length=1)]
    required_signals: tuple[ReleaseSignalName, ...] = ()
    waiver_count: Annotated[int, Field(ge=0)] = 0
    computed_at: datetime
    ready: bool

    @model_validator(mode="after")
    def _sweep_is_total_and_honest(self) -> ReleaseReadiness:
        """Reject a partial sweep, an undeclared requirement or a lying verdict.

        Raises:
            ValueError: When the rows do not cover every signal exactly
                once, a required signal has no row, or ``ready``
                disagrees with the rows and the waiver count.
        """
        reported = [row.signal for row in self.signals]
        if sorted(set(reported)) != sorted(ReleaseSignalName):
            missing = sorted(set(ReleaseSignalName) - set(reported))
            raise ValueError(
                f"readiness must report every signal; missing {[name.value for name in missing]}"
            )
        if len(reported) != len(set(reported)):
            raise ValueError("readiness reports a signal more than once")
        undeclared = sorted(set(self.required_signals) - set(reported))
        if undeclared:
            raise ValueError(
                f"required signals absent from the sweep: {[name.value for name in undeclared]}"
            )
        derived_ready = self.first_red is None and self.waiver_count == 0
        if self.ready != derived_ready:
            raise ValueError(
                f"ready={self.ready} disagrees with the rows "
                f"(first_red={self.first_red}, waiver_count={self.waiver_count})"
            )
        return self

    def row(self, signal: ReleaseSignalName) -> ReleaseSignalRow:
        """Return the row reporting *signal*.

        Args:
            signal: Signal to look up.

        Returns:
            The matching row.

        Raises:
            KeyError: When the sweep reports no such signal.
        """
        for row in self.signals:
            if row.signal is signal:
                return row
        raise KeyError(f"readiness {self.release_key} has no row for {signal.value!r}")

    @property
    def first_red(self) -> ReleaseSignalName | None:
        """Return the first required signal that is not passing.

        The approval denial names this signal, so approval and the tag
        chokepoint quote the same row rather than each picking their own.
        """
        for name in self.required_signals:
            if self.row(name).status is not ReleaseSignalStatus.PASS:
                return name
        return None


def derive_required_signals(config: ReleaseConfig) -> tuple[ReleaseSignalName, ...]:
    """Return the required signal subset *config* implies.

    The subset is a function of one authored surface: the rows bound by
    ``gates.required``, plus tree cleanliness when ``require_clean_tree``
    is set, plus ancestry when ``require_ancestor_of_remote`` is set,
    plus credentials when any target is required. There is deliberately
    no second authored list to drift from this derivation.

    Args:
        config: Loaded checkpoint configuration.

    Returns:
        The required signals in :class:`ReleaseSignalName` declaration
        order, deduplicated (two gates may bind one row).
    """
    required: set[ReleaseSignalName] = set()
    for gate in config.gates.required:
        bound = GATE_SIGNAL_BINDINGS[gate]
        if bound is not None:
            required.add(bound)
    if config.require_clean_tree:
        required.add(ReleaseSignalName.TREE_CLEANLINESS)
    if config.require_ancestor_of_remote:
        required.add(ReleaseSignalName.ANCESTRY)
    if config.required_target_ids:
        required.add(ReleaseSignalName.CREDENTIALS)
    return tuple(name for name in ReleaseSignalName if name in required)


def _unavailable(signal: ReleaseSignalName) -> ReleaseSignalOutcome:
    """Return the outcome for a signal with no probe registered.

    Args:
        signal: The signal with no producer yet.

    Returns:
        An ``unavailable`` outcome whose remediation names the gap.
    """
    return ReleaseSignalOutcome(
        status=ReleaseSignalStatus.UNAVAILABLE,
        remediation=(
            f"no producer is registered for the {signal.value!r} signal at this "
            f"checkpoint; register a probe or drop the requirement"
        ),
    )


def _run_probe(probe: ReleaseSignalProbe, context: ReleaseSignalContext) -> ReleaseSignalOutcome:
    """Run *probe* and convert any exception into a blocked outcome.

    Containing the exception here is what makes the sweep total: one
    broken probe reports itself as ``blocked`` and the remaining
    signals still get computed.

    Args:
        probe: The probe to run.
        context: What to run it against.

    Returns:
        The probe's outcome, or a ``blocked`` outcome naming the
        exception type and message.
    """
    try:
        return probe(context)
    except Exception as exc:
        logger.warning(
            f"_run_probe signal={context.signal.value!r} error={type(exc).__name__} detail={exc}"
        )
        return ReleaseSignalOutcome(
            status=ReleaseSignalStatus.BLOCKED,
            remediation=(
                f"the {context.signal.value!r} probe raised "
                f"{type(exc).__name__}: {exc}; repair the probe and rerun preflight"
            ),
        )


def compute_readiness(
    config: ReleaseConfig,
    *,
    probes: Mapping[ReleaseSignalName, ReleaseSignalProbe] | None = None,
    observed_revision: str | None = None,
    computed_at: datetime,
    ttl_seconds: int = DEFAULT_SIGNAL_TTL_SECONDS,
    waiver_count: int = 0,
) -> ReleaseReadiness:
    """Compute every readiness signal for *config* without fail-fast.

    Args:
        config: Loaded checkpoint configuration.
        probes: Producer per signal. Signals absent from the map report
            ``unavailable``; a probe that raises reports ``blocked``.
        observed_revision: Source revision the sweep is computed
            against, stamped on every row.
        computed_at: Timezone-aware UTC instant the sweep ran.
        ttl_seconds: Freshness window per row; must be positive.
        waiver_count: Gate waivers recorded against this checkpoint.

    Returns:
        A total :class:`ReleaseReadiness` with one row per signal.

    Raises:
        ValueError: When *ttl_seconds* is not positive, *waiver_count*
            is negative, or *computed_at* is naive.
    """
    if ttl_seconds <= 0:
        raise ValueError(f"ttl_seconds must be positive, got {ttl_seconds}")
    if waiver_count < 0:
        raise ValueError(f"waiver_count must not be negative, got {waiver_count}")
    if computed_at.tzinfo is None:
        raise ValueError("computed_at must be timezone-aware")
    registry: Mapping[ReleaseSignalName, ReleaseSignalProbe] = probes or {}
    expires_at = computed_at + timedelta(seconds=ttl_seconds)
    rows: list[ReleaseSignalRow] = []
    for signal in ReleaseSignalName:
        probe = registry.get(signal)
        outcome = (
            _unavailable(signal)
            if probe is None
            else _run_probe(probe, ReleaseSignalContext(config, signal, observed_revision))
        )
        passing = outcome.status is ReleaseSignalStatus.PASS
        rows.append(
            ReleaseSignalRow(
                signal=signal,
                status=outcome.status,
                failure_code=None if passing else SIGNAL_FAILURE_CODES[signal],
                observed_revision=observed_revision,
                evidence_refs=outcome.evidence_refs,
                computed_at=computed_at,
                expires_at=expires_at,
                remediation=outcome.remediation,
            )
        )
    required = derive_required_signals(config)
    ready = _is_ready(rows, required, waiver_count)
    readiness = ReleaseReadiness(
        release_key=config.release_key,
        version=config.version,
        channel=config.channel,
        gate_profile=config.gates.profile,
        signals=tuple(rows),
        required_signals=required,
        waiver_count=waiver_count,
        computed_at=computed_at,
        ready=ready,
    )
    logger.info(
        f"compute_readiness release_key={readiness.release_key!r} "
        f"signals={len(readiness.signals)} required={len(required)} "
        f"ready={ready} waiver_count={waiver_count}"
    )
    return readiness


def _is_ready(
    rows: Sequence[ReleaseSignalRow],
    required: Sequence[ReleaseSignalName],
    waiver_count: int,
) -> bool:
    """Return whether every required row passes and no waiver is outstanding.

    Args:
        rows: The computed signal rows.
        required: The derived required subset.
        waiver_count: Gate waivers recorded against the checkpoint.

    Returns:
        ``True`` when the checkpoint may be approved.
    """
    if waiver_count:
        return False
    by_name = {row.signal: row for row in rows}
    return all(by_name[name].status is ReleaseSignalStatus.PASS for name in required)


__all__ = [
    "DEFAULT_SIGNAL_TTL_SECONDS",
    "GATE_SIGNAL_BINDINGS",
    "SIGNAL_FAILURE_CODES",
    "ReleaseReadiness",
    "ReleaseSignalContext",
    "ReleaseSignalFailureCode",
    "ReleaseSignalName",
    "ReleaseSignalOutcome",
    "ReleaseSignalProbe",
    "ReleaseSignalRow",
    "ReleaseSignalStatus",
    "compute_readiness",
    "derive_required_signals",
]
