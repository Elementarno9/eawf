"""Pydantic v2 models for profile bodies and composed profiles.

Each model carries ``model_config = ConfigDict(extra="forbid")`` per AGENTS.md
rule 2: every YAML/JSON ingestion path validates against a closed schema so
typos in profile YAMLs surface as :class:`ValidationError` rather than silent
drift.

Two shapes live here:

- :class:`ProfileBody` — the on-disk payload, one per ``data/<id>.yaml``.
- :class:`ComposedProfile` — the merged view returned by
  :func:`eawf.profiles.compose.compose`. Adds ``provenance`` /
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

from pydantic import BaseModel, ConfigDict, model_validator


class StateExtensions(BaseModel):
    """State keys the profile requires materialised on ``state.json``."""

    model_config = ConfigDict(extra="forbid")

    fields_required: list[str] = []


class InstrumentReq(BaseModel):
    """A single external-tool requirement declared by a profile.

    Mirrors :class:`eawf.install.instrument_probe.InstrumentSpec` field-for-
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


class ComposedProfile(BaseModel):
    """Output of :func:`eawf.profiles.compose.compose` (v2).

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
    provenance: dict[str, list[str]] = {}
    override_audit: dict[str, list[str]] = {}
    conflict_warnings: list[str] = []
