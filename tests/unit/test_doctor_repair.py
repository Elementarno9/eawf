"""Doctor repair planning and digest-guard tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from eawf.kernel.store.ledger import LedgerRecord, render_ledger_line
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.observability.doctor import repair
from eawf.observability.doctor.checks import CheckResult
from eawf.observability.doctor.repair import build_repair_plan
from eawf.platform.lint.ledger_history import LedgerHistoryReport, UnreachableFinding
from eawf.runtime.daemon.methods.doctor import _apply_config
from eawf.workflow.integration.rebind import RewritePair, append_rewrite_pairs, rewrite_map_path


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


# --- a recorded history rewrite is left to its rebind --------------------------

_LEDGER = ".ea/generations/gen-x/ledger/task.jsonl"
_HISTORY = ".ea/generations/gen-x/history/change.jsonl"
_MAPPED, _OTHER_MAPPED, _DANGLING, _NEW = "1" * 40, "2" * 40, "3" * 40, "4" * 40


def _ledger_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, mapped: bool) -> None:
    """Lay out a ledger whose rows bind a rewritten and a dangling commit, and fake the scan.

    Line 1 binds only a rewritten commit, line 2 a rewritten and a dangling
    one; the history quotes another rewritten commit.
    """
    ledger = tmp_path / _LEDGER
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        "".join(
            render_ledger_line(
                LedgerRecord(
                    collection=Epoch2Collection.TASK,
                    record_key=key,
                    status="COMPLETED",
                    recorded_at=datetime(2026, 11, 1, tzinfo=UTC),
                    payload={"legacy_refs": {"commits": list(shas)}},
                )
            )
            + "\n"
            for key, shas in (("TSK-1", (_MAPPED,)), ("TSK-2", (_MAPPED, _DANGLING)))
        ),
        encoding="utf-8",
    )
    findings = (
        UnreachableFinding(path=_LEDGER, line=1, record_key="TSK-1", sha=_MAPPED),
        UnreachableFinding(path=_LEDGER, line=2, record_key="TSK-2", sha=_MAPPED),
        UnreachableFinding(path=_LEDGER, line=2, record_key="TSK-2", sha=_DANGLING),
        UnreachableFinding(path=_HISTORY, line=1, record_key="c-1", sha=_OTHER_MAPPED),
    )
    report = LedgerHistoryReport(
        range="origin/main",
        base="a" * 40,
        tip="WORKTREE",
        ledgers_scanned=2,
        append_only=(),
        unreachable=findings,
        malformed=(),
    )
    monkeypatch.setattr(repair, "scan_ledger_history", lambda _repo, _spec: report)
    monkeypatch.setattr(repair, "_git_text", lambda *_a, **_k: "")
    monkeypatch.setattr(repair, "_rebind_target", lambda *_a: (None, "no commit shares it"))
    monkeypatch.setattr(repair, "_archive_ref", lambda _repo, sha: f"refs/eawf/archive/{sha}")
    if mapped:
        append_rewrite_pairs(
            rewrite_map_path(tmp_path / ".ea"),
            (
                RewritePair(old=old, new=_NEW, applied_at=datetime(2026, 11, 2, tzinfo=UTC))
                for old in (_MAPPED, _OTHER_MAPPED)
            ),
        )


def _frozen(rows: tuple[repair.LedgerSupersession, ...]) -> list[str]:
    return [entry.sha for row in rows for entry in row.unreachable]


def test_rewrite_absent_map_freezes_every_unreachable_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ledger_repo(tmp_path, monkeypatch, mapped=False)

    rows, unrepairable = repair.plan_ledger_supersessions(tmp_path)

    assert [row.line for row in rows] == [1, 2]
    assert _frozen(rows) == [_MAPPED, _MAPPED, _DANGLING]
    assert unrepairable == 1


def test_rewrite_mapped_commit_is_never_frozen_and_gets_no_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ledger_repo(tmp_path, monkeypatch, mapped=True)

    rows, unrepairable = repair.plan_ledger_supersessions(tmp_path)

    # Line 1 names only a rewritten commit, so nothing supersedes it.
    (row,) = rows
    assert (row.line, _frozen(rows), row.rebinds) == (2, [_DANGLING], {})
    assert unrepairable == 0
    record = repair.superseding_record(tmp_path, row, recorded_at=datetime(2026, 11, 3, tzinfo=UTC))
    assert [entry["sha"] for entry in record.payload["unreachable_shas"]] == [_DANGLING]


def test_rewrite_plan_counts_rewritten_commits_with_the_rebind_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _quiet_host_checks(monkeypatch)
    monkeypatch.setattr(repair, "_artifact_digest_action", lambda _root: (None, 0))
    for name in ("_receipt_rekey_action", "_legacy_seal_action"):
        monkeypatch.setattr(repair, name, lambda _root: None)
    _ledger_repo(tmp_path, monkeypatch, mapped=True)

    plan = build_repair_plan(tmp_path)

    (action,) = plan.actions
    assert (action.action_id, action.record_count) == (repair.LEDGER_SUPERSEDE_ACTION, 1)
    assert plan.unresolved_findings == ["ledger.rewritten=2: run eawf batch rebind-rewritten"]


def test_rewrite_plan_names_no_rewritten_count_without_a_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _quiet_host_checks(monkeypatch)
    monkeypatch.setattr(repair, "_artifact_digest_action", lambda _root: (None, 0))
    for name in ("_receipt_rekey_action", "_legacy_seal_action"):
        monkeypatch.setattr(repair, name, lambda _root: None)
    _ledger_repo(tmp_path, monkeypatch, mapped=False)

    plan = build_repair_plan(tmp_path)

    assert plan.unresolved_findings == ["ledger.unreachable_unrepairable=1"]
    assert plan.actions[0].record_count == 2


@pytest.mark.parametrize(
    "content", [b'{"old": "1"}\n', b'{"old": "' + b"1" * 40 + b'", "new'], ids=["bad", "torn"]
)
def test_rewrite_malformed_record_is_a_finding_naming_the_file_not_no_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: bytes
) -> None:
    _quiet_host_checks(monkeypatch)
    monkeypatch.setattr(repair, "_artifact_digest_action", lambda _root: (None, 0))
    for name in ("_receipt_rekey_action", "_legacy_seal_action"):
        monkeypatch.setattr(repair, name, lambda _root: None)
    _ledger_repo(tmp_path, monkeypatch, mapped=False)
    record = rewrite_map_path(tmp_path / ".ea")
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_bytes(content)

    plan = build_repair_plan(tmp_path)

    assert plan.actions == []
    (finding,) = plan.unresolved_findings
    assert finding.startswith(
        "ledger.rewrite_map_invalid=1: .ea/store/rewrite_map.jsonl: rewrite_map_invalid: line 1"
    )
    assert plan.status == "needs_user"
