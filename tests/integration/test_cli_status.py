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
from eawf.kernel.state.epoch2.decision import Decision, ratify, supersede
from eawf.kernel.state.epoch2.task import Task, TaskPriority, TaskStatus
from eawf.kernel.store.compaction import read_document, write_document
from eawf.kernel.store.ledger import (
    LedgerRecord,
    append_ledger_record,
    line_digest,
    render_ledger_line,
)
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
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


def _state_with_closed_wave() -> dict[str, Any]:
    """Deep-copy the base fixture and add one closed, pinned wave."""
    state: dict[str, Any] = json.loads(json.dumps(_VALID_STATE))
    state["waves"]["P01-I01-W00"] = {
        **state["waves"]["P01-I01-W01"],
        "id": "P01-I01-W00",
        "status": "closed",
        "claim_session_id": None,
        "outcome": "ok",
        "commit": "a" * 40,
        "closed_at": "2026-05-08T01:00:00Z",
    }
    state["iters"]["P01-I01"]["wave_ids"].append("P01-I01-W00")
    return state


@pytest.mark.parametrize("epoch", [1, 2])
def test_status_resolves_no_wave_commit_from_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    epoch: int,
) -> None:
    """Status reads git once for its head and never per closed or active wave."""
    state = _state_with_closed_wave()
    state_path = _seed(tmp_path, state) if epoch == 1 else lay_epoch2_tree(tmp_path, state=state)

    def _raise(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("status must not resolve a wave commit from git")

    monkeypatch.setattr("eawf.runtime.vcs.wave_sha.derive_wave_sha", _raise)
    monkeypatch.setattr("eawf.runtime.vcs.wave_sha.build_wave_sha_index", _raise)
    monkeypatch.setenv("EA_STATE", str(state_path))
    _stub_no_git(monkeypatch)
    result = runner.invoke(app, ["--json", "status"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert "drift" not in payload
    assert all("commit" not in wave for wave in payload["active_waves"])
    text = runner.invoke(app, ["status"])
    assert text.exit_code == 0, text.output
    assert "drift:" not in text.stdout


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


_DECISION_SCOPE = "eawf://WSP-MAIN/PRJ-QR/REP-QR/milestone/MLS-0001"


def _native_decision(key: str, created_at: str, **overrides: Any) -> Decision:
    """Return an active native decision filed at ``created_at``."""
    fields: dict[str, Any] = {
        "key": key,
        "scope_ref": _DECISION_SCOPE,
        "title": f"Native verdict {key}",
        "decision": "Do the thing.",
        "rationale": "It is the thing to do.",
        "alternatives": [{"key": "do", "label": "Do it"}, {"key": "skip", "label": "Skip it"}],
        "chosen_option_key": "do",
        "consequences": ["The thing is done"],
        "evidence_refs": ["eawf://WSP-MAIN/PRJ-QR/REP-QR/evidence/EVD-0001"],
        "effective_policy_revision": 1,
        "created_at": created_at,
    }
    fields.update(overrides)
    proposed = Decision.model_validate(fields)
    return ratify(proposed, by="OP-0001", at=proposed.created_at)


def _decision_ledger(state_path: Path) -> Path:
    authority = resolve_authority(state_path.parent)
    assert authority.target is not None and authority.generation_id is not None
    document_path = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    return ledger_path(document_path, Epoch2Collection.DECISION)


def _file_decision(
    state_path: Path, decision: Decision, *, replaces: LedgerRecord | None = None
) -> LedgerRecord:
    """Append ``decision``'s ledger line, as a successor of ``replaces`` when given."""
    record = LedgerRecord(
        collection=Epoch2Collection.DECISION,
        record_key=decision.key,
        status=decision.status.value,
        recorded_at=decision.created_at,
        supersedes=None if replaces is None else line_digest(render_ledger_line(replaces)),
        payload={"payload": decision.model_dump(mode="json")},
    )
    append_ledger_record(_decision_ledger(state_path), record)
    return record


def _status_decisions(state_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    monkeypatch.setenv("EA_STATE", str(state_path))
    _stub_no_git(monkeypatch)
    result = runner.invoke(app, ["--json", "status"])
    assert result.exit_code == 0, result.output
    decisions: list[dict[str, str]] = json.loads(result.stdout)["recent_decisions"]
    return decisions


def test_status_lists_a_native_decision_of_an_epoch2_tree_beside_the_imported_ones(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A native decision appears once, newest first, and an imported row is not doubled."""
    state = _state_with_decisions_and_backlog()
    state_path = lay_epoch2_tree(tmp_path, state=state)
    # The cutover files each epoch-1 decision into the ledger verbatim, under
    # its own status, so the epoch-1 record would otherwise be read twice.
    imported = LedgerRecord(
        collection=Epoch2Collection.DECISION,
        record_key="D02",
        status="imported",
        recorded_at=datetime(2026, 5, 9, tzinfo=UTC),
        payload={"origin": {"kind": "legacy"}, "payload": state["decisions"]["D02"]},
    )
    append_ledger_record(_decision_ledger(state_path), imported)
    _file_decision(state_path, _native_decision("D03", "2026-05-10T00:00:00Z"))
    assert _status_decisions(state_path, monkeypatch) == [
        {"id": "D03", "title": "Native verdict D03", "status": "active"},
        {
            "id": "D02",
            "title": "Adopt Pydantic v2 strict models at every boundary",
            "status": "active",
        },
        {
            "id": "D01",
            "title": "Pick portalocker for cross-platform file locks",
            "status": "active",
        },
    ]


def test_status_orders_a_native_decision_older_than_an_epoch1_one_after_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = lay_epoch2_tree(tmp_path, state=_state_with_decisions_and_backlog())
    _file_decision(state_path, _native_decision("D00", "2026-05-08T12:00:00Z"))
    assert [d["id"] for d in _status_decisions(state_path, monkeypatch)] == ["D02", "D00", "D01"]


def test_status_caps_the_merged_decisions_at_five(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = lay_epoch2_tree(tmp_path, state=_state_with_decisions_and_backlog())
    for day in range(10, 14):
        _file_decision(state_path, _native_decision(f"D{day}", f"2026-05-{day}T00:00:00Z"))
    decisions = _status_decisions(state_path, monkeypatch)
    assert [d["id"] for d in decisions] == ["D13", "D12", "D11", "D10", "D02"]


def test_status_lists_no_decision_for_an_epoch2_tree_with_an_empty_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = lay_epoch2_tree(tmp_path, state=_VALID_STATE)
    assert _status_decisions(state_path, monkeypatch) == []


def test_status_shows_a_superseded_native_decision_as_superseded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = lay_epoch2_tree(tmp_path, state=_VALID_STATE)
    old = _native_decision("D10", "2026-05-10T00:00:00Z")
    old_line = _file_decision(state_path, old)
    new = _native_decision("D11", "2026-05-11T00:00:00Z", supersedes="D10")
    _file_decision(state_path, supersede(old, new), replaces=old_line)
    _file_decision(state_path, new)
    assert _status_decisions(state_path, monkeypatch) == [
        {"id": "D11", "title": "Native verdict D11", "status": "active"},
        {"id": "D10", "title": "Native verdict D10", "status": "superseded"},
    ]


def test_status_leaves_the_decisions_of_an_epoch1_tree_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An epoch-1 tree never reads a decision ledger."""
    state_path = _seed(tmp_path, _state_with_decisions_and_backlog())

    def _raise(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("an epoch-1 tree must not read native decisions")

    monkeypatch.setattr("eawf.observability.reflect.decisions.read_native_decisions", _raise)
    assert [d["id"] for d in _status_decisions(state_path, monkeypatch)] == ["D02", "D01"]


def test_status_refuses_a_torn_decision_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = lay_epoch2_tree(tmp_path, state=_VALID_STATE)
    ledger = _decision_ledger(state_path)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text('{"schema_version":"2.0"')
    monkeypatch.setenv("EA_STATE", str(state_path))
    _stub_no_git(monkeypatch)
    result = runner.invoke(app, ["--json", "status"])
    assert result.exit_code == 1, result.output
    assert "decision ledger unreadable" in json.loads(result.stdout)["message"]


def test_status_refuses_a_native_decision_line_its_model_rejects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = lay_epoch2_tree(tmp_path, state=_VALID_STATE)
    record = LedgerRecord(
        collection=Epoch2Collection.DECISION,
        record_key="D10",
        status="ACTIVE",
        recorded_at=datetime(2026, 5, 10, tzinfo=UTC),
        payload={"payload": {"key": "D10"}},
    )
    append_ledger_record(_decision_ledger(state_path), record)
    monkeypatch.setenv("EA_STATE", str(state_path))
    _stub_no_git(monkeypatch)
    result = runner.invoke(app, ["--json", "status"])
    assert result.exit_code == 1, result.output
    assert "decision ledger unreadable" in json.loads(result.stdout)["message"]
