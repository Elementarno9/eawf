"""Profile composition subsystem for eawf.

Per ``docs/architecture/profiles.md``, each profile is a YAML body that
declares:

- ``name`` / ``version`` / ``description`` — identity metadata.
- ``state_extensions`` — top-level state-keys the profile requires (e.g.
  ``research`` → ``hypotheses``, ``audits``).
- ``instrument_requirements`` — external tools the profile expects on PATH.
- ``render_blocks`` — chunks of templated text the renderer (Phase 3 W04)
  emits into AGENTS.md / .claude/ skill files.
- ``skills_referenced`` / ``hooks_referenced`` — opt-in inventories that the
  Phase 5 skill registry consumes.

Schema v2 (P25-W15) adds three composability fields on each
:class:`ProfileBody`:

- ``conflicts_with`` — profile ids that cannot coexist with this body.
- ``overrides`` — profile ids whose contributions this body claims.
- ``dispatch_session_policy`` — closed enum consumed by the dispatch layer.

This package owns three concerns:

1. **Loading** — :mod:`eawf.platform.profiles.loader` reads one YAML body via
   :func:`load_profile` and composes a deterministic merged view via
   :func:`load_composed_profile`.
2. **Composition** — :mod:`eawf.platform.profiles.compose` deep-merges a sequence of
   profile bodies into one :class:`ComposedProfile` and records provenance,
   override audit, and non-fatal conflict warnings.
3. **Models** — :mod:`eawf.platform.profiles.models` defines the Pydantic v2 schemas
   for both shapes with ``extra="forbid"`` per AGENTS.md rule 2.

Public API:

    ProfileBody, ComposedProfile, RenderBlock, InstrumentReq, StateExtensions
    load_profile, list_profiles, load_composed_profile
    compose, STRICTEST_KEYS, ProfileConflict, ConflictResolution
"""

from __future__ import annotations

from eawf.platform.profiles.compose import (
    STRICTEST_KEYS,
    ConflictResolution,
    ProfileConflict,
    compose,
)
from eawf.platform.profiles.loader import (
    list_profiles,
    load_composed_profile,
    load_profile,
)
from eawf.platform.profiles.models import (
    ComposedProfile,
    InstrumentReq,
    ProfileBody,
    RenderBlock,
    StateExtensions,
)

__all__ = [
    "STRICTEST_KEYS",
    "ComposedProfile",
    "ConflictResolution",
    "InstrumentReq",
    "ProfileBody",
    "ProfileConflict",
    "RenderBlock",
    "StateExtensions",
    "compose",
    "list_profiles",
    "load_composed_profile",
    "load_profile",
]
