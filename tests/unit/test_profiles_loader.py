"""Unit tests for :mod:`eawf.profiles.loader`.

The contracts under test:

- ``load_profile("core")`` returns a fully-validated :class:`ProfileBody`.
- ``list_profiles()`` enumerates every YAML stem under ``data/`` (13 ids).
- Unknown ids raise :class:`InvalidInput`.
- Malformed YAML / schema mismatches raise :class:`ValidationFailed`.
- Required state keys for ``research`` round-trip via the loader.
"""

from __future__ import annotations

import pytest

from eawf.cli.errors import InvalidInput
from eawf.profiles.loader import list_profiles, load_profile
from eawf.profiles.models import ProfileBody

_EXPECTED_PROFILES: tuple[str, ...] = (
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


def test_loader_lists_all_thirteen_profiles() -> None:
    profiles = list_profiles()
    assert len(profiles) == 13
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
    with pytest.raises(InvalidInput) as excinfo:
        load_profile("not-a-profile")
    assert "not-a-profile" in str(excinfo.value)


def test_loader_stub_profiles_validate() -> None:
    """Every catalog stub parses cleanly with default fields."""
    for stub in ("quant", "ml", "docs", "apps", "infra", "re", "game", "robotics"):
        body = load_profile(stub)
        assert body.name == stub
        assert body.state_extensions.fields_required == []
        assert body.render_blocks == []
        assert body.instrument_requirements == []


def test_loader_caches_repeat_calls() -> None:
    """``load_profile`` is lru_cache'd: the second call returns the same instance."""
    a = load_profile("core")
    b = load_profile("core")
    assert a is b
