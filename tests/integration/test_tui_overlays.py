"""Integration tests for the detail-overlay TUI surface (P20-I01-W04).

Covers byte-stable golden snapshots for every overlay kind plus
structural assertions that hold independent of the snapshot bytes.

When the renderer drifts intentionally, regenerate the snapshots:

    cd <repo>
    uv run python -c "
    import io, json
    from datetime import UTC, datetime
    from pathlib import Path
    from rich.console import Console
    from eawf.state.models import State
    from eawf.store.kinds.event import EventPayload
    from eawf.tui.overlays import open_overlay

    fixture = Path('tests/golden/tui')
    state = State.model_validate(
        json.loads((fixture / 'overlay_state.json').read_text())
    )
    events = [
        EventPayload(
            timestamp=datetime(2026, 5, 15, 10, 0, 0, tzinfo=UTC),
            event_type='wave_claim',
            actor='executor',
            command='wave claim P20-I01-W04',
            args_hash='deadbeefdeadbeef',
            before_state_version='v0',
            after_state_version='v1',
            status='ok',
            message='claimed P20-I01-W04',
        ),
        EventPayload(
            timestamp=datetime(2026, 5, 15, 10, 5, 0, tzinfo=UTC),
            event_type='wave_close',
            actor='executor',
            command='wave close P20-I01-W04',
            args_hash='cafef00dcafef00d',
            before_state_version='v1',
            after_state_version='v2',
            status='ok',
            message='closed P20-I01-W04',
        ),
    ]
    plans = [
        ('hypothesis', 'H01-01', None, 'overlay_hypothesis_default.txt'),
        ('decision', 'D15', None, 'overlay_decision_default.txt'),
        ('memory', 'M01', None, 'overlay_memory_default.txt'),
        ('events', 'recent', events, 'overlay_events_default.txt'),
        ('dispatch', 'P20-I01-W04', None, 'overlay_dispatch_default.txt'),
    ]
    for kind, target, evlist, name in plans:
        buf = io.StringIO()
        Console(file=buf, force_terminal=False, width=100, height=30, record=False).print(
            open_overlay(kind, state, target, events=evlist)
        )
        (fixture / name).write_text(buf.getvalue())
    "
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from rich.console import Console

from eawf.state.models import State
from eawf.store.kinds.event import EventPayload
from eawf.tui.overlays import open_overlay

_FIXTURE_DIR: Path = Path(__file__).parent.parent / "golden" / "tui"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_fixture_state() -> State:
    payload = json.loads((_FIXTURE_DIR / "overlay_state.json").read_text(encoding="utf-8"))
    return State.model_validate(payload)


def _events_fixture() -> list[EventPayload]:
    return [
        EventPayload(
            timestamp=datetime(2026, 5, 15, 10, 0, 0, tzinfo=UTC),
            event_type="wave_claim",
            actor="executor",
            command="wave claim P20-I01-W04",
            args_hash="deadbeefdeadbeef",
            before_state_version="v0",
            after_state_version="v1",
            status="ok",
            message="claimed P20-I01-W04",
        ),
        EventPayload(
            timestamp=datetime(2026, 5, 15, 10, 5, 0, tzinfo=UTC),
            event_type="wave_close",
            actor="executor",
            command="wave close P20-I01-W04",
            args_hash="cafef00dcafef00d",
            before_state_version="v1",
            after_state_version="v2",
            status="ok",
            message="closed P20-I01-W04",
        ),
    ]


def _render(renderable: object) -> str:
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=100, height=30, record=False)
    console.print(renderable)
    return buf.getvalue()


def _normalise_trailing_newline(text: str) -> str:
    if text.endswith("\n"):
        return text[:-1]
    return text


# ---------------------------------------------------------------------------
# Golden snapshots — one per overlay kind (success criterion 3)
# ---------------------------------------------------------------------------


@pytest.mark.golden
def test_overlay_hypothesis_matches_golden() -> None:
    state = _load_fixture_state()
    actual = _normalise_trailing_newline(_render(open_overlay("hypothesis", state, "H01-01")))
    expected = _normalise_trailing_newline(
        (_FIXTURE_DIR / "overlay_hypothesis_default.txt").read_text(encoding="utf-8")
    )
    assert actual == expected, (
        "hypothesis-overlay drift — regenerate "
        "tests/golden/tui/overlay_hypothesis_default.txt with the snippet at "
        "the top of test_tui_overlays.py."
    )


@pytest.mark.golden
def test_overlay_decision_matches_golden() -> None:
    state = _load_fixture_state()
    actual = _normalise_trailing_newline(_render(open_overlay("decision", state, "D15")))
    expected = _normalise_trailing_newline(
        (_FIXTURE_DIR / "overlay_decision_default.txt").read_text(encoding="utf-8")
    )
    assert actual == expected


@pytest.mark.golden
def test_overlay_memory_matches_golden() -> None:
    state = _load_fixture_state()
    actual = _normalise_trailing_newline(_render(open_overlay("memory", state, "M01")))
    expected = _normalise_trailing_newline(
        (_FIXTURE_DIR / "overlay_memory_default.txt").read_text(encoding="utf-8")
    )
    assert actual == expected


@pytest.mark.golden
def test_overlay_events_matches_golden() -> None:
    state = _load_fixture_state()
    actual = _normalise_trailing_newline(
        _render(open_overlay("events", state, "recent", events=_events_fixture()))
    )
    expected = _normalise_trailing_newline(
        (_FIXTURE_DIR / "overlay_events_default.txt").read_text(encoding="utf-8")
    )
    assert actual == expected


@pytest.mark.golden
def test_overlay_dispatch_matches_golden() -> None:
    state = _load_fixture_state()
    actual = _normalise_trailing_newline(_render(open_overlay("dispatch", state, "P20-I01-W04")))
    expected = _normalise_trailing_newline(
        (_FIXTURE_DIR / "overlay_dispatch_default.txt").read_text(encoding="utf-8")
    )
    assert actual == expected


# ---------------------------------------------------------------------------
# Structural assertions (independent of byte-equality)
# ---------------------------------------------------------------------------


def test_overlay_brand_outside_left_of_breadcrumb_across_all_kinds() -> None:
    """Eä brand must sit outside-left of the breadcrumb in every overlay."""
    state = _load_fixture_state()
    events = _events_fixture()
    plans: list[tuple[str, str, list[EventPayload] | None]] = [
        ("hypothesis", "H01-01", None),
        ("decision", "D15", None),
        ("memory", "M01", None),
        ("events", "recent", events),
        ("dispatch", "P20-I01-W04", None),
    ]
    for kind, target, ev in plans:
        rendered = _render(open_overlay(kind, state, target, events=ev))  # type: ignore[arg-type]
        brand_idx = rendered.find("Eä")
        crumb_idx = rendered.find("EAWF")
        assert brand_idx >= 0, f"missing brand in {kind} overlay"
        assert crumb_idx > brand_idx, f"breadcrumb not after brand in {kind} overlay"


def test_overlay_dispatch_uses_typed_wave_graph_edges() -> None:
    """Dispatch overlay reads DAG edges from the typed accessor."""
    state = _load_fixture_state()
    rendered = _render(open_overlay("dispatch", state, "P20-I01-W04"))
    # In the fixture, W04 depends on W03 which is in_progress → blocked_by W03.
    lines = rendered.splitlines()
    blocked_lines = [line for line in lines if "blocked_by:" in line]
    assert len(blocked_lines) == 1
    assert "P20-I01-W03" in blocked_lines[0]


def test_overlay_two_renders_byte_stable() -> None:
    """Calling :func:`open_overlay` twice must produce identical bytes."""
    state = _load_fixture_state()
    first = _render(open_overlay("hypothesis", state, "H01-01"))
    second = _render(open_overlay("hypothesis", state, "H01-01"))
    assert first == second


def test_overlay_decision_renders_alternatives_when_present() -> None:
    """Fixture D15 lists alternatives; overlay must surface them."""
    state = _load_fixture_state()
    rendered = _render(open_overlay("decision", state, "D15"))
    assert "Textual" in rendered or "blessed" in rendered, (
        "alternatives missing from decision overlay output"
    )


def test_overlay_memory_renders_tier_value() -> None:
    state = _load_fixture_state()
    rendered = _render(open_overlay("memory", state, "M01"))
    assert "working" in rendered
