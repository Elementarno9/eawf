"""The report hooks file against a native Run on a tree with no v1 ``state.json``.

``eawf hook run agent_end``, ``eawf hook dispatch`` and ``eawf hook agent-output``
name the native Run they speak for. The Run's scope and the role its sealed
capsule grants come from the selected generation, so the hooks keep working on
a tree that never had, or no longer has, the v1 document.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from click.testing import Result
from typer.testing import CliRunner

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.compaction import read_document, write_document
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.agent_report import AgentReportPayload
from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record
from eawf.kernel.store.paths import ledger_path, store_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import hook as hook_module
from eawf.surfaces.render.envelope import OutputEnvelope
from tests.integration.surfaces.cli._native_run import (
    AT,
    PREFIX,
    RUN_KEY,
    RUN_URN,
    TASK_KEY,
    TASK_URN,
    add_run,
    document_path,
    native_repo,
    run_urn,
)

pytestmark = pytest.mark.integration

runner = CliRunner()


@pytest.fixture(autouse=True)
def _drop_inherited_state_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let ``-w``, not an inherited ``EA_STATE``, pick the tree under test."""
    monkeypatch.delenv("EA_STATE", raising=False)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A native tree holding one executor Run, and no v1 document."""
    root = native_repo(tmp_path / "repo")
    assert not (root / ".ea" / "state.json").exists()
    return root


@pytest.fixture
def v1_reads(monkeypatch: pytest.MonkeyPatch, repo: Path) -> Iterator[list[Path]]:
    """Record every open, read_bytes and read_text of the tree's v1 document."""
    v1_document = (repo / ".ea" / "state.json").resolve()
    seen: list[Path] = []

    def spy(name: str) -> Callable[..., Any]:
        real = getattr(Path, name)

        def wrapper(self: Path, *args: Any, **kwargs: Any) -> Any:
            if self.resolve() == v1_document:
                seen.append(self)
            return real(self, *args, **kwargs)

        return wrapper

    for name in ("open", "read_bytes", "read_text"):
        monkeypatch.setattr(Path, name, spy(name))
    yield seen


def _agent_end(run_ref: str = RUN_URN, role: str = "executor") -> dict[str, Any]:
    body: dict[str, Any] = {
        "role": "executor",
        "verdict": "pass",
        "confidence": "high",
        "summary": "native report",
        "task_key": TASK_KEY,
        "outcome": "done",
    }
    if role == "auditor":
        body = {
            "role": "auditor",
            "verdict": "pass",
            "confidence": "high",
            "summary": "audit",
            "target_id": TASK_KEY,
        }
    return {"run_ref": run_ref, "base_id": TASK_KEY, "body": body}


def _store_rows(repo: Path, kind: StoreKind) -> list[Envelope]:
    path = store_path(repo / ".ea" / "state.json", kind)
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [Envelope.model_validate_json(line) for line in lines if line.strip()]


def _chunks(repo: Path) -> list[Envelope]:
    return [
        row
        for row in _store_rows(repo, StoreKind.EVENT)
        if row.payload.get("event_type") == "agent.output.chunk"
    ]


def _invoke(repo: Path, *args: str, payload: dict[str, Any]) -> Result:
    return runner.invoke(app, ["-w", str(repo), "hook", *args], input=json.dumps(payload))


def test_report_hooks_run_without_v1_state(repo: Path, v1_reads: list[Path]) -> None:
    reported = _invoke(repo, "run", "agent_end", payload=_agent_end())
    assert reported.exit_code == 0, reported.stdout
    rows = _store_rows(repo, StoreKind.EXECUTOR_REPORT)
    assert len(rows) == 1
    header = AgentReportPayload.model_validate(rows[0].payload).header
    assert (header.session_id, header.scope_id, header.role.value) == (
        RUN_KEY,
        TASK_KEY,
        "executor",
    )

    forwarded = _invoke(
        repo, "agent-output", payload={"run_ref": RUN_URN, "seq": 0, "text": "line one"}
    )
    assert forwarded.exit_code == 0, forwarded.stdout
    chunks = _chunks(repo)
    assert len(chunks) == 1
    assert chunks[0].scope_id == TASK_KEY
    assert chunks[0].payload["session_id"] == RUN_KEY

    assert v1_reads == []
    assert not hasattr(hook_module, "_load_state")


def test_hook_dispatch_files_the_same_report(repo: Path, v1_reads: list[Path]) -> None:
    first = _invoke(repo, "dispatch", "--event-type", "agent_end", payload=_agent_end())
    second = _invoke(repo, "dispatch", "--event-type", "agent_end", payload=_agent_end())
    assert (first.exit_code, second.exit_code) == (0, 0), second.stdout
    bodies = [OutputEnvelope.model_validate_json(r.stdout).body for r in (first, second)]
    assert [body["attempt"] for body in bodies if isinstance(body, dict)] == [1, 2]
    assert len(_store_rows(repo, StoreKind.EXECUTOR_REPORT)) == 2
    assert v1_reads == []


def test_a_compacted_run_reports_from_its_ledger_record(repo: Path) -> None:
    path = document_path(repo)
    document = read_document(path)
    row = document[Epoch2Collection.RUN.value].pop(RUN_KEY)
    write_document(path, document)
    append_ledger_record(
        ledger_path(path, Epoch2Collection.RUN),
        LedgerRecord(
            collection=Epoch2Collection.RUN,
            record_key=RUN_KEY,
            status="COMPLETED",
            recorded_at=AT,
            payload=row,
        ),
    )

    result = _invoke(repo, "run", "agent_end", payload=_agent_end())
    assert result.exit_code == 0, result.stdout
    assert len(_store_rows(repo, StoreKind.EXECUTOR_REPORT)) == 1


@pytest.mark.parametrize("verb", [("run", "agent_end"), ("agent-output",)])
def test_an_unknown_run_is_refused_and_writes_nothing(repo: Path, verb: tuple[str, ...]) -> None:
    missing = run_urn("RUN-00000099")
    payload = (
        _agent_end(run_ref=missing)
        if verb[0] == "run"
        else {"run_ref": missing, "seq": 0, "text": "x"}
    )
    result = _invoke(repo, *verb, payload=payload)
    assert result.exit_code == 1, result.stdout
    assert "RUN-00000099" in result.output
    assert _store_rows(repo, StoreKind.EXECUTOR_REPORT) == []
    assert _chunks(repo) == []


def test_a_run_bound_without_a_capsule_is_refused(repo: Path) -> None:
    add_run(repo, "RUN-00000002", role=None)

    result = _invoke(repo, "run", "agent_end", payload=_agent_end(run_ref=run_urn("RUN-00000002")))
    assert result.exit_code == 1, result.stdout
    assert "capsule" in result.output
    assert _store_rows(repo, StoreKind.EXECUTOR_REPORT) == []


def test_a_body_claiming_another_role_is_refused(repo: Path) -> None:
    result = _invoke(repo, "run", "agent_end", payload=_agent_end(role="auditor"))
    assert result.exit_code == 2, result.stdout
    assert "does not match" in result.output
    assert _store_rows(repo, StoreKind.AUDITOR_REPORT) == []
    assert _store_rows(repo, StoreKind.EXECUTOR_REPORT) == []


@pytest.mark.parametrize(
    "payload",
    [
        {"run_ref": RUN_KEY, "seq": 0, "text": "x"},
        {"run_ref": TASK_URN, "seq": 0, "text": "x"},
        {"session_id": "SES-001", "seq": 0, "text": "x"},
        {"run_ref": f"{PREFIX}/run/RUN-1", "seq": 0, "text": "x"},
        {"run_ref": RUN_URN, "seq": -1, "text": "x"},
    ],
    ids=["bare-key", "task-urn", "v1-session-field", "short-run-key", "negative-seq"],
)
def test_a_malformed_output_payload_is_refused(repo: Path, payload: dict[str, Any]) -> None:
    result = _invoke(repo, "agent-output", payload=payload)
    assert result.exit_code == 1, result.stdout
    assert "rejected" in result.output
    assert _chunks(repo) == []


def test_a_tree_without_a_generation_is_refused(tmp_path: Path) -> None:
    (tmp_path / ".ea").mkdir()
    result = _invoke(tmp_path, "run", "agent_end", payload=_agent_end())
    assert result.exit_code == 1, result.stdout
    assert "generation" in result.output
