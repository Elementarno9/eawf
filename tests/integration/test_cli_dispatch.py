"""Integration tests for the Typer dispatcher round-trip.

Drives the root ``eawf`` app via :class:`typer.testing.CliRunner` to confirm
the global flags, version envelope, and unknown-command exit behaviour all
hang together correctly.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from eawf.cli.app import app

runner = CliRunner()


def test_version_json_envelope_round_trips() -> None:
    result = runner.invoke(app, ["--json", "version"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["version"].startswith("0.1.0")


def test_unknown_command_exits_with_code_2() -> None:
    """Typer maps unknown subcommands to a non-zero exit (Click's default 2)."""
    result = runner.invoke(app, ["does-not-exist"])
    assert result.exit_code == 2


def test_bare_invocation_prints_banner() -> None:
    """``eawf`` with no subcommand emits the version banner — Phase 1 contract."""
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "eawf 0.1.0" in result.stdout
    assert "v0.1 in development" in result.stdout


def test_version_text_envelope() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "eawf 0.1.0" in result.stdout


def test_version_flag_short_circuits() -> None:
    """The ``--version`` eager flag exits before any subcommand runs."""
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.stdout


def test_validate_subcommand_still_registered() -> None:
    """Phase 1 ``validate`` command must remain wired through the new app."""
    result = runner.invoke(app, ["validate", "--help"])
    assert result.exit_code == 0
    assert "Validate a state document" in result.stdout
