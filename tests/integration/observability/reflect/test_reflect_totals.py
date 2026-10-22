"""Reflect totals and highlighted issues, read off a canary tree's Runs.

Requirement row proved here, by id:

- ``SKILL-031``: the report's money and token figures come from the measurement the
  Runs carry -- the usage readings on their own streams -- and from nothing else; a
  cost no reading priced is shown as unpriced and never as zero.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.runtime.events import RunEventKind, RunEventRecord
from eawf.kernel.runtime.usage import UsagePayload
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.store.compaction import read_document, write_document
from eawf.observability.reflect.issues import HighlightedIssue, IssueKind
from eawf.observability.reflect.report import ReflectReport, load_report
from eawf.observability.reflect.totals import TotalsRow
from eawf.surfaces.cli.app import app
from tests.contract.surfaces.cli._reflect_tree import RUN_PREFIX, event, native_tree, run_row

runner = CliRunner()

TASK_PREFIX = "eawf://WSP-RFL/PRJ-RFL/REP-RFL/task"

#: The fields a totals row carries: the seven money and token facts and its mark.
TOTALS_FIELDS = {
    "scope",
    "cost_microusd",
    "input_tokens",
    "output_tokens",
    "cache_tokens",
    "priced_runs",
    "unpriced_runs",
    "quotable",
}


def usage(key: str, sequence: int, *, cost: int | None, tokens: int = 100) -> RunEventRecord:
    """Return one delta usage reading of Run ``key``, priced at ``cost`` microUSD or unpriced."""
    return event(
        key,
        sequence,
        event_kind=RunEventKind.USAGE_OBSERVED,
        payload=UsagePayload(
            input_tokens=tokens,
            output_tokens=tokens // 2,
            cache_tokens=tokens * 3,
            cost_microusd=cost,
            price_source=None if cost is None else "billed",
            usage_source="provider_receipt",
            is_cumulative=False,
            measurement_quality="measured",
        ),
    )


def own_project(tree_root: Path, *codes: str) -> None:
    """Record ``codes`` as the projects the tree holds."""
    authority = resolve_authority(tree_root)
    assert authority.target is not None and authority.generation_id is not None
    path = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    document = read_document(path)
    document["project"] = {code: {"key": code} for code in codes}
    write_document(path, document)


def on_task(row: dict[str, Any], task: str) -> dict[str, Any]:
    """Return ``row`` scoped to the Task keyed ``task``."""
    return row | {"scope": row["scope"] | {"task_ref": f"{TASK_PREFIX}/{task}"}}


@pytest.fixture
def priced_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a tree with two priced Runs (1000 and 2500 microUSD) and one unpriced Run."""
    monkeypatch.delenv("EA_STATE", raising=False)
    tree_root = native_tree(
        tmp_path,
        [
            on_task(run_row("RUN-00000001"), "RFL-0001"),
            on_task(run_row("RUN-00000002"), "RFL-0002"),
            on_task(run_row("RUN-00000003"), "RFL-0003"),
        ],
        [
            usage("RUN-00000001", 1, cost=400),
            usage("RUN-00000001", 2, cost=600),
            usage("RUN-00000002", 1, cost=2500),
            usage("RUN-00000003", 1, cost=None),
        ],
    )
    own_project(tree_root, "RFL")
    return tree_root


def _run(tree_root: Path, *args: str) -> Any:
    return runner.invoke(
        app, ["-w", str(tree_root.parent), "reflect", "run", "--local-only", *args]
    )


def test_totals_sum_priced_runs_and_mark_unpriced(priced_tree: Path) -> None:
    """3500 microUSD over two priced Runs, one unpriced Run counted apart and printed unpriced."""
    result = _run(priced_tree, "--format", "json")
    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    (row,) = document["totals"]
    assert set(row) == TOTALS_FIELDS
    assert row["scope"] == "project:RFL"
    assert row["cost_microusd"] == 3500
    assert (row["priced_runs"], row["unpriced_runs"]) == (2, 1)
    assert (row["input_tokens"], row["output_tokens"], row["cache_tokens"]) == (400, 200, 1200)
    costs = {line["run_key"]: line["cost_microusd"] for line in document["sessions"]}
    assert costs == {"RUN-00000001": 1000, "RUN-00000002": 2500, "RUN-00000003": None}

    text = _run(priced_tree)
    assert text.exit_code == 0, text.output
    (unpriced,) = [
        line for line in text.output.splitlines() if line.startswith("session RUN-00000003")
    ]
    assert "cost unpriced" in unpriced
    assert "$0.00" not in unpriced
    assert "$0.0010" in text.output and "$0.0025" in text.output
    assert "totals project:RFL cost $0.0035 · priced 2 · unpriced 1" in text.output


def test_totals_of_only_unpriced_runs_state_no_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary: with no priced Run the total cost is null, not zero."""
    monkeypatch.delenv("EA_STATE", raising=False)
    tree_root = native_tree(
        tmp_path, [run_row("RUN-00000001")], [usage("RUN-00000001", 1, cost=None)]
    )
    own_project(tree_root, "RFL")
    result = _run(tree_root, "--format", "json")
    assert result.exit_code == 0, result.output
    (row,) = json.loads(result.output)["totals"]
    assert row["cost_microusd"] is None
    assert (row["priced_runs"], row["unpriced_runs"]) == (0, 1)
    assert "$0.00" not in _run(tree_root).output


def test_totals_are_empty_on_an_empty_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Boundary: no session, no totals row and no issue."""
    monkeypatch.delenv("EA_STATE", raising=False)
    tree_root = native_tree(tmp_path, [], [])
    document = json.loads(_run(tree_root, "--format", "json").output)
    assert (document["totals"], document["issues"], document["sessions"]) == ([], [], [])


def test_a_totals_row_refuses_an_unknown_field() -> None:
    """Error path: the totals row is closed."""
    with pytest.raises(ValidationError):
        TotalsRow.model_validate(
            {
                "scope": "project:RFL",
                "cost_microusd": 1,
                "input_tokens": None,
                "output_tokens": None,
                "cache_tokens": None,
                "priced_runs": 1,
                "unpriced_runs": 0,
                "quotable": True,
                "variance": 0,
            }
        )


def test_highlighted_issues_are_typed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Schema 2 carries typed issues, and the document reflect run writes validates."""
    monkeypatch.delenv("EA_STATE", raising=False)
    failed = run_row("RUN-00000002", status="FAILED") | {
        "failure": {"code": "provider-lost", "message": "the provider went away"}
    }
    tree_root = native_tree(
        tmp_path,
        [
            on_task(run_row("RUN-00000001"), "RFL-0001"),
            on_task(failed, "RFL-0001"),
            on_task(run_row("RUN-00000003"), "RFL-0002"),
        ],
        [usage("RUN-00000003", 1, cost=None)],
    )
    own_project(tree_root, "RFL")
    result = runner.invoke(app, ["--json", "-w", str(tmp_path), "reflect", "run", "--local-only"])
    assert result.exit_code == 0, result.output
    report = load_report(Path(json.loads(result.output)["result"]["document"]))
    assert report.schema_version == 2
    by_kind = {issue.kind: issue for issue in report.issues}
    assert set(by_kind) == {IssueKind.RUN_FAILED, IssueKind.UNPRICED_SPEND, IssueKind.TASK_REWORKED}
    assert str(by_kind[IssueKind.RUN_FAILED].subject) == f"{RUN_PREFIX}/RUN-00000002"
    assert by_kind[IssueKind.RUN_FAILED].evidence_rows == ("RUN-00000002",)
    assert by_kind[IssueKind.UNPRICED_SPEND].evidence_rows == ("RUN-00000003",)
    reworked = by_kind[IssueKind.TASK_REWORKED]
    assert str(reworked.subject) == f"{TASK_PREFIX}/RFL-0001"
    assert reworked.evidence_rows == ("RUN-00000001", "RUN-00000002")
    assert all(issue.quotable for issue in report.issues)
    dumped = report.model_dump(mode="json")
    assert ReflectReport.model_validate(dumped) == report
    assert {"subject", "evidence_rows", "quotable"} <= set(dumped["issues"][0])


@pytest.mark.parametrize(
    ("subject", "rows"),
    [
        (f"{RUN_PREFIX}/RUN-00000001", ()),
        ("eawf://WSP-RFL/PRJ-RFL/REP-RFL/repository/RFL", ("RUN-00000001",)),
        (f"{RUN_PREFIX}/RUN-00000001", ("RUN-1",)),
    ],
    ids=["no-evidence", "subject-of-another-kind", "malformed-run-key"],
)
def test_an_issue_refuses_an_unbacked_or_mistyped_row(subject: str, rows: tuple[str, ...]) -> None:
    """Error path: an issue names a Run, Task, Batch or Milestone and at least one Run row."""
    with pytest.raises(ValidationError):
        HighlightedIssue.model_validate(
            {"kind": "run_failed", "subject": subject, "evidence_rows": rows, "quotable": True}
        )


def test_a_schema_1_report_is_refused() -> None:
    """Error path: a report of the earlier shape does not validate as the current one."""
    with pytest.raises(ValidationError):
        ReflectReport.model_validate(
            {
                "schema_version": 1,
                "projects": [],
                "projection_revision": 0,
                "title_fill": "disabled",
                "sessions": [],
            }
        )
