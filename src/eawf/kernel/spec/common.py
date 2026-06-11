"""Shared building blocks for PhaseSpec / IterSpec / WaveSpec models.

These types are imported by :mod:`eawf.kernel.spec.phase`, :mod:`eawf.kernel.spec.iter`,
:mod:`eawf.kernel.spec.wave` and (later) the research / hypothesis / decision /
audit spec modules. Defining them once here avoids drift between the
seven spec models and keeps the cross-spec citation contract (verdict
ids, brief paths, test refs, file scopes, evidence refs) in a single
authoritative place.
"""

from __future__ import annotations

import re
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


# Canonical artifact kind -> sub-directory under .ea/artifacts/. The map IS the
# placement contract: an artifact of a given kind lives under exactly one
# sub-directory and nowhere else. Mirrors the promote-side `_KIND_SUBDIR` router
# in `eawf.surfaces.cli.commands.draft` so the model boundary and the promoter
# never drift on where a kind files. The EAWF023 placement lint enforces the
# same contract over the whole git-tracked artifact tree.
ARTIFACT_KIND_SUBDIR: dict[str, str] = {
    "research": "research",
    "audit": "audits",
    "plan": "plans",
    "hypothesis": "hypotheses",
    "decision": "decisions",
    "incident": "incidents",
}


def artifact_path_str(kind: str) -> Any:
    """Return an ``Annotated[str, ...]`` validating an artifact path for ``kind``.

    The returned annotation accepts a repo-relative path under the kind's
    canonical ``.ea/artifacts/<subdir>/`` sub-directory whose filename stem
    leads with a ``YYYY-MM-DD-`` date prefix, e.g.
    ``.ea/artifacts/audits/2026-06-11-p30-i14-closeout.md``. A nested
    sub-directory under the kind subdir (e.g. ``research/long-term/``) is
    allowed; the date stem applies to the terminal filename. This is the
    model-boundary twin of the EAWF023 placement lint.

    Args:
        kind: A canonical artifact kind (a key of
            :data:`ARTIFACT_KIND_SUBDIR`).

    Returns:
        An ``Annotated[str, Field(...)]`` type enforcing the kind's path
        contract, suitable as a Pydantic field annotation.

    Raises:
        ValueError: When ``kind`` is not a canonical artifact kind.
    """
    subdir = ARTIFACT_KIND_SUBDIR.get(kind)
    if subdir is None:
        allowed = ", ".join(sorted(ARTIFACT_KIND_SUBDIR))
        raise ValueError(f"unknown artifact kind: {kind!r} (allowed: {allowed})")
    pattern = rf"^\.ea/artifacts/{subdir}/(.+/)?\d{{4}}-\d{{2}}-\d{{2}}-.+\.md$"
    return Annotated[str, Field(min_length=1, pattern=pattern)]


# Repo-relative path to any durable artifact: a file under one of the canonical
# kind sub-directories whose filename stem leads with a YYYY-MM-DD- date prefix.
# The generic (kind-agnostic) twin of :func:`artifact_path_str`; use the factory
# when a field is pinned to one kind, this when any artifact path is acceptable.
ArtifactPathStr = Annotated[
    str,
    Field(
        min_length=1,
        pattern=(
            r"^\.ea/artifacts/(audits|research|plans|hypotheses|decisions|incidents)/"
            r"(.+/)?\d{4}-\d{2}-\d{2}-.+\.md$"
        ),
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


def tier_label(tier: OracleTier) -> str:
    """Return the human ``T<n> <flavor>`` label for an :class:`OracleTier`.

    The member name is ``T<n>_<FLAVOR>`` (e.g. ``T1_STATIC``); this splits it
    into the short ``T<n>`` code and the lowercased flavor word so a renderer
    reads ``T1 static`` rather than the screaming-snake enum name. The minimal
    formatter lives here beside the enum so every surface (the TUI criteria
    tab, future CLI renders) shares one canonical tier-label form.

    Args:
        tier: The oracle tier to label.

    Returns:
        The ``"T<n> <flavor>"`` label, e.g. ``"T7 jury"``.
    """
    code, flavor = tier.name.split("_", 1)
    return f"{code} {flavor.lower()}"


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


#: Gate KIND (the :attr:`GateSpec.kind` / ``CheckKind`` string) -> cheapest
#: oracle tier that can falsify it. The principle: the cheapest deterministic
#: falsifier wins, so static parse / lint / schema / citation checks sit at
#: T1, structural / state / FSM checks at T2, contract / command checks at T4,
#: and golden diffs at T5.
_GATE_KIND_TIER: dict[str, OracleTier] = {
    "file_exists": OracleTier.T1_STATIC,
    "path_glob_nonempty": OracleTier.T1_STATIC,
    "regex_in_file": OracleTier.T1_STATIC,
    "schema_validate": OracleTier.T1_STATIC,
    "citation_resolves": OracleTier.T1_STATIC,
    "criterion_in_diff": OracleTier.T1_STATIC,
    "state_field_equals": OracleTier.T2_STRUCTURAL,
    "transition_coverage": OracleTier.T2_STRUCTURAL,
    "affordance_parity": OracleTier.T2_STRUCTURAL,
    "svg_well_formed": OracleTier.T2_STRUCTURAL,
    "tui_flow": OracleTier.T2_STRUCTURAL,
    "journal_chain": OracleTier.T2_STRUCTURAL,
    "command_exit_zero": OracleTier.T4_CONTRACT,
    "verify_implements": OracleTier.T4_CONTRACT,
    "svg_pixel_diff": OracleTier.T5_GOLDEN,
    "mockup_golden_diff": OracleTier.T5_GOLDEN,
}


def _tier_for_gate_kind(kind: str) -> OracleTier:
    """Map a gate KIND string to the cheapest oracle tier that falsifies it.

    Args:
        kind: The :attr:`GateSpec.kind` value (a ``CheckKind`` family name).

    Returns:
        The cheapest :class:`OracleTier` that can produce a verdict for *kind*.

    Raises:
        ValueError: When *kind* is not a recognised gate kind.
    """
    tier = _GATE_KIND_TIER.get(kind)
    if tier is None:
        raise ValueError(f"unknown gate kind: {kind!r}")
    return tier


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


#: ``kind`` stamped on a criterion the legacy-to-typed converter promotes out of
#: the grandfathered no-op state. Distinct from :data:`GRANDFATHERED_KIND` so a
#: renderer / audit / the readiness legacy-count can tell a converted-and-gated
#: criterion apart from a still-legacy one: the readiness path counts only
#: ``kind == GRANDFATHERED_KIND`` rows as legacy, so a ``converted`` row drops
#: out of that tally and routes through the gated spec path instead.
CONVERTED_KIND = "converted"

#: Default gate kind the converter attaches when the eawf021 parser cannot
#: resolve a richer falsifier from the criterion prose. ``criterion_in_diff``
#: greps a derived pattern across the wave's file scopes, so it falsifies any
#: legacy string without a per-criterion ``argv`` -- the safe hand-authored
#: fallback when the verb/locus parse is ambiguous.
_FALLBACK_GATE_KIND = "criterion_in_diff"

#: Verb-value -> the cheapest deterministic gate KIND that falsifies a clause
#: observing that verb. Drives the gate the converter attaches when the eawf021
#: parser DOES resolve a verb: a ``validates`` / ``matches_pattern`` /
#: ``emits`` / ``renders_token`` clause maps to the static ``regex_in_file``
#: grep, a ``file_matches`` clause to the ``criterion_in_diff`` file grep.
#: Verbs with no cheap deterministic single-file falsifier (``returns`` /
#: ``raises`` / ``exits`` / ``judged`` / ...) fall through to
#: :data:`_FALLBACK_GATE_KIND` so every converted criterion still carries a
#: runnable gate. The map deliberately emits only the two argv-free file-grep
#: kinds: an ``argv``-bearing kind (``command_exit_zero``) would route the
#: derived grep through the L0 argv allowlist, which does not admit ``grep``.
_VERB_VALUE_TO_GATE_KIND: dict[str, str] = {
    "validates": "regex_in_file",
    "matches_pattern": "regex_in_file",
    "emits": "regex_in_file",
    "file_matches": "criterion_in_diff",
    "renders_token": "regex_in_file",
}

#: Default proof locus the converter pins on a converted criterion's response
#: clause when the parser resolved no explicit locus. ``SOURCE`` pairs with the
#: ``criterion_in_diff`` / ``regex_in_file`` file-grep gates the fallback emits.
_DEFAULT_LOCUS_VALUE = "source"

#: Default observe verb when the parser resolves no verb. ``file_matches`` pairs
#: with the ``criterion_in_diff`` fallback gate (a file-grep contract).
_DEFAULT_VERB_VALUE = "file_matches"


def legacy_criterion_pattern(text: str) -> str:
    """Return a regex pattern derived from a legacy criterion string.

    The converter's fallback gate (``criterion_in_diff`` / ``regex_in_file``)
    needs a concrete ``pattern`` to grep. The longest alphanumeric token in the
    criterion text is the most distinctive keyword to anchor on; it is regex-
    escaped so punctuation in the token cannot corrupt the pattern. A string
    with no alphanumeric run yields a catch-all ``.`` so the gate still
    compiles (an empty pattern would raise at gate construction).

    Args:
        text: The legacy success-criterion string.

    Returns:
        A regex pattern string anchoring on the criterion's most distinctive
        token, or ``"."`` when the text carries no alphanumeric run.
    """
    tokens: list[str] = re.findall(r"[A-Za-z0-9_]+", text)
    if not tokens:
        return "."
    longest = max(tokens, key=len)
    return re.escape(longest)


def convert_legacy_criterion(
    text: str,
    *,
    index: int,
    file_scopes: list[str],
) -> tuple[CriterionSpec, GateSpec]:
    """Convert a grandfathered legacy criterion STRING into a typed, gated pair.

    This is the legacy-to-typed converter: it lifts a free-form
    success-criterion string out of the grandfathered no-op state
    (:data:`GRANDFATHERED_KIND`, no gates) into a typed
    :class:`CriterionSpec` carrying a real :class:`ResponseClause` plus an
    attached, falsifying :class:`GateSpec`. The eawf021 verb/locus parser
    (:func:`eawf.platform.lint.eawf021_measurable_criterion.extract_observation`)
    seeds the response clause; where the parse is ambiguous (no recognised verb
    or locus) the converter hand-authors a ``criterion_in_diff`` gate whose
    pattern is the criterion's most distinctive token
    (:func:`legacy_criterion_pattern`).

    The gate kind is chosen from the parsed verb via
    :data:`_VERB_VALUE_TO_GATE_KIND`, falling back to
    :data:`_FALLBACK_GATE_KIND`. The response clause's ``gate_ref`` names that
    gate kind, so :func:`assign_oracle_tier` derives the criterion's oracle tier
    from the cheapest deterministic falsifier of the attached gate. The returned
    criterion carries ``kind == CONVERTED_KIND`` and
    ``evidence_kind == "deterministic"``, so the readiness path no longer counts
    it as legacy and the deterministic floor runs its gate live.

    The returned pair is NOT yet tier-resolved: pass it through
    :func:`validate_criterion_gate_refs` (which computes + persists
    ``oracle_tier`` in place) before scoring -- mirroring the close path that
    runs the same validator over state-loaded criteria.

    Args:
        text: The legacy success-criterion string (1-500 chars).
        index: 1-based position of the criterion within its wave; drives the
            ``CR-<index>`` / ``GATE-<index>`` ids.
        file_scopes: The wave's file scopes the file-grep gates resolve their
            ``pattern`` against. Must be non-empty so the gate has somewhere to
            look.

    Returns:
        A ``(criterion, gate)`` tuple. The criterion's ``gate_ids`` references
        the gate id, and the gate's ``criterion_id`` references the criterion
        id, so :func:`validate_criterion_gate_refs` accepts the pair.

    Raises:
        ValueError: When *file_scopes* is empty (a file-grep gate needs at
            least one scope to resolve its pattern against).
    """
    from eawf.platform.lint.eawf021_measurable_criterion import extract_observation

    if not file_scopes:
        raise ValueError(f"convert_legacy_criterion requires file_scopes: text={text!r}")

    verb_value, locus_value = extract_observation(text)
    verb_value = verb_value or _DEFAULT_VERB_VALUE
    locus_value = locus_value or _DEFAULT_LOCUS_VALUE
    gate_kind = _VERB_VALUE_TO_GATE_KIND.get(verb_value, _FALLBACK_GATE_KIND)

    criterion_id = f"CR-{index:02d}"
    gate_id = f"GATE-{index:02d}"
    pattern = legacy_criterion_pattern(text)

    response = ResponseClause(
        observe=ObserveVerb(verb_value),
        object=text[:200],
        locus=ProofLocus(locus_value),
        gate_ref=gate_kind,
    )
    measurable_signal = text[:300] if len(text) >= 20 else GRANDFATHERED_SIGNAL
    criterion = CriterionSpec(
        id=criterion_id,
        text=text,
        kind=CONVERTED_KIND,
        acceptance_style="binary",
        evidence_kind="deterministic",
        gate_ids=[gate_id],
        quality_dimension=QualityDimension.FUNCTIONAL_SUITABILITY,
        measurable_signal=measurable_signal,
        response=response,
    )
    gate = GateSpec(
        id=gate_id,
        criterion_id=criterion_id,
        kind=gate_kind,
        args=_gate_args_for_kind(gate_kind, pattern=pattern, file_scopes=file_scopes),
        policy="block",
        cadence="every-wave",
    )
    return criterion, gate


def _gate_args_for_kind(
    gate_kind: str,
    *,
    pattern: str,
    file_scopes: list[str],
) -> dict[str, Any]:
    """Return the hand-authored ``args`` body for a converter-emitted gate kind.

    Each deterministic gate kind the converter emits has its own ``args``
    shape; this keeps the per-kind body in one place so
    :func:`convert_legacy_criterion` stays a flat assembler:

    * ``criterion_in_diff`` -> ``{criterion, pattern, file_scopes}`` (file grep
      across the wave's scopes).
    * ``regex_in_file`` -> ``{path, pattern}`` (single-file grep; the first
      file scope is the path).

    Args:
        gate_kind: The gate kind to build args for.
        pattern: The grep pattern derived from the criterion text.
        file_scopes: The wave's file scopes (non-empty).

    Returns:
        The per-kind ``args`` dict ready to attach to a :class:`GateSpec`.
    """
    if gate_kind == "regex_in_file":
        return {"path": file_scopes[0], "pattern": pattern}
    return {"criterion": pattern, "pattern": pattern, "file_scopes": list(file_scopes)}


def backfill_legacy_criteria(
    legacy_texts: list[str],
    *,
    file_scopes: list[str],
) -> tuple[list[CriterionSpec], list[GateSpec]]:
    """Convert a wave's full legacy success-criteria list into typed, gated rows.

    The batch entry point the backfill tool drives: maps each grandfathered
    string through :func:`convert_legacy_criterion`, then runs the resulting
    rows through :func:`validate_criterion_gate_refs` so every criterion's
    ``oracle_tier`` is computed + persisted (and the cross-reference integrity
    is asserted) exactly as the close path does. The returned criteria carry
    ``kind == CONVERTED_KIND`` -- so the legacy count over the returned set is
    ZERO -- and a populated, gate-kind-derived ``oracle_tier``.

    Args:
        legacy_texts: The wave's free-form success-criterion strings.
        file_scopes: The wave's file scopes the file-grep gates resolve
            against. Must be non-empty.

    Returns:
        A ``(criteria, gates)`` tuple. Every criterion is typed + gated +
        tier-resolved; none carry ``kind == GRANDFATHERED_KIND``.

    Raises:
        ValueError: When *file_scopes* is empty, or a converted criterion/gate
            pair fails cross-reference / tier validation.
    """
    criteria: list[CriterionSpec] = []
    gates: list[GateSpec] = []
    for position, text in enumerate(legacy_texts, start=1):
        criterion, gate = convert_legacy_criterion(
            text,
            index=position,
            file_scopes=file_scopes,
        )
        criteria.append(criterion)
        gates.append(gate)
    validate_criterion_gate_refs(criteria, gates)
    return criteria, gates


class SourceUnit(_StrictModel):
    """One atomic clause/sentence span extracted from a source brief.

    The anti-drift generator (FS11) splits a brief into ``SourceUnit``
    rows BEFORE any criteria are authored, so the coverage diff is
    computed over the brief's own spans rather than over an LLM's
    self-report of what it dropped. ``span_id`` is a stable extraction
    id (``U-007``) and ``char_offset`` is the 0-based start offset of the
    span in the original brief text, so a finding can be traced back to
    the exact source location.
    """

    span_id: Annotated[str, Field(min_length=1)]
    quote: Annotated[str, Field(min_length=1, max_length=1000)]
    char_offset: Annotated[int, Field(ge=0)]


class DeferredDeliverable(_StrictModel):
    """An explicitly deferred source span with a recorded target.

    A span that the planner consciously chooses NOT to cover in the
    current wave set is suppressed from the ``uncovered`` finding list
    only when an authored ``DeferredDeliverable`` names where the work
    is filed (``target`` -> phase / wave / backlog id) and why
    (``reason``). The 20-char ``reason`` floor forces a real rationale:
    the TUI brief-criteria-drift incident dropped its rich digit-map
    detail with neither a criterion nor a deferral, so the gap went
    permanently untracked.
    """

    span_id: Annotated[str, Field(min_length=1)]
    reason: Annotated[str, Field(min_length=20, max_length=500)]
    target: Annotated[str, Field(min_length=1)]


class CoverageReport(_StrictModel):
    """Deterministic coverage diff of source spans against criteria.

    ``covered`` lists span ids mapped to at least one emitted criterion;
    ``deferred`` carries the explicit :class:`DeferredDeliverable` rows;
    ``uncovered`` lists span ids that map to neither -- each a hard
    finding. The diff is computed over :class:`SourceUnit` ids, never
    over an LLM's claim that it dropped nothing.
    """

    covered: list[str] = Field(default_factory=list)
    deferred: list[DeferredDeliverable] = Field(default_factory=list)
    uncovered: list[str] = Field(default_factory=list)


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


def validate_criterion_gate_refs(
    criteria: list[CriterionSpec],
    gates: list[GateSpec],
    *,
    allow_computed_tier: bool = False,
) -> None:
    """Reject a criterion/gate set whose cross-references do not resolve.

    Enforces referential integrity between a wave's typed criteria and
    its gate rows at the close-mutation boundary, where the
    grandfathered common case (empty ``gate_ids`` + empty ``gates``) is a
    deliberate no-op: a wave whose criteria carry no gate references and
    that owns no gate rows has nothing to reject, so every live and
    migration-grandfathered criterion passes cleanly. The checks fire
    only when a wave author actually attaches gate references:

    1. Every :attr:`CriterionSpec.gate_ids` entry resolves to a
       :attr:`GateSpec.id` in *gates*.
    2. Every :attr:`GateSpec.criterion_id` resolves back to a
       :attr:`CriterionSpec.id` in *criteria*.
    3. A ``deterministic``-kind criterion's gate compiles
       (:func:`eawf.workflow.verify.compile.compile_gate` returns a
       non-``None`` runnable spec) -- an orphan deterministic gate that
       cannot compile would silently never falsify the criterion.

    Author-set tier rejection + server-side compute: the tier is owned by
    :func:`assign_oracle_tier`, never authored on input, so a non-``None``
    :attr:`CriterionSpec.oracle_tier` on an INPUT criterion (the spec-body
    parse / ``spec.sync`` path) indicates a malformed spec and is rejected.
    After that check, every criterion that carries a :class:`ResponseClause`
    has its tier computed in place from the clause -- ``JUDGED`` + non-human
    locus computes ``T7_JURY``, a ``command_exit_zero`` ``gate_ref`` computes
    ``T4_CONTRACT`` per the gate-kind tier map -- so ``JUDGED`` becomes the
    only path to the jury and the value is authoritative rather than
    vaporware ``None``.

    Because the function PERSISTS the computed tier in place, a later
    re-validation of the same criteria (the close path re-runs this over
    state-loaded criteria that already carry the tier ``spec.sync``
    computed) would otherwise trip the author-set guard on its own output.
    ``allow_computed_tier`` resolves that: when ``True`` a non-``None`` tier
    is accepted iff it equals the value this function recomputes from the
    response (a tier IT persisted), and still rejected when it differs (a
    corrupted or injected tier). The strict default keeps the input
    boundary (``spec.sync``) rejecting any author-set tier.

    Args:
        criteria: The wave's typed criterion rows. Each is mutated in
            place so its computed ``oracle_tier`` is populated.
        gates: The wave's typed gate rows.
        allow_computed_tier: When ``True`` (the close re-validation path) a
            non-``None`` ``oracle_tier`` that matches the recomputed tier is
            accepted as this function's own persisted output rather than
            rejected as author-set. Defaults to ``False`` for the input
            boundary, which rejects any non-``None`` tier.

    Raises:
        ValueError: When a criterion references an unknown gate id, a
            gate references an unknown criterion id, a deterministic
            criterion's gate fails to compile, a criterion carries an
            author-set ``oracle_tier`` (a non-``None`` tier that is not an
            accepted recompute match), or a criterion's response clause
            is malformed (e.g. a ``JUDGED`` clause with an empty
            ``jury_reason`` or a ``gate_ref`` naming an unknown gate kind).
    """
    # Local import keeps the module-level layer thin and avoids a cycle:
    # the compile layer imports CriterionSpec / GateSpec from this module.
    from eawf.workflow.verify.compile import compile_gate

    criterion_by_id = {c.id: c for c in criteria}
    gate_ids = {g.id for g in gates}

    for criterion in criteria:
        computed = (
            assign_oracle_tier(criterion.response) if criterion.response is not None else None
        )
        if criterion.oracle_tier is not None and not (
            allow_computed_tier and criterion.oracle_tier == computed
        ):
            raise ValueError(f"oracle_tier must not be author-set: criterion={criterion.id!r}")
        if computed is not None:
            criterion.oracle_tier = computed
        for ref in criterion.gate_ids:
            if ref not in gate_ids:
                raise ValueError(f"criterion {criterion.id!r} references unknown gate id: {ref!r}")

    for gate in gates:
        owner = criterion_by_id.get(gate.criterion_id)
        if owner is None:
            raise ValueError(
                f"gate {gate.id!r} references unknown criterion id: {gate.criterion_id!r}"
            )
        is_deterministic = owner.evidence_kind == "deterministic"
        if is_deterministic and compile_gate(gate, criterion=owner) is None:
            raise ValueError(
                f"deterministic gate {gate.id!r} for criterion {owner.id!r} does not compile"
            )


def _rebuild_state_models() -> None:
    """Resolve the ``list[CriterionSpec]`` + ``list[GateSpec]`` forward refs on :class:`Wave`.

    :attr:`eawf.kernel.state.models.Wave.success_criteria` annotates
    ``list[CriterionSpec]`` and :attr:`eawf.kernel.state.models.Wave.gates`
    annotates ``list[GateSpec]``, but ``state.models`` cannot import either
    type (this module imports ``IdStr`` from ``state.models``, so a top-level
    import there would be a cycle). The forward references are therefore
    resolved here, after both :class:`CriterionSpec` and :class:`GateSpec` are
    defined: the function injects the types into the ``state.models`` namespace
    and rebuilds :class:`Wave` so Pydantic can compile the field schemas.
    Running it from this module (rather than the bottom of ``state.models``) is
    cycle-safe regardless of which module the import chain enters first, because
    by the time this runs both modules are fully defined.
    """
    from eawf.kernel.state import models as _state_models

    _state_models.CriterionSpec = CriterionSpec  # type: ignore[attr-defined]
    _state_models.GateSpec = GateSpec  # type: ignore[attr-defined]
    _state_models.Wave.model_rebuild()


# Resolve the Wave forward reference eagerly on import of this module. The
# call is idempotent (a second rebuild is a no-op) so it is safe even when
# ``state.models`` has already triggered it.
_rebuild_state_models()
