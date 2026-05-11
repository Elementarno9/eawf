"""Unit tests for the layered profile discovery + mtime cache (P14-W04 / D18)."""

from __future__ import annotations

import os
import textwrap
import time
from pathlib import Path

import pytest

from eawf.cli.errors import InvalidInput, ValidationFailed
from eawf.profiles import discovery


@pytest.fixture(autouse=True)
def _clear_cache():
    discovery._clear_cache_for_tests()
    yield
    discovery._clear_cache_for_tests()


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``~`` to *tmp_path/home* so user-overlay tests are isolated."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def _write_profile(path: Path, name: str, description: str = "from-overlay") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = textwrap.dedent(
        f"""\
        name: {name}
        description: {description}
        version: "1.0"
        """
    )
    path.write_text(body)


def test_builtin_profile_loads_when_no_overlays(fake_home: Path) -> None:
    body = discovery.load_profile_with_discovery("core")
    assert body.name == "core"
    loc = discovery.discover_profile("core")
    assert loc.source == "builtin"


def test_user_overlay_overrides_builtin(fake_home: Path) -> None:
    user_path = discovery.user_profiles_dir() / "core.yaml"
    _write_profile(user_path, "core", description="user-overlay")
    loc = discovery.discover_profile("core")
    assert loc.source == "user"
    body = discovery.load_profile_with_discovery("core")
    assert body.description == "user-overlay"


def test_workspace_overlay_overrides_user(fake_home: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    user_path = discovery.user_profiles_dir() / "core.yaml"
    _write_profile(user_path, "core", description="user-overlay")
    ws_path = discovery.workspace_profiles_dir(workspace) / "core.yaml"
    _write_profile(ws_path, "core", description="workspace-overlay")
    loc = discovery.discover_profile("core", workspace=workspace)
    assert loc.source == "workspace"
    body = discovery.load_profile_with_discovery("core", workspace=workspace)
    assert body.description == "workspace-overlay"


def test_user_only_profile_loads(fake_home: Path) -> None:
    user_path = discovery.user_profiles_dir() / "custom.yaml"
    _write_profile(user_path, "custom")
    body = discovery.load_profile_with_discovery("custom")
    assert body.name == "custom"
    listing = discovery.list_profiles_all()
    assert "custom" in listing


def test_unknown_id_raises_invalid_input(fake_home: Path) -> None:
    with pytest.raises(InvalidInput):
        discovery.load_profile_with_discovery("doesnotexist")


def test_malformed_yaml_raises_validation_failed(fake_home: Path) -> None:
    user_path = discovery.user_profiles_dir() / "broken.yaml"
    user_path.parent.mkdir(parents=True, exist_ok=True)
    user_path.write_text("name: broken\nversion: [unterminated")
    with pytest.raises(ValidationFailed, match="malformed YAML"):
        discovery.load_profile_with_discovery("broken")


def test_schema_violation_raises_validation_failed(fake_home: Path) -> None:
    user_path = discovery.user_profiles_dir() / "bad-schema.yaml"
    user_path.parent.mkdir(parents=True, exist_ok=True)
    user_path.write_text("name: bad-schema\nbogus_extra_field: true\n")
    with pytest.raises(ValidationFailed, match="schema rejected"):
        discovery.load_profile_with_discovery("bad-schema")


def test_mtime_bump_invalidates_cache(fake_home: Path) -> None:
    user_path = discovery.user_profiles_dir() / "muta.yaml"
    _write_profile(user_path, "muta", description="v1")
    first = discovery.load_profile_with_discovery("muta")
    assert first.description == "v1"
    # Bump mtime then rewrite content; cache key should diverge.
    time.sleep(0.01)
    future = time.time() + 1.0
    os.utime(user_path, (future, future))
    _write_profile(user_path, "muta", description="v2")
    second = discovery.load_profile_with_discovery("muta")
    assert second.description == "v2"


def test_same_mtime_returns_cached_object(fake_home: Path) -> None:
    user_path = discovery.user_profiles_dir() / "stable.yaml"
    _write_profile(user_path, "stable", description="v1")
    a = discovery.load_profile_with_discovery("stable")
    b = discovery.load_profile_with_discovery("stable")
    assert a is b


def test_list_profiles_union(fake_home: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_profile(discovery.user_profiles_dir() / "u-only.yaml", "u-only")
    _write_profile(discovery.workspace_profiles_dir(workspace) / "w-only.yaml", "w-only")
    listing = discovery.list_profiles_all(workspace=workspace)
    assert "u-only" in listing
    assert "w-only" in listing
    assert "core" in listing
