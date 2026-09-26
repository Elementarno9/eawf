"""The release gate proofs reuse the serving daemon, and plan submit names every finding.

``prove_gates`` must not start a second daemon on the store the serving
daemon owns, so it hands its isolated ``PATH`` to that daemon instead of
spawning one in a throwaway runtime directory; the daemon's proof runner
then gives the ``PATH`` to each proof command. A plan submission refused
by several lens findings names each of them, so one resubmission can
repair them all.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from eawf.workflow.planning.apply import validate_plan_proposal
from eawf.workflow.planning.revision import PlanRefusal
from eawf.workflow.release import checkpoint_receipts
from eawf.workflow.release.pipeline_host import GitHubReleaseHost
from tests.unit.workflow.planning.test_criterion_oracle_lens import (
    AT,
    BATCH,
    TASK,
    _criterion,
    make_body,
    make_document,
    make_proposal,
)

pytestmark = pytest.mark.unit


def test_prove_gates_hands_the_proof_path_to_the_serving_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One ``release receipts`` call, no private runtime dir, no daemon stop."""
    calls: list[tuple[list[str], dict[str, str] | None]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append((list(argv), kwargs.get("env")))
        return SimpleNamespace(returncode=0, stdout=json.dumps({"receipts": []}), stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    host = GitHubReleaseHost(tmp_path, state_path=tmp_path / "state.json")

    assert host.prove_gates("0.7.0.dev3") == {"receipts": []}

    assert len(calls) == 1
    argv, env = calls[0]
    assert argv[-4:-1] == ["receipts", "0.7.0.dev3", "--proof-path"]
    assert "daemon" not in argv
    assert env is None or "EAWF_RUNTIME_DIR" not in env


def test_pinned_worktree_runs_proofs_with_the_named_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner's proof command sees the caller's PATH; ``None`` inherits the daemon's."""
    seen: list[dict[str, str] | None] = []

    def fake_git(repo_root: Path, *args: str) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        seen.append(kwargs.get("env"))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(checkpoint_receipts, "_git", fake_git)
    monkeypatch.setattr("subprocess.run", fake_run)

    class _Command:
        command_id = "PC-01"
        source_sha = "a" * 40
        argv = ("true",)
        timeout_seconds = 5

    for proof_path in ("/isolated/bin", None):
        with checkpoint_receipts.pinned_worktree(
            tmp_path, "a" * 40, proof_path=proof_path
        ) as run_proof:
            assert run_proof(_Command()).passed  # type: ignore[arg-type]
    isolated, inherited = seen
    assert isolated is not None and isolated["PATH"] == "/isolated/bin"
    assert isolated.keys() >= os.environ.keys() - {"PATH"}
    assert inherited is None


def _orphan_criterion(id_: str) -> dict[str, Any]:
    return _criterion(
        id_,
        response={
            "observe": "exits",
            "object": "the described behaviour holds",
            "locus": "pytest",
            "gate_ref": f"not_a_gate_{id_[-1]}",
        },
    )


def test_submit_names_every_blocking_finding() -> None:
    body = make_body(
        tasks=[
            {
                "urn": TASK,
                "batch_ref": BATCH,
                "priority": "P1",
                "intent": "do the described work",
                "write_claims": ["src/eawf/product/base.py"],
                "criteria": [_orphan_criterion("CR-0001"), _orphan_criterion("CR-0002")],
            }
        ]
    )

    outcome = validate_plan_proposal(make_document(), proposal=make_proposal(body), at=AT)

    assert isinstance(outcome, PlanRefusal)
    assert outcome.guard == "plan_lens_criterion_fidelity"
    assert "'CR-0001'" in outcome.detail
    assert "'CR-0002'" in outcome.detail
    assert outcome.detail.count("fails oracle-tier assignment") == 2
