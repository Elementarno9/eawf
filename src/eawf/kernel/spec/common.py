"""Shared building blocks for PhaseSpec / IterSpec / WaveSpec models.

These types are imported by :mod:`eawf.kernel.spec.phase`, :mod:`eawf.kernel.spec.iter`,
:mod:`eawf.kernel.spec.wave` and (later) the research / hypothesis / decision /
audit spec modules. Defining them once here avoids drift between the
seven spec models and keeps the cross-spec citation contract (verdict
ids, brief paths, test refs, file scopes, evidence refs) in a single
authoritative place.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eawf.kernel.state.models import IdStr


class _StrictModel(BaseModel):
    """Base model that forbids unknown keys.

    Per AGENTS rule 2 every YAML/JSON ingestion model uses
    ``ConfigDict(extra="forbid")`` so unrecognised keys fail at load
    time rather than silently drifting through the pipeline.
    """

    model_config = ConfigDict(extra="forbid")


# Verdict citation id pattern: V12, V12-RC3, D17, R5, H03-12.
#
#   V = verdict (cluster brief or earlier research)
#   D = decision (operator-ratified D# in a brief's §4 matrix)
#   R = recommendation (long-term-features long-term R# in §"Final picks")
#   H = hypothesis id (per-state H<NN>-<NN>)
VerdictIdStr = Annotated[
    str,
    Field(pattern=r"^[VDRH]\d+(-[A-Z0-9]+)?$"),
]


# Repo-relative path beneath .ea/local/research/ OR .ea/artifacts/research/.
# Local drafts and promoted artifacts are both addressable so cluster briefs
# can cite each other across the local-draft / promoted-artifact boundary.
BriefPathStr = Annotated[
    str,
    Field(
        min_length=1,
        pattern=r"^\.ea/(local|artifacts)/research/.+\.md$",
    ),
]


# Repo-relative path under tests/. Extension is intentionally loose so the
# same TestRef can point at .py, .svg snapshots, .json fixtures, .md golden
# files, .txt diff baselines, asciinema casts, or future test artefacts.
TestRef = Annotated[
    str,
    Field(
        min_length=1,
        pattern=r"^tests/.+$",
    ),
]


# Repo-relative path under src/, tools/, .ea/, docs/, build/, or tests/.
# The union covers every directory a spec's file_scope can legitimately
# touch; paths outside it indicate the spec is over-reaching its
# project-tree boundary.
FileScopeRef = Annotated[
    str,
    Field(
        min_length=1,
        pattern=r"^(src|tools|\.ea|docs|build|tests)/.+$",
    ),
]


class VerdictCitation(_StrictModel):
    """One verdict citation tying a spec back to a research brief.

    Used by PhaseSpec / IterSpec / WaveSpec / DecisionSpec to record
    which prior verdict, decision, recommendation, or hypothesis the
    spec implements. The pair ``(verdict_id, brief)`` is the addressable
    unit; ``line`` and ``note`` are optional annotations.
    """

    verdict_id: VerdictIdStr
    brief: BriefPathStr
    line: int | None = Field(default=None, ge=1)
    note: str | None = None


# Canonical evidence-reference vocabulary shared across the spec layer
# (HypothesisSpec.evidence_chain, DecisionSpec citations, EvidenceRecord
# refs) AND the agent-report layer (AgentReportEvidenceRef). Defining it
# once here means downstream models import the same Literal — the
# agent-report kind set is a strict subset (equal) by construction
# rather than by convention.
#
#   "audit"        -> audit URN
#   "artifact"     -> artifact id / URN
#   "decision"     -> decision URN (urn:eawf:v1:decision:OWNER/ID)
#   "store_record" -> store-record URN
#   "external_url" -> external http(s) URL
EvidenceKind = Literal[
    "audit",
    "artifact",
    "decision",
    "store_record",
    "external_url",
]


class EvidenceRef(_StrictModel):
    """One row of a HypothesisSpec.evidence_chain.

    Slim by design: the audit-DSL runner walks evidence chains looking
    for a typed reference (audit URN, artifact id, decision URN, store
    record URN, or external URL) plus a short summary. The full
    evidence document lives behind the ``ref``, not inlined here.
    """

    kind: EvidenceKind
    ref: str
    summary: str = Field(min_length=1, max_length=400)


# Criterion verification flavor — how a CriterionSpec is checked.
#
# Distinct from :data:`EvidenceKind` (which classifies *what* a
# reference points at). This Literal classifies *how* a criterion's
# evidence is gathered when the readiness compute (W06) and the
# compile-gate (W08) score it:
#
#   "deterministic" -> an automated check (test exit code, regex match,
#                      schema validation) that produces a bit answer.
#   "jury"          -> a vote of multiple agent reviewers; the
#                      minority-veto policy lives in the gate machinery.
#   "attested"      -> a human operator signs off; the attestation is
#                      stored as a typed Decision row.
CriterionEvidenceKind = Literal[
    "deterministic",
    "jury",
    "attested",
]


# How a CriterionSpec is scored — binary (pass / fail) or graded
# (a continuous score the gate machinery thresholds).
CriterionAcceptanceStyle = Literal["binary", "graded"]


# How a GateSpec failure is escalated — block (hard stop), warn
# (advise but proceed), or advisory (record-only, never gates).
GatePolicy = Literal["block", "warn", "advisory"]


# When a GateSpec runs — per wave, per iter, per phase, on ship CI,
# or only when invoked by hand.
GateCadence = Literal[
    "every-wave",
    "every-iter",
    "every-phase",
    "ship",
    "manual",
]


class OracleTier(IntEnum):
    """Ordered oracle tiers; lower = cheaper + more deterministic.

    The runner MUST exhaust lower tiers before T7. IntEnum so ``min`` / ``<``
    order the escalation.
    """

    T1_STATIC = 1
    T2_STRUCTURAL = 2
    T3_SNAPSHOT = 3
    T4_CONTRACT = 4
    T5_GOLDEN = 5
    T6_APPROVAL = 6
    T7_JURY = 7


class ObserveVerb(StrEnum):
    """The EARS ``shall <observe>`` verb a response clause asserts."""

    RETURNS = "returns"
    RAISES = "raises"
    HOLDS_FOR_ALL = "holds_for_all"
    EXITS = "exits"
    EMITS = "emits"
    VALIDATES = "validates"
    MATCHES_PATTERN = "matches_pattern"
    TRANSITIONS_TO = "transitions_to"
    RENDERS_TOKEN = "renders_token"
    TRIGGERS_ACTION = "triggers_action"
    FILE_MATCHES = "file_matches"
    JUDGED = "judged"


class ProofLocus(StrEnum):
    """Where a response clause's proof is observed."""

    PYTEST = "pytest"
    HYPOTHESIS = "hypothesis"
    CLI_EXIT = "cli_exit"
    LOG_CAPTURE = "log_capture"
    SCHEMA = "schema"
    SOURCE = "source"
    STATE_JSON = "state_json"
    TUI_SNAPSHOT = "tui_snapshot"
    TUI_PILOT = "tui_pilot"
    GOLDEN = "golden"
    HUMAN = "human"
    JURY = "jury"


class QualityDimension(StrEnum):
    """ISO-25010:2023 product-quality characteristic a criterion targets.

    Six members are the pre-existing set (relocated from wave.py); four
    are added for the full 2023 characteristic set. OPERABILITY is retained
    for back-compat so existing WaveBehavior rows keep validating.
    """

    FUNCTIONAL_SUITABILITY = "functional_suitability"
    PERFORMANCE_EFFICIENCY = "performance_efficiency"
    INTERACTION_CAPABILITY = "interaction_capability"
    RELIABILITY = "reliability"
    SECURITY = "security"
    OPERABILITY = "operability"
    COMPATIBILITY = "compatibility"
    MAINTAINABILITY = "maintainability"
    FLEXIBILITY = "flexibility"
    SAFETY = "safety"


class ResponseClause(_StrictModel):
    """The EARS ``shall <response>`` clause, typed.

    ``jury_reason`` is required iff ``observe`` is ``JUDGED`` — the
    CriterionSpec validator enforces that, since the clause is only
    meaningful in the context of its owning criterion.
    """

    observe: ObserveVerb
    object: Annotated[str, Field(min_length=1, max_length=200)]
    locus: ProofLocus
    expected: str | None = None
    quantifier: Literal["single", "forall"] = "single"
    gate_ref: IdStr | None = None
    jury_reason: str | None = None


_VERB_TIER: dict[ObserveVerb, OracleTier] = {
    ObserveVerb.VALIDATES: OracleTier.T1_STATIC,
    ObserveVerb.MATCHES_PATTERN: OracleTier.T1_STATIC,
    ObserveVerb.EXITS: OracleTier.T2_STRUCTURAL,
    ObserveVerb.EMITS: OracleTier.T2_STRUCTURAL,
    ObserveVerb.TRANSITIONS_TO: OracleTier.T2_STRUCTURAL,
    ObserveVerb.TRIGGERS_ACTION: OracleTier.T2_STRUCTURAL,
    ObserveVerb.RENDERS_TOKEN: OracleTier.T3_SNAPSHOT,
    ObserveVerb.RETURNS: OracleTier.T4_CONTRACT,
    ObserveVerb.RAISES: OracleTier.T4_CONTRACT,
    ObserveVerb.HOLDS_FOR_ALL: OracleTier.T4_CONTRACT,
    ObserveVerb.FILE_MATCHES: OracleTier.T5_GOLDEN,
}


def _tier_for_gate_kind(gate_ref: IdStr) -> OracleTier:
    """Map a gate reference to its oracle tier.

    Stub until the run_oracle wave supplies the real gate-kind -> tier map.

    Raises:
        ValueError: always, until the gate-kind map lands.
    """
    raise ValueError(f"unknown gate kind: {gate_ref!r}")


def assign_oracle_tier(r: ResponseClause) -> OracleTier:
    """Total: verb -> cheapest tier; JUDGED is the only path to T6/T7.

    Raises:
        ValueError: observe==JUDGED with empty jury_reason; or
            quantifier==forall with locus != HYPOTHESIS; or gate_ref names
            an unknown gate kind.
    """
    if r.gate_ref is not None:
        return _tier_for_gate_kind(r.gate_ref)
    if r.observe in _VERB_TIER:
        if r.quantifier == "forall" and r.locus is not ProofLocus.HYPOTHESIS:
            raise ValueError(f"forall response must use hypothesis locus: object={r.object!r}")
        return _VERB_TIER[r.observe]
    if r.observe is ObserveVerb.JUDGED:
        if not r.jury_reason:
            raise ValueError("judged response requires jury_reason (auditable fallthrough)")
        return OracleTier.T6_APPROVAL if r.locus is ProofLocus.HUMAN else OracleTier.T7_JURY
    raise ValueError(f"unhandled observe verb: {r.observe!r}")


class CriterionSpec(_StrictModel):
    """One success-criterion row attached to a wave / iter / phase.

    The v0.4 spec layer types the criterion separately from the
    free-form ``Wave.success_criteria: list[str]`` field on the state
    model. The state-model field stays string-shaped for v0.4.0 so the
    existing roadmap / planner / dispatch surfaces are not perturbed;
    downstream waves (W06 readiness compute, W08 compile-gate, W11
    waivers) operate on :class:`CriterionSpec` once the field migrates
    in a later release.

    The ``gate_ids`` list addresses :class:`GateSpec` rows by id; the
    spec layer does not enforce referential integrity (the W08
    compile-gate does that when both lists are co-resident).
    """

    id: IdStr
    text: Annotated[str, Field(min_length=1, max_length=500)]
    kind: str
    acceptance_style: CriterionAcceptanceStyle
    evidence_kind: CriterionEvidenceKind
    gate_ids: list[IdStr] = Field(default_factory=list)
    required: bool = True
    waiver_reason: str | None = None
    quality_dimension: QualityDimension
    measurable_signal: Annotated[str, Field(min_length=20, max_length=300)]
    response: ResponseClause | None = None
    oracle_tier: OracleTier | None = None

    @model_validator(mode="after")
    def _judged_requires_reason(self) -> CriterionSpec:
        """Require a jury_reason on every JUDGED response clause.

        Raises:
            ValueError: when the response observes JUDGED but carries no
                jury_reason (an auditable fallthrough requires a reason).
        """
        if (
            self.response is not None
            and self.response.observe is ObserveVerb.JUDGED
            and not self.response.jury_reason
        ):
            raise ValueError("judged response requires jury_reason")
        return self


#: Sentinel ``kind`` for a criterion synthesised from a free-form legacy
#: success-criterion string (operator ``--success`` input or a pre-typed
#: on-disk wave). Distinguishes a grandfathered row from an authored typed
#: criterion so renderers + audits can surface the difference.
GRANDFATHERED_KIND = "legacy"

#: Fallback ``measurable_signal`` for a legacy string too short to clear the
#: 20-char floor. Lets a one-word legacy criterion grandfather without
#: failing the :class:`CriterionSpec` ``measurable_signal`` bound.
GRANDFATHERED_SIGNAL = "grandfathered legacy criterion"


def grandfather_criterion(text: str, *, index: int) -> CriterionSpec:
    """Build a grandfathered :class:`CriterionSpec` from a legacy string.

    The producer chain (CLI ``wave plan --success``, daemon ``add_wave``,
    ``roadmap revise --add-wave``) accepts free-form operator strings; this
    helper wraps each into the typed criterion shape so the typed
    ``Wave.success_criteria`` field can accept it. The same shape is mirrored
    by the ``1.6 -> 1.7`` state migration so a migrated on-disk criterion and a
    freshly-planned one are indistinguishable.

    The id is ``CR-<index>`` zero-padded to two digits (e.g. ``CR-01``). The
    ``measurable_signal`` is the text truncated to the 300-char cap when it
    already clears the 20-char floor, else the :data:`GRANDFATHERED_SIGNAL`
    fallback so a short legacy string still validates.

    Args:
        text: The legacy success-criterion string (1-500 chars).
        index: 1-based position of the criterion within its wave.

    Returns:
        A :class:`CriterionSpec` with ``kind == GRANDFATHERED_KIND``.
    """
    measurable_signal = text[:300] if len(text) >= 20 else GRANDFATHERED_SIGNAL
    return CriterionSpec(
        id=f"CR-{index:02d}",
        text=text,
        kind=GRANDFATHERED_KIND,
        acceptance_style="binary",
        evidence_kind="attested",
        quality_dimension=QualityDimension.FUNCTIONAL_SUITABILITY,
        measurable_signal=measurable_signal,
    )


class GateSpec(_StrictModel):
    """One gate row that scores a :class:`CriterionSpec` at some cadence.

    The ``kind`` field names the check family
    (``command_exit_zero``, ``regex_match``, ``schema_validate``, etc.)
    and ``args`` carries the per-kind arguments. The spec layer does
    not validate the full ``args`` shape — that is the gate-runner
    subsystem's responsibility (W08 lands per-kind args validators).
    The one exception is the ``argv`` vector on argv-bearing kinds
    (``command_exit_zero`` today): an
    ``@model_validator`` routes ``args["argv"]`` through the L0
    argv-policy at construction time so a malformed or shell-deny
    argv cannot reach the spec layer regardless of which builder
    constructed the row. The same policy fires again at spec-promote
    persistence via
    :func:`eawf.kernel.spec.promotion.validate_argv_gates` — defense
    in depth across the parse-time and persistence-time seams.

    ``timeout_s`` is ``None`` by default so a kind that has a class
    default (e.g. command_exit_zero defaults to 30s) does not need an
    explicit override.
    """

    id: IdStr
    criterion_id: IdStr
    kind: str
    args: dict[str, Any] = Field(default_factory=dict)
    policy: GatePolicy
    cadence: GateCadence
    required: bool = True
    timeout_s: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _argv_passes_l0_policy(self) -> GateSpec:
        """Validate ``args['argv']`` through the L0 argv-policy on argv-bearing kinds.

        Defense-in-depth companion to
        :func:`eawf.kernel.spec.promotion.validate_argv_gates`: catches
        a bad argv at construction time so it never reaches the
        promote-time check. Skips silently when ``kind`` is not in
        :data:`eawf.kernel.spec.promotion.ARGV_BEARING_GATE_KINDS` so
        non-argv gates (``regex_match``, ``schema_validate``, ...) are
        unaffected.

        Raises:
            ValueError: When ``kind`` requires an ``argv`` vector and
                ``args['argv']`` is missing, mis-shaped, or rejected by
                the L0 policy. Pydantic wraps this into
                :class:`pydantic.ValidationError` at the ``model_validate``
                boundary.
        """
        # Local import keeps the module-level layer thin and avoids a
        # circular import (``promotion`` itself imports :class:`GateSpec`).
        from eawf.kernel.spec.promotion import (
            ARGV_BEARING_GATE_KINDS,
            DEFAULT_GATE_ARGV_ALLOWLIST,
        )
        from eawf.runtime.sandbox.argv_policy import (
            ArgvPolicyError,
            validate_gate_argv,
        )

        if self.kind not in ARGV_BEARING_GATE_KINDS:
            return self
        argv = self.args.get("argv")
        if argv is None:
            raise ValueError(f"gate {self.id!r} kind={self.kind!r} missing required args['argv']")
        try:
            validate_gate_argv(argv, allowlist=list(DEFAULT_GATE_ARGV_ALLOWLIST))
        except ArgvPolicyError as exc:
            raise ValueError(f"gate {self.id!r} argv rejected by L0 policy: {exc}") from exc
        return self


def _rebuild_state_models() -> None:
    """Resolve the ``list[CriterionSpec]`` forward ref on :class:`Wave`.

    :attr:`eawf.kernel.state.models.Wave.success_criteria` annotates
    ``list[CriterionSpec]`` but ``state.models`` cannot import
    :class:`CriterionSpec` (this module imports ``IdStr`` from ``state.models``,
    so a top-level import there would be a cycle). The forward reference is
    therefore resolved here, after :class:`CriterionSpec` is defined: the
    function injects the type into the ``state.models`` namespace and rebuilds
    :class:`Wave` so Pydantic can compile the field schema. Running it from
    this module (rather than the bottom of ``state.models``) is cycle-safe
    regardless of which module the import chain enters first, because by the
    time this runs both modules are fully defined.
    """
    from eawf.kernel.state import models as _state_models

    _state_models.CriterionSpec = CriterionSpec  # type: ignore[attr-defined]
    _state_models.Wave.model_rebuild()


# Resolve the Wave forward reference eagerly on import of this module. The
# call is idempotent (a second rebuild is a no-op) so it is safe even when
# ``state.models`` has already triggered it.
_rebuild_state_models()
