"""Unit tests for ``render_plugin_manifest`` + ``PluginManifest``.

Two test surfaces share this file:

* ``render_plugin_manifest`` (Claude marketplace ``package.json``
  renderer, Phase 6 W05) — scrubs author email, omits absent URL
  fields, byte-stable across calls.
* ``PluginManifest(BaseModel, extra="forbid")`` (C07a §5.7, XB19) —
  closed canonical schema fed into the three per-runtime
  renderers. Rejects extra keys; required-field assertions.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from eawf.runtimes.claude.plugin_package import render_plugin_manifest
from eawf.runtimes.manifest import (
    PluginContributes,
    PluginInfo,
    PluginManaged,
    PluginManifest,
)

pytestmark = pytest.mark.unit


def test_manifest_no_email_when_pyproject_has_email() -> None:
    """Author email is consumed by the helper but never appears in output."""
    rendered = render_plugin_manifest(
        version="0.1.0.dev0",
        author_name="X",
        author_email="x@example.com",  # MUST NOT appear in output.
        homepage="https://example.com",
        repository="https://example.com/repo",
    )
    assert "@" not in rendered
    parsed = json.loads(rendered)
    assert "email" not in parsed["author"]


def test_manifest_omits_homepage_when_absent() -> None:
    """``homepage=None`` and ``repository=None`` keep the keys out entirely."""
    rendered = render_plugin_manifest(
        version="0.1.0.dev0",
        author_name="X",
        author_email=None,
        homepage=None,
        repository=None,
    )
    parsed = json.loads(rendered)
    assert "homepage" not in parsed
    assert "repository" not in parsed


def test_manifest_required_fields() -> None:
    """The canonical keys are always present, even with minimal inputs."""
    rendered = render_plugin_manifest(
        version="0.1.0.dev0",
        author_name="X",
        author_email=None,
        homepage=None,
        repository=None,
    )
    parsed = json.loads(rendered)
    for key in ("name", "version", "license", "keywords"):
        assert key in parsed
    # ``skills`` and ``agents`` are intentionally omitted so Claude Code
    # falls back to its default discovery (``./skills/`` and ``./agents/``).
    # Emitting ``"agents": "./agents"`` was rejected by the CC manifest
    # schema with ``agents: Invalid input``.
    assert "skills" not in parsed
    assert "agents" not in parsed
    assert parsed["name"] == "eawf"
    assert parsed["license"] == "MIT"


def test_manifest_keywords_canonical_list() -> None:
    """Keywords are exactly the canonical list, not derived from inputs."""
    rendered = render_plugin_manifest(
        version="0.1.0.dev0",
        author_name="X",
        author_email=None,
        homepage=None,
        repository=None,
    )
    parsed = json.loads(rendered)
    assert parsed["keywords"] == [
        "workflow",
        "agents",
        "state-machine",
        "tdd",
        "research",
    ]


def test_manifest_byte_stable_across_calls() -> None:
    """Two renders with identical inputs produce identical bytes."""
    kwargs = {
        "version": "0.1.0.dev0",
        "author_name": "X",
        "author_email": None,
        "homepage": "https://example.com",
        "repository": "https://example.com/repo",
    }
    a = render_plugin_manifest(**kwargs)
    b = render_plugin_manifest(**kwargs)
    assert a == b
    # Sanity: trailing newline + sorted-keys canonical form.
    assert a.endswith("\n")
    parsed = json.loads(a)
    rebuilt = json.dumps(parsed, sort_keys=True, indent=2) + "\n"
    assert a == rebuilt


def test_manifest_emits_homepage_when_present() -> None:
    """Provided ``homepage`` ends up in the output verbatim."""
    rendered = render_plugin_manifest(
        version="0.1.0.dev0",
        author_name="X",
        author_email=None,
        homepage="https://example.com/eawf",
        repository=None,
    )
    parsed = json.loads(rendered)
    assert parsed["homepage"] == "https://example.com/eawf"
    assert "repository" not in parsed


def test_manifest_emits_repository_when_present() -> None:
    """Provided ``repository`` ends up in the output verbatim."""
    rendered = render_plugin_manifest(
        version="0.1.0.dev0",
        author_name="X",
        author_email=None,
        homepage=None,
        repository="https://example.com/repo",
    )
    parsed = json.loads(rendered)
    assert parsed["repository"] == "https://example.com/repo"
    assert "homepage" not in parsed


def test_manifest_handles_special_chars_in_author_name() -> None:
    """Quotes / backslashes in author_name must not corrupt JSON."""
    rendered = render_plugin_manifest(
        version="0.1.0.dev0",
        author_name='X "quoted" \\Y',
        author_email=None,
        homepage=None,
        repository=None,
    )
    parsed = json.loads(rendered)  # must not raise
    assert parsed["author"]["name"] == 'X "quoted" \\Y'


def test_manifest_handles_special_chars_in_url_fields() -> None:
    """Backslashes / quotes in homepage / repository round-trip cleanly."""
    rendered = render_plugin_manifest(
        version="0.1.0.dev0",
        author_name="X",
        author_email=None,
        homepage='https://example.com/"odd"',
        repository="https://example.com/path\\with\\slashes",
    )
    parsed = json.loads(rendered)
    assert parsed["homepage"] == 'https://example.com/"odd"'
    assert parsed["repository"] == "https://example.com/path\\with\\slashes"


def test_manifest_drops_hardcoded_skill_agent_counts() -> None:
    """Description must not lie when the skill / agent registries grow."""
    rendered = render_plugin_manifest(
        version="0.1.0.dev0",
        author_name="X",
        author_email=None,
        homepage=None,
        repository=None,
    )
    parsed = json.loads(rendered)
    description = parsed["description"]
    # No literal counts in the description; the README and docs carry
    # the canonical lists when callers want them enumerated.
    assert "10 skills" not in description
    assert "8 agents" not in description


# ---------------------------------------------------------------------------
# PluginManifest(BaseModel) — closed-schema validation (C07a §5.7, XB19)
# ---------------------------------------------------------------------------


def _valid_manifest_dict() -> dict[str, object]:
    """Return one well-formed dict that round-trips through PluginManifest."""
    return {
        "schema_version": "1.0",
        "plugin": {
            "name": "eawf",
            "version": "1.0",
            "description": "Eä Workflow plugin — agent-driven development skills.",
            "runtime": "claude-code",
            "generator": "eawf-plugin-claude",
        },
        "contributes": {
            "skills": ["research", "prep"],
            "agents": ["executor"],
            "hooks": {"session_level": ["session_start"]},
        },
        "managed": {
            "body_hash_field": "__eawf_managed.body_hash",
            "timestamp_field": "__eawf_managed.timestamp",
            "source_files": ["AGENTS.md"],
        },
    }


def test_plugin_manifest_round_trip() -> None:
    """A well-formed dict validates and round-trips losslessly."""
    raw = _valid_manifest_dict()
    manifest = PluginManifest.model_validate(raw)
    assert manifest.schema_version == "1.0"
    assert manifest.plugin.runtime == "claude-code"
    assert manifest.contributes.skills == ["research", "prep"]
    dumped = manifest.model_dump()
    # round trip preserves every field.
    assert dumped["plugin"]["name"] == "eawf"


def test_plugin_manifest_rejects_extra_top_level_key() -> None:
    """``extra='forbid'`` per XB19 — unknown top-level key fails fast."""
    raw = _valid_manifest_dict()
    raw["mystery"] = "boom"
    with pytest.raises(ValidationError):
        PluginManifest.model_validate(raw)


def test_plugin_manifest_rejects_extra_nested_key() -> None:
    """Nested models share ``extra='forbid'`` — unknown keys reject."""
    raw = _valid_manifest_dict()
    raw["plugin"]["nickname"] = "boom"  # type: ignore[index]
    with pytest.raises(ValidationError):
        PluginManifest.model_validate(raw)


def test_plugin_manifest_rejects_missing_required_field() -> None:
    """Missing required fields fail validation."""
    raw = _valid_manifest_dict()
    del raw["plugin"]
    with pytest.raises(ValidationError):
        PluginManifest.model_validate(raw)


def test_plugin_manifest_rejects_unknown_runtime() -> None:
    """``plugin.runtime`` is closed to the three canonical ids."""
    raw = _valid_manifest_dict()
    raw["plugin"]["runtime"] = "aider"  # type: ignore[index]
    with pytest.raises(ValidationError):
        PluginManifest.model_validate(raw)


def test_plugin_manifest_rejects_unknown_schema_version() -> None:
    """``schema_version`` is pinned to ``"1.0"`` per Q5 / BOT-03."""
    raw = _valid_manifest_dict()
    raw["schema_version"] = "2.0"
    with pytest.raises(ValidationError):
        PluginManifest.model_validate(raw)


def test_plugin_contributes_defaults_empty_lists() -> None:
    """Optional list/dict fields default to empty for partial runtimes."""
    contributes = PluginContributes()
    assert contributes.skills == []
    assert contributes.agents == []
    assert contributes.hooks == {}


def test_plugin_managed_requires_source_files() -> None:
    """The managed namespace MUST name at least the body_hash / timestamp fields."""
    with pytest.raises(ValidationError):
        PluginManaged(
            body_hash_field="__eawf_managed.body_hash",
            timestamp_field="__eawf_managed.timestamp",
        )  # type: ignore[call-arg]


def test_plugin_info_rejects_blank_runtime() -> None:
    """``runtime`` does not accept the empty string (closed Literal)."""
    with pytest.raises(ValidationError):
        PluginInfo(
            name="eawf",
            version="1.0",
            description="d",
            runtime="",  # type: ignore[arg-type]
            generator="g",
        )
