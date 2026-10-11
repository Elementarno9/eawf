"""A landed Batch closes only once each criterion naming a verify command is proved at its head.

Each test provisions a canary over a real git repository holding a few committed test
files, seeds ready Tasks whose criteria name a ``uv run pytest`` command in backticks,
and drives the close directly. ``uv`` is a shim that runs pytest with this interpreter,
so the gate runs for real without building an environment for the fixture repository.
The first close starts the proof in the background and answers ``proving``; the test
then closes again until the proof has settled, and reads the outcome back from the
tree: the receipts in the receipt ledger, the Tasks' completion events in the firehose,
and the Batch row.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.spec.common import CriterionSpec
from eawf.kernel.store.ledger import effective_records, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import batch_proof
from eawf.runtime.daemon.batch_close import BatchClosure
from eawf.runtime.daemon.batch_proof import proof_notes, verify_argv
from eawf.runtime.daemon.gate_execution import GateExecutionContext
from eawf.runtime.verification.receipts import VerificationLeg
from eawf.workflow.audit_dsl.models import CheckResult
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    BATCH_URN,
    document_path,
    firehose_path,
    tree_root,
)
from tests.integration.runtime.daemon.test_batch_close import (
    batch,
    close,
    commit,
    git,
    ledger_status,
    live,
    task,
)
from tests.integration.runtime.daemon.test_batch_close import make_tree as make_bare_tree

#: The test files every fixture repository commits, by path.
CHECKS: Final = {
    "tests/test_ok.py": "def test_ok():\n    assert True\n",
    "tests/test_bad.py": "def test_bad():\n    assert False\n",
    "tests/test_slow.py": "import time\n\n\ndef test_slow():\n    time.sleep(30)\n",
    "tests/test_clean.py": (
        "from pathlib import Path\n\n\ndef test_clean():\n"
        "    assert 'dirty' not in Path('src/module.py').read_text()\n"
    ),
    "tests/test_later.py": (
        "from pathlib import Path\n\n\ndef test_later():\n"
        "    assert Path('src/later.py').is_file()\n"
    ),
}


def pytest_of(name: str) -> str:
    """Return the verify command that runs the fixture's ``tests/test_<name>.py``."""
    return f"uv run pytest -q tests/test_{name}.py"


#: A command that passes in any checkout of the fixture repository.
PASSING: Final = pytest_of("ok")

#: A command that fails in any checkout of the fixture repository.
FAILING: Final = pytest_of("bad")


@pytest.fixture
def uv_shim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put a ``uv`` first on ``PATH`` whose ``run pytest`` is this interpreter's pytest."""
    shims = tmp_path / "shims"
    shims.mkdir()
    shim = shims / "uv"
    shim.write_text(
        "#!/bin/sh\n"
        '[ "$1" = run ] && shift\n'
        f'[ "$1" = pytest ] && shift && exec "{sys.executable}" -m pytest -p no:randomly "$@"\n'
        '[ "$1" = python ] && shift\n'
        f'exec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shims}:{os.environ['PATH']}")
    return shim


pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("uv_shim")]


def make_tree(
    tmp_path: Path, *, batches: dict[str, dict[str, Any]], tasks: dict[str, dict[str, Any]]
) -> tuple[CanaryProvision, Path]:
    """Provision the canary of :func:`make_bare_tree`, its repository holding :data:`CHECKS`."""
    canary, repo = make_bare_tree(tmp_path, batches=batches, tasks=tasks)
    for path, text in CHECKS.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text, encoding="utf-8")
    git(repo, "add", "tests")
    git(repo, "commit", "-q", "-m", "test: the checks the criteria run")
    return canary, repo


class NeverRun:
    """A gate runner that records a call it should never have had."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(
        self, leg: VerificationLeg, *, cwd: Path, gate_context: GateExecutionContext
    ) -> CheckResult:
        self.calls += 1
        raise AssertionError(f"ran {leg.contract.gate_id} in {cwd}")


#: How long a test waits for a background proof to settle.
SETTLE_SECONDS: Final = 60.0


def criterion(criterion_id: str, command: str | None) -> dict[str, Any]:
    """Return a criterion proved by *command*, or answered by evidence when it is ``None``."""
    row: dict[str, Any] = {
        "id": criterion_id,
        "text": f"criterion {criterion_id} holds at the landed head",
        "kind": "behavioral",
        "acceptance_style": "binary",
        "evidence_kind": "deterministic",
        "gate_ids": ["G-01"],
        "quality_dimension": "functional_suitability",
        "measurable_signal": f"the evidence bound to {criterion_id} shows it holds",
    }
    if command is not None:
        row["measurable_signal"] = f"`{command}` exits 0 at the landed head."
        row["response"] = {
            "observe": "exits",
            "object": "zero from this criterion's verify command",
            "locus": "pytest",
            "gate_ref": "command_exit_zero",
            "quantifier": "single",
        }
    return row


def proved_task(key: str, *commands: str | None) -> dict[str, Any]:
    """Return a ready Task whose criteria are proved by *commands*, in order."""
    row = task(key, "READY_TO_INTEGRATE")
    number = key.rsplit("-", 1)[1]
    row["criteria"] = [
        criterion(f"CR-{number}-{index}", command) for index, command in enumerate(commands, 1)
    ]
    return row


def settle(canary: CanaryProvision, tmp_path: Path) -> BatchClosure:
    """Close the Batch until its proof no longer answers ``proving``, and return the last try."""
    deadline = time.monotonic() + SETTLE_SECONDS
    closure = close(canary, tmp_path)
    while closure.proof == "proving":
        assert time.monotonic() < deadline, f"the proof did not settle: {closure.reason}"
        time.sleep(0.1)
        closure = close(canary, tmp_path)
    return closure


def receipts(canary: CanaryProvision) -> list[dict[str, Any]]:
    """Return every filed proof in the receipt ledger."""
    lines = read_ledger_records(ledger_path(document_path(canary), Epoch2Collection.RECEIPT))
    return [
        item.payload
        for item in effective_records(lines)
        if item.payload.get("payload_kind") == "proof_receipt"
    ]


def completion_refs(canary: CanaryProvision, key: str) -> list[str]:
    """Return the binding refs of the event that completed Task *key*."""
    for line in firehose_path(canary).read_text(encoding="utf-8").splitlines():
        payload = json.loads(line).get("payload", {})
        if (
            payload.get("entity_ref", "").endswith(f"/{key}")
            and payload.get("to_status") == "COMPLETED"
        ):
            return list(payload["binding_refs"])
    raise AssertionError(f"no completion event for {key}")


# ---- proved criteria close the Batch ------------------------------------------


def test_passing_criteria_file_receipts_and_complete_tasks_citing_them(tmp_path: Path) -> None:
    canary, repo = make_tree(
        tmp_path,
        batches={"BAT-0007": batch("BAT-0007", "PLANNED", ["EAWF-0042", "EAWF-0043"])},
        tasks={
            "EAWF-0042": proved_task("EAWF-0042", PASSING, pytest_of("clean")),
            "EAWF-0043": proved_task("EAWF-0043", PASSING),
        },
    )
    head = commit(repo, "src/alpha.py", "feat: alpha\n\nTask: EAWF-0042")

    first = close(canary, tmp_path)
    closure = settle(canary, tmp_path)

    assert first.proof == "proving"
    assert first.reason == f"proving 3 criteria at {head[:12]}"
    assert closure.closed, closure.reason
    assert closure.proof == "proved"
    assert closure.completed_task_keys == ("EAWF-0042", "EAWF-0043")
    filed = receipts(canary)
    assert sorted(item["receipt"]["criterion_ids"][0] for item in filed) == [
        "CR-0042-1",
        "CR-0042-2",
        "CR-0043-1",
    ]
    for item in filed:
        assert item["receipt"]["result"] == "pass"
        assert item["receipt"]["freshness"]["revision_binding"]["head_sha"] == head
    for key in ("EAWF-0042", "EAWF-0043"):
        assert ledger_status(canary, Epoch2Collection.TASK, key) == "COMPLETED"
        mine = {item["receipt"]["id"] for item in filed if item["task_ref"].endswith(key)}
        cited = {
            ref.rsplit("/", 1)[1] for ref in completion_refs(canary, key) if "/receipt/" in ref
        }
        assert cited == mine
    assert proof_notes(tree_root(canary)) == {}


def test_an_evidence_only_task_completes_without_a_proof(tmp_path: Path) -> None:
    canary, _repo = make_tree(
        tmp_path,
        batches={"BAT-0007": batch("BAT-0007", "PLANNED", ["EAWF-0042"])},
        tasks={"EAWF-0042": proved_task("EAWF-0042", None)},
    )

    closure = close(canary, tmp_path)

    assert closure.closed, closure.reason
    assert closure.proof is None
    assert receipts(canary) == []
    assert ledger_status(canary, Epoch2Collection.TASK, "EAWF-0042") == "COMPLETED"


def test_the_proof_runs_in_a_detached_checkout_so_live_dirt_is_irrelevant(
    tmp_path: Path,
) -> None:
    canary, repo = make_tree(
        tmp_path,
        batches={"BAT-0007": batch("BAT-0007", "PLANNED", ["EAWF-0042"])},
        tasks={"EAWF-0042": proved_task("EAWF-0042", pytest_of("clean"))},
    )
    (repo / "src" / "module.py").write_text("# dirty, never committed\n", encoding="utf-8")

    closure = settle(canary, tmp_path)

    # the check fails on the dirty live tree and passes in a clean checkout of HEAD
    assert closure.closed, closure.reason
    assert git(repo, "diff", "--name-only") == "src/module.py"
    assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1


# ---- an unproved criterion keeps the Batch where it stands ---------------------


def test_a_failing_criterion_keeps_the_batch_open_and_names_it(tmp_path: Path) -> None:
    canary, repo = make_tree(
        tmp_path,
        batches={"BAT-0007": batch("BAT-0007", "PLANNED", ["EAWF-0042", "EAWF-0043"])},
        tasks={
            "EAWF-0042": proved_task("EAWF-0042", PASSING),
            "EAWF-0043": proved_task("EAWF-0043", PASSING, FAILING),
        },
    )
    head = git(repo, "rev-parse", "HEAD")
    before = live(canary, Epoch2Collection.BATCH)["BAT-0007"]

    closure = settle(canary, tmp_path)

    assert not closure.closed
    assert closure.proof == "unmet"
    assert closure.reason == f"CR-0043-2 unmet at {head[:12]}"
    assert closure.completed_task_keys == ()
    assert live(canary, Epoch2Collection.BATCH)["BAT-0007"] == before
    for key in ("EAWF-0042", "EAWF-0043"):
        assert live(canary, Epoch2Collection.TASK)[key]["status"] == "READY_TO_INTEGRATE"
    results = {
        item["receipt"]["criterion_ids"][0]: item["receipt"]["result"] for item in receipts(canary)
    }
    assert results == {"CR-0042-1": "pass", "CR-0043-1": "pass", "CR-0043-2": "fail"}
    assert proof_notes(tree_root(canary)) == {BATCH_URN: closure.reason}


def test_a_criterion_over_its_budget_keeps_the_batch_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(batch_proof, "PROOF_BUDGET_SECONDS", 1)
    canary, repo = make_tree(
        tmp_path,
        batches={"BAT-0007": batch("BAT-0007", "PLANNED", ["EAWF-0042"])},
        tasks={"EAWF-0042": proved_task("EAWF-0042", pytest_of("slow"))},
    )
    head = git(repo, "rev-parse", "HEAD")

    closure = settle(canary, tmp_path)

    assert not closure.closed
    assert closure.proof == "over budget"
    assert closure.reason == f"CR-0042-1 over budget at {head[:12]}"
    (filed,) = receipts(canary)
    assert filed["receipt"]["result"] == "fail"
    assert filed["receipt"]["exit_status"] is None


def test_a_criterion_naming_a_missing_test_file_is_unmet_without_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = NeverRun()
    monkeypatch.setattr(batch_proof, "run_leg", runner)
    canary, repo = make_tree(
        tmp_path,
        batches={"BAT-0007": batch("BAT-0007", "PLANNED", ["EAWF-0042"])},
        tasks={"EAWF-0042": proved_task("EAWF-0042", PASSING, pytest_of("missing"))},
    )
    head = git(repo, "rev-parse", "HEAD")

    closure = close(canary, tmp_path)

    assert closure.proof == "unmet"
    assert closure.reason == (
        f"CR-0042-2 unmet at {head[:12]}: missing test path tests/test_missing.py"
    )
    assert runner.calls == 0
    assert receipts(canary) == []


#: Backticked spans a measurable signal may carry that are prose, placeholders or
#: commands other than pytest over test paths; none of them may ever be run.
NEVER_RUN: Final = (
    "real",
    "target_date",
    "uv run eawf --json batch show <BAT-0105 URN>",
    "rg -n Land one commit per Batch AGENTS.md",
    "git merge-base --is-ancestor <branch> origin/main",
    "git push --force origin main",
    "rm -rf /",
    "bash -c true",
    "pytest -q tests/test_ok.py",
    "uv run pytest -q tests/test_ok.py; rm -rf src",
    "uv run pytest -q tests/test_ok.py && git push",
    "uv run pytest -q tests/../../etc/passwd",
    "uv run pytest -q src/module.py",
    "uv run pytest -q <path>",
    "uv run pytest -q tests/test_ok.py -k $(whoami)",
    "uv run pytest -q --basetemp=/ tests/test_ok.py",
    "uv run pytest -q --rootdir=/ tests/test_ok.py",
    "uv run pytest -q -p evil tests/test_ok.py",
    "uv run pytest -q tests/test_ok.py 'unbalanced",
    "uv run python -c 'import os' tests/test_ok.py",
)


@pytest.mark.parametrize("span", NEVER_RUN)
def test_a_span_that_is_not_pytest_over_test_paths_is_never_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, span: str
) -> None:
    runner = NeverRun()
    monkeypatch.setattr(batch_proof, "run_leg", runner)
    canary, _repo = make_tree(
        tmp_path,
        batches={"BAT-0007": batch("BAT-0007", "PLANNED", ["EAWF-0042"])},
        tasks={"EAWF-0042": proved_task("EAWF-0042", span)},
    )

    closure = close(canary, tmp_path)

    assert closure.closed, closure.reason
    assert closure.proof is None
    assert runner.calls == 0
    assert receipts(canary) == []


# ---- reading the verify command off a criterion -------------------------------


def _spec(command: str | None, **overrides: Any) -> CriterionSpec:
    row = criterion("CR-0001-1", command)
    row.update(overrides)
    return CriterionSpec.model_validate(row)


def _other_gate() -> CriterionSpec:
    row = criterion("CR-0001-1", "git status")
    row["response"]["gate_ref"] = "regex_match"
    return CriterionSpec.model_validate(row)


def test_verify_argv_reads_the_first_backticked_command() -> None:
    signal = f"`{PASSING}` exits 0, unlike `git push`."
    spec = _spec(PASSING, measurable_signal=signal)

    assert verify_argv(spec) == ["uv", "run", "pytest", "-q", "tests/test_ok.py"]


def test_verify_argv_takes_pytest_options_and_node_ids() -> None:
    command = "uv run python -m pytest -q -k 'bindings and not slow' tests/unit/a.py::test_b"

    assert verify_argv(_spec(command)) == [
        "uv",
        "run",
        "python",
        "-m",
        "pytest",
        "-q",
        "-k",
        "bindings and not slow",
        "tests/unit/a.py::test_b",
    ]


def _locus(locus: str) -> CriterionSpec:
    row = criterion("CR-0001-1", PASSING)
    row["response"]["locus"] = locus
    return CriterionSpec.model_validate(row)


@pytest.mark.parametrize(
    "spec",
    [
        pytest.param(_spec(None), id="no-response"),
        pytest.param(_other_gate(), id="other-gate"),
        pytest.param(_locus("source"), id="other-locus"),
        pytest.param(
            _spec(PASSING, measurable_signal="uv run pytest exits 0 with no backticks"),
            id="no-backticks",
        ),
        *(pytest.param(_spec(span), id=f"span-{index}") for index, span in enumerate(NEVER_RUN)),
    ],
)
def test_verify_argv_is_none_for_a_criterion_naming_no_pytest_command(
    spec: CriterionSpec,
) -> None:
    assert verify_argv(spec) is None
