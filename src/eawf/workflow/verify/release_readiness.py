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
the rows the checkpoint's ``gates.required`` list binds -- through the
gate binding table of :mod:`eawf.kernel.release.gate_binding`, which is
the one declaration of what each gate reads -- plus the rows the three
configuration flags imply (tree cleanliness under ``require_clean_tree``,
ancestry under ``require_ancestor_of_remote``, credentials whenever any
target is required). A second authored list would be free to drift from
the first, so there is no such list.

The readiness object therefore carries two blocks over one set of facts.
The twelve **signal** rows are what the sweep established; the eight
**gate** rows are what the profile makes of them. They are kept separate
rather than merged because a gate settled by a proof command has no row
to merge into, and folding it in would require inventing one.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Annotated, Final, Literal

from pydantic import ConfigDict, Field, model_validator

from eawf.kernel.release.gate_binding import (
    GateBinding,
    GateEvidenceKind,
    ReleaseGateRow,
)
from eawf.kernel.release.signals import (
    SIGNAL_FAILURE_CODES,
    ReleaseSignalContext,
    ReleaseSignalFailureCode,
    ReleaseSignalName,
    ReleaseSignalOutcome,
    ReleaseSignalProbe,
    ReleaseSignalStatus,
)
from eawf.kernel.release.waiver import (
    ReleaseWaiver,
    WaiverDisposition,
    classify_waivers,
)
from eawf.kernel.spec.common import _StrictModel
from eawf.kernel.spec.release import (
    NormalizedVersionStr,
    ReferenceStr,
    ReleaseChannel,
    ReleaseGateProfile,
    ReleaseKeyStr,
)
from eawf.kernel.spec.release_config import ReleaseConfig, ReleaseGateName
from eawf.workflow.release.producers import DEFAULT_RELEASE_PROBES
from eawf.workflow.release.train import gate_bindings_for

logger = logging.getLogger(__name__)

#: Freshness window a readiness row is valid for when the caller does
#: not pin one. One hour is short enough that an approval cannot quote a
#: signal computed against yesterday's tree.
DEFAULT_SIGNAL_TTL_SECONDS: Final[int] = 3600


#: The readiness row each declared ``dev1`` gate name reads, or ``None``
#: when the gate is settled by a proof command run at the pinned
#: revision and so contributes no row to the derived required set.
#: Projected from the authored binding table rather than authored a
#: second time: a component binding contributes its parent row, so
#: ``dependency_inventory`` and ``security_review`` both land on
#: ``dependencies``.
GATE_SIGNAL_BINDINGS: Final[Mapping[ReleaseGateName, ReleaseSignalName | None]] = {
    gate: binding.required_signal
    for gate, binding in gate_bindings_for(ReleaseGateProfile.DEV1).items()
}


class WaiverAcknowledgement(_StrictModel):
    """One operator's acceptance of a counted waiver, carried in the receipt.

    A fully explained waiver set is not red -- it is reported *for
    acknowledgement*, and blocks approval until the operator states, in
    the readiness receipt itself, which protection they are accepting
    the loss of. That statement is this record.

    The acknowledgement repeats the waiver's ``scope`` and
    ``protected_principal`` rather than pointing at a row index: an
    index survives a reordering of the waiver block, so it would go on
    reading as acknowledged while naming a different protection.

    Attributes:
        scope: The waived scope, matching a counted waiver's ``scope``.
        protected_principal: The protection being accepted as lost,
            matching that waiver's ``protected_principal``.
        acknowledged_by: Who accepted the loss.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: Annotated[str, Field(min_length=1)]
    protected_principal: Annotated[str, Field(min_length=1)]
    acknowledged_by: Annotated[str, Field(min_length=1)]

    @property
    def waived(self) -> tuple[str, str]:
        """Return the ``(scope, protected_principal)`` pair this covers."""
        return (self.scope, self.protected_principal)


def _unacknowledged_waivers(
    waivers: Sequence[ReleaseWaiver],
    acknowledgements: Sequence[WaiverAcknowledgement],
    disposition: WaiverDisposition,
) -> tuple[ReleaseWaiver, ...]:
    """Return the counted waivers no acknowledgement covers.

    Empty for every disposition but
    :attr:`~eawf.kernel.release.waiver.WaiverDisposition.AWAITING_ACKNOWLEDGEMENT`:
    nothing is counted under ``NONE``, and an ``UNEXPLAINED`` set is not
    acknowledgeable at all, so listing its rows here would suggest an
    acknowledgement could clear them.

    Args:
        waivers: The counted waiver rows.
        acknowledgements: The acknowledgements the receipt carries.
        disposition: What the counted waivers mean for readiness.

    Returns:
        The waivers still awaiting an acknowledgement, in row order.
    """
    if disposition is not WaiverDisposition.AWAITING_ACKNOWLEDGEMENT:
        return ()
    covered = {ack.waived for ack in acknowledgements}
    return tuple(
        waiver for waiver in waivers if (waiver.scope, waiver.protected_principal) not in covered
    )


def _waivers_cleared(disposition: WaiverDisposition, outstanding: Sequence[ReleaseWaiver]) -> bool:
    """Return whether the waiver block leaves the checkpoint approvable.

    Args:
        disposition: What the counted waivers mean for readiness.
        outstanding: The waivers no acknowledgement covers.

    Returns:
        ``True`` when nothing is counted, or when every counted waiver
        is explained and acknowledged. An ``UNEXPLAINED`` set is never
        cleared: there is nothing to acknowledge.
    """
    if disposition is WaiverDisposition.NONE:
        return True
    if disposition is WaiverDisposition.UNEXPLAINED:
        return False
    return not outstanding


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
        gates: One row per gate the profile admits, in the profile's
            declaration order. A projection of the signals through the
            binding table, not a second set of facts.
        required_signals: The derived required subset. Never authored;
            see :func:`derive_required_signals`.
        waiver_count: Gate waivers recorded against this checkpoint.
            A field beside the signals, never a thirteenth signal row.
        waivers: The counted waivers, each naming its scope, reason and
            protected principal. May be shorter than
            :attr:`waiver_count`, which is itself the finding that some
            waiver was counted with nothing attached.
        waiver_disposition: What the counted waivers mean for readiness;
            derived from :attr:`waivers` and :attr:`waiver_count`.
        waiver_acknowledgements: The operator acceptances this receipt
            carries. Each must name a counted waiver; an explained
            waiver blocks approval until one covers it.
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
    gates: tuple[ReleaseGateRow, ...] = ()
    required_signals: tuple[ReleaseSignalName, ...] = ()
    waiver_count: Annotated[int, Field(ge=0)] = 0
    waivers: tuple[ReleaseWaiver, ...] = ()
    waiver_disposition: WaiverDisposition = WaiverDisposition.NONE
    waiver_acknowledgements: tuple[WaiverAcknowledgement, ...] = ()
    computed_at: datetime
    ready: bool

    @model_validator(mode="after")
    def _sweep_is_total_and_honest(self) -> ReleaseReadiness:
        """Reject a partial sweep, an undeclared requirement or a lying verdict.

        Raises:
            ValueError: When the rows do not cover every signal exactly
                once, a gate is reported twice, a required signal has no
                row, the waiver disposition disagrees with the waivers,
                an acknowledgement names no counted waiver, or ``ready``
                disagrees with the rows and the waivers.
        """
        reported = [row.signal for row in self.signals]
        if sorted(set(reported)) != sorted(ReleaseSignalName):
            missing = sorted(set(ReleaseSignalName) - set(reported))
            raise ValueError(
                f"readiness must report every signal; missing {[name.value for name in missing]}"
            )
        if len(reported) != len(set(reported)):
            raise ValueError("readiness reports a signal more than once")
        gates = [row.gate for row in self.gates]
        if len(gates) != len(set(gates)):
            raise ValueError("readiness reports a gate more than once")
        undeclared = sorted(set(self.required_signals) - set(reported))
        if undeclared:
            raise ValueError(
                f"required signals absent from the sweep: {[name.value for name in undeclared]}"
            )
        derived_disposition = classify_waivers(self.waivers, waiver_count=self.waiver_count)
        if self.waiver_disposition is not derived_disposition:
            raise ValueError(
                f"waiver_disposition={self.waiver_disposition.value!r} disagrees with the "
                f"{self.waiver_count} counted waiver(s) "
                f"(derived {derived_disposition.value!r})"
            )
        counted = {(waiver.scope, waiver.protected_principal) for waiver in self.waivers}
        dangling = sorted(
            f"{ack.scope}/{ack.protected_principal}"
            for ack in self.waiver_acknowledgements
            if ack.waived not in counted
        )
        if dangling:
            raise ValueError(
                f"waiver acknowledgement(s) name no counted waiver: {dangling}; "
                f"an acknowledgement of nothing accepts no loss"
            )
        derived_ready = self.first_red is None and self.waivers_cleared
        if self.ready != derived_ready:
            raise ValueError(
                f"ready={self.ready} disagrees with the rows "
                f"(first_red={self.first_red}, waiver_count={self.waiver_count}, "
                f"unacknowledged={len(self.unacknowledged_waivers)})"
            )
        return self

    @property
    def unacknowledged_waivers(self) -> tuple[ReleaseWaiver, ...]:
        """Return the counted waivers this receipt carries no acknowledgement for."""
        return _unacknowledged_waivers(
            self.waivers, self.waiver_acknowledgements, self.waiver_disposition
        )

    @property
    def waivers_cleared(self) -> bool:
        """Return whether the waiver block leaves the checkpoint approvable."""
        return _waivers_cleared(self.waiver_disposition, self.unacknowledged_waivers)

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

    def gate_row(self, gate: ReleaseGateName) -> ReleaseGateRow:
        """Return the row reporting *gate*.

        Args:
            gate: Gate to look up.

        Returns:
            The matching row.

        Raises:
            KeyError: When the sweep reports no such gate.
        """
        for row in self.gates:
            if row.gate is gate:
                return row
        raise KeyError(f"readiness {self.release_key} has no row for gate {gate.value!r}")

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

    @property
    def first_red_gate(self) -> ReleaseGateName | None:
        """Return the first required gate whose bound row is not passing.

        Gates are scanned in profile order, so the denial an operator
        reads names the earliest gate they have to repair rather than an
        arbitrary one. A gate settled by a proof command is skipped: it
        contributes no row to the required set, so it cannot be the
        reason a sweep is not ready, and naming it would send the
        operator to fix something the sweep never measured.
        """
        for row in self.gates:
            if not row.required or row.evidence_kind is GateEvidenceKind.PROOF_COMMAND:
                continue
            if row.status is not ReleaseSignalStatus.PASS:
                return row.gate
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

    Raises:
        GateBindingError: When the checkpoint's profile has no authored
            binding table, or that table fails validation.
    """
    bindings = gate_bindings_for(config.gates.profile)
    required: set[ReleaseSignalName] = set()
    for gate in config.gates.required:
        bound = bindings[gate].required_signal
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


def _gate_rows(
    config: ReleaseConfig,
    bindings: Mapping[ReleaseGateName, GateBinding],
    rows: Sequence[ReleaseSignalRow],
) -> tuple[ReleaseGateRow, ...]:
    """Return one row per gate the profile admits, in profile order.

    A gate bound to a row or to a component of a row inherits that row's
    verdict: a component is a finer-grained *reading* of the row, not a
    separately computed one, so two gates on one row report that row
    twice rather than disagreeing. A gate bound to a proof command has
    no row to inherit and reports ``unavailable`` naming the command,
    which is the same shape the sweep uses for any producer that has not
    landed.

    Args:
        config: Loaded checkpoint configuration.
        bindings: The profile's validated binding table.
        rows: The computed signal rows.

    Returns:
        The gate rows.
    """
    by_signal = {row.signal: row for row in rows}
    required = set(config.gates.required)
    gate_rows: list[ReleaseGateRow] = []
    for gate, binding in bindings.items():
        if binding.kind is GateEvidenceKind.PROOF_COMMAND:
            assert binding.proof is not None
            status = ReleaseSignalStatus.UNAVAILABLE
            remediation = (
                f"gate {gate.value!r} is settled by proof command "
                f"{binding.proof.command_id!r}; run it at the pinned source revision "
                f"through the gate runner and attach the receipt"
            )
        else:
            assert binding.signal is not None
            row = by_signal[binding.signal]
            status = row.status
            remediation = row.remediation
        gate_rows.append(
            ReleaseGateRow(
                gate=gate,
                evidence_kind=binding.kind,
                evidence_ref=binding.evidence_ref,
                required=gate in required,
                status=status,
                remediation=remediation,
            )
        )
    return tuple(gate_rows)


def compute_readiness(
    config: ReleaseConfig,
    *,
    probes: Mapping[ReleaseSignalName, ReleaseSignalProbe] | None = None,
    observed_revision: str | None = None,
    computed_at: datetime,
    ttl_seconds: int = DEFAULT_SIGNAL_TTL_SECONDS,
    waiver_count: int = 0,
    waivers: Sequence[ReleaseWaiver] = (),
    acknowledgements: Sequence[WaiverAcknowledgement] = (),
) -> ReleaseReadiness:
    """Compute every readiness signal for *config* without fail-fast.

    Args:
        config: Loaded checkpoint configuration.
        probes: Producer per signal, overriding
            :data:`~eawf.workflow.release.producers.DEFAULT_RELEASE_PROBES`.
            Signals with no producer either way report ``unavailable``;
            a probe that raises reports ``blocked``.
        observed_revision: Source revision the sweep is computed
            against, stamped on every row.
        computed_at: Timezone-aware UTC instant the sweep ran.
        ttl_seconds: Freshness window per row; must be positive.
        waiver_count: Gate waivers recorded against this checkpoint.
            Defaults to the length of *waivers* when left at zero, so a
            caller supplying the rows never has to count them too.
        waivers: The counted waivers. A count larger than the number of
            rows is itself the finding that a waiver was recorded with
            no explanation.
        acknowledgements: Operator acceptances to carry in the receipt.
            An explained waiver blocks approval until one names it; an
            acknowledgement naming no counted waiver is refused.

    Returns:
        A total :class:`ReleaseReadiness` with one row per signal and
        one row per gate the profile admits.

    Raises:
        ValueError: When *ttl_seconds* is not positive, *waiver_count*
            is negative, *computed_at* is naive, *waiver_count* is
            non-zero and disagrees with the number of *waivers*, or an
            acknowledgement names no counted waiver.
    """
    if ttl_seconds <= 0:
        raise ValueError(f"ttl_seconds must be positive, got {ttl_seconds}")
    if waiver_count < 0:
        raise ValueError(f"waiver_count must not be negative, got {waiver_count}")
    if computed_at.tzinfo is None:
        raise ValueError("computed_at must be timezone-aware")
    counted = _counted_waivers(waiver_count=waiver_count, waivers=waivers)
    registry: Mapping[ReleaseSignalName, ReleaseSignalProbe] = probes or {}
    expires_at = computed_at + timedelta(seconds=ttl_seconds)
    rows: list[ReleaseSignalRow] = []
    for signal in ReleaseSignalName:
        probe = registry.get(signal) or DEFAULT_RELEASE_PROBES.get(signal)
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
    bindings = gate_bindings_for(config.gates.profile)
    required = derive_required_signals(config)
    disposition = classify_waivers(tuple(waivers), waiver_count=counted)
    outstanding = _unacknowledged_waivers(waivers, acknowledgements, disposition)
    ready = _is_ready(rows, required, disposition, outstanding)
    readiness = ReleaseReadiness(
        release_key=config.release_key,
        version=config.version,
        channel=config.channel,
        gate_profile=config.gates.profile,
        signals=tuple(rows),
        gates=_gate_rows(config, bindings, rows),
        required_signals=required,
        waiver_count=counted,
        waivers=tuple(waivers),
        waiver_disposition=disposition,
        waiver_acknowledgements=tuple(acknowledgements),
        computed_at=computed_at,
        ready=ready,
    )
    logger.info(
        f"compute_readiness release_key={readiness.release_key!r} "
        f"signals={len(readiness.signals)} gates={len(readiness.gates)} "
        f"required={len(required)} ready={ready} waiver_count={counted} "
        f"waiver_disposition={disposition.value!r} "
        f"unacknowledged={len(outstanding)}"
    )
    return readiness


def _counted_waivers(*, waiver_count: int, waivers: Sequence[ReleaseWaiver]) -> int:
    """Return how many waivers are counted against the checkpoint.

    Args:
        waiver_count: The caller's count; zero means "derive from the
            rows", which keeps a caller that supplies rows from having
            to count them too.
        waivers: The supplied waiver rows.

    Returns:
        The effective count.

    Raises:
        ValueError: When a non-zero count is smaller than the number of
            supplied rows, which would leave rows uncounted.
    """
    if waiver_count == 0:
        return len(waivers)
    if waiver_count < len(waivers):
        raise ValueError(
            f"waiver_count={waiver_count} is smaller than the {len(waivers)} waiver row(s) "
            f"supplied; every row is a counted waiver"
        )
    return waiver_count


def _is_ready(
    rows: Sequence[ReleaseSignalRow],
    required: Sequence[ReleaseSignalName],
    disposition: WaiverDisposition,
    outstanding: Sequence[ReleaseWaiver],
) -> bool:
    """Return whether every required row passes and no waiver is outstanding.

    Args:
        rows: The computed signal rows.
        required: The derived required subset.
        disposition: What the counted waivers mean for readiness. An
            unexplained set is red and cannot be acknowledged.
        outstanding: The explained waivers no acknowledgement covers;
            each blocks until the operator accepts the loss in the
            receipt.

    Returns:
        ``True`` when the checkpoint may be approved.
    """
    if not _waivers_cleared(disposition, outstanding):
        return False
    by_name = {row.signal: row for row in rows}
    return all(by_name[name].status is ReleaseSignalStatus.PASS for name in required)


__all__ = [
    "DEFAULT_SIGNAL_TTL_SECONDS",
    "GATE_SIGNAL_BINDINGS",
    "SIGNAL_FAILURE_CODES",
    "GateBinding",
    "GateEvidenceKind",
    "ReleaseGateRow",
    "ReleaseReadiness",
    "ReleaseSignalContext",
    "ReleaseSignalFailureCode",
    "ReleaseSignalName",
    "ReleaseSignalOutcome",
    "ReleaseSignalProbe",
    "ReleaseSignalRow",
    "ReleaseSignalStatus",
    "ReleaseWaiver",
    "WaiverAcknowledgement",
    "WaiverDisposition",
    "compute_readiness",
    "derive_required_signals",
]
