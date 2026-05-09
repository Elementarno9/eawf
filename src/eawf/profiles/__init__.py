"""Profile composition subsystem for eawf.

Per ``ea-proposal.md`` §"v0.1 profile bodies", each profile is a YAML body that
declares:

- ``name`` / ``version`` / ``description`` — identity metadata.
- ``state_extensions`` — top-level state-keys the profile requires (e.g.
  ``research`` → ``hypotheses``, ``audits``).
- ``instrument_requirements`` — external tools the profile expects on PATH.
- ``render_blocks`` — chunks of templated text the renderer (Phase 3 W04)
  emits into AGENTS.md / .claude/ skill files.
- ``skills_referenced`` / ``hooks_referenced`` — opt-in inventories that the
  Phase 5 skill registry consumes.

This package owns three concerns:

1. **Loading** — :mod:`eawf.profiles.loader` reads one YAML body from
   ``data/<id>.yaml`` and turns it into a :class:`ProfileBody`.
2. **Composition** — :mod:`eawf.profiles.compose` deep-merges a sequence of
   profile bodies into one :class:`ComposedProfile` and records provenance for
   each top-level key.
3. **Models** — :mod:`eawf.profiles.models` defines the Pydantic v2 schemas
   for both shapes with ``extra="forbid"`` per AGENTS.md rule 2.

Public API:

    ProfileBody, ComposedProfile, RenderBlock, InstrumentReq, StateExtensions
    load_profile, list_profiles
    compose, STRICTEST_KEYS
"""

from __future__ import annotations

from eawf.profiles.compose import STRICTEST_KEYS, compose
from eawf.profiles.loader import list_profiles, load_profile
from eawf.profiles.models import (
    ComposedProfile,
    InstrumentReq,
    ProfileBody,
    RenderBlock,
    StateExtensions,
)

__all__ = [
    "STRICTEST_KEYS",
    "ComposedProfile",
    "InstrumentReq",
    "ProfileBody",
    "RenderBlock",
    "StateExtensions",
    "compose",
    "list_profiles",
    "load_profile",
]
