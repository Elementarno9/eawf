"""``telemetry.enabled`` gates collection at the producer, not only the display.

The session store's ``session_closed`` event exists only for the telemetry
projection, so with collection off a terminal session must leave no such row
behind while its human-timeline ``session.close`` row is still written.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from eawf.kernel.config import layered
from eawf.observability.telemetry.gate import telemetry_enabled
from eawf.surfaces.cli.errors import ValidationError


@pytest.fixture(autouse=True)
def _isolate_global_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the global config layer at a tmp file and clear the telemetry env leaf."""
    fake_global = tmp_path / "global-config.yaml"
    monkeypatch.setattr(layered, "global_config_path", lambda: fake_global)
    monkeypatch.delenv("EAWF_TELEMETRY__ENABLED", raising=False)
    return fake_global


def _write_yaml(path: Path, body: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"schema_version": "1.0", **body}), encoding="utf-8")


def _repo(tmp_path: Path, *, enabled: bool | None) -> Path:
    """Return a repo root whose repo layer sets ``telemetry.enabled`` (or nothing)."""
    repo = tmp_path / "repo"
    (repo / ".ea").mkdir(parents=True)
    if enabled is not None:
        _write_yaml(repo / ".ea" / "config.yaml", {"telemetry": {"enabled": enabled}})
    return repo


# -- the gate itself -------------------------------------------------------


def test_gate_defaults_to_enabled_without_any_config(tmp_path: Path) -> None:
    assert telemetry_enabled(_repo(tmp_path, enabled=None)) is True


@pytest.mark.parametrize("enabled", [True, False])
def test_gate_follows_the_repo_layer(tmp_path: Path, enabled: bool) -> None:
    assert telemetry_enabled(_repo(tmp_path, enabled=enabled)) is enabled


def test_gate_follows_the_global_layer(tmp_path: Path, _isolate_global_config: Path) -> None:
    _write_yaml(_isolate_global_config, {"telemetry": {"enabled": False}})
    assert telemetry_enabled(_repo(tmp_path, enabled=None)) is False


def test_gate_repo_layer_overrides_the_global_layer(
    tmp_path: Path, _isolate_global_config: Path
) -> None:
    _write_yaml(_isolate_global_config, {"telemetry": {"enabled": False}})
    assert telemetry_enabled(_repo(tmp_path, enabled=True)) is True


def test_gate_on_a_root_with_no_ea_dir_reads_the_default(tmp_path: Path) -> None:
    assert telemetry_enabled(tmp_path / "absent") is True


def test_gate_raises_on_malformed_config(tmp_path: Path) -> None:
    repo = _repo(tmp_path, enabled=None)
    (repo / ".ea" / "config.yaml").write_text("telemetry: [1, 2\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="malformed YAML"):
        telemetry_enabled(repo)
