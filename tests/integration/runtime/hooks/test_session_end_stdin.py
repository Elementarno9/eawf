"""Stdin-first hook wrapper contract.

Claude Code delivers hook input as a JSON document on stdin. The rendered
wrappers forward that document verbatim instead of synthesising one from argv,
which is kept only as the empty-stdin fallback. The real Stop payload's key set
is the ``claude_session_end_stdin.json`` fixture.

- The rendered ``session_end`` wrapper forwards the stdin JSON verbatim.
- The ``subagent_stop`` wrapper carries the same stdin-first contract.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from eawf.runtime.hooks.event import HookEventType
from eawf.surfaces.render.hooks import render_hook_sh

_FIXTURES = Path(__file__).parent / "fixtures"
_SESSION_END_FIXTURE = _FIXTURES / "claude_session_end_stdin.json"

_BASH = shutil.which("bash")
_needs_bash = pytest.mark.skipif(_BASH is None, reason="bash is required to run the wrapper")


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


@_needs_bash
def test_wrapper_bootstraps_uv_from_stripped_path() -> None:
    """A hook fired with a stripped PATH still resolves ``uv`` (no exit 127).

    Simulates a git-worktree / GUI-launched session whose PATH omits the uv
    install dir. The wrapper's PATH bootstrap appends ``$HOME/.local/bin`` (and
    peers), so a ``uv`` living only there is found, resolved to an absolute
    path, and the payload reaches it instead of dying with command-not-found.
    """
    assert _BASH is not None
    script = render_hook_sh(HookEventType.SESSION_END)
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        home = tmp / "home"
        (home / ".local" / "bin").mkdir(parents=True)
        wrapper = tmp / "session_end.sh"
        wrapper.write_text(script, encoding="utf-8")
        capture = tmp / "forwarded_stdin"
        # A fake uv ONLY in a bootstrap dir, deliberately absent from PATH.
        uv_shim = home / ".local" / "bin" / "uv"
        uv_shim.write_text('#!/usr/bin/env bash\ncat > "$EAWF_TEST_CAPTURE"\n', encoding="utf-8")
        uv_shim.chmod(0o755)
        env = {
            "PATH": "/usr/bin:/bin",  # stripped: no uv anywhere on it
            "HOME": str(home),
            "EAWF_TEST_CAPTURE": str(capture),
        }
        result = subprocess.run(
            [_BASH, str(wrapper)],
            input=b'{"session_id":"stripped-path"}',
            env=env,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
        assert capture.exists(), "bootstrap failed to resolve uv from a stripped PATH"
        assert capture.read_bytes() == b'{"session_id":"stripped-path"}'


# --- session_end wrapper ----------------------------------------------------


@_needs_bash
def test_session_end_wrapper_forwards_stdin_verbatim() -> None:
    fixture = _SESSION_END_FIXTURE.read_bytes()
    forwarded = _run_rendered_wrapper(HookEventType.SESSION_END, stdin=fixture)
    # Lossless forward: the wrapper drops no key of the document.
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


# --- subagent_stop wrapper carries the same contract ------------------------


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
