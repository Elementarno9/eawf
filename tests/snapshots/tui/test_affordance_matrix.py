"""TUI affordance-resolves matrix over MODES x footer keys (FS10).

The dead-hotkey incident (the footer advertised ``c config`` but only the
scope screens bound ``c``, so every mode screen advertised a key that
resolved to NO binding) was caught after the fact by a single hand-fix
covering one mode. This matrix is the regression killer: it asserts that
for EVERY mode screen registered in
:data:`~eawf.surfaces.tui.modes.registry.MODE_REGISTRY` and EVERY key that
mode's footer advertises, the key resolves to a real
:class:`~textual.binding.Binding` (it does NOT classify
:data:`~eawf.surfaces.tui.snapshot.behaviour_probe.ProbeStatus.UNRESOLVED`).

Enumerating the mode axis from the registry -- not a hardcoded list --
means a future mode auto-joins the matrix the moment its
:class:`~eawf.surfaces.tui.modes.registry.ModeSpec` row lands, so a new
pane cannot advertise a dead footer key without this test catching it.

Semantics (aligned with the FS09 affordance-parity check):

* A **dead affordance** is an advertised key that resolves to NO binding
  -- :data:`ProbeStatus.UNRESOLVED`. That is the exact dead-``c`` bug:
  the footer promises a key but no :class:`~textual.binding.Binding`
  answers it.
* A resolved binding whose effect the coarse observable-signal set cannot
  see (an intra-pane cursor move, an F5 refresh, a scope re-select that
  lands the same scope) classifies :data:`ProbeStatus.NO_OP`. That is NOT
  a dead affordance -- a resolving binding IS a present affordance -- so
  the matrix asserts each advertised key is NOT ``UNRESOLVED``; it does
  NOT require ``OBSERVABLE``.

Coverage:

* CR-1 (triggers_action, tui_pilot) -- for each mode in the registry,
  every advertised footer key resolves (not ``UNRESOLVED``).
* boundary -- the mode axis is enumerated from
  :data:`MODE_REGISTRY` (the parametrization covers ``len(MODE_REGISTRY)``
  modes, so a new mode auto-joins), and every advertised key in every mode
  resolves.
* error-path -- a synthetic advertised key with NO resolving binding is
  driven through both the key-press probe and the
  :func:`~eawf.workflow.audit_dsl.kinds.affordance_parity.check_affordance_parity`
  matrix logic; both flag it ``UNRESOLVED`` / ``fail``, proving the matrix
  catches a dead key rather than passing vacuously.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

import pytest

from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modes.nav import NAV_SCOPES, legal_scopes_for_mode
from eawf.surfaces.tui.modes.registry import MODE_REGISTRY
from eawf.surfaces.tui.snapshot.behaviour_probe import (
    DEFERRED_KEYS,
    ProbeStatus,
    _advertised_keys_at,
    _unresolved_keys_at,
    record_keypress_transcript,
    sweep_unresolved_affordances,
)
from eawf.surfaces.tui.snapshot.pilot_harness import settle_screen
from eawf.workflow.audit_dsl import CheckSpec
from eawf.workflow.audit_dsl.kinds import affordance_parity as ap_module
from eawf.workflow.audit_dsl.kinds.affordance_parity import (
    _advertised_keys,
    check_affordance_parity,
)

if TYPE_CHECKING:
    from textual.pilot import Pilot

# tests/snapshots/tui/test_affordance_matrix.py -> parents[3] is the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_REPO_STATE_REL = "tests/fixtures/states/valid/03-phase-iter-wave-active.json"
_REPO_STATE = (_REPO_ROOT / _REPO_STATE_REL).resolve()

#: The Pilot terminal size the matrix runs at. Wide enough that the full
#: footer hint strip renders without clipping any advertised token (so no
#: advertised key is dropped from the enumeration by a too-narrow frame),
#: matching the affordance-parity check's default.
_SIZE: tuple[int, int] = (120, 40)

_COMMIT = "matrix01"

#: The set of mode names the matrix covers, enumerated from the registry so
#: a new mode auto-joins. Each is one parametrize case.
_MODE_NAMES: tuple[str, ...] = tuple(spec.name for spec in MODE_REGISTRY)

#: A key bound to no action anywhere in the app -- the synthetic dead-
#: affordance shape the error-path drives to prove the matrix is not
#: vacuous.
_DEAD_KEY = "z"


# --------------------------------------------------------------------------
# boundary: the mode axis is the whole registry (a new mode auto-joins)
# --------------------------------------------------------------------------


def test_matrix_mode_axis_covers_every_registered_mode() -> None:
    # The parametrized mode axis is enumerated from MODE_REGISTRY, not a
    # hardcoded list, so its length equals the registry's -- a new ModeSpec
    # row joins the matrix with no test edit.
    assert len(_MODE_NAMES) == len(MODE_REGISTRY)
    assert set(_MODE_NAMES) == {spec.name for spec in MODE_REGISTRY}


# --------------------------------------------------------------------------
# CR-1: every (mode, advertised-key) pair resolves (not UNRESOLVED)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", _MODE_NAMES)
def test_mode_advertises_at_least_one_resolving_key(mode: str) -> None:
    # Every mode screen advertises a footer strip (at minimum the injected
    # ``c config`` / ``F5 refresh`` globals), so the enumeration is never
    # empty -- a vacuous pass (no keys to probe) would let a dead-key mode
    # slip through.
    async def body() -> list[str]:
        return await _advertised_keys(mode=mode, state_path=_REPO_STATE, size=_SIZE)

    keys = asyncio.run(body())
    assert keys, f"mode {mode!r} advertised no footer keys"


@pytest.mark.parametrize("mode", _MODE_NAMES)
def test_every_advertised_key_in_mode_resolves(mode: str) -> None:
    # The matrix cell for one mode: enumerate the mode's advertised footer
    # keys, then classify each against the resolved binding map -- a pure,
    # press-free ``active_bindings`` read (the W12 sweep path). An UNRESOLVED
    # key is one absent from the map (the advertised-but-dead affordance); a
    # resolving binding IS a present affordance (NO_OP is allowed), so the
    # cell passes iff no advertised key is unresolved. The press-free read
    # mounts twice per cell (enumerate + read) instead of pressing every
    # advertised key against its own fresh mount (~1 mount per advertised
    # key), and gives the identical UNRESOLVED verdict the driver would.
    async def body() -> list[str]:
        keys = await _advertised_keys_at(
            scope="repo", mode=mode, state_path=_REPO_STATE, size=_SIZE
        )
        return await _unresolved_keys_at(
            scope="repo", mode=mode, keys=keys, state_path=_REPO_STATE, size=_SIZE
        )

    dead = asyncio.run(body())
    assert not dead, f"mode {mode!r} advertises unresolved key(s): {', '.join(dead)}"


@pytest.mark.parametrize("mode", _MODE_NAMES)
def test_check_affordance_parity_passes_for_every_mode(mode: str) -> None:
    # The same matrix expressed through the FS09 audit kind: for each mode,
    # the parity check passes (every advertised footer key resolves). This is
    # the matrix as a single dispatchable check per mode.
    spec = CheckSpec(
        kind="affordance_parity",
        name=f"{mode}-matrix",
        args={"mode": mode, "state_path": _REPO_STATE_REL},
    )
    result = check_affordance_parity(spec, _REPO_ROOT)
    assert result.status == "pass", result.details
    assert result.passed is True


# --------------------------------------------------------------------------
# error-path: a synthetic dead advertised key is caught (not vacuous)
# --------------------------------------------------------------------------


async def _press_dead_key_in_mode(pilot: Pilot[object], mode: str, key: str) -> ProbeStatus:
    """Switch *pilot*'s app to *mode*, drive *key*, return its probe status."""
    app = pilot.app
    await settle_screen(pilot)
    await app.switch_mode(mode)  # type: ignore[attr-defined]
    await settle_screen(pilot)
    transcript = await record_keypress_transcript(pilot, [key], source_commit=_COMMIT)
    return transcript.outcomes[0].status


def test_synthetic_dead_key_classifies_unresolved() -> None:
    # Drive a key bound to nothing through the SAME key-press probe the matrix
    # uses, against a real mode screen. It classifies UNRESOLVED -- proving
    # the matrix logic detects a dead key rather than passing vacuously.
    async def body() -> ProbeStatus:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            return await _press_dead_key_in_mode(pilot, "doctor", _DEAD_KEY)

    assert asyncio.run(body()) is ProbeStatus.UNRESOLVED


def test_matrix_fails_when_footer_advertises_a_dead_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Synthesize a footer that advertises a key with NO resolving binding by
    # injecting it into the advertised-key enumeration, then run the matrix
    # logic (the FS09 check). It probes the synthetic key, classifies it
    # UNRESOLVED, and FAILS -- naming the offending key. This proves the
    # matrix catches a dead key; a vacuous matrix would pass here.
    async def _fake_advertised(
        *, mode: str, state_path: Path | None, size: tuple[int, int]
    ) -> list[str]:
        return [_DEAD_KEY]

    monkeypatch.setattr(ap_module, "_advertised_keys", _fake_advertised)
    spec = CheckSpec(
        kind="affordance_parity",
        name="synthetic-dead-key",
        args={"mode": "doctor", "state_path": _REPO_STATE_REL},
    )
    result = check_affordance_parity(spec, _REPO_ROOT)
    assert result.status == "fail"
    assert result.passed is False
    assert result.details is not None
    assert _DEAD_KEY in result.details


# --------------------------------------------------------------------------
# CR-01 (W12): the sweep over NAV_SCOPES x MODE_REGISTRY reports zero
# unresolved advertised keys outside DEFERRED_KEYS
# --------------------------------------------------------------------------


class _SweepRun(NamedTuple):
    """One shared ``_REPO_STATE`` sweep: its result, mount count, cell budget."""

    result: tuple[str, ...]
    mounts: int
    legal_cells: int


@pytest.fixture(scope="module")
def repo_state_sweep() -> _SweepRun:
    """Run the ``_REPO_STATE`` sweep ONCE (under a mount counter) for reuse.

    The sweep mounts ~2x per legal cell; the result-is-green assert and the
    mount-budget assert are two independent reads of the SAME sweep, so a
    single module-scoped run feeds both instead of remounting the whole
    matrix twice. Counting real ``EaApp.run_test`` entries captures the
    mount budget the guard test asserts.
    """
    legal_cells = sum(len(legal_scopes_for_mode(spec.name)) for spec in MODE_REGISTRY)
    mounts = 0
    original_run_test = EaApp.run_test

    def counting_run_test(self: EaApp, **kwargs: object) -> object:
        nonlocal mounts
        mounts += 1
        return original_run_test(self, **kwargs)

    # Module-scoped patch (a plain ``monkeypatch`` fixture is function-scoped
    # and would ScopeMismatch here); the context auto-restores run_test.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(EaApp, "run_test", counting_run_test)
        result = asyncio.run(sweep_unresolved_affordances(state_path=_REPO_STATE))
    return _SweepRun(result=result, mounts=mounts, legal_cells=legal_cells)


def test_sweep_reports_zero_unresolved_keys(repo_state_sweep: _SweepRun) -> None:
    # The W12 gate: drive every legal (scope, mode) cell's advertised footer
    # keys through the real key->Binding path and assert NONE resolve to no
    # binding (outside the documented DEFERRED_KEYS allowlist). A footer that
    # newly advertises a dead key surfaces here as a "<scope>/<mode>/<key>"
    # triple; a green sweep is the empty tuple.
    unresolved = repo_state_sweep.result
    assert unresolved == (), f"sweep found unresolved advertised keys: {', '.join(unresolved)}"


def test_sweep_covers_every_legal_scope_mode_cell() -> None:
    # boundary: the sweep axis is the whole NAV_SCOPES x MODE_REGISTRY product
    # restricted to legal cells. The cell count equals the sum over modes of
    # the legal scopes for that mode -- a new mode or scope auto-joins the
    # sweep with no test edit. (Verifies the sweep is not vacuous over an
    # empty axis.)
    expected_cells = sum(len(legal_scopes_for_mode(spec.name)) for spec in MODE_REGISTRY)
    assert expected_cells > 0
    # Every mode is legal at repo + workspace (the two single-state scopes);
    # only home + doctor are also legal at the user portfolio scope, so the
    # cell count is strictly greater than two per-scope rows would give.
    assert expected_cells >= 2 * len(MODE_REGISTRY)


def test_sweep_axis_is_subset_of_nav_scopes() -> None:
    # boundary: every scope the sweep visits is a real NAV_SCOPES entry (no
    # mode advertises a scope outside the three-scope axis), so the sweep can
    # never probe an unreachable cell.
    swept_scopes = {scope for spec in MODE_REGISTRY for scope in legal_scopes_for_mode(spec.name)}
    assert swept_scopes
    assert swept_scopes <= set(NAV_SCOPES)


def test_sweep_no_state_path_reports_zero_unresolved() -> None:
    # boundary: state_path is optional. The app launches with no bound state
    # (the user-scope no-state shape) and the swept footer keys still all
    # resolve -- a None fixture is a legal, green sweep input.
    unresolved = asyncio.run(sweep_unresolved_affordances(state_path=None))
    assert unresolved == (), f"no-state sweep found unresolved keys: {', '.join(unresolved)}"


def test_sweep_mounts_at_most_twice_per_legal_cell(repo_state_sweep: _SweepRun) -> None:
    # W12 instrumentation guard: the sweep reads each cell's binding map once
    # instead of pressing every advertised key against its own fresh mount, so
    # the mount count is bounded at twice the legal-cell count (one mount to
    # enumerate the footer, one to read the map) -- never once per advertised
    # key (~250 mounts). Counting real EaApp.run_test entries proves the budget
    # holds and, transitively, that no per-key press remount survives.
    mounts = repo_state_sweep.mounts
    legal_cells = repo_state_sweep.legal_cells
    unresolved = repo_state_sweep.result
    assert mounts > 0, "sweep never mounted the app -- vacuous over an empty axis"
    assert mounts <= 2 * legal_cells, (
        f"sweep mounted {mounts} times over the 2x{legal_cells}-legal-cell budget"
    )
    assert unresolved == ()


def test_sweep_deferred_keys_excluded_from_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # error-path / not-vacuous: inject a synthetic dead advertised key into
    # the enumeration of one cell, then assert the sweep reports it -- proving
    # the sweep catches a dead key rather than passing vacuously. Re-running
    # with that exact triple in DEFERRED_KEYS suppresses it, proving the
    # allowlist is the documented escape hatch and nothing else.
    import eawf.surfaces.tui.snapshot.behaviour_probe as bp

    async def _fake_advertised(
        *, scope: str, mode: str, state_path: object, size: object
    ) -> list[str]:
        # Only the repo/doctor cell advertises the synthetic dead key; every
        # other cell advertises nothing, so the sweep probes exactly one key.
        if scope == "repo" and mode == "doctor":
            return [_DEAD_KEY]
        return []

    monkeypatch.setattr(bp, "_advertised_keys_at", _fake_advertised)
    triple = f"repo/doctor/{_DEAD_KEY}"

    # Without a deferral the dead key is reported (caught, not vacuous).
    unresolved = asyncio.run(sweep_unresolved_affordances(state_path=_REPO_STATE))
    assert triple in unresolved

    # With the exact triple deferred the same sweep suppresses it.
    monkeypatch.setattr(bp, "DEFERRED_KEYS", frozenset({triple}))
    deferred = asyncio.run(sweep_unresolved_affordances(state_path=_REPO_STATE))
    assert triple not in deferred


def test_deferred_keys_is_a_frozenset() -> None:
    # The allowlist is an immutable, set-membership-checked constant so a
    # deferral is an explicit triple, never a mutable list a caller can grow
    # at runtime. Empty today: no cell has a known-pending dead affordance.
    assert isinstance(DEFERRED_KEYS, frozenset)
    assert frozenset() == DEFERRED_KEYS
