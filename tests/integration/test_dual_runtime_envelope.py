"""Ship-gate: dual-runtime dispatch envelope equivalence.

Verifies that ``eawf wave dispatch <wave_id> --runtime=claude-code`` and
``eawf wave dispatch <wave_id> --runtime=claude-agent-sdk`` agree on the
prompt body — the SDK branch is a strict superset that adds ``runtime``,
``mcp_servers``, and ``allowed_tools`` projected from
``state.mcp_grants``.

Spec source: ``.ea/local/research/p10-plan.md`` §W04. Mirrors the
research-brief mind-change criterion: when the dual-runtime envelopes
diverge in a non-trivial way, the W02 ``McpGrant`` shape needs richer
fields and the phase reopens W02 instead of closing.
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
    yield tmp_path


def _bootstrap_wave(workspace: Path) -> str:
    """Init QR, open P01-I01, plan a single wave; return the wave id."""
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
            "ship-gate-fixture-wave",
            "--files",
            "src/",
            "--effort-bucket",
            "M",
        ],
    )
    assert res.exit_code == 0, res.stdout
    return "P01-I01-W01"


def _add_mcp_server(server_id: str) -> None:
    res = runner.invoke(
        app,
        ["mcp", "add", server_id, "--command", "/usr/bin/true"],
    )
    assert res.exit_code == 0, res.stdout


def _grant(scope_kind: str, scope_id: str, server_id: str) -> None:
    res = runner.invoke(
        app,
        ["mcp", "grant", scope_kind, scope_id, server_id],
    )
    assert res.exit_code == 0, res.stdout


def _dispatch_json(wave_id: str, runtime: str) -> dict[str, object]:
    res = runner.invoke(
        app,
        ["--json", "wave", "dispatch", wave_id, "--runtime", runtime],
    )
    assert res.exit_code == 0, res.stdout
    return json.loads(res.stdout)


def test_dual_runtime_envelope_prompt_byte_equivalent(workspace: Path) -> None:
    """Stripping ``runtime`` + SDK-only fields, both envelopes hold the same prompt."""
    wave_id = _bootstrap_wave(workspace)

    cc_env = _dispatch_json(wave_id, "claude-code")
    sdk_env = _dispatch_json(wave_id, "claude-agent-sdk")

    cc_normalised = {k: v for k, v in cc_env.items() if k != "runtime"}
    sdk_normalised = {
        k: v for k, v in sdk_env.items() if k not in {"runtime", "mcp_servers", "allowed_tools"}
    }
    assert cc_normalised == sdk_normalised

    # Prompt body is identical byte-for-byte across both runtimes.
    assert cc_env["prompt"] == sdk_env["prompt"]
    # Runtime field differs as expected.
    assert cc_env["runtime"] == "claude-code"
    assert sdk_env["runtime"] == "claude-agent-sdk"


def test_sdk_envelope_empty_grants_allowed_tools_empty(workspace: Path) -> None:
    """When ``state.mcp_grants`` is unset/empty, SDK allowed_tools = []."""
    wave_id = _bootstrap_wave(workspace)

    sdk_env = _dispatch_json(wave_id, "claude-agent-sdk")
    assert sdk_env["allowed_tools"] == []
    assert sdk_env["mcp_servers"] == []


def test_sdk_envelope_wave_grant_projects_to_allowed_tools(workspace: Path) -> None:
    """A wave-scoped grant for the dispatched wave projects to ``mcp__<server>__*``."""
    wave_id = _bootstrap_wave(workspace)
    _add_mcp_server("fixture-srv")
    _grant("wave", wave_id, "fixture-srv")

    sdk_env = _dispatch_json(wave_id, "claude-agent-sdk")
    assert sdk_env["allowed_tools"] == ["mcp__fixture-srv__*"]
    # mcp_servers projects the registered server pool, regardless of grants.
    server_ids = [s["id"] for s in sdk_env["mcp_servers"]]
    assert server_ids == ["fixture-srv"]


def test_sdk_envelope_other_wave_grant_does_not_leak(workspace: Path) -> None:
    """A grant scoped to a different wave does NOT enter allowed_tools."""
    wave_id = _bootstrap_wave(workspace)
    _add_mcp_server("other-srv")
    _grant("wave", "P01-I01-W99", "other-srv")

    sdk_env = _dispatch_json(wave_id, "claude-agent-sdk")
    assert sdk_env["allowed_tools"] == []
    # The server still appears in mcp_servers — projection is independent of grants.
    server_ids = [s["id"] for s in sdk_env["mcp_servers"]]
    assert server_ids == ["other-srv"]


def test_claude_code_envelope_omits_sdk_only_fields(workspace: Path) -> None:
    """The claude-code branch carries no ``mcp_servers`` or ``allowed_tools`` keys."""
    wave_id = _bootstrap_wave(workspace)
    _add_mcp_server("noop-srv")
    _grant("wave", wave_id, "noop-srv")

    cc_env = _dispatch_json(wave_id, "claude-code")
    assert "mcp_servers" not in cc_env
    assert "allowed_tools" not in cc_env
    assert cc_env["runtime"] == "claude-code"


def test_dual_runtime_unknown_runtime_rejected(workspace: Path) -> None:
    """An unknown ``--runtime`` value fails with InvalidInput (exit 3)."""
    wave_id = _bootstrap_wave(workspace)
    res = runner.invoke(
        app,
        ["wave", "dispatch", wave_id, "--runtime", "claude-bogus"],
    )
    assert res.exit_code == 1, res.stdout
    assert "unknown runtime" in res.stdout
