"""Lifecycle helpers (project/subproject/phase/iter/wave).

The lifecycle package houses pure-functional helpers that the
:mod:`eawf.cli.commands.lifecycle` Typer handlers compose under a held
sibling lock. The split keeps the CLI handlers thin (parse → emit) and lets
the allocator/transition logic be unit-tested without spinning up Typer.

The transition helpers are physically organised per entity
(:mod:`~eawf.lifecycle.project`, :mod:`~eawf.lifecycle.phase`,
:mod:`~eawf.lifecycle.iter_`, :mod:`~eawf.lifecycle.wave`) to keep each
module under the Q25 LOC cap; the full surface is re-exported from
:mod:`eawf.lifecycle.transitions` and from this package root so both
``from eawf.lifecycle import open_phase`` and
``from eawf.lifecycle.transitions import open_phase`` resolve unchanged.
"""

from __future__ import annotations

from eawf.lifecycle.transitions import (
    LifecycleError,
    activate_iter,
    activate_phase,
    add_subproject,
    archive_phase,
    claim_wave,
    close_iter,
    close_phase,
    close_wave,
    edit_wave_plan,
    fail_wave,
    open_iter,
    open_phase,
    plan_iter,
    plan_phase,
    plan_wave,
    remove_wave_plan,
    reopen_phase,
    set_wave_deps,
    switch_subproject,
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
    "edit_wave_plan",
    "fail_wave",
    "open_iter",
    "open_phase",
    "plan_iter",
    "plan_phase",
    "plan_wave",
    "remove_wave_plan",
    "reopen_phase",
    "set_wave_deps",
    "switch_subproject",
]
