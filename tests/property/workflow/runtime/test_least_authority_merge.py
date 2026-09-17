"""Least-authority merge over generated layer stacks.

Every expectation here is computed independently of the merge engine from
the generated layers themselves: grants intersect, denials union,
ceilings take the minimum, floors take the maximum, and a denied tool
leaves the grant. A second family compiles whole Runs with generated
repository and Run override layers, including layers that try to widen,
and checks that no secret, path, or network authority ends up wider than
the operator profile granted.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from eawf.kernel.config.providers import LayeredProfile, ProviderConfiguration
from eawf.kernel.runtime.compiled import (
    AuthorityCeiling,
    ContextNarrowing,
    EnvironmentNarrowing,
    LimitCeilings,
    ModelNarrowing,
    PolicyOverlay,
    SandboxNarrowing,
    ToolNarrowing,
)
from eawf.kernel.runtime.provider import (
    AUTHORITY_LEVEL_ORDER,
    NETWORK_MODE_ORDER,
    SANDBOX_MODE_ORDER,
    SOURCE_LAYER_PRECEDENCE,
    AgentProviderProfile,
    SourceLayer,
)
from eawf.workflow.runtime.compile import compile_run_spec
from eawf.workflow.runtime.merge import merge_policy_layers
from tests import _provider_helpers as fx

pytestmark = pytest.mark.property

TOOLS = ("repo_read", "repo_search", "diff_read", "workspace_apply_patch", "submit_report")
VARIABLES = ("LANG", "TZ", "PAGER", "API_TOKEN")
MODELS = ("m-a", "m-b", "m-c")
CAPABILITIES = ("semantic_tools", "event_replay", "managed_sandbox", "usage_receipts")
LIMIT_BOUNDS = {
    "wall_seconds": (1, 86_400),
    "output_bytes": (1, 16_777_216),
    "child_runs": (0, 32),
    "tool_calls": (0, 100_000),
    "cost_microusd": (0, 10**9),
    "concurrency": (1, 32),
}
ORDERS: dict[str, Sequence[str]] = {
    **{f"/authority/{axis}": order for axis, order in AUTHORITY_LEVEL_ORDER.items()},
    "/sandbox/mode": SANDBOX_MODE_ORDER,
    "/sandbox/network": NETWORK_MODE_ORDER,
}
OVERRIDE_LAYERS: tuple[SourceLayer, ...] = (
    "repository",
    "track_policy",
    "milestone_policy",
    "batch_policy",
    "run_override",
)


def _subset(members: Sequence[str]) -> st.SearchStrategy[tuple[str, ...]]:
    return st.lists(st.sampled_from(members), unique=True).map(tuple)


def _maybe(strategy: st.SearchStrategy[Any]) -> st.SearchStrategy[Any]:
    return st.none() | strategy


@st.composite
def overlays(
    draw: st.DrawFn,
    layers: Sequence[SourceLayer] = SOURCE_LAYER_PRECEDENCE,
    *,
    authority_only: bool = False,
) -> PolicyOverlay:
    """Draw one overlay that may declare, omit, or try to widen any field.

    With *authority_only*, the model and context fields stay undeclared so
    a whole-Run compile cannot fail on an emptied model set or a context
    floor above its ceiling, and every example reaches the assertions.
    """
    layer = draw(st.sampled_from(layers))
    axes = {
        axis: draw(_maybe(st.sampled_from(order))) for axis, order in AUTHORITY_LEVEL_ORDER.items()
    }
    model_policy = ModelNarrowing()
    context_policy = ContextNarrowing()
    if not authority_only:
        model_policy = ModelNarrowing(
            allowed=draw(_maybe(_subset(MODELS))), default=draw(_maybe(st.sampled_from(MODELS)))
        )
        context_policy = ContextNarrowing(
            max_input_tokens=draw(_maybe(st.integers(1024, 400_000))),
            minimum_remaining_tokens=draw(_maybe(st.integers(512, 400_000))),
        )
    return PolicyOverlay(
        layer=layer,
        source_ref=f"policy://{layer.replace('_', '-')}/n{draw(st.integers(0, 3))}",
        model_policy=model_policy,
        context_policy=context_policy,
        tool_policy=ToolNarrowing(
            allow=draw(_maybe(_subset(TOOLS))),
            deny=draw(_subset(TOOLS)),
            max_parallel_calls=draw(_maybe(st.integers(1, 32))),
        ),
        sandbox=SandboxNarrowing(
            mode=draw(_maybe(st.sampled_from(SANDBOX_MODE_ORDER))),
            network=draw(_maybe(st.sampled_from(NETWORK_MODE_ORDER))),
        ),
        environment=EnvironmentNarrowing(allowed_variable_names=draw(_maybe(_subset(VARIABLES)))),
        limits=LimitCeilings(
            **{
                name: draw(_maybe(st.integers(low, high)))
                for name, (low, high) in LIMIT_BOUNDS.items()
            }
        ),
        authority_ceiling=AuthorityCeiling(**axes),
        required_capabilities=draw(_subset(CAPABILITIES)),
    )


STACKS = st.lists(overlays(), min_size=1, max_size=8)


def _ordered(stack: Sequence[PolicyOverlay]) -> list[PolicyOverlay]:
    return sorted(stack, key=lambda o: SOURCE_LAYER_PRECEDENCE.index(o.layer))


def _declared(stack: Sequence[PolicyOverlay], attribute: str) -> list[tuple[PolicyOverlay, Any]]:
    """Return ``(layer, value)`` for every layer declaring dotted *attribute*."""
    found = []
    for overlay in _ordered(stack):
        value: Any = overlay
        for part in attribute.split("."):
            value = getattr(value, part)
        if value is not None:
            found.append((overlay, value))
    return found


SCALAR_RULES = {
    **{f"/authority/{axis}": (f"authority_ceiling.{axis}", min) for axis in AUTHORITY_LEVEL_ORDER},
    **{f"/limits/{name}": (f"limits.{name}", min) for name in LIMIT_BOUNDS},
    "/sandbox/mode": ("sandbox.mode", min),
    "/sandbox/network": ("sandbox.network", min),
    "/context_policy/max_input_tokens": ("context_policy.max_input_tokens", min),
    "/tool_policy/max_parallel_calls": ("tool_policy.max_parallel_calls", min),
    "/context_policy/minimum_remaining_tokens": ("context_policy.minimum_remaining_tokens", max),
}


@settings(max_examples=300, deadline=None)
@given(stack=STACKS)
def test_merge_policy_layers_ceilings_take_minimum_and_floors_maximum(
    stack: list[PolicyOverlay],
) -> None:
    merged = merge_policy_layers(stack)
    for path, (attribute, pick) in SCALAR_RULES.items():
        declared = _declared(stack, attribute)
        if not declared:
            assert path not in merged.values
            continue
        order = ORDERS.get(path)
        expected = pick((v for _, v in declared), key=order.index if order else None)
        assert merged.values[path] == expected, path
        source = merged.provenance[path]
        assert source.merge_operation == ("minimum" if pick is min else "maximum")
        holders = [o for o, v in declared if v == expected]
        assert (source.source_layer, source.source_ref) == (
            holders[-1].layer,
            holders[-1].source_ref,
        )


@settings(max_examples=300, deadline=None)
@given(stack=STACKS)
def test_merge_policy_layers_grants_intersect_and_denials_union(
    stack: list[PolicyOverlay],
) -> None:
    merged = merge_policy_layers(stack)
    denied = sorted({tool for o in stack for tool in o.tool_policy.deny})
    if denied:
        assert merged.values["/tool_policy/deny"] == tuple(denied)
        assert merged.provenance["/tool_policy/deny"].merge_operation == "union_deny"
    else:
        assert "/tool_policy/deny" not in merged.values
    grants = _declared(stack, "tool_policy.allow")
    if grants:
        expected = [
            tool
            for tool in grants[0][1]
            if all(tool in allow for _, allow in grants) and tool not in denied
        ]
        assert merged.values["/tool_policy/allow"] == tuple(expected)
        assert merged.provenance["/tool_policy/allow"].merge_operation == "intersection"
    names = _declared(stack, "environment.allowed_variable_names")
    if names:
        expected_names = [n for n in names[0][1] if all(n in v for _, v in names)]
        assert merged.values["/environment/allowed_variable_names"] == tuple(expected_names)
    models = _declared(stack, "model_policy.allowed")
    expected_models = [
        m for m in (models[0][1] if models else ()) if all(m in v for _, v in models)
    ]
    assert merged.model_allowed == tuple(expected_models)
    if expected_models:
        assert merged.values["/model_id"] in expected_models
    else:
        assert "/model_id" not in merged.values


@settings(max_examples=300, deadline=None)
@given(stack=STACKS)
def test_merge_policy_layers_required_capabilities_union(stack: list[PolicyOverlay]) -> None:
    merged = merge_policy_layers(stack)
    required = sorted({c for o in stack for c in o.required_capabilities})
    if required:
        assert merged.values["/capabilities"] == tuple(required)
        assert merged.provenance["/capabilities"].merge_operation == "maximum"
    else:
        assert "/capabilities" not in merged.values


@settings(max_examples=200, deadline=None)
@given(stack=STACKS, extra=overlays())
def test_merge_policy_layers_adding_a_layer_never_widens(
    stack: list[PolicyOverlay], extra: PolicyOverlay
) -> None:
    before = merge_policy_layers(stack).values
    after = merge_policy_layers([*stack, extra]).values
    for path, (_, pick) in SCALAR_RULES.items():
        if path not in before or before[path] is None:
            continue
        order = ORDERS.get(path)
        rank = order.index if order else (lambda value: value)
        if pick is min:
            assert rank(after[path]) <= rank(before[path]), path
        else:
            assert rank(after[path]) >= rank(before[path]), path
    for path in ("/tool_policy/allow", "/environment/allowed_variable_names"):
        if path in before:
            assert set(after[path]) <= set(before[path]), path
    for path in ("/tool_policy/deny", "/capabilities"):
        if path in before:
            assert set(after[path]) >= set(before[path]), path


@settings(max_examples=200, deadline=None)
@given(stack=STACKS)
def test_merge_policy_layers_names_a_declaring_layer_for_every_value(
    stack: list[PolicyOverlay],
) -> None:
    merged = merge_policy_layers(stack)
    assert set(merged.values) == set(merged.provenance)
    refs = {(o.layer, o.source_ref) for o in stack}
    for source in merged.provenance.values():
        assert (source.source_layer, source.source_ref) in refs
        assert source.source_ref not in source.superseded_source_refs


# ---- whole-Run compilation under hostile overrides ---------------------------

PROFILE = AgentProviderProfile.model_validate(
    fx.profile_document(
        sandbox={
            "mode": "workspace_write",
            "network": "deny",
            "filesystem_policy_ref": "policy://filesystem/task-workspace",
            "network_policy_ref": "policy://network/package-index",
        },
        tool_policy={"allow": ["repo_read", "repo_search", "workspace_apply_patch"]},
        model_policy={"allowed": [fx.CERTIFIED_MODEL], "default": fx.CERTIFIED_MODEL},
    )
)
BASE = fx.configuration(profiles=[PROFILE.model_dump(mode="json")])


def _rank(order: Sequence[str], value: str) -> int:
    return list(order).index(value)


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    repository=st.lists(overlays(layers=("repository",), authority_only=True), max_size=3),
    caller=st.lists(overlays(layers=OVERRIDE_LAYERS[1:], authority_only=True), max_size=3),
    task=st.booleans(),
)
def test_compile_run_spec_overrides_never_widen_secret_path_or_network(
    repository: list[PolicyOverlay], caller: list[PolicyOverlay], task: bool
) -> None:
    configuration = ProviderConfiguration(
        profiles=(LayeredProfile(layer="workspace", profile=PROFILE),),
        routes=BASE.routes,
        repository_overlays=tuple(repository),
    )
    request = fx.task_request() if task else fx.review_request()
    spec = compile_run_spec(
        request,
        configuration=configuration,
        bindings=[fx.binding()],
        compiled_at=fx.COMPILED_AT,
        policy_overlays=caller,
    )
    sandbox, environment, authority = (
        PROFILE.sandbox,
        PROFILE.environment,
        PROFILE.authority_ceiling,
    )
    assert spec.sandbox.filesystem_policy_ref == sandbox.filesystem_policy_ref
    assert spec.sandbox.network_policy_ref == sandbox.network_policy_ref
    assert _rank(NETWORK_MODE_ORDER, spec.sandbox.network) <= _rank(
        NETWORK_MODE_ORDER, sandbox.network
    )
    assert _rank(SANDBOX_MODE_ORDER, spec.sandbox.mode) <= _rank(SANDBOX_MODE_ORDER, sandbox.mode)
    assert spec.environment.secret_broker_ref == environment.secret_broker_ref
    assert spec.environment.environment_class_ref == environment.environment_class_ref
    assert set(spec.environment.allowed_variable_names) <= set(environment.allowed_variable_names)
    assert spec.auth_profile_ref == PROFILE.auth_profile_ref
    for axis, order in AUTHORITY_LEVEL_ORDER.items():
        assert _rank(order, getattr(spec.authority, axis)) <= _rank(
            order, getattr(authority, axis)
        ), axis
    assert set(spec.tool_policy.allow) <= set(PROFILE.tool_policy.allow)
    for name in LIMIT_BOUNDS:
        ceiling = getattr(PROFILE.limits, name)
        if ceiling is not None:
            assert getattr(spec.limits, name) <= ceiling, name
    if not task:
        assert spec.sandbox.mode == "read_only"
        assert spec.authority.workspace != "scoped_write"
