"""Stdin-first hook wrapper contract, and the transcript-backed counter seam.

Claude Code delivers hook input as a JSON document on stdin. P30-I23-W27 fixed
the wrapper so that document is forwarded verbatim instead of synthesised from
argv; P30-I25-W25 fixes what the runner does with it.

The real Stop payload carries **no** cost block and **no** usage block -- its
keys are ``session_id``, ``transcript_path``, ``cwd``, ``prompt_id``,
``permission_mode``, ``effort``, ``hook_event_name``, ``stop_hook_active``,
``last_assistant_message``, ``background_tasks``, ``session_crons`` (the
``claude_session_end_stdin.json`` fixture is that key set). The counters live in
the session transcript the payload points at, so the runner aggregates
``transcript_path`` and falls back to a statusline-shaped parse only when no
transcript resolves. The prior fixture asserted a ``cost`` block production never
sends -- it was green while EU capture was dead.

These tests pin the fixed contract end to end:

- CR-01: the rendered ``session_end`` wrapper forwards the stdin JSON verbatim,
  and feeding the real (cost-free) Stop payload through ``eawf hook run
  session_end`` still lands real counters on the ``runtime.capture`` RPC params,
  sourced from the transcript.
- CR-02: the ``subagent_stop`` wrapper carries the same stdin-first contract with
  argv synthesis only as the empty-stdin fallback.
- The statusline-shaped fallback parse still works for a payload that does carry
  counters inline.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.runtime.hooks.event import HookEventType
from eawf.runtime.hooks.runner import _normalise_claude_hook_payload
from eawf.runtime.runtimes.claude.runtime_counters import parse_runtime_counters
from eawf.surfaces.cli.app import app
from eawf.surfaces.render.hooks import render_hook_sh

_FIXTURES = Path(__file__).parent / "fixtures"
_SESSION_END_FIXTURE = _FIXTURES / "claude_session_end_stdin.json"
_TRANSCRIPT_FIXTURE = _FIXTURES / "claude_session_transcript.jsonl"

_BASH = shutil.which("bash")
_needs_bash = pytest.mark.skipif(_BASH is None, reason="bash is required to run the wrapper")

#: Totals the transcript fixture aggregates to (two billed messages, each
#: repeated across content-block rows, plus one ``turn_duration`` row). The
#: duration is Claude's own figure for that turn -- not the span across the rows,
#: which is the wall clock and would include any wait for the operator to approve
#: a tool (P30-I25-W43).
_FIXTURE_DURATION_MS = 502968
_FIXTURE_INPUT_TOKENS = 4
_FIXTURE_OUTPUT_TOKENS = 1013
_FIXTURE_CACHE_CREATION_TOKENS = 67527
_FIXTURE_CACHE_READ_TOKENS = 61868


def _stop_payload_pointing_at_fixture() -> dict[str, Any]:
    """Return the real Stop key set with ``transcript_path`` on the fixture."""
    payload = json.loads(_SESSION_END_FIXTURE.read_text(encoding="utf-8"))
    payload["transcript_path"] = str(_TRANSCRIPT_FIXTURE)
    return payload


def _statusline_shaped_payload() -> dict[str, Any]:
    """Return a counter-carrying payload in the statusline (fallback) shape."""
    return {
        "session_id": "sess-placeholder-eu27",
        "hook_event_name": "SubagentStop",
        "model": "claude-sonnet-5",
        "cost": {
            "input_tokens": 4200,
            "output_tokens": 900,
            "total_tokens": 5100,
            "cached_input_tokens": 3000,
            "cost_usd": "0.0417",
        },
        "usage": {
            "input_tokens": 4200,
            "output_tokens": 900,
            "cache_creation_input_tokens": 500,
            "cache_read_input_tokens": 3000,
        },
    }


def _run_rendered_wrapper(
    event_type: HookEventType, *, stdin: bytes, positional: tuple[str, ...] = ()
) -> bytes:
    """Run the rendered wrapper and return the stdin bytes it forwards to ``uv``.

    The wrapper ends in ``... | exec uv run eawf hook run <event>``. A fake
    ``uv`` on ``PATH`` records whatever the wrapper piped into it, so the test
    observes exactly what the wrapper would hand to the CLI without invoking a
    real ``uv``/``eawf``.
    """
    assert _BASH is not None
    script = render_hook_sh(event_type)
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        wrapper = tmp / f"{event_type.value}.sh"
        wrapper.write_text(script, encoding="utf-8")
        shim_dir = tmp / "bin"
        shim_dir.mkdir()
        capture = tmp / "forwarded_stdin"
        uv_shim = shim_dir / "uv"
        uv_shim.write_text('#!/usr/bin/env bash\ncat > "$EAWF_TEST_CAPTURE"\n', encoding="utf-8")
        uv_shim.chmod(0o755)
        env = dict(os.environ)
        env["PATH"] = f"{shim_dir}{os.pathsep}{env.get('PATH', '')}"
        env["EAWF_TEST_CAPTURE"] = str(capture)
        result = subprocess.run(
            [_BASH, str(wrapper), *positional],
            input=stdin,
            env=env,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
        assert capture.exists(), "wrapper forwarded nothing to uv"
        return capture.read_bytes()


class _RecordingClient:
    """Daemon-client stand-in recording every RPC the hook issues."""

    def __init__(self, sink: list[tuple[str, dict[str, Any]]]) -> None:
        self._sink = sink

    def __enter__(self) -> _RecordingClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._sink.append((method, params))
        return {"ok": True}


def _record_capture_rpc(
    monkeypatch: pytest.MonkeyPatch, *, stdin: str
) -> list[tuple[str, dict[str, Any]]]:
    """Drive ``eawf hook run session_end`` on *stdin*, returning the RPCs it made."""
    recorded: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "eawf.runtime.hooks.runner._default_daemon_client_factory",
        lambda: _RecordingClient(recorded),
    )
    result = CliRunner().invoke(
        app, ["hook", "run", "session_end", "--runtime", "claude"], input=stdin
    )
    assert result.exit_code == 0, result.stdout
    return recorded


# --- CR-01: session_end wrapper + runner seam ------------------------------


@_needs_bash
def test_session_end_wrapper_forwards_stdin_verbatim() -> None:
    fixture = _SESSION_END_FIXTURE.read_bytes()
    forwarded = _run_rendered_wrapper(HookEventType.SESSION_END, stdin=fixture)
    # Lossless forward: dropping any of the document would drop transcript_path,
    # which is the only route to the session's counters.
    assert json.loads(forwarded) == json.loads(fixture)
    assert json.loads(forwarded)["transcript_path"], "transcript_path must survive the forward"


@_needs_bash
def test_session_end_wrapper_falls_back_to_argv_on_empty_stdin() -> None:
    forwarded = _run_rendered_wrapper(HookEventType.SESSION_END, stdin=b"")
    assert json.loads(forwarded) == {
        "hook_event_name": "SessionEnd",
        "claude_event_name": "session_end",
        "args": ["", "", "", ""],
    }


def test_session_end_wrapper_is_stdin_first_with_argv_fallback() -> None:
    script = render_hook_sh(HookEventType.SESSION_END)
    assert "if [ ! -t 0 ]; then" in script
    assert '_eawf_stdin="$(cat)"' in script
    assert 'if [ -n "${_eawf_stdin}" ]; then' in script
    assert "eawf hook run session_end --runtime claude" in script
    # The argv synthesis survives, but only inside the empty-stdin else branch.
    assert '"hook_event_name":"%s"' in script


def test_real_stop_payload_carries_no_cost_block() -> None:
    payload = json.loads(_SESSION_END_FIXTURE.read_text(encoding="utf-8"))
    # The regression the fixture exists to pin: production never sends `cost`
    # or `usage`, so a parser gated on them captures nothing, forever.
    assert "cost" not in payload
    assert "usage" not in payload
    assert parse_runtime_counters(_normalise_claude_hook_payload(payload)) is None


def test_stop_stdin_delivers_transcript_counters_to_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdin = json.dumps(_stop_payload_pointing_at_fixture())
    recorded = _record_capture_rpc(monkeypatch, stdin=stdin)

    assert len(recorded) == 1
    method, params = recorded[0]
    assert method == "runtime.capture"
    # The cost-free Stop payload still yields real counters, read off the
    # transcript it points at.
    assert params["api_duration_ms"] == _FIXTURE_DURATION_MS
    assert params["total_duration_ms"] == _FIXTURE_DURATION_MS
    assert params["input_tokens"] == _FIXTURE_INPUT_TOKENS
    assert params["output_tokens"] == _FIXTURE_OUTPUT_TOKENS
    assert params["cache_creation_input_tokens"] == _FIXTURE_CACHE_CREATION_TOKENS
    assert params["cache_read_input_tokens"] == _FIXTURE_CACHE_READ_TOKENS
    assert params["harness"] == "claude-code"
    assert params["model"] == "claude-opus-4-8"
    assert Decimal(params["cost_usd"]) > 0
    assert params["session_id"] == "sess-placeholder-eu27"


def test_stop_stdin_with_unreadable_transcript_is_a_clean_no_op(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = json.loads(_SESSION_END_FIXTURE.read_text(encoding="utf-8"))
    payload["transcript_path"] = str(tmp_path / "absent.jsonl")
    recorded = _record_capture_rpc(monkeypatch, stdin=json.dumps(payload))
    # Fail-open: no transcript, no statusline block -> no RPC, exit 0.
    assert recorded == []


def test_statusline_shaped_payload_still_captures(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _record_capture_rpc(monkeypatch, stdin=json.dumps(_statusline_shaped_payload()))
    assert len(recorded) == 1
    _, params = recorded[0]
    # The fallback path: a payload carrying counters inline needs no transcript.
    assert params["cost_usd"] == "0.0417"
    assert params["input_tokens"] == 4200
    assert params["model"] == "claude-sonnet-5"


# --- CR-02: subagent_stop wrapper carries the same contract ----------------


@_needs_bash
def test_subagent_stop_wrapper_forwards_stdin_verbatim() -> None:
    payload = _statusline_shaped_payload()
    forwarded = _run_rendered_wrapper(
        HookEventType.SUBAGENT_STOP, stdin=json.dumps(payload).encode("utf-8")
    )
    assert json.loads(forwarded) == payload


@_needs_bash
def test_subagent_stop_wrapper_falls_back_to_argv_on_empty_stdin() -> None:
    forwarded = _run_rendered_wrapper(HookEventType.SUBAGENT_STOP, stdin=b"")
    assert json.loads(forwarded) == {
        "hook_event_name": "SubagentStop",
        "claude_event_name": "subagent_stop",
        "args": ["", "", "", ""],
    }


def test_subagent_stop_wrapper_is_stdin_first_with_argv_fallback() -> None:
    script = render_hook_sh(HookEventType.SUBAGENT_STOP)
    assert "if [ ! -t 0 ]; then" in script
    assert '_eawf_stdin="$(cat)"' in script
    assert 'if [ -n "${_eawf_stdin}" ]; then' in script
    assert "eawf hook run subagent_stop --runtime claude" in script
    assert '"hook_event_name":"%s"' in script


# --- runner normaliser: counter-carrying payload -> statusline parser -------


def test_normalise_lifts_usage_and_coerces_string_cost_usd() -> None:
    counters = parse_runtime_counters(_normalise_claude_hook_payload(_statusline_shaped_payload()))
    assert counters is not None
    assert counters.cost_usd == Decimal("0.0417")
    assert counters.input_tokens == 4200
    assert counters.output_tokens == 900
    assert counters.cache_creation_input_tokens == 500
    assert counters.cache_read_input_tokens == 3000
    assert counters.model == "claude-sonnet-5"
    assert counters.harness == "claude-code"


def test_normalise_is_noop_for_statusline_shape() -> None:
    statusline = {
        "cost": {"api_duration_ms": 17000, "total_duration_ms": 21000, "cost_usd": 0.42},
        "context_window": {"current_usage": {"input_tokens": 100, "output_tokens": 50}},
        "model": {"id": "claude-opus-4-7"},
    }
    counters = parse_runtime_counters(_normalise_claude_hook_payload(statusline))
    assert counters is not None
    # The statusline shape is untouched: numeric cost_usd, durations, and the
    # existing context_window all still parse.
    assert counters.cost_usd == Decimal("0.42")
    assert counters.api_duration_ms == 17000
    assert counters.total_duration_ms == 21000
    assert counters.input_tokens == 100
    assert counters.output_tokens == 50
    assert counters.model == "claude-opus-4-7"


def test_normalise_never_raises_on_malformed_cost_block() -> None:
    # Fail-open: a non-numeric string cost_usd and a non-dict usage must not
    # crash the hook. The unusable cost_usd is dropped, leaving no usable
    # counter at all, so the parse degrades to None rather than raising.
    normalised = _normalise_claude_hook_payload({"cost": {"cost_usd": "not-a-number"}, "usage": 7})
    assert parse_runtime_counters(normalised) is None
