"""Unit tests for the OpenCode runtime plugin installer (P14-W07 / D13)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eawf.runtimes.opencode import doctor_plugin, install_plugin
from eawf.runtimes.opencode.plugin_install import (
    IntegrityViolation,
    expected_plugin_js_bytes,
)


def test_install_writes_config_and_plugin_js(tmp_path: Path) -> None:
    result = install_plugin(tmp_path)
    config_path = tmp_path / "opencode.json"
    plugin_path = tmp_path / "plugin.js"
    assert config_path.is_file()
    assert plugin_path.is_file()
    parsed = json.loads(config_path.read_text())
    assert "__eawf_managed" in parsed
    assert "mcp" in parsed
    assert "plugin.js" in parsed["plugins"]
    assert result.plugin_js is not None
    assert result.plugin_js.action == "created"
    assert result.config is not None


def test_install_idempotent(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    second = install_plugin(tmp_path)
    assert second.plugin_js is not None
    assert second.plugin_js.action == "unchanged"
    assert second.config is not None
    assert second.config.action == "unchanged"


def test_install_dry_run_writes_nothing(tmp_path: Path) -> None:
    result = install_plugin(tmp_path, dry_run=True)
    assert result.dry_run is True
    assert not (tmp_path / "opencode.json").exists()
    assert not (tmp_path / "plugin.js").exists()


def test_install_refuses_hand_edited_plugin_js(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    (tmp_path / "plugin.js").write_text("// hand edit\n")
    with pytest.raises(IntegrityViolation):
        install_plugin(tmp_path)


def test_install_force_overrides_hand_edit(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    (tmp_path / "plugin.js").write_text("// hand edit\n")
    result = install_plugin(tmp_path, force=True)
    assert result.plugin_js is not None
    assert result.plugin_js.action == "updated"


def test_install_preserves_user_top_level_keys(tmp_path: Path) -> None:
    config_path = tmp_path / "opencode.json"
    config_path.write_text(json.dumps({"theme": "midnight", "mcp": {"foo": "bar"}}))
    install_plugin(tmp_path)
    parsed = json.loads(config_path.read_text())
    assert parsed["theme"] == "midnight"
    assert parsed["mcp"]["foo"] == "bar"
    assert "__eawf_managed" in parsed


def test_install_rejects_non_object_config(tmp_path: Path) -> None:
    (tmp_path / "opencode.json").write_text("[]")
    with pytest.raises(ValueError, match="must be a JSON object"):
        install_plugin(tmp_path)


def test_doctor_reports_clean_after_install(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    report = doctor_plugin(tmp_path)
    assert report.clean is True


def test_doctor_flags_missing(tmp_path: Path) -> None:
    report = doctor_plugin(tmp_path)
    assert report.clean is False
    assert report.missing


def test_doctor_flags_plugin_js_drift(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    (tmp_path / "plugin.js").write_text("// tampered\n")
    report = doctor_plugin(tmp_path)
    assert report.clean is False
    assert any(e.kind == "plugin_js" for e in report.drifted)


def test_doctor_flags_config_missing_managed_hash(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    (tmp_path / "opencode.json").write_text(json.dumps({"mcp": {}}))
    report = doctor_plugin(tmp_path)
    assert report.clean is False
    drifted_kinds = {e.kind for e in report.drifted}
    assert "config" in drifted_kinds


def test_expected_plugin_js_carries_version_stamp() -> None:
    body = expected_plugin_js_bytes().decode("utf-8")
    assert "__EAWF_PLUGIN_VERSION__" not in body
    assert "version: '1.0'" in body
