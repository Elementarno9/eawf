"""Canonical ``PluginManifest`` schema (C07a §5.7, XB19).

The manifest is the single canonical shape that feeds the three
per-runtime render paths (Claude, Codex, OpenCode). It is loaded
from ``build/<runtime>-plugin/manifest.yaml`` and validated via
Pydantic v2 :class:`BaseModel` with ``extra="forbid"`` so a new
field at v0.4 fails-fast on the v0.3 daemon (per F11 in §6).

History
-------

C07a-V9 (added 2026-05-18 per XB10) named per-runtime plugin
manifests as a first-class distribution channel. XB19 (2026-05-18)
corrected the prior brief: the manifest was described as
"Pydantic" but defined as ``@dataclass(frozen=True)``; this
module implements the corrected ``BaseModel`` form.

Boundaries
----------

* :class:`PluginInfo` — name / version / description / runtime
  identifier / generator string. One per manifest.
* :class:`PluginContributes` — declared skills (slash command
  names), agents (subagent roles), and hooks (event-type to
  script-path lists). Lists default to empty for runtimes that
  do not consume a given contribution kind (e.g. OpenCode has
  no top-level ``agents`` block in its native manifest).
* :class:`PluginManaged` — names the fields the rendered output
  carries for drift detection (the field names live in the
  manifest, not the values — values are computed by the
  per-runtime renderer at install time).
* :class:`PluginManifest` — top-level schema with
  ``schema_version: Literal["1.0"]`` per Q5 / BOT-03.

Naming
------

The ``runtime`` field uses the canonical runtime identifiers that
also key :attr:`~eawf.state.models.SessionAttempt.runtime` and
the ``runtime.preference`` config layer:

* ``"claude-code"``
* ``"codex"``
* ``"opencode"``
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

RuntimeId = Literal["claude-code", "codex", "opencode"]


class PluginInfo(BaseModel):
    """Identity block for one plugin manifest.

    Attributes:
        name: Plugin name (``"eawf"`` for the canonical built-in).
        version: SemVer-like version string carried into the
            rendered output for drift detection.
        description: Human-readable single-sentence description
            (shown in marketplace listings + CLI status output).
        runtime: Canonical runtime identifier — one of
            ``"claude-code"`` / ``"codex"`` / ``"opencode"``.
        generator: Generator identifier baked into the rendered
            ``__eawf_managed`` namespace (e.g. ``"eawf-plugin-claude"``).
    """

    model_config = ConfigDict(extra="forbid")
    name: str
    version: str
    description: str
    runtime: RuntimeId
    generator: str


class PluginContributes(BaseModel):
    """Declared contributions a plugin provides.

    Attributes:
        skills: Skill names (slash command stems without leading
            ``/``) the plugin contributes. Ordered for deterministic
            rendering.
        agents: Subagent role names the plugin contributes. Claude
            renders one ``agents/<role>.md`` per entry; Codex nests
            agents inside skills; OpenCode renders
            ``.opencode/agent/<role>.md``.
        hooks: Mapping from hook category name to the list of
            event-type identifiers under that category. Two
            categories: ``"session_level"`` (subscribed at runtime)
            and ``"workflow_level"`` (CLI-fired only).
    """

    model_config = ConfigDict(extra="forbid")
    skills: list[str] = []
    agents: list[str] = []
    hooks: dict[str, list[str]] = {}


class PluginManaged(BaseModel):
    """Names of the managed-namespace fields the renderer emits.

    Attributes:
        body_hash_field: JSON pointer (dotted) to the field that
            carries the blake2b-64 hash of the managed body
            (used by the drift detector).
        timestamp_field: JSON pointer (dotted) to the field that
            carries the rendered-at timestamp.
        source_files: Repo-relative source files whose SHA flows
            into the body hash (changes to these files invalidate
            the rendered output via the doctor).
    """

    model_config = ConfigDict(extra="forbid")
    body_hash_field: str
    timestamp_field: str
    source_files: list[str]


class PluginManifest(BaseModel):
    """Canonical plugin manifest — one file per runtime.

    Loaded from ``build/<runtime>-plugin/manifest.yaml`` and fed
    into the per-runtime renderer that emits the on-disk plugin
    tree. ``schema_version`` is pinned to ``"1.0"`` per Q5 /
    BOT-03 (string MAJOR.MINOR; bumps backward-compatibly).

    Attributes:
        schema_version: Manifest schema version. Literal
            ``"1.0"`` for the v0.3-v0.5 surface.
        plugin: Identity block per :class:`PluginInfo`.
        contributes: Declared contributions per
            :class:`PluginContributes`.
        managed: Managed-namespace field naming per
            :class:`PluginManaged`.
    """

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"] = "1.0"
    plugin: PluginInfo
    contributes: PluginContributes
    managed: PluginManaged


__all__ = [
    "PluginContributes",
    "PluginInfo",
    "PluginManaged",
    "PluginManifest",
    "RuntimeId",
]
