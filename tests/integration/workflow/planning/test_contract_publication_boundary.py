"""PLAN-040: a provider-scoped contract transfers only on an exact provider match.

Driven through the real ``PLAN-SUBMIT`` and ``PLAN-APPROVE`` daemon verbs
over a provisioned canary. Submission records the providers the plan's
Runs dispatch to, read from the repository's runtime ladder rather than
from the proposal, and approval judges every ``measured`` citation
against that record: a contract measured through other providers is not
reported measured, so the criterion is refused as ungrounded.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods.planning import PLAN_APPROVE_METHOD, PLAN_SUBMIT_METHOD
from eawf.workflow.evidence.measured_contract import CONTRACT_BODY_URI
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    BATCH_URN,
    MILESTONE_URN,
    TASK_URN,
    document_path,
    method_context,
    provision,
    seed,
    seed_row,
    tree_root,
)

pytestmark = pytest.mark.integration

SLOT: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
REPOSITORY_URN: Final = f"{SLOT}/repository/REP-EAWF"
ACTION_URN: Final = f"{SLOT}/pending-action/ACT-0040"
HEAD: Final = "a" * 40
ACTOR: Final = "OP-0001"
REVISION_KEY: Final = "PRV-0040"
OPERATOR: Final[dict[str, str]] = {"principal_kind": "operator", "principal_id": ACTOR}
CONTRACT_ID: Final = "MCT-26091801"
PROBE: Final = "uv run python probes/replay_order.py"

V1_STATE_FIXTURE: Final = (
    Path(__file__).resolve().parents[3] / "fixtures" / "states" / "valid" / "01-empty-repo.json"
)


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


def _canary(tmp_path: Path, *, code: str, preference: list[str]) -> CanaryProvision:
    """Provision a canary whose runtime ladder is *preference*."""
    canary = provision(tmp_path / code.lower(), code=code)
    seed(
        canary,
        {
            "track": {"TRK-RUNTIME": seed_row("track", "ACTIVE")},
            "repository": {"REP-EAWF": {"key": "REP-EAWF", "head_sha": HEAD}},
        },
    )
    config = tree_root(canary) / "config.yaml"
    document = yaml.safe_load(config.read_text()) if config.exists() else {}
    document.setdefault("schema_version", "1.0")
    document["runtime"] = {"preference": preference}
    config.write_text(yaml.safe_dump(document))
    return canary


def _promote_contract(canary: CanaryProvision, *, provider_tuple: list[str]) -> None:
    """Write a v1 state holding one contract measured through *provider_tuple*."""
    payload = json.loads(V1_STATE_FIXTURE.read_text(encoding="utf-8"))
    payload["artifacts"] = {
        CONTRACT_ID: {
            "id": CONTRACT_ID,
            "kind": "plan_spec",
            "uri": CONTRACT_BODY_URI,
            "urn": f"urn:eawf:v1:artifact:{payload['project']['code']}/{CONTRACT_ID}",
            "created_at": "2026-09-18T00:00:00Z",
            "metadata": {
                "probe_command": PROBE,
                "environment": {
                    "scale_band": "dev",
                    "population": "every recorded replay log",
                    "population_size": 1200,
                    "host_platform": "linux",
                    "toolchain": "python 3.14.3",
                    "repository": None,
                    "provider_tuple": provider_tuple,
                },
            },
        }
    }
    (tree_root(canary) / "state.json").write_text(json.dumps(payload), encoding="utf-8")


def _body() -> dict[str, Any]:
    """Return a single-Task plan body whose one criterion cites the contract."""
    return {
        "milestone_urn": MILESTONE_URN,
        "milestone": {
            "key": "MLS-0030",
            "primary_track_ref": f"{SLOT}/track/TRK-RUNTIME",
            "title": "Ship a measured replay order",
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
                "criteria": [
                    {
                        "id": "CR-01",
                        "text": "replay order holds as the probe measured it",
                        "kind": "functional_suitability",
                        "acceptance_style": "binary",
                        "evidence_kind": "deterministic",
                        "quality_dimension": "functional_suitability",
                        "measurable_signal": "uv run pytest tests/unit/kernel/state exits zero",
                        "grounding": "measured",
                        "contract_refs": [CONTRACT_ID],
                    }
                ],
            }
        ],
        "citations": [],
    }


def _dispatch(
    method: str, canary: CanaryProvision, tmp_path: Path, *, params: dict[str, Any]
) -> dict[str, Any]:
    ctx = method_context(tmp_path / "runtime")
    return asyncio.run(methods.dispatch(method, ctx, {"repo_root": str(canary.root), **params}))


def _submit_and_approve(canary: CanaryProvision, tmp_path: Path) -> dict[str, Any]:
    submitted = _dispatch(
        PLAN_SUBMIT_METHOD,
        canary,
        tmp_path,
        params={
            "proposal": {"key": REVISION_KEY, "author": OPERATOR, "body": _body()},
            "actor": ACTOR,
            "idempotency_key": "req-submit",
        },
    )
    assert submitted["status"] == "ok", submitted
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


def test_plan_040_submission_records_the_providers_the_plan_dispatches_to(
    tmp_path: Path,
) -> None:
    canary = _canary(tmp_path, code="PRVREC", preference=["codex", "claude-code"])
    _promote_contract(canary, provider_tuple=["claude", "codex"])

    _submit_and_approve(canary, tmp_path)

    rows = document_rows(read_document(document_path(canary)), Epoch2Collection.PLAN_REVISION)
    assert rows[REVISION_KEY]["provider_tuple"] == ["claude", "codex"]


def test_plan_040_a_contract_measured_through_exactly_the_plans_providers_is_measured(
    tmp_path: Path,
) -> None:
    canary = _canary(tmp_path, code="PRVMATCH", preference=["codex", "claude-code"])
    _promote_contract(canary, provider_tuple=["claude", "codex"])

    answer = _submit_and_approve(canary, tmp_path)

    assert answer["status"] == "ok", answer


@pytest.mark.parametrize(
    ("preference", "measured_through"),
    [
        (["codex"], ["claude"]),
        (["claude-code", "codex"], ["claude"]),
        (["claude-code"], ["claude", "codex"]),
    ],
    ids=["disjoint", "plan-wider", "plan-narrower"],
)
def test_plan_040_a_contract_measured_through_other_providers_is_not_reported_measured(
    tmp_path: Path, preference: list[str], measured_through: list[str]
) -> None:
    canary = _canary(tmp_path, code="PRVMISS", preference=preference)
    _promote_contract(canary, provider_tuple=measured_through)

    answer = _submit_and_approve(canary, tmp_path)

    assert answer["status"] == "error"
    assert answer["errors"][0]["code"] == "transition_guard_failed"
    assert answer["errors"][0]["guard"] == "criteria_grounded_at_approval"
