"""RUN-009: a required capability that is not verified denies dispatch.

The provider-neutral fixture ``capability-unknown.json`` states the four
ways a required capability fails to be verified -- nobody observed it, the
evidence lapsed, it is only available degraded, and the bound driver does
not declare it at all -- plus the one state that admits an unattended Run.
Each row drives the registered dispatch verb against a provisioned canary
and asserts the denial together with the thing that makes the denial
worth having: no workspace was leased and no launcher was called.

The lapsed case is driven by evidence that expires an hour before the
fixed dispatch stamp, so the margin is an hour of recorded time rather
than a race against a clock. Nothing here sleeps.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any, Final, Literal, Self

import pytest
from pydantic import BaseModel, ConfigDict, model_validator

from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.native_dispatch import DispatchParams, compile_for_dispatch
from eawf.runtime.workspace.lease import root_leases
from eawf.workflow.runtime.compile import CompileRejection
from tests import _provider_helpers as fx
from tests.integration.runtime.daemon.test_native_dispatch import (
    AT,
    LedgerReadingLauncher,
    dispatch,
    dispatch_params,
    make_canary,
    method_ctx,
    root_ctx,
)

pytestmark = pytest.mark.integration


#: Where the provider-neutral conformance fixtures live.
FIXTURE_ROOT: Final = Path(__file__).resolve().parents[3] / "fixtures" / "runtime_contract" / "v1"

#: The stamp the fixture's lapsed evidence is measured against.
LAPSED_AT: Final = AT - timedelta(hours=1)

#: A stamp comfortably inside the evidence window.
CURRENT_AT: Final = AT + timedelta(days=30)


class CaseExpectation(BaseModel):
    """What the fixture says one capability state produces."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: Literal["denied", "admitted"]
    capability_status: Literal["verified", "degraded", "expired", "unknown", "unsupported"]
    code: str | None = None

    @model_validator(mode="after")
    def _a_denial_names_its_code(self) -> Self:
        """Require a code exactly on a denial.

        Raises:
            ValueError: A denial names no code, or an admission names one,
                either of which would assert against an answer the
                compiler never produces.
        """
        if (self.verdict == "denied") != (self.code is not None):
            raise ValueError(f"a {self.verdict} case disagrees with its code")
        return self


class CapabilityCase(BaseModel):
    """One capability state the fixture pins an outcome for."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    summary: str
    manifest_declares: bool
    observation: Literal["verified", "degraded", "expired", "absent"]
    expected: CaseExpectation


class DispatchFixture(BaseModel):
    """The provider-neutral capability fixture as it is filed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["runtime-dispatch/v1"]
    fixture_id: str
    summary: str
    required_capability: str
    cases: tuple[CapabilityCase, ...]

    @model_validator(mode="after")
    def _every_state_is_covered(self) -> Self:
        """Require the fixture to state both verdicts.

        Raises:
            ValueError: The fixture pins only denials or only admissions,
                which would let a compiler that denies everything, or one
                that denies nothing, pass it.
        """
        verdicts = {case.expected.verdict for case in self.cases}
        if verdicts != {"denied", "admitted"}:
            raise ValueError(f"a capability fixture states both verdicts, not {sorted(verdicts)}")
        return self


def load_fixture() -> DispatchFixture:
    """Return the capability fixture this suite is driven by."""
    return DispatchFixture.model_validate(
        json.loads((FIXTURE_ROOT / "capability-unknown.json").read_text(encoding="utf-8"))
    )


FIXTURE: Final = load_fixture()


def observation_for(case: CapabilityCase, capability_id: str) -> dict[str, Any] | None:
    """Return the capability observation *case* describes, or ``None``."""
    if case.observation == "absent":
        return None
    if case.observation == "degraded":
        return fx.observation(
            capability_id,
            status="degraded",
            certification_evidence_ref=None,
            verified_at=None,
            expires_at=None,
            degradation_workflow_ref="workflow://degraded/replay",
        ).model_dump(mode="json")
    expires = LAPSED_AT if case.observation == "expired" else CURRENT_AT
    return fx.observation(capability_id, expires_at=expires.isoformat()).model_dump(mode="json")


def binding_for(case: CapabilityCase) -> Any:
    """Return the runtime binding the fixture case describes.

    The required capability is dropped from the manifest, from the
    observations, or from neither, so the compiler sees exactly the state
    the fixture names and nothing is asserted about a state nobody built.
    """
    capability_id = FIXTURE.required_capability
    manifest = fx.manifest_document()
    if not case.manifest_declares:
        manifest["capabilities"] = [
            row for row in manifest["capabilities"] if row["capability_id"] != capability_id
        ]
    others = [
        fx.observation(other, expires_at=CURRENT_AT.isoformat()).model_dump(mode="json")
        for other in (*fx.REQUIRED, "usage_receipts")
        if other != capability_id
    ]
    described = observation_for(case, capability_id)
    return fx.binding(
        manifest=manifest,
        capabilities=others if described is None else [*others, described],
    )


def case_ids() -> list[str]:
    """Return the fixture's case identifiers, for readable test names."""
    return [case.case_id for case in FIXTURE.cases]


@pytest.mark.parametrize("case", FIXTURE.cases, ids=case_ids())
def test_required_capability_state_decides_unattended_dispatch(
    case: CapabilityCase, tmp_path: Path
) -> None:
    """Only a verified required capability admits an unattended Run."""
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    launcher = LedgerReadingLauncher(canary, runtime)
    params = dispatch_params(canary, bindings=[binding_for(case)])

    if case.expected.verdict == "admitted":
        answer = dispatch(method_ctx(runtime), canary, launcher, params=params)
        assert answer["stage"] == "announced"
        assert len(launcher.calls) == 1
        return

    with pytest.raises(DaemonValidationError) as caught:
        dispatch(method_ctx(runtime), canary, launcher, params=params)
    assert str(case.expected.code) in str(caught.value)
    assert FIXTURE.required_capability in str(caught.value)


@pytest.mark.parametrize("case", FIXTURE.cases, ids=case_ids())
def test_a_denied_dispatch_leases_nothing_and_spawns_nothing(
    case: CapabilityCase, tmp_path: Path
) -> None:
    """The denial lands before a worktree exists or a process starts."""
    if case.expected.verdict != "denied":
        pytest.skip("the admitted case leases and spawns by design")
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    launcher = LedgerReadingLauncher(canary, runtime)

    with pytest.raises(DaemonValidationError):
        dispatch(
            method_ctx(runtime),
            canary,
            launcher,
            params=dispatch_params(canary, bindings=[binding_for(case)]),
        )

    assert launcher.calls == []
    assert root_leases(root_ctx(canary, runtime)) == ()


@pytest.mark.parametrize("case", FIXTURE.cases, ids=case_ids())
def test_a_denied_dispatch_writes_no_attempt_and_no_binding(
    case: CapabilityCase, tmp_path: Path
) -> None:
    """A refused compile leaves the run ledger exactly as it was."""
    if case.expected.verdict != "denied":
        pytest.skip("the admitted case records its attempt by design")
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"

    with pytest.raises(DaemonValidationError):
        dispatch(
            method_ctx(runtime),
            canary,
            LedgerReadingLauncher(canary, runtime),
            params=dispatch_params(canary, bindings=[binding_for(case)]),
        )

    from tests.integration.runtime.daemon.test_native_dispatch import ledger_records

    assert ledger_records(canary, runtime) == ()


def test_the_compiler_names_the_capability_rule_that_refused(tmp_path: Path) -> None:
    """The refusal carries the compiler's own code, not a re-coded one."""
    canary = make_canary(tmp_path / "repo")
    unknown = next(case for case in FIXTURE.cases if case.case_id == "unknown")

    args = DispatchParams.model_validate(
        {
            key: value
            for key, value in dispatch_params(canary, bindings=[binding_for(unknown)]).items()
            if key != "repo_root"
        }
    )

    with pytest.raises(DaemonValidationError) as caught:
        compile_for_dispatch(args, now=AT)
    assert CompileRejection.REQUIRED_CAPABILITY_UNAVAILABLE.value in str(caught.value)


def test_a_provider_configuration_that_does_not_load_refuses_the_dispatch(
    tmp_path: Path,
) -> None:
    """A layer the loader rejects denies before the compiler is reached."""
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    launcher = LedgerReadingLauncher(canary, runtime)
    params = dispatch_params(canary)
    params["provider_documents"]["repository"] = {"routes": [fx.task_route_document()]}

    with pytest.raises(DaemonValidationError, match="dispatch_provider_config_invalid"):
        dispatch(method_ctx(runtime), canary, launcher, params=params)
    assert launcher.calls == []
    assert root_leases(root_ctx(canary, runtime)) == ()


def test_an_unknown_configuration_layer_is_refused_by_name(tmp_path: Path) -> None:
    """A layer nobody declares never reaches the provider loader."""
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    params = dispatch_params(canary)
    params["provider_documents"]["invented"] = {}

    with pytest.raises(DaemonValidationError, match="not a provider configuration layer"):
        dispatch(method_ctx(runtime), canary, LedgerReadingLauncher(canary, runtime), params=params)


def test_an_attended_dispatch_is_not_what_this_rule_governs(tmp_path: Path) -> None:
    """A required capability denies whether or not an operator is present.

    The unattended flag gates the model pin, not the capability rule, so
    an attended Run carrying the same unverified requirement is refused
    for the same reason.
    """
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    unknown = next(case for case in FIXTURE.cases if case.case_id == "unknown")
    params = dispatch_params(canary, bindings=[binding_for(unknown)])
    params["compile_request"]["unattended"] = False

    with pytest.raises(DaemonValidationError) as caught:
        dispatch(method_ctx(runtime), canary, LedgerReadingLauncher(canary, runtime), params=params)
    assert CompileRejection.REQUIRED_CAPABILITY_UNAVAILABLE.value in str(caught.value)


def test_an_uncertified_model_denies_an_unattended_dispatch(tmp_path: Path) -> None:
    """The other unattended gate is the model pin, and it denies too."""
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    launcher = LedgerReadingLauncher(canary, runtime)
    binding = fx.binding(certified_models=[])
    params = dispatch_params(canary, bindings=[binding])

    with pytest.raises(DaemonValidationError) as caught:
        dispatch(method_ctx(runtime), canary, launcher, params=params)
    assert CompileRejection.MODEL_UNCERTIFIED.value in str(caught.value)
    assert launcher.calls == []


def test_the_fixture_states_every_capability_state_the_compiler_has() -> None:
    """The fixture is not silently narrower than the status vocabulary."""
    described = {case.expected.capability_status for case in FIXTURE.cases}
    assert described == {"verified", "degraded", "expired", "unknown", "unsupported"}


def test_the_fixture_refuses_a_denial_without_a_code() -> None:
    """A fixture row that denies without naming a code is not loadable."""
    with pytest.raises(ValueError, match="disagrees with its code"):
        CaseExpectation.model_validate({"verdict": "denied", "capability_status": "unknown"})


def test_the_fixture_refuses_an_admission_carrying_a_code() -> None:
    """An admitted row naming a refusal code is not loadable either."""
    with pytest.raises(ValueError, match="disagrees with its code"):
        CaseExpectation.model_validate(
            {"verdict": "admitted", "capability_status": "verified", "code": "x"}
        )


def test_the_fixture_refuses_a_state_outside_the_closed_vocabulary() -> None:
    """A capability status the compiler cannot produce is not loadable."""
    with pytest.raises(ValueError, match="capability_status"):
        CaseExpectation.model_validate(
            {"verdict": "denied", "capability_status": "maybe", "code": "x"}
        )


def test_lapsed_evidence_is_measured_against_the_supplied_stamp(tmp_path: Path) -> None:
    """The lapse is a recorded hour, not a race against the wall clock.

    The same binding admits the Run when the compile stamp is inside the
    evidence window and denies it when the stamp is past the lapse, so the
    difference the fixture's ``expired`` row asserts is the stamp alone.
    """
    assert LAPSED_AT < AT < CURRENT_AT
    canary = make_canary(tmp_path / "repo")
    expired = next(case for case in FIXTURE.cases if case.case_id == "expired")
    args = DispatchParams.model_validate(
        {
            key: value
            for key, value in dispatch_params(canary, bindings=[binding_for(expired)]).items()
            if key != "repo_root"
        }
    )

    assert compile_for_dispatch(args, now=LAPSED_AT - timedelta(minutes=1)).model_id
    with pytest.raises(DaemonValidationError, match="required_capability_unavailable"):
        compile_for_dispatch(args, now=AT)
