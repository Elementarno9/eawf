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
- ``render_blocks`` — ``{id, target, body_template | (rationale, mechanism,
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
            ``advisory``. v0.4.0 keeps all floor checks effectively
            advisory at the wave-close boundary (W19 flips the
            enforcement); the field is captured so W19 has the shape
            it needs.
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
      :func:`~eawf.workflow.lifecycle.waivers.resolve_waiver_mode`.

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
    """

    model_config = ConfigDict(extra="forbid")

    floor_checks: list[FloorCheck] = Field(default_factory=list)
    argv_allowlist: list[str] = Field(default_factory=list)
    timeout_class_seconds: dict[Literal["quick", "standard", "slow", "very_slow"], int] | None = (
        None
    )
    waiver_mode: Literal["A", "B", "C"] = "B"


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
    provenance: dict[str, list[str]] = {}
    override_audit: dict[str, list[str]] = {}
    conflict_warnings: list[str] = []
