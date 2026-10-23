"""Unit tests for :mod:`eawf.kernel.config.profile`.

The contracts under test:

- ``enable_profile(<id>, layer=<L>, ...)`` writes the profile id into
  ``profiles.enabled`` of the layer's YAML file.
- The response names the profile's required state keys per
  :data:`KNOWN_PROFILES` (e.g. ``research`` → ``hypotheses``, ``audits``).
- Re-enabling an already-enabled profile is a no-op (file unchanged).
- Unknown profile id → :class:`InvalidInput`.
- Unknown / read-only layer → :class:`InvalidInput`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from eawf.kernel.config.profile import KNOWN_PROFILES, enable_profile
from eawf.surfaces.cli.errors import UserError

# --- Layer-file write contract ---------------------------------------------


def test_enable_writes_profile_to_layer_file(tmp_path: Path) -> None:
    layer_file = tmp_path / "config.yaml"
    result = enable_profile("python", layer="repo", layer_file_path=layer_file)
    assert layer_file.exists()
    body = yaml.safe_load(layer_file.read_text(encoding="utf-8"))
    assert "python" in body["profiles"]["enabled"]
    assert result["profile"] == "python"
    assert result["layer"] == "repo"
    assert result["already_enabled"] is False


def test_enable_creates_intermediate_dirs(tmp_path: Path) -> None:
    layer_file = tmp_path / ".ea" / "local" / "config.yaml"
    enable_profile("python", layer="local", layer_file_path=layer_file)
    assert layer_file.exists()


def test_enable_appends_to_existing_profiles(tmp_path: Path) -> None:
    layer_file = tmp_path / "config.yaml"
    layer_file.write_text("profiles:\n  enabled: [core]\n", encoding="utf-8")
    result = enable_profile("python", layer="repo", layer_file_path=layer_file)
    body = yaml.safe_load(layer_file.read_text(encoding="utf-8"))
    assert body["profiles"]["enabled"] == ["core", "python"]
    assert result["already_enabled"] is False


def test_enable_idempotent_for_already_enabled(tmp_path: Path) -> None:
    layer_file = tmp_path / "config.yaml"
    layer_file.write_text("profiles:\n  enabled: [core, python]\n", encoding="utf-8")
    before = layer_file.read_bytes()
    result = enable_profile("python", layer="repo", layer_file_path=layer_file)
    after = layer_file.read_bytes()
    assert before == after  # file not rewritten
    assert result["already_enabled"] is True


def test_enable_preserves_other_top_level_keys(tmp_path: Path) -> None:
    layer_file = tmp_path / "config.yaml"
    layer_file.write_text(
        "estimation:\n  eu_minutes: 60\nplanning:\n  approval: auto\n",
        encoding="utf-8",
    )
    enable_profile("research", layer="repo", layer_file_path=layer_file)
    body = yaml.safe_load(layer_file.read_text(encoding="utf-8"))
    assert body["estimation"]["eu_minutes"] == 60
    assert body["planning"]["approval"] == "auto"
    assert body["profiles"]["enabled"] == ["research"]


def test_python_profile_has_no_required_state_keys(tmp_path: Path) -> None:
    layer_file = tmp_path / "config.yaml"
    result = enable_profile("python", layer="repo", layer_file_path=layer_file)
    assert result["state_keys_required"] == []


# --- Error paths ------------------------------------------------------------


def test_unknown_profile_raises_invalid_input(tmp_path: Path) -> None:
    layer_file = tmp_path / "config.yaml"
    with pytest.raises(UserError) as excinfo:
        enable_profile("not-a-profile", layer="repo", layer_file_path=layer_file)
    assert "unknown profile" in str(excinfo.value)


def test_unknown_layer_raises_invalid_input(tmp_path: Path) -> None:
    layer_file = tmp_path / "config.yaml"
    with pytest.raises(UserError):
        enable_profile("python", layer="garbage", layer_file_path=layer_file)


def test_built_in_layer_is_read_only(tmp_path: Path) -> None:
    layer_file = tmp_path / "config.yaml"
    with pytest.raises(UserError) as excinfo:
        enable_profile("python", layer="built-in", layer_file_path=layer_file)
    assert "read-only" in str(excinfo.value)


def test_env_layer_is_not_writable(tmp_path: Path) -> None:
    """``env`` and ``cli`` are runtime-only; they cannot host a saved profile."""
    layer_file = tmp_path / "config.yaml"
    with pytest.raises(UserError):
        enable_profile("python", layer="env", layer_file_path=layer_file)


# --- Registry contract -----------------------------------------------------


def test_known_profiles_includes_v0_1_set() -> None:
    """All v0.1 profile ids per docs/architecture/profiles.md are registered."""
    expected = {
        "core",
        "python",
        "research",
        "docs",
        "apps",
        "infra",
        "ml",
        "quant",
        "re",
        "game",
        "robotics",
    }
    assert expected.issubset(KNOWN_PROFILES.keys())


def test_research_profile_requires_hypotheses_and_audits() -> None:
    assert set(KNOWN_PROFILES["research"]) == {"hypotheses", "audits"}


def test_enable_reports_the_required_state_keys_and_writes_no_state(tmp_path: Path) -> None:
    """Enable names the profile's required keys; no state document is touched."""
    layer_file = tmp_path / "config.yaml"
    result = enable_profile("research", layer="repo", layer_file_path=layer_file)
    assert set(result["state_keys_required"]) == {"hypotheses", "audits"}
    assert "state_keys_materialised" not in result
    assert not (tmp_path / "state.json").exists()
