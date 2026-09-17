"""Canonical provider configuration: one typed rejection per loader rule.

The loader is the only path from the ``runtime`` config sections to
provider records, so its contract is the rule table: each rule refuses
with its own :class:`ProviderConfigRejection` member, and a caller
branches on that member rather than on message text. The file-backed
entry point is exercised against real layer files under a temporary
home, workspace, and repository.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from eawf.kernel.config.providers import (
    ProviderConfigError,
    ProviderConfigRejection,
    ProviderRegistry,
    load_provider_configuration,
    parse_provider_configuration,
)
from eawf.surfaces.cli.errors import ValidationError as CliValidationError
from tests import _provider_helpers as fx

pytestmark = pytest.mark.contract

Rule = ProviderConfigRejection
REPOSITORY_BROKER = "secrets://repository/broker"


def _patch(**fields: Any) -> dict[str, Any]:
    """Return a repository patch of the fixture profile."""
    return {"profile_id": "fixture_codex", "source_ref": fx.REPOSITORY_SOURCE, **fields}


def _repository(*patches: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"profiles": list(patches), **extra}


def _reject(layers: Any, code: Rule, registry: ProviderRegistry | None = None) -> None:
    with pytest.raises(ProviderConfigError) as caught:
        parse_provider_configuration(layers, registry=registry or fx.registry())
    assert caught.value.code is code
    assert str(caught.value).startswith(f"{code.value}: ")


# ---- accepted configuration --------------------------------------------------


def test_parse_provider_configuration_accepts_canonical_layers() -> None:
    loaded = fx.configuration()
    assert [(p.layer, p.profile.profile_id) for p in loaded.profiles] == [
        ("workspace", "fixture_codex")
    ]
    assert sorted((r.layer, r.route.route_id) for r in loaded.routes) == [
        ("global", "review"),
        ("workspace", "mutating-task"),
    ]
    assert loaded.repository_overlays == ()


def test_parse_provider_configuration_workspace_replaces_global_record() -> None:
    layers = fx.documents()
    layers["global"]["profiles"] = [fx.profile_document(revision=1, source_ref=fx.GLOBAL_SOURCE)]
    layers["workspace"]["profiles"] = [fx.profile_document(revision=7)]
    loaded = parse_provider_configuration(layers, registry=fx.registry())
    entry = loaded.profile("fixture_codex")
    assert entry is not None
    assert (entry.layer, entry.profile.revision) == ("workspace", 7)
    assert loaded.profile("absent") is None


def test_parse_provider_configuration_accepts_narrowing_repository_patch() -> None:
    patch = _patch(
        tool_policy={"allow": ["repo_read"], "deny": ["workspace_apply_patch"]},
        limits={"wall_seconds": 600},
        sandbox={"mode": "read_only"},
        environment={"allowed_variable_names": ["LANG"]},
        authority_ceiling={"credentials": "none"},
        model_policy={"allowed": [fx.CERTIFIED_MODEL]},
        context_policy={"minimum_remaining_tokens": 8192},
        required_capabilities=["usage_receipts"],
    )
    loaded = fx.configuration(repository=_repository(patch))
    (overlay,) = loaded.repository_overlays
    assert overlay.layer == "repository"
    assert overlay.limits.wall_seconds == 600


def test_parse_provider_configuration_accepts_empty_layers() -> None:
    loaded = parse_provider_configuration({}, registry=fx.registry())
    assert (loaded.profiles, loaded.routes, loaded.repository_overlays) == ((), (), ())


def test_provider_config_error_maps_to_validation_bucket() -> None:
    error = ProviderConfigError(Rule.SCHEMA_INVALID, "bad")
    assert isinstance(error, CliValidationError)
    assert error.kind == "schema_invalid"


# ---- one rejection per operator-layer rule ----------------------------------


def test_loader_rejects_unregistered_driver_ref() -> None:
    profile = fx.profile_document(driver_ref="driver://unregistered/v1")
    _reject(fx.documents(profiles=[profile]), Rule.UNREGISTERED_DRIVER_REF)


def test_loader_rejects_unregistered_auth_ref() -> None:
    profile = fx.profile_document(auth_profile_ref="auth://operator/unknown")
    _reject(fx.documents(profiles=[profile]), Rule.UNREGISTERED_AUTH_REF)


def test_loader_rejects_empty_model_allowlist() -> None:
    profile = fx.profile_document(model_policy={"allowed": [], "default": fx.CERTIFIED_MODEL})
    _reject(fx.documents(profiles=[profile]), Rule.EMPTY_MODEL_ALLOWLIST)


def test_loader_rejects_empty_model_allowlist_in_repository_patch() -> None:
    patch = _patch(model_policy={"allowed": []})
    _reject(fx.documents(repository=_repository(patch)), Rule.EMPTY_MODEL_ALLOWLIST)


@pytest.mark.parametrize("where", ["profile_required", "profile_optional", "route", "patch"])
def test_loader_rejects_capability_absent_from_certification_schema(where: str) -> None:
    capability = "teleport"
    profile = fx.profile_document()
    route = fx.task_route_document()
    repository = None
    if where == "profile_required":
        profile["required_capabilities"].append(capability)
    elif where == "profile_optional":
        profile["optional_capabilities"].append(capability)
    elif where == "route":
        route["required_capabilities"].append(capability)
    else:
        repository = _repository(_patch(required_capabilities=[capability]))
    layers = fx.documents(profiles=[profile], routes=[route], repository=repository)
    _reject(layers, Rule.CAPABILITY_NOT_CERTIFIABLE)


def test_loader_rejects_post_acceptance_fallback() -> None:
    route = fx.task_route_document(fallback_causes=["post_acceptance_transport_failure"])
    _reject(fx.documents(routes=[route]), Rule.FALLBACK_AFTER_ACCEPTANCE)


@pytest.mark.parametrize(
    "sandbox",
    [
        {"mode": "read_only", "filesystem_policy_ref": "policy://filesystem/task-workspace"},
        {"mode": "workspace_write"},
    ],
)
def test_loader_rejects_mutating_route_without_managed_isolation(sandbox: dict[str, Any]) -> None:
    profile = fx.profile_document(sandbox=sandbox)
    _reject(fx.documents(profiles=[profile]), Rule.MUTATING_ROUTE_UNMANAGED)


def test_loader_rejects_flagged_mutating_route_without_managed_isolation() -> None:
    profile = fx.profile_document(sandbox={"mode": "read_only"})
    route = fx.review_route_document(match={"requires_mutation": True})
    _reject(fx.documents(profiles=[profile], global_routes=[route]), Rule.MUTATING_ROUTE_UNMANAGED)


def test_loader_accepts_read_only_route_on_read_only_profile() -> None:
    profile = fx.profile_document(sandbox={"mode": "read_only"})
    loaded = fx.configuration(profiles=[profile], routes=[])
    assert [r.route.route_id for r in loaded.routes] == ["review"]


def test_loader_rejects_route_naming_unknown_profile() -> None:
    route = fx.task_route_document(allowed_profiles=["fixture_codex", "ghost"])
    _reject(fx.documents(routes=[route]), Rule.ROUTE_PROFILE_UNKNOWN)


def test_loader_rejects_duplicate_record_id_in_one_layer() -> None:
    layers = fx.documents(profiles=[fx.profile_document(), fx.profile_document(revision=2)])
    _reject(layers, Rule.DUPLICATE_RECORD_ID)


@pytest.mark.parametrize(
    "layers",
    [
        {"workspace": {"profiles": [fx.profile_document(executable="/bin/sh")]}},
        {"workspace": {"profiles": {"fixture_codex": fx.profile_document()}}},
        {"workspace": {"profiles": ["fixture_codex"]}},
        {"global": {"routes": [fx.review_route_document(match={})]}},
    ],
    ids=["unknown-key", "profiles-not-list", "entry-not-mapping", "wildcard-match"],
)
def test_loader_rejects_schema_invalid_documents(layers: dict[str, Any]) -> None:
    _reject(layers, Rule.SCHEMA_INVALID)


# ---- one rejection per repository-layer rule ---------------------------------


@pytest.mark.parametrize(
    "patch",
    [
        _patch(profile_id="repository_only"),
        {**_patch(), "driver_ref": fx.DRIVER_REF},
    ],
    ids=["unknown-profile-id", "names-a-driver"],
)
def test_loader_rejects_repository_adding_profile(patch: dict[str, Any]) -> None:
    _reject(fx.documents(repository=_repository(patch)), Rule.REPOSITORY_ADDS_PROFILE)


def test_loader_rejects_repository_adding_route() -> None:
    repository = _repository(routes=[fx.task_route_document(route_id="repo-route")])
    _reject(fx.documents(repository=repository), Rule.REPOSITORY_ADDS_ROUTE)


@pytest.mark.parametrize(
    "patch",
    [
        _patch(tool_policy={"allow": ["repo_read", "run_scoped_command"]}),
        _patch(tool_policy={"command_family_refs": ["pytest"]}),
    ],
    ids=["allow-new-tool", "command-family"],
)
def test_loader_rejects_repository_adding_tools(patch: dict[str, Any]) -> None:
    _reject(fx.documents(repository=_repository(patch)), Rule.REPOSITORY_ADDS_TOOLS)


@pytest.mark.parametrize(
    "patch",
    [
        _patch(sandbox={"network": "brokered_allowlist"}),
        _patch(sandbox={"network_policy_ref": "policy://network/anywhere"}),
    ],
    ids=["brokered-network", "network-policy"],
)
def test_loader_rejects_repository_adding_network(patch: dict[str, Any]) -> None:
    _reject(fx.documents(repository=_repository(patch)), Rule.REPOSITORY_ADDS_NETWORK)


@pytest.mark.parametrize(
    ("patch", "profile_overrides"),
    [
        (_patch(environment={"allowed_variable_names": ["LANG", "API_TOKEN"]}), {}),
        (
            _patch(authority_ceiling={"credentials": "reference_only"}),
            {
                "authority_ceiling": {
                    "state": "proposal_only",
                    "workspace": "scoped_write",
                    "credentials": "none",
                }
            },
        ),
        (_patch(environment={"secret_broker_ref": REPOSITORY_BROKER}), {}),
        (_patch(environment={"environment_class_ref": "environment://host/full"}), {}),
        ({**_patch(), "auth_profile_ref": fx.OTHER_AUTH_REF}, {}),
    ],
    ids=["variable-name", "credential-level", "secret-broker", "environment-class", "auth-ref"],
)
def test_loader_rejects_repository_adding_credentials(
    patch: dict[str, Any], profile_overrides: dict[str, Any]
) -> None:
    layers = fx.documents(
        profiles=[fx.profile_document(**profile_overrides)], repository=_repository(patch)
    )
    _reject(layers, Rule.REPOSITORY_ADDS_CREDENTIALS)


@pytest.mark.parametrize(
    ("patch", "profile_overrides"),
    [
        (_patch(limits={"wall_seconds": 9000}), {}),
        (_patch(context_policy={"max_input_tokens": 262_144}), {}),
        (_patch(tool_policy={"max_parallel_calls": 8}), {}),
        (_patch(model_policy={"allowed": [fx.CERTIFIED_MODEL, "gpt-9"]}), {}),
        (_patch(model_policy={"default": "gpt-9"}), {}),
        (_patch(authority_ceiling={"git": "read_metadata"}), {"authority_ceiling": {}}),
        (
            _patch(sandbox={"mode": "workspace_write"}),
            {"sandbox": {"mode": "read_only"}},
        ),
        (_patch(sandbox={"filesystem_policy_ref": "policy://filesystem/everything"}), {}),
    ],
    ids=[
        "limit",
        "context-ceiling",
        "parallel-calls",
        "model-allowlist",
        "model-default",
        "authority-axis",
        "sandbox-mode",
        "filesystem-policy",
    ],
)
def test_loader_rejects_repository_widening_authority(
    patch: dict[str, Any], profile_overrides: dict[str, Any]
) -> None:
    layers = fx.documents(
        profiles=[fx.profile_document(**profile_overrides)],
        routes=[],
        repository=_repository(patch),
    )
    _reject(layers, Rule.REPOSITORY_WIDENS_AUTHORITY)


def test_loader_rejects_repository_patch_naming_its_layer() -> None:
    patch = _patch(layer="run_override")
    _reject(fx.documents(repository=_repository(patch)), Rule.SCHEMA_INVALID)


def test_loader_checks_unscoped_patch_against_every_profile() -> None:
    second = fx.profile_document(
        profile_id="fixture_claude",
        driver_ref=fx.OTHER_DRIVER_REF,
        auth_profile_ref=fx.OTHER_AUTH_REF,
        tool_policy={"allow": ["repo_read"]},
        provider_options={"provider_kind": "claude"},
    )
    patch = {"source_ref": fx.REPOSITORY_SOURCE, "tool_policy": {"allow": ["repo_search"]}}
    layers = fx.documents(profiles=[fx.profile_document(), second], repository=_repository(patch))
    _reject(layers, Rule.REPOSITORY_ADDS_TOOLS)


# ---- file-backed loading -----------------------------------------------------


def _write_layer(path: Path, runtime: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"runtime": {"default": "claude-code", **runtime}}), encoding="utf-8"
    )


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Return a workspace and repository root under an isolated home."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    layers = fx.documents(repository=_repository(_patch(limits={"wall_seconds": 900})))
    _write_layer(home / ".config" / "eawf" / "config.yaml", layers["global"])
    workspace, repository = tmp_path / "workspace", tmp_path / "workspace" / "repo"
    _write_layer(workspace / ".ea" / "config.yaml", layers["workspace"])
    _write_layer(repository / ".ea" / "config.yaml", layers["repository"])
    return workspace, repository


def test_load_provider_configuration_reads_three_layer_files(roots: tuple[Path, Path]) -> None:
    workspace, repository = roots
    loaded = load_provider_configuration(
        workspace=workspace, repository=repository, registry=fx.registry()
    )
    assert {r.layer for r in loaded.routes} == {"global", "workspace"}
    assert [o.limits.wall_seconds for o in loaded.repository_overlays] == [900]


def test_load_provider_configuration_without_repository_skips_its_layer(
    roots: tuple[Path, Path],
) -> None:
    workspace, _ = roots
    loaded = load_provider_configuration(
        workspace=workspace, repository=None, registry=fx.registry()
    )
    assert loaded.repository_overlays == ()


def test_load_provider_configuration_treats_missing_files_as_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    loaded = load_provider_configuration(
        workspace=tmp_path / "ws", repository=tmp_path / "repo", registry=fx.registry()
    )
    assert loaded.profiles == ()


def test_load_provider_configuration_rejects_shared_layer_path(roots: tuple[Path, Path]) -> None:
    workspace, _ = roots
    with pytest.raises(ProviderConfigError) as caught:
        load_provider_configuration(
            workspace=workspace, repository=workspace, registry=fx.registry()
        )
    assert caught.value.code is Rule.AMBIGUOUS_LAYER_PATH


def test_load_provider_configuration_rejects_unreadable_layer(roots: tuple[Path, Path]) -> None:
    workspace, repository = roots
    (repository / ".ea" / "config.yaml").write_text("runtime: [unclosed", encoding="utf-8")
    with pytest.raises(ProviderConfigError) as caught:
        load_provider_configuration(
            workspace=workspace, repository=repository, registry=fx.registry()
        )
    assert caught.value.code is Rule.LAYER_UNREADABLE


def test_load_provider_configuration_rejects_non_mapping_runtime(
    roots: tuple[Path, Path],
) -> None:
    workspace, repository = roots
    (workspace / ".ea" / "config.yaml").write_text("runtime: [1, 2]\n", encoding="utf-8")
    with pytest.raises(ProviderConfigError) as caught:
        load_provider_configuration(
            workspace=workspace, repository=repository, registry=fx.registry()
        )
    assert caught.value.code is Rule.SCHEMA_INVALID


def test_load_provider_configuration_rejects_repository_widening_from_file(
    roots: tuple[Path, Path],
) -> None:
    workspace, repository = roots
    _write_layer(
        repository / ".ea" / "config.yaml",
        _repository(_patch(sandbox={"network": "brokered_allowlist"})),
    )
    with pytest.raises(ProviderConfigError) as caught:
        load_provider_configuration(
            workspace=workspace, repository=repository, registry=fx.registry()
        )
    assert caught.value.code is Rule.REPOSITORY_ADDS_NETWORK
