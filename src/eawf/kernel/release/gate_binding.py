"""What evidence each gate name of a profile reads, and its loader.

A gate profile names the checks a checkpoint must pass; the twelve
readiness signals name the facts a sweep can establish. Neither implies
the other, and until they are bound the profile is decorative: eight
gate names sitting beside twelve rows with no declared function between
them is exactly how a gate passes vacuously.

This module declares that function. Every gate of a profile binds to
exactly one evidence source, of exactly three kinds:

* a **signal row** -- the gate reads the whole row (``changelog_entry``
  reads ``changelog``);
* a **component of a signal row** -- the gate reads one named check that
  shares a row with another (``dependency_inventory`` and
  ``security_review`` read ``dependencies.inventory`` and
  ``dependencies.vulnerability``);
* a **proof command** -- the gate has no row at all and is settled by
  running an argv at the pinned source revision
  (``front_door_journey``).

:func:`load_gate_bindings` is the fail-fast boundary. It refuses a
profile gate nobody bound, a binding that names a signal or component
that does not exist, and a proof command whose argv the L0 gate-runner
policy would reject -- each with a typed
:class:`GateBindingRejection` so a caller branches on the cause rather
than on prose.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, Final

import yaml
from pydantic import ConfigDict, Field, ValidationError, model_validator

from eawf.kernel.release.signals import (
    SIGNAL_COMPONENTS,
    ReleaseSignalName,
    ReleaseSignalStatus,
    component_ref,
    declared_components,
)
from eawf.kernel.spec.common import _StrictModel
from eawf.kernel.spec.release import ReleaseGateProfile
from eawf.kernel.spec.release_config import ReleaseGateName
from eawf.runtime.sandbox.argv_policy import ArgvPolicyError, validate_gate_argv

logger = logging.getLogger(__name__)

#: Argv heads a release proof command may dispatch through the gate
#: runner. The set is the project's own tooling surface -- the ``uv`` /
#: ``uvx`` wrappers, the ``run`` sub-verb the depth-1 recursion consumes,
#: and the three inner commands the authored proofs invoke. A new head
#: is a policy decision, not a config knob, so it is spelled here rather
#: than read from the checkpoint file.
PROOF_ARGV_ALLOWLIST: Final[tuple[str, ...]] = (
    "uv",
    "uvx",
    "run",
    "pytest",
    "eawf",
)


class GateEvidenceKind(StrEnum):
    """The four shapes of evidence a gate can be bound to.

    Values:
        SIGNAL: The gate reads a whole readiness row.
        SIGNAL_COMPONENT: The gate reads one named component of a row it
            shares with another gate.
        PROOF_COMMAND: The gate has no row; it is settled by running an
            argv at the pinned source revision.
        WAIVER_BLOCK: The gate has no row either; it reads the waiver
            block the readiness receipt carries beside the signals.
            Waivers are counted per checkpoint rather than probed, so
            making one a thirteenth signal row would have required a
            producer that cannot exist.
    """

    SIGNAL = "signal"
    SIGNAL_COMPONENT = "signal_component"
    PROOF_COMMAND = "proof_command"
    WAIVER_BLOCK = "waiver_block"


#: The evidence kinds that read a readiness row, and so contribute one to
#: the derived required set.
_ROW_BACKED_KINDS: Final[frozenset[GateEvidenceKind]] = frozenset(
    {GateEvidenceKind.SIGNAL, GateEvidenceKind.SIGNAL_COMPONENT}
)

#: The evidence reference a waiver-block gate reports. A constant rather
#: than a signal name because there is no row to name: the waiver block
#: sits beside the twelve rows on the readiness receipt.
WAIVER_BLOCK_REF: Final[str] = "readiness:waivers"


class GateBindingRejection(StrEnum):
    """Closed vocabulary for why a gate binding table was rejected.

    Values:
        SCHEMA_INVALID: The payload does not match the model shape.
        UNDECLARED_PROFILE: No gate set is declared for the profile.
        UNBOUND_PROFILE_GATE: A gate the profile requires has no binding.
        UNBOUND_GATE_OUTSIDE_PROFILE: A binding names a gate the profile
            does not admit.
        DUPLICATE_BINDING: Two bindings name the same gate.
        UNKNOWN_SIGNAL: A binding names a signal that does not exist.
        UNKNOWN_COMPONENT: A binding names a component that is not
            declared for its signal.
        ARGV_REJECTED: A proof command's argv fails the L0 argv policy.
    """

    SCHEMA_INVALID = "schema_invalid"
    UNDECLARED_PROFILE = "undeclared_profile"
    UNBOUND_PROFILE_GATE = "unbound_profile_gate"
    UNBOUND_GATE_OUTSIDE_PROFILE = "unbound_gate_outside_profile"
    DUPLICATE_BINDING = "duplicate_binding"
    UNKNOWN_SIGNAL = "unknown_signal"
    UNKNOWN_COMPONENT = "unknown_component"
    ARGV_REJECTED = "argv_rejected"


class GateBindingError(ValueError):
    """A gate binding table was rejected at load.

    Subclasses :class:`ValueError` so existing boundary handlers keep
    working; :attr:`code` is what a caller branches on.

    Attributes:
        code: Which rejection fired.
    """

    def __init__(self, code: GateBindingRejection, message: str) -> None:
        """Store the typed *code* alongside the operator-facing *message*."""
        super().__init__(message)
        self.code = code


#: The eight gates the ``dev1`` profile admits, in declaration order.
#: Named separately because ``dev2`` is defined as a superset of them: a
#: checkpoint that dropped a gate its predecessor passed could not claim
#: the train stabilizes monotonically, so the later profile extends the
#: tuple rather than restating it.
DEV1_GATES: Final[tuple[ReleaseGateName, ...]] = (
    ReleaseGateName.VERSION_CONSISTENCY,
    ReleaseGateName.CHANGELOG_ENTRY,
    ReleaseGateName.DEPENDENCY_INVENTORY,
    ReleaseGateName.ARTIFACT_REPRODUCIBILITY,
    ReleaseGateName.SECURITY_REVIEW,
    ReleaseGateName.EPOCH1_STABILIZATION,
    ReleaseGateName.TELEMETRY_PRODUCER,
    ReleaseGateName.FRONT_DOOR_JOURNEY,
)

#: The four gates ``dev2`` adds on top of :data:`DEV1_GATES`. Each names
#: a protection the epoch-2 work introduced and nothing before it could
#: have proven: the cutover rehearsal, the daemon-hosted gate runner, the
#: strictness census over the epoch-2 entity package, and the waiver
#: block that says how much of all of it was bought rather than earned.
DEV2_ADDED_GATES: Final[tuple[ReleaseGateName, ...]] = (
    ReleaseGateName.MIGRATION,
    ReleaseGateName.HOSTED_GATE_RUNNER,
    ReleaseGateName.SCHEMA_STRICTNESS,
    ReleaseGateName.WAIVER_COUNT,
)

#: The gate names each profile admits. Only ``dev1`` and ``dev2`` are
#: authored: the later profiles add their gates with the waves that build
#: their producers, and an unauthored profile is a loud
#: :attr:`GateBindingRejection.UNDECLARED_PROFILE` rather than a silently
#: empty gate set.
PROFILE_GATES: Final[Mapping[ReleaseGateProfile, tuple[ReleaseGateName, ...]]] = {
    ReleaseGateProfile.DEV1: DEV1_GATES,
    ReleaseGateProfile.DEV2: (*DEV1_GATES, *DEV2_ADDED_GATES),
}


def profile_gates(profile: ReleaseGateProfile) -> tuple[ReleaseGateName, ...]:
    """Return the gate names *profile* admits, in declaration order.

    Args:
        profile: Gate profile a checkpoint runs under.

    Returns:
        The profile's gate names.

    Raises:
        GateBindingError: With
            :attr:`GateBindingRejection.UNDECLARED_PROFILE` when no gate
            set has been authored for *profile* yet.
    """
    try:
        return PROFILE_GATES[profile]
    except KeyError as exc:
        authored = sorted(known.value for known in PROFILE_GATES)
        raise GateBindingError(
            GateBindingRejection.UNDECLARED_PROFILE,
            f"no gate set is declared for profile {profile.value!r}; have {authored}",
        ) from exc


class ProofCommand(_StrictModel):
    """An argv a gate is settled by, run at the pinned source revision.

    Attributes:
        command_id: Stable identity of the proof, quoted by the receipt
            the gate runner writes.
        argv: The vector handed to the runner. Validated against the L0
            argv policy at load, not at construction, so a rejected
            argv carries the typed
            :attr:`GateBindingRejection.ARGV_REJECTED` rather than a
            bare :class:`pydantic.ValidationError`.
        timeout_seconds: Wall budget for one run.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    command_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    argv: Annotated[tuple[str, ...], Field(min_length=1)]
    timeout_seconds: Annotated[int, Field(gt=0)]


@dataclass(frozen=True, slots=True)
class ResolvedProofCommand:
    """A proof command pinned to the revision it must be run at.

    The pin is the point of the type. A proof command run against
    whatever the working tree happens to hold proves nothing about the
    checkpoint, so the gate runner is handed a vector *and* the SHA it
    is answerable for, never the vector alone.

    Attributes:
        command_id: Identity of the proof.
        argv: The vector to run.
        source_sha: Commit the run is answerable for.
        timeout_seconds: Wall budget for the run.
    """

    command_id: str
    argv: tuple[str, ...]
    source_sha: str
    timeout_seconds: int


class GateBinding(_StrictModel):
    """One gate name bound to exactly one evidence source.

    Attributes:
        gate: The gate name being bound.
        kind: Which of the three evidence shapes this binding uses.
        signal: The bound row, for
            :attr:`GateEvidenceKind.SIGNAL` and
            :attr:`GateEvidenceKind.SIGNAL_COMPONENT`.
        component: The bare component name (``inventory``), for
            :attr:`GateEvidenceKind.SIGNAL_COMPONENT` only.
        proof: The argv, for :attr:`GateEvidenceKind.PROOF_COMMAND` only.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    gate: ReleaseGateName
    kind: GateEvidenceKind
    signal: ReleaseSignalName | None = None
    component: str | None = None
    proof: ProofCommand | None = None

    @model_validator(mode="after")
    def _binds_exactly_one_source(self) -> GateBinding:
        """Reject a binding whose fields do not match its declared kind.

        Raises:
            ValueError: When the field set carries no evidence for the
                declared kind, or carries evidence of another kind
                alongside it.
        """
        expected: dict[GateEvidenceKind, tuple[str, ...]] = {
            GateEvidenceKind.SIGNAL: ("signal",),
            GateEvidenceKind.SIGNAL_COMPONENT: ("signal", "component"),
            GateEvidenceKind.PROOF_COMMAND: ("proof",),
            GateEvidenceKind.WAIVER_BLOCK: (),
        }
        required = expected[self.kind]
        present = tuple(
            name for name in ("signal", "component", "proof") if getattr(self, name) is not None
        )
        if present != required:
            raise ValueError(
                f"gate {self.gate.value!r} declares kind {self.kind.value!r}, which binds "
                f"{list(required)}, but carries {list(present)}"
            )
        return self

    @property
    def evidence_ref(self) -> str:
        """Return the one evidence source this gate reads, as a string.

        Returns:
            The signal name, the dotted ``<signal>.<component>``
            reference, ``proof:<command_id>``, or
            :data:`WAIVER_BLOCK_REF`.
        """
        if self.kind is GateEvidenceKind.WAIVER_BLOCK:
            return WAIVER_BLOCK_REF
        if self.kind is GateEvidenceKind.PROOF_COMMAND:
            assert self.proof is not None
            return f"proof:{self.proof.command_id}"
        assert self.signal is not None
        if self.kind is GateEvidenceKind.SIGNAL:
            return self.signal.value
        assert self.component is not None
        return component_ref(self.signal, self.component)

    @property
    def required_signal(self) -> ReleaseSignalName | None:
        """Return the readiness row this gate contributes to the required set.

        A component binding contributes its *parent* row: a gate reading
        ``dependencies.inventory`` still needs the ``dependencies`` row
        computed. A proof-command binding contributes nothing, because
        it is settled by a run rather than by a row; a waiver-block
        binding contributes nothing for the same reason, and its verdict
        is already folded into the sweep's own readiness.
        """
        return self.signal if self.kind in _ROW_BACKED_KINDS else None

    def resolve_proof(self, source_sha: str) -> ResolvedProofCommand:
        """Return this gate's proof command pinned to *source_sha*.

        Args:
            source_sha: The 40-hex commit the run is answerable for.

        Returns:
            The pinned command.

        Raises:
            TypeError: When *source_sha* is not a string.
            ValueError: When this binding is not a proof command, or
                *source_sha* is not a 40-character lowercase hex SHA.
        """
        if self.proof is None:
            raise ValueError(
                f"gate {self.gate.value!r} binds {self.kind.value!r}, not a proof command"
            )
        if not isinstance(source_sha, str):
            raise TypeError(f"source_sha must be str; got {type(source_sha).__name__}")
        if not _is_sha(source_sha):
            raise ValueError(
                f"gate {self.gate.value!r} proof command must be pinned to a 40-hex "
                f"source SHA; got {source_sha!r}"
            )
        return ResolvedProofCommand(
            command_id=self.proof.command_id,
            argv=self.proof.argv,
            source_sha=source_sha,
            timeout_seconds=self.proof.timeout_seconds,
        )


class ReleaseGateRow(_StrictModel):
    """One gate's row in a readiness object's gate block.

    Attributes:
        gate: The gate this row reports.
        evidence_kind: Which evidence shape the gate is bound to.
        evidence_ref: The bound evidence, per
            :attr:`GateBinding.evidence_ref`.
        required: Whether the checkpoint's ``gates.required`` list names
            this gate.
        status: The bound evidence's verdict. A proof-command gate is
            ``unavailable`` until its runner receipt lands.
        remediation: Operator next action; non-empty for any non-pass
            verdict.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    gate: ReleaseGateName
    evidence_kind: GateEvidenceKind
    evidence_ref: Annotated[str, Field(min_length=1)]
    required: bool
    status: ReleaseSignalStatus
    remediation: str = ""

    @model_validator(mode="after")
    def _red_row_names_a_next_action(self) -> ReleaseGateRow:
        """Reject a non-passing gate row that offers the operator nothing.

        Raises:
            ValueError: When the row is not passing and carries no
                remediation.
        """
        if self.status is not ReleaseSignalStatus.PASS and not self.remediation.strip():
            raise ValueError(
                f"gate {self.gate.value!r} is {self.status.value!r} and must carry remediation"
            )
        return self


def _is_sha(value: str) -> bool:
    """Return whether *value* is a 40-character lowercase hex SHA."""
    return len(value) == 40 and all(char in "0123456789abcdef" for char in value)


def _decode(source: str | Mapping[str, Any]) -> Sequence[Any]:
    """Return the ``bindings`` list of *source*.

    Args:
        source: YAML text or an already-decoded mapping. Either form may
            be wrapped in a top-level ``bindings:`` key, which is how the
            authored table is written.

    Returns:
        The raw binding rows.

    Raises:
        GateBindingError: With
            :attr:`GateBindingRejection.SCHEMA_INVALID` when the payload
            is not valid YAML, not a mapping, or carries no binding list.
    """
    if isinstance(source, str):
        try:
            decoded = yaml.safe_load(source)
        except yaml.YAMLError as exc:
            raise GateBindingError(
                GateBindingRejection.SCHEMA_INVALID,
                f"gate binding table is not valid YAML: {exc}",
            ) from exc
    else:
        decoded = source
    if not isinstance(decoded, Mapping):
        raise GateBindingError(
            GateBindingRejection.SCHEMA_INVALID,
            f"gate binding table must be a mapping, got {type(decoded).__name__}",
        )
    rows = decoded.get("bindings")
    if not isinstance(rows, Sequence) or isinstance(rows, str):
        raise GateBindingError(
            GateBindingRejection.SCHEMA_INVALID,
            f"gate binding table must carry a 'bindings' list, got {type(rows).__name__}",
        )
    return rows


def _reject_unknown_evidence(row: Mapping[str, Any]) -> None:
    """Raise when a raw binding row names a signal or component that does not exist.

    Runs before model validation because Pydantic would report an
    unknown signal as a generic enum error, which loses the distinction
    between "you named a row that does not exist" and "you named a
    component that does not exist" -- two different authoring mistakes
    with two different fixes.

    Args:
        row: One raw binding row.

    Raises:
        GateBindingError: With
            :attr:`GateBindingRejection.UNKNOWN_SIGNAL` or
            :attr:`GateBindingRejection.UNKNOWN_COMPONENT`.
    """
    signal = row.get("signal")
    if signal is None:
        return
    known = {name.value for name in ReleaseSignalName}
    if signal not in known:
        raise GateBindingError(
            GateBindingRejection.UNKNOWN_SIGNAL,
            f"binding for gate {row.get('gate')!r} names unknown signal {signal!r}; "
            f"have {sorted(known)}",
        )
    component = row.get("component")
    if component is None:
        return
    parent = ReleaseSignalName(signal)
    if component not in SIGNAL_COMPONENTS.get(parent, frozenset()):
        raise GateBindingError(
            GateBindingRejection.UNKNOWN_COMPONENT,
            f"binding for gate {row.get('gate')!r} names unknown component "
            f"{component_ref(parent, str(component))!r}; have {list(declared_components())}",
        )


def _reject_bad_proof_argv(binding: GateBinding) -> None:
    """Raise when a proof command's argv fails the L0 gate-runner policy.

    Args:
        binding: A validated binding.

    Raises:
        GateBindingError: With
            :attr:`GateBindingRejection.ARGV_REJECTED`.
    """
    if binding.proof is None:
        return
    try:
        validate_gate_argv(list(binding.proof.argv), allowlist=list(PROOF_ARGV_ALLOWLIST))
    except ArgvPolicyError as exc:
        raise GateBindingError(
            GateBindingRejection.ARGV_REJECTED,
            f"proof command {binding.proof.command_id!r} for gate {binding.gate.value!r} "
            f"was rejected by the L0 argv policy: {exc}",
        ) from exc


def parse_gate_bindings(source: str | Mapping[str, Any]) -> tuple[GateBinding, ...]:
    """Parse *source* into shape-validated bindings, in authored order.

    Parsing is separate from the profile-dependent validation so a
    caller that only needs the authored shape (a renderer, a diff) can
    stop here while :func:`load_gate_bindings` continues into the
    profile coverage checks.

    Args:
        source: YAML text or an already-decoded mapping carrying a
            ``bindings`` list.

    Returns:
        The validated bindings.

    Raises:
        GateBindingError: On any rejection; the typed
            :attr:`GateBindingError.code` names which.
    """
    bindings: list[GateBinding] = []
    for row in _decode(source):
        if not isinstance(row, Mapping):
            raise GateBindingError(
                GateBindingRejection.SCHEMA_INVALID,
                f"each binding must be a mapping, got {type(row).__name__}",
            )
        _reject_unknown_evidence(row)
        try:
            binding = GateBinding.model_validate(dict(row))
        except ValidationError as exc:
            raise GateBindingError(
                GateBindingRejection.SCHEMA_INVALID,
                f"binding for gate {row.get('gate')!r} rejected: "
                f"{exc.error_count()} schema error(s); first: {exc.errors()[0]['msg']}",
            ) from exc
        _reject_bad_proof_argv(binding)
        bindings.append(binding)
    return tuple(bindings)


def load_gate_bindings(
    source: str | Mapping[str, Any],
    *,
    profile: ReleaseGateProfile,
) -> Mapping[ReleaseGateName, GateBinding]:
    """Return the binding of every gate *profile* admits, fully validated.

    Runs the shape validation of :func:`parse_gate_bindings` and then the
    profile coverage checks: no duplicate gate, no binding for a gate the
    profile does not admit, and -- the load-bearing one -- no gate of the
    profile left unbound. A gate with no binding is a gate that passes by
    reading nothing, which is the failure this table exists to prevent.

    Args:
        source: YAML text or an already-decoded mapping.
        profile: Gate profile whose coverage is being checked.

    Returns:
        The bindings keyed by gate, in the profile's declaration order.

    Raises:
        GateBindingError: On any rejection; the typed
            :attr:`GateBindingError.code` names which.
    """
    declared = profile_gates(profile)
    bound: dict[ReleaseGateName, GateBinding] = {}
    for binding in parse_gate_bindings(source):
        if binding.gate in bound:
            raise GateBindingError(
                GateBindingRejection.DUPLICATE_BINDING,
                f"gate {binding.gate.value!r} is bound more than once under profile "
                f"{profile.value!r}; a gate reads exactly one evidence source",
            )
        if binding.gate not in declared:
            raise GateBindingError(
                GateBindingRejection.UNBOUND_GATE_OUTSIDE_PROFILE,
                f"gate {binding.gate.value!r} is not admitted by profile {profile.value!r}; "
                f"have {[gate.value for gate in declared]}",
            )
        bound[binding.gate] = binding
    missing = [gate.value for gate in declared if gate not in bound]
    if missing:
        raise GateBindingError(
            GateBindingRejection.UNBOUND_PROFILE_GATE,
            f"profile {profile.value!r} declares gate(s) {missing} with no binding; "
            f"an unbound gate passes by reading nothing",
        )
    logger.info(
        f"load_gate_bindings profile={profile.value!r} gates={len(bound)} "
        f"proof_commands={sum(1 for b in bound.values() if b.proof is not None)}"
    )
    return {gate: bound[gate] for gate in declared}


def resolved_proof_commands(
    bindings: Mapping[ReleaseGateName, GateBinding],
    *,
    source_sha: str,
) -> tuple[ResolvedProofCommand, ...]:
    """Return every proof command in *bindings*, pinned to *source_sha*.

    Args:
        bindings: A loaded binding table.
        source_sha: The 40-hex commit the runs are answerable for.

    Returns:
        The pinned commands in binding order; empty when the profile
        binds no proof command.

    Raises:
        TypeError: When *source_sha* is not a string.
        ValueError: When *source_sha* is not a 40-hex SHA.
    """
    return tuple(
        binding.resolve_proof(source_sha)
        for binding in bindings.values()
        if binding.kind is GateEvidenceKind.PROOF_COMMAND
    )


__all__ = [
    "DEV1_GATES",
    "DEV2_ADDED_GATES",
    "PROFILE_GATES",
    "PROOF_ARGV_ALLOWLIST",
    "WAIVER_BLOCK_REF",
    "GateBinding",
    "GateBindingError",
    "GateBindingRejection",
    "GateEvidenceKind",
    "ProofCommand",
    "ReleaseGateRow",
    "ResolvedProofCommand",
    "load_gate_bindings",
    "parse_gate_bindings",
    "profile_gates",
    "resolved_proof_commands",
]
