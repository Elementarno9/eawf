"""Stdin-first hook wrapper contract for P30-I23-W27.

Claude Code delivers hook input as a JSON document on stdin. SessionEnd,
Stop, and SubagentStop stdin carry the session cost + token usage totals
that feed EU capture (the D-EU-CAPTURE seam). Before this wave the rendered
wrappers synthesised a payload from positional args and threw the stdin
document away, so ``capture_runtime_on_session_end`` always reported
``runtime.capture skipped: no cost block`` and EU stayed at $0.

These tests pin the fixed contract end to end:

- CR-01: the rendered ``session_end`` wrapper forwards the stdin JSON
  verbatim, and feeding a recorded real SessionEnd payload through
  ``eawf hook run session_end`` lands a non-empty cost block on the
  ``runtime.capture`` RPC params.
- CR-02: the ``subagent_stop`` wrapper carries the same stdin-first
  contract with argv synthesis only as the empty-stdin fallback.

The recorded fixture matches the official Claude Code hooks reference
shape (tokens under a flat ``usage`` block, a string ``cost_usd``); the
runner normaliser bridges it to the statusline parser without touching
the statusline contract.
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

_BASH = shutil.which("bash")
_needs_bash = pytest.mark.skipif(_BASH is None, reason="bash is required to run the wrapper")


def _subagent_stop_payload() -> dict[str, Any]:
    """Return a path-scrubbed real SubagentStop stdin payload (with model)."""
    return {
        "session_id": "sess-placeholder-eu27",
        "agent_id": "agent-placeholder-01",
        "agent_type": "executor",
        "transcript_path": "/workspace/proj/.claude/session.jsonl",
        "cwd": "/workspace/proj",
        "effort": {"level": "high"},
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


# --- CR-01: session_end wrapper + runner seam ------------------------------


@_needs_bash
def test_session_end_wrapper_forwards_stdin_verbatim() -> None:
    fixture = _SESSION_END_FIXTURE.read_bytes()
    forwarded = _run_rendered_wrapper(HookEventType.SESSION_END, stdin=fixture)
    # Lossless forward: dropping any of the document would drop the cost totals.
    assert json.loads(forwarded) == json.loads(fixture)
    assert json.loads(forwarded)["cost"], "the cost block must survive the forward"


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


def test_session_end_stdin_delivers_cost_block_to_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[tuple[str, dict[str, Any]]] = []

    class _RecordingClient:
        def __enter__(self) -> _RecordingClient:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            recorded.append((method, params))
            return {"ok": True}

    monkeypatch.setattr(
        "eawf.runtime.hooks.runner._default_daemon_client_factory",
        lambda: _RecordingClient(),
    )
    fixture = _SESSION_END_FIXTURE.read_text(encoding="utf-8")
    result = CliRunner().invoke(
        app, ["hook", "run", "session_end", "--runtime", "claude"], input=fixture
    )
    assert result.exit_code == 0, result.stdout

    assert len(recorded) == 1
    method, params = recorded[0]
    assert method == "runtime.capture"
    # The non-empty cost block reaches capture_runtime_on_session_end.
    assert params["cost_usd"] == "0.8231"
    assert params["input_tokens"] == 152340
    assert params["output_tokens"] == 8215
    assert params["cache_creation_input_tokens"] == 12000
    assert params["cache_read_input_tokens"] == 141200
    assert params["harness"] == "claude-code"
    assert params["session_id"] == "sess-placeholder-eu27"


# --- CR-02: subagent_stop wrapper carries the same contract ----------------


@_needs_bash
def test_subagent_stop_wrapper_forwards_stdin_verbatim() -> None:
    payload = _subagent_stop_payload()
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


# --- runner normaliser: real hook shape -> statusline parser ---------------


def test_normalise_lifts_usage_and_coerces_string_cost_usd() -> None:
    counters = parse_runtime_counters(_normalise_claude_hook_payload(_subagent_stop_payload()))
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
    # crash the hook. The unusable cost_usd is dropped; the cost block stays a
    # dict so counters parse (with no cost value) rather than raising.
    normalised = _normalise_claude_hook_payload({"cost": {"cost_usd": "not-a-number"}, "usage": 7})
    counters = parse_runtime_counters(normalised)
    assert counters is not None
    assert counters.cost_usd is None
    assert counters.input_tokens is None
