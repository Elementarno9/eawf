"""PLAN-040: measured evidence does not cross a publication boundary.

A plan published into another project whose criterion cites a contract
measured in the publishing repository is not reported measured there: the
citation resolver refuses the foreign contract, naming the probe to re-run,
so the approval gate counts the criterion ungrounded exactly as if it were
graded assumed. A contract row recording no environment is citable only
inside its own producing report, so no plan may cite it at all. A contract
measured through named providers transfers only to a plan whose Runs
dispatch to exactly those providers.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from eawf.kernel.config.layered import resolve_dispatch_provider_tuple
from eawf.kernel.spec.measured_contract import MeasurementEnvironment
from eawf.kernel.state.epoch2.plan_revision import PlanBody, PlanRevision, plan_content_digest
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


# ---- a provider-scoped contract transfers only on an exact provider match --


def _provider_scoped(contract_id: str, provider_tuple: list[str]) -> Artifact:
    environment = {**_environment("QR"), "provider_tuple": provider_tuple}
    return _contract(contract_id, {"probe_command": PROBE, "environment": environment})


@pytest.fixture
def providers(consuming: State) -> State:
    """The consuming state, holding contracts measured through named providers."""
    rows = [
        _provider_scoped("MCT-00000011", ["claude"]),
        _provider_scoped("MCT-00000012", ["claude", "codex"]),
    ]
    consuming.artifacts = {**consuming.artifacts, **{row.id: row for row in rows}}
    return consuming


def _resolves_under(state: State, provider_tuple: tuple[str, ...]) -> Any:
    def contract_resolves(ref: str) -> bool:
        try:
            resolve_contract_citation(state, ref, provider_tuple=provider_tuple)
        except UserError:
            return False
        return True

    return contract_resolves


def test_plan_040_a_provider_scoped_contract_transfers_on_an_exact_match(
    providers: State,
) -> None:
    resolved = resolve_contract_citation(
        providers, "MCT-00000012", provider_tuple=("claude", "codex")
    )
    assert resolved.id == "MCT-00000012"


@pytest.mark.parametrize(
    ("contract_id", "plan_providers"),
    [
        ("MCT-00000011", ("codex",)),
        ("MCT-00000011", ("claude", "codex")),
        ("MCT-00000012", ("claude",)),
        ("MCT-00000011", ()),
    ],
    ids=["disjoint", "plan-wider", "plan-narrower", "plan-records-none"],
)
def test_plan_040_a_provider_scoped_contract_is_refused_without_an_exact_match(
    providers: State, contract_id: str, plan_providers: tuple[str, ...]
) -> None:
    with pytest.raises(UserError, match="exact provider match") as caught:
        resolve_contract_citation(providers, contract_id, provider_tuple=plan_providers)
    assert caught.value.kind == "contract_environment_incompatible"
    assert PROBE in str(caught.value)


def test_plan_040_a_provider_mismatch_is_not_reported_measured(providers: State) -> None:
    body = _measured_body("MCT-00000011")
    resolves = _resolves_under(providers, ("codex",))
    ungrounded = ungrounded_approval_criteria(body, contract_is_resolvable=resolves)
    assert [ref.rsplit("/", 1)[1] for ref in ungrounded] == ["CR-01"]
    matching = _resolves_under(providers, ("claude",))
    assert ungrounded_approval_criteria(body, contract_is_resolvable=matching) == ()


def test_plan_040_a_contract_that_is_not_provider_scoped_transfers_to_any_providers(
    providers: State,
) -> None:
    for plan_providers in ((), ("claude",), ("claude", "codex")):
        resolved = resolve_contract_citation(
            providers, "MCT-00000001", provider_tuple=plan_providers
        )
        assert resolved.id == "MCT-00000001"


def test_plan_040_a_citation_read_outside_a_plan_is_not_judged_by_provider(
    providers: State,
) -> None:
    assert resolve_contract_citation(providers, "MCT-00000011").id == "MCT-00000011"


def test_plan_040_the_repository_boundary_still_holds_under_a_matching_provider(
    consuming: State,
) -> None:
    foreign = {**_environment("PUB"), "provider_tuple": ["claude"]}
    consuming.artifacts["MCT-00000013"] = _contract(
        "MCT-00000013", {"probe_command": PROBE, "environment": foreign}
    )
    with pytest.raises(UserError, match="does not transfer across repositories"):
        resolve_contract_citation(consuming, "MCT-00000013", provider_tuple=("claude",))


# ---- the provider tuple is typed in one canonical spelling -----------------


def test_plan_040_an_environment_recorded_before_the_field_is_not_provider_scoped() -> None:
    environment = MeasurementEnvironment.model_validate({**_environment("QR"), "scale_band": "dev"})
    assert environment.provider_tuple == ()


@pytest.mark.parametrize("provider_tuple", [["claude"], ["claude", "codex", "opencode"]])
def test_plan_040_an_environment_accepts_a_sorted_distinct_provider_tuple(
    provider_tuple: list[str],
) -> None:
    payload = {**_environment("QR"), "scale_band": "dev", "provider_tuple": provider_tuple}
    assert MeasurementEnvironment.model_validate(payload).provider_tuple == tuple(provider_tuple)


@pytest.mark.parametrize(
    "provider_tuple",
    [["codex", "claude"], ["claude", "claude"], ["Claude"], [""], "claude"],
    ids=["unsorted", "repeated", "bad-grammar", "empty-id", "not-a-tuple"],
)
def test_plan_040_an_environment_refuses_a_non_canonical_provider_tuple(
    provider_tuple: object,
) -> None:
    payload = {**_environment("QR"), "scale_band": "dev", "provider_tuple": provider_tuple}
    with pytest.raises(ValidationError):
        MeasurementEnvironment.model_validate(payload)


def _revision_payload(**extra: Any) -> dict[str, Any]:
    body = json.loads(BODY.read_text(encoding="utf-8"))
    return {
        "key": "PRV-0040",
        "revision": 1,
        "status": "DRAFT",
        "author": {"principal_kind": "operator", "principal_id": "OP-0001"},
        "created_at": AT.isoformat(),
        "updated_at": AT.isoformat(),
        "content_digest": plan_content_digest(PlanBody.model_validate(body)),
        "base_state_revision": 1,
        "policy_revision": 1,
        "body": body,
        **extra,
    }


def test_plan_040_a_plan_revision_recorded_before_the_field_loads_with_no_providers() -> None:
    assert PlanRevision.model_validate(_revision_payload()).provider_tuple == ()


def test_plan_040_a_plan_revision_refuses_an_unsorted_provider_tuple() -> None:
    with pytest.raises(ValidationError):
        PlanRevision.model_validate(_revision_payload(provider_tuple=["codex", "claude"]))


# ---- the plan's providers are the runtime ladder its Runs dispatch through --


def _configure_runtime(repo: Path, runtime: dict[str, Any]) -> Path:
    config = repo / ".ea" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(yaml.safe_dump({"schema_version": "1.0", "runtime": runtime}))
    return repo


@pytest.mark.parametrize(
    ("runtime", "expected"),
    [
        ({"preference": ["codex", "claude-code"]}, ("claude", "codex")),
        ({"preference": ["opencode"]}, ("opencode",)),
        ({"preference": ["codex", "codex"]}, ("codex",)),
        ({"adapters": ["codex"], "preference": []}, ("codex",)),
    ],
    ids=["whole-ladder-sorted", "single", "repeat-collapses", "adapters-fallback"],
)
def test_plan_040_the_dispatch_provider_tuple_is_the_whole_runtime_ladder(
    tmp_path: Path, runtime: dict[str, Any], expected: tuple[str, ...]
) -> None:
    assert resolve_dispatch_provider_tuple(_configure_runtime(tmp_path, runtime)) == expected


def test_plan_040_an_unconfigured_repository_dispatches_to_the_built_in_default(
    tmp_path: Path,
) -> None:
    assert resolve_dispatch_provider_tuple(tmp_path) == ("claude",)


def test_plan_040_a_runtime_ladder_naming_an_unknown_adapter_is_refused(tmp_path: Path) -> None:
    repo = _configure_runtime(tmp_path, {"preference": ["claude-code", "gemini"]})
    with pytest.raises(ValidationError):
        resolve_dispatch_provider_tuple(repo)
