"""Tests for the key-press affordance probe + ``affordance_parity`` kind (FS09).

Two capabilities, one question -- "does an advertised key resolve":

* the key-press driver
  :func:`~eawf.surfaces.tui.snapshot.behaviour_probe.record_keypress_transcript`
  drives the REAL key->Binding path (it presses the key) rather than the
  action-string path, so it sees a dead affordance (a key with no resolving
  binding) the action-string driver routes around; and
* the
  :func:`~eawf.workflow.audit_dsl.kinds.affordance_parity.check_affordance_parity`
  audit kind enumerates a mode screen's advertised footer keys and fails,
  naming each offending key, when an advertised key resolves to no binding.

Coverage:

* CR-1 (triggers_action) -- pressing ``c`` in a mode screen resolves to
  ``open_config`` (the config modal opens; the press classifies observable,
  not unresolved / no-op).
* CR-2 (returns) -- a key with no binding classifies ``UNRESOLVED`` via the
  driver, and the check flags it (naming the key).
* boundary -- a real mode whose every footer key resolves passes
  (``status="pass"``).
* error-path -- the check degrades a malformed ``args`` (missing / non-str
  ``mode``, bad ``size``, missing fixture) to ``status="fail"`` rather than
  raising; the registry dispatches the kind.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.snapshot.behaviour_probe import (
    ProbeStatus,
    record_keypress_transcript,
)
from eawf.surfaces.tui.snapshot.pilot_harness import settle_screen
from eawf.workflow.audit_dsl import CHECK_REGISTRY, CheckResult, CheckSpec
from eawf.workflow.audit_dsl import models as models_module
from eawf.workflow.audit_dsl.kinds import affordance_parity as ap_module
from eawf.workflow.audit_dsl.kinds.affordance_parity import check_affordance_parity

if TYPE_CHECKING:
    from textual.pilot import Pilot

# tests/snapshots/tui/test_affordance_parity.py -> parents[3] is the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_REPO_STATE_REL = "tests/fixtures/states/valid/03-phase-iter-wave-active.json"
_REPO_STATE = (_REPO_ROOT / _REPO_STATE_REL).resolve()
_SIZE = (120, 40)
_COMMIT = "abc1234"

# ``c`` is bound app-wide to ``open_config`` on ``EaApp.BINDINGS`` so it
# resolves from every mode screen (the mode screens subclass ScopeScreen,
# which never bound it -- a scope-only binding left config unreachable from
# any mode, which W11 fixed by lifting ``c`` to the app).
_CONFIG_KEY = "c"
# A key bound to no action anywhere in the app -- the dead-affordance shape.
_DEAD_KEY = "z"


async def _press_in_mode(pilot: Pilot[object], mode: str, key: str) -> ProbeStatus:
    """Switch *pilot*'s app to *mode*, drive *key*, return its probe status."""
    app = pilot.app
    await settle_screen(pilot)
    await app.switch_mode(mode)  # type: ignore[attr-defined]
    await settle_screen(pilot)
    transcript = await record_keypress_transcript(pilot, [key], source_commit=_COMMIT)
    return transcript.outcomes[0].status


def _status_in_mode(mode: str, key: str) -> ProbeStatus:
    """Run the key-press probe for *key* in *mode* against a fresh app."""

    async def body() -> ProbeStatus:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            return await _press_in_mode(pilot, mode, key)

    return asyncio.run(body())


# --------------------------------------------------------------------------
# CR-1: pressing `c` in a mode screen resolves to open_config (config opens)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["home", "doctor", "trust", "evidence"])
def test_press_c_in_mode_resolves_to_open_config(mode: str) -> None:
    # The press resolves to a real binding (NOT unresolved) and does
    # something observable (the config modal opens), so the affordance the
    # footer advertises is live in every mode.
    status = _status_in_mode(mode, _CONFIG_KEY)
    assert status is ProbeStatus.OBSERVABLE
    assert status is not ProbeStatus.UNRESOLVED


def test_press_c_in_mode_binds_open_config_action() -> None:
    # Inspect the active binding map directly: ``c`` -> ``open_config`` is
    # the resolution CR-1 names, app-wide.
    async def body() -> str | None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await app.switch_mode("doctor")
            await settle_screen(pilot)
            entry = app.screen.active_bindings.get(_CONFIG_KEY)
            return entry.binding.action if entry is not None else None

    assert asyncio.run(body()) == "open_config"


def test_press_c_opens_config_modal_signal() -> None:
    # The observable signal a ``c`` press moves is the modal stack growing
    # (the ConfigModal pushes), so the affordance is unmistakably live.
    async def body() -> tuple[str, ...]:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await app.switch_mode("doctor")
            await settle_screen(pilot)
            transcript = await record_keypress_transcript(
                pilot, [_CONFIG_KEY], source_commit=_COMMIT
            )
            return transcript.outcomes[0].signals

    signals = asyncio.run(body())
    joined = "; ".join(signals)
    assert "modal_depth" in joined


# --------------------------------------------------------------------------
# CR-2: a key with no resolving binding classifies UNRESOLVED
# --------------------------------------------------------------------------


def test_press_dead_key_classifies_unresolved() -> None:
    status = _status_in_mode("doctor", _DEAD_KEY)
    assert status is ProbeStatus.UNRESOLVED


def test_keypress_transcript_carries_key_string_in_action() -> None:
    # The ``action`` field holds the key string for a key probe, so the
    # transcript renders through the shared evidence formatter unchanged.
    async def body() -> str:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            transcript = await record_keypress_transcript(pilot, [_DEAD_KEY], source_commit=_COMMIT)
            return transcript.outcomes[0].action

    assert asyncio.run(body()) == _DEAD_KEY


def test_keypress_transcript_is_deterministic_across_runs() -> None:
    # Worker-drained determinism: the same probe list yields the same typed
    # transcript across two runs.
    async def body() -> object:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            return await record_keypress_transcript(
                pilot, [_CONFIG_KEY, _DEAD_KEY], source_commit=_COMMIT
            )

    first = asyncio.run(body())
    second = asyncio.run(body())
    assert first == second


# --------------------------------------------------------------------------
# boundary: a real mode whose every footer key resolves passes
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["home", "doctor", "autopilot"])
def test_check_affordance_parity_real_mode_passes(mode: str) -> None:
    spec = CheckSpec(
        kind="affordance_parity",
        name=f"{mode}-parity",
        args={"mode": mode, "state_path": _REPO_STATE_REL},
    )
    result = check_affordance_parity(spec, _REPO_ROOT)
    assert result.status == "pass", result.details
    assert result.passed is True


def test_check_affordance_parity_no_state_path_passes() -> None:
    # ``state_path`` is optional; the app launches with no bound state and
    # the home footer keys still resolve.
    spec = CheckSpec(
        kind="affordance_parity",
        name="home-no-state",
        args={"mode": "home"},
    )
    result = check_affordance_parity(spec, _REPO_ROOT)
    assert result.status == "pass", result.details


def test_check_affordance_parity_passes_inside_running_event_loop() -> None:
    # The deterministic close gate scores the floor synchronously while the
    # daemon JSON-RPC handler's event loop is running; a bare ``asyncio.run``
    # would raise "cannot be called from a running event loop" and degrade the
    # check to a spurious fail. The thread-offload path keeps it passing.
    spec = CheckSpec(
        kind="affordance_parity",
        name="home-in-loop",
        args={"mode": "home"},
    )

    async def body() -> CheckResult:
        return check_affordance_parity(spec, _REPO_ROOT)

    result = asyncio.run(body())
    assert result.status == "pass", result.details


# --------------------------------------------------------------------------
# error-path: a synthetic dead advertised key fails with the key named
# --------------------------------------------------------------------------


def test_check_affordance_parity_dead_advertised_key_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Inject a synthetic advertised key with no binding: the check probes it,
    # classifies it UNRESOLVED, and fails naming the key.
    async def _fake_advertised(
        *, mode: str, state_path: Path | None, size: tuple[int, int]
    ) -> list[str]:
        return [_DEAD_KEY]

    monkeypatch.setattr(ap_module, "_advertised_keys", _fake_advertised)
    spec = CheckSpec(
        kind="affordance_parity",
        name="dead-key",
        args={"mode": "doctor", "state_path": _REPO_STATE_REL},
    )
    result = check_affordance_parity(spec, _REPO_ROOT)
    assert result.status == "fail"
    assert result.passed is False
    assert result.details is not None
    assert _DEAD_KEY in result.details


def test_check_affordance_parity_missing_mode_arg_fails() -> None:
    spec = CheckSpec(
        kind="affordance_parity",
        name="no-mode",
        args={"state_path": _REPO_STATE_REL},
    )
    result = check_affordance_parity(spec, _REPO_ROOT)
    assert result.status == "fail"
    assert result.details is not None
    assert "mode" in result.details


def test_check_affordance_parity_non_str_mode_arg_fails() -> None:
    spec = CheckSpec(
        kind="affordance_parity",
        name="bad-mode-type",
        args={"mode": 5},
    )
    result = check_affordance_parity(spec, _REPO_ROOT)
    assert result.status == "fail"
    assert result.details is not None
    assert "mode" in result.details


def test_check_affordance_parity_bad_size_arg_fails() -> None:
    spec = CheckSpec(
        kind="affordance_parity",
        name="bad-size",
        args={"mode": "home", "size": [120]},
    )
    result = check_affordance_parity(spec, _REPO_ROOT)
    assert result.status == "fail"
    assert result.details is not None
    assert "size" in result.details


def test_check_affordance_parity_missing_state_file_fails() -> None:
    spec = CheckSpec(
        kind="affordance_parity",
        name="missing-state",
        args={"mode": "home", "state_path": "does/not/exist.json"},
    )
    result = check_affordance_parity(spec, _REPO_ROOT)
    assert result.status == "fail"
    assert result.details is not None
    assert "does/not/exist.json" in result.details


def test_check_affordance_parity_unknown_mode_fails() -> None:
    # An unregistered mode name degrades to a fail (the switch_mode raises;
    # the check catches it), never an aborted run.
    spec = CheckSpec(
        kind="affordance_parity",
        name="unknown-mode",
        args={"mode": "nonexistent", "state_path": _REPO_STATE_REL},
    )
    result = check_affordance_parity(spec, _REPO_ROOT)
    assert result.status == "fail"
    assert result.details is not None
    assert "nonexistent" in result.details


# --------------------------------------------------------------------------
# registry dispatch + CheckKind Literal source
# --------------------------------------------------------------------------


def test_check_registry_affordance_parity_dispatches() -> None:
    spec = CheckSpec(
        kind="affordance_parity",
        name="registry",
        args={"mode": "home", "state_path": _REPO_STATE_REL},
    )
    fn = CHECK_REGISTRY["affordance_parity"]
    result = fn(spec, _REPO_ROOT)
    assert result.status == "pass", result.details


def test_check_kind_literal_source_contains_affordance_parity() -> None:
    source = inspect.getsource(models_module)
    assert '"affordance_parity"' in source
