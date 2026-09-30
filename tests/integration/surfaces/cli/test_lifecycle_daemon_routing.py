"""Verb→daemon routing + WAL-backed in-process fallback for lifecycle verbs.

P27-I02-W18 routed the eager-scope mutating lifecycle verbs through the
generic :func:`eawf.surfaces.cli._dispatch._mutate_via_daemon` shim (rule 4: the
daemon is the canonical mutator) and flipped the in-process fallback in
:func:`eawf.surfaces.cli.commands.lifecycle._commit_mutation` to a **state-first,
WAL-backed** ordering that mirrors the daemon's outcome-WAL algorithm.

The suite has three planes:

1. **Routing** — with ``daemon.proxy_enabled=True`` + a reachable daemon,
   each routed verb marshals one typed
   :class:`~eawf.kernel.state.mutations.Mutation` of the correct
   :class:`~eawf.kernel.state.mutations.MutationKind` across ``state.mutate``;
   the in-process fallback does NOT run.
2. **Registry** — the verb→kind table the routing test parametrises is
   pinned against the daemon's apply registry so a kind can never be
   routed that the daemon cannot dispatch.
3. **Fallback + crash safety** — with the daemon down (transport error)
   the in-process WAL-backed writer carries the mutation; an injected
   crash between the state write and the event append leaves NO phantom
   event (no event row whose state change did not commit) and the next
   mutation's ``replay_wal`` reconciles the ``.pending`` record.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from eawf.kernel.state.mutations import MutationKind

pytestmark = pytest.mark.integration

runner = CliRunner()


# ---- fixtures ---------------------------------------------------------------


# ---- fake daemon client -----------------------------------------------------


# ---- plane 1: routing -------------------------------------------------------

#: Verb argv (after the bootstrap to a PENDING wave) → expected MutationKind.
#: Each tuple is ``(test_id, argv, expected_kind, expected_scope_id)``.
_ROUTED_VERBS: list[tuple[str, list[str], MutationKind, str]] = [
    (
        "wave_plan",
        [
            "wave",
            "plan",
            "P01-I01",
            "--id",
            "P01-I01-W02",
            "--title",
            "w2",
            "--files",
            "src/",
            "--effort-bucket",
            "M",
        ],
        MutationKind.ROADMAP_REVISE,
        "P01-I01-W02",
    ),
    (
        "wave_claim",
        ["wave", "claim", "P01-I01-W01", "--session", "S-1"],
        MutationKind.WAVE_CLAIM,
        "P01-I01-W01",
    ),
    (
        "wave_fail",
        ["wave", "fail", "P01-I01-W01", "--reason", "boom"],
        MutationKind.WAVE_FAIL,
        "P01-I01-W01",
    ),
    (
        "phase_activate",
        ["phase", "activate", "P01"],
        MutationKind.PHASE_ACTIVATE,
        "P01",
    ),
]


# ---- plane 2: registry ------------------------------------------------------


def test_routed_kinds_are_all_in_daemon_apply_registry() -> None:
    """Every routed kind resolves to a real daemon apply function.

    A verb can only proxy a kind the daemon can dispatch; pinning the
    routed kinds against the apply registry stops a verb from routing a
    kind the daemon would reject with ``-32601``.
    """
    from eawf.runtime.daemon.methods.state import _APPLY_REGISTRY

    routed_kinds = {kind for _id, _argv, kind, _scope in _ROUTED_VERBS}
    routed_kinds.add(MutationKind.ITER_CLOSE)
    routed_kinds.add(MutationKind.PHASE_CLOSE)
    routed_kinds.add(MutationKind.TRACK_ADD)
    routed_kinds.add(MutationKind.TRACK_SWITCH)
    assert routed_kinds <= set(_APPLY_REGISTRY)


# ---- plane 3: fallback + crash safety ---------------------------------------
