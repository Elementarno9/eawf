"""The canary runs under the spec the tuple will serve, or not at all.

A canary is evidence about a tuple only if it ran under the same
authority the tuple will serve: the same tool grants, the same denials,
the same sandbox policy, and the same environment. Each of the four is
checked before the canary is dispatched, and a difference on any of them
refuses the stage with ``schema_mismatch`` and grades no containment at
all -- the refusal is visible in the result as the axes that differed.

The Run identity is checked first. A canary that carried the served Run's
own reference would be the production Run, which is the one Run the
containment probe set may never touch, so that is refused before parity
is even considered.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.runtime.certification import CertificationFailureCode
from eawf.kernel.runtime.compiled import (
    UNSEALED_DIGEST,
    CompiledCapability,
    CompiledRunSpec,
    ResolvedFieldSource,
)
from eawf.kernel.runtime.provider import AgentProviderProfile, AuthorityGrant
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.conformance import StageParams, canary, probe
from eawf.runtime.runtimes.conformance import (
    CanaryRequest,
    ParityAxis,
    ProbeRequest,
    RuntimeTuple,
    canary_parity_mismatches,
)
from eawf.runtime.runtimes.containment import (
    CONTAINMENT_ATTEMPTS,
    ContainmentAttempt,
    ContainmentCallOutcome,
    DenialReason,
)
from tests import _provider_helpers as fx

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
CANARY_RUN_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000011"
EVIDENCE_REF = "artifact://conformance/canary-2026-09-17"
OBSERVED_FLAGS = ("--allowedTools", "--output-format")
EVIDENCED_CAPABILITIES = ("tool_use", "streaming", "session_resume")

PROFILE = AgentProviderProfile.model_validate(fx.profile_document())


def runtime_tuple() -> RuntimeTuple:
    """Return the runtime tuple the canary is run for."""
    return RuntimeTuple(
        manifest_ref="driver://claude-sdk/v1",
        manifest_digest=fx.digest("a"),
        distribution_version="2.1.0",
        sdk_or_server_version="1.9.0",
        auth_kind="subscription",
        model_family="claude-opus",
        os_class="macos",
        architecture="aarch64",
        managed_profile_digest=fx.digest("e"),
        conformance_suite_version="1.0.0",
    )


def spec(**overrides: Any) -> CompiledRunSpec:
    """Return a sealed compiled spec with *overrides* applied."""
    fields: dict[str, Any] = {
        "run_ref": fx.RUN_URN,
        "run_scope": fx.task_request().run_scope,
        "purpose": "implement",
        "agent_role": "executor",
        "route_policy_ref": "route://mutating-task",
        "route_policy_revision": 3,
        "profile_ref": "profile://fixture_codex",
        "profile_revision": 1,
        "driver_manifest_ref": fx.DRIVER_REF,
        "driver_manifest_digest": fx.digest("a"),
        "certification_ref": fx.CERTIFICATION_REF,
        "certification_digest": fx.digest("c"),
        "auth_profile_ref": fx.AUTH_REF,
        "auth_kind": "subscription",
        "model_id": fx.CERTIFIED_MODEL,
        "capabilities": (
            CompiledCapability.model_validate(
                {
                    **fx.observation("semantic_tools").model_dump(),
                    "requested": "required",
                    "requested_level": True,
                }
            ),
        ),
        "limits": PROFILE.limits,
        "sandbox": PROFILE.sandbox,
        "environment": PROFILE.environment,
        "session": PROFILE.session,
        "stream": PROFILE.stream,
        "authority": AuthorityGrant.model_validate(PROFILE.authority_ceiling.model_dump()),
        "tool_policy": PROFILE.tool_policy,
        "context_policy": PROFILE.context_policy,
        "provider_options": PROFILE.provider_options,
        "source_map": (
            ResolvedFieldSource(
                field_path="/model_id",
                effective_value_digest=UNSEALED_DIGEST,
                source_layer="workspace",
                source_ref=fx.WORKSPACE_SOURCE,
                merge_operation="replace",
            ),
        ),
        "compiled_at": fx.COMPILED_AT,
        "compiler_version": "1.0.0",
    }
    fields.update(overrides)
    return CompiledRunSpec.seal(fields)


def canary_spec(**overrides: Any) -> CompiledRunSpec:
    """Return the canary's own Run compiled from the served spec."""
    return spec(run_ref=CANARY_RUN_URN, **overrides)


def contained() -> tuple[ContainmentCallOutcome, ...]:
    """Return a denied outcome for every containment attempt."""
    return tuple(
        ContainmentCallOutcome(
            attempt=attempt, denied=True, denial_reason=DenialReason.SCOPE_ESCAPE
        )
        for attempt in CONTAINMENT_ATTEMPTS
    )


@pytest.fixture
def ctx(tmp_path: Path) -> MethodContext:
    """Return a daemon context bound to a throwaway tree."""
    return MethodContext(
        started_at=NOW.isoformat(),
        pid=4242,
        protocol_version=PROTOCOL_VERSION,
        version="test",
        state_path=tmp_path / ".ea" / "state.json",
    )


def call(handler: Any, ctx: MethodContext, request: Any) -> dict[str, Any]:
    """Invoke one stage verb over the wire shape and return its result."""
    params = StageParams(request=request.model_dump(mode="json")).model_dump(mode="json")
    result: dict[str, Any] = asyncio.run(handler(ctx, params))
    return result


def probed(ctx: MethodContext) -> None:
    """Clear the probe stage so the canary may start."""
    request = ProbeRequest(
        runtime_tuple=runtime_tuple(),
        runtime_id="claude-code",
        installed=True,
        observed_flags=OBSERVED_FLAGS,
        required_capabilities=EVIDENCED_CAPABILITIES,
        evidence_ref=EVIDENCE_REF,
    )
    assert call(probe, ctx, request)["record"]["outcome"] == "passed"


def canary_for(**overrides: Any) -> CanaryRequest:
    """Return a canary request, at parity unless *overrides* break it."""
    document: dict[str, Any] = {
        "runtime_tuple": runtime_tuple(),
        "served_spec": spec(),
        "canary_spec": canary_spec(),
        "outcomes": contained(),
        "evidence_ref": EVIDENCE_REF,
    }
    document.update(overrides)
    return CanaryRequest.model_validate(document)


# ---- parity holds -----------------------------------------------------------


def test_a_canary_at_parity_is_dispatched(ctx: MethodContext) -> None:
    """Matching grants, denials, sandbox and environment clear the check."""
    probed(ctx)

    result = call(canary, ctx, canary_for())

    assert result["record"]["outcome"] == "passed"
    assert result["parity_mismatches"] == []
    assert result["containment"]["passed"] is True


def test_parity_ignores_the_run_the_spec_belongs_to() -> None:
    """The canary is its own Run; that difference is not a parity break."""
    assert canary_parity_mismatches(served=spec(), canary=canary_spec()) == ()


def test_parity_holds_when_both_specs_grant_no_tool() -> None:
    """An empty grant on both sides is parity, not an absent comparison."""
    empty = PROFILE.tool_policy.model_copy(update={"allow": ()})

    mismatches = canary_parity_mismatches(
        served=spec(tool_policy=empty), canary=canary_spec(tool_policy=empty)
    )

    assert mismatches == ()


# ---- one axis at a time -----------------------------------------------------


def test_a_narrowed_tool_grant_is_a_parity_break() -> None:
    """One fewer tool is a different contract, not a smaller one."""
    narrowed = PROFILE.tool_policy.model_copy(update={"allow": ("repo_read",)})

    mismatches = canary_parity_mismatches(served=spec(), canary=canary_spec(tool_policy=narrowed))

    assert mismatches == (ParityAxis.TOOL_GRANTS,)


def test_an_added_tool_denial_is_a_parity_break() -> None:
    """A denial the served spec does not carry changes what was exercised."""
    denied = PROFILE.tool_policy.model_copy(update={"deny": ("repo_search",)})

    mismatches = canary_parity_mismatches(served=spec(), canary=canary_spec(tool_policy=denied))

    assert mismatches == (ParityAxis.TOOL_DENIALS,)


def test_a_changed_sandbox_policy_is_a_parity_break() -> None:
    """Another filesystem policy is another isolation."""
    other = PROFILE.sandbox.model_copy(
        update={"filesystem_policy_ref": "policy://filesystem/other-workspace"}
    )

    mismatches = canary_parity_mismatches(served=spec(), canary=canary_spec(sandbox=other))

    assert mismatches == (ParityAxis.SANDBOX_POLICY,)


def test_a_changed_environment_digest_is_a_parity_break() -> None:
    """One variable fewer is a different environment digest."""
    other = PROFILE.environment.model_copy(update={"allowed_variable_names": ("LANG",)})

    mismatches = canary_parity_mismatches(served=spec(), canary=canary_spec(environment=other))

    assert mismatches == (ParityAxis.ENVIRONMENT_DIGEST,)


def test_a_changed_timezone_is_a_parity_break() -> None:
    """The digest covers the whole environment, not a chosen subset."""
    other = PROFILE.environment.model_copy(update={"timezone": "Europe/Berlin"})

    mismatches = canary_parity_mismatches(served=spec(), canary=canary_spec(environment=other))

    assert mismatches == (ParityAxis.ENVIRONMENT_DIGEST,)


def test_every_broken_axis_is_reported() -> None:
    """A spec that differs on all four names all four."""
    drifted = canary_spec(
        tool_policy=PROFILE.tool_policy.model_copy(
            update={"allow": ("repo_read",), "deny": ("repo_search",)}
        ),
        sandbox=PROFILE.sandbox.model_copy(
            update={"filesystem_policy_ref": "policy://filesystem/other-workspace"}
        ),
        environment=PROFILE.environment.model_copy(update={"allowed_variable_names": ()}),
    )

    mismatches = canary_parity_mismatches(served=spec(), canary=drifted)

    assert mismatches == (
        ParityAxis.TOOL_GRANTS,
        ParityAxis.TOOL_DENIALS,
        ParityAxis.SANDBOX_POLICY,
        ParityAxis.ENVIRONMENT_DIGEST,
    )


# ---- rejection happens before dispatch --------------------------------------


def test_a_broken_parity_refuses_the_stage_before_dispatch(ctx: MethodContext) -> None:
    """The refusal names schema_mismatch and grades no containment."""
    probed(ctx)
    narrowed = PROFILE.tool_policy.model_copy(update={"allow": ("repo_read",)})

    result = call(canary, ctx, canary_for(canary_spec=canary_spec(tool_policy=narrowed)))

    assert result["record"]["outcome"] == "refused"
    assert result["record"]["reason_code"] == CertificationFailureCode.SCHEMA_MISMATCH.value
    assert result["containment"] is None
    assert result["parity_mismatches"] == [ParityAxis.TOOL_GRANTS.value]


def test_a_broken_parity_is_refused_even_with_an_escaping_probe_set(
    ctx: MethodContext,
) -> None:
    """Nothing was dispatched, so nothing the caller reported is graded."""
    probed(ctx)
    escaped = (
        ContainmentCallOutcome(attempt=ContainmentAttempt.CANONICAL_GIT, denied=False),
        *(row for row in contained() if row.attempt is not ContainmentAttempt.CANONICAL_GIT),
    )
    other = PROFILE.sandbox.model_copy(
        update={"filesystem_policy_ref": "policy://filesystem/other-workspace"}
    )

    result = call(canary, ctx, canary_for(canary_spec=canary_spec(sandbox=other), outcomes=escaped))

    assert result["record"]["outcome"] == "refused"
    assert result["containment"] is None
    assert result["quarantine_trigger"] is None


def test_a_refused_canary_admits_a_canary_at_parity(ctx: MethodContext) -> None:
    """A refusal ran nothing, so the passed probe still admits a canary."""
    probed(ctx)
    narrowed = PROFILE.tool_policy.model_copy(update={"allow": ("repo_read",)})
    call(canary, ctx, canary_for(canary_spec=canary_spec(tool_policy=narrowed)))

    result = call(canary, ctx, canary_for())

    assert result["record"]["outcome"] == "passed"
    assert result["containment"]["passed"] is True


def test_a_canary_may_not_be_the_run_the_tuple_serves(ctx: MethodContext) -> None:
    """The probe set never runs against a production Run."""
    probed(ctx)

    with pytest.raises(ValueError, match="its own Run"):
        call(canary, ctx, canary_for(canary_spec=spec()))


def test_the_canary_request_refuses_a_repeated_attempt() -> None:
    """Two reports of one call would let a reported escape be dropped."""
    doubled = (*contained(), contained()[0])

    with pytest.raises(ValueError, match="more than once"):
        canary_for(outcomes=doubled)
