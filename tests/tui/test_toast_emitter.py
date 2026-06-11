"""Unit + Pilot tests for the C06 ``ToastEmitter`` (P27-I04-W08).

Covers the pure state-snapshot diff engine (:meth:`ToastEmitter.diff`) —
wave close / audit verdict / needs-user toasts, the first-load and
daemon-reconnect flood guards, and the ``ui.toasts`` off/important/all
verbosity gate — plus the focus-preserving ``app.notify`` driver
(:meth:`ToastEmitter.emit`) end-to-end through the real :class:`EaApp`.
"""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf.kernel.config.registry import LEAF_KEY_REGISTRY, registry_lookup
from eawf.kernel.state.models import State
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.toast_emitter import (
    DEFAULT_VERBOSITY,
    FLOOD_THRESHOLD,
    ToastEmitter,
    ToastNotification,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"


def _payload() -> dict[str, Any]:
    """Return a fresh mutable copy of the active phase/iter/wave fixture."""
    return copy.deepcopy(orjson.loads(_PHASE_ITER_WAVE.read_bytes()))


def _state(payload: dict[str, Any]) -> State:
    return State.model_validate(payload)


def _base_state() -> State:
    """Return the fixture as-is (wave W01 IN_PROGRESS, no audits)."""
    return _state(_payload())


def _wave_closed_state() -> State:
    """Return the fixture with W01 flipped to CLOSED."""
    payload = _payload()
    payload["waves"]["P01-I01-W01"]["status"] = "closed"
    payload["waves"]["P01-I01-W01"]["closed_at"] = payload["phases"]["P01"]["opened_at"]
    return _state(payload)


def _with_extra_waves(payload: dict[str, Any], count: int, *, status: str) -> dict[str, Any]:
    """Add *count* extra waves with the given *status* to the fixture iter."""
    opened = payload["phases"]["P01"]["opened_at"]
    for n in range(2, 2 + count):
        wave_id = f"P01-I01-W{n:02d}"
        payload["iters"]["P01-I01"]["wave_ids"].append(wave_id)
        payload["waves"][wave_id] = {
            "id": wave_id,
            "iter_id": "P01-I01",
            "title": f"wave {n}",
            "status": status,
            "deps": [],
            "blocks": [],
            "file_scopes": [],
            "success_criteria": [],
            "opened_at": opened,
            "closed_at": None,
        }
    return payload


def _audit_payload(payload: dict[str, Any], *, verdict: str | None) -> dict[str, Any]:
    """Attach a single audit row carrying *verdict* to the fixture."""
    payload["audits"] = {
        "A01": {
            "id": "A01",
            "scope_id": "QR",
            "kind": "evaluation",
            "status": "complete" if verdict is not None else "running",
            "created_at": payload["phases"]["P01"]["opened_at"],
            "verdict": verdict,
        }
    }
    return payload


def _fleet_payload(
    payload: dict[str, Any], *, failed: int = 0, closed: int = 0
) -> dict[str, Any]:
    """Attach a fleet run whose counters carry *failed* / *closed* tallies.

    Bumps the on-disk schema version so the additive ``fleet_run`` field
    validates; the run is otherwise minimal -- only the counters the failure
    diff reads are seeded.
    """
    payload["schema_version"] = "1.10"
    payload["fleet_run"] = {
        "run_state": "draining",
        "armed_at": payload["phases"]["P01"]["opened_at"],
        "counters": {"failed": failed, "closed": closed},
    }
    return payload


class _NotifySink:
    """Minimal stand-in capturing ``app.notify`` calls for the emit path."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self._fail = fail

    def notify(self, message: str, *, severity: str = "information", **_: object) -> None:
        if self._fail:
            raise RuntimeError("render boom")
        self.calls.append((message, severity))


# --- wave close --------------------------------------------------------------


def test_wave_closed_emits_toast() -> None:
    emitter = ToastEmitter("important")
    notes = emitter.diff(_base_state(), _wave_closed_state())
    assert len(notes) == 1
    assert notes[0].category == "wave_close"
    assert "P01-I01-W01" in notes[0].message
    assert "closed" in notes[0].message
    assert notes[0].severity == "information"


def test_diff_no_change_emits_nothing() -> None:
    emitter = ToastEmitter("important")
    assert emitter.diff(_base_state(), _base_state()) == []


def test_diff_wave_already_closed_in_prev_is_noop() -> None:
    """A wave CLOSED in both snapshots is not re-announced."""
    emitter = ToastEmitter("important")
    closed = _wave_closed_state()
    assert emitter.diff(closed, closed) == []


def test_diff_wave_first_seen_already_closed_is_noop() -> None:
    """A wave absent from prev but CLOSED now has no transition to announce."""
    emitter = ToastEmitter("important")
    prev = _base_state()
    payload = _payload()
    payload = _with_extra_waves(payload, 1, status="closed")
    current = _state(payload)
    # W02 is brand new and already closed -> no transition. W01 unchanged.
    assert emitter.diff(prev, current) == []


# --- verbosity gating --------------------------------------------------------


def test_quiet_mode_suppresses() -> None:
    emitter = ToastEmitter("off")
    assert emitter.diff(_base_state(), _wave_closed_state()) == []


def test_important_mode_emits_wave_close() -> None:
    emitter = ToastEmitter("important")
    assert emitter.diff(_base_state(), _wave_closed_state())


def test_all_mode_emits_wave_close() -> None:
    """``all`` is a superset of ``important`` — the important set still fires."""
    emitter = ToastEmitter("all")
    notes = emitter.diff(_base_state(), _wave_closed_state())
    assert [n.category for n in notes] == ["wave_close"]


def test_default_verbosity_is_important() -> None:
    assert ToastEmitter().verbosity == DEFAULT_VERBOSITY == "important"


def test_unknown_verbosity_rejected() -> None:
    with pytest.raises(ValueError, match="unknown toast verbosity"):
        ToastEmitter("loud")  # type: ignore[arg-type]


def test_flood_threshold_below_one_rejected() -> None:
    with pytest.raises(ValueError, match="flood_threshold must be >= 1"):
        ToastEmitter("important", flood_threshold=0)


# --- first-load guard --------------------------------------------------------


def test_first_load_emits_nothing() -> None:
    """No previous snapshot to diff -> no toasts on initial mount."""
    emitter = ToastEmitter("important")
    assert emitter.diff(None, _wave_closed_state()) == []


def test_first_load_emits_nothing_in_all_mode() -> None:
    emitter = ToastEmitter("all")
    assert emitter.diff(None, _base_state()) == []


# --- daemon-reconnect flood guard --------------------------------------------


def test_flood_guard_collapses_to_summary() -> None:
    """N > threshold transitions in one revision -> a single summary toast."""
    emitter = ToastEmitter("important")  # threshold defaults to FLOOD_THRESHOLD
    prev_payload = _with_extra_waves(_payload(), FLOOD_THRESHOLD + 1, status="in_progress")
    prev = _state(prev_payload)
    current_payload = copy.deepcopy(prev_payload)
    # Close W01 plus every extra wave -> (FLOOD_THRESHOLD + 2) closes > threshold.
    for wave_id, wave in current_payload["waves"].items():  # noqa: B007
        wave["status"] = "closed"
        wave["closed_at"] = current_payload["phases"]["P01"]["opened_at"]
    current = _state(current_payload)

    notes = emitter.diff(prev, current)
    assert len(notes) == 1
    assert notes[0].category == "summary"
    assert "changes" in notes[0].message


def test_no_flood_when_at_threshold() -> None:
    """Exactly ``threshold`` transitions still emit per-change toasts."""
    emitter = ToastEmitter("important", flood_threshold=3)
    prev_payload = _with_extra_waves(_payload(), 2, status="in_progress")  # W01, W02, W03
    prev = _state(prev_payload)
    current_payload = copy.deepcopy(prev_payload)
    for wave in current_payload["waves"].values():
        wave["status"] = "closed"
        wave["closed_at"] = current_payload["phases"]["P01"]["opened_at"]
    current = _state(current_payload)

    notes = emitter.diff(prev, current)
    assert len(notes) == 3
    assert {n.category for n in notes} == {"wave_close"}


# --- audit verdict -----------------------------------------------------------


def test_audit_verdict_emits_toast() -> None:
    emitter = ToastEmitter("important")
    prev = _state(_audit_payload(_payload(), verdict=None))
    current = _state(_audit_payload(_payload(), verdict="pass"))
    notes = emitter.diff(prev, current)
    assert len(notes) == 1
    assert notes[0].category == "audit_verdict"
    assert "A01" in notes[0].message
    assert "pass" in notes[0].message
    assert notes[0].severity == "information"


def test_audit_major_verdict_is_warning() -> None:
    emitter = ToastEmitter("important")
    prev = _state(_audit_payload(_payload(), verdict=None))
    current = _state(_audit_payload(_payload(), verdict="major"))
    notes = emitter.diff(prev, current)
    assert notes[0].severity == "warning"


def test_audit_verdict_unchanged_is_noop() -> None:
    emitter = ToastEmitter("important")
    prev = _state(_audit_payload(_payload(), verdict="pass"))
    current = _state(_audit_payload(_payload(), verdict="pass"))
    assert emitter.diff(prev, current) == []


# --- needs-user --------------------------------------------------------------


def test_needs_user_raise_emits_toast() -> None:
    emitter = ToastEmitter("important")
    notes = emitter.diff(
        _base_state(),
        _base_state(),
        prev_open_pause_count=0,
        open_pause_count=1,
    )
    assert len(notes) == 1
    assert notes[0].category == "needs_user"
    assert "1 question" in notes[0].message
    assert notes[0].severity == "warning"


def test_needs_user_multiple_raise_pluralizes() -> None:
    emitter = ToastEmitter("important")
    notes = emitter.diff(
        _base_state(),
        _base_state(),
        prev_open_pause_count=1,
        open_pause_count=3,
    )
    assert "2 questions" in notes[0].message


def test_needs_user_resolved_does_not_emit() -> None:
    """A falling pause count (operator answered) raises no toast."""
    emitter = ToastEmitter("important")
    notes = emitter.diff(
        _base_state(),
        _base_state(),
        prev_open_pause_count=2,
        open_pause_count=1,
    )
    assert notes == []


def test_negative_pause_count_rejected() -> None:
    emitter = ToastEmitter("important")
    with pytest.raises(ValueError, match="pause counts must be >= 0"):
        emitter.diff(_base_state(), _base_state(), open_pause_count=-1)


# --- failure (fleet lane failed) ---------------------------------------------


def test_lane_failure_emits_failure_class_toast() -> None:
    """A rising fleet ``failed`` tally fires the distinct failure-class toast."""
    emitter = ToastEmitter("important")
    prev = _state(_fleet_payload(_payload(), failed=0))
    current = _state(_fleet_payload(_payload(), failed=1))
    notes = emitter.diff(prev, current)
    assert len(notes) == 1
    assert notes[0].category == "failure"
    assert "1 lane failed" in notes[0].message
    # The failure class reads error red, distinct from the warning band.
    assert notes[0].severity == "error"


def test_lane_failure_multiple_pluralizes() -> None:
    """Two lanes failing in one revision pluralizes the failure toast body."""
    emitter = ToastEmitter("important")
    prev = _state(_fleet_payload(_payload(), failed=1))
    current = _state(_fleet_payload(_payload(), failed=3))
    notes = emitter.diff(prev, current)
    assert "2 lanes failed" in notes[0].message


def test_normal_close_does_not_emit_failure_toast() -> None:
    """C2 negative path: a clean close bumps ``closed``, never the failure toast.

    The load-bearing C2 assertion -- a normal close advances ``counters.closed``
    while ``failed`` stays put, so the failure-class toast NEVER fires for a
    clean close. Only the close-class toast (off the wave transition) would.
    """
    emitter = ToastEmitter("important")
    prev = _state(_fleet_payload(_payload(), failed=2, closed=3))
    current = _state(_fleet_payload(_payload(), failed=2, closed=4))
    notes = emitter.diff(prev, current)
    assert [n.category for n in notes] == []  # no failure toast on a clean close


def test_lane_failure_falling_tally_does_not_emit() -> None:
    """A fresh run armed over a finished one (falling ``failed``) raises nothing."""
    emitter = ToastEmitter("important")
    prev = _state(_fleet_payload(_payload(), failed=3))
    current = _state(_fleet_payload(_payload(), failed=0))
    assert emitter.diff(prev, current) == []


def test_lane_failure_no_run_in_prev_is_noop() -> None:
    """A run that first appears (no prev run) announces no retroactive failure."""
    emitter = ToastEmitter("important")
    prev = _base_state()  # no fleet_run
    current = _state(_fleet_payload(_payload(), failed=2))
    assert emitter.diff(prev, current) == []


def test_lane_failure_suppressed_when_off() -> None:
    """The ``off`` verbosity silences the failure class like every other."""
    emitter = ToastEmitter("off")
    prev = _state(_fleet_payload(_payload(), failed=0))
    current = _state(_fleet_payload(_payload(), failed=1))
    assert emitter.diff(prev, current) == []


# --- mixed change set --------------------------------------------------------


def test_wave_close_and_audit_verdict_both_emit() -> None:
    emitter = ToastEmitter("important")
    prev = _state(_audit_payload(_payload(), verdict=None))
    current = _state(_audit_payload(_wave_closed_state().model_dump(mode="json"), verdict="pass"))
    notes = emitter.diff(prev, current)
    cats = {n.category for n in notes}
    assert cats == {"wave_close", "audit_verdict"}


# --- emit driver (drop-don't-crash) ------------------------------------------


def test_emit_calls_notify_per_notification() -> None:
    emitter = ToastEmitter("important")
    sink = _NotifySink()
    notes = emitter.emit(sink, _base_state(), _wave_closed_state())  # type: ignore[arg-type]
    assert len(notes) == 1
    assert len(sink.calls) == 1
    assert "P01-I01-W01" in sink.calls[0][0]


def test_emit_drops_toast_on_render_failure() -> None:
    """A notify that raises is swallowed — the app never crashes."""
    emitter = ToastEmitter("important")
    sink = _NotifySink(fail=True)
    # Returns the computed notifications even though notify blew up.
    notes = emitter.emit(sink, _base_state(), _wave_closed_state())  # type: ignore[arg-type]
    assert len(notes) == 1
    assert sink.calls == []


def test_emit_first_load_does_not_notify() -> None:
    emitter = ToastEmitter("important")
    sink = _NotifySink()
    notes = emitter.emit(sink, None, _wave_closed_state())  # type: ignore[arg-type]
    assert notes == []
    assert sink.calls == []


# --- focus-preserving Pilot path ---------------------------------------------


def test_emit_through_real_app_preserves_focus() -> None:
    """emit() drives the real EaApp.notify without moving focus (ambient)."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            focus_before = app.focused
            emitter = ToastEmitter("important")
            notes = emitter.emit(app, _base_state(), _wave_closed_state())
            await pilot.pause()
            assert len(notes) == 1
            # Ambient region: the toast does not steal focus.
            assert app.focused is focus_before
            # Textual stacked the notification in its toast rack.
            assert len(app._notifications) == 1

    asyncio.run(body())


# --- ToastNotification model -------------------------------------------------


def test_toast_notification_forbids_extra() -> None:
    with pytest.raises(ValueError):
        ToastNotification(message="x", category="wave_close", bogus=1)  # type: ignore[call-arg]


def test_toast_notification_is_frozen() -> None:
    note = ToastNotification(message="x", category="wave_close")
    with pytest.raises(ValueError):
        note.message = "y"  # type: ignore[misc]


# --- config registry row -----------------------------------------------------


def test_ui_toasts_registry_row_present() -> None:
    entry = registry_lookup("ui.toasts")
    assert entry is not None
    assert entry.tab == "ui"
    assert entry.type == "choice"
    assert entry.default == "important"
    assert entry.choices == ("off", "important", "all")


def test_ui_toasts_leaf_key_present() -> None:
    leaf = LEAF_KEY_REGISTRY.get("ui.toasts")
    assert leaf is not None
    assert leaf.domain == "ui"
    assert leaf.default == "important"
    assert leaf.choices == ("off", "important", "all")
