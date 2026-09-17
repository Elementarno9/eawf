"""The Run spec compiler: sealed output, source map, and every refusal.

A compiled spec is immutable and explains itself: each field resolved from
configuration names the layer it came from and the merge operation that
produced it, and the value digest on that row matches the compiled value.
The compiler refuses a mutating purpose outside a task scope before it
looks at any route, and each other refusal carries its own code.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from eawf.kernel.config.providers import ProviderConfiguration
from eawf.kernel.runtime.compiled import (
    CompiledRunSpec,
    PolicyOverlay,
    RunCompileRequest,
    canonical_digest,
    json_pointer_value,
)
from eawf.kernel.runtime.provider import (
    AuthorityGrant,
    ContextPolicy,
    EnvironmentPolicy,
    RuntimeLimits,
    SandboxPolicy,
    ToolPolicy,
)
from eawf.workflow.runtime.compile import (
    CompileRejection,
    RunCompileError,
    compile_run_spec,
    select_route,
)
from eawf.workflow.runtime.merge import SAFE_BASELINE_REF
from tests import _provider_helpers as fx

pytestmark = pytest.mark.unit

SECTIONS: dict[str, type[BaseModel]] = {
    "limits": RuntimeLimits,
    "sandbox": SandboxPolicy,
    "environment": EnvironmentPolicy,
    "authority": AuthorityGrant,
    "tool_policy": ToolPolicy,
    "context_policy": ContextPolicy,
}
TOP_LEVEL = {
    "/auth_profile_ref",
    "/capabilities",
    "/driver_manifest_ref",
    "/model_id",
    "/profile_ref",
    "/provider_options",
    "/route_policy_ref",
    "/session",
    "/stream",
}


def _compile(
    request: RunCompileRequest | None = None,
    *,
    configuration: ProviderConfiguration | None = None,
    bindings: list[Any] | None = None,
    overlays: tuple[PolicyOverlay, ...] = (),
    compiled_at: datetime = fx.COMPILED_AT,
) -> CompiledRunSpec:
    return compile_run_spec(
        request or fx.task_request(),
        configuration=configuration or fx.configuration(),
        bindings=bindings if bindings is not None else [fx.binding()],
        compiled_at=compiled_at,
        policy_overlays=overlays,
    )


def _refused(code: CompileRejection, **kwargs: Any) -> RunCompileError:
    with pytest.raises(RunCompileError) as caught:
        _compile(**kwargs)
    assert caught.value.code is code
    return caught.value


def _run_override(**fields: Any) -> PolicyOverlay:
    return PolicyOverlay.model_validate(
        {"layer": "run_override", "source_ref": "policy://run/override", **fields}
    )


def _row(spec: CompiledRunSpec, path: str) -> Any:
    return next(row for row in spec.source_map if row.field_path == path)


# ---- sealed, immutable output ------------------------------------------------


def test_compile_run_spec_emits_sealed_immutable_spec() -> None:
    spec = _compile()
    assert spec.compiler_version == "1.0.0"
    with pytest.raises(ValidationError, match="frozen"):
        spec.model_id = fx.UNCERTIFIED_MODEL
    assert CompiledRunSpec.model_validate_json(spec.model_dump_json()) == spec


def test_compile_run_spec_is_deterministic() -> None:
    first, second = _compile(), _compile()
    assert first.contract_digest == second.contract_digest
    assert first == second


def test_compile_run_spec_refuses_persisted_edit() -> None:
    document = _compile().model_dump(mode="json")
    document["limits"]["wall_seconds"] = 86_400
    with pytest.raises(ValidationError, match="does not match"):
        CompiledRunSpec.model_validate(document)


def test_compile_run_spec_binds_identity_and_installation_facts() -> None:
    spec = _compile()
    assert (spec.route_policy_ref, spec.route_policy_revision) == ("route://mutating-task", 3)
    assert (spec.profile_ref, spec.profile_revision) == ("profile://fixture_codex", 1)
    assert spec.driver_manifest_digest == fx.digest("a")
    assert (spec.certification_ref, spec.certification_digest) == (
        fx.CERTIFICATION_REF,
        fx.digest("c"),
    )
    assert spec.auth_kind == "subscription"
    assert spec.model_id == fx.CERTIFIED_MODEL


# ---- source map ---------------------------------------------------------------


def test_compile_run_spec_source_map_covers_every_resolved_field() -> None:
    spec = _compile()
    expected = set(TOP_LEVEL)
    for section, model in SECTIONS.items():
        expected |= {f"/{section}/{name}" for name in model.model_fields}
    assert {row.field_path for row in spec.source_map} == expected


def test_compile_run_spec_source_map_digests_match_values() -> None:
    spec = _compile()
    payload = spec.model_dump(mode="json")
    for row in spec.source_map:
        value = json_pointer_value(payload, row.field_path)
        assert row.effective_value_digest == canonical_digest(value), row.field_path


@pytest.mark.parametrize(
    ("path", "layer", "operation"),
    [
        ("/tool_policy/allow", "workspace", "intersection"),
        ("/environment/allowed_variable_names", "workspace", "intersection"),
        ("/limits/wall_seconds", "workspace", "minimum"),
        ("/limits/child_runs", "safe_baseline", "minimum"),
        ("/sandbox/mode", "workspace", "minimum"),
        ("/context_policy/minimum_remaining_tokens", "workspace", "maximum"),
        ("/capabilities", "workspace", "maximum"),
        ("/model_id", "workspace", "replace"),
        ("/sandbox/filesystem_policy_ref", "workspace", "replace"),
        ("/environment/secret_broker_ref", "workspace", "replace"),
        ("/provider_options", "workspace", "replace"),
        ("/route_policy_ref", "workspace", "replace"),
    ],
)
def test_compile_run_spec_source_map_names_layer_and_operation(
    path: str, layer: str, operation: str
) -> None:
    row = _row(_compile(), path)
    assert (row.source_layer, row.merge_operation) == (layer, operation)


def test_compile_run_spec_source_map_names_global_route_layer() -> None:
    row = _row(_compile(fx.review_request()), "/route_policy_ref")
    assert (row.source_layer, row.source_ref) == ("global", fx.GLOBAL_SOURCE)


def test_compile_run_spec_source_map_names_narrowing_override() -> None:
    override = _run_override(limits={"wall_seconds": 60}, tool_policy={"deny": ["repo_search"]})
    spec = _compile(overlays=(override,))
    wall = _row(spec, "/limits/wall_seconds")
    assert (wall.source_layer, wall.source_ref) == ("run_override", "policy://run/override")
    assert fx.WORKSPACE_SOURCE in wall.superseded_source_refs
    assert wall.constraint_reason is not None
    deny = _row(spec, "/tool_policy/deny")
    assert (deny.source_layer, deny.merge_operation) == ("run_override", "union_deny")
    assert spec.limits.wall_seconds == 60


def test_compile_run_spec_source_map_names_repository_layer() -> None:
    repository = {
        "profiles": [
            {
                "profile_id": "fixture_codex",
                "source_ref": fx.REPOSITORY_SOURCE,
                "limits": {"output_bytes": 4096},
            }
        ]
    }
    spec = _compile(configuration=fx.configuration(repository=repository))
    row = _row(spec, "/limits/output_bytes")
    assert (row.source_layer, row.source_ref) == ("repository", fx.REPOSITORY_SOURCE)
    assert spec.limits.output_bytes == 4096


def test_compile_run_spec_denial_removes_granted_tool() -> None:
    spec = _compile(overlays=(_run_override(tool_policy={"deny": ["workspace_apply_patch"]}),))
    assert "workspace_apply_patch" not in spec.tool_policy.allow
    assert "workspace_apply_patch" in spec.tool_policy.deny
    reason = _row(spec, "/tool_policy/allow").constraint_reason
    assert reason is not None
    assert "union_deny" in reason


# ---- safe baseline -----------------------------------------------------------


def test_compile_run_spec_read_only_purpose_gets_read_only_ceiling() -> None:
    spec = _compile(fx.review_request())
    assert spec.sandbox.mode == "read_only"
    assert spec.authority.workspace == "read_only"
    for path in ("/sandbox/mode", "/authority/workspace"):
        row = _row(spec, path)
        assert (row.source_layer, row.source_ref) == ("safe_baseline", SAFE_BASELINE_REF)
    assert spec.limits.child_runs == 2


def test_compile_run_spec_task_scope_forbids_child_runs() -> None:
    spec = _compile()
    assert spec.limits.child_runs == 0
    assert _row(spec, "/limits/child_runs").source_layer == "safe_baseline"
    assert spec.sandbox.mode == "workspace_write"
    assert spec.authority.workspace == "scoped_write"


# ---- TaskScope and request refusals -----------------------------------------


def test_compile_run_spec_rejects_mutating_purpose_outside_task_scope() -> None:
    request = fx.review_request(purpose="implement", agent_role="executor")
    error = _refused(CompileRejection.MUTATING_PURPOSE_OUTSIDE_TASK_SCOPE, request=request)
    assert "only a task scope may write" in str(error)


@pytest.mark.parametrize("purpose", ["implement", "integrate", "repair"])
def test_compile_run_spec_rejects_each_mutating_purpose_on_batch_scope(purpose: str) -> None:
    request = fx.review_request(purpose=purpose, agent_role="executor")
    _refused(CompileRejection.MUTATING_PURPOSE_OUTSIDE_TASK_SCOPE, request=request)


def test_compile_run_spec_rejects_purpose_the_scope_does_not_declare() -> None:
    request = fx.task_request(purpose="repair")
    _refused(CompileRejection.PURPOSE_SCOPE_MISMATCH, request=request)


def test_compile_run_spec_rejects_mutation_by_read_only_role() -> None:
    _refused(CompileRejection.ROLE_CANNOT_MUTATE, request=fx.task_request(agent_role="reviewer"))


def test_compile_run_spec_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _compile(compiled_at=datetime(2026, 9, 17, 12, 0))


def test_compile_run_spec_rejects_configuration_overlay_outside_repository_layer() -> None:
    misfiled = _run_override(limits={"wall_seconds": 60})
    configuration = fx.configuration().model_copy(update={"repository_overlays": (misfiled,)})
    _refused(CompileRejection.OVERLAY_LAYER_INVALID, configuration=configuration)


@pytest.mark.parametrize("layer", ["safe_baseline", "global", "workspace", "repository"])
def test_compile_run_spec_rejects_caller_overlay_outside_caller_layers(layer: str) -> None:
    misfiled = PolicyOverlay.model_validate({"layer": layer, "source_ref": fx.GLOBAL_SOURCE})
    _refused(CompileRejection.OVERLAY_LAYER_INVALID, overlays=(misfiled,))


# ---- route and profile selection ----------------------------------------------


def test_compile_run_spec_rejects_request_no_route_matches() -> None:
    request = fx.review_request(
        run_scope={"scope_kind": "batch", "purpose": "plan", "batch_ref": fx.BATCH_URN},
        purpose="plan",
        agent_role="planner",
    )
    _refused(CompileRejection.NO_MATCHING_ROUTE, request=request)


def test_select_route_prefers_priority_then_route_id() -> None:
    routes = [
        fx.task_route_document(route_id="b-route", priority=500),
        fx.task_route_document(route_id="a-route", priority=500),
        fx.task_route_document(route_id="low", priority=900, disabled=True),
        fx.task_route_document(route_id="mid", priority=100),
    ]
    selected = select_route(fx.task_request(), fx.configuration(routes=routes))
    assert selected.route.route_id == "a-route"


def test_select_route_honours_mutation_flag() -> None:
    route = fx.review_route_document(match={"requires_mutation": False})
    configuration = fx.configuration(routes=[], global_routes=[route])
    assert select_route(fx.review_request(), configuration).route.route_id == "review"
    _refused(CompileRejection.NO_MATCHING_ROUTE, configuration=configuration)


def test_compile_run_spec_falls_back_to_next_eligible_profile() -> None:
    second = fx.profile_document(
        profile_id="fixture_claude",
        driver_ref=fx.OTHER_DRIVER_REF,
        auth_profile_ref=fx.OTHER_AUTH_REF,
        provider_options={"provider_kind": "claude"},
    )
    route = fx.task_route_document(allowed_profiles=["fixture_claude", "fixture_codex"])
    configuration = fx.configuration(profiles=[fx.profile_document(), second], routes=[route])
    spec = _compile(configuration=configuration)
    assert spec.profile_ref == "profile://fixture_codex"


@pytest.mark.parametrize("disable", ["profile", "manifest", "unbound"])
def test_compile_run_spec_rejects_route_without_eligible_profile(disable: str) -> None:
    configuration = fx.configuration()
    bindings = [fx.binding()]
    if disable == "profile":
        configuration = fx.configuration(profiles=[fx.profile_document(disabled=True)])
    elif disable == "manifest":
        bindings = [fx.binding(manifest=fx.manifest_document(disabled=True))]
    else:
        bindings = []
    error = _refused(
        CompileRejection.NO_ELIGIBLE_PROFILE, configuration=configuration, bindings=bindings
    )
    assert "fixture_codex" in str(error)


# ---- model pin ----------------------------------------------------------------


def test_compile_run_spec_rejects_uncertified_model_unattended() -> None:
    override = _run_override(model_policy={"allowed": [fx.UNCERTIFIED_MODEL]})
    error = _refused(CompileRejection.MODEL_UNCERTIFIED, overlays=(override,))
    assert fx.UNCERTIFIED_MODEL in str(error)


def test_compile_run_spec_names_certified_alternative() -> None:
    override = _run_override(model_policy={"default": fx.UNCERTIFIED_MODEL})
    error = _refused(CompileRejection.MODEL_UNCERTIFIED, overlays=(override,))
    assert f"certified alternative: {fx.CERTIFIED_MODEL}" in str(error)


def test_compile_run_spec_admits_uncertified_model_when_attended() -> None:
    override = _run_override(model_policy={"default": fx.UNCERTIFIED_MODEL})
    spec = _compile(fx.task_request(unattended=False), overlays=(override,))
    assert spec.model_id == fx.UNCERTIFIED_MODEL
    row = _row(spec, "/model_id")
    assert (row.source_layer, row.merge_operation) == ("run_override", "replace")


def test_compile_run_spec_rejects_model_missing_from_listing() -> None:
    binding = fx.binding(listed_models=[fx.UNCERTIFIED_MODEL])
    _refused(CompileRejection.MODEL_UNKNOWN, bindings=[binding])


def test_compile_run_spec_rejects_emptied_model_allowlist() -> None:
    override = _run_override(model_policy={"allowed": ["gpt-9"]})
    _refused(CompileRejection.NO_ELIGIBLE_MODEL, overlays=(override,))


def test_compile_run_spec_falls_back_to_first_allowed_model() -> None:
    profile = fx.profile_document(
        model_policy={
            "allowed": [fx.CERTIFIED_MODEL, fx.UNCERTIFIED_MODEL],
            "default": fx.UNCERTIFIED_MODEL,
        }
    )
    override = _run_override(model_policy={"allowed": [fx.CERTIFIED_MODEL]})
    spec = _compile(configuration=fx.configuration(profiles=[profile]), overlays=(override,))
    assert spec.model_id == fx.CERTIFIED_MODEL


# ---- capabilities -------------------------------------------------------------


def test_compile_run_spec_compiles_capabilities_in_canonical_order() -> None:
    spec = _compile()
    rows = {row.capability_id: row for row in spec.capabilities}
    assert list(rows) == sorted(rows)
    assert rows["semantic_tools"].requested == "required"
    assert rows["semantic_tools"].status == "verified"
    assert rows["usage_receipts"].requested == "optional"
    assert rows["context_compaction"].status == "unsupported"
    assert rows["context_compaction"].reason_code == "not_declared_by_driver"
    assert fx.WORKSPACE_SOURCE in rows["semantic_tools"].constraint_source_refs


def test_compile_run_spec_rejects_unobserved_required_capability() -> None:
    observed = [fx.observation("semantic_tools").model_dump(mode="json")]
    error = _refused(
        CompileRejection.REQUIRED_CAPABILITY_UNAVAILABLE,
        bindings=[fx.binding(capabilities=observed)],
    )
    assert "event_replay" in str(error)


def test_compile_run_spec_rejects_expired_required_capability() -> None:
    expired = fx.COMPILED_AT.isoformat()
    observed = [
        fx.observation(capability_id, expires_at=expired).model_dump(mode="json")
        for capability_id in fx.REQUIRED
    ]
    _refused(
        CompileRejection.REQUIRED_CAPABILITY_UNAVAILABLE,
        bindings=[fx.binding(capabilities=observed)],
    )


def test_compile_run_spec_marks_expired_optional_capability() -> None:
    observed = [
        fx.observation(capability_id).model_dump(mode="json") for capability_id in fx.REQUIRED
    ]
    stale = fx.observation(
        "usage_receipts", expires_at=(fx.COMPILED_AT - timedelta(seconds=1)).isoformat()
    )
    spec = _compile(bindings=[fx.binding(capabilities=[*observed, stale.model_dump(mode="json")])])
    row = next(row for row in spec.capabilities if row.capability_id == "usage_receipts")
    assert (row.requested, row.status, row.reason_code) == (
        "optional",
        "expired",
        "evidence_expired",
    )


def test_compile_run_spec_rejects_required_capability_raised_by_override() -> None:
    observed = [
        fx.observation(capability_id).model_dump(mode="json") for capability_id in fx.REQUIRED
    ]
    override = _run_override(required_capabilities=["usage_receipts"])
    error = _refused(
        CompileRejection.REQUIRED_CAPABILITY_UNAVAILABLE,
        bindings=[fx.binding(capabilities=observed)],
        overlays=(override,),
    )
    assert "usage_receipts" in str(error)


def test_compile_run_spec_rejects_merged_context_bounds_conflict() -> None:
    override = _run_override(context_policy={"minimum_remaining_tokens": 200_000})
    _refused(CompileRejection.POLICY_BOUNDS_CONFLICT, overlays=(override,))
