"""Tests for global flag parsing and propagation via Typer's ctx.obj.

The Typer root callback in :mod:`eawf.surfaces.cli.app` resolves ``--json``, ``--plain``,
``--no-input``, and ``-w/--workspace`` into a :class:`eawf.surfaces.cli.flags.GlobalFlags`
dataclass attached to ``ctx.obj``. The hidden ``scope-debug`` subcommand prints
the resolved flags so we can assert propagation via ``CliRunner`` without
instantiating each downstream command.

``--scope`` is intentionally *not* a global flag (W5 demotion) — subcommands
that consume a scope ID declare their own per-command option. We assert below
that the root callback rejects ``--scope`` to lock that decision in.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app

runner = CliRunner()


def test_json_flag_recognised() -> None:
    result = runner.invoke(app, ["--json", "version"])
    assert result.exit_code == 0
    assert "0.2.0" in result.stdout


def test_plain_flag_disables_color() -> None:
    result = runner.invoke(app, ["--plain", "version"])
    assert result.exit_code == 0


def test_workspace_flag_propagates_to_context() -> None:
    result = runner.invoke(app, ["-w", "/tmp/fake-ws", "scope-debug"])
    assert result.exit_code == 0
    assert "workspace=/tmp/fake-ws" in result.stdout


def test_scope_flag_is_not_a_global_option() -> None:
    """W5: --scope is intentionally per-command, not global.

    Click rejects unknown options with exit code 2 and emits "No such option"
    on stderr. We assert both signals so a future re-introduction of a global
    --scope cannot land silently.
    """
    result = runner.invoke(app, ["--scope", "P01-I01", "scope-debug"])
    assert result.exit_code == 2
    combined = result.stdout + (result.stderr or "")
    assert "No such option" in combined or "no such option" in combined.lower()


def test_no_input_disables_prompts() -> None:
    result = runner.invoke(app, ["--no-input", "version"])
    assert result.exit_code == 0


@pytest.mark.parametrize("flag", ["--json", "--plain", "--no-input"])
def test_flag_can_appear_before_subcommand(flag: str) -> None:
    result = runner.invoke(app, [flag, "version"])
    assert result.exit_code == 0


def test_workspace_long_form_propagates() -> None:
    result = runner.invoke(app, ["--workspace", "/tmp/another-ws", "scope-debug"])
    assert result.exit_code == 0
    assert "workspace=/tmp/another-ws" in result.stdout


def test_default_flags_have_unset_values() -> None:
    """Without any flags, scope-debug shows the default GlobalFlags values."""
    result = runner.invoke(app, ["scope-debug"])
    assert result.exit_code == 0
    assert "workspace=None" in result.stdout
    assert "json=False" in result.stdout
    assert "plain=False" in result.stdout


def test_combined_flags_set_independently() -> None:
    """All global flags can be combined and each is reported via scope-debug."""
    result = runner.invoke(
        app,
        [
            "--json",
            "--plain",
            "--no-input",
            "-w",
            "/tmp/combo",
            "scope-debug",
        ],
    )
    assert result.exit_code == 0
    # JSON output is selected, but scope-debug always echoes a text payload via
    # typer.echo regardless. The handler emits the multi-line text body.
    assert "workspace=/tmp/combo" in result.stdout
    assert "json=True" in result.stdout
    assert "plain=True" in result.stdout
