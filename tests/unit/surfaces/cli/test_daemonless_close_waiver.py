"""Tests: daemonless gate-bearing close-with-waiver door + close-mechanism stamp.

Covers the P30-I16-W18 bypass-door bundle in
:mod:`eawf.surfaces.cli._mutation`:

* :func:`enforce_daemonless_close_waiver` REJECTS a GATE-BEARING wave close
  under ``EAWF_DAEMONLESS`` when no waiver flag is passed, ALLOWS it (and
  appends a waiver event naming the wave + reason) when the flag is passed, and
  leaves a NON-gate-bearing daemonless close untouched (the env hatch still
  works with no waiver).
* :func:`resolve_close_mechanism` / :func:`close_event_extras` stamp the
  ``close_mechanism`` field on every wave-close event (``daemon`` when daemon-
  mediated, ``daemonless`` for a non-gate-bearing fallback,
  ``daemonless-waiver`` for the gated bypass).
"""

from __future__ import annotations

import orjson
import pytest

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.models import Wave
from eawf.kernel.store.paths import store_path
from eawf.surfaces.cli._mutation import (
    close_event_extras,
    resolve_close_mechanism,
    wave_is_gate_bearing,
)
from tests._criteria_helpers import legacy_criteria

_WAVE_ID = "P30-I16-W18"


def _gate(gate_id: str = "G1", kind: str = "jury_verdict") -> dict[str, object]:
    """A minimal valid GateSpec payload of *kind* for a gate-bearing wave."""
    return {
        "id": gate_id,
        "criterion_id": "CR-01",
        "kind": kind,
        "args": {},
        "policy": "block",
        "cadence": "every-wave",
    }


def _make_wave(*, gates: list[dict[str, object]] | None = None) -> Wave:
    """Build a claimed :class:`Wave`, optionally carrying typed gates."""
    return Wave.model_validate(
        {
            "id": _WAVE_ID,
            "iter_id": "P30-I16",
            "title": "bypass-door bundle",
            "status": "claimed",
            "deps": [],
            "blocks": [],
            "file_scopes": ["src/eawf/surfaces/cli/_mutation.py"],
            "success_criteria": [
                c.model_dump(mode="json")
                for c in legacy_criteria("daemonless close needs a waiver")
            ],
            "gates": gates or [],
            "agent_role": "executor",
            "effort_bucket": "M",
            "opened_at": "2026-06-11T00:00:00Z",
            "claimed_at": "2026-06-11T00:00:00Z",
        }
    )


def _state_path(tmp_path: object) -> object:
    """Create an empty ``.ea/`` dir and return the ``state.json`` path."""
    ea = tmp_path / ".ea"  # type: ignore[operator]
    ea.mkdir()
    state_path = ea / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    return state_path


def _read_events(state_path: object) -> list[dict[str, object]]:
    """Decode every event-store envelope under *state_path*'s store dir."""
    event_path = store_path(state_path, StoreKind.EVENT)  # type: ignore[arg-type]
    if not event_path.exists():
        return []
    rows: list[dict[str, object]] = []
    for line in event_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(orjson.loads(line))
    return rows


# --- wave_is_gate_bearing: the gate-bearing predicate ----------------------


def test_wave_is_gate_bearing_true_with_gates() -> None:
    """A wave attaching at least one typed gate is gate-bearing."""
    assert wave_is_gate_bearing(_make_wave(gates=[_gate()])) is True


def test_wave_is_gate_bearing_false_without_gates() -> None:
    """A wave with an empty gate list is NOT gate-bearing."""
    assert wave_is_gate_bearing(_make_wave(gates=[])) is False


# --- enforce_daemonless_close_waiver: the bypass door ----------------------


# --- close_mechanism stamp on every close event ----------------------------


def test_resolve_close_mechanism_daemon_when_not_daemonless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-daemonless close resolves to the ``daemon`` mechanism."""
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    assert resolve_close_mechanism(gate_bearing=True, waived=True) == "daemon"
    assert resolve_close_mechanism(gate_bearing=False, waived=False) == "daemon"


def test_resolve_close_mechanism_daemonless_variants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under the env hatch the mechanism splits gate-bearing-waived from the rest."""
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    assert resolve_close_mechanism(gate_bearing=True, waived=True) == "daemonless-waiver"
    assert resolve_close_mechanism(gate_bearing=True, waived=False) == "daemonless"
    assert resolve_close_mechanism(gate_bearing=False, waived=False) == "daemonless"


def test_close_event_extras_stamps_mechanism_preserving_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criterion 1 (stamp): every wave-close event carries ``close_mechanism``,
    folded into the existing advisory extras without dropping them.
    """
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    extras = close_event_extras({"readiness_warnings_count": 2}, gate_bearing=True, waived=False)
    assert extras["close_mechanism"] == "daemon"
    # Base extras survive.
    assert extras["readiness_warnings_count"] == 2


def test_close_event_extras_none_base_yields_mechanism_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``None`` base extras dict yields a fresh dict carrying only the field."""
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    extras = close_event_extras(None, gate_bearing=True, waived=True)
    assert extras == {"close_mechanism": "daemonless-waiver"}
