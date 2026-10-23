"""The Task prompt names where a worker starts and asks it to batch its reads."""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.kernel.spec.common import CriterionSpec, ObserveVerb, ProofLocus, ResponseClause
from tests.unit.workflow.runtime.test_task_request import (
    CRITERIA,
    EXECUTOR_BODY,
    build,
    criterion,
    make_task,
)

pytestmark = pytest.mark.unit

PARALLEL_READS = (
    "Issue independent reads together in one turn: several repo_read and repo_search "
    "calls in one response, not one call per turn."
)


def with_locus(criterion_id: str, locus: str) -> CriterionSpec:
    """Return a criterion whose response clause names *locus* as its proof object."""
    base = criterion(criterion_id, f"criterion {criterion_id} holds")
    response = ResponseClause(observe=ObserveVerb.EXITS, object=locus, locus=ProofLocus.PYTEST)
    return base.model_copy(update={"response": response})


def where_to_start(prompt: str) -> list[str]:
    """Return the bullet lines of the prompt's ``## Where to start`` section."""
    section = prompt.split("## Where to start", 1)[1].split("## Changing files", 1)[0]
    return [line[2:] for line in section.splitlines() if line.startswith("- ")]


def test_where_to_start_lists_claims_then_each_criterion_locus_once_in_order() -> None:
    task = make_task(
        write_claims=["src/eawf/a.py", "tests/unit/test_a.py"],
        criteria=[
            with_locus("CR-01", "src/eawf/a.py:10 (render)"),
            criterion("CR-02", "a criterion with no response clause"),
            with_locus("CR-03", "tests/unit/test_a.py::test_a"),
            with_locus("CR-04", "src/eawf/a.py:10 (render)"),
        ],
    )
    prompt = build(task).prompt
    assert where_to_start(prompt) == [
        "src/eawf/a.py",
        "tests/unit/test_a.py",
        "src/eawf/a.py:10 (render)",
        "tests/unit/test_a.py::test_a",
    ]
    assert prompt.index("## Write claims") < prompt.index("## Where to start")
    assert prompt.index("## Where to start") < prompt.index("## Changing files")


def test_where_to_start_lists_write_claims_alone_without_response_clauses() -> None:
    assert where_to_start(build().prompt) == ["src/eawf/workflow/runtime"]


def test_where_to_start_with_a_claim_also_named_as_a_locus_lists_it_once() -> None:
    task = make_task(
        write_claims=["src/eawf/a.py"], criteria=[with_locus("CR-01", "src/eawf/a.py")]
    )
    assert where_to_start(build(task).prompt) == ["src/eawf/a.py"]


def test_parallel_reads_sentence_sits_in_where_to_start() -> None:
    prompt = build().prompt
    section = prompt.split("## Where to start", 1)[1].split("## Changing files", 1)[0]
    assert "Open these paths first" in section
    assert PARALLEL_READS in section
    assert prompt.count(PARALLEL_READS) == 1


def test_where_to_start_prompt_keeps_section_order_without_absolute_paths(tmp_path: Path) -> None:
    prompt = build(make_task(criteria=[with_locus("CR-01", "src/eawf/a.py:1"), CRITERIA[1]])).prompt
    marks = [
        EXECUTOR_BODY.rstrip(),
        "# Task EAWF-0042",
        "## Intent",
        "## Criteria",
        "## Write claims",
        "## Where to start",
        "## Changing files",
        "## Finish",
    ]
    positions = [prompt.index(mark) for mark in marks]
    assert positions == sorted(positions)
    assert len(prompt) <= 200_000
    assert str(tmp_path) not in prompt
    assert str(Path.cwd()) not in prompt
