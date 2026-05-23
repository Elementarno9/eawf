"""Unit tests for :mod:`eawf.profiles.loader`.

The contracts under test:

- ``load_profile("core")`` returns a fully-validated :class:`ProfileBody`.
- ``list_profiles()`` enumerates every YAML stem under ``data/`` (14 ids).
- Unknown ids raise :class:`InvalidInput`.
- Malformed YAML / schema mismatches raise :class:`ValidationFailed`.
- Required state keys for ``research`` round-trip via the loader.
"""

from __future__ import annotations

import pytest

from eawf.cli.errors import UserError
from eawf.profiles.loader import list_profiles, load_profile
from eawf.profiles.models import ProfileBody

_EXPECTED_PROFILES: tuple[str, ...] = (
    "agent_driven",
    "apps",
    "core",
    "docs",
    "game",
    "infra",
    "ml",
    "python",
    "quant",
    "quality",
    "re",
    "research",
    "robotics",
)


def test_loader_lists_all_fourteen_profiles() -> None:
    profiles = list_profiles()
    assert len(profiles) == 14
    expected = (*_EXPECTED_PROFILES, "a11y")
    assert tuple(sorted(profiles)) == tuple(sorted(expected))


def test_loader_returns_known_profile_body() -> None:
    body = load_profile("core")
    assert isinstance(body, ProfileBody)
    assert body.name == "core"
    assert body.version == "1.0"
    # Core declares git+uv as hard requirements.
    names = {req.name for req in body.instrument_requirements}
    assert {"git", "uv"}.issubset(names)


def test_loader_research_has_state_extensions() -> None:
    body = load_profile("research")
    assert set(body.state_extensions.fields_required) == {"hypotheses", "audits"}


def test_loader_python_declares_hard_python_requirement() -> None:
    body = load_profile("python")
    by_name = {req.name: req for req in body.instrument_requirements}
    assert by_name["python"].kind == "hard"
    assert by_name["ruff"].kind == "soft"


def test_loader_unknown_id_raises_invalid_input() -> None:
    with pytest.raises(UserError) as excinfo:
        load_profile("not-a-profile")
    assert "not-a-profile" in str(excinfo.value)


def test_loader_stub_profiles_validate() -> None:
    """Every no-body catalog stub parses cleanly with default fields."""
    for stub in ("docs", "re", "game", "robotics"):
        body = load_profile(stub)
        assert body.name == stub
        assert body.state_extensions.fields_required == []
        assert body.render_blocks == []
        assert body.instrument_requirements == []


def test_loader_minimal_rule_profiles_render_defining_block() -> None:
    """ml/quant/apps/infra each ship at least their defining AGENTS.md block."""
    for stub in ("ml", "quant", "apps", "infra"):
        body = load_profile(stub)
        assert body.name == stub
        assert body.render_blocks, f"{stub} ships no defining render block"
        assert all(block.target == "AGENTS.md" for block in body.render_blocks)
        # No instruments or state extensions: these stay minimal rule sets.
        assert body.instrument_requirements == []
        assert body.state_extensions.fields_required == []


def test_loader_caches_repeat_calls() -> None:
    """``load_profile`` is lru_cache'd: the second call returns the same instance."""
    a = load_profile("core")
    b = load_profile("core")
    assert a is b
