"""Authoring validation and execution-contract compilation for delivery criteria.

Delivery reuses the planning-owned :class:`~eawf.kernel.spec.common.CriterionSpec`
and :class:`~eawf.kernel.spec.common.GateSpec` unchanged: those models refuse
unknown fields, and no delivery-local variant of either exists. Delivery
adds two things on top. The authoring floor catches a criterion set whose
proof cannot mean what its prose claims, before any gate runs. The compile
step turns a set that clears the floor into one :class:`ExecutionContract`
per gate.

Validation reports every finding at once, each tagged with the
:class:`AuthoringRule` it broke, so an author repairs the whole set in one
pass; compilation refuses any set with a finding. A contract's digests are
recomputed from the rows it carries, never stored beside them, so the
freshness key built from them cannot outlive a change to a covered
criterion or to the gate.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Final, Self

from pydantic import ConfigDict, Field, model_validator

from eawf.kernel.delivery.receipts import canonical_digest
from eawf.kernel.spec.common import (
    SINGLE_SITE_GATE_KINDS,
    UNIVERSAL_SCOPE_TOKENS,
    CriterionEvidenceKind,
    CriterionSpec,
    DeferredDeliverable,
    GateSpec,
    ObserveVerb,
    OracleTier,
    ProofLocus,
    ResponseClause,
    SourceUnit,
    assign_oracle_tier,
    response_from_gate,
    tier_label,
)
from eawf.kernel.state.epoch2.base import Epoch2Model
from eawf.kernel.store.kinds.gate_receipt import GateIdentityStr
from eawf.workflow.audit_dsl.models import CheckSpec
from eawf.workflow.propose.generator import coverage_diff
from eawf.workflow.verify.compile import compile_gate

logger = logging.getLogger(__name__)

#: Declared ceiling on criteria in one scope. Past this a scope is an
#: agenda rather than a deliverable, and its audit stops being readable.
#: Authored policy: most shipped waves carry one to five criteria.
MAX_CRITERIA_PER_SCOPE: Final = 8

#: Loci only a judgment can observe. A machine verb placed here names a
#: proof no deterministic runner can take.
_JUDGMENT_LOCI: Final = frozenset({ProofLocus.HUMAN, ProofLocus.JURY})

#: Word-boundary match over the planning module's universal-scope tokens,
#: so ``overall`` does not read as ``all``.
_UNIVERSAL_SCOPE_RE: Final = re.compile(
    r"\b(?:" + "|".join(re.escape(token) for token in UNIVERSAL_SCOPE_TOKENS) + r")\b",
    re.IGNORECASE,
)

#: How much of a promise quote a finding repeats.
_QUOTE_PREVIEW_CHARS: Final = 80


class _FrozenModel(Epoch2Model):
    """Strict and immutable: a compiled contract is never edited in place."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class AuthoringRule(StrEnum):
    """The authoring floor a criterion set must clear before it compiles."""

    UNRESOLVED_GATE = "unresolved_gate"
    ORPHAN_GATE = "orphan_gate"
    UNGATED_CRITERION = "ungated_criterion"
    UNKNOWN_ORACLE = "unknown_oracle"
    UNCOMPILED_GATE = "uncompiled_gate"
    DETERMINISTIC_JURY_ROUTE = "deterministic_jury_route"
    QUANTIFIER_MISMATCH = "quantifier_mismatch"
    LOCUS_MISMATCH = "locus_mismatch"
    SCOPE_MISMATCH = "scope_mismatch"
    UNCOVERED_PROMISE = "uncovered_promise"
    COMPLEXITY_CEILING = "complexity_ceiling"


class AuthoringFinding(_FrozenModel):
    """One broken authoring rule and the row that broke it.

    ``subject_id`` is a criterion id, a gate id, a promise span id, or the
    scope id, whichever the rule is about.
    """

    rule: AuthoringRule
    subject_id: str = Field(min_length=1)
    message: str = Field(min_length=1)


class CriteriaAuthoringError(ValueError):
    """A criterion set failed the authoring floor and was not compiled.

    Attributes:
        findings: Every finding, in the order validation reported them.
    """

    def __init__(self, findings: Sequence[AuthoringFinding]) -> None:
        """Keep *findings* and render them into the message.

        Args:
            findings: The non-empty findings that blocked compilation.
        """
        self.findings = tuple(findings)
        summary = "; ".join(
            f"{finding.rule.value} {finding.subject_id}: {finding.message}"
            for finding in self.findings
        )
        super().__init__(f"criteria authoring rejected: {summary}")


class CriteriaAuthoring(_FrozenModel):
    """One scope's authored criteria, gates, and the promises they must cover.

    ``promise_coverage`` maps a promise span id to the criteria that prove
    it; a promise is covered only through this map or a deferral, never by
    guessing from prose. Structural integrity (unique ids, references that
    name real rows) is checked here at load; the authoring rules proper run
    in :func:`validate_criteria_authoring`.
    """

    scope_id: GateIdentityStr
    criteria: tuple[CriterionSpec, ...] = Field(min_length=1)
    gates: tuple[GateSpec, ...] = ()
    promises: tuple[SourceUnit, ...] = ()
    promise_coverage: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    deferrals: tuple[DeferredDeliverable, ...] = ()

    @model_validator(mode="after")
    def _ids_are_unique(self) -> Self:
        """Require unique criterion, gate, and promise ids.

        Raises:
            ValueError: An id repeats inside its row family.
        """
        families = {
            "criterion": [row.id for row in self.criteria],
            "gate": [row.id for row in self.gates],
            "promise": [row.span_id for row in self.promises],
        }
        for family, ids in families.items():
            repeated = sorted({value for value in ids if ids.count(value) > 1})
            if repeated:
                raise ValueError(f"{family} ids repeat: {', '.join(repeated)}")
        return self

    @model_validator(mode="after")
    def _promise_references_resolve(self) -> Self:
        """Require coverage and deferral rows to name real promises and criteria.

        Raises:
            ValueError: A coverage key or deferral names an unknown promise,
                a coverage row lists no criterion, or it names an unknown
                criterion.
        """
        span_ids = {row.span_id for row in self.promises}
        criterion_ids = {row.id for row in self.criteria}
        for span_id, covering in self.promise_coverage.items():
            if span_id not in span_ids:
                raise ValueError(f"promise_coverage names unknown promise {span_id!r}")
            if not covering:
                raise ValueError(f"promise_coverage for {span_id!r} lists no criterion")
            unknown = sorted(set(covering) - criterion_ids)
            if unknown:
                raise ValueError(
                    f"promise_coverage for {span_id!r} names unknown criteria {unknown}"
                )
        for deferral in self.deferrals:
            if deferral.span_id not in span_ids:
                raise ValueError(f"deferral names unknown promise {deferral.span_id!r}")
        return self


def gate_kind_oracle_tier(kind: str) -> OracleTier:
    """Return the cheapest oracle tier that can falsify a gate of *kind*.

    The kind-to-tier table is owned by the planning module; this resolves
    it through the public :func:`~eawf.kernel.spec.common.assign_oracle_tier`
    with a probe clause naming *kind*, so delivery never keeps a second
    copy of the table.

    Args:
        kind: A :attr:`GateSpec.kind` value.

    Returns:
        The tier the planning mapping assigns to *kind*.

    Raises:
        ValueError: *kind* is not a recognised gate kind.
    """
    probe = ResponseClause(
        observe=ObserveVerb.EXITS,
        object=kind,
        locus=ProofLocus.PYTEST,
        gate_ref=kind,
    )
    return assign_oracle_tier(probe)


def _finding(rule: AuthoringRule, subject_id: str, message: str) -> AuthoringFinding:
    return AuthoringFinding(rule=rule, subject_id=subject_id, message=message)


def _bound_gates(criterion: CriterionSpec, gate_by_id: Mapping[str, GateSpec]) -> list[GateSpec]:
    """Return the gates *criterion* references that resolve, in reference order."""
    return [gate_by_id[ref] for ref in criterion.gate_ids if ref in gate_by_id]


def _effective_clause(criterion: CriterionSpec, bound: list[GateSpec]) -> ResponseClause | None:
    """Return the authored clause, or the one the planning module derives from the gates.

    A gate of unknown kind makes derivation impossible; that gate is
    reported by the oracle rule, so the criterion is treated as clause-free
    here rather than reported twice.
    """
    if criterion.response is not None:
        return criterion.response
    try:
        return response_from_gate(criterion, bound)
    except ValueError:
        return None


def _clause_tier(clause: ResponseClause) -> OracleTier | None:
    """Return the clause's mapped tier, or ``None`` when another rule refuses the clause."""
    try:
        return assign_oracle_tier(clause)
    except ValueError:
        return None


def _gate_reference_findings(
    authoring: CriteriaAuthoring,
    gate_by_id: Mapping[str, GateSpec],
) -> list[AuthoringFinding]:
    """Check that every reference resolves and every gate is owned and listed."""
    findings: list[AuthoringFinding] = []
    criterion_by_id = {criterion.id: criterion for criterion in authoring.criteria}
    for criterion in authoring.criteria:
        findings.extend(
            _finding(
                AuthoringRule.UNRESOLVED_GATE,
                criterion.id,
                f"gate id {ref!r} resolves to no gate in the scope",
            )
            for ref in criterion.gate_ids
            if ref not in gate_by_id
        )
        findings.extend(_gate_coverage_findings(criterion, _bound_gates(criterion, gate_by_id)))
    for gate in authoring.gates:
        owner = criterion_by_id.get(gate.criterion_id)
        if owner is None:
            message = f"owner criterion {gate.criterion_id!r} does not exist"
        elif gate.id not in owner.gate_ids:
            message = f"owner criterion {owner.id!r} does not list this gate"
        else:
            continue
        findings.append(_finding(AuthoringRule.ORPHAN_GATE, gate.id, message))
    return findings


def _gate_coverage_findings(
    criterion: CriterionSpec, bound: list[GateSpec]
) -> list[AuthoringFinding]:
    """Require a gate on every criterion that is not attested, and none on one that is.

    A required deterministic criterion must also hold at least one required
    blocking gate, so an advisory result never reads as a required pass.
    """
    if criterion.evidence_kind == "attested":
        if not criterion.gate_ids:
            return []
        message = "an attested criterion carries no gate"
    elif not criterion.gate_ids:
        message = f"a {criterion.evidence_kind} criterion binds no gate"
    elif (
        criterion.required
        and criterion.evidence_kind == "deterministic"
        and bound
        and not any(gate.required and gate.policy == "block" for gate in bound)
    ):
        message = "a required criterion binds no required blocking gate"
    else:
        return []
    return [_finding(AuthoringRule.UNGATED_CRITERION, criterion.id, message)]


def _oracle_findings(
    authoring: CriteriaAuthoring,
    gate_by_id: Mapping[str, GateSpec],
) -> list[AuthoringFinding]:
    """Check that every gate has an oracle tier and every deterministic gate compiles."""
    findings: list[AuthoringFinding] = []
    criterion_by_id = {criterion.id: criterion for criterion in authoring.criteria}
    for gate in authoring.gates:
        try:
            gate_kind_oracle_tier(gate.kind)
        except ValueError:
            findings.append(
                _finding(
                    AuthoringRule.UNKNOWN_ORACLE,
                    gate.id,
                    f"gate kind {gate.kind!r} maps to no oracle tier",
                )
            )
            continue
        owner = criterion_by_id.get(gate.criterion_id)
        if (
            owner is not None
            and owner.evidence_kind == "deterministic"
            and not _compiles(gate, owner)
        ):
            findings.append(
                _finding(
                    AuthoringRule.UNCOMPILED_GATE,
                    gate.id,
                    f"deterministic gate of kind {gate.kind!r} compiles to no runnable check",
                )
            )
    for criterion in authoring.criteria:
        findings.extend(_criterion_oracle_findings(criterion, _bound_gates(criterion, gate_by_id)))
    return findings


def _compiles(gate: GateSpec, owner: CriterionSpec) -> bool:
    """Return whether *gate* compiles to a runnable check for *owner*."""
    try:
        return compile_gate(gate, criterion=owner) is not None
    except ValueError:
        return False


def _criterion_oracle_findings(
    criterion: CriterionSpec,
    bound: list[GateSpec],
) -> list[AuthoringFinding]:
    """Check the criterion's oracle against its gates and its evidence kind."""
    findings: list[AuthoringFinding] = []
    clause = _effective_clause(criterion, bound)
    tier = None if clause is None else _clause_tier(clause)
    bound_kinds = {gate.kind for gate in bound}
    if clause is not None and clause.gate_ref is not None and clause.gate_ref not in bound_kinds:
        findings.append(
            _finding(
                AuthoringRule.UNKNOWN_ORACLE,
                criterion.id,
                f"response names oracle kind {clause.gate_ref!r} that no bound gate carries",
            )
        )
    authored = criterion.oracle_tier
    if authored is not None and authored != tier:
        findings.append(
            _finding(
                AuthoringRule.UNKNOWN_ORACLE,
                criterion.id,
                f"authored oracle tier {tier_label(authored)} is not the mapped tier",
            )
        )
    judgment = [
        value for value in (tier, authored) if value is not None and value >= OracleTier.T6_APPROVAL
    ]
    if criterion.evidence_kind == "deterministic" and judgment:
        findings.append(
            _finding(
                AuthoringRule.DETERMINISTIC_JURY_ROUTE,
                criterion.id,
                f"a deterministic criterion is routed to {tier_label(max(judgment))}",
            )
        )
    return findings


def _clause_findings(
    authoring: CriteriaAuthoring,
    gate_by_id: Mapping[str, GateSpec],
) -> list[AuthoringFinding]:
    """Check that quantifier, locus, and prose scope agree with the proof."""
    findings: list[AuthoringFinding] = []
    for criterion in authoring.criteria:
        bound = _bound_gates(criterion, gate_by_id)
        clause = _effective_clause(criterion, bound)
        if clause is not None:
            findings.extend(_quantifier_findings(criterion.id, clause, bound))
            findings.extend(_locus_findings(criterion.id, clause))
        if _scope_mismatch(criterion, clause, bound):
            findings.append(
                _finding(
                    AuthoringRule.SCOPE_MISMATCH,
                    criterion.id,
                    "text claims universal scope but every bound gate reads a single site",
                )
            )
    return findings


def _quantifier_findings(
    criterion_id: str,
    clause: ResponseClause,
    bound: list[GateSpec],
) -> list[AuthoringFinding]:
    """Refuse a universal verb witnessed once, and a universal clause no gate can enumerate."""
    messages: list[str] = []
    if clause.observe is ObserveVerb.HOLDS_FOR_ALL and clause.quantifier == "single":
        messages.append("holds_for_all is a universal verb but the clause is quantified single")
    if (
        clause.quantifier == "forall"
        and bound
        and all(gate.kind in SINGLE_SITE_GATE_KINDS for gate in bound)
    ):
        messages.append("a forall clause is bound only to single-site gates")
    return [
        _finding(AuthoringRule.QUANTIFIER_MISMATCH, criterion_id, message) for message in messages
    ]


def _locus_findings(criterion_id: str, clause: ResponseClause) -> list[AuthoringFinding]:
    """Refuse a locus the clause's quantifier or verb cannot be observed at."""
    messages: list[str] = []
    if clause.quantifier == "forall" and clause.locus is not ProofLocus.HYPOTHESIS:
        messages.append(f"a forall clause needs the hypothesis locus, not {clause.locus.value}")
    judged = clause.observe is ObserveVerb.JUDGED
    if judged != (clause.locus in _JUDGMENT_LOCI):
        messages.append(
            f"verb {clause.observe.value} cannot be observed at locus {clause.locus.value}"
        )
    return [_finding(AuthoringRule.LOCUS_MISMATCH, criterion_id, message) for message in messages]


def _scope_mismatch(
    criterion: CriterionSpec,
    clause: ResponseClause | None,
    bound: list[GateSpec],
) -> bool:
    """Return whether universal prose rests only on single-site gates.

    The planning model refuses this pairing only when the clause itself
    names a single-site kind; this checks the gates the criterion actually
    binds, which is what runs.
    """
    if not bound or _UNIVERSAL_SCOPE_RE.search(criterion.text) is None:
        return False
    if clause is not None and clause.quantifier == "forall":
        return False
    return all(gate.kind in SINGLE_SITE_GATE_KINDS for gate in bound)


def _promise_findings(authoring: CriteriaAuthoring) -> list[AuthoringFinding]:
    """Report each promise mapped to no criterion and named by no deferral."""
    if not authoring.promises:
        return []
    covered = {span_id for span_id, covering in authoring.promise_coverage.items() if covering}
    report = coverage_diff(list(authoring.promises), covered, list(authoring.deferrals))
    quotes = {unit.span_id: unit.quote for unit in authoring.promises}
    return [
        _finding(
            AuthoringRule.UNCOVERED_PROMISE,
            span_id,
            f"promise {quotes[span_id][:_QUOTE_PREVIEW_CHARS]!r} maps to no criterion "
            "and no deferral",
        )
        for span_id in report.uncovered
    ]


def _complexity_findings(
    authoring: CriteriaAuthoring,
    *,
    max_criteria: int,
) -> list[AuthoringFinding]:
    """Report a scope carrying more criteria than the ceiling admits."""
    count = len(authoring.criteria)
    if count <= max_criteria:
        return []
    return [
        _finding(
            AuthoringRule.COMPLEXITY_CEILING,
            authoring.scope_id,
            f"scope carries {count} criteria, over the ceiling of {max_criteria}; split it",
        )
    ]


def validate_criteria_authoring(
    authoring: CriteriaAuthoring,
    *,
    max_criteria: int = MAX_CRITERIA_PER_SCOPE,
) -> tuple[AuthoringFinding, ...]:
    """Return every authoring-floor finding for one scope's criterion set.

    Rules run in :class:`AuthoringRule` groups and rows in declaration
    order, so identical input always yields the identical finding list.

    Args:
        authoring: The loaded criterion set.
        max_criteria: The complexity ceiling on criteria per scope; at
            least 1.

    Returns:
        The findings; empty when the set clears the floor.

    Raises:
        ValueError: *max_criteria* is below 1.
    """
    if max_criteria < 1:
        raise ValueError(f"max_criteria must be at least 1, got {max_criteria}")
    gate_by_id = {gate.id: gate for gate in authoring.gates}
    findings = (
        *_gate_reference_findings(authoring, gate_by_id),
        *_oracle_findings(authoring, gate_by_id),
        *_clause_findings(authoring, gate_by_id),
        *_promise_findings(authoring),
        *_complexity_findings(authoring, max_criteria=max_criteria),
    )
    logger.debug(
        f"validate_criteria_authoring scope={authoring.scope_id!r} "
        f"criteria={len(authoring.criteria)} gates={len(authoring.gates)} "
        f"findings={len(findings)}"
    )
    return findings


def _criteria_payload(criteria: Sequence[CriterionSpec]) -> list[dict[str, object]]:
    """Return the digest payload of *criteria*, sorted by id.

    ``oracle_tier`` is left out because it is computed, not authored: a
    validator persisting it in place must not change what was authored.
    """
    ordered = sorted(criteria, key=lambda criterion: criterion.id)
    return [criterion.model_dump(mode="json", exclude={"oracle_tier"}) for criterion in ordered]


class ExecutionContract(_FrozenModel):
    """One gate compiled against the criteria it covers.

    The contract is a runtime projection keyed by the gate id, never plan
    state. It carries its own copies of the covered rows, and its digests
    are recomputed from them, so a receipt keyed on a contract digest is
    keyed on content.
    """

    scope_id: GateIdentityStr
    gate: GateSpec
    criteria: tuple[CriterionSpec, ...] = Field(min_length=1)
    oracle_tier: OracleTier
    check: CheckSpec | None

    @property
    def gate_id(self) -> str:
        """Return the id of the compiled gate."""
        return self.gate.id

    @property
    def criterion_ids(self) -> tuple[str, ...]:
        """Return the ids of the covered criteria, sorted."""
        return tuple(sorted(criterion.id for criterion in self.criteria))

    @property
    def evidence_kind(self) -> CriterionEvidenceKind:
        """Return the owning criterion's evidence kind, which decides reuse."""
        return self._owner().evidence_kind

    def _owner(self) -> CriterionSpec:
        return next(
            criterion for criterion in self.criteria if criterion.id == self.gate.criterion_id
        )

    @model_validator(mode="after")
    def _coverage_is_exact(self) -> Self:
        """Require the owner among the covered rows, each of which lists the gate.

        Raises:
            ValueError: The owner is absent, a covered criterion does not
                reference the gate, or a criterion repeats.
        """
        ids = [criterion.id for criterion in self.criteria]
        if len(set(ids)) != len(ids):
            raise ValueError(f"contract {self.gate.id!r} repeats a covered criterion")
        if self.gate.criterion_id not in ids:
            raise ValueError(f"contract {self.gate.id!r} does not cover its owner criterion")
        strays = sorted(
            criterion.id for criterion in self.criteria if self.gate.id not in criterion.gate_ids
        )
        if strays:
            raise ValueError(f"contract {self.gate.id!r} covers criteria not listing it: {strays}")
        return self

    @model_validator(mode="after")
    def _compiled_shape_matches(self) -> Self:
        """Require the mapped tier, and a runnable check exactly for a deterministic owner.

        Raises:
            ValueError: The tier is not the planning mapping's, a
                deterministic owner has no check (or another owner has
                one), or the check is not named for the gate.
        """
        if self.oracle_tier != gate_kind_oracle_tier(self.gate.kind):
            raise ValueError(f"contract {self.gate.id!r} carries a tier the mapping does not give")
        deterministic = self.evidence_kind == "deterministic"
        if deterministic != (self.check is not None):
            raise ValueError(
                f"contract {self.gate.id!r} needs a runnable check exactly when deterministic"
            )
        if self.check is not None and self.check.name != self.gate.id:
            raise ValueError(f"contract {self.gate.id!r} carries a check named {self.check.name!r}")
        return self

    def criteria_digest(self) -> str:
        """Return the ``sha256:`` digest of the covered criteria."""
        return canonical_digest(_criteria_payload(self.criteria))

    def gate_digest(self) -> str:
        """Return the ``sha256:`` digest of the gate, its compiled check, and its tier."""
        return canonical_digest(
            {
                "gate": self.gate.model_dump(mode="json"),
                "check": None if self.check is None else self.check.model_dump(mode="json"),
                "oracle_tier": int(self.oracle_tier),
            }
        )

    def contract_digest(self) -> str:
        """Return the one ``sha256:`` identity of the whole compiled contract."""
        return canonical_digest(
            {
                "scope_id": self.scope_id,
                "gate_id": self.gate.id,
                "criteria_digest": self.criteria_digest(),
                "gate_digest": self.gate_digest(),
            }
        )


class ExecutionContractSet(_FrozenModel):
    """Every compiled contract of one scope, with the scope-wide digests.

    ``criteria`` holds every criterion of the scope, attested ones
    included, because the frozen criteria revision a binding records covers
    all of them and not only the gated ones.
    """

    scope_id: GateIdentityStr
    criteria: tuple[CriterionSpec, ...] = Field(min_length=1)
    contracts: tuple[ExecutionContract, ...] = ()

    @model_validator(mode="after")
    def _contracts_belong_to_scope(self) -> Self:
        """Require one contract per gate, all compiled for this scope.

        Raises:
            ValueError: A contract names another scope, or a gate id repeats.
        """
        foreign = sorted(item.gate_id for item in self.contracts if item.scope_id != self.scope_id)
        if foreign:
            raise ValueError(f"contracts compiled for another scope: {foreign}")
        gate_ids = [item.gate_id for item in self.contracts]
        if len(set(gate_ids)) != len(gate_ids):
            raise ValueError("a gate is compiled more than once")
        return self

    def contract(self, gate_id: str) -> ExecutionContract:
        """Return the contract compiled for *gate_id*.

        Args:
            gate_id: The gate to look up.

        Returns:
            The compiled contract.

        Raises:
            KeyError: No contract was compiled for *gate_id*.
        """
        for item in self.contracts:
            if item.gate_id == gate_id:
                return item
        raise KeyError(gate_id)

    def criteria_digest(self) -> str:
        """Return the ``sha256:`` digest of the scope's frozen criteria revision."""
        return canonical_digest(_criteria_payload(self.criteria))

    def gate_manifest_digest(self) -> str:
        """Return the ``sha256:`` digest over every compiled gate, by gate id."""
        return canonical_digest({item.gate_id: item.gate_digest() for item in self.contracts})


def compile_execution_contracts(
    authoring: CriteriaAuthoring,
    *,
    max_criteria: int = MAX_CRITERIA_PER_SCOPE,
) -> ExecutionContractSet:
    """Compile a criterion set that clears the authoring floor.

    Each gate becomes one :class:`ExecutionContract` covering every
    criterion that lists it, with the runnable check
    :func:`~eawf.workflow.verify.compile.compile_gate` produces for a
    deterministic owner. The contracts hold deep copies, so later edits to
    the authored rows cannot change a compiled digest.

    Args:
        authoring: The loaded criterion set.
        max_criteria: The complexity ceiling passed to
            :func:`validate_criteria_authoring`.

    Returns:
        The scope's contracts, sorted by gate id.

    Raises:
        CriteriaAuthoringError: The set has at least one authoring finding.
        ValueError: *max_criteria* is below 1.
    """
    findings = validate_criteria_authoring(authoring, max_criteria=max_criteria)
    if findings:
        logger.warning(
            f"compile_execution_contracts status=rejected scope={authoring.scope_id!r} "
            f"findings={len(findings)}"
        )
        raise CriteriaAuthoringError(findings)
    criteria = tuple(
        criterion.model_copy(deep=True)
        for criterion in sorted(authoring.criteria, key=lambda row: row.id)
    )
    contracts = tuple(
        _compile_contract(authoring.scope_id, gate, criteria)
        for gate in sorted(authoring.gates, key=lambda row: row.id)
    )
    logger.debug(
        f"compile_execution_contracts status=ok scope={authoring.scope_id!r} "
        f"contracts={len(contracts)}"
    )
    return ExecutionContractSet(scope_id=authoring.scope_id, criteria=criteria, contracts=contracts)


def _compile_contract(
    scope_id: str,
    gate: GateSpec,
    criteria: tuple[CriterionSpec, ...],
) -> ExecutionContract:
    """Compile one validated gate against the criteria that list it."""
    covered = tuple(criterion for criterion in criteria if gate.id in criterion.gate_ids)
    owner = next(criterion for criterion in covered if criterion.id == gate.criterion_id)
    return ExecutionContract(
        scope_id=scope_id,
        gate=gate.model_copy(deep=True),
        criteria=covered,
        oracle_tier=gate_kind_oracle_tier(gate.kind),
        check=compile_gate(gate, criterion=owner),
    )


__all__ = [
    "MAX_CRITERIA_PER_SCOPE",
    "AuthoringFinding",
    "AuthoringRule",
    "CriteriaAuthoring",
    "CriteriaAuthoringError",
    "ExecutionContract",
    "ExecutionContractSet",
    "compile_execution_contracts",
    "gate_kind_oracle_tier",
    "validate_criteria_authoring",
]
