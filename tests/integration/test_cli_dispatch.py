"""Integration tests for the Typer dispatcher round-trip.

Drives the root ``eawf`` app via :class:`typer.testing.CliRunner` to confirm
the global flags, version envelope, and unknown-command exit behaviour all
hang together correctly.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from eawf.surfaces.cli.app import app

runner = CliRunner()


def test_version_json_envelope_round_trips() -> None:
    result = runner.invoke(app, ["--json", "version"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["version"].startswith("0.3.0")


def test_unknown_command_exits_with_code_2() -> None:
    """Typer maps unknown subcommands to a non-zero exit (Click's default 2)."""
    result = runner.invoke(app, ["does-not-exist"])
    assert result.exit_code == 2


def test_bare_invocation_prints_banner() -> None:
    """``eawf`` with no subcommand routes to the TUI surface (P14-W10).

    Off-TTY (CliRunner has no real terminal) the TUI falls back to the
    deterministic status text — its first byte is the ``Eä`` brand per
    the W10 contract.
    """
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "Eä" in result.stdout


def test_version_text_envelope() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "eawf 0.3.0" in result.stdout


def test_version_flag_short_circuits() -> None:
    """The ``--version`` eager flag exits before any subcommand runs."""
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "0.3.0" in result.stdout


def test_validate_subcommand_still_registered() -> None:
    """Phase 1 ``validate`` command must remain wired through the new app."""
    result = runner.invoke(app, ["validate", "--help"])
    assert result.exit_code == 0
    # Phase 4 W01 narrowed the help to mention envelope mode too;
    # check for the stable ``Validate a state`` substring that
    # survives the narrowing.
    assert "Validate a state" in result.stdout
