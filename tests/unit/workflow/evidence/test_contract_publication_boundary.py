"""PLAN-040: measured evidence does not cross a publication boundary.

A plan published into another project whose criterion cites a contract
measured in the publishing repository is not reported measured there: the
citation resolver refuses the foreign contract, naming the probe to re-run,
so the approval gate counts the criterion ungrounded exactly as if it were
graded assumed. A contract row recording no environment is citable only
inside its own producing report, so no plan may cite it at all.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.epoch2.plan_revision import PlanBody
from eawf.kernel.state.models import Artifact, State
from eawf.surfaces.cli.errors import UserError
from eawf.workflow.evidence import _io
from eawf.workflow.evidence.measured_contract import CONTRACT_BODY_URI, resolve_contract_citation
from eawf.workflow.planning.revision import ungrounded_approval_criteria

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures"
STATE = FIXTURES / "states" / "valid" / "01-empty-repo.json"
BODY = FIXTURES / "planning" / "grounding" / "single_criterion_body.json"
PROBE = "uv run python probes/replay_order.py"
AT = datetime(2026, 9, 18, tzinfo=UTC)


def _environment(repository: str | None) -> dict[str, Any]:
    return {
        "scale_band": "thousands",
        "population": "every recorded replay log",
        "population_size": 1200,
        "host_platform": "linux",
        "toolchain": "python 3.14.3",
        "repository": repository,
    }


def _contract(contract_id: str, metadata: dict[str, Any], uri: str = CONTRACT_BODY_URI) -> Artifact:
    return Artifact(
        id=contract_id,
        kind="plan_spec",
        uri=uri,
        urn=f"urn:eawf:v1:artifact:QR/{contract_id}",
        created_at=AT,
        metadata=metadata,
    )


@pytest.fixture
def consuming(tmp_path: Path) -> State:
    """The consuming project's state, holding contracts published into it."""
    target = tmp_path / "state.json"
    shutil.copy(STATE, target)
    state = _io.load_state(target)
    assert state.project is not None and state.project.code == "QR"
    rows = [
        _contract("MCT-00000001", {"probe_command": PROBE, "environment": _environment("QR")}),
        _contract("MCT-00000002", {"probe_command": PROBE, "environment": _environment("PUB")}),
        _contract("MCT-00000003", {"probe_command": PROBE}),
        _contract("MCT-00000004", {"probe_command": PROBE, "environment": _environment(None)}),
        _contract("ART-0005", {}, uri="repo:docs/plan.md"),
    ]
    state.artifacts = {row.id: row for row in rows}
    return state


def _resolves(state: State) -> Any:
    def contract_resolves(ref: str) -> bool:
        try:
            resolve_contract_citation(state, ref)
        except UserError:
            return False
        return True

    return contract_resolves


def _measured_body(contract_id: str) -> PlanBody:
    payload = json.loads(BODY.read_text(encoding="utf-8"))
    payload["tasks"][0]["criteria"][0].update(grounding="measured", contract_refs=[contract_id])
    return PlanBody.model_validate(payload)


def test_plan_040_a_contract_measured_here_stays_measured(consuming: State) -> None:
    body = _measured_body("MCT-00000001")
    assert ungrounded_approval_criteria(body, contract_is_resolvable=_resolves(consuming)) == ()


def test_plan_040_a_foreign_repository_contract_is_never_admissible(consuming: State) -> None:
    with pytest.raises(UserError) as caught:
        resolve_contract_citation(consuming, "MCT-00000002")
    assert caught.value.kind == "contract_environment_incompatible"
    assert PROBE in str(caught.value)


def test_plan_040_a_published_plan_citing_a_foreign_contract_is_not_reported_measured(
    consuming: State,
) -> None:
    body = _measured_body("MCT-00000002")
    ungrounded = ungrounded_approval_criteria(body, contract_is_resolvable=_resolves(consuming))
    assert [ref.rsplit("/", 1)[1] for ref in ungrounded] == ["CR-01"]


def test_plan_040_a_contract_with_no_environment_is_citable_only_in_its_report(
    consuming: State,
) -> None:
    with pytest.raises(UserError, match="only inside the report that produced it") as caught:
        resolve_contract_citation(consuming, "MCT-00000003")
    assert caught.value.kind == "contract_environment_incompatible"
    body = _measured_body("MCT-00000003")
    assert ungrounded_approval_criteria(body, contract_is_resolvable=_resolves(consuming))


def test_plan_040_an_unrestricted_environment_transfers(consuming: State) -> None:
    assert resolve_contract_citation(consuming, "MCT-00000004").id == "MCT-00000004"


def test_plan_040_a_non_contract_artifact_is_not_judged_as_a_measurement(
    consuming: State,
) -> None:
    assert resolve_contract_citation(consuming, "ART-0005").id == "ART-0005"


def test_plan_040_an_unreachable_contract_is_not_reported_measured(consuming: State) -> None:
    body = _measured_body("MCT-00000099")
    assert ungrounded_approval_criteria(body, contract_is_resolvable=_resolves(consuming))
