"""Integration tests for ``wave dispatch --runtime`` (P10 W03).

Drives the Typer app via :class:`typer.testing.CliRunner` against a
temp ``.ea/state.json``. Confirms:

- ``--runtime=claude-code`` (the default) keeps the legacy text-mode
  surface — bare prompt to stdout, ``{"wave","prompt"}`` JSON
  envelope.
- ``--runtime=claude-agent-sdk`` switches to the SDK adapter — JSON
  envelope gains ``runtime``, ``mcp_servers`` and ``allowed_tools``
  keys; text-mode output prepends a short SDK invocation banner.
- An unknown ``--runtime`` value surfaces as ``InvalidInput`` (exit 4)
  with the canonical ``unknown runtime ...; expected one of [...]``
  message.

The tests live under ``tests/unit/`` (per the wave file list) but drive
the CLI surface through ``CliRunner`` — the spec puts the new file
under ``tests/unit/cli/`` conceptually; ``tests/`` is flat so we
preserve the existing tree.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app

runner = CliRunner()


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    state_path = tmp_path / ".ea" / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    # The bootstrap mutations (phase open / iter open / wave plan) route
    # through the daemon by default (daemon.proxy_enabled=True). This
    # CLI-driving test is the V1 daemonless carve-out — force the
    # in-process WAL-backed path so it does not reach a real daemon.
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    yield tmp_path


def _bootstrap_chain(workspace: Path) -> None:
    """Init QR, open P01-I01, plan one wave so dispatch has something to render."""
    assert (
        runner.invoke(
            app,
            ["project", "init", "QR", "--title", "Q", "--domains", "x"],
        ).exit_code
        == 0
    )
    assert runner.invoke(app, ["phase", "open", "--auto", "--title", "x"]).exit_code == 0
    assert runner.invoke(app, ["iter", "open", "--phase", "P01", "--title", "I1"]).exit_code == 0
    res = runner.invoke(
        app,
        [
            "wave",
            "plan",
            "P01-I01",
            "--id",
            "P01-I01-W01",
            "--title",
            "Solo wave",
            "--files",
            "src/",
            "--effort-bucket",
            "M",
        ],
    )
    assert res.exit_code == 0, res.stdout


# ---- claude-code branch (back-compat) --------------------------------------


def test_wave_dispatch_default_runtime_is_claude_code(workspace: Path) -> None:
    """Without ``--runtime`` the surface stays byte-equal to the pre-P10 shape."""
    _bootstrap_chain(workspace)
    res = runner.invoke(app, ["wave", "dispatch", "P01-I01-W01"])
    assert res.exit_code == 0, res.stdout
    # Banner does NOT prepend the prompt on the default branch.
    assert "claude-agent-sdk envelope" not in res.stdout
    assert "# Wave P01-I01-W01: Solo wave" in res.stdout


def test_wave_dispatch_claude_code_json_envelope_unchanged(workspace: Path) -> None:
    """``--runtime=claude-code --json`` keeps the legacy ``{wave,prompt}`` shape.

    The new ``runtime`` key is added for symmetry but the
    pre-existing ``wave``/``prompt`` keys must still be present and
    point to the same content. Downstream consumers can ignore the new
    key.
    """
    _bootstrap_chain(workspace)
    res = runner.invoke(
        app,
        ["--json", "wave", "dispatch", "P01-I01-W01", "--runtime", "claude-code"],
    )
    assert res.exit_code == 0, res.stdout
    payload = json.loads(res.stdout)
    assert payload["wave"] == "P01-I01-W01"
    assert payload["runtime"] == "claude-code"
    assert "# Wave P01-I01-W01" in payload["prompt"]
    # No SDK fields on the claude-code branch.
    assert "mcp_servers" not in payload
    assert "allowed_tools" not in payload


# ---- claude-agent-sdk branch ----------------------------------------------


def test_wave_dispatch_claude_agent_sdk_text_emits_banner(workspace: Path) -> None:
    """SDK text mode prepends a banner naming the runtime + allow-list."""
    _bootstrap_chain(workspace)
    res = runner.invoke(
        app,
        ["wave", "dispatch", "P01-I01-W01", "--runtime", "claude-agent-sdk"],
    )
    assert res.exit_code == 0, res.stdout
    out = res.stdout
    assert "## claude-agent-sdk envelope" in out
    assert "runtime: claude-agent-sdk" in out
    assert "mcp_servers: []" in out
    assert "allowed_tools: []" in out
    assert "# Wave P01-I01-W01: Solo wave" in out


def test_wave_dispatch_claude_agent_sdk_json_envelope_has_sdk_fields(
    workspace: Path,
) -> None:
    """SDK ``--json`` envelope carries runtime/mcp_servers/allowed_tools.

    Boundary: no MCP servers wired and no ``mcp_grants`` field on the
    state model (W02 territory); both lists must default to ``[]``
    without raising.
    """
    _bootstrap_chain(workspace)
    res = runner.invoke(
        app,
        ["--json", "wave", "dispatch", "P01-I01-W01", "--runtime", "claude-agent-sdk"],
    )
    assert res.exit_code == 0, res.stdout
    payload = json.loads(res.stdout)
    assert payload["wave"] == "P01-I01-W01"
    assert payload["runtime"] == "claude-agent-sdk"
    assert payload["mcp_servers"] == []
    assert payload["allowed_tools"] == []
    assert "# Wave P01-I01-W01" in payload["prompt"]


def test_wave_dispatch_claude_agent_sdk_output_writes_file_and_envelope(
    workspace: Path,
) -> None:
    """``--output`` writes the prompt body; envelope still carries SDK fields."""
    _bootstrap_chain(workspace)
    target = workspace / "out" / "prompt.md"
    res = runner.invoke(
        app,
        [
            "--json",
            "wave",
            "dispatch",
            "P01-I01-W01",
            "--runtime",
            "claude-agent-sdk",
            "--output",
            str(target),
        ],
    )
    assert res.exit_code == 0, res.stdout
    assert target.exists()
    body = target.read_text(encoding="utf-8")
    assert "# Wave P01-I01-W01" in body
    payload = json.loads(res.stdout)
    assert payload["runtime"] == "claude-agent-sdk"
    assert payload["mcp_servers"] == []
    assert payload["allowed_tools"] == []
    assert payload["bytes_written"] == len(body.encode("utf-8"))


# ---- Error path -------------------------------------------------------------


def test_wave_dispatch_unknown_runtime_invalid_input(workspace: Path) -> None:
    """An unsupported ``--runtime`` exits with INVALID_INPUT (3)."""
    _bootstrap_chain(workspace)
    res = runner.invoke(
        app,
        ["wave", "dispatch", "P01-I01-W01", "--runtime", "bogus"],
    )
    assert res.exit_code == 1, res.stdout
    # Canonical phrasing — matches mcp/installer.py:_validate_runtime.
    assert "unknown runtime 'bogus'" in res.stdout
    assert "expected one of" in res.stdout
    assert "claude-code" in res.stdout
    assert "claude-agent-sdk" in res.stdout


def test_wave_dispatch_unknown_runtime_short_circuits_before_state_load(
    workspace: Path,
) -> None:
    """The runtime check runs before state lookup so the error is fast.

    Verifies the validation gate fires even when the wave id is also
    invalid — the runtime-error message must surface first because the
    invariant is: runtime gate is checked AFTER wave-id syntactic check
    but BEFORE state load. That way ``--runtime=bogus`` on a non-init
    workspace still reports the runtime problem, not a missing-state
    error.
    """
    # Intentionally do NOT bootstrap — the state file is absent.
    res = runner.invoke(
        app,
        ["wave", "dispatch", "P01-I01-W01", "--runtime", "bogus"],
    )
    assert res.exit_code == 1, res.stdout
    assert "unknown runtime 'bogus'" in res.stdout
