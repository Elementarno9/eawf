"""Agent report bodies name native entities, and epoch-1 rows still read.

A report body says which Task, Milestone or Tasks it speaks for. New
bodies spell those fields with the native names and refuse the epoch-1
ones, so an agent cannot write a row the epoch-1 vocabulary describes.
Rows already stored under the old names stay readable: the stored-row
reader upgrades them once, in memory, and never rewrites the file.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import AgentSessionRole
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.agent_report import (
    AgentReportPayload,
    ExecutorReportBody,
    OperatorReportBody,
    PlannedTaskSummary,
    PlannerReportBody,
    store_kind_for_role,
)
from eawf.kernel.store.paths import store_path
from eawf.runtime.runtimes.claude.plugin_package import package_plugin
from eawf.workflow.agent_report.rollup import find_agent_report, iter_agent_reports
from eawf.workflow.agent_report.store import (
    ReportAuthor,
    append_report_as,
    parse_agent_report_body,
)

_COMMON: Final[dict[str, Any]] = {
    "verdict": "pass",
    "confidence": "high",
    "summary": "done",
}

_EXAMPLE_BLOCK: Final = re.compile(r"## Typed output envelope\n.*?```json\n(.*?)\n```", re.DOTALL)


def _executor(**fields: Any) -> dict[str, Any]:
    return {"role": "executor", **_COMMON, "outcome": "landed", **fields}


def _planner(**fields: Any) -> dict[str, Any]:
    return {"role": "planner", **_COMMON, "objective": "plan the work", **fields}


def _operator(**fields: Any) -> dict[str, Any]:
    return {"role": "operator", **_COMMON, **fields}


def _task_summary(**fields: Any) -> dict[str, Any]:
    return {"title": "Task title", **fields}


def test_report_bodies_carry_native_field_names() -> None:
    executor = parse_agent_report_body(_executor(task_key="EAWF-0001"))
    planner = parse_agent_report_body(
        _planner(tasks=[_task_summary(task_key="EAWF-0002", depends_on=["EAWF-0001"])])
    )
    operator = parse_agent_report_body(
        _operator(milestone_key="MLS-0001", completed_task_keys=["EAWF-0001", "EAWF-0002"])
    )

    assert isinstance(executor, ExecutorReportBody)
    assert executor.task_key == "EAWF-0001"
    assert isinstance(planner, PlannerReportBody)
    assert planner.tasks == [
        PlannedTaskSummary(task_key="EAWF-0002", title="Task title", depends_on=["EAWF-0001"])
    ]
    assert isinstance(operator, OperatorReportBody)
    assert operator.milestone_key == "MLS-0001"
    assert operator.completed_task_keys == ["EAWF-0001", "EAWF-0002"]
    for model, retired in (
        (ExecutorReportBody, "wave_id"),
        (PlannerReportBody, "waves"),
        (PlannedTaskSummary, "wave_id"),
        (OperatorReportBody, "phase_id"),
        (OperatorReportBody, "completed_wave_ids"),
    ):
        assert retired not in model.model_fields


@pytest.mark.parametrize(
    ("body", "retired"),
    [
        (_executor(wave_id="EAWF-0001"), "wave_id"),
        (_executor(task_key="EAWF-0001", wave_id="EAWF-0001"), "wave_id"),
        (_planner(waves=[]), "waves"),
        (_planner(tasks=[_task_summary(task_key="EAWF-0001", wave_id="EAWF-0001")]), "wave_id"),
        (_operator(milestone_key="MLS-0001", phase_id="MLS-0001"), "phase_id"),
        (_operator(milestone_key="MLS-0001", completed_wave_ids=[]), "completed_wave_ids"),
    ],
)
def test_new_body_naming_an_epoch1_field_is_refused(body: dict[str, Any], retired: str) -> None:
    with pytest.raises(ValidationError) as caught:
        parse_agent_report_body(body)

    forbidden = [err for err in caught.value.errors() if err["type"] == "extra_forbidden"]
    assert [err["loc"][-1] for err in forbidden] == [retired]


@pytest.mark.parametrize(
    "body",
    [
        _executor(),
        _executor(task_key=""),
        _operator(),
        _operator(milestone_key=""),
        _planner(tasks=[_task_summary()]),
        _planner(tasks=[_task_summary(task_key="")]),
        _operator(milestone_key="MLS-0001", completed_task_keys="EAWF-0001"),
    ],
)
def test_native_key_fields_keep_their_constraints(body: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse_agent_report_body(body)


def test_native_list_fields_default_empty() -> None:
    planner = parse_agent_report_body(_planner())
    operator = parse_agent_report_body(_operator(milestone_key="MLS-0001"))

    assert isinstance(planner, PlannerReportBody)
    assert planner.tasks == []
    assert isinstance(operator, OperatorReportBody)
    assert operator.completed_task_keys == []


def _epoch1_envelope(role: str, base_id: str, body: dict[str, Any]) -> dict[str, Any]:
    report_id = f"AR-{role}-{base_id}-01"
    moment = "2026-07-01T12:00:00Z"
    return {
        "id": report_id,
        "kind": f"{role}_report",
        "scope_id": "EAWF",
        "created_at": moment,
        "summary": "done",
        "payload": {
            "header": {
                "report_id": report_id,
                "role": role,
                "session_id": "SES-001",
                "scope_id": "EAWF",
                "base_id": base_id,
                "attempt": 1,
                "runtime": "claude",
                "generated_at": moment,
                "summary": "done",
            },
            "body": body,
        },
    }


def _write_store(state_path: Path, role: AgentSessionRole, rows: list[dict[str, Any]]) -> Path:
    path = store_path(state_path, store_kind_for_role(role))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{json.dumps(row)}\n" for row in rows), encoding="utf-8")
    return path


def _state_path(tmp_path: Path) -> Path:
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    return state_dir / "state.json"


def test_stored_epoch1_report_rows_read_under_native_names(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    executor_path = _write_store(
        state_path,
        AgentSessionRole.EXECUTOR,
        [_epoch1_envelope("executor", "P30-I20-W59", _executor(wave_id="P30-I20-W59"))],
    )
    planner_path = _write_store(
        state_path,
        AgentSessionRole.PLANNER,
        [
            _epoch1_envelope(
                "planner",
                "P30-I20",
                _planner(
                    waves=[
                        _task_summary(wave_id="P30-I20-W01"),
                        _task_summary(wave_id="P30-I20-W02", depends_on=["P30-I20-W01"]),
                    ]
                ),
            )
        ],
    )
    operator_path = _write_store(
        state_path,
        AgentSessionRole.OPERATOR,
        [
            _epoch1_envelope(
                "operator",
                "P30",
                _operator(phase_id="P30", completed_wave_ids=["P30-I20-W01", "P30-I20-W02"]),
            )
        ],
    )
    before = {path: path.read_bytes() for path in (executor_path, planner_path, operator_path)}

    rows = {row.payload.header.role: row.payload.body for row in iter_agent_reports(state_path)}

    executor = rows[AgentSessionRole.EXECUTOR]
    assert isinstance(executor, ExecutorReportBody)
    assert executor.task_key == "P30-I20-W59"
    planner = rows[AgentSessionRole.PLANNER]
    assert isinstance(planner, PlannerReportBody)
    assert [(item.task_key, item.depends_on) for item in planner.tasks] == [
        ("P30-I20-W01", []),
        ("P30-I20-W02", ["P30-I20-W01"]),
    ]
    operator = rows[AgentSessionRole.OPERATOR]
    assert isinstance(operator, OperatorReportBody)
    assert operator.milestone_key == "P30"
    assert operator.completed_task_keys == ["P30-I20-W01", "P30-I20-W02"]
    found = find_agent_report(state_path, "AR-executor-P30-I20-W59-01")
    assert found is not None
    assert found.payload.body == executor

    result = append_report_as(
        ReportAuthor(session_id="SES-002", role=AgentSessionRole.EXECUTOR, scope_id="EAWF"),
        state_path=state_path,
        base_id="P30-I20-W59",
        body=ExecutorReportBody.model_validate(_executor(task_key="EAWF-0001")),
        runtime="claude",
        generated_at=datetime(2026, 7, 2, tzinfo=UTC),
    )

    assert result.attempt == 2
    assert executor_path.read_bytes().startswith(before[executor_path])
    appended = Envelope.model_validate_json(executor_path.read_bytes().splitlines()[-1])
    assert appended.payload["body"]["task_key"] == "EAWF-0001"
    assert "wave_id" not in appended.payload["body"]
    assert planner_path.read_bytes() == before[planner_path]
    assert operator_path.read_bytes() == before[operator_path]


def test_stored_row_naming_both_spellings_is_refused() -> None:
    raw = _epoch1_envelope(
        "executor", "EAWF-0001", _executor(wave_id="EAWF-0001", task_key="EAWF-0002")
    )["payload"]

    with pytest.raises(ValidationError, match="wave_id"):
        AgentReportPayload.model_validate(raw)


def test_stored_native_row_reads_unchanged() -> None:
    raw = _epoch1_envelope("executor", "EAWF-0001", _executor(task_key="EAWF-0001"))["payload"]

    payload = AgentReportPayload.model_validate(raw)

    assert isinstance(payload.body, ExecutorReportBody)
    assert payload.body.task_key == "EAWF-0001"
    assert raw["body"] == _executor(task_key="EAWF-0001")


def test_role_page_examples_validate_against_the_schema(tmp_path: Path) -> None:
    target = tmp_path / "plugin"
    package_plugin(target, include_marketplace=False, include_readme=False, extra_tools={})
    examples: dict[str, Any] = {}
    for page in sorted((target / "agents").glob("*.md")):
        blocks = _EXAMPLE_BLOCK.findall(page.read_text(encoding="utf-8"))
        assert len(blocks) == 1, page.name
        examples[page.stem] = parse_agent_report_body(json.loads(blocks[0]))

    assert {stem: body.role for stem, body in examples.items()} == {
        role.value: role.value for role in AgentSessionRole
    }
    executor = examples["executor"]
    assert isinstance(executor, ExecutorReportBody)
    assert executor.task_key
    planner = examples["planner"]
    assert isinstance(planner, PlannerReportBody)
    assert len(planner.tasks) == 1
    operator = examples["operator"]
    assert isinstance(operator, OperatorReportBody)
    assert operator.milestone_key
    assert len(operator.completed_task_keys) == 1
