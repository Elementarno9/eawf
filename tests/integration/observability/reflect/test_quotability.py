"""Every session and totals row of a reflection report says whether it may be quoted.

Requirement row proved here, by id:

- ``SKILL-035``: a statistic scoped to the current project is quotable into a committed
  artifact, and a row that is not is marked non-quotable in the report itself.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app
from tests.contract.surfaces.cli._reflect_tree import native_tree, run_row
from tests.integration.observability.reflect.test_reflect_totals import own_project, usage

runner = CliRunner()


def other_project(row: dict[str, Any]) -> dict[str, Any]:
    """Return ``row`` re-addressed to the project ``OTH``."""
    return row | {"urn": row["urn"].replace("/PRJ-RFL/", "/PRJ-OTH/")}


def _report(tree_root: Path) -> dict[str, Any]:
    result = runner.invoke(
        app,
        ["-w", str(tree_root.parent), "reflect", "run", "--local-only", "--format", "json"],
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_own_project_rows_are_quotable_and_cross_project_rows_are_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The RFL session and totals row are quotable; the OTH ones are marked non-quotable."""
    monkeypatch.delenv("EA_STATE", raising=False)
    tree_root = native_tree(
        tmp_path,
        [run_row("RUN-00000001"), other_project(run_row("RUN-00000002"))],
        [usage("RUN-00000001", 1, cost=10), usage("RUN-00000002", 1, cost=20)],
    )
    own_project(tree_root, "RFL")
    document = _report(tree_root)
    assert {line["run_key"]: line["quotable"] for line in document["sessions"]} == {
        "RUN-00000001": True,
        "RUN-00000002": False,
    }
    assert {row["scope"]: row["quotable"] for row in document["totals"]} == {
        "project:OTH": False,
        "project:RFL": True,
    }
    text = runner.invoke(app, ["-w", str(tree_root.parent), "reflect", "run", "--local-only"])
    rows = [line for line in text.output.splitlines() if line.startswith(("session ", "totals "))]
    assert len(rows) == 4
    assert all(line.endswith(("· quotable", "· non-quotable")) for line in rows)
    assert [line for line in rows if line.endswith("· non-quotable")] == [
        line for line in rows if "RUN-00000002" in line or "project:OTH" in line
    ]


def test_a_tree_that_records_no_project_quotes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary: a tree with no project row owns no Run, so no row is quotable."""
    monkeypatch.delenv("EA_STATE", raising=False)
    tree_root = native_tree(tmp_path, [run_row("RUN-00000001")], [])
    document = _report(tree_root)
    assert [line["quotable"] for line in document["sessions"]] == [False]
    assert [row["quotable"] for row in document["totals"]] == [False]
