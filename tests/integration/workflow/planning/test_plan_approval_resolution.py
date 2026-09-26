"""H1/M4: approval resolves every grounding citation, or refuses closed.

Two failure modes share one fix. F1: an accepted-risk criterion's
``accepted_risk_decision_ref`` was only checked for presence, so an id
naming no recorded Decision (``DEC-bogus``) reached ``APPROVED``. F10: a
v1 ``state.json`` that exists but fails to load was treated the same as
one that never existed, admitting every ``measured`` and ``accepted-risk``
citation instead of refusing them.

Both are proved here through the real ``PLAN-SUBMIT`` and ``PLAN-APPROVE``
daemon verbs over a provisioned canary, the same route an operator drives:
what is asserted is that the shipped approval gate resolves a citation
against the recorded Decisions and the promoted-contract store, and fails
closed rather than open when that store cannot be read.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods.planning import PLAN_APPROVE_METHOD, PLAN_SUBMIT_METHOD
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    BATCH_URN,
    MILESTONE_URN,
    TASK_URN,
    method_context,
    provision,
    seed,
    seed_row,
    tree_root,
)

pytestmark = pytest.mark.integration

SLOT: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
REPOSITORY_URN: Final = f"{SLOT}/repository/REP-EAWF"
ACTION_URN: Final = f"{SLOT}/pending-action/ACT-0060"
HEAD: Final = "a" * 40
ACTOR: Final = "OP-0001"
REVISION_KEY: Final = "PRV-0060"
OPERATOR: Final[dict[str, str]] = {"principal_kind": "operator", "principal_id": ACTOR}

V1_STATE_FIXTURE: Final = (
    Path(__file__).resolve().parents[3] / "fixtures" / "states" / "valid" / "01-empty-repo.json"
)
V1_STATE_SCOPE: Final = "QR"


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


def _planned_canary(tmp_path: Path, *, code: str) -> CanaryProvision:
    """Provision a canary holding the Track and repository the plan binds."""
    canary = provision(tmp_path / code.lower(), code=code)
    seed(
        canary,
        {
            "track": {"TRK-RUNTIME": seed_row("track", "ACTIVE")},
            "repository": {"REP-EAWF": {"key": "REP-EAWF", "head_sha": HEAD}},
        },
    )
    return canary


def _decision_row(decision_id: str) -> dict[str, Any]:
    """Return one loader-valid, recorded-active Decision payload keyed *decision_id*."""
    return {
        "id": decision_id,
        "scope_id": V1_STATE_SCOPE,
        "title": "Ship the accepted risk unverified",
        "rationale": "verifying it now costs more than the risk of shipping it unverified",
        "status": "active",
        "created_at": "2026-09-25T00:00:00Z",
    }


def _write_v1_state(
    canary: CanaryProvision, *, decisions: dict[str, dict[str, Any]] | None = None
) -> None:
    """Write a v1 ``state.json`` beside *canary*'s epoch-2 tree, optionally recording Decisions."""
    payload = json.loads(V1_STATE_FIXTURE.read_text(encoding="utf-8"))
    if decisions is not None:
        payload["decisions"] = decisions
    (tree_root(canary) / "state.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_corrupt_v1_state(canary: CanaryProvision) -> None:
    """Write a ``state.json`` that exists but does not parse as JSON."""
    (tree_root(canary) / "state.json").write_text("{not valid json", encoding="utf-8")


def _criterion(
    *,
    grounding: str,
    accepted_risk_decision_ref: str | None = None,
    contract_refs: list[str] | None = None,
) -> dict[str, Any]:
    """Return one loader-valid criterion payload graded *grounding*."""
    payload: dict[str, Any] = {
        "id": "CR-01",
        "text": "the accepted risk is knowingly shipped unverified",
        "kind": "functional_suitability",
        "acceptance_style": "binary",
        "evidence_kind": "deterministic",
        "quality_dimension": "functional_suitability",
        "measurable_signal": "uv run pytest tests/unit/kernel/state exits zero",
        "grounding": grounding,
    }
    if accepted_risk_decision_ref is not None:
        payload["accepted_risk_decision_ref"] = accepted_risk_decision_ref
    if contract_refs is not None:
        payload["contract_refs"] = contract_refs
    return payload


def _body(criterion: dict[str, Any]) -> dict[str, Any]:
    """Return a loader-valid single-Task plan body payload carrying *criterion*."""
    return {
        "milestone_urn": MILESTONE_URN,
        "milestone": {
            "key": "MLS-0030",
            "primary_track_ref": f"{SLOT}/track/TRK-RUNTIME",
            "title": "Ship a knowingly accepted risk",
            "outcome": "An operator observes the described behaviour end to end.",
            "appetite": "S",
            "exclusions": ["none"],
            "acceptance_journey": [
                {
                    "step_id": "AS-01",
                    "actor": "operator",
                    "action": "observe the described behaviour",
                    "expected_observation": "the behaviour is present",
                    "evidence_kinds": ["artifact"],
                }
            ],
        },
        "batches": [{"urn": BATCH_URN, "repository_ref": REPOSITORY_URN}],
        "tasks": [
            {
                "urn": TASK_URN,
                "batch_ref": BATCH_URN,
                "priority": "P1",
                "intent": "do the described work",
                "criteria": [criterion],
            }
        ],
        "citations": [],
    }


def _dispatch(
    method: str, canary: CanaryProvision, tmp_path: Path, *, params: dict[str, Any]
) -> dict[str, Any]:
    """Drive one planning verb against *canary* and return the envelope."""
    ctx = method_context(tmp_path / "runtime")
    return asyncio.run(methods.dispatch(method, ctx, {"repo_root": str(canary.root), **params}))


def _submit(canary: CanaryProvision, tmp_path: Path, *, body: dict[str, Any]) -> dict[str, Any]:
    """Submit *body* under the canonical revision key and return the envelope."""
    return _dispatch(
        PLAN_SUBMIT_METHOD,
        canary,
        tmp_path,
        params={
            "proposal": {"key": REVISION_KEY, "author": OPERATOR, "body": body},
            "actor": ACTOR,
            "idempotency_key": "req-submit",
        },
    )


def _approve(canary: CanaryProvision, tmp_path: Path) -> dict[str, Any]:
    """Seal the operator approval over the submitted revision and return the envelope."""
    return _dispatch(
        PLAN_APPROVE_METHOD,
        canary,
        tmp_path,
        params={
            "key": REVISION_KEY,
            "expected_revision": 2,
            "action_ref": ACTION_URN,
            "approved_by": OPERATOR,
            "actor": ACTOR,
            "idempotency_key": "req-approve",
        },
    )


# ---- CR-01 / H1: accepted-risk Decision refs resolve against v1 state ------


def test_approval_resolves_an_accepted_risk_decision_ref_recorded_in_v1_state(
    tmp_path: Path,
) -> None:
    """A Decision id actually recorded in ``state.json`` clears approval."""
    canary = _planned_canary(tmp_path, code="DECRSLV")
    _write_v1_state(canary, decisions={"D67": _decision_row("D67")})
    body = _body(_criterion(grounding="accepted-risk", accepted_risk_decision_ref="D67"))
    assert _submit(canary, tmp_path, body=body)["status"] == "ok"

    answer = _approve(canary, tmp_path)

    assert answer["status"] == "ok"


def test_approval_refuses_an_accepted_risk_criterion_whose_decision_ref_is_unrecorded(
    tmp_path: Path,
) -> None:
    """Gate-fire proof: an unrecorded Decision id (e.g. DEC-bogus) no longer reaches APPROVED.

    Reverting the resolver wiring in ``approve_plan_revision`` reds this
    test: the plan is otherwise well-formed and the only unresolved
    binding is the Decision citation.
    """
    canary = _planned_canary(tmp_path, code="DECBOGUS")
    _write_v1_state(canary, decisions={"D67": _decision_row("D67")})
    body = _body(_criterion(grounding="accepted-risk", accepted_risk_decision_ref="DEC-BOGUS"))
    assert _submit(canary, tmp_path, body=body)["status"] == "ok"

    answer = _approve(canary, tmp_path)

    assert answer["status"] == "error"
    assert answer["errors"][0]["code"] == "transition_guard_failed"
    assert answer["errors"][0]["guard"] == "criteria_grounded_at_approval"


@pytest.mark.parametrize(
    ("status", "extra"),
    [
        ("superseded", {"superseded_by": "D68"}),
        ("obsolete", {"obsoleted_at": "2026-09-25T01:00:00Z"}),
        ("reversed", {}),
    ],
)
def test_approval_refuses_an_accepted_risk_criterion_whose_decision_no_longer_stands(
    tmp_path: Path, status: str, extra: dict[str, Any]
) -> None:
    """Gate-fire proof: a recorded but superseded or obsolete Decision refuses.

    The Decision key is present in ``state.decisions``, so a resolver
    checking presence alone would approve; reverting the status check
    reds this test.
    """
    canary = _planned_canary(tmp_path, code=f"DEC{status.upper()[:6]}")
    _write_v1_state(canary, decisions={"D67": {**_decision_row("D67"), "status": status, **extra}})
    body = _body(_criterion(grounding="accepted-risk", accepted_risk_decision_ref="D67"))
    assert _submit(canary, tmp_path, body=body)["status"] == "ok"

    answer = _approve(canary, tmp_path)

    assert answer["status"] == "error"
    assert answer["errors"][0]["code"] == "transition_guard_failed"
    assert answer["errors"][0]["guard"] == "criteria_grounded_at_approval"


def test_approval_refuses_an_active_decision_that_names_a_successor(tmp_path: Path) -> None:
    """Boundary: an ACTIVE row that already names ``superseded_by`` no longer stands."""
    canary = _planned_canary(tmp_path, code="DECSUCC")
    _write_v1_state(canary, decisions={"D67": {**_decision_row("D67"), "superseded_by": "D68"}})
    body = _body(_criterion(grounding="accepted-risk", accepted_risk_decision_ref="D67"))
    assert _submit(canary, tmp_path, body=body)["status"] == "ok"

    answer = _approve(canary, tmp_path)

    assert answer["status"] == "error"
    assert answer["errors"][0]["guard"] == "criteria_grounded_at_approval"


def test_approval_admits_an_accepted_risk_criterion_when_v1_state_is_absent(
    tmp_path: Path,
) -> None:
    """A tree with no v1 state.json has nothing to resolve against, so it admits.

    Matches the existing ``measured``/``contract_refs`` skip semantics:
    a tree that predates the store (or never provisioned one) cannot
    honestly refuse a citation it has no way to look up.
    """
    canary = _planned_canary(tmp_path, code="NOV1STATE")
    body = _body(_criterion(grounding="accepted-risk", accepted_risk_decision_ref="DEC-ANY"))
    assert _submit(canary, tmp_path, body=body)["status"] == "ok"

    answer = _approve(canary, tmp_path)

    assert answer["status"] == "ok"


# ---- CR-01 / M4: an unreadable store fails closed, not open ----------------


def test_approval_refuses_a_measured_criterion_when_v1_state_is_unreadable(
    tmp_path: Path,
) -> None:
    """Gate-fire proof: a state.json that exists but fails to parse fails closed.

    Reverting the fail-closed resolver reds this test: a broken store
    would otherwise be treated the same as a missing one and skip the
    check, admitting a contract citation nobody promoted.
    """
    canary = _planned_canary(tmp_path, code="CORRUPT1")
    _write_corrupt_v1_state(canary)
    body = _body(_criterion(grounding="measured", contract_refs=["MCT-NEVER-PROMOTED"]))
    assert _submit(canary, tmp_path, body=body)["status"] == "ok"

    answer = _approve(canary, tmp_path)

    assert answer["status"] == "error"
    assert answer["errors"][0]["code"] == "transition_guard_failed"
    assert answer["errors"][0]["guard"] == "criteria_grounded_at_approval"


def test_approval_refuses_an_accepted_risk_criterion_when_v1_state_is_unreadable(
    tmp_path: Path,
) -> None:
    """The same fail-closed rule applies to a Decision citation, not only a contract one."""
    canary = _planned_canary(tmp_path, code="CORRUPT2")
    _write_corrupt_v1_state(canary)
    body = _body(_criterion(grounding="accepted-risk", accepted_risk_decision_ref="D67"))
    assert _submit(canary, tmp_path, body=body)["status"] == "ok"

    answer = _approve(canary, tmp_path)

    assert answer["status"] == "error"
    assert answer["errors"][0]["code"] == "transition_guard_failed"
    assert answer["errors"][0]["guard"] == "criteria_grounded_at_approval"
