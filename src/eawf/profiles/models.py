"""Pydantic v2 models for profile bodies and composed profiles.

Each model carries ``model_config = ConfigDict(extra="forbid")`` per AGENTS.md
rule 2: every YAML/JSON ingestion path validates against a closed schema so
typos in profile YAMLs surface as :class:`ValidationError` rather than silent
drift.

Two shapes live here:

- :class:`ProfileBody` — the on-disk payload, one per ``data/<id>.yaml``.
- :class:`ComposedProfile` — the merged view returned by
  :func:`eawf.profiles.compose.compose`. Adds a ``provenance`` map recording
  which input profiles contributed each top-level key.

Field semantics mirror ``docs/architecture/profiles.md``:

- ``state_extensions.fields_required`` — top-level state keys to materialise.
- ``instrument_requirements`` — ``{name, kind, probe, version_args, version_regex}``.
- ``render_blocks`` — ``{id, target, body_template, version}``. ``id`` is the
  composition merge key; later overrides earlier per id.
- ``skills_referenced`` / ``hooks_referenced`` — string lists, union-merged.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


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
    file (e.g. ``"AGENTS.md"``); ``body_template`` is the Jinja2 source the
    Phase 3 W04 renderer compiles. ``version`` is recorded in the rendered
    managed-region marker so re-renders can detect template upgrades.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    target: str
    body_template: str
    version: str = "1.0"


class ProfileBody(BaseModel):
    """Closed schema for a single ``data/<id>.yaml`` profile body."""

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str = "1.0"
    description: str = ""
    state_extensions: StateExtensions = StateExtensions()
    instrument_requirements: list[InstrumentReq] = []
    render_blocks: list[RenderBlock] = []
    skills_referenced: list[str] = []
    hooks_referenced: list[str] = []


class ComposedProfile(BaseModel):
    """Output of :func:`eawf.profiles.compose.compose`.

    Mirrors :class:`ProfileBody` field-for-field plus a ``provenance`` map
    keyed by top-level field name. Each entry lists the input profile names
    (in caller order) that contributed at least one entry to that field; an
    empty list means no input profile populated the field, so the default
    applies.

    The ``name`` field on the composed view is a deterministic, ``+``-joined
    label (e.g. ``"core+python+research"``). Callers needing the original
    inputs should consult ``provenance``.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str = "1.0"
    description: str = ""
    state_extensions: StateExtensions = StateExtensions()
    instrument_requirements: list[InstrumentReq] = []
    render_blocks: list[RenderBlock] = []
    skills_referenced: list[str] = []
    hooks_referenced: list[str] = []
    provenance: dict[str, list[str]] = {}
