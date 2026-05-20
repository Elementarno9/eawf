"""Per-skill manifest schema (C04b §5 / D-b1..D-b3).

C04b lifts each skill's contribution metadata into a typed
:class:`SkillManifest` so the round-trip contract (V9) can be validated
fail-fast rather than sniffed out of SKILL.md frontmatter at sync time.
The manifest is the per-skill body that the canonical
:class:`~eawf.runtimes.manifest.PluginManifest` lists under
``contributes.skills`` and that plugin sync (C07-owned) projects into
each runtime's native plugin shape.

Design decisions baked in (C04b §4):

* **D-b1 — frozen envelope status enum.** The closed five-value set
  ``ok | needs_user | blocked | failed | partial`` is owned by
  :data:`eawf.render.envelope.EnvelopeStatus`. This module re-exports it
  rather than redefining the literal so the freeze has exactly one source
  of truth; a future drift in either place would surface as a parity test
  failure (``tests/runtimes/test_plugin_manifest.py`` +
  ``tests/unit/test_render_envelope.py``).
* **D-b2 — single ``runtime`` field.** Subset visibility is expressed by a
  single ``runtime: list[Literal[...]]`` field keyed to the canonical
  runtime ids, dropping the ``visibility.runtimes`` alternate. Empty list
  means "no runtime can host this skill" and is rejected; a skill visible
  everywhere lists all three ids.
* **D-b3 — ``output_dir`` (BOT-06).** The write-destination field uses the
  canonical name ``output_dir`` (never ``out_dir`` / ``target_dir``) per
  the naming-conventions canon. ``ConfigDict(extra="forbid")`` makes a
  stray ``target_dir`` key fail validation, so the rename is enforced by
  the schema rather than by convention alone.

Failure modes (C04b §6):

* ``F-b02`` — a non-``Literal`` runtime id (e.g. ``"aider"``) is rejected
  by the closed ``RuntimeId`` literal at load time.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from eawf.render.envelope import EnvelopeStatus

# Canonical runtime identifiers — mirrors
# :data:`eawf.runtimes.manifest.RuntimeId` and the ``runtime.preference``
# config layer. The closed literal is what enforces C04b F-b02 (non-Literal
# runtime rejection).
RuntimeId = Literal["claude-code", "codex", "opencode"]


class SkillManifest(BaseModel):
    """Per-skill manifest body listed in ``PluginManifest.contributes.skills``.

    The manifest is the V9 round-trip contract for one skill: every field
    must project cleanly into each runtime named in :attr:`runtime`, or the
    skill must narrow :attr:`runtime` to the subset that can host it. Plugin
    sync (C07-owned) consumes this shape; this module owns only the schema +
    its invariants.

    Attributes:
        name: Canonical skill name including the leading slash
            (e.g. ``"/research"``).
        description: One-sentence human-readable skill description shown in
            marketplace listings and CLI tables.
        runtime: Non-empty subset of the canonical runtime ids that can host
            this skill. Per D-b2 this is the single source of visibility;
            an empty list is rejected (a skill no runtime can host is a
            manifest authoring error).
        dispatch: Free-form dispatch-side controls (``session_policy``,
            ``model_hint``, etc.) carried as a flat ``str | bool | int``
            mapping. Kept open per C04b §5 so dispatch knobs can grow
            without a schema bump; the canonical ``PluginManifest`` stays
            closed.
        output_envelope_kind: Name of the typed output-envelope body the
            skill emits (per the envelope catalog). The terminal status of
            that envelope is constrained to :data:`EnvelopeStatus`.
        output_dir: Optional write-destination directory the skill renders
            into. Canonical name per BOT-06 (D-b3) — ``output_dir`` never
            ``out_dir`` / ``target_dir``. ``None`` means the skill writes no
            files of its own.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    runtime: list[RuntimeId]
    dispatch: dict[str, str | bool | int] = Field(default_factory=dict)
    output_envelope_kind: str
    output_dir: str | None = None

    @field_validator("runtime")
    @classmethod
    def _runtime_non_empty(cls, value: list[RuntimeId]) -> list[RuntimeId]:
        """Reject an empty ``runtime`` list.

        Raises:
            ValueError: ``runtime`` is empty — a skill that no runtime can
                host is a manifest authoring error rather than a valid
                "hidden" skill.
        """
        if not value:
            raise ValueError("runtime must name at least one runtime id; got empty list")
        return value


__all__ = [
    "EnvelopeStatus",
    "RuntimeId",
    "SkillManifest",
]
