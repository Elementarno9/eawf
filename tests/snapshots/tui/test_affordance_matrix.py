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
from typing import TYPE_CHECKING

import pytest

from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modes.registry import MODE_REGISTRY
from eawf.surfaces.tui.snapshot.behaviour_probe import (
    ProbeStatus,
    record_keypress_transcript,
)
from eawf.surfaces.tui.snapshot.pilot_harness import settle_screen
from eawf.workflow.audit_dsl import CheckSpec
from eawf.workflow.audit_dsl.kinds import affordance_parity as ap_module
from eawf.workflow.audit_dsl.kinds.affordance_parity import (
    _advertised_keys,
    _probe_key,
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
    # keys, drive each through the real key->Binding path (per-key fresh
    # mount + settle_screen so a destructive key cannot bleed into the next),
    # and assert NONE classify UNRESOLVED -- every advertised key resolves to
    # a binding. NO_OP is allowed (a resolving binding is a present
    # affordance); only UNRESOLVED is a dead affordance.
    async def body() -> dict[str, ProbeStatus]:
        keys = await _advertised_keys(mode=mode, state_path=_REPO_STATE, size=_SIZE)
        statuses: dict[str, ProbeStatus] = {}
        for key in keys:
            statuses[key] = await _probe_key(mode=mode, key=key, state_path=_REPO_STATE, size=_SIZE)
        return statuses

    statuses = asyncio.run(body())
    dead = [key for key, status in statuses.items() if status is ProbeStatus.UNRESOLVED]
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
