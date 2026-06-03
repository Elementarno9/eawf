"""Unit tests for ``tools/idle_contract_gate.py``.

Covers the deterministic idle-contract gate that guards the band-scoped
spec-jury QC gate against the B091 idle-verifier regression:

- the gate PASSES on the current tree (the producer is importable + the shipped
  ``quality`` profile wires it on for a non-empty UI/UX band that resolves
  band-scoped);
- it FAILS with :attr:`GateFailure.PRODUCER_IDLE` when fed a profile set in
  which no profile enables a verify band (empty ``uiux_bands`` everywhere) --
  the idle-forever detection;
- it FAILS with :attr:`GateFailure.BAND_ENFORCES_GLOBALLY` when the resolver
  is stubbed to enforce for a non-UI wave -- the fleet-wide-flip detection;
- the happy path (producer wired + band present + band-scoped resolver) passes
  explicitly.

The gate module is loaded via :mod:`importlib` because ``tools/`` is excluded
from the package and so is not importable by name. The checks are injectable
(``profiles`` / ``resolve_fn``) so the failure cases never touch shipped
profiles.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.state.models import Wave
from eawf.platform.profiles.models import ProfileBody, VerifyBlock

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GATE_PATH = _REPO_ROOT / "tools" / "idle_contract_gate.py"
_TOOL_DIR = _GATE_PATH.parent


def _load_module():
    if str(_TOOL_DIR) not in sys.path:
        sys.path.insert(0, str(_TOOL_DIR))
    spec = importlib.util.spec_from_file_location("idle_contract_gate", _GATE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["idle_contract_gate"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod():
    return _load_module()


# --------------------------------------------------------------------------- #
# Synthetic-profile + resolver builders (no shipped-profile edits).
# --------------------------------------------------------------------------- #


def _profile(*, name: str, verify: VerifyBlock | None) -> ProfileBody:
    """Build a minimal :class:`ProfileBody` carrying *verify* and nothing else."""
    return ProfileBody(name=name, verify=verify)


def _band_verify() -> VerifyBlock:
    """A band-scoped enforce block: non-empty band + enforcement on."""
    return VerifyBlock(enforce=True, uiux_bands=["tui", "render"])


def _idle_verify() -> VerifyBlock:
    """An idle block: enforce on but NO band, so the producer is never wired on."""
    return VerifyBlock(enforce=True, uiux_bands=[])


def _global_resolver(verify: VerifyBlock | None, wave: Wave) -> VerifyBlock | None:
    """A resolver that enforces for EVERY wave -- the fleet-wide-flip defect."""
    del wave  # global flip ignores the per-wave band
    return verify


# --------------------------------------------------------------------------- #
# Pass path -- the real shipped tree.
# --------------------------------------------------------------------------- #


def test_gate_passes_on_current_tree(mod) -> None:
    # Default args load the shipped profiles + the real resolver: the producer
    # is wired on (quality profile) and resolves band-scoped.
    result = mod.check_idle_contract()
    assert result.passed is True
    assert result.failure is None


def test_gate_passes_with_band_profile_and_real_resolver(mod) -> None:
    # Boundary / happy path asserted explicitly: a producer-present +
    # band-present + band-scoped tree passes. Inject a one-profile list so the
    # assertion does not depend on which shipped profiles exist.
    profiles = [_profile(name="quality", verify=_band_verify())]
    result = mod.check_idle_contract(profiles=profiles)
    assert result.passed is True
    assert result.failure is None
    assert "quality" in result.message


# --------------------------------------------------------------------------- #
# Failure path -- idle producer (no band enables enforcement).
# --------------------------------------------------------------------------- #


def test_gate_fails_when_no_profile_enables_a_band(mod) -> None:
    # Every profile leaves the band empty, so the producer is idle-forever.
    profiles = [
        _profile(name="core", verify=None),
        _profile(name="quality", verify=_idle_verify()),
    ]
    result = mod.check_idle_contract(profiles=profiles)
    assert result.passed is False
    assert result.failure is mod.GateFailure.PRODUCER_IDLE
    assert "idle" in result.message


def test_gate_fails_when_profile_list_is_empty(mod) -> None:
    # No profiles at all is the degenerate idle case.
    result = mod.check_idle_contract(profiles=[])
    assert result.passed is False
    assert result.failure is mod.GateFailure.PRODUCER_IDLE


# --------------------------------------------------------------------------- #
# Failure path -- band enforces globally (resolver returns enforce for non-UI).
# --------------------------------------------------------------------------- #


def test_gate_fails_when_band_enforces_globally(mod) -> None:
    # The profile declares a band, but the (stubbed) resolver enforces for the
    # non-UI probe too -- a fleet-wide flip that would gate every wave.
    profiles = [_profile(name="quality", verify=_band_verify())]
    result = mod.check_idle_contract(profiles=profiles, resolve_fn=_global_resolver)
    assert result.passed is False
    assert result.failure is mod.GateFailure.BAND_ENFORCES_GLOBALLY
    assert "globally" in result.message
    assert "quality" in result.message


def test_gate_reports_idle_before_global(mod) -> None:
    # Precedence: a profile set that is BOTH idle (empty band) and paired with a
    # global resolver names the more fundamental idle failure first.
    profiles = [_profile(name="quality", verify=_idle_verify())]
    result = mod.check_idle_contract(profiles=profiles, resolve_fn=_global_resolver)
    assert result.failure is mod.GateFailure.PRODUCER_IDLE


# --------------------------------------------------------------------------- #
# Probe-wave construction -- UI vs non-UI band membership.
# --------------------------------------------------------------------------- #


def test_probe_waves_split_on_ui_scope(mod) -> None:
    # The gate's UI / non-UI probe scopes must actually straddle the band line
    # so the band-scoped assertion is meaningful: with the REAL resolver the UI
    # probe enforces and the non-UI probe does not.
    band = _band_verify()
    ui_wave = mod._make_probe_wave(scope=mod._UI_SCOPE)
    non_ui_wave = mod._make_probe_wave(scope=mod._NON_UI_SCOPE)

    from eawf.workflow.verify.readiness import resolve_wave_verify_block

    ui_resolved = resolve_wave_verify_block(band, ui_wave)
    non_ui_resolved = resolve_wave_verify_block(band, non_ui_wave)
    assert ui_resolved is not None and ui_resolved.enforce is True
    assert non_ui_resolved is not None and non_ui_resolved.enforce is False


def test_probe_wave_id_title_carry_no_band_token(mod) -> None:
    # Band membership must be decided by file_scopes alone -- the neutral id /
    # title must not accidentally contain a 'tui' / 'render' substring.
    wave = mod._make_probe_wave(scope=mod._NON_UI_SCOPE)
    corpus = f"{wave.id}\n{wave.title}".lower()
    assert "tui" not in corpus
    assert "render" not in corpus


# --------------------------------------------------------------------------- #
# CLI wrapper.
# --------------------------------------------------------------------------- #


def test_cli_returns_zero_on_pass(mod) -> None:
    code = mod.main(["idle_contract_gate.py"])
    assert code == 0


def test_cli_returns_one_on_failure(mod, monkeypatch) -> None:
    # Force the idle failure by making the shipped-profile loader return a set
    # with no band, then confirm the CLI maps the failed result onto exit 1.
    monkeypatch.setattr(
        mod,
        "_load_shipped_profiles",
        lambda: [
            ProfileBody(
                name="quality",
                verify=VerifyBlock(enforce=True, uiux_bands=[]),
            )
        ],
    )
    code = mod.main(["idle_contract_gate.py"])
    assert code == 1


def test_make_probe_wave_is_validated(mod) -> None:
    # The probe wave is a real validated Wave (not a loose dict) so the gate
    # exercises the same model the resolver consumes in production.
    wave = mod._make_probe_wave(scope=mod._UI_SCOPE)
    assert isinstance(wave, Wave)
    assert wave.file_scopes == [mod._UI_SCOPE]
    assert isinstance(wave.opened_at, datetime)
    assert wave.opened_at.tzinfo is UTC
