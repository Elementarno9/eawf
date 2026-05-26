"""Shared building blocks for PhaseSpec / IterSpec / WaveSpec models.

These types are imported by :mod:`eawf.kernel.spec.phase`, :mod:`eawf.kernel.spec.iter`,
:mod:`eawf.kernel.spec.wave` and (later) the research / hypothesis / decision /
audit spec modules. Defining them once here avoids drift between the
seven spec models and keeps the cross-spec citation contract (verdict
ids, brief paths, test refs, file scopes, evidence refs) in a single
authoritative place.
"""

from __future__ import annotations

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
