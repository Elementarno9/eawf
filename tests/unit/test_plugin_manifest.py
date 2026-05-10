"""Unit tests for ``render_plugin_manifest``.

Per Phase 6 W05 spec: the manifest must scrub author email even when
present in pyproject, must omit homepage/repository when undefined, and
must always carry the canonical required keys.
"""

from __future__ import annotations

import json

import pytest

from eawf.runtimes.claude.plugin_package import render_plugin_manifest

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
    for key in ("name", "version", "license", "skills", "agents", "keywords"):
        assert key in parsed
    assert parsed["skills"] == "./skills"
    assert parsed["agents"] == "./agents"
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
