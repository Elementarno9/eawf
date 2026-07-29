"""Doctor repair planning and digest-guard tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from eawf.kernel.state.enums import WaveStatus
from eawf.kernel.state.models import State, Wave
from eawf.observability.doctor.checks import CheckResult
from eawf.observability.doctor.repair import build_repair_plan
from eawf.runtime.daemon.methods.doctor import _apply_config
from eawf.workflow.lifecycle.repin_provenance import (
    append_commit_repin_provenance,
    complete_commit_repin_provenance,
)
from eawf.workflow.lifecycle.wave_sha import RepairAction


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
    assert migrated["flow"] == {"advance_after": {"research": True}}
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


def test_commit_repin_provenance_is_typed_and_idempotent(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    action = RepairAction(
        wave_id="P02-I01-W01",
        kind="pinned_mismatch",
        old_commit="a" * 40,
        new_commit="b" * 40,
        identity_digest=f"sha256:{'c' * 64}",
    )

    assert append_commit_repin_provenance(state_path, [action]) == 1
    assert append_commit_repin_provenance(state_path, [action]) == 0
    path = state_path.parent / "store" / "commit_repin.jsonl"
    rows = path.read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1
    assert '"disposition":"historical_repin"' in rows[0]
    assert '"status":"applied"' in rows[0]


def test_commit_repin_intent_completes_after_state_write(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    action = RepairAction(
        wave_id="P02-I01-W01",
        kind="pinned_mismatch",
        old_commit="a" * 40,
        new_commit="b" * 40,
        identity_digest=f"sha256:{'c' * 64}",
    )
    state = State.model_construct(
        waves={
            action.wave_id: Wave.model_construct(
                id=action.wave_id,
                status=WaveStatus.CLOSED,
                commit=action.new_commit,
            )
        }
    )

    assert (
        append_commit_repin_provenance(
            state_path,
            [action],
            status="planned",
        )
        == 1
    )
    assert complete_commit_repin_provenance(state_path, state) == 1
    assert complete_commit_repin_provenance(state_path, state) == 0
    rows = (
        (state_path.parent / "store" / "commit_repin.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert len(rows) == 2
