"""Integration tests for ``eawf status`` driving the CLI via :class:`CliRunner`.

A small valid state.json is stamped under ``tmp_path/.ea/`` and ``EA_STATE``
is pointed at it. The git invocations are stubbed out via monkeypatching of
:func:`subprocess.run` so the test never depends on a real worktree.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import orjson
import pytest
from typer.testing import CliRunner

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.state.epoch2.task import Task, TaskPriority, TaskStatus
from eawf.kernel.store.compaction import read_document, write_document
from eawf.surfaces.cli.app import app
from tests._epoch2_helpers import lay_epoch2_tree

runner = CliRunner()


_VALID_STATE: dict[str, Any] = {
    "schema_version": "1.0",
    "scope_kind": "repo",
    "urn": "urn:eawf:v1:state:QR",
    "updated_at": "2026-05-08T00:00:00Z",
    "project": {
        "code": "QR",
        "slug": "quant-research",
        "title": "Quant Research",
        "description": "",
        "domains": ["quant"],
        "default_branch": "main",
        "status": "active",
        "repo_urn": "urn:eawf:v1:repo:QR",
    },
    "current": {
        "project_code": "QR",
        "track_id": None,
        "phase_id": "P01",
        "iter_id": "P01-I01",
        "active_wave_ids": ["P01-I01-W01"],
        "active_session_ids": [],
    },
    "workspace": None,
    "phases": {
        "P01": {
            "id": "P01",
            "scope_id": "QR",
            "track_id": None,
            "title": "Bootstrap",
            "status": "active",
            "iter_ids": ["P01-I01"],
            "outcome_ids": [],
            "opened_at": "2026-05-08T00:00:00Z",
            "closed_at": None,
            "audit_id": None,
        }
    },
    "iters": {
        "P01-I01": {
            "id": "P01-I01",
            "phase_id": "P01",
            "title": "Iter 1",
            "status": "active",
            "wave_ids": ["P01-I01-W01"],
            "estimate_id": None,
            "audit_id": None,
            "opened_at": "2026-05-08T00:00:00Z",
            "closed_at": None,
        }
    },
    "waves": {
        "P01-I01-W01": {
            "id": "P01-I01-W01",
            "iter_id": "P01-I01",
            "title": "W1",
            "status": "in_progress",
            "deps": [],
            "file_scopes": ["src/foo.py"],
            "claim_session_id": "S-1",
            "worktree_id": None,
            "outcome": None,
            "opened_at": "2026-05-08T00:00:00Z",
            "closed_at": None,
        }
    },
    "artifacts": {},
    "agent_sessions": {},
    "plugins": {},
    "indexes": {},
}


def _stub_no_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force every git subprocess invocation to fail with FileNotFoundError."""

    def _fake_run(*_args: Any, **_kwargs: Any) -> Any:
        raise FileNotFoundError("git stubbed away")

    monkeypatch.setattr(subprocess, "run", _fake_run)


def _seed(tmp_path: Path, state: dict[str, Any] | None = None) -> Path:
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    state_path = state_dir / "state.json"
    state_path.write_bytes(orjson.dumps(state if state is not None else _VALID_STATE))
    return state_path


def test_status_json_envelope_round_trips(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = _seed(tmp_path)
    monkeypatch.setenv("EA_STATE", str(state_path))
    _stub_no_git(monkeypatch)
    result = runner.invoke(app, ["--json", "status"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["project"]["code"] == "QR"
    assert payload["scope_kind"] == "repo"
    assert payload["current"]["phase_id"] == "P01"
    assert payload["current"]["iter_id"] == "P01-I01"
    assert payload["active_waves"][0]["id"] == "P01-I01-W01"
    assert payload["last_phase_audit"] is None
    assert payload["last_iter_audit"] is None
    assert payload["git"] == {"head": None, "branch": None, "dirty": None}
    assert payload["blockers"] == []
    assert payload["last_closed_waves"] == []
    assert payload["recent_decisions"] == []
    assert payload["open_backlog"] == []


def test_status_text_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = _seed(tmp_path)
    monkeypatch.setenv("EA_STATE", str(state_path))
    _stub_no_git(monkeypatch)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "project: QR (Quant Research)" in result.stdout
    assert "phase=P01 iter=P01-I01" in result.stdout
    assert "blockers: none" in result.stdout


def test_status_command_local_json_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``eawf status --json`` (subcommand-level) also activates JSON emission."""
    state_path = _seed(tmp_path)
    monkeypatch.setenv("EA_STATE", str(state_path))
    _stub_no_git(monkeypatch)
    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["project"]["code"] == "QR"


def test_status_returns_not_found_when_state_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EA_STATE", str(tmp_path / ".ea" / "state.json"))
    _stub_no_git(monkeypatch)
    result = runner.invoke(app, ["--json", "status"])
    assert result.exit_code == 1  # NOT_FOUND
    body = json.loads(result.stdout)
    assert body["error"] == "UserError"
    assert "state.json" in body["message"]


def test_status_returns_invalid_input_when_state_malformed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    state_path = state_dir / "state.json"
    bad = dict(_VALID_STATE)
    bad["scope_kind"] = "not-a-real-scope-kind"
    state_path.write_bytes(orjson.dumps(bad))
    monkeypatch.setenv("EA_STATE", str(state_path))
    _stub_no_git(monkeypatch)
    result = runner.invoke(app, ["--json", "status"])
    assert result.exit_code == 1  # INVALID_INPUT
    body = json.loads(result.stdout)
    assert body["error"] == "UserError"


def test_status_workspace_flag_overrides_pwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(tmp_path)
    monkeypatch.delenv("EA_STATE", raising=False)
    _stub_no_git(monkeypatch)
    result = runner.invoke(app, ["--json", "status", "-w", str(tmp_path)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["project"]["code"] == "QR"


def test_status_lists_last_closed_waves(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = json.loads(json.dumps(_VALID_STATE))  # deep copy
    state["waves"]["P01-I01-W00"] = {
        "id": "P01-I01-W00",
        "iter_id": "P01-I01",
        "title": "W0",
        "status": "closed",
        "deps": [],
        "file_scopes": [],
        "claim_session_id": None,
        "worktree_id": None,
        "outcome": "ok",
        "opened_at": "2026-05-07T00:00:00Z",
        "closed_at": "2026-05-07T01:00:00Z",
    }
    state["iters"]["P01-I01"]["wave_ids"].append("P01-I01-W00")
    state_path = _seed(tmp_path, state)
    monkeypatch.setenv("EA_STATE", str(state_path))
    _stub_no_git(monkeypatch)
    result = runner.invoke(app, ["--json", "status"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert "P01-I01-W00" in payload["last_closed_waves"]


def test_status_drift_summary_honors_repo_ack_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``eawf status`` uses ``.eawf/drift-acks.json`` before counting drift."""
    state = json.loads(json.dumps(_VALID_STATE))  # deep copy
    state["waves"]["P01-I01-W00"] = {
        "id": "P01-I01-W00",
        "iter_id": "P01-I01",
        "title": "W0",
        "status": "closed",
        "deps": [],
        "file_scopes": [],
        "claim_session_id": None,
        "worktree_id": None,
        "outcome": "ok",
        "commit": "a" * 40,
        "opened_at": "2026-05-07T00:00:00Z",
        "closed_at": "2026-05-07T01:00:00Z",
    }
    state["iters"]["P01-I01"]["wave_ids"].append("P01-I01-W00")
    (tmp_path / ".git").mkdir()
    state_path = _seed(tmp_path, state)
    ack_dir = tmp_path / ".eawf"
    ack_dir.mkdir()
    (ack_dir / "drift-acks.json").write_text(
        json.dumps({"acked_wave_ids": ["P01-I01-W00"]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_STATE", str(state_path))
    _stub_no_git(monkeypatch)

    result = runner.invoke(app, ["--json", "status"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["drift"] == {"count": 0, "tier": "ok"}


def test_status_payload_keys_documented_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lock the JSON envelope key surface so future waves don't drift it."""
    state_path = _seed(tmp_path)
    monkeypatch.setenv("EA_STATE", str(state_path))
    _stub_no_git(monkeypatch)
    result = runner.invoke(app, ["--json", "status"])
    payload = json.loads(result.stdout)
    expected_keys = {
        "project",
        "scope_kind",
        "current",
        "last_phase_audit",
        "last_iter_audit",
        "active_waves",
        "active_sessions",
        "last_closed_waves",
        "recent_decisions",
        "open_backlog",
        "open_backlog_count",
        "git",
        "drift",
        "authority",
        "native",
        "blockers",
        "research_campaign",
    }
    assert set(payload.keys()) == expected_keys
    # No campaign is staged on the base fixture, so the fold is None.
    assert payload["research_campaign"] is None
    # An epoch-1 tree holds no generation, so there is no native work to list.
    assert payload["native"] is None


def _state_with_decisions_and_backlog() -> dict[str, Any]:
    """Deep-copy the base fixture and stamp two decisions + three backlog items."""
    state = json.loads(json.dumps(_VALID_STATE))
    state["decisions"] = {
        "D01": {
            "id": "D01",
            "scope_id": "QR",
            "title": "Pick portalocker for cross-platform file locks",
            "rationale": "portalocker is the only maintained cross-platform advisory lock.",
            "alternatives": ["fcntl-only"],
            "status": "active",
            "created_at": "2026-05-08T00:00:00Z",
        },
        "D02": {
            "id": "D02",
            "scope_id": "QR",
            "title": "Adopt Pydantic v2 strict models at every boundary",
            "rationale": "strict validation at ingestion keeps downstream code typed.",
            "alternatives": [],
            "status": "active",
            "created_at": "2026-05-09T00:00:00Z",
        },
    }
    state["backlog"] = {
        "B01": {
            "id": "B01",
            "scope_id": "QR",
            "title": "Wire telemetry capture into wave close",
            "priority": "P1",
            "status": "open",
            "created_at": "2026-05-08T00:00:00Z",
        },
        "B02": {
            "id": "B02",
            "scope_id": "QR",
            "title": "Backfill estimate reference classes",
            "priority": "P0",
            "status": "in_progress",
            "created_at": "2026-05-08T00:00:00Z",
        },
        "B03": {
            "id": "B03",
            "scope_id": "QR",
            "title": "Old idea that already shipped",
            "priority": "P2",
            "status": "closed",
            "created_at": "2026-05-08T00:00:00Z",
            "closed_at": "2026-05-09T00:00:00Z",
        },
    }
    return state


def test_status_recent_decisions_newest_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``recent_decisions`` lists decisions newest-first with a compact projection."""
    state_path = _seed(tmp_path, _state_with_decisions_and_backlog())
    monkeypatch.setenv("EA_STATE", str(state_path))
    _stub_no_git(monkeypatch)
    result = runner.invoke(app, ["--json", "status"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    decisions = payload["recent_decisions"]
    assert [d["id"] for d in decisions] == ["D02", "D01"]
    assert decisions[0] == {
        "id": "D02",
        "title": "Adopt Pydantic v2 strict models at every boundary",
        "status": "active",
    }


def test_status_open_backlog_filters_closed_and_sorts_by_priority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``open_backlog`` drops closed items and sorts P0 before P1."""
    state_path = _seed(tmp_path, _state_with_decisions_and_backlog())
    monkeypatch.setenv("EA_STATE", str(state_path))
    _stub_no_git(monkeypatch)
    result = runner.invoke(app, ["--json", "status"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    backlog = payload["open_backlog"]
    assert [b["id"] for b in backlog] == ["B02", "B01"]  # P0 before P1; B03 closed → absent
    assert backlog[0]["priority"] == "P0"
    assert backlog[0]["status"] == "in_progress"


def test_status_is_byte_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``eawf status`` mutates nothing: state.json is byte-equal before and after.

    The whole status surface is a pure projection (AGENTS rule: the digest /
    status projections are PURE). This test hashes state.json before and after
    the command and asserts the digest is unchanged, so a future regression
    that writes through the status path is caught.
    """
    import hashlib

    state_path = _seed(tmp_path, _state_with_decisions_and_backlog())
    monkeypatch.setenv("EA_STATE", str(state_path))
    _stub_no_git(monkeypatch)
    before = hashlib.sha256(state_path.read_bytes()).hexdigest()
    result = runner.invoke(app, ["--json", "status"])
    assert result.exit_code == 0, result.output
    after = hashlib.sha256(state_path.read_bytes()).hexdigest()
    assert before == after


def test_status_text_branch_surfaces_decisions_and_backlog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The text branch shows compact recent-decisions + open-backlog lines."""
    state_path = _seed(tmp_path, _state_with_decisions_and_backlog())
    monkeypatch.setenv("EA_STATE", str(state_path))
    _stub_no_git(monkeypatch)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "recent decisions: D02, D01" in result.stdout
    assert "open backlog: 2 (B02, B01)" in result.stdout


def test_status_research_campaign_fold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A staged + run campaign folds a research_campaign summary into status."""
    from datetime import UTC, datetime

    from eawf.kernel.spec.research import ResearchDepth
    from eawf.kernel.spec.research_campaign import (
        ResearchDomainConfig,
        ResearchProfileBlock,
        stage_campaign,
    )
    from eawf.kernel.store.kinds.research_campaign import ResearchCampaignPayload
    from eawf.runtime.daemon.methods.research import (
        ResearchRoundPayload,
        persist_campaign,
        persist_round,
    )

    state_path = _seed(tmp_path)
    monkeypatch.setenv("EA_STATE", str(state_path))
    _stub_no_git(monkeypatch)
    block = ResearchProfileBlock(
        default_depth=ResearchDepth.MEDIUM,
        domains={"market-structure": ResearchDomainConfig(focus="venues")},
    )
    persist_campaign(
        state_path,
        ResearchCampaignPayload(
            campaign_id="campaign-s",
            config=block,
            campaign=stage_campaign("options-pricing landscape", block),
        ),
    )
    persist_round(
        state_path,
        ResearchRoundPayload(
            campaign_id="campaign-s",
            round_number=1,
            domains=["market-structure"],
            finding_lines=["a claim"],
            claim_ids=["CLM-r1-market-structure-0"],
            saturated=False,
            checkpoint=True,
            recorded_at=datetime(2026, 6, 11, 12, tzinfo=UTC),
        ),
    )

    result = runner.invoke(app, ["--json", "status"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    campaign = payload["research_campaign"]
    assert campaign is not None
    assert campaign["campaigns"] == 1
    assert campaign["rounds_run"] == 1
    assert campaign["kind"] == "runnable"
    # The text branch surfaces a compact research line.
    text = runner.invoke(app, ["status"])
    assert "research: runnable (rounds=1" in text.stdout


def _native_task(key: str, status: TaskStatus) -> dict[str, Any]:
    """Return one native backlog Task row, as ``eawf task create`` stores it."""
    at = datetime(2026, 10, 1, tzinfo=UTC).isoformat()
    row: dict[str, Any] = Task.model_validate(
        {
            "uid": str(uuid4()),
            "key": key,
            "urn": f"eawf://QR/QR/QR/task/{key}",
            "origin": {"kind": "native", "mapping_basis": "native", "confidence": "exact"},
            "revision": 1,
            "created_at": at,
            "updated_at": at,
            "priority": TaskPriority.P1.value,
            "intent": f"deliver {key}",
            "contract_revision": 1,
            "status": status.value,
        }
    ).model_dump(mode="json")
    return row


def _epoch2_tree_with_tasks(tmp_path: Path, tasks: dict[str, TaskStatus]) -> Path:
    """Bear a tree at epoch 2 from the base fixture and file ``tasks`` in its generation."""
    state_path = lay_epoch2_tree(tmp_path, state=_VALID_STATE)
    authority = resolve_authority(state_path.parent)
    assert authority.target is not None and authority.generation_id is not None
    document_path = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    document = read_document(document_path)
    document["task"] = {key: _native_task(key, status) for key, status in tasks.items()}
    write_document(document_path, document)
    return state_path


def test_status_lists_the_open_native_tasks_of_an_epoch2_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = _epoch2_tree_with_tasks(
        tmp_path, {"QR-0001": TaskStatus.DRAFT, "QR-0002": TaskStatus.DROPPED}
    )
    monkeypatch.setenv("EA_STATE", str(state_path))
    _stub_no_git(monkeypatch)
    result = runner.invoke(app, ["--json", "status"])
    assert result.exit_code == 0, result.output
    native = json.loads(result.stdout)["native"]
    assert native == {
        "open_task_count": 1,
        "open_tasks": [{"key": "QR-0001", "status": "DRAFT", "title": "deliver QR-0001"}],
    }
    text = runner.invoke(app, ["status"]).stdout
    assert "open tasks: 1 (QR-0001 DRAFT)" in text
    # The frozen epoch-1 pointers would name a wave nothing moves any more.
    assert "phase=P01" not in text
    assert "open backlog:" not in text


def test_status_says_an_epoch2_tree_with_no_open_task_has_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = _epoch2_tree_with_tasks(tmp_path, {})
    monkeypatch.setenv("EA_STATE", str(state_path))
    _stub_no_git(monkeypatch)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "open tasks: none" in result.stdout


def test_status_refuses_a_generation_document_that_is_not_an_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = _epoch2_tree_with_tasks(tmp_path, {})
    authority = resolve_authority(state_path.parent)
    assert authority.target is not None and authority.generation_id is not None
    document_path = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    document_path.write_text("[]")
    monkeypatch.setenv("EA_STATE", str(state_path))
    _stub_no_git(monkeypatch)
    result = runner.invoke(app, ["--json", "status"])
    assert result.exit_code == 1, result.output
    envelope = json.loads(result.stdout)
    assert "generation document unreadable" in envelope["message"]
