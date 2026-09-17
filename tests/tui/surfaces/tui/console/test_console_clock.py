"""Every timed console behaviour reads the one console clock.

The go prefix holds at 1.49 s and drops at 1.51 s, each arming owning its own deadline;
the guarded quit rejects a second Escape at 79 ms, quits at 81 ms and 1.49 s, and re-arms at
1.51 s; a toast stands until its own dwell passes. A held clock holds every deadline. No
console module other than the clock reads a time source or hands a timer to a scheduler.
"""

from __future__ import annotations

import ast
import time
from pathlib import Path

import pytest

from eawf.surfaces.tui.console import clock as console_clock
from eawf.surfaces.tui.console.clock import (
    PREFIX_TIMEOUT,
    QUIT_CEILING,
    RACK_MAX,
    TICK_KEY,
    TOAST_DWELL,
    Clock,
    FakeClock,
    QuitStep,
    arm_prefix,
    expire_prefix,
    notify,
    quit_step,
    sweep_toasts,
)
from eawf.surfaces.tui.console.session import Session
from eawf.surfaces.tui.console.tokens import Severity

CONSOLE_DIR = Path(console_clock.__file__).resolve().parent
CLOCK_MODULE = "clock.py"

# Calls that read a time source or schedule a callback outside the console clock.
_TIME_CALLS = frozenset(
    {
        "time.time",
        "time.monotonic",
        "time.perf_counter",
        "time.time_ns",
        "time.monotonic_ns",
        "time.localtime",
        "time.gmtime",
        "datetime.now",
        "datetime.utcnow",
        "datetime.today",
        "date.today",
        "asyncio.sleep",
    }
)
_SCHEDULERS = frozenset({"set_timer", "set_interval", "call_later", "call_at", "call_soon"})
_TIME_MODULES = frozenset({"time", "sched", "threading"})


def _time_reads(tree: ast.AST) -> list[int]:
    """Return the lines that import a time module, read a clock or schedule a callback."""
    hits: list[int] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Import)
            and any(alias.name.split(".")[0] in _TIME_MODULES for alias in node.names)
        ) or (isinstance(node, ast.ImportFrom) and (node.module or "") in _TIME_MODULES):
            hits.append(node.lineno)
        elif isinstance(node, ast.Call):
            name = ast.unparse(node.func)
            tail = name.rsplit(".", 1)[-1]
            if tail in _SCHEDULERS or any(name.endswith(call) for call in _TIME_CALLS):
                hits.append(node.lineno)
    return hits


def _session() -> Session:
    return Session(route="scope.home")


def test_clock_now_reads_the_monotonic_clock() -> None:
    before = time.monotonic()
    reading = Clock().now()
    assert before <= reading <= time.monotonic()


def test_fake_clock_holds_until_advanced() -> None:
    held = FakeClock()
    assert held.now() == held.now() == pytest.approx(1000.0)
    held.advance(0.25)
    assert held.now() == pytest.approx(1000.25)
    held.advance(0)
    assert held.now() == pytest.approx(1000.25)


def test_fake_clock_advance_backwards_raises_value_error() -> None:
    with pytest.raises(ValueError, match="cannot move back"):
        FakeClock(start=5.0).advance(-0.001)


def test_arm_prefix_numbers_each_arming() -> None:
    session, held = _session(), FakeClock()
    assert arm_prefix(session, held) == 1
    assert arm_prefix(session, held) == 2
    assert session.prefix == "g"
    assert session.prefix_deadline == pytest.approx(held.now() + PREFIX_TIMEOUT)


def test_expire_prefix_holds_at_1_49s_and_drops_at_1_51s() -> None:
    session, held = _session(), FakeClock()
    arm_prefix(session, held)
    held.advance(1.49)
    assert expire_prefix(session, held) is False
    assert session.prefix == "g"
    held.advance(0.02)
    assert expire_prefix(session, held) is True
    assert session.prefix is None
    assert session.prefix_deadline is None


def test_expire_prefix_counts_and_logs_the_cancellation() -> None:
    session, held = _session(), FakeClock()
    arm_prefix(session, held)
    held.advance(PREFIX_TIMEOUT)
    assert expire_prefix(session, held) is True
    assert session.prefix_cancels == 1
    assert (session.log[0].key, session.log[0].note) == (TICK_KEY, "prefix timed out after 1.5s")
    assert expire_prefix(session, held) is False
    assert session.prefix_cancels == 1


def test_expire_prefix_later_arming_survives_the_earlier_deadline() -> None:
    session, held = _session(), FakeClock()
    arm_prefix(session, held)
    held.advance(1.0)
    arm_prefix(session, held)
    held.advance(0.6)
    assert expire_prefix(session, held) is False
    assert session.prefix == "g"
    held.advance(1.0)
    assert expire_prefix(session, held) is True


def test_expire_prefix_unarmed_session_is_untouched() -> None:
    session, held = _session(), FakeClock()
    held.advance(10)
    assert expire_prefix(session, held) is False
    assert session.prefix_cancels == 0
    session.prefix = "g"
    assert expire_prefix(session, held) is False


def test_expire_prefix_held_clock_never_expires() -> None:
    session, held = _session(), FakeClock()
    arm_prefix(session, held)
    assert not any(expire_prefix(session, held) for _ in range(20))
    assert session.prefix == "g"


@pytest.mark.parametrize(
    ("gap", "step"),
    [
        (0.0, QuitStep.BURST),
        (0.079, QuitStep.BURST),
        (0.081, QuitStep.QUIT),
        (1.49, QuitStep.QUIT),
        (QUIT_CEILING, QuitStep.ARMED),
        (1.51, QuitStep.ARMED),
    ],
)
def test_quit_step_judges_the_second_escape_by_its_gap(gap: float, step: QuitStep) -> None:
    session, held = _session(), FakeClock()
    assert quit_step(session, held).step == QuitStep.ARMED
    armed_at = session.last_esc
    held.advance(gap)
    check = quit_step(session, held)
    assert check.step == step
    assert check.gap_ms == int((held.now() - armed_at) * 1000)
    assert abs(check.gap_ms - gap * 1000) <= 1
    if step == QuitStep.QUIT:
        assert session.last_esc == 0.0
    elif step == QuitStep.BURST:
        assert session.last_esc == armed_at
    else:
        assert session.last_esc == pytest.approx(held.now())


def test_quit_step_disarmed_guard_arms_with_no_gap() -> None:
    session, held = _session(), FakeClock()
    check = quit_step(session, held)
    assert (check.step, check.gap_ms) == (QuitStep.ARMED, 0)
    assert session.last_esc == pytest.approx(held.now())


def test_quit_step_after_disarm_arms_again() -> None:
    session, held = _session(), FakeClock()
    quit_step(session, held)
    session.disarm_quit()
    held.advance(0.5)
    assert quit_step(session, held).step == QuitStep.ARMED


def test_notify_stamps_the_toast_with_the_console_clock() -> None:
    session, held = _session(), FakeClock(start=42.0)
    notify(session, held, text="copied", title="done", sev=Severity.OK)
    assert [(t.title, t.text, t.sev, t.at) for t in session.toasts] == [
        ("done", "copied", Severity.OK, pytest.approx(42.0))
    ]


def test_notify_past_the_cap_drops_the_oldest() -> None:
    session, held = _session(), FakeClock()
    for n in range(RACK_MAX + 2):
        notify(session, held, text=f"t{n}", title="done", sev=Severity.INFO)
    assert [t.text for t in session.toasts] == [f"t{n}" for n in range(2, RACK_MAX + 2)]


def test_sweep_toasts_expires_at_the_dwell_only() -> None:
    session, held = _session(), FakeClock()
    notify(session, held, text="copied", title="done", sev=Severity.INFO)
    held.advance(4.99)
    assert sweep_toasts(session, held) == []
    assert len(session.toasts) == 1
    assert session.rack_last == pytest.approx(held.now())
    held.advance(0.02)
    gone = sweep_toasts(session, held)
    assert [t.text for t in gone] == ["copied"]
    assert session.toasts == []
    assert session.log[0].note == "toast expired · done · 5s"


def test_sweep_toasts_expires_exactly_at_the_dwell() -> None:
    session, held = _session(), FakeClock()
    notify(session, held, text="copied", title="done", sev=Severity.INFO)
    held.advance(TOAST_DWELL)
    assert [t.text for t in sweep_toasts(session, held)] == ["copied"]


def test_sweep_toasts_each_toast_keeps_its_own_arming() -> None:
    session, held = _session(), FakeClock()
    notify(session, held, text="first", title="done", sev=Severity.INFO)
    held.advance(3)
    notify(session, held, text="second", title="done", sev=Severity.WARN)
    held.advance(2)
    assert [t.text for t in sweep_toasts(session, held)] == ["first"]
    assert [t.text for t in session.toasts] == ["second"]
    held.advance(3)
    assert [t.text for t in sweep_toasts(session, held)] == ["second"]


def test_sweep_toasts_empty_rack_changes_nothing() -> None:
    session, held = _session(), FakeClock()
    held.advance(60)
    assert sweep_toasts(session, held) == []
    assert session.log == []


def test_sweep_toasts_held_clock_keeps_every_toast() -> None:
    session, held = _session(), FakeClock()
    notify(session, held, text="copied", title="done", sev=Severity.INFO)
    assert not any(sweep_toasts(session, held) for _ in range(20))
    assert len(session.toasts) == 1


def test_time_reads_scan_flags_a_renderer_timestamp() -> None:
    # the scan must red on the defect it exists for, or its green result proves nothing
    bad = ast.parse(
        "import time\n"
        "from datetime import datetime\n"
        "def row():\n"
        "    return f'{time.time()} {datetime.now()}'\n"
        "def arm(app):\n"
        "    app.set_timer(1.5, app.cancel)\n"
    )
    assert sorted(_time_reads(bad)) == [1, 4, 4, 6]
    assert _time_reads(ast.parse("from datetime import UTC, datetime\nx = clock.now()\n")) == []


def test_console_modules_read_time_only_through_the_clock() -> None:
    offenders = {
        path.relative_to(CONSOLE_DIR).as_posix(): hits
        for path in sorted(CONSOLE_DIR.rglob("*.py"))
        if path.name != CLOCK_MODULE
        and (hits := _time_reads(ast.parse(path.read_text(encoding="utf-8"))))
    }
    assert offenders == {}


def test_console_clock_reads_time_but_schedules_nothing() -> None:
    tree = ast.parse((CONSOLE_DIR / CLOCK_MODULE).read_text(encoding="utf-8"))
    calls = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    assert "time.monotonic" in calls
    assert not {call.rsplit(".", 1)[-1] for call in calls} & _SCHEDULERS
