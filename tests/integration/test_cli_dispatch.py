"""Integration tests for the Typer dispatcher round-trip.

Drives the root ``eawf`` app via :class:`typer.testing.CliRunner` to confirm
the global flags, version envelope, and unknown-command exit behaviour all
hang together correctly.
"""

from __future__ import annotations

import json

from packaging.version import Version
from typer.testing import CliRunner

import eawf
from eawf.surfaces.cli.app import app
from eawf.surfaces.render.brand import ACCENT_HEX, accent_sgr

runner = CliRunner()


def test_version_json_envelope_round_trips() -> None:
    result = runner.invoke(app, ["--json", "version"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    # The surfaced version is composed: an editable checkout (the test
    # tree) carries a PEP 440 ``+dev.g<sha>`` local segment, a wheel build
    # carries the bare base. Assert the PEP 440 *base version* matches the
    # single source (eawf.__version__) so the test stays green on both
    # paths and across a release bump.
    assert Version(payload["version"]).base_version == eawf.__version__


def test_unknown_command_exits_with_code_2() -> None:
    """Typer maps unknown subcommands to a non-zero exit (Click's default 2)."""
    result = runner.invoke(app, ["does-not-exist"])
    assert result.exit_code == 2


def test_bare_invocation_prints_banner() -> None:
    """``eawf`` with no subcommand routes to the TUI surface (P14-W10).

    Off-TTY (CliRunner has no real terminal) the TUI falls back to the
    deterministic status text. Per D-BRAND-MARK the brand mark leads the
    header: the ``◉`` accent glyph, a space, then the two-tone ``Eä``
    wordmark (the brand accent SGR sits between the ``E`` and the ``ä``).
    The assert pins the exact ``◉ E<accent-sgr>ä`` byte run the brand
    renderer emits.
    """
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert result.stdout.startswith(f"◉ E{accent_sgr(ACCENT_HEX)}ä")


def test_version_text_envelope() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    # The text banner carries the composed display version; an editable
    # checkout appends a ``+dev.g<sha>`` local segment, so assert the base
    # version prefix rather than an exact bare-base match.
    assert result.stdout.startswith(f"eawf {eawf.__version__}")


def test_version_flag_short_circuits() -> None:
    """The ``--version`` eager flag exits before any subcommand runs."""
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert eawf.__version__ in result.stdout


def test_validate_subcommand_still_registered() -> None:
    """Phase 1 ``validate`` command must remain wired through the new app."""
    result = runner.invoke(app, ["validate", "--help"])
    assert result.exit_code == 0
    # Phase 4 W01 narrowed the help to mention envelope mode too;
    # check for the stable ``Validate a state`` substring that
    # survives the narrowing.
    assert "Validate a state" in result.stdout
