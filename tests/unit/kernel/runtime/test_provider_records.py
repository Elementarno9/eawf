"""Strict provider records and the three-state model pin.

Three halves are pinned. The six records the runtime contract names refuse
an unknown key, including one nested inside a policy leaf, and each
survives a JSON round trip unchanged. The record rules that keep ambient
authority out hold at the boundary: a raw executable path, a
post-acceptance fallback, an unmanaged sandbox, and a tampered compiled
digest are all refused. And a model pin resolves to exactly one of
certified, uncertified, or unknown, with the dispatch and save rights each
state carries.
"""

from __future__ import annotations

import itertools
import typing
from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from eawf.kernel.runtime.compiled import (
    UNSEALED_DIGEST,
    CompiledCapability,
    CompiledRunSpec,
    ModelPinResolution,
    ModelPinState,
    ResolvedFieldSource,
    RuntimeBinding,
    canonical_digest,
    json_pointer_value,
    resolve_model_pin,
)
from eawf.kernel.runtime.provider import (
    AUTHORITY_LEVEL_ORDER,
    AgentProviderProfile,
    AuthorityGrant,
    CapabilityDeclaration,
    ContextPolicy,
    DriverManifest,
    LockedDistribution,
    ModelPolicy,
    ProviderRecordId,
    RouteMatch,
    RoutePolicy,
    RunScopeKind,
    SandboxPolicy,
    SessionPolicy,
    TypedUrn,
    reject_repeats,
    semver_key,
)
from eawf.kernel.state.epoch2.run import RunPurpose, RunScope
from tests import _provider_helpers as fx

pytestmark = pytest.mark.unit

RESOLVED_SOURCE = {
    "field_path": "/limits/wall_seconds",
    "effective_value_digest": fx.digest("d"),
    "source_layer": "repository",
    "source_ref": fx.REPOSITORY_SOURCE,
    "merge_operation": "minimum",
    "superseded_source_refs": [fx.WORKSPACE_SOURCE, "policy://baseline/compiled"],
    "constraint_reason": "minimum over 3 declaring layers",
}


def _sealed_fields(**overrides: Any) -> dict[str, Any]:
    """Return the content of a small valid compiled spec, digests unsealed."""
    profile = AgentProviderProfile.model_validate(fx.profile_document())
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
        "limits": profile.limits,
        "sandbox": profile.sandbox,
        "environment": profile.environment,
        "session": profile.session,
        "stream": profile.stream,
        "authority": AuthorityGrant.model_validate(profile.authority_ceiling.model_dump()),
        "tool_policy": profile.tool_policy,
        "context_policy": profile.context_policy,
        "provider_options": profile.provider_options,
        "source_map": (
            ResolvedFieldSource.model_validate(
                {**RESOLVED_SOURCE, "effective_value_digest": UNSEALED_DIGEST}
            ),
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
    return fields


def _records() -> dict[str, BaseModel]:
    """Return one valid instance of each record the contract names."""
    return {
        "DriverManifest": DriverManifest.model_validate(fx.manifest_document()),
        "LockedDistribution": LockedDistribution.model_validate(
            fx.manifest_document()["distribution"]
        ),
        "AgentProviderProfile": AgentProviderProfile.model_validate(fx.profile_document()),
        "RoutePolicy": RoutePolicy.model_validate(fx.task_route_document()),
        "CompiledRunSpec": CompiledRunSpec.seal(_sealed_fields()),
        "ResolvedFieldSource": ResolvedFieldSource.model_validate(RESOLVED_SOURCE),
    }


RECORD_NAMES = sorted(_records())


# ---- strictness and round trip ----------------------------------------------


@pytest.mark.parametrize("name", RECORD_NAMES)
def test_provider_records_reject_unknown_top_level_field(name: str) -> None:
    record = _records()[name]
    document = record.model_dump(mode="json")
    document["ambient_executable"] = "/usr/local/bin/tool"
    with pytest.raises(ValidationError) as caught:
        type(record).model_validate(document)
    assert any(error["type"] == "extra_forbidden" for error in caught.value.errors())


@pytest.mark.parametrize(
    ("name", "section"),
    [
        ("DriverManifest", "distribution"),
        ("AgentProviderProfile", "sandbox"),
        ("RoutePolicy", "match"),
        ("CompiledRunSpec", "authority"),
    ],
)
def test_provider_records_reject_unknown_nested_field(name: str, section: str) -> None:
    record = _records()[name]
    document = record.model_dump(mode="json")
    document[section]["shell"] = "bash"
    with pytest.raises(ValidationError, match="extra"):
        type(record).model_validate(document)


@pytest.mark.parametrize("name", RECORD_NAMES)
def test_provider_records_round_trip_through_json(name: str) -> None:
    record = _records()[name]
    restored = type(record).model_validate_json(record.model_dump_json())
    assert restored == record
    assert restored.model_dump_json() == record.model_dump_json()


@pytest.mark.parametrize("name", RECORD_NAMES)
def test_provider_records_are_frozen(name: str) -> None:
    record = _records()[name]
    field = next(iter(type(record).model_fields))
    with pytest.raises(ValidationError, match="frozen"):
        setattr(record, field, getattr(record, field))


def test_provider_records_hold_sequences_as_tuples() -> None:
    profile = _records()["AgentProviderProfile"]
    assert isinstance(profile, AgentProviderProfile)
    assert isinstance(profile.tool_policy.allow, tuple)
    assert isinstance(profile.model_policy.allowed, tuple)


# ---- identifier and reference grammar ---------------------------------------


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("a", True),
        ("a" * 64, True),
        ("a" * 65, False),
        ("", False),
        ("Fixture", False),
        ("1fixture", False),
        ("fixture_codex-2", True),
    ],
)
def test_provider_record_id_grammar_bounds(value: str, valid: bool) -> None:
    adapter = TypeAdapter(ProviderRecordId)
    if valid:
        assert adapter.validate_python(value) == value
    else:
        with pytest.raises(ValidationError):
            adapter.validate_python(value)


@pytest.mark.parametrize(
    "value",
    ["/usr/local/bin/codex", "component://../bin", "component://", "file:///bin/sh", "ftp://x"],
)
def test_typed_urn_refuses_paths_and_unknown_schemes(value: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(TypedUrn).validate_python(value)


def test_locked_distribution_rejects_raw_executable_path() -> None:
    document = fx.manifest_document()["distribution"]
    document["entrypoint_ref"] = "/usr/local/bin/codex"
    with pytest.raises(ValidationError) as caught:
        LockedDistribution.model_validate(document)
    assert caught.value.errors()[0]["loc"] == ("entrypoint_ref",)


def test_locked_distribution_rejects_path_shaped_package() -> None:
    document = fx.manifest_document()["distribution"]
    document["package"] = "/opt/codex"
    with pytest.raises(ValidationError):
        LockedDistribution.model_validate(document)


# ---- driver manifest rules ---------------------------------------------------


def test_driver_manifest_rejects_ascending_worker_versions() -> None:
    document = fx.manifest_document(worker_protocol_versions=["1.0.0", "2.0.0"])
    with pytest.raises(ValidationError, match="newest first"):
        DriverManifest.model_validate(document)


def test_driver_manifest_rejects_repeated_capability() -> None:
    document = fx.manifest_document()
    document["capabilities"].append(document["capabilities"][0])
    with pytest.raises(ValidationError, match="appears more than once"):
        DriverManifest.model_validate(document)


@pytest.mark.parametrize("field", ["auth_kinds", "worker_protocol_versions", "capabilities"])
def test_driver_manifest_rejects_empty_required_list(field: str) -> None:
    with pytest.raises(ValidationError, match=r"too_short|at least 1"):
        DriverManifest.model_validate(fx.manifest_document(**{field: []}))


def test_driver_manifest_rejects_repeated_auth_kind() -> None:
    document = fx.manifest_document(auth_kinds=["api_key", "api_key"])
    with pytest.raises(ValidationError, match="appears more than once"):
        DriverManifest.model_validate(document)


def test_capability_declaration_keeps_true_and_one_distinct() -> None:
    declaration = CapabilityDeclaration(
        capability_id="parallel_calls",
        level_kind="integer",
        allowed_levels=(True, 1),
        default_level=1,
    )
    assert declaration.allowed_levels == (True, 1)
    with pytest.raises(ValidationError, match="not allowed"):
        CapabilityDeclaration(
            capability_id="parallel_calls",
            level_kind="integer",
            allowed_levels=(True,),
            default_level=1,
        )


def test_semver_key_orders_release_above_pre_release() -> None:
    versions = ["1.9.0-rc1", "2.0.0", "1.9.0", "10.0.0"]
    assert sorted(versions, key=semver_key, reverse=True) == [
        "10.0.0",
        "2.0.0",
        "1.9.0",
        "1.9.0-rc1",
    ]


def test_reject_repeats_accepts_empty_and_refuses_duplicate() -> None:
    assert reject_repeats(()) == ()
    with pytest.raises(ValueError, match="more than once"):
        reject_repeats(("a", "a"))


# ---- operator profile and route rules ---------------------------------------


def test_model_policy_rejects_default_outside_allowed() -> None:
    with pytest.raises(ValidationError, match="not in allowed"):
        ModelPolicy(allowed=("a",), default="b")


def test_model_policy_rejects_empty_and_repeated_allowlist() -> None:
    with pytest.raises(ValidationError) as caught:
        ModelPolicy.model_validate({"allowed": [], "default": "a"})
    assert caught.value.errors()[0]["type"] == "too_short"
    with pytest.raises(ValidationError, match="more than once"):
        ModelPolicy(allowed=("a", "a"), default="a")


def test_context_policy_rejects_floor_at_ceiling() -> None:
    with pytest.raises(ValidationError, match="below max_input_tokens"):
        ContextPolicy(max_input_tokens=2048, minimum_remaining_tokens=2048)


def test_session_policy_rejects_lost_after_at_heartbeat() -> None:
    with pytest.raises(ValidationError, match="exceed heartbeat"):
        SessionPolicy(heartbeat_seconds=30, lost_after_seconds=30)


def test_sandbox_policy_requires_policy_for_brokered_network() -> None:
    with pytest.raises(ValidationError, match="network_policy_ref"):
        SandboxPolicy(mode="read_only", network="brokered_allowlist")
    brokered = SandboxPolicy(
        mode="read_only", network="brokered_allowlist", network_policy_ref="policy://network/pypi"
    )
    assert brokered.network_policy_ref == "policy://network/pypi"


@pytest.mark.parametrize(
    ("field", "value"),
    [("managed", False), ("expose_canonical_git", True), ("expose_daemon_storage", True)],
)
def test_sandbox_policy_refuses_unmanaged_exposure(field: str, value: bool) -> None:
    with pytest.raises(ValidationError):
        SandboxPolicy.model_validate({"mode": "read_only", field: value})


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("tool_policy", "ambient_provider_tools", True),
        ("environment", "inherit_ambient", True),
        ("authority_ceiling", "publish", "allowed"),
        ("authority_ceiling", "merge", "allowed"),
        ("authority_ceiling", "git", "write"),
    ],
)
def test_agent_provider_profile_refuses_ambient_or_canonical_authority(
    section: str, field: str, value: object
) -> None:
    document = fx.profile_document()
    document.setdefault(section, {})[field] = value
    with pytest.raises(ValidationError):
        AgentProviderProfile.model_validate(document)


def test_agent_provider_profile_rejects_overlapping_capability_sets() -> None:
    document = fx.profile_document(optional_capabilities=["semantic_tools"])
    with pytest.raises(ValidationError, match="both required and optional"):
        AgentProviderProfile.model_validate(document)


def test_agent_provider_profile_rejects_free_form_provider_options() -> None:
    document = fx.profile_document(
        provider_options={"provider_kind": "codex", "config_toml": "sandbox = 'off'"}
    )
    with pytest.raises(ValidationError, match="extra"):
        AgentProviderProfile.model_validate(document)


def test_route_match_rejects_whole_wildcard() -> None:
    with pytest.raises(ValidationError, match="at least one field"):
        RouteMatch()


def test_route_match_reports_mutation_from_purpose_or_flag() -> None:
    assert RouteMatch(purpose=(RunPurpose.IMPLEMENT,)).mutating
    assert RouteMatch(requires_mutation=True).mutating
    assert not RouteMatch(purpose=(RunPurpose.REVIEW,)).mutating
    assert not RouteMatch(scope_kind="task").mutating


def test_route_policy_rejects_post_acceptance_fallback() -> None:
    document = fx.task_route_document(fallback_causes=["post_acceptance_transport_failure"])
    with pytest.raises(ValidationError) as caught:
        RoutePolicy.model_validate(document)
    assert caught.value.errors()[0]["type"] == "enum"


def test_route_policy_rejects_empty_and_repeated_profiles() -> None:
    with pytest.raises(ValidationError):
        RoutePolicy.model_validate(fx.task_route_document(allowed_profiles=[]))
    with pytest.raises(ValidationError, match="more than once"):
        RoutePolicy.model_validate(fx.task_route_document(allowed_profiles=["a", "a"]))


def test_authority_level_order_is_total_over_grant_levels() -> None:
    hints = typing.get_type_hints(AuthorityGrant, include_extras=True)
    assert set(AUTHORITY_LEVEL_ORDER) == set(AuthorityGrant.model_fields)
    for axis, order in AUTHORITY_LEVEL_ORDER.items():
        assert set(order) == set(typing.get_args(hints[axis]))


def test_run_scope_kind_matches_run_scope_variants() -> None:
    variants = typing.get_args(typing.get_args(RunScope)[0])
    discriminators = {typing.get_args(v.model_fields["scope_kind"].annotation)[0] for v in variants}
    assert discriminators == set(typing.get_args(RunScopeKind))


# ---- capability observation, binding, and source rows -----------------------


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"certification_evidence_ref": None}, "requires evidence"),
        ({"basis": None}, "requires basis"),
        ({"basis": "simulated"}, "native"),
        ({"status": "degraded"}, "degradation_workflow_ref"),
        ({"status": "unknown", "basis": None}, "requires reason_code"),
    ],
)
def test_capability_observation_binds_facts_to_status(
    overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        fx.observation("semantic_tools", **overrides)


def test_capability_observation_accepts_emulated_degraded_with_workflow() -> None:
    row = fx.observation(
        "interrupt_ack",
        status="degraded",
        basis="emulated",
        degradation_workflow_ref="workflow://interrupt/poll",
    )
    assert row.basis == "emulated"


def test_runtime_binding_rejects_auth_kind_the_manifest_lacks() -> None:
    with pytest.raises(ValidationError, match="does not support auth kind"):
        fx.binding(auth_kind="oauth")


def test_runtime_binding_rejects_repeated_observation() -> None:
    row = fx.observation("semantic_tools").model_dump(mode="json")
    with pytest.raises(ValidationError, match="more than once"):
        RuntimeBinding.model_validate({**fx.binding().model_dump(), "capabilities": [row, row]})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("field_path", "limits.wall_seconds"),
        ("field_path", "/Limits"),
        ("source_layer", "local"),
        ("merge_operation", "union"),
        ("source_ref", "artifact://x"),
        ("superseded_source_refs", [fx.WORKSPACE_SOURCE, fx.WORKSPACE_SOURCE]),
        ("constraint_reason", "   "),
    ],
)
def test_resolved_field_source_rejects_out_of_contract_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        ResolvedFieldSource.model_validate({**RESOLVED_SOURCE, field: value})


def test_json_pointer_value_walks_objects_and_lists() -> None:
    payload = {"capabilities": [{"capability_id": "a"}], "limits": {"wall_seconds": 5}}
    assert json_pointer_value(payload, "/limits/wall_seconds") == 5
    assert json_pointer_value(payload, "/capabilities/0/capability_id") == "a"
    with pytest.raises(KeyError):
        json_pointer_value(payload, "/capabilities/1")
    with pytest.raises(KeyError):
        json_pointer_value(payload, "/limits/cost")


def test_canonical_digest_ignores_key_order() -> None:
    assert canonical_digest({"a": 1, "b": [1, 2]}) == canonical_digest({"b": [1, 2], "a": 1})
    assert canonical_digest({"a": 1}) != canonical_digest({"a": 2})
    with pytest.raises(TypeError):
        canonical_digest({"a": object()})


# ---- compiled spec integrity -------------------------------------------------


def test_compiled_run_spec_seal_fills_every_digest() -> None:
    spec = CompiledRunSpec.seal(_sealed_fields())
    payload = spec.model_dump(mode="json")
    assert spec.scope_digest == canonical_digest(payload["run_scope"])
    for row in spec.source_map:
        assert row.effective_value_digest == canonical_digest(
            json_pointer_value(payload, row.field_path)
        )


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("limits", "wall_seconds"), 86_400),
        (("model_id",), fx.UNCERTIFIED_MODEL),
        (("scope_digest",), fx.digest("e")),
        (("source_map", 0, "effective_value_digest"), fx.digest("e")),
    ],
)
def test_compiled_run_spec_rejects_tampered_content(path: tuple[Any, ...], value: object) -> None:
    document = CompiledRunSpec.seal(_sealed_fields()).model_dump(mode="json")
    target: Any = document
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValidationError, match="does not match"):
        CompiledRunSpec.model_validate(document)


def test_compiled_run_spec_rejects_source_row_naming_no_field() -> None:
    rows = (
        ResolvedFieldSource.model_validate(
            {**RESOLVED_SOURCE, "field_path": "/limits/burst_seconds"}
        ),
    )
    with pytest.raises(KeyError):
        CompiledRunSpec.seal(_sealed_fields(source_map=rows))


def test_compiled_run_spec_rejects_purpose_disagreeing_with_scope() -> None:
    with pytest.raises(ValidationError, match="disagrees"):
        CompiledRunSpec.seal(_sealed_fields(purpose="repair"))


def test_compiled_run_spec_rejects_write_authority_outside_task_scope() -> None:
    batch = fx.review_request().run_scope
    with pytest.raises(ValidationError, match="write authority"):
        CompiledRunSpec.seal(
            _sealed_fields(run_scope=batch, purpose="review", agent_role="reviewer")
        )


def test_compiled_run_spec_rejects_mutating_purpose_under_read_only_role() -> None:
    with pytest.raises(ValidationError, match="cannot run a mutating purpose"):
        CompiledRunSpec.seal(_sealed_fields(agent_role="reviewer"))


def test_compiled_run_spec_rejects_unsorted_source_map() -> None:
    rows = tuple(reversed(_sealed_fields()["source_map"]))
    with pytest.raises(ValidationError, match="sorted by field_path"):
        CompiledRunSpec.seal(_sealed_fields(source_map=rows))


def test_compiled_run_spec_rejects_empty_source_map() -> None:
    with pytest.raises(ValidationError):
        CompiledRunSpec.seal(_sealed_fields(source_map=()))


# ---- three-state model pin ---------------------------------------------------


@pytest.mark.parametrize(
    ("listed", "certified", "state"),
    [
        (True, True, ModelPinState.CERTIFIED),
        (True, False, ModelPinState.UNCERTIFIED),
        (False, True, ModelPinState.UNKNOWN),
        (False, False, ModelPinState.UNKNOWN),
    ],
)
def test_resolve_model_pin_resolves_exactly_one_state(
    listed: bool, certified: bool, state: ModelPinState
) -> None:
    resolution = resolve_model_pin(
        "m-pin",
        requested=["m-pin"],
        listed=["m-pin"] if listed else [],
        certified=["m-pin"] if certified else [],
    )
    assert resolution.state is state
    assert [s for s in ModelPinState if s is resolution.state] == [state]


def test_resolve_model_pin_state_is_total_over_membership() -> None:
    for listed, certified in itertools.product((False, True), repeat=2):
        resolution = resolve_model_pin(
            "m",
            requested=(),
            listed=("m",) * listed,
            certified=("m",) * certified,
        )
        assert resolution.state in set(ModelPinState)


def test_resolve_model_pin_certified_allows_unattended_dispatch() -> None:
    resolution = resolve_model_pin("a", requested=["a"], listed=["a"], certified=["a"])
    assert resolution.unattended_dispatch
    assert resolution.selectable
    assert resolution.certified_alternative is None


def test_resolve_model_pin_uncertified_names_certified_alternative() -> None:
    resolution = resolve_model_pin(
        "b", requested=["c", "b", "a"], listed=["a", "b", "c"], certified=["a", "c"]
    )
    assert resolution.state is ModelPinState.UNCERTIFIED
    assert resolution.selectable
    assert not resolution.unattended_dispatch
    assert resolution.certified_alternative == "c"


def test_resolve_model_pin_uncertified_without_alternative() -> None:
    resolution = resolve_model_pin("b", requested=["b", "z"], listed=["b"], certified=["z"])
    assert resolution.certified_alternative is None


def test_resolve_model_pin_unknown_cannot_be_saved() -> None:
    resolution = resolve_model_pin("gone", requested=[], listed=[], certified=["gone"])
    assert resolution.state is ModelPinState.UNKNOWN
    assert not resolution.selectable
    assert not resolution.unattended_dispatch


def test_model_pin_resolution_round_trips_and_refuses_unknown_state() -> None:
    resolution = resolve_model_pin("a", requested=["a"], listed=["a"], certified=[])
    assert ModelPinResolution.model_validate_json(resolution.model_dump_json()) == resolution
    with pytest.raises(ValidationError):
        ModelPinResolution.model_validate({"model_id": "a", "state": "maybe"})
