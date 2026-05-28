"""Integration tests for the regrouped ``eawf --help`` panel layout (P20-W12).

The brief requires top-level command help to follow the metadata registry
alphabetical order — panels named after the
:data:`eawf.kernel.config.registry.CONFIG_REGISTRY` tabs, alphabetical between
panels and alphabetical-by-command-name within each panel.

These tests parse the actual ``eawf --help`` output via Typer's
:class:`CliRunner`, strip ANSI sequences for determinism, and compare
against the structured golden under
``tests/golden/cli/help_panels.golden.txt``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.help_panels import COMMAND_PANELS, PANEL_ORDER

runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_PANEL_HEADER_RE = re.compile(r"^[╭──]+\s+(\S[^──]*?)\s+[──]+[╮]?$")
# Real command rows start with ``│ <name>`` — the command name sits at a
# fixed column (1 space after the box-drawing char). Continuation rows are
# padded with the column width's worth of spaces, so the regex anchors on
# *exactly one* leading space inside the box to reject continuations.
_COMMAND_ROW_RE = re.compile(r"^│ (\S+)\s+")

_GOLDEN_PATH = Path(__file__).parent.parent / "golden" / "cli" / "help_panels.golden.txt"


def _strip_ansi(text: str) -> str:
    """Drop ANSI escape sequences so the parser sees plain text."""
    return _ANSI_RE.sub("", text)


def _extract_panels(help_text: str) -> list[tuple[str, list[str]]]:
    """Parse the rendered help into ``[(panel_name, [command_names])]``.

    The parser walks the ANSI-stripped lines and (a) records every panel
    header line that follows ``╭─ <name> ──...``, then (b) accumulates
    command names from subsequent ``│ <name>   <desc>`` rows until the
    closing ``╰─`` line. Options panels and the leading Usage block are
    skipped — only command panels are returned.
    """
    panels: list[tuple[str, list[str]]] = []
    current_name: str | None = None
    current_commands: list[str] = []
    for raw_line in help_text.splitlines():
        line = raw_line.rstrip()
        header = _PANEL_HEADER_RE.match(line)
        if header:
            if current_name is not None:
                panels.append((current_name, current_commands))
            current_name = header.group(1).strip()
            current_commands = []
            continue
        if line.startswith("╰") and current_name is not None:
            panels.append((current_name, current_commands))
            current_name = None
            current_commands = []
            continue
        if current_name is not None:
            match = _COMMAND_ROW_RE.match(line)
            if match:
                token = match.group(1)
                # Options-panel rows start with ``--flag`` — skip; only
                # command names are tracked for ordering checks.
                if not token.startswith("-") and token not in current_commands:
                    current_commands.append(token)
    if current_name is not None:
        panels.append((current_name, current_commands))
    return panels


def _command_panels_only(
    panels: list[tuple[str, list[str]]],
) -> list[tuple[str, list[str]]]:
    """Filter out the Options panel — only command panels matter for ordering."""
    return [
        (name, cmds)
        for name, cmds in panels
        if name not in {"Options"} and not name.startswith("Options")
    ]


def test_help_panels_render_in_alphabetical_order() -> None:
    """Command panels render in alphabetical order of panel name."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    panels = _command_panels_only(_extract_panels(_strip_ansi(result.stdout)))
    panel_names = [name for name, _ in panels]
    assert panel_names == sorted(panel_names), panel_names
    # Registry tabs plus Click's fallback ``Commands`` panel are allowed.
    assert set(panel_names).issubset(set(PANEL_ORDER) | {"Commands"}), panel_names


def test_commands_within_each_panel_are_alphabetical() -> None:
    """Commands inside each panel render in alphabetical order."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    panels = _command_panels_only(_extract_panels(_strip_ansi(result.stdout)))
    for name, commands in panels:
        assert commands == sorted(commands), f"panel {name}: {commands}"


def test_every_root_command_has_a_panel_assignment() -> None:
    """Every registry-panel command in ``--help`` has a :data:`COMMAND_PANELS` entry.

    A future ``app.add_typer(...)`` registration that forgets to set
    ``rich_help_panel`` lands in Click's default ``Commands`` panel. That
    fallback is rendered and pinned by the golden; registry panels stay
    strict.
    """
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    panels = _command_panels_only(_extract_panels(_strip_ansi(result.stdout)))
    all_rendered_commands: set[str] = set()
    for panel, cmds in panels:
        if panel == "Commands":
            continue
        all_rendered_commands.update(cmds)
    missing = all_rendered_commands - set(COMMAND_PANELS.keys())
    assert not missing, f"commands missing from COMMAND_PANELS: {sorted(missing)}"


def test_help_panel_golden_matches() -> None:
    """The structured panel layout matches the committed golden snapshot.

    The golden encodes panel order + per-panel command list — independent
    of terminal width / Rich box-art rendering. Refresh with::

        EAWF_REFRESH_GOLDEN=1 uv run pytest tests/integration/test_cli_help_groups.py

    when an intentional addition / removal lands.
    """
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    panels = _command_panels_only(_extract_panels(_strip_ansi(result.stdout)))
    rendered = "\n".join(f"{name}: {', '.join(commands)}" for name, commands in panels)
    rendered = rendered + "\n"

    import os

    if os.environ.get("EAWF_REFRESH_GOLDEN"):
        _GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        _GOLDEN_PATH.write_text(rendered, encoding="utf-8")
        pytest.skip(f"refreshed golden at {_GOLDEN_PATH}")

    assert _GOLDEN_PATH.exists(), (
        f"golden not found at {_GOLDEN_PATH}; run with EAWF_REFRESH_GOLDEN=1 to write it"
    )
    expected = _GOLDEN_PATH.read_text(encoding="utf-8")
    assert rendered == expected, (
        "help panel layout drifted from golden.\n"
        f"--- expected ---\n{expected}\n--- got ---\n{rendered}"
    )
