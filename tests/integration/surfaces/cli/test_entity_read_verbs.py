"""The ``list`` and ``show`` read verbs of the native entities.

Each reads the selected generation of the tree it resolves, without a daemon:
a Task, Milestone, plan revision, decision or pending action is listed or
shown as the generation stores it, so an agent never parses the document by
hand. The v1 ``state.json`` is never opened.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.state.epoch2.decision import Decision, ratify
from eawf.kernel.state.epoch2.task import Task
from eawf.kernel.store.compaction import read_document, write_document
from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app

pytestmark = pytest.mark.integration

runner = CliRunner()

PREFIX: Final = "eawf://WSP-MAIN/PRJ-DEMO/REP-DEMO"
TASK_KEYS: Final = frozenset(
    {"key", "urn", "status", "priority", "title", "batch_ref", "due_scope"}
)
AT: Final = datetime(2026, 10, 1, tzinfo=UTC).isoformat()
CRITERION: Final = {
    "id": "CR-0001-1",
    "text": "The verb lists the rows.",
    "kind": "behavioral",
    "acceptance_style": "binary",
    "evidence_kind": "deterministic",
    "gate_ids": ["G-01"],
    "required": True,
    "quality_dimension": "functional_suitability",
    "measurable_signal": "`uv run pytest -q tests/x.py` exits 0.",
}

#: Each entity group, the top-level array its list answers under, the key its
#: show answers under, and the key the tree below files one record under.
ENTITIES: Final = (
    ("task", "tasks", "task", "DEMO-0003"),
    ("milestone", "milestones", "milestone", "MLS-0001"),
    ("plan", "plan_revisions", "plan_revision", "PRV-0001"),
    ("decision", "decisions", "decision", "D0001"),
    ("action", "actions", "action", "ACT-0001"),
)


def _task(key: str, status: str, **fields: Any) -> dict[str, Any]:
    row: dict[str, Any] = Task.model_validate(
        {
            "uid": str(uuid4()),
            "key": key,
            "urn": f"{PREFIX}/task/{key}",
            "origin": {"kind": "native", "mapping_basis": "native", "confidence": "exact"},
            "revision": 1,
            "created_at": AT,
            "updated_at": AT,
            "priority": "P1",
            "intent": f"Deliver {key}: the long form of what {key} does",
            "contract_revision": 1,
            "status": status,
            "criteria": [] if status == "DRAFT" else [CRITERION],
            **fields,
        }
    ).model_dump(mode="json")
    return row


def _decision() -> Decision:
    proposed = Decision.model_validate(
        {
            "key": "D0001",
            "scope_ref": f"{PREFIX}/milestone/MLS-0001",
            "title": "Read native entities through verbs",
            "decision": "Add list and show verbs.",
            "rationale": "Agents stop parsing the document by hand.",
            "alternatives": [{"key": "do", "label": "Do it"}, {"key": "skip", "label": "Skip"}],
            "chosen_option_key": "do",
            "consequences": ["Agents read typed rows"],
            "evidence_refs": [f"{PREFIX}/evidence/EVD-0001"],
            "effective_policy_revision": 1,
            "created_at": AT,
        }
    )
    return ratify(proposed, by="OP-0001", at=proposed.created_at)


def _born_tree(repo: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Bear a tree with ``eawf init``, file one record of each entity, return the rows."""
    result = runner.invoke(
        app, ["--json", "--no-input", "init", "--target", str(repo), "--project-code", "DEMO"]
    )
    assert result.exit_code == exit_codes.OK, result.output
    monkeypatch.delenv("EA_STATE", raising=False)
    monkeypatch.chdir(repo)
    authority = resolve_authority(repo / ".ea")
    assert authority.target is not None and authority.generation_id is not None
    document_path = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT

    rows: dict[str, Any] = {
        "task": {
            "DEMO-0001": _task(
                "DEMO-0001", "DRAFT", title="Ship it", due_scope=f"{PREFIX}/milestone/MLS-0001"
            ),
            "DEMO-0002": _task(
                "DEMO-0002",
                "PLANNED",
                batch_ref=f"{PREFIX}/batch/BAT-0001",
                due_scope=f"{PREFIX}/milestone/MLS-0002",
            ),
        },
        "milestone": {
            "MLS-0001": {
                "urn": f"{PREFIX}/milestone/MLS-0001",
                "revision": 1,
                "status": "ACTIVE",
                "title": "First cut",
            },
            "MLS-0002": {"urn": f"{PREFIX}/milestone/MLS-0002", "revision": 2, "status": "PLANNED"},
        },
        "plan": {"PRV-0001": {"key": "PRV-0001", "revision": 3, "status": "APPLIED"}},
        "action": {
            "ACT-0001": {
                "id": "ACT-0001",
                "urn": f"{PREFIX}/pending-action/ACT-0001",
                "status": "WAITING",
                "question": "Approve PRV-0001?",
            }
        },
    }
    document = read_document(document_path)
    document["task"] = rows["task"]
    document["milestone"] = rows["milestone"]
    document["batch"] = {
        "BAT-0001": {
            "urn": f"{PREFIX}/batch/BAT-0001",
            "revision": 1,
            "status": "ACTIVE",
            "milestone_ref": f"{PREFIX}/milestone/MLS-0002",
        }
    }
    document["plan_revision"] = rows["plan"]
    document["pending_action"] = rows["action"]
    write_document(document_path, document)

    # A finished Task has compacted out of the document into its ledger.
    finished = _task(
        "DEMO-0003",
        "COMPLETED",
        batch_ref=f"{PREFIX}/batch/BAT-0001",
        due_scope=f"{PREFIX}/milestone/MLS-0001",
        integrated_binding={
            "head_sha": "a" * 40,
            "tree_sha": "b" * 40,
            "contract_digest": f"sha256:{'c' * 64}",
            "policy_revision": 1,
            "evidence_digest": f"sha256:{'d' * 64}",
        },
    )
    rows["task"]["DEMO-0003"] = finished
    append_ledger_record(
        ledger_path(document_path, Epoch2Collection.TASK),
        LedgerRecord(
            collection=Epoch2Collection.TASK,
            record_key="DEMO-0003",
            status="COMPLETED",
            recorded_at=datetime.fromisoformat(AT),
            payload=finished,
        ),
    )
    decision = _decision()
    rows["decision"] = {"D0001": decision.model_dump(mode="json")}
    append_ledger_record(
        ledger_path(document_path, Epoch2Collection.DECISION),
        LedgerRecord(
            collection=Epoch2Collection.DECISION,
            record_key=decision.key,
            status=decision.status.value,
            recorded_at=decision.created_at,
            payload={"payload": decision.model_dump(mode="json")},
        ),
    )
    return rows


def _answer(*argv: str) -> dict[str, Any]:
    """Run ``eawf --json <argv>`` and return the verb's answer, unwrapped from its envelope."""
    result = runner.invoke(app, ["--json", *argv])
    assert result.exit_code == exit_codes.OK, result.output
    payload: dict[str, Any] = json.loads(result.stdout)
    if argv[0] == "plan":
        # The plan group sits outside the contract groups, so it prints its payload bare.
        return payload
    assert payload["status"] == "ok"
    assert payload["operation"] == " ".join(argv[:2])
    answer: dict[str, Any] = payload["result"]
    return answer


def test_task_list_json_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = _born_tree(tmp_path, monkeypatch)
    answer = _answer("task", "list")
    assert set(answer) == {"tasks"}
    tasks = answer["tasks"]
    assert [row["key"] for row in tasks] == ["DEMO-0001", "DEMO-0002", "DEMO-0003"]
    assert all(set(row) == TASK_KEYS for row in tasks)
    assert tasks[0] == {
        "key": "DEMO-0001",
        "urn": f"{PREFIX}/task/DEMO-0001",
        "status": "DRAFT",
        "priority": "P1",
        "title": "Ship it",
        "batch_ref": None,
        "due_scope": f"{PREFIX}/milestone/MLS-0001",
    }
    # An untitled Task reads its title from the label its intent leads with.
    assert tasks[1]["title"] == "Deliver DEMO-0002"
    assert tasks[2]["status"] == rows["task"]["DEMO-0003"]["status"] == "COMPLETED"

    def keys(*argv: str) -> list[str]:
        return [row["key"] for row in _answer("task", "list", *argv)["tasks"]]

    assert keys("--status", "PLANNED") == ["DEMO-0002"]
    assert keys("--status", "completed") == ["DEMO-0003"]
    assert keys("--status", "RUNNING") == []
    # A Task is under a Milestone when it is due in it or filed in one of its Batches.
    assert keys("--milestone", "MLS-0001") == ["DEMO-0001", "DEMO-0003"]
    assert keys("--milestone", "MLS-0002") == ["DEMO-0002", "DEMO-0003"]
    assert keys("--milestone", "MLS-0002", "--status", "COMPLETED") == ["DEMO-0003"]
    assert keys("--milestone", "MLS-9999") == []

    refused = runner.invoke(app, ["--json", "task", "list", "--status", "NOPE"])
    assert refused.exit_code == 2, refused.output


def test_entity_list_verbs_json_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = _born_tree(tmp_path, monkeypatch)
    for group, array, _show_key, _key in ENTITIES[1:]:
        answer = _answer(group, "list")
        assert set(answer) == {array}, group
        listed = answer[array]
        assert [row["key"] for row in listed] == sorted(rows[group]), group
        assert all(set(row) == {"key", "urn", "status", "title"} for row in listed), group
    milestones = _answer("milestone", "list")["milestones"]
    assert milestones[0] == {
        "key": "MLS-0001",
        "urn": f"{PREFIX}/milestone/MLS-0001",
        "status": "ACTIVE",
        "title": "First cut",
    }
    assert milestones[1]["title"] is None
    assert _answer("plan", "list")["plan_revisions"] == [
        {"key": "PRV-0001", "urn": None, "status": "APPLIED", "title": None}
    ]
    assert _answer("decision", "list")["decisions"] == [
        {
            "key": "D0001",
            "urn": None,
            "status": "ACTIVE",
            "title": "Read native entities through verbs",
        }
    ]
    assert _answer("action", "list")["actions"][0]["status"] == "WAITING"

    # The text branch lists one line per record.
    text = runner.invoke(app, ["milestone", "list"])
    assert text.exit_code == exit_codes.OK, text.output
    assert text.stdout.splitlines() == ["MLS-0001  ACTIVE  First cut", "MLS-0002  PLANNED"]


def test_entity_list_verbs_on_an_empty_generation_list_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = runner.invoke(
        app, ["--json", "--no-input", "init", "--target", str(tmp_path), "--project-code", "DEMO"]
    )
    assert result.exit_code == exit_codes.OK, result.output
    monkeypatch.delenv("EA_STATE", raising=False)
    monkeypatch.chdir(tmp_path)
    for group, array, _show_key, _key in ENTITIES:
        assert _answer(group, "list") == {array: []}, group
    text = runner.invoke(app, ["task", "list"])
    assert text.exit_code == exit_codes.OK, text.output
    assert text.stdout.strip() == "no tasks"


def test_entity_show_prints_the_stored_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = _born_tree(tmp_path, monkeypatch)
    for group, _array, show_key, key in ENTITIES:
        answer = _answer(group, "show", key)
        assert answer == {show_key: rows[group][key]}, group
    # A Task the document still holds shows its document row.
    assert _answer("task", "show", "DEMO-0001") == {"task": rows["task"]["DEMO-0001"]}

    for group, _array, _show_key, _key in ENTITIES:
        refused = runner.invoke(app, ["--json", group, "show", "NOPE-0001"])
        assert refused.exit_code == exit_codes.USER_ERROR, (group, refused.output)
        body = json.loads(refused.stdout)
        assert body["error"] == "UserError", group
        assert body["data"]["kind"] == "NotFound", group
        assert "NOPE-0001" in body["message"], group

    text = runner.invoke(app, ["plan", "show", "PRV-0001"])
    assert text.exit_code == exit_codes.OK, text.output
    assert json.loads(text.stdout) == rows["plan"]["PRV-0001"]


def test_read_verbs_refuse_a_tree_with_no_native_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir()
    state_path.write_text("{}")
    monkeypatch.setenv("EA_STATE", str(state_path))
    for group, _array, _show_key, key in ENTITIES:
        for argv in (("list",), ("show", key)):
            result = runner.invoke(app, ["--json", group, *argv])
            assert result.exit_code == exit_codes.USER_ERROR, (group, result.output)
            body = json.loads(result.stdout)
            assert body["data"]["kind"] == "NotFound", group
            assert "epoch 2" in body["message"], group


def test_read_verbs_refuse_an_unreadable_generation_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _born_tree(tmp_path, monkeypatch)
    authority = resolve_authority(tmp_path / ".ea")
    assert authority.target is not None and authority.generation_id is not None
    document_path = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    document_path.write_text("[]")
    result = runner.invoke(app, ["--json", "task", "list"])
    assert result.exit_code == exit_codes.USER_ERROR, result.output
    body = json.loads(result.stdout)
    assert body["data"]["kind"] == "InvalidInput"
    assert "generation document unreadable" in body["message"]


def test_read_verbs_need_no_daemon_and_no_v1_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _born_tree(tmp_path, monkeypatch)
    v1_document = (tmp_path / ".ea" / "state.json").resolve()
    assert not v1_document.exists()
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")

    def _no_daemon(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a read verb must not reach the daemon")

    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient.__init__", _no_daemon)
    touched: list[str] = []

    def _recording(name: str, original: Callable[..., Any]) -> Callable[..., Any]:
        def _wrapper(self: Path, *args: Any, **kwargs: Any) -> Any:
            if self.resolve() == v1_document:
                touched.append(name)
            return original(self, *args, **kwargs)

        return _wrapper

    for name in ("open", "read_bytes", "read_text"):
        monkeypatch.setattr(Path, name, _recording(name, getattr(Path, name)))

    for group, array, show_key, key in ENTITIES:
        assert len(_answer(group, "list")[array]) == len(rows[group]), group
        assert _answer(group, "show", key) == {show_key: rows[group][key]}, group
    assert touched == []


def test_summarize_reads_every_stored_row_shape() -> None:
    """Boundary: an imported wrapper, a row with no urn, a blank one and no rows at all."""
    from eawf.observability.reflect.entities import EntitySummary, summarize

    assert summarize(Epoch2Collection.MILESTONE, {}) == ()
    rows = {
        "MLS-0009": {"payload": {"record_key": "MLS-0009"}, "status": "COMPLETED"},
        "PRV-0001": {"key": "PRV-0001", "revision": 1, "status": "APPLIED", "title": "  "},
        "ZZZ": "not a row",
    }
    assert summarize(Epoch2Collection.MILESTONE, rows) == (
        EntitySummary(
            key="MLS-0009", urn="legacy:milestone/MLS-0009", status="COMPLETED", title=None
        ),
        EntitySummary(key="PRV-0001", urn=None, status="APPLIED", title=None),
        EntitySummary(key="ZZZ", urn=None, status=None, title=None),
    )
