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

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eawf.kernel.spec.audit import AuditCadence
from eawf.kernel.spec.research_campaign import ResearchProfileBlock
from eawf.platform.render_block import DEFAULT_RENDER_BLOCK_TIER, RenderBlockTier


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
    waiver_mode: Literal["A", "B", "C"] = "B"
    enforce: bool = False
    cross_vendor_jury: bool = False
    uiux_bands: list[str] = Field(default_factory=list)
    jury_vendors: list[str] = Field(default_factory=lambda: ["claude", "codex", "opencode"])
    odr_floor: float = Field(default=0.80, ge=0.0, le=1.0)
    checkpoint: CheckpointBlock = Field(default_factory=CheckpointBlock)


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
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    target: str
    body_template: str = ""
    rationale: str | None = None
    mechanism: str | None = None
    verification: str | None = None
    tier: RenderBlockTier = DEFAULT_RENDER_BLOCK_TIER
    version: str = "1.0"

    @property
    def is_structured(self) -> bool:
        """``True`` when this block carries the structured triad (not prose)."""
        return self.rationale is not None

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
