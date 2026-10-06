"""Doctor repair planning and digest-guard tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from eawf.observability.doctor.checks import CheckResult
from eawf.observability.doctor.repair import build_repair_plan
from eawf.runtime.daemon.methods.doctor import _apply_config


def _quiet_host_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "eawf.observability.doctor.repair.global_config_path",
        lambda: Path("__missing_global_config__"),
    )
    monkeypatch.setattr(
        "eawf.observability.doctor.repair.check_manifest_in_sync",
        lambda **_kwargs: CheckResult(name="manifest_in_sync", status="ok"),
    )
    monkeypatch.setattr(
        "eawf.observability.doctor.repair._desired_sync_drift",
        lambda _workspace: False,
    )
    monkeypatch.setattr(
        "eawf.observability.doctor.repair.check_launchd_agent",
        lambda: CheckResult(name="launchd_agent", status="ok"),
    )
    monkeypatch.setattr("eawf.observability.doctor.repair.find_stray_daemons", lambda: [])


def test_build_repair_plan_classifies_legacy_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _quiet_host_checks(monkeypatch)
    config_path = tmp_path / ".ea" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "flow": {"auto_accept": {"research": True, "review": True}},
                "audit": {"fix_safe": True},
            }
        ),
        encoding="utf-8",
    )

    plan = build_repair_plan(tmp_path)

    assert plan.status == "ready"
    assert [action.action_id for action in plan.actions] == ["config.normalize.1"]
    action = plan.actions[0]
    assert action.scope == "workspace"
    assert action.mutation_class == "committed_config"
    assert action.record_count == 1


def test_apply_config_is_idempotent_and_keeps_backup_local(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _quiet_host_checks(monkeypatch)
    config_path = tmp_path / ".ea" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "flow": {"auto_accept": {"research": True, "ship": True}},
            }
        ),
        encoding="utf-8",
    )
    action = build_repair_plan(tmp_path).actions[0]

    status, changed = _apply_config(tmp_path, action)

    assert (status, changed) == ("applied", 1)
    migrated = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert "flow" not in migrated
    assert list((tmp_path / ".ea" / "local" / "config-backups").glob("*.bak.*"))
    assert build_repair_plan(tmp_path).actions == []


def test_apply_config_rejects_changed_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eawf.runtime.daemon.methods import DaemonValidationError

    _quiet_host_checks(monkeypatch)
    config_path = tmp_path / ".ea" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "flow": {"auto_accept": {"research": True}},
            }
        ),
        encoding="utf-8",
    )
    action = build_repair_plan(tmp_path).actions[0]
    config_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "flow": {"auto_accept": {"research": False}},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(DaemonValidationError, match="preview changed"):
        _apply_config(tmp_path, action)
