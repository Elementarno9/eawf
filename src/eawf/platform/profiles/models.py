"""Pydantic v2 models for profile bodies and composed profiles.

Each model carries ``model_config = ConfigDict(extra="forbid")`` per AGENTS.md
rule 2: every YAML/JSON ingestion path validates against a closed schema so
typos in profile YAMLs surface as :class:`ValidationError` rather than silent
drift.

Two shapes live here:

- :class:`ProfileBody` — the on-disk payload, one per ``data/<id>.yaml``.
- :class:`ComposedProfile` — the merged view returned by
  :func:`eawf.platform.profiles.compose.compose`. Adds ``provenance`` /
  ``override_audit`` / ``conflict_warnings`` recording which input profiles
  contributed each field, which overrides discharged conflicts, and which
  non-fatal overlaps the loader logged.

Field semantics mirror ``docs/architecture/profiles.md``:

- ``state_extensions.fields_required`` — top-level state keys to materialise.
- ``instrument_requirements`` — ``{name, kind, probe, version_args, version_regex}``.
- ``render_blocks`` — ``{id, target, tier, body_template | (rationale, mechanism,
  verification), version}``. ``id`` is the composition merge key; later
  overrides earlier per id. A block carries its body in exactly one of two
  shapes: a prose ``body_template`` or the structured
  ``rationale``/``mechanism``/``verification`` triad (XOR enforced by a
  model-validator).
- ``skills_referenced`` / ``hooks_referenced`` — string lists, union-merged.

Schema v2 (P25-W15) adds three new fields on :class:`ProfileBody`:

- ``conflicts_with`` — profile ids that cannot coexist with this one.
- ``overrides`` — profile ids whose contributions this one claims (discharges
  the conflict edge).
- ``dispatch_session_policy`` — closed enum (``fresh`` / ``continue`` /
  ``hybrid`` / ``None`` = inherit) consumed by the dispatch layer when this
  profile is in the composed view.

``schema_version: Literal["1.0"]`` lets the loader gate unknown future
formats; bodies that omit the key default to ``"1.0"``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eawf.kernel.spec.audit import AuditCadence
from eawf.kernel.spec.research_campaign import ResearchProfileBlock
from eawf.kernel.state.enums import TrackKind
from eawf.platform.profiles.clarity import DEFAULT_OUTPUT_STYLE, OutputStyle
from eawf.platform.render_block import (
    DEFAULT_RENDER_BLOCK_PLACEMENT,
    DEFAULT_RENDER_BLOCK_TIER,
    DISPATCH_SYSTEM_PROMPT_TARGET,
    RenderBlockPlacement,
    RenderBlockTier,
)


class StateExtensions(BaseModel):
    """State keys the profile requires materialised on ``state.json``."""

    model_config = ConfigDict(extra="forbid")

    fields_required: list[str] = []


class FloorCheck(BaseModel):
    """One floor-pack check declared on ``ProfileBody.verify.floor_checks``.

    The verify spine (P28-I01-W10) translates each floor check into a
    runnable :class:`~eawf.workflow.audit_dsl.models.CheckSpec` via
    :func:`eawf.workflow.verify.compile.compile_floor_pack`. Each check
    is one ``command_exit_zero`` invocation whose argv passes the L0
    argv-policy at profile-load time — a malformed argv fails the
    profile load, never reaches the gate runner.

    The shape is intentionally narrow for v0.4.0: only the metadata the
    readiness compute + gate runner actually consult right now is
    exposed. Future fields (e.g. per-check env vars, output capture
    rules) are additive.

    Attributes:
        name: Stable, profile-local id for the floor check. Surfaces
            in the :class:`~eawf.workflow.audit_dsl.models.CheckSpec.name`
            slot so per-gate evidence + waivers can address it.
        cmd: argv vector handed to
            :func:`~eawf.runtime.sandbox.argv_policy.validate_gate_argv`
            and (post-compile) to :func:`subprocess.run`.
        scope: File-set scope the runner resolves against the git
            diff-base — see
            :data:`~eawf.workflow.audit_dsl.models.Scope`.
        cadence: When the check fires. Reuses the 5-value
            :data:`~eawf.kernel.spec.audit.AuditCadence` per AGENTS
            naming rule 17.
        policy: How a failure is escalated — ``block`` / ``warn`` /
            ``advisory``. The profile-level ``enforce`` bit controls
            whether a non-ready close-readiness view rejects the close
            mutation or stays advisory.
        required: Whether the check is required for the rolled-up
            ``ready`` flag. Defaults ``True``.
        requires_gpu: Hint to the dispatcher that the check needs GPU
            access (e.g. CUDA, hardware-in-the-loop). Captured for
            future sandbox routing; the v0.4.0 runner ignores it.
        runs_outside_jail: HIL escape hatch — when ``True`` the check
            is allowed to bypass the jail-mode sandbox. v0.4.0
            captures the bit so the v0.4.1 jail / spawn machinery has
            the shape it needs.
        timeout_class: Timeout-class budget literal handed to the
            runner — see
            :data:`~eawf.workflow.audit_dsl.models.TimeoutClass`.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=72)
    cmd: list[str] = Field(min_length=1)
    scope: Literal["changed", "touched", "all"]
    cadence: AuditCadence
    policy: Literal["block", "warn", "advisory"]
    required: bool = True
    requires_gpu: bool = False
    runs_outside_jail: bool = False
    timeout_class: Literal["quick", "standard", "slow", "very_slow"] = "standard"


class CheckpointBlock(BaseModel):
    """Drift-cadence dial mounted on :attr:`VerifyBlock.checkpoint`.

    Folds the drift cadence into a single knob so a profile expresses
    "how often the verify spine pulses against accumulated drift" once,
    rather than scattering the decision across several seams. The
    ``checkpoint_mode`` picks the cadence shape; the two ``drift_budget_*``
    leaves carry the units a later wave reads to size the pulse window.

    The two cadence shapes:

    * ``optimistic`` (the default) — drift is tolerated between
      checkpoints and reconciled at the budget pulse, so independent
      waves keep flowing and the spine only stops the line when the
      accumulated drift crosses the budget. This is the shift-left
      default the v0.6 cadence ships.
    * ``barrier`` — every checkpoint is a hard stop: the spine blocks
      until drift is reconciled before the next wave proceeds. A
      downstream profile opts into the stricter shape by setting
      ``checkpoint_mode: barrier``, which wins composition over an
      upstream ``optimistic``.

    The drift budget is the slack the optimistic cadence spends before
    it must reconcile. ``drift_budget_waves`` counts waves that may
    accumulate drift before a pulse; ``drift_budget_eu`` is the same
    bound expressed in estimated-units so a slate of small waves and a
    slate of large waves get comparable windows. Both are non-negative;
    ``0`` means "reconcile every wave" (the strictest optimistic shape,
    equivalent in cadence to a barrier without the hard block).

    Attributes:
        checkpoint_mode: Drift-cadence shape — ``optimistic`` (default,
            reconcile at the budget pulse) or ``barrier`` (hard stop at
            every checkpoint). A downstream ``barrier`` wins composition.
        drift_budget_waves: Number of waves that may accumulate drift
            before the optimistic cadence pulses. ``Field(ge=0)`` rejects
            a negative budget at the load boundary.
        drift_budget_eu: Estimated-units of drift the optimistic cadence
            tolerates before a pulse. ``Field(ge=0)`` rejects a negative
            budget at the load boundary.
    """

    model_config = ConfigDict(extra="forbid")

    checkpoint_mode: Literal["optimistic", "barrier"] = "optimistic"
    drift_budget_waves: int = Field(default=3, ge=0)
    drift_budget_eu: float = Field(default=3.5, ge=0.0)


class JuryAuthorityConfig(BaseModel):
    """The trust floors a cross-vendor jury must clear to earn blocking authority.

    Mounted on :attr:`VerifyBlock.jury_authority`. Mirrors the four trust floors
    the earned-authority gate
    (:func:`eawf.observability.eval.jury_validation.jury_block_authority`) reads:
    a jury veto is held ADVISORY (logged, never blocking) until the jury's
    validation report and verbosity probe clear every floor here. The leaf is
    declared in this profile-models module so the floors are auditable from the
    profile and resolved through the config chain without pulling the
    observability layer onto the profile cold-import path; the daemon close path
    maps it onto the eval-module config it consumes.

    ``extra="forbid"`` so a drifted config key surfaces as a
    :class:`pydantic.ValidationError` at profile load rather than silently
    widening the authority a jury may earn. Every default is tuned so a jury that
    has NOT been validated stays advisory -- blocking authority is only ever
    earned, so a profile that omits the leaf keeps the safe advisory-by-default
    behaviour.

    Attributes:
        min_labeled_waves: Minimum labelled verdicts the validation cohort must
            carry before the jury can earn blocking authority. ``Field(ge=1)``
            rejects a zero floor (which would defeat the earned-authority
            guarantee) at the load boundary.
        known_bad_catch_lb_floor: Wilson / Beta lower-bound floor on the jury's
            known-bad catch rate, in ``[0.0, 1.0]``. The conservative LOWER
            bound (not the point estimate) must clear this floor.
        unanimous_pass_ceiling: Ceiling on the unanimous-pass-on-known-bad
            (false-clean) rate, in ``[0.0, 1.0]``. A jury whose false-clean rate
            is at or above this ceiling is denied authority -- a hot blind spot
            disqualifies the panel even when the catch-rate LB clears.
    """

    model_config = ConfigDict(extra="forbid")

    min_labeled_waves: int = Field(default=20, ge=1)
    known_bad_catch_lb_floor: float = Field(default=0.80, ge=0.0, le=1.0)
    unanimous_pass_ceiling: float = Field(default=0.10, ge=0.0, le=1.0)


class VerifyBlock(BaseModel):
    """Profile-fed verify spine configuration.

    Mounted on :attr:`ProfileBody.verify`. The verify spine (W06 + W08
    + W10 + W11 + W15) consults this block for:

    * the **floor pack** (:attr:`floor_checks`) — translated into
      :class:`~eawf.workflow.audit_dsl.models.CheckSpec` rows by
      :func:`eawf.workflow.verify.compile.compile_floor_pack` and run
      through the W15-hardened gate runner;
    * the **argv allowlist** (:attr:`argv_allowlist`) — handed to the
      L0 argv-policy validator at compile time;
    * the **timeout-class table** (:attr:`timeout_class_seconds`) — a
      forward-looking override of the runner's default per-class
      seconds; ``None`` defers to the runner default (the v0.4.0
      runner does not yet read overrides — captured for v0.4.1+);
    * the **waiver-mode** (:attr:`waiver_mode`) — consumed by W11's
      :func:`~eawf.workflow.lifecycle.waivers.resolve_waiver_mode`;
    * the **enforcement bit** (:attr:`enforce`) — when true, close
      seams reject a non-ready
      :class:`~eawf.workflow.verify.models.CloseReadiness` instead
      of surfacing it as advisory-only.

    All fields are optional; an absent ``verify:`` leaf on a profile
    yields an empty block so the verify spine has nothing to compile.

    Attributes:
        floor_checks: Per-profile floor of deterministic checks the
            verify spine compiles into the readiness view. Empty list
            means "this profile contributes no floor checks".
        argv_allowlist: argv heads the L0 argv-policy accepts at
            floor-pack compile time (and, later, at spec-promote
            time). Combined with the kernel-spec default allowlist
            inside :func:`compile_floor_pack`.
        timeout_class_seconds: Optional per-class seconds override.
            ``None`` defers to the gate runner default.
        waiver_mode: Mode-gated linkage policy for operator waivers —
            see :data:`~eawf.workflow.lifecycle.waivers.WaiverMode`.
            Defaults to ``"B"`` (reason required, decision/audit
            optional) per the W11 default.
        enforce: Whether close readiness is a hard gate. Defaults
            ``False`` so existing profiles keep the advisory close
            behavior until they opt in.
        cross_vendor_jury: Opt-in upgrade of the high-risk (``"always"``)
            wave-close verdict gate from a single fresh-context auditor to a
            three-vendor disjoint-family jury
            (:func:`eawf.observability.eval.cross_vendor_jury.convene_cross_vendor_jury`).
            Only meaningful when :attr:`enforce` is ``True``. Defaults
            ``False`` so existing enforcing profiles keep the single-auditor
            gate; when ``True`` the close path convenes the jury for the
            ``"always"`` subset and a split (no quorum) routes to the
            operator. The path degrades to the single-auditor gate when the
            cross-vendor CLI lanes are unavailable on the host.
        uiux_bands: Substring tokens that mark a wave as UI/UX-banded for
            the spec-jury close gate
            (:func:`eawf.workflow.dispatch.spec_jury.wave_in_uiux_band`). A
            wave is banded when its ``file_scopes`` are UI surface (per
            :func:`eawf.kernel.spec.heuristics.is_ui_scope`) OR any token here
            matches its id / title. The UI-surface ``file_scopes`` arm is the
            structural band definition — a wave touching ``surfaces/tui/`` or
            ``surfaces/render/`` is UI/UX-risky regardless of its title —
            while the token list is the override for waves a profile wants
            banded by name. Defaults to an empty list so no wave is banded by
            *token* until a profile opts in; the ``file_scopes`` arm bands UI
            waves with no config. Band membership drives band-conditional
            enforcement
            (:func:`eawf.workflow.verify.readiness.resolve_wave_verify_block`),
            so this is meaningful even when the merged ``enforce`` bit is
            ``False`` — the resolver turns enforcement ON for a band wave and
            OFF for a non-band wave rather than flipping it fleet-wide.
        jury_vendors: The disjoint-vendor panel the band-scoped spec jury
            convenes one juror from each of. Declared as config so the panel
            is auditable from the profile (defaults to the full cross-vendor
            triple ``["claude", "codex", "opencode"]``, all three of whose
            spawn runtimes are live). The LIVE multi-vendor invocation stays
            IDLE in v0.5: the daemon close path's per-item ballot fn
            (:func:`eawf.runtime.daemon.methods.state._spec_jury_ballot_fn`)
            returns ``None`` so no juror is spawned from this list — wiring
            the live panel is a deferred calibration note, and the
            deterministic canned-ballot gate (W08) is what proves the gate
            discriminates. Recording the panel here keeps the intent typed
            and ready for the live binding without enabling a spawn.
        odr_floor: Advisory Oracle-Determinism-Ratio floor consulted at
            iter-close by
            :func:`eawf.observability.metrics.odr.odr_below_floor`. The
            ODR is the fraction of a scope's required criteria gated by a
            deterministic oracle (tiers T1..T5) rather than a judgment
            oracle (T6/T7); when the computed ratio falls below this
            floor the close seam surfaces an ADVISORY finding (log only)
            but never blocks. Defaults to ``0.80``.
        odr_blocking: When ``True``, a below-:attr:`odr_floor` ODR at iter
            close is a HARD gate:
            :func:`eawf.workflow.lifecycle.iter_.close_iter` raises
            :class:`~eawf.workflow.lifecycle._errors.LifecycleError`
            instead of surfacing the sub-floor ratio as an advisory
            (log-only) finding. Defaults ``False`` so every existing
            profile keeps the advisory-only ODR behaviour; a profile opts
            in only after its legacy sub-floor criteria have been drained,
            otherwise every iter close would block on the un-drained floor.
        require_iter_audit_accepted: When ``True``, iter close requires a
            completed accepted audit row. This gate is tighten-only across
            profile and repo composition.
        jury_authority: The trust floors a cross-vendor jury must clear to
            earn BLOCKING authority at the close gate (a
            :class:`JuryAuthorityConfig`). Until the jury's validation report
            and verbosity probe clear every floor here, its veto is held
            advisory (logged, never blocking) by
            :func:`eawf.observability.eval.jury_validation.jury_block_authority`.
            Defaults to the safe advisory-leaning floors so an enforcing
            profile that omits the leaf keeps the advisory-only jury behaviour.
        checkpoint: Drift-cadence dial — one :class:`CheckpointBlock`
            knob carrying the cadence shape (``optimistic`` default /
            ``barrier``) plus the drift-budget units a later wave reads
            to size the pulse window. Defaults to an optimistic block
            via the default factory; a downstream profile that sets
            ``checkpoint.checkpoint_mode: barrier`` wins composition.
    """

    model_config = ConfigDict(extra="forbid")

    floor_checks: list[FloorCheck] = Field(default_factory=list)
    argv_allowlist: list[str] = Field(default_factory=list)
    timeout_class_seconds: dict[Literal["quick", "standard", "slow", "very_slow"], int] | None = (
        None
    )
    waiver_mode: Literal["A", "B", "C", "disabled"] = "B"
    enforce: bool = False
    cross_vendor_jury: bool = False
    #: Wall-clock ceiling (seconds) for every close-time juror / auditor
    #: spawn. A hung vendor CLI previously awaited forever while the close
    #: held the state lock (ZD-R3); the ceiling makes the spawn error out
    #: as a structured juror-error outcome instead. Operator-ratified
    #: default: 600s.
    juror_wall_clock_seconds: Annotated[float, Field(gt=0)] = 600.0
    uiux_bands: list[str] = Field(default_factory=list)
    jury_vendors: list[str] = Field(default_factory=lambda: ["claude", "codex", "opencode"])
    odr_floor: float = Field(default=0.80, ge=0.0, le=1.0)
    odr_blocking: bool = False
    require_iter_audit_accepted: bool = False
    jury_authority: JuryAuthorityConfig = Field(default_factory=JuryAuthorityConfig)
    checkpoint: CheckpointBlock = Field(default_factory=CheckpointBlock)


class OutputBlock(BaseModel):
    """Profile-fed house output-style configuration.

    Mounted on :attr:`ProfileBody.output` and resolved through the config
    chain as ``output.style``. The single ``style`` leaf selects the house
    output style the directive renderer
    (:func:`eawf.platform.profiles.clarity.render_style_directive`) ships into
    each vendor slot at plugin install. Both styles cover the same six
    newcomer-test dimensions; they differ only in directive verbosity.

    The field is a closed :class:`~eawf.platform.profiles.clarity.OutputStyle`
    enum, so an unknown ``output.style`` token in a profile YAML fails the
    profile load with a :class:`ValidationError` rather than silently
    defaulting. The default is
    :data:`~eawf.platform.profiles.clarity.DEFAULT_OUTPUT_STYLE`
    (``lean``), so a profile that omits ``output:`` resolves to the terse
    senior-developer style.

    Attributes:
        style: House output style token. Defaults to ``lean``; ``explain``
            opts into the verbose directive. An unknown token raises
            :class:`ValidationError` at the load boundary.
    """

    model_config = ConfigDict(extra="forbid")

    style: OutputStyle = DEFAULT_OUTPUT_STYLE


class TrackKindSpec(BaseModel):
    """Per-kind parametrization for a :class:`~eawf.kernel.state.models.Track`.

    A profile contributes one :class:`TrackKindSpec` per
    :class:`~eawf.kernel.state.enums.TrackKind` it supports, keyed under
    :attr:`TrackProfileBlock.kinds`. The spec carries the kind-specific noun,
    status vocabulary, outcome template, and overview view a Track of that kind
    surfaces, so the lifecycle + render surfaces parametrize per kind from one
    typed source rather than branching on a free-string tag.

    ``extra="forbid"`` so a drifted leaf surfaces as a
    :class:`pydantic.ValidationError` at profile load rather than silently
    widening the spec.

    Attributes:
        noun: Operator-facing singular noun for a track of this kind
            (e.g. ``strategy`` / ``model`` / ``target``). Surfaces in the
            track overview header and dispatch prose.
        status_lifecycle: Ordered status vocabulary the kind's tracks move
            through, most-planned to most-terminal. Each entry is a status
            token the kind's overview view renders as a lifecycle band.
        outcome_template: Default outcome metric template the kind seeds a
            new track's outcomes from (e.g. ``sharpe`` for a strategy,
            ``val_loss`` for a model). Empty when the kind seeds no default.
        overview_view: Identifier of the render view the kind's track
            overview uses (e.g. ``leaderboard``). Picks which projection the
            standings render selects for the kind.
    """

    model_config = ConfigDict(extra="forbid")

    noun: str = Field(min_length=1, max_length=72)
    status_lifecycle: list[str] = Field(default_factory=list)
    outcome_template: str = ""
    overview_view: str = ""


class TrackProfileBlock(BaseModel):
    """Profile-fed per-kind Track parametrization.

    Mounted on :attr:`ProfileBody.track`. A profile declares the
    :class:`~eawf.kernel.state.enums.TrackKind` set it supports under
    :attr:`kinds`, mapping each kind to the :class:`TrackKindSpec` that
    parametrizes the kind's noun, status lifecycle, outcome template, and
    overview view. ``None`` means the profile contributes no track config; a
    profile that omits ``track:`` declares no kinds.

    The :attr:`kinds` map is keyed by the closed
    :class:`~eawf.kernel.state.enums.TrackKind` enum, so an unknown kind token
    in a profile YAML fails the profile load with a
    :class:`pydantic.ValidationError` at the ingestion boundary.

    Attributes:
        kinds: Mapping from :class:`~eawf.kernel.state.enums.TrackKind` to the
            :class:`TrackKindSpec` parametrizing that kind. Empty when the
            profile declares the block but no kinds.
    """

    model_config = ConfigDict(extra="forbid")

    kinds: dict[TrackKind, TrackKindSpec] = Field(default_factory=dict)


class InstrumentReq(BaseModel):
    """A single external-tool requirement declared by a profile.

    Mirrors :class:`eawf.platform.install.instrument_probe.InstrumentSpec` field-for-
    field so the Phase 3 W01 probe can consume composed profile output
    directly. ``kind`` participates in strictest-wins composition: when two
    profiles declare the same instrument, ``hard`` overrides ``soft``.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: Literal["hard", "soft"] = "hard"
    probe: Literal["which", "version"] = "which"
    version_args: list[str] = []
    version_regex: str | None = None


#: Hard upper bound on a reference-placed block's ``summary``. The summary is
#: the ONE line the always-loaded managed file keeps when the body moves to
#: ``docs/rules/<id>.md``, so it is charged against the byte budget the move
#: exists to protect: 21 summaries at 200 characters already cost ~4 KB of a
#: 32 KB cap. An unbounded summary would let a rule creep its whole body back
#: into the file one edit at a time. Sized just above the longest shipped
#: summary so the bound bites on growth, not on today's prose.
RENDER_BLOCK_SUMMARY_MAX: int = 200


class RenderBlock(BaseModel):
    """A chunk of templated content the renderer emits into a managed file.

    ``id`` is the merge key during composition; ``target`` names the destination
    file (e.g. ``"AGENTS.md"``); ``version`` is recorded in the rendered
    managed-region marker so re-renders can detect template upgrades.

    A block carries its body in exactly one of two shapes:

    - **Prose** — ``body_template`` is a non-empty Jinja2 source the renderer
      compiles verbatim. This is the legacy shape every shipped profile uses.
    - **Structured** — the ``rationale`` / ``mechanism`` / ``verification``
      triad is fully populated and ``body_template`` is left empty. The
      renderer emits a fixed ``Rationale`` / ``Mechanism`` / ``Verification``
      sub-heading layout from the triad so authors who want the canonical
      three-part rule shape do not hand-format markdown.

    The :meth:`_exactly_one_body_shape` validator enforces the XOR: a block
    that fills neither shape, both shapes, or only part of the triad is a
    :class:`ValidationError`. An empty ``body_template`` (the ``""`` default)
    means "not the prose shape"; non-empty means prose.

    ``agent_role`` (FLEET-5 / P30-I06-W05) binds a block to one subagent role
    for the per-role dispatch tier. When ``target`` is
    :data:`~eawf.platform.render_block.DISPATCH_SYSTEM_PROMPT_TARGET` the block
    is a "Zone 3" role rule: its body is injected into the dispatched system
    prompt for waves whose ``agent_role`` matches. ``agent_role`` is required
    for that target and forbidden for every other target (a managed-file block
    such as ``AGENTS.md`` carries no role binding). ``None`` for the legacy
    managed-file blocks.

    ``placement`` decides where the block's bytes live. ``"root"`` (the
    default, so a block that declares nothing renders exactly as it always did)
    emits the body verbatim into the managed file. ``"reference"`` writes the
    full body to ``docs/rules/<id>.md`` and leaves one compact line in the
    managed file, so an expansion that only justifies or elaborates a rule
    stops consuming the reader's always-loaded byte budget. A ``"reference"``
    block MUST carry a ``summary`` -- the obligation in one sentence -- because
    that is the line the managed file gets; a ``"root"`` block MUST NOT, since
    nothing would ever read it. A summary is capped at
    :data:`RENDER_BLOCK_SUMMARY_MAX` characters: it is the part of a moved rule
    that still costs always-loaded bytes, so it stays one sentence rather than
    growing back into a body.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    target: str
    body_template: str = ""
    rationale: str | None = None
    mechanism: str | None = None
    verification: str | None = None
    tier: RenderBlockTier = DEFAULT_RENDER_BLOCK_TIER
    placement: RenderBlockPlacement = DEFAULT_RENDER_BLOCK_PLACEMENT
    summary: Annotated[str, Field(min_length=1, max_length=RENDER_BLOCK_SUMMARY_MAX)] | None = None
    version: str = "1.0"
    agent_role: str | None = None

    @property
    def is_structured(self) -> bool:
        """``True`` when this block carries the structured triad (not prose)."""
        return self.rationale is not None

    @property
    def is_reference_placed(self) -> bool:
        """``True`` when the full body belongs in ``docs/rules/<id>.md``.

        A reference-placed block contributes only its :attr:`summary` line to
        the managed file; the renderer writes the body to its own file. See
        :data:`~eawf.platform.render_block.RenderBlockPlacement`.
        """
        return self.placement == "reference"

    @property
    def is_role_tier(self) -> bool:
        """``True`` when this block binds to a role for the dispatch tier.

        A role-tier block targets
        :data:`~eawf.platform.render_block.DISPATCH_SYSTEM_PROMPT_TARGET` and
        carries a non-``None`` :attr:`agent_role`; its body is injected into
        the dispatched system prompt for matching waves rather than rendered
        into a managed file.
        """
        return self.target == DISPATCH_SYSTEM_PROMPT_TARGET

    @property
    def body_text(self) -> str:
        """Return the block's raw body text (prose or structured triad).

        Prose blocks return :attr:`body_template` verbatim; structured blocks
        join the ``rationale`` / ``mechanism`` / ``verification`` triad with a
        blank line. The XOR validator guarantees exactly one shape is set, so
        the result is always non-empty for a valid block. This is the text a
        per-tier token budget weighs and the role-tier dispatch injection
        splices into the system prompt.
        """
        if self.body_template:
            return self.body_template
        parts = [self.rationale, self.mechanism, self.verification]
        return "\n\n".join(part for part in parts if part is not None)

    @model_validator(mode="after")
    def _exactly_one_body_shape(self) -> RenderBlock:
        """Enforce exactly one of prose ``body_template`` or the full triad.

        A block is *prose* when ``body_template`` is non-empty; it is
        *structured* when all three of ``rationale`` / ``mechanism`` /
        ``verification`` are set. Exactly one shape must hold: a block that
        fills both, fills neither, or fills only part of the triad is invalid.

        Raises:
            ValueError: when the block fills both the prose and structured
                shapes at once, fills neither, or supplies an incomplete
                triad (one or two of the three triad fields).
        """
        has_prose = bool(self.body_template)
        triad = (self.rationale, self.mechanism, self.verification)
        triad_set = [field is not None for field in triad]
        has_full_triad = all(triad_set)
        has_partial_triad = any(triad_set) and not has_full_triad

        if has_partial_triad:
            raise ValueError(
                f"render_block id={self.id!r} has an incomplete structured triad: "
                f"rationale/mechanism/verification must all be set together"
            )
        if has_prose and has_full_triad:
            raise ValueError(
                f"render_block id={self.id!r} sets both body_template and the "
                f"rationale/mechanism/verification triad: provide exactly one"
            )
        if not has_prose and not has_full_triad:
            raise ValueError(
                f"render_block id={self.id!r} sets neither body_template nor the "
                f"rationale/mechanism/verification triad: provide exactly one"
            )
        return self

    @model_validator(mode="after")
    def _role_binding_matches_target(self) -> RenderBlock:
        """Enforce ``agent_role`` is set iff the block targets the dispatch tier.

        A role-tier block (``target ==``
        :data:`~eawf.platform.render_block.DISPATCH_SYSTEM_PROMPT_TARGET`)
        MUST name the role it binds to; a managed-file block (any other
        target) MUST NOT carry a role binding.

        Raises:
            ValueError: when a dispatch-tier block omits ``agent_role``, or a
                non-dispatch block supplies one.
        """
        if self.target == DISPATCH_SYSTEM_PROMPT_TARGET:
            if self.agent_role is None:
                raise ValueError(
                    f"render_block id={self.id!r} targets the dispatch tier "
                    f"but sets no agent_role: a role-tier block must name its role"
                )
        elif self.agent_role is not None:
            raise ValueError(
                f"render_block id={self.id!r} sets agent_role={self.agent_role!r} "
                f"but targets {self.target!r}: agent_role is reserved for the dispatch tier"
            )
        return self

    @model_validator(mode="after")
    def _summary_matches_placement(self) -> RenderBlock:
        """Enforce ``summary`` is set iff ``placement`` is ``"reference"``.

        A reference-placed block's summary IS the line the managed file
        carries, so omitting it would leave the reader a bare link with no
        statement of the obligation. A root-placed block renders its whole
        body, so a summary would be unread text drifting out of sync.

        Raises:
            ValueError: when a reference-placed block omits ``summary`` (or
                gives a blank one), or a root-placed block supplies one.
        """
        if self.placement == "reference":
            if self.summary is None or not self.summary.strip():
                raise ValueError(
                    f"render_block id={self.id!r} is placement=reference but sets no "
                    f"summary: the managed file needs one line stating the obligation"
                )
        elif self.summary is not None:
            raise ValueError(
                f"render_block id={self.id!r} sets summary={self.summary!r} but is "
                f"placement={self.placement!r}: summary is reserved for reference placement"
            )
        return self


class ProfileBody(BaseModel):
    """Closed schema for a single ``data/<id>.yaml`` profile body (v2).

    ``extends`` (P14-W05) records a parent profile id when the body was
    generated by ``eawf profile new --inherit <parent>``. The field is
    informational in v0.3 — composition still operates on the explicit
    profiles list passed to ``compose``; the v0.4 follow-up wires
    automatic ancestor resolution. ``None`` for stand-alone profiles.

    Schema v2 fields (P25-W15):

    - ``conflicts_with`` — profile ids that cannot coexist with this body in
      the same composition unless one declares the other in ``overrides``.
    - ``overrides`` — profile ids whose contributions this body claims.
      Declaring ``overrides: [b]`` discharges the (this, b) conflict edge
      and records every overlap in :attr:`ComposedProfile.override_audit`.
    - ``dispatch_session_policy`` — closed enum consumed by the dispatch
      layer. ``None`` defers to the skill / global default; non-``None``
      values participate in last-non-None-wins composition.

    ``research`` (P29-I01-W14 + W27) mounts the typed
    :class:`~eawf.kernel.spec.research_campaign.ResearchProfileBlock` —
    the per-domain research-campaign config a profile contributes. ``None``
    means the profile declares no campaign config; non-``None`` values
    participate in last-non-``None``-wins composition, mirroring
    ``dispatch_session_policy`` (a downstream profile that sets the block
    wins).

    ``output`` (P30-I03-W04) mounts the typed :class:`OutputBlock`, resolved
    through the config chain as ``output.style``. It selects the house output
    style the directive renderer ships into each vendor slot at plugin
    install. Defaults to a ``lean`` block via the default factory, so a
    profile that omits ``output:`` resolves to the terse style; an unknown
    ``output.style`` token raises :class:`ValidationError` at load.

    ``track`` mounts the typed :class:`TrackProfileBlock` -- the per-kind Track
    parametrization a profile contributes (noun, status lifecycle, outcome
    template, overview view, keyed by the closed
    :class:`~eawf.kernel.state.enums.TrackKind`). ``None`` means the profile
    contributes no track config; an unknown kind token in the ``track.kinds``
    map raises :class:`ValidationError` at the load boundary.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    name: str
    version: str = "1.0"
    description: str = ""
    extends: str | None = None
    state_extensions: StateExtensions = StateExtensions()
    instrument_requirements: list[InstrumentReq] = []
    render_blocks: list[RenderBlock] = []
    skills_referenced: list[str] = []
    hooks_referenced: list[str] = []
    conflicts_with: list[str] = []
    overrides: list[str] = []
    dispatch_session_policy: Literal["fresh", "continue", "hybrid"] | None = None
    verify: VerifyBlock | None = None
    research: ResearchProfileBlock | None = None
    output: OutputBlock = Field(default_factory=OutputBlock)
    track: TrackProfileBlock | None = None


class ComposedProfile(BaseModel):
    """Output of :func:`eawf.platform.profiles.compose.compose` (v2).

    Mirrors :class:`ProfileBody` field-for-field plus three audit maps:

    - ``provenance`` — top-level field name → input profile names (caller
      order) that contributed at least one entry to that field. An empty
      list means no input profile populated the field.
    - ``override_audit`` — field path → ordered override chain. For each
      ``(overrider, overridden)`` edge in the composition's override graph,
      every merged leaf the overrider's contribution claimed gets a
      ``[overrider, overridden]`` chain recorded under the leaf's field
      path (e.g. ``"render_blocks[id=hypothesis-format]"``).
    - ``conflict_warnings`` — non-fatal warnings emitted during merge
      (e.g. same ``render_block.id`` declared by two non-overriding
      profiles, the later body wins but the loader logs the overlap).

    The ``name`` field on the composed view is a deterministic, ``+``-joined
    label (e.g. ``"core+python+research"``). Callers needing the original
    inputs should consult ``provenance``.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    name: str
    version: str = "1.0"
    description: str = ""
    state_extensions: StateExtensions = StateExtensions()
    instrument_requirements: list[InstrumentReq] = []
    render_blocks: list[RenderBlock] = []
    skills_referenced: list[str] = []
    hooks_referenced: list[str] = []
    dispatch_session_policy: Literal["fresh", "continue", "hybrid"] | None = None
    verify: VerifyBlock | None = None
    research: ResearchProfileBlock | None = None
    provenance: dict[str, list[str]] = {}
    override_audit: dict[str, list[str]] = {}
    conflict_warnings: list[str] = []

    def partition_render_blocks_by_tier(
        self,
        target: str,
    ) -> tuple[list[RenderBlock], list[RenderBlock]]:
        """Split the *target*-bound render_blocks into ``(tier0, reference)`` lists.

        Walks :attr:`render_blocks` once in source order, keeping only blocks
        whose :attr:`RenderBlock.target` equals *target*, and routes each into
        the tier0 list (always-on Zone 1) or the reference list (lazy Zone 2)
        per its :attr:`RenderBlock.tier`. Relative order within each tier is the
        source-declared order, so a renderer can emit Zone 1 then Zone 2 while
        preserving the author's intra-tier sequencing.

        Args:
            target: Destination filename to filter on (e.g. ``"AGENTS.md"``).

        Returns:
            A ``(tier0_blocks, reference_blocks)`` pair. Either list is empty
            when no targeted block carries that tier; the renderer relies on
            the empty case to suppress an empty zone.
        """
        tier0: list[RenderBlock] = []
        reference: list[RenderBlock] = []
        for block in self.render_blocks:
            if block.target != target:
                continue
            if block.tier == "tier0":
                tier0.append(block)
            else:
                reference.append(block)
        return tier0, reference

    def role_tier_blocks(self) -> dict[str, str]:
        """Return the role-tier dispatch blocks as an ``agent_role -> body`` map.

        Walks :attr:`render_blocks` in source order, keeping only role-tier
        blocks (those targeting
        :data:`~eawf.platform.render_block.DISPATCH_SYSTEM_PROMPT_TARGET`), and
        maps each block's :attr:`RenderBlock.agent_role` to its
        :attr:`RenderBlock.body_text`. The dispatch renderer injects the
        matching role's body into the system prompt for waves of that role; a
        role absent from the map is a true no-op (the static
        ``RoleSpec.system_prompt`` renders unchanged).

        When two role-tier blocks bind the same role, the later one wins (the
        same last-declared-wins rule composition already applied per
        ``RenderBlock.id``).

        Returns:
            A mapping from ``agent_role`` value to the block body to inject.
            Empty when the composed profile declares no role-tier block.
        """
        out: dict[str, str] = {}
        for block in self.render_blocks:
            if not block.is_role_tier or block.agent_role is None:
                continue
            out[block.agent_role] = block.body_text
        return out
