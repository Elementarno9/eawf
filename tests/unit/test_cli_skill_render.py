"""Unit tests for ``eawf skill render`` CLI surface (P10-W01).

Pins:

- ``skill render <name> --format=skill-md`` (default) emits bytes
  byte-equal to the SKILL.md
  :mod:`eawf.runtime.runtimes.claude.plugin_install` writes for the same skill.
  Both code paths funnel through
  :func:`eawf.surfaces.render.skills.render_skill_md_from_spec`, so the byte-
  equality is a shared-fixture assertion against the canonical render.
- ``skill render <name> --format=json`` emits a JSON object with the
  same four keys (``name``, ``status``, ``body_schema``,
  ``description``) one row of :func:`_list_payload` emits, plus a
  ``body`` field carrying the SKILL.md string.
- Bare and slashed skill names both resolve via
  :func:`_resolve_skill_name`.
- Unknown skill name → :class:`InvalidInput` (exit 3, mirrors the
  pre-existing ``skill run`` error path).
- Unknown ``--format`` → :class:`InvalidInput` (exit 3).
"""

from __future__ import annotations

import json
from typing import cast

import click
import pytest
import typer
from typer.testing import CliRunner

from eawf.runtime.runtimes.claude.plugin_install import _render_skill
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands.skill import _list_payload
from eawf.surfaces.render.envelope import CANONICAL_SKILL_NAMES
from eawf.surfaces.render.skills import SKILL_REGISTRY, render_skill_md_from_spec

# ``eawf skill render`` is the operator surface over the execution-backed
# skills (``CANONICAL_SKILL_NAMES``). The render ``SKILL_REGISTRY`` is a
# superset that also carries model-only code-quality playbooks; those have
# no execution body and are intentionally not operator-renderable.
_RENDERABLE_SPECS = [s for s in SKILL_REGISTRY if f"/{s.skill_name}" in set(CANONICAL_SKILL_NAMES)]
_MODEL_ONLY_SPECS = [
    s for s in SKILL_REGISTRY if f"/{s.skill_name}" not in set(CANONICAL_SKILL_NAMES)
]


@pytest.fixture
def cli_runner() -> CliRunner:
    """Fresh CliRunner per test so stdout buffers don't bleed."""
    return CliRunner()


def test_render_cmd_skill_md_matches_plugin_install_bytes(cli_runner: CliRunner) -> None:
    """Default ``--format=skill-md`` output is byte-equal to the bytes
    :func:`eawf.runtime.runtimes.claude.plugin_install._render_skill` emits for
    the same registry entry. The shared
    :func:`render_skill_md_from_spec` helper guarantees this; the test
    pins it so a future refactor that diverges the two code paths fails
    loudly.
    """
    spec = SKILL_REGISTRY[0]  # research
    expected = _render_skill(spec)

    result = cli_runner.invoke(app, ["skill", "render", spec.skill_name])
    assert result.exit_code == 0, result.stdout
    # CliRunner captures stdout verbatim — including the lack of trailing
    # newline (typer.echo was invoked with nl=False to preserve byte
    # equality with the on-disk SKILL.md).
    assert result.stdout == expected


def test_render_cmd_skill_md_uses_shared_render_helper(cli_runner: CliRunner) -> None:
    """Cross-check: the CLI emits the same bytes
    :func:`render_skill_md_from_spec` emits — the two helpers are the
    canonical render path and must not diverge.
    """
    spec = SKILL_REGISTRY[3]  # ship
    expected = render_skill_md_from_spec(spec)

    result = cli_runner.invoke(app, ["skill", "render", spec.skill_name])
    assert result.exit_code == 0, result.stdout
    assert result.stdout == expected


def test_render_cmd_skill_md_accepts_slashed_form(cli_runner: CliRunner) -> None:
    """``--format=skill-md`` resolves a leading-slash skill name via
    :func:`_resolve_skill_name` exactly as the bare form does.
    """
    spec_research = next(s for s in SKILL_REGISTRY if s.skill_name == "research")
    expected = render_skill_md_from_spec(spec_research)

    result = cli_runner.invoke(app, ["skill", "render", "/research"])
    assert result.exit_code == 0, result.stdout
    assert result.stdout == expected


def test_render_cmd_skill_md_accepts_bare_form(cli_runner: CliRunner) -> None:
    """Bare form (no leading slash) resolves identically."""
    spec_research = next(s for s in SKILL_REGISTRY if s.skill_name == "research")
    expected = render_skill_md_from_spec(spec_research)

    result = cli_runner.invoke(app, ["skill", "render", "research"])
    assert result.exit_code == 0, result.stdout
    assert result.stdout == expected


def test_render_cmd_json_keys_match_list_payload(cli_runner: CliRunner) -> None:
    """``--format=json`` carries the same four metadata keys one row of
    ``skill list --json`` carries, plus a ``body`` field with the
    canonical SKILL.md string.
    """
    result = cli_runner.invoke(app, ["skill", "render", "/research", "--format", "json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    # Same shape as one entry of _list_payload(), plus body.
    list_row = next(row for row in _list_payload()["skills"] if row["name"] == "/research")
    metadata_keys = {"name", "status", "body_schema", "description"}
    assert metadata_keys.issubset(payload.keys())
    for key in metadata_keys:
        assert payload[key] == list_row[key]
    # Body field is the canonical SKILL.md string.
    spec_research = next(s for s in SKILL_REGISTRY if s.skill_name == "research")
    assert payload["body"] == render_skill_md_from_spec(spec_research)


def test_render_cmd_json_keys_are_exactly_the_documented_set(cli_runner: CliRunner) -> None:
    """The JSON payload exposes exactly five top-level keys:
    ``name``/``status``/``body_schema``/``description``/``body``. Pin
    the set so a future field addition fails the test and the surface
    documentation gets updated alongside.
    """
    result = cli_runner.invoke(app, ["skill", "render", "/audit", "--format", "json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert set(payload.keys()) == {"name", "status", "body_schema", "description", "body"}


def test_render_cmd_unknown_skill_returns_invalid_input(cli_runner: CliRunner) -> None:
    """Unknown skill name maps to :class:`InvalidInput` (exit 3),
    mirroring the :func:`_resolve_skill_name` error path that
    ``skill run`` uses.
    """
    result = cli_runner.invoke(app, ["skill", "render", "/not-a-skill"])
    assert result.exit_code == 1, result.stdout
    assert "unknown skill" in result.stdout


def test_render_cmd_unknown_format_returns_invalid_input(cli_runner: CliRunner) -> None:
    """``--format=html`` (or any value outside ``{skill-md, json}``) is
    rejected with :class:`InvalidInput` (exit 3) and the message lists
    the canonical alternatives.
    """
    result = cli_runner.invoke(app, ["skill", "render", "/research", "--format", "html"])
    assert result.exit_code == 1, result.stdout
    assert "unknown --format" in result.stdout
    assert "'json'" in result.stdout
    assert "'skill-md'" in result.stdout


def test_render_cmd_every_renderable_entry_renders(cli_runner: CliRunner) -> None:
    """Boundary case: every execution-backed skill renders cleanly via the
    bare-name form. Catches a future registry addition whose body trips
    ``StrictUndefined`` in the Jinja2 template.
    """
    for spec in _RENDERABLE_SPECS:
        result = cli_runner.invoke(app, ["skill", "render", spec.skill_name])
        assert result.exit_code == 0, f"skill {spec.skill_name!r} failed: {result.stdout}"
        # Sanity: frontmatter line carries the bare skill name.
        assert f"name: {spec.skill_name}\n" in result.stdout


def test_render_cmd_json_for_every_renderable_entry(cli_runner: CliRunner) -> None:
    """Boundary case (JSON branch): every execution-backed skill yields a
    well-formed JSON object via the slashed form.
    """
    for spec in _RENDERABLE_SPECS:
        slashed = f"/{spec.skill_name}"
        result = cli_runner.invoke(app, ["skill", "render", slashed, "--format", "json"])
        assert result.exit_code == 0, f"skill {slashed!r} failed: {result.stdout}"
        payload = cast(dict[str, object], json.loads(result.stdout))
        assert payload["name"] == slashed
        assert isinstance(payload["body"], str)
        assert payload["body"].startswith("---\n")


def test_render_cmd_rejects_model_only_skills(cli_runner: CliRunner) -> None:
    """Model-only code-quality playbooks are not operator-renderable: the
    ``skill render`` CLI rejects them with :class:`InvalidInput` (exit 1)
    even though they live in the render ``SKILL_REGISTRY``. They are
    reachable only as on-disk SKILL.md files the model reads, not via the
    operator CLI.
    """
    assert _MODEL_ONLY_SPECS, "expected at least one model-only skill in the registry"
    for spec in _MODEL_ONLY_SPECS:
        result = cli_runner.invoke(app, ["skill", "render", spec.skill_name])
        assert result.exit_code == 1, f"{spec.skill_name!r} unexpectedly rendered: {result.stdout}"
        assert "unknown skill" in result.stdout


def test_render_cmd_help_documents_format_alternatives(cli_runner: CliRunner) -> None:
    """The ``--format`` option's docstring lists both ``skill-md`` and ``json``.

    Asserted against the introspected Click option (not the rendered
    ``--help`` text) because Typer's Rich-driven help formatter degrades
    to an empty panel under :class:`CliRunner` (non-TTY), making any
    stdout-substring check brittle across rich versions and CI runners.
    """
    # Resolve the bound Click command via Typer's ``get_command`` shim;
    # the test stays a docstring check rather than an end-to-end help
    # render so Rich's non-TTY downgrade can't drop the option panel.
    click_app = cast(click.Group, typer.main.get_command(app))
    skill_group = cast(click.Group, click_app.commands["skill"])
    render = skill_group.commands["render"]
    fmt_param = next(cast(click.Option, p) for p in render.params if "--format" in (p.opts or []))
    assert "skill-md" in (fmt_param.help or "")
    assert "json" in (fmt_param.help or "")
