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
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli._mutation import (
    DAEMONLESS_WAIVER_EVENT_TYPE,
    close_event_extras,
    enforce_daemonless_close_waiver,
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


def test_daemonless_gate_bearing_without_waiver_rejected(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 1 (reject): a gate-bearing daemonless close WITHOUT the waiver
    flag is REJECTED with a typed UserError, and writes no waiver event.
    """
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    state_path = _state_path(tmp_path)
    wave = _make_wave(gates=[_gate()])
    with pytest.raises(cli_errors.UserError) as exc:
        enforce_daemonless_close_waiver(wave, state_path=state_path, waived=False)
    assert "gate-bearing" in str(exc.value)
    assert exc.value.kind == "InvalidInput"
    # No bypass event written on the reject path.
    assert _read_events(state_path) == []


def test_daemonless_gate_bearing_with_waiver_appends_event(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 1 (accept): a gate-bearing daemonless close WITH the waiver flag
    succeeds, returns the ``daemonless-waiver`` mechanism, and appends a waiver
    EVENT naming the wave + reason.
    """
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    state_path = _state_path(tmp_path)
    wave = _make_wave(gates=[_gate()])
    mechanism = enforce_daemonless_close_waiver(
        wave, state_path=state_path, waived=True, reason="recovery shell; daemon down"
    )
    assert mechanism == "daemonless-waiver"
    events = _read_events(state_path)
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["event_type"] == DAEMONLESS_WAIVER_EVENT_TYPE
    # The event NAMES the wave + reason so the override is auditable.
    assert payload["extras"]["wave"] == _WAVE_ID
    assert payload["extras"]["reason"] == "recovery shell; daemon down"
    assert payload["extras"]["close_mechanism"] == "daemonless-waiver"
    assert events[0]["scope_id"] == _WAVE_ID


def test_daemonless_non_gate_bearing_needs_no_waiver(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-gate-bearing daemonless close keeps the env hatch: no waiver needed,
    no bypass event, mechanism ``daemonless``.
    """
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    state_path = _state_path(tmp_path)
    wave = _make_wave(gates=[])
    mechanism = enforce_daemonless_close_waiver(wave, state_path=state_path, waived=False)
    assert mechanism == "daemonless"
    assert _read_events(state_path) == []


def test_non_daemonless_close_is_daemon_mechanism(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the env hatch the close is daemon-mediated: mechanism ``daemon``,
    no waiver needed even for a gate-bearing wave.
    """
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    state_path = _state_path(tmp_path)
    wave = _make_wave(gates=[_gate()])
    mechanism = enforce_daemonless_close_waiver(wave, state_path=state_path, waived=False)
    assert mechanism == "daemon"
    assert _read_events(state_path) == []


def test_waiver_event_reason_defaults_to_unspecified(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A waived close with no reason records ``unspecified`` (boundary case)."""
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    state_path = _state_path(tmp_path)
    wave = _make_wave(gates=[_gate()])
    enforce_daemonless_close_waiver(wave, state_path=state_path, waived=True, reason=None)
    events = _read_events(state_path)
    assert events[0]["payload"]["extras"]["reason"] == "unspecified"


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
