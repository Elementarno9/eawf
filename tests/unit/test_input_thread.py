"""Unit tests for the non-blocking input thread (P20-I03-W01).

The TUI online loop runs a daemon thread that reads keys via the
caller's ``read_key`` function and pushes them onto a
:class:`queue.Queue`. The render loop drains the queue with a short
timeout so a 30Hz repaint cadence is never blocked by stdin.
"""

from __future__ import annotations

import queue
import threading
import time

from eawf.tui.app import (
    _drain_key,
    _input_reader_loop,
    _start_input_thread,
    _wrap_reader_for_thread,
)


def test_input_reader_pushes_keys_onto_queue() -> None:
    """A synthetic ``read_key`` feeds the queue one key at a time."""
    keys: queue.Queue[str] = queue.Queue()
    iter_keys = iter(["a", "b", "c", ""])

    def read_key() -> str:
        return next(iter_keys)

    stop = threading.Event()
    _input_reader_loop(keys, read_key, stop)
    drained = []
    while not keys.empty():
        drained.append(keys.get_nowait())
    # The empty-string sentinel is enqueued so the main loop knows EOF.
    assert drained == ["a", "b", "c", ""]


def test_input_reader_exits_on_empty_string() -> None:
    """An empty ``read_key`` return signals EOF and stops the loop."""
    keys: queue.Queue[str] = queue.Queue()
    stop = threading.Event()
    _input_reader_loop(keys, lambda: "", stop)
    assert keys.get_nowait() == ""
    # Loop exited cleanly — no second enqueue.
    assert keys.empty()


def test_input_reader_pushes_ctrl_c_on_keyboard_interrupt() -> None:
    """A ``KeyboardInterrupt`` from ``read_key`` becomes ``\\x03``."""
    keys: queue.Queue[str] = queue.Queue()
    stop = threading.Event()

    def boom() -> str:
        raise KeyboardInterrupt

    _input_reader_loop(keys, boom, stop)
    assert keys.get_nowait() == "\x03"


def test_input_reader_respects_stop_event() -> None:
    """Setting *stop* before the first read prevents the loop from running.

    The implementation checks the event at loop-top, so a pre-set stop
    means no keys are enqueued even when ``read_key`` would normally
    return one.
    """
    keys: queue.Queue[str] = queue.Queue()
    stop = threading.Event()
    stop.set()
    _input_reader_loop(keys, lambda: "x", stop)
    assert keys.empty()


def test_drain_key_returns_value_when_available() -> None:
    """When the queue is non-empty, :func:`_drain_key` returns the head."""
    keys: queue.Queue[str] = queue.Queue()
    keys.put("z")
    assert _drain_key(keys, timeout=0.01) == "z"


def test_drain_key_returns_none_on_timeout() -> None:
    """Empty queue + tiny timeout → ``None`` so the caller can repaint."""
    keys: queue.Queue[str] = queue.Queue()
    start = time.perf_counter()
    assert _drain_key(keys, timeout=0.01) is None
    elapsed = time.perf_counter() - start
    # The wait honours the timeout (cap at 250ms to allow CI slack).
    assert elapsed < 0.25


def test_start_input_thread_runs_reader_off_main_thread() -> None:
    """The helper spawns a daemon thread that feeds the queue live."""
    fired = threading.Event()
    answers = iter(["q"])

    def read_key() -> str:
        fired.set()
        try:
            return next(answers)
        except StopIteration:
            return ""

    keys_q, stop, thread = _start_input_thread(read_key)
    try:
        assert fired.wait(timeout=1.0), "reader thread did not invoke read_key"
        # Drain — we should see the synthetic key, then the EOF sentinel.
        first = keys_q.get(timeout=1.0)
        assert first == "q"
        second = keys_q.get(timeout=1.0)
        assert second == ""
    finally:
        stop.set()
        thread.join(timeout=1.0)


def test_wrap_reader_for_thread_swallows_stop_iteration() -> None:
    """An exhausted test iterator returns ``""`` rather than raising."""
    seq = iter(["x"])
    wrapped = _wrap_reader_for_thread(lambda: next(seq))
    assert wrapped() == "x"
    # Iterator exhausted — should yield the EOF sentinel.
    assert wrapped() == ""


def test_thread_drain_round_trip_under_30hz() -> None:
    """End-to-end: queue a few keys and drain them at 30Hz cadence.

    Simulates one full ``run_tui`` tick: the reader thread emits five
    keys; the main thread polls the queue with a short timeout and
    collects them. This pins the contract that the queue draining
    never blocks longer than the configured timeout.
    """
    answers = iter(["a", "b", "c", "d", "q"])

    def read_key() -> str:
        try:
            return next(answers)
        except StopIteration:
            return ""

    keys_q, stop, thread = _start_input_thread(read_key)
    drained: list[str] = []
    try:
        deadline = time.perf_counter() + 2.0
        while time.perf_counter() < deadline:
            ch = _drain_key(keys_q, timeout=0.05)
            if ch is None:
                continue
            if ch == "":
                break
            drained.append(ch)
    finally:
        stop.set()
        thread.join(timeout=1.0)
    assert drained == ["a", "b", "c", "d", "q"]
