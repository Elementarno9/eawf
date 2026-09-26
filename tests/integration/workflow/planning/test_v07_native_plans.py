"""The committed rc1, rc2 and stable roadmap proposals validate and apply.

Each proposal under ``docs/roadmap/v0.7`` is one native PlanRevision for one
Milestone of the remaining v0.7 release train. They are walked through the
daemon's own submit, approve and apply methods on a disposable epoch-2 tree,
so a proposal a lens refuses, an approval by anyone but an operator, or a
source atom that maps to no criterion reds here before the operator submits
it live.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.epoch2.plan_revision import PlanApproval
from eawf.kernel.store.compaction import read_document
from eawf.platform.install.canary import CanaryProvision
from eawf.workflow.planning.apply import PlanRevisionProposal, grade_plan_grounding
from eawf.workflow.planning.lenses import run_plan_lenses
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    document_path,
    provision,
)
from tests.integration.workflow.planning._v07_plans import (
    COMMITTED_FILES,
    LIVE_SLOT,
    OPERATOR,
    PLAN_NAMES,
    ROADMAP,
    apply,
    approve,
    create_containers,
    foreign_urns,
    land,
    load_proposal,
    submit,
)

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _canary(tmp_path: Path, code: str) -> CanaryProvision:
    """A canary that is its own git repository, with the plans' Track and repository created."""
    canary = provision(tmp_path / code.lower(), code=code)
    _git(canary.root, "init", "-q")
    (canary.root / "README").write_text("canary\n", encoding="utf-8")
    _git(canary.root, "add", "README")
    _git(
        canary.root,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "-q",
        "-m",
        "seed",
    )
    create_containers(canary.root, tmp_path / "runtime")
    return canary


def _collection(canary: CanaryProvision, name: str) -> dict[str, Any]:
    rows = read_document(document_path(canary)).get(name, {})
    assert isinstance(rows, dict)
    return rows


def test_the_roadmap_holds_exactly_the_three_release_milestones() -> None:
    assert sorted(path.stem for path in ROADMAP.glob("*.json")) == sorted(
        (*PLAN_NAMES, "repository", "track")
    )
    titles = [load_proposal(name)["body"]["milestone"]["title"] for name in PLAN_NAMES]
    assert titles == [
        "Cut 0.7.0rc1 with the epoch-1 TUI retired",
        "Cut 0.7.0rc2 on cross-provider, platform and recovery evidence",
        "Publish 0.7.0 against the complete acceptance gate",
    ]
    assert all(len(title) <= 72 and not title.endswith(".") for title in titles)


@pytest.mark.parametrize("name", COMMITTED_FILES)
def test_every_committed_urn_uses_the_live_slot(name: str) -> None:
    document = load_proposal(name)

    assert foreign_urns(document) == []


def test_a_planted_fixture_slot_reds_the_slot_check() -> None:
    planted = load_proposal("rc1")
    planted["body"]["tasks"][0]["urn"] = planted["body"]["tasks"][0]["urn"].replace(
        LIVE_SLOT, "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
    )

    assert foreign_urns(planted) == [planted["body"]["tasks"][0]["urn"]]


@pytest.mark.parametrize("name", PLAN_NAMES)
def test_every_source_atom_maps_to_a_criterion_of_its_plan(name: str) -> None:
    body = PlanRevisionProposal.model_validate(load_proposal(name)).body
    criteria = {criterion.id for task in body.tasks for criterion in task.criteria}

    assert body.source_atoms
    assert body.dropped_atoms == ()
    for atom in body.source_atoms:
        assert atom.mapped_criterion_ids, atom.atom_id
        assert set(atom.mapped_criterion_ids) <= criteria, atom.atom_id
    assert run_plan_lenses(grade_plan_grounding(body)) == ()


def test_a_requirement_id_is_owned_by_at_most_one_plan() -> None:
    owners: dict[str, str] = {}
    for name in PLAN_NAMES:
        for atom in load_proposal(name)["body"]["source_atoms"]:
            assert atom["atom_id"] not in owners, (atom["atom_id"], owners.get(atom["atom_id"]))
            owners[atom["atom_id"]] = name


def test_the_rc1_plan_schedules_the_flag_day_work() -> None:
    body = load_proposal("rc1")["body"]
    intents = " ".join(task["intent"] for task in body["tasks"])
    atoms = {atom["atom_id"] for atom in body["source_atoms"]}

    assert "epoch-1 TUI application" in intents
    assert "Refuse epoch-1 state mutation" in intents
    assert {"REL-021", "LINT-001", "LINT-036", "LINT-037"} <= atoms


def test_the_three_plans_apply_on_one_epoch_2_tree(tmp_path: Path) -> None:
    canary = _canary(tmp_path, "V07PLANS")

    for name in PLAN_NAMES:
        land(name, canary.root, tmp_path / "runtime")

    head = _git(canary.root, "rev-parse", "HEAD")
    assert _collection(canary, "repository")["EAWF"]["head_sha"] == head
    revisions = _collection(canary, "plan_revision")
    tasks = _collection(canary, "task")
    for name in PLAN_NAMES:
        proposal = load_proposal(name)
        record = revisions[proposal["key"]]
        assert record["status"] == "APPLIED"
        assert [binding["head_sha"] for binding in record["head_bindings"]] == [head]
        approval = PlanApproval.model_validate(record["approval"])
        assert approval.approved_by.model_dump() == OPERATOR
        milestone = _collection(canary, "milestone")[proposal["body"]["milestone"]["key"]]
        assert milestone["status"] == "PLANNED"
        for planned in proposal["body"]["tasks"]:
            assert tasks[planned["urn"].rsplit("/", 1)[1]]["status"] == "PLANNED"


@pytest.mark.parametrize("kind", ["service", "team"])
def test_an_approval_by_a_non_operator_principal_is_refused(tmp_path: Path, kind: str) -> None:
    canary = _canary(tmp_path, f"NOTOP{kind.upper()}")
    proposal = load_proposal("rc1")
    assert submit(proposal, canary.root, tmp_path / "runtime")["status"] == "ok"

    answer = approve(
        proposal,
        canary.root,
        tmp_path / "runtime",
        {"principal_kind": kind, "principal_id": "SVC-0001"},
    )

    assert answer["status"] == "error"
    assert answer["errors"][0]["code"] == "schema_validation_failed"
    assert _collection(canary, "plan_revision")[proposal["key"]]["status"] == "VALIDATED"
    refused = apply(proposal, canary.root, tmp_path / "runtime")
    assert refused["status"] == "error"
    assert _collection(canary, "milestone") == {}


def test_a_non_operator_approval_raises_at_the_model() -> None:
    proposal = load_proposal("stable")
    with pytest.raises(ValueError, match="requires a operator principal"):
        PlanApproval.model_validate(
            {
                "action_ref": f"{proposal['body']['milestone_urn'].rsplit('/milestone/', 1)[0]}"
                "/pending-action/ACT-0301",
                "approved_by": {"principal_kind": "service", "principal_id": "SVC-0001"},
                "approved_at": "2026-09-26T00:00:00Z",
                "content_digest": f"sha256:{'c' * 64}",
                "base_state_revision": 1,
                "policy_revision": 1,
            }
        )
