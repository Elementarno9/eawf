"""Integration tests for ``eawf render-output``.

Drives the Typer dispatcher with stdin via :class:`typer.testing.CliRunner`
and asserts the JSON ⇄ markdown round-trip is byte-stable plus the
``--strict`` exit-4 contract.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from eawf.cli.app import app
from eawf.cli.exit_codes import VALIDATION_FAILED

runner = CliRunner()

# A minimal but representative envelope used as the round-trip seed.
_ENVELOPE_JSON: str = json.dumps(
    {
        "header": {
            "skill": "research-spike",
            "scope": "PROJECT",
            "session": "s1",
            "status": "ok",
        },
        "body": "## Body\n\nFirst para.\n\nSecond para.\n",
        "footer": {
            "artifacts": ["doc-1", "doc-2"],
            "store_records": [],
            "mutations": ["m-1"],
            "evidence": {"hypothesis": "H01-01"},
            "next_actions": ["audit"],
            "warnings": [],
        },
    },
    sort_keys=True,
)


def test_cli_render_output_md_to_json_to_md_byte_stable() -> None:
    """Pipe JSON → markdown → JSON → markdown; second markdown identical to first."""
    md1 = runner.invoke(app, ["render-output", "--format", "markdown"], input=_ENVELOPE_JSON)
    assert md1.exit_code == 0, md1.output
    json2 = runner.invoke(app, ["render-output", "--format", "json"], input=md1.stdout)
    assert json2.exit_code == 0, json2.output
    md3 = runner.invoke(app, ["render-output", "--format", "markdown"], input=json2.stdout)
    assert md3.exit_code == 0, md3.output
    assert md1.stdout == md3.stdout


def test_cli_render_output_strict_rejects_malformed_json() -> None:
    """``--format markdown --strict`` with non-JSON stdin → exit 4."""
    result = runner.invoke(
        app,
        ["render-output", "--format", "markdown", "--strict"],
        input="this is not json {{{",
    )
    assert result.exit_code == VALIDATION_FAILED


def test_cli_render_output_strict_rejects_malformed_markdown() -> None:
    """``--format json --strict`` with non-markdown stdin → exit 4."""
    result = runner.invoke(
        app,
        ["render-output", "--format", "json", "--strict"],
        input="just some text without frontmatter\n",
    )
    assert result.exit_code == VALIDATION_FAILED


def test_cli_render_output_default_format_is_markdown() -> None:
    """No ``--format`` argument falls back to markdown emission."""
    result = runner.invoke(app, ["render-output"], input=_ENVELOPE_JSON)
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("---\n")
    assert "<!-- eawf:footer" in result.stdout


def test_cli_render_output_invalid_format_rejected() -> None:
    """An unknown ``--format`` value surfaces InvalidInput (exit 3)."""
    result = runner.invoke(app, ["render-output", "--format", "yaml"], input=_ENVELOPE_JSON)
    assert result.exit_code != 0


def test_cli_render_output_strict_rejects_envelope_missing_keys() -> None:
    """JSON parses but Pydantic rejects on missing required fields."""
    result = runner.invoke(
        app,
        ["render-output", "--format", "markdown", "--strict"],
        input=json.dumps({"header": {}, "body": "x"}),  # missing footer
    )
    assert result.exit_code == VALIDATION_FAILED
