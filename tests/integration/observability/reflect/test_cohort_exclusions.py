"""Prior reflection sessions and the helper sessions they spawn stay out of the baseline.

Requirement row proved here, by id:

- ``SKILL-032``: the report persists statistics and metadata only, and keeps prior runs
  of /reflect and every helper session out of the baseline: each is listed in the
  excluded section with its reason and counted in no session or totals row.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.kernel.runtime.events import MessageSummaryPayload, RunEventKind, RunEventRecord
from eawf.observability.reflect import report as rp
from eawf.observability.reflect.runs import read_tree_runs
from eawf.observability.reflect.titles import REFLECTION_MARKER
from eawf.surfaces.cli.app import app
from tests.contract.surfaces.cli._reflect_tree import RUN_PREFIX, event, native_tree, run_row
from tests.integration.observability.reflect.test_reflect_totals import own_project, usage

runner = CliRunner()


def asked(key: str, sequence: int, text: str) -> RunEventRecord:
    """Return the line summarizing a user message of Run ``key``."""
    return event(
        key,
        sequence,
        event_kind=RunEventKind.MESSAGE_SUMMARIZED,
        payload=MessageSummaryPayload(message_role="user", summary=text),
    )


def delegated(row: dict[str, Any], parent: str) -> dict[str, Any]:
    """Return ``row`` as a Run the Run keyed ``parent`` delegated."""
    return row | {"parent_run_ref": f"{RUN_PREFIX}/{parent}"}


@pytest.fixture
def mixed_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a tree with one baseline Run, one prior reflection and one title helper."""
    monkeypatch.delenv("EA_STATE", raising=False)
    tree_root = native_tree(
        tmp_path,
        [run_row("RUN-00000001"), run_row("RUN-00000002"), run_row("RUN-00000003")],
        [
            asked("RUN-00000001", 1, "fix the loader"),
            usage("RUN-00000001", 2, cost=1000),
            asked("RUN-00000002", 1, "/reflect run --local-only"),
            usage("RUN-00000002", 2, cost=700),
            asked("RUN-00000003", 1, f"{REFLECTION_MARKER} write a title"),
            usage("RUN-00000003", 2, cost=300),
        ],
    )
    own_project(tree_root, "RFL")
    return tree_root


def test_reflect_and_helper_runs_are_excluded(mixed_tree: Path) -> None:
    """Both land in excluded with their reason, and in no session or totals row."""
    result = runner.invoke(
        app,
        ["-w", str(mixed_tree.parent), "reflect", "run", "--local-only", "--format", "json"],
    )
    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    assert [(row["run_key"], row["reason"]) for row in document["excluded"]] == [
        ("RUN-00000002", "reflect_session"),
        ("RUN-00000003", "helper_session"),
    ]
    assert [row["cost_microusd"] for row in document["excluded"]] == [700, 300]
    assert [line["run_key"] for line in document["sessions"]] == ["RUN-00000001"]
    (row,) = document["totals"]
    assert (row["cost_microusd"], row["priced_runs"], row["unpriced_runs"]) == (1000, 1, 0)
    assert all("RUN-00000002" not in issue["evidence_rows"] for issue in document["issues"])


def test_a_run_a_reflection_delegated_is_a_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child of a prior reflection is a helper session even with no message of its own."""
    monkeypatch.delenv("EA_STATE", raising=False)
    tree_root = native_tree(
        tmp_path,
        [run_row("RUN-00000001"), delegated(run_row("RUN-00000002"), "RUN-00000001")],
        [asked("RUN-00000001", 1, "/eawf:reflect run")],
    )
    cohort = rp.select_cohort(
        read_tree_runs(tree_root), scope=rp.ReportScope(), window_days=None, now=datetime.now(UTC)
    )
    assert cohort.sessions == ()
    assert [(item.run.key, reason) for item, reason in cohort.excluded] == [
        ("RUN-00000001", "reflect_session"),
        ("RUN-00000002", "helper_session"),
    ]


@pytest.mark.parametrize(
    "text",
    ["", "please /reflect on this", "/reflection notes", "/reflectrun"],
    ids=["no-request", "mid-sentence", "longer-word", "glued-action"],
)
def test_a_request_that_does_not_open_with_the_skill_stays_in_the_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    """Boundary: only a request opening with the skill's own name is a prior reflection."""
    monkeypatch.delenv("EA_STATE", raising=False)
    events = [asked("RUN-00000001", 1, text)] if text else []
    tree_root = native_tree(tmp_path, [run_row("RUN-00000001")], events)
    cohort = rp.select_cohort(
        read_tree_runs(tree_root), scope=rp.ReportScope(), window_days=None, now=datetime.now(UTC)
    )
    assert [item.run.key for item in cohort.sessions] == ["RUN-00000001"]
    assert cohort.excluded == ()
