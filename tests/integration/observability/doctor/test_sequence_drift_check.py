"""Doctor names a committed canonical sequence behind its own committed tier.

A Run-only write moves the live cursor in the status projection and
leaves the committed generation document as it is, so a committed
sequence behind the live one is a healthy tree. ``eawf doctor`` reports
``canonical_sequence_drift`` only when the committed change feed records
a change the committed document never reached.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.store.changes import (
    ChangeRecord,
    ChangeTier,
    FieldChange,
    StoredValue,
    append_change_record,
)
from eawf.kernel.store.compaction import CANONICAL_SEQUENCE_KEY, read_document, write_document
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.observability.doctor.checks_sequence import CHECK_NAME, check_canonical_sequence_drift
from eawf.platform.install.instrument_probe import PROBE_VERSION, ProbeReport, ProbeResult
from eawf.surfaces.cli.app import app
from tests._epoch2_helpers import lay_epoch2_tree

runner = CliRunner()


@pytest.fixture(autouse=True)
def _sandbox_host_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the operator's global config and live daemons out of every test."""
    from eawf.runtime.daemon.service_install import SupervisedAgentReport

    report = SupervisedAgentReport(
        supervisor="none",
        label="",
        installed=False,
        loaded=False,
        program=None,
        drift=False,
        rival_pid=None,
    )
    monkeypatch.setattr(
        "eawf.runtime.daemon.service_install.detect_supervised_agent",
        lambda *_a, **_k: report,
    )
    monkeypatch.setattr(
        "eawf.kernel.config.layered.global_config_path",
        lambda: tmp_path / "global-config.yaml",
    )
    monkeypatch.setattr(
        "eawf.observability.doctor.checks._probe_running_daemon_version", lambda: None
    )
    monkeypatch.setattr("eawf.observability.doctor.daemon_strays.find_stray_daemons", lambda: [])


def _green_probe(*_args: object, **_kwargs: object) -> ProbeReport:
    return ProbeReport(
        probe_version=PROBE_VERSION,
        profile_ids=["core"],
        results=[
            ProbeResult(name="git", kind="hard", status="ok", path="git"),
            ProbeResult(name="python", kind="hard", status="ok", path="python"),
            ProbeResult(name="uv", kind="hard", status="ok", path="uv"),
        ],
    )


def _generation_document(repo: Path) -> Path:
    authority = resolve_authority(repo / ".ea")
    assert authority.target is not None and authority.generation_id is not None
    return authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT


def _tree(tmp_path: Path, *, committed: int, live: int) -> tuple[Path, Path]:
    """Bear a tree whose committed sequence is *committed* and live one *live*."""
    repo = tmp_path / "repo"
    lay_epoch2_tree(repo)
    document = _generation_document(repo)
    body = read_document(document)
    body["milestone"] = {"MLS-0001": {"status": "PLANNED", "seen_at": committed}}
    body[CANONICAL_SEQUENCE_KEY] = committed
    write_document(document, body)
    write_document(document, read_document(document) | {CANONICAL_SEQUENCE_KEY: live})
    assert json.loads(document.read_bytes())[CANONICAL_SEQUENCE_KEY] == committed
    assert read_document(document)[CANONICAL_SEQUENCE_KEY] == live
    return repo, document


def _doctor_check(repo: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Run ``eawf --json doctor`` on *repo* and return its drift check."""
    monkeypatch.setattr("eawf.observability.doctor.checks.probe", _green_probe)
    monkeypatch.chdir(repo)
    result = runner.invoke(app, ["--json", "-w", str(repo), "doctor"])
    checks: list[dict[str, Any]] = json.loads(result.stdout)["checks"]
    (check,) = [c for c in checks if c["name"] == CHECK_NAME]
    return check


def _file_committed_change(document: Path, sequence: int) -> None:
    """File one committed change at *sequence* in the generation's change feed."""
    append_change_record(
        document,
        ChangeRecord(
            change_id=f"EVT-{sequence}:milestone:MLS-0001",
            tier=ChangeTier.COMMITTED,
            collection=Epoch2Collection.MILESTONE,
            record_key="MLS-0001",
            event_name="domain.milestone.updated",
            revision_before=1,
            revision_after=2,
            canonical_sequence=sequence,
            actor_ref=None,
            recorded_at=datetime(2026, 10, 24, tzinfo=UTC),
            changes=(
                FieldChange(
                    field="seen_at",
                    before=StoredValue(value=sequence - 1, size=4),
                    after=StoredValue(value=sequence, size=4),
                ),
            ),
        ),
    )


def test_doctor_is_ok_when_only_local_writes_moved_the_live_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, document = _tree(tmp_path, committed=7299, live=7744)
    _file_committed_change(document, 7299)

    check = _doctor_check(repo, monkeypatch)

    assert check["status"] == "ok"
    assert "7299" in check["detail"]


def test_doctor_names_a_committed_document_behind_its_change_feed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, document = _tree(tmp_path, committed=7299, live=7744)
    _file_committed_change(document, 7300)

    behind = _doctor_check(repo, monkeypatch)
    assert behind["status"] == "warn"
    assert "7299" in behind["detail"]
    assert "7300" in behind["detail"]

    body = read_document(document)
    body["milestone"]["MLS-0001"]["seen_at"] = 7744
    write_document(document, body)
    level = _doctor_check(repo, monkeypatch)
    assert level["status"] == "ok"
    assert "7744" in level["detail"]


def test_drift_is_ok_without_an_anchor() -> None:
    assert check_canonical_sequence_drift(workspace=None).status == "ok"


def test_drift_is_ok_on_an_epoch1_tree(tmp_path: Path) -> None:
    (tmp_path / ".ea").mkdir()
    (tmp_path / ".ea" / "state.json").write_text("{}", encoding="utf-8")

    result = check_canonical_sequence_drift(workspace=tmp_path)

    assert (result.status, result.detail) == ("ok", "no epoch-2 generation")


def test_drift_is_ok_before_any_committed_change(tmp_path: Path) -> None:
    repo, _document = _tree(tmp_path, committed=4, live=5)

    result = check_canonical_sequence_drift(workspace=repo)

    assert (result.status, result.detail) == ("ok", "no committed change recorded yet")


def test_drift_warns_one_behind(tmp_path: Path) -> None:
    repo, document = _tree(tmp_path, committed=4, live=5)
    _file_committed_change(document, 5)

    result = check_canonical_sequence_drift(workspace=repo)

    assert result.status == "warn"
    assert result.detail is not None
    assert "4" in result.detail and "5" in result.detail


def test_drift_is_ok_when_level(tmp_path: Path) -> None:
    repo, document = _tree(tmp_path, committed=1, live=1)
    _file_committed_change(document, 1)

    result = check_canonical_sequence_drift(workspace=repo)

    assert result.status == "ok"


def test_drift_warns_when_the_committed_copy_is_absent(tmp_path: Path) -> None:
    repo, document = _tree(tmp_path, committed=3, live=3)
    _file_committed_change(document, 3)
    raw = json.loads(document.read_bytes())
    del raw[CANONICAL_SEQUENCE_KEY]
    document.write_text(json.dumps(raw), encoding="utf-8")

    result = check_canonical_sequence_drift(workspace=repo)

    assert result.status == "warn"
    assert result.detail is not None
    assert "absent" in result.detail and "3" in result.detail


@pytest.mark.parametrize("raw", ["[]", "{not json"])
def test_drift_warns_on_an_unreadable_document(tmp_path: Path, raw: str) -> None:
    repo, document = _tree(tmp_path, committed=1, live=1)
    document.write_text(raw, encoding="utf-8")

    result = check_canonical_sequence_drift(workspace=repo)

    assert result.status == "warn"
    assert result.detail is not None
    assert "unreadable" in result.detail
