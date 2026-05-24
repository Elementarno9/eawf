"""Pure-functional open/close transitions for Phase/Iter/Wave.

Every helper mutates the supplied :class:`State` in place and returns either
the affected entity or a small NamedTuple of relevant fields. The CLI
handlers call these inside a held sibling lock; tests call them directly to
keep transitions fast.

Design rules:

- Transitions only enforce **structural** guards (parent open/closed, status
  matches expected before-state). Schema-level invariants (URN regex, enum
  values) live on the Pydantic models. Cross-entity invariants (e.g.
  ``current.phase_id`` must be open) run via :func:`validate_state` on the
  candidate state after the mutation.
- Every transition raises :class:`LifecycleError` on rejection — the CLI
  layer translates that into the right exit code (mostly ``INVALID_INPUT``
  but ``VALIDATION_FAILED`` for closure guards).

The transitions are physically organised per entity to keep each module
under the Q25 LOC cap; this module re-exports the full surface so the
``eawf.lifecycle.transitions`` import path keeps working unchanged:

- :class:`LifecycleError` — :mod:`eawf.lifecycle._errors`
- project/subproject helpers — :mod:`eawf.lifecycle.project`
- phase helpers — :mod:`eawf.lifecycle.phase`
- iter helpers — :mod:`eawf.lifecycle.iter_`
- wave helpers — :mod:`eawf.lifecycle.wave`
"""

from __future__ import annotations

from eawf.lifecycle._errors import LifecycleError
from eawf.lifecycle.iter_ import (
    activate_iter,
    close_iter,
    edit_iter_plan,
    open_iter,
    plan_iter,
)
from eawf.lifecycle.phase import (
    activate_phase,
    archive_phase,
    close_phase,
    has_scope_collapse_decision,
    open_phase,
    plan_phase,
    reopen_phase,
)
from eawf.lifecycle.project import (
    add_subproject,
    switch_subproject,
)
from eawf.lifecycle.wave import (
    claim_wave,
    close_wave,
    edit_wave_plan,
    fail_wave,
    plan_wave,
    release_wave,
    remove_wave_plan,
    set_wave_deps,
    start_wave,
)

__all__ = [
    "LifecycleError",
    "activate_iter",
    "activate_phase",
    "add_subproject",
    "archive_phase",
    "claim_wave",
    "close_iter",
    "close_phase",
    "close_wave",
    "edit_iter_plan",
    "edit_wave_plan",
    "fail_wave",
    "has_scope_collapse_decision",
    "open_iter",
    "open_phase",
    "plan_iter",
    "plan_phase",
    "plan_wave",
    "release_wave",
    "remove_wave_plan",
    "reopen_phase",
    "set_wave_deps",
    "start_wave",
    "switch_subproject",
]
