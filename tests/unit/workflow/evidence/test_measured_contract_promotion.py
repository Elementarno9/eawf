"""PLAN-024/025: a verified SpikeReport is the sole vector onto the evidence path.

Two mechanisms are covered here and nothing else needs a daemon to check
them, because both are pure functions of a :class:`State` object.

:func:`submit_evidence` generalises :func:`promote_measured_contract` past
the four hard-coded preflight contracts: any verified
:class:`SpikeReport` may submit its contracts, each landing as one
artifact revision at its recorded content digest. A report that is not
verified, or a contract measured in a repository other than the
submitting one, is refused before anything is written -- the two
gate-fire proofs CR-01 names. A contract with a blank ``boundary`` never
reaches ``submit_evidence`` at all: :class:`MeasuredContract` refuses it
at the schema boundary.

:func:`resolve_contract_citation` carries the same environment refusal on
the read side, so a citation naming a contract measured elsewhere is
refused at the point it would be cited, not only at the point it was
submitted.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.runtime.semantic import SemanticToolId
from eawf.kernel.spec.measured_contract import (
    MeasuredContract,
    MeasurementEnvironment,
    ObservedLimit,
    ScaleBand,
    SpikeReport,
)
from eawf.kernel.state.models import State
from eawf.runtime.daemon.semantic_handlers import SEMANTIC_HANDLERS
from eawf.surfaces.cli.errors import UserError
from eawf.workflow.evidence._io import load_state
from eawf.workflow.evidence.measured_contract import (
    PREFLIGHT_CONTRACTS,
    promote_measured_contract,
    resolve_contract_citation,
    submit_evidence,
)

pytestmark = pytest.mark.unit

FIXTURE = (
    Path(__file__).resolve().parents[3] / "fixtures" / "states" / "valid" / "01-empty-repo.json"
)

#: The fixture project code; artifact URNs and the environment check are
#: built from it.
SCOPE = "QR"

_ENVIRONMENT: dict[str, Any] = {
    "scale_band": "dev",
    "population": "one synthetic surface probed for this test",
    "population_size": 1,
    "host_platform": "darwin",
    "toolchain": "python 3.14.3",
}

_LIMIT: dict[str, Any] = {
    "name": "widget_count",
    "value": 3.0,
    "unit": "count",
    "direction": "ceiling",
    "basis": "one synthetic probe run",
}


def make_contract(contract_id: str, **overrides: Any) -> MeasuredContract:
    """Return a well-formed :class:`MeasuredContract` for *contract_id*."""
    fields: dict[str, Any] = {
        "contract_id": contract_id,
        "surface": "a synthetic surface probed for this test",
        "probe_command": "uv run python probe.py",
        "observed": {"widget_count": 3},
        "limits": (ObservedLimit.model_validate(_LIMIT),),
        "boundary": "measured once, on one synthetic population",
        "observed_at": datetime(2026, 9, 25, tzinfo=UTC),
        "observed_at_ref": "tests/fixtures/measured_contract/probe-output.json",
        "environment": MeasurementEnvironment.model_validate(_ENVIRONMENT),
    }
    fields.update(overrides)
    return MeasuredContract.model_validate(fields)


def make_report(
    *contracts: MeasuredContract, verified: bool = True, report_id: str = "RPT-0001"
) -> SpikeReport:
    """Return a :class:`SpikeReport` carrying *contracts*."""
    return SpikeReport(report_id=report_id, verified=verified, contracts=contracts)


@pytest.fixture
def state() -> State:
    """A fresh, unmutated :class:`State` loaded from the fixture."""
    return load_state(FIXTURE)


# ---- submit_evidence: the general promotion path ---------------------------


def test_submit_evidence_promotes_each_contract_as_one_artifact_revision(state: State) -> None:
    report = make_report(make_contract("MCT-99990001"), make_contract("MCT-99990002"))

    promotions = submit_evidence(state, report=report, scope_id=SCOPE)

    assert [promotion.artifact_id for promotion in promotions] == ["MCT-99990001", "MCT-99990002"]
    for promotion in promotions:
        assert promotion.urn == f"urn:eawf:v1:artifact:{SCOPE}/{promotion.artifact_id}"
        row = state.artifacts[promotion.artifact_id]
        assert row.metadata["boundary"].strip()
        assert row.sha256 == promotion.content_digest.removeprefix("sha256:")


def test_submit_evidence_returns_empty_for_a_report_that_measured_nothing(state: State) -> None:
    """Boundary: a spike that discriminated between designs without probing anything."""
    report = make_report(report_id="RPT-EMPTY")

    assert submit_evidence(state, report=report, scope_id=SCOPE) == ()
    assert state.artifacts == load_state(FIXTURE).artifacts


def test_submit_evidence_mints_one_evidence_row_per_contract(state: State) -> None:
    report = make_report(make_contract("MCT-99990003"))

    (promotion,) = submit_evidence(state, report=report, scope_id=SCOPE)

    assert promotion.evidence.refs == ["MCT-99990003", promotion.urn]
    assert promotion.evidence.metrics["contract_id"] == "MCT-99990003"


def test_submit_evidence_records_the_digest_add_artifact_stored(state: State) -> None:
    """CR-02: the promoted row is addressed by its recorded content digest."""
    report = make_report(make_contract("MCT-99990004"))

    (promotion,) = submit_evidence(state, report=report, scope_id=SCOPE)

    assert promotion.content_digest.startswith("sha256:")
    assert len(promotion.content_digest.removeprefix("sha256:")) == 64
    assert state.artifacts["MCT-99990004"].sha256 == promotion.content_digest.removeprefix(
        "sha256:"
    )


# ---- CR-01, gate-fire proof 1: an unverified report is refused -------------


def test_submit_evidence_refuses_an_unverified_report(state: State) -> None:
    report = make_report(make_contract("MCT-99990005"), verified=False)

    with pytest.raises(UserError) as excinfo:
        submit_evidence(state, report=report, scope_id=SCOPE)

    assert excinfo.value.kind == "spike_report_unverified"
    assert "MCT-99990005" not in state.artifacts


# ---- CR-01, gate-fire proof 2: a blank boundary raises ValidationError -----


def test_measured_contract_refuses_an_empty_boundary() -> None:
    with pytest.raises(ValidationError, match="at least 1 character"):
        make_contract("MCT-99990006", boundary="")


def test_measured_contract_refuses_a_whitespace_only_boundary() -> None:
    with pytest.raises(ValidationError, match="blank"):
        make_contract("MCT-99990006", boundary="   ")


# ---- CR-01: an incompatible environment is refused, at submission ---------


def test_submit_evidence_refuses_a_contract_measured_in_another_repository(state: State) -> None:
    foreign = make_contract(
        "MCT-99990007",
        environment=MeasurementEnvironment.model_validate({**_ENVIRONMENT, "repository": "OTHER"}),
    )
    report = make_report(foreign)

    with pytest.raises(UserError) as excinfo:
        submit_evidence(state, report=report, scope_id=SCOPE)

    assert excinfo.value.kind == "contract_environment_incompatible"
    assert "MCT-99990007" not in state.artifacts


def test_submit_evidence_admits_a_contract_measured_in_the_submitting_repository(
    state: State,
) -> None:
    local = make_contract(
        "MCT-99990008",
        environment=MeasurementEnvironment.model_validate({**_ENVIRONMENT, "repository": SCOPE}),
    )
    report = make_report(local)

    (promotion,) = submit_evidence(state, report=report, scope_id=SCOPE)

    assert promotion.artifact_id == "MCT-99990008"


def test_submit_evidence_refuses_the_whole_report_before_writing_any_contract(
    state: State,
) -> None:
    """A later contract's refusal leaves an earlier one in the same report unwritten."""
    ok = make_contract("MCT-99990009")
    foreign = make_contract(
        "MCT-99990010",
        environment=MeasurementEnvironment.model_validate({**_ENVIRONMENT, "repository": "OTHER"}),
    )
    report = make_report(ok, foreign)

    with pytest.raises(UserError) as excinfo:
        submit_evidence(state, report=report, scope_id=SCOPE)

    assert excinfo.value.kind == "contract_environment_incompatible"
    assert "MCT-99990009" not in state.artifacts
    assert "MCT-99990010" not in state.artifacts


# ---- CR-01: an incompatible environment is refused, at citation -----------


def test_resolve_contract_citation_refuses_a_citation_from_another_repository(
    state: State,
) -> None:
    foreign = make_contract(
        "MCT-99990011",
        environment=MeasurementEnvironment.model_validate({**_ENVIRONMENT, "repository": "OTHER"}),
    )
    promote_measured_contract(
        state, contract=foreign, scope_id="OTHER", required_band=ScaleBand.DEV
    )

    with pytest.raises(UserError) as excinfo:
        resolve_contract_citation(state, "MCT-99990011")

    assert excinfo.value.kind == "contract_environment_incompatible"


def test_resolve_contract_citation_admits_a_citation_with_no_repository_restriction(
    state: State,
) -> None:
    """Regression: the four preflight contracts carry no repository, so are unaffected."""
    contract = PREFLIGHT_CONTRACTS["MCT-26081303"]
    assert contract.environment.repository is None
    promote_measured_contract(state, contract=contract, scope_id=SCOPE, required_band=ScaleBand.DEV)

    resolved = resolve_contract_citation(state, "MCT-26081303")

    assert resolved.id == "MCT-26081303"


def test_resolve_contract_citation_admits_a_matching_repository(state: State) -> None:
    local = make_contract(
        "MCT-99990012",
        environment=MeasurementEnvironment.model_validate({**_ENVIRONMENT, "repository": SCOPE}),
    )
    promote_measured_contract(state, contract=local, scope_id=SCOPE, required_band=ScaleBand.DEV)

    resolved = resolve_contract_citation(state, "MCT-99990012")

    assert resolved.id == "MCT-99990012"


# ---- SpikeReport: the sole vector for a MeasuredContract -------------------


def test_spike_report_defaults_to_no_contracts() -> None:
    report = SpikeReport(report_id="RPT-0002", verified=True)

    assert report.contracts == ()


def test_spike_report_rejects_a_blank_report_id() -> None:
    with pytest.raises(ValidationError, match="blank"):
        SpikeReport(report_id="   ", verified=True)


def test_spike_report_rejects_an_unknown_field() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        SpikeReport.model_validate({"report_id": "RPT-0003", "verified": True, "notes": "x"})


def test_spike_report_is_frozen() -> None:
    report = make_report(report_id="RPT-0004")
    with pytest.raises(ValidationError, match="frozen"):
        report.verified = False  # type: ignore[misc]


# ---- error paths ------------------------------------------------------------


def test_submit_evidence_propagates_a_duplicate_promotion_refusal(state: State) -> None:
    report = make_report(make_contract("MCT-99990013"))
    submit_evidence(state, report=report, scope_id=SCOPE)

    with pytest.raises(UserError, match="already exists"):
        submit_evidence(state, report=report, scope_id=SCOPE)


def test_measurement_environment_repository_is_optional() -> None:
    environment = MeasurementEnvironment.model_validate(_ENVIRONMENT)

    assert environment.repository is None


# ---- daemon wiring: submit_evidence is a brokered semantic tool -------------


def test_submit_evidence_is_registered_in_semantic_handlers() -> None:
    """W06 gate-fire proof: ``SUBMIT_EVIDENCE`` stays wired into the daemon.

    Every other test in this module drives :func:`submit_evidence` as a
    pure function of a :class:`State`, so none of them would notice
    ``SemanticToolId.SUBMIT_EVIDENCE`` being dropped from
    :data:`SEMANTIC_HANDLERS` -- the daemon's only route to it. Removing
    that entry reds this test without touching anything else here.
    """
    assert SemanticToolId.SUBMIT_EVIDENCE in SEMANTIC_HANDLERS
