"""A route that can select a mutating Run is a write route.

A route match leaves a field empty to mean "any", so a match that sets
neither ``requires_mutation`` nor a purpose also selects implement,
integrate, and repair Runs. The loader treats such a route as mutating
and refuses it with ``mutating_route_unmanaged`` when an allowed profile
lacks a ``workspace_write`` sandbox under a filesystem policy. The
compiler refuses the same unmanaged write on its own, for a
configuration built without the loader, yet still compiles a read-only
Run through that profile because the sandbox it emits is read-only.
"""

from __future__ import annotations

from typing import Any

import pytest

from eawf.kernel.config.providers import (
    LayeredProfile,
    LayeredRoute,
    ProviderConfigError,
    ProviderConfigRejection,
    ProviderConfiguration,
    parse_provider_configuration,
)
from eawf.kernel.runtime.compiled import CompiledRunSpec, RunCompileRequest
from eawf.kernel.runtime.provider import AgentProviderProfile, RoutePolicy
from eawf.workflow.runtime.compile import CompileRejection, RunCompileError, compile_run_spec
from tests import _provider_helpers as fx

pytestmark = pytest.mark.contract

REPOSITORY_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/repository/REP-EAWF"
FILESYSTEM_POLICY = "policy://filesystem/task-workspace"
UNMANAGED_WRITE: dict[str, Any] = {"mode": "workspace_write", "network": "deny"}
MANAGED_WRITE: dict[str, Any] = {**UNMANAGED_WRITE, "filesystem_policy_ref": FILESYSTEM_POLICY}

#: Matches that leave ``requires_mutation`` unset and list no purpose.
WILDCARD_MATCHES: dict[str, dict[str, Any]] = {
    "scope-kind": {"scope_kind": "task"},
    "agent-role": {"agent_role": ["executor"]},
    "repository": {"repository_refs": [REPOSITORY_URN]},
    "regime": {"regime": ["steady"]},
}
#: Matches that leave ``requires_mutation`` unset and list a mutating purpose.
MUTATING_PURPOSE_MATCHES: dict[str, dict[str, Any]] = {
    "mutating-purpose": {"purpose": ["integrate"]},
    "mixed-purposes": {"purpose": ["review", "repair"]},
}
WRITE_MATCHES = {**WILDCARD_MATCHES, **MUTATING_PURPOSE_MATCHES}
#: Sandboxes that do not give a Run a writable workspace under a policy.
UNMANAGED_SANDBOXES: dict[str, dict[str, Any]] = {
    "write-without-policy": UNMANAGED_WRITE,
    "read-only": {"mode": "read_only"},
    "read-only-with-policy": {"mode": "read_only", "filesystem_policy_ref": FILESYSTEM_POLICY},
}
#: Matches a route declares when it only ever selects read-only Runs.
READ_ONLY_MATCHES: dict[str, dict[str, Any]] = {
    "flag-false": {"scope_kind": "task", "requires_mutation": False},
    "read-only-purposes": {"agent_role": ["executor"], "purpose": ["review", "audit"]},
}


def _layers(match: dict[str, Any], sandbox: dict[str, Any]) -> dict[Any, Any]:
    return fx.documents(
        profiles=[fx.profile_document(sandbox=sandbox)],
        routes=[fx.task_route_document(match=match)],
    )


def _refused_at_load(layers: dict[Any, Any]) -> ProviderConfigError:
    with pytest.raises(ProviderConfigError) as caught:
        parse_provider_configuration(layers, registry=fx.registry())
    assert caught.value.code is ProviderConfigRejection.MUTATING_ROUTE_UNMANAGED
    return caught.value


def _unloaded_configuration(
    match: dict[str, Any], sandbox: dict[str, Any]
) -> ProviderConfiguration:
    """Build a configuration directly, as a caller that skips the loader would."""
    profile = AgentProviderProfile.model_validate(fx.profile_document(sandbox=sandbox))
    route = RoutePolicy.model_validate(fx.task_route_document(match=match))
    return ProviderConfiguration(
        profiles=(LayeredProfile(layer="workspace", profile=profile),),
        routes=(LayeredRoute(layer="workspace", route=route),),
        repository_overlays=(),
    )


def _compile(request: RunCompileRequest, configuration: ProviderConfiguration) -> CompiledRunSpec:
    return compile_run_spec(
        request, configuration=configuration, bindings=[fx.binding()], compiled_at=fx.COMPILED_AT
    )


def _implement_request() -> RunCompileRequest:
    """Return an implement request every wildcard match selects."""
    return fx.task_request(repository_ref=REPOSITORY_URN)


# ---- load ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "sandbox", list(UNMANAGED_SANDBOXES.values()), ids=list(UNMANAGED_SANDBOXES)
)
@pytest.mark.parametrize("match", list(WRITE_MATCHES.values()), ids=list(WRITE_MATCHES))
def test_parse_provider_configuration_refuses_unmanaged_write_route(
    match: dict[str, Any], sandbox: dict[str, Any]
) -> None:
    error = _refused_at_load(_layers(match, sandbox))
    assert "'fixture_codex'" in str(error)


@pytest.mark.parametrize("match", list(WRITE_MATCHES.values()), ids=list(WRITE_MATCHES))
def test_parse_provider_configuration_accepts_managed_write_route(match: dict[str, Any]) -> None:
    loaded = parse_provider_configuration(_layers(match, MANAGED_WRITE), registry=fx.registry())
    routes = {entry.route.route_id: entry.route for entry in loaded.routes}
    assert routes["mutating-task"].match.mutating


@pytest.mark.parametrize("match", list(READ_ONLY_MATCHES.values()), ids=list(READ_ONLY_MATCHES))
def test_parse_provider_configuration_accepts_read_only_route_on_unmanaged_profile(
    match: dict[str, Any],
) -> None:
    loaded = parse_provider_configuration(_layers(match, UNMANAGED_WRITE), registry=fx.registry())
    assert not any(entry.route.match.mutating for entry in loaded.routes)


def test_parse_provider_configuration_names_the_unmanaged_profile() -> None:
    managed = fx.profile_document(sandbox=MANAGED_WRITE)
    unmanaged = fx.profile_document(profile_id="fixture_unmanaged", sandbox=UNMANAGED_WRITE)
    route = fx.task_route_document(
        match={"scope_kind": "task"}, allowed_profiles=["fixture_codex", "fixture_unmanaged"]
    )
    error = _refused_at_load(fx.documents(profiles=[managed, unmanaged], routes=[route]))
    assert "'fixture_unmanaged'" in str(error)
    assert "'fixture_codex'" not in str(error)
    assert "requires_mutation false" in str(error)


# ---- compile -------------------------------------------------------------------


@pytest.mark.parametrize("match", list(WILDCARD_MATCHES.values()), ids=list(WILDCARD_MATCHES))
def test_wildcard_write_route_is_refused_at_load_and_at_compile(match: dict[str, Any]) -> None:
    _refused_at_load(_layers(match, UNMANAGED_WRITE))
    configuration = _unloaded_configuration(match, UNMANAGED_WRITE)
    with pytest.raises(RunCompileError) as caught:
        _compile(_implement_request(), configuration)
    assert caught.value.code is CompileRejection.MUTATING_ROUTE_UNMANAGED
    assert str(caught.value).startswith("mutating_route_unmanaged: ")
    assert "'fixture_codex'" in str(caught.value)


@pytest.mark.parametrize("match", list(WILDCARD_MATCHES.values()), ids=list(WILDCARD_MATCHES))
def test_compile_run_spec_emits_managed_write_sandbox_through_wildcard_route(
    match: dict[str, Any],
) -> None:
    configuration = parse_provider_configuration(
        _layers(match, MANAGED_WRITE), registry=fx.registry()
    )
    spec = _compile(_implement_request(), configuration)
    assert spec.route_policy_ref == "route://mutating-task"
    assert (spec.sandbox.mode, spec.sandbox.filesystem_policy_ref) == (
        "workspace_write",
        FILESYSTEM_POLICY,
    )


def test_compile_run_spec_admits_read_only_run_through_unmanaged_profile() -> None:
    configuration = _unloaded_configuration({"scope_kind": "batch"}, UNMANAGED_WRITE)
    spec = _compile(fx.review_request(), configuration)
    assert (spec.sandbox.mode, spec.sandbox.filesystem_policy_ref) == ("read_only", None)
