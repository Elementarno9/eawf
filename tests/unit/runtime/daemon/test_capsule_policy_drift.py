"""Sealing a capsule cannot widen the merged tool policy.

``seal_capsule`` takes every enforcement field off the compiled spec except
the tool grants and denials, which arrive on the caller's
:class:`CapsuleRequest`. Those two are the fields authority is actually
enforced on, because the semantic gateway admits a call by reading the sealed
capsule rather than the spec. A caller that assembled them from anything other
than the merged tool policy would seal back exactly the authority the
least-authority merge had just withdrawn, and the run binding would record
that digest as though the merge had produced it.

A capsule stricter than the policy stays legal; only a widening is refused.
"""

from __future__ import annotations

import pytest

from eawf.kernel.runtime.capsule import StopCondition
from eawf.kernel.runtime.compiled import CompiledRunSpec
from eawf.runtime.daemon.native_dispatch import CapsuleRequest, seal_capsule
from eawf.workflow.runtime.compile import compile_run_spec
from tests import _provider_helpers as fx

pytestmark = pytest.mark.unit

#: A semantic tool the fixture profile grants.
GRANTED = "repo_read"

#: A semantic tool the fixture profile can withhold.
WITHHELD = "run_scoped_command"


def _spec(*, deny: tuple[str, ...] = ()) -> CompiledRunSpec:
    """Compile a spec whose merged tool policy denies *deny*."""
    profile = fx.profile_document(
        tool_policy={
            "allow": ["repo_read", "repo_search", "workspace_apply_patch", "submit_report"],
            "deny": list(deny),
        }
    )
    return compile_run_spec(
        fx.task_request(),
        configuration=fx.configuration(profiles=[profile]),
        bindings=[fx.binding()],
        compiled_at=fx.COMPILED_AT,
    )


def _request(*, grants: tuple[str, ...] = (), denials: tuple[str, ...] = ()) -> CapsuleRequest:
    """Return a capsule request carrying *grants* and *denials*."""
    return CapsuleRequest(
        criteria_digest=fx.digest("b"),
        report_schema_ref="schema://agent/report/v1",
        tool_grants=grants,
        tool_denials=denials,
        stop_conditions=(StopCondition.BUDGET_EXHAUSTED,),
    )


# ---- the widening this guard exists to stop --------------------------------


def test_dropping_a_merged_denial_is_refused() -> None:
    """A capsule that forgets a merged denial cannot seal."""
    spec = _spec(deny=(WITHHELD,))
    with pytest.raises(ValueError, match="capsule_policy_drift"):
        seal_capsule(spec=spec, request=_request(denials=()))


def test_granting_what_the_policy_denies_is_refused() -> None:
    """A capsule cannot grant a tool the merged policy withheld."""
    spec = _spec(deny=(WITHHELD,))
    with pytest.raises(ValueError, match="capsule_policy_drift"):
        seal_capsule(spec=spec, request=_request(grants=(WITHHELD,), denials=(WITHHELD,)))


def test_the_refusal_names_the_dropped_denial() -> None:
    """The message says which denial went missing, not merely that one did."""
    spec = _spec(deny=(WITHHELD,))
    with pytest.raises(ValueError) as excinfo:
        seal_capsule(spec=spec, request=_request(denials=()))
    assert WITHHELD in str(excinfo.value)


# ---- what stays legal -----------------------------------------------------


def test_carrying_the_merged_denial_seals() -> None:
    """The honest capsule -- denials equal to the merged policy -- seals."""
    spec = _spec(deny=(WITHHELD,))
    capsule = seal_capsule(spec=spec, request=_request(denials=(WITHHELD,)))
    assert WITHHELD in capsule.tool_denials


def test_a_stricter_capsule_seals() -> None:
    """Denying more than the policy requires is not a widening."""
    spec = _spec(deny=(WITHHELD,))
    capsule = seal_capsule(spec=spec, request=_request(denials=(WITHHELD, GRANTED)))
    assert GRANTED in capsule.tool_denials


def test_an_empty_policy_admits_any_capsule() -> None:
    """A policy that denies nothing has nothing to widen."""
    capsule = seal_capsule(spec=_spec(), request=_request(grants=(GRANTED,)))
    assert capsule.tool_grants == (GRANTED,)


def test_a_granted_tool_the_policy_does_not_deny_seals() -> None:
    """The guard judges denials, not whether a grant appears in the allow set."""
    capsule = seal_capsule(
        spec=_spec(deny=(WITHHELD,)), request=_request(grants=(GRANTED,), denials=(WITHHELD,))
    )
    assert capsule.tool_grants == (GRANTED,)


def test_seal_capsule_still_takes_its_other_fields_from_the_spec() -> None:
    """The guard does not disturb the fields that were already spec-derived."""
    spec = _spec(deny=(WITHHELD,))
    capsule = seal_capsule(spec=spec, request=_request(denials=(WITHHELD,)))
    assert capsule.run_ref == spec.run_ref
    assert capsule.policy_digest == spec.policy_digest
    assert capsule.compiled_spec_digest == spec.contract_digest


# ---- a request that names no tool_grants at all ----------------------------


def test_a_request_naming_no_tool_grants_still_reaches_submit_report() -> None:
    """Omitting tool_grants still lets the Run file the report it must end with."""
    capsule = seal_capsule(
        spec=_spec(),
        request=CapsuleRequest(
            criteria_digest=fx.digest("b"),
            report_schema_ref="schema://agent/report/v1",
            stop_conditions=(StopCondition.BUDGET_EXHAUSTED,),
        ),
    )
    assert capsule.tool_grants == ("submit_report",)


def test_an_explicit_empty_tool_grants_overrides_the_default() -> None:
    """Naming an empty grant list is a positive statement, not an omission."""
    capsule = seal_capsule(spec=_spec(), request=_request(grants=()))
    assert capsule.tool_grants == ()
