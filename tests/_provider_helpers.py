"""Shared fixtures for provider records, configuration, and Run compilation.

The documents mirror the canonical provider configuration: one Codex-shaped
profile, a mutating task route, and a read-only review route. Every builder
returns a fresh value, so a test that edits one cannot leak into another.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

from eawf.kernel.config.providers import (
    ProviderConfiguration,
    ProviderRegistry,
    parse_provider_configuration,
)
from eawf.kernel.runtime.compiled import CapabilityObservation, RunCompileRequest, RuntimeBinding

COMPILED_AT = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
DRIVER_REF = "driver://codex-app-server/v1"
OTHER_DRIVER_REF = "driver://claude-sdk/v1"
AUTH_REF = "auth://operator/codex-default"
OTHER_AUTH_REF = "auth://operator/claude-default"
CERTIFICATION_REF = "certification://codex-app-server/2026-09"
WORKSPACE_SOURCE = "config://workspace/runtime"
GLOBAL_SOURCE = "config://global/runtime"
REPOSITORY_SOURCE = "config://repository/runtime"
RUN_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000010"
TASK_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0042"
BATCH_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/batch/BAT-0007"
REQUIRED = ("semantic_tools", "event_replay", "interrupt_ack", "managed_sandbox")
OPTIONAL = ("usage_receipts", "context_compaction")
CERTIFIED_MODEL = "gpt-5.6-sol"
UNCERTIFIED_MODEL = "gpt-5.6-mini"
UNLISTED_MODEL = "gpt-5.6-retired"


def digest(character: str) -> str:
    """Return a well-formed digest made of one repeated hex *character*."""
    return f"sha256:{character * 64}"


def profile_document(**overrides: Any) -> dict[str, Any]:
    """Return a valid operator profile document with *overrides* applied."""
    document: dict[str, Any] = {
        "profile_id": "fixture_codex",
        "driver_ref": DRIVER_REF,
        "auth_profile_ref": AUTH_REF,
        "revision": 1,
        "source_ref": WORKSPACE_SOURCE,
        "model_policy": {
            "allowed": [CERTIFIED_MODEL, UNCERTIFIED_MODEL],
            "default": CERTIFIED_MODEL,
        },
        "required_capabilities": list(REQUIRED),
        "optional_capabilities": list(OPTIONAL),
        "tool_policy": {
            "allow": ["repo_read", "repo_search", "workspace_apply_patch", "submit_report"],
            "deny": [],
        },
        "limits": {"wall_seconds": 2400, "output_bytes": 1_048_576, "child_runs": 2},
        "sandbox": {
            "mode": "workspace_write",
            "network": "deny",
            "filesystem_policy_ref": "policy://filesystem/task-workspace",
        },
        "environment": {
            "allowed_variable_names": ["LANG", "TZ"],
            "secret_broker_ref": "secrets://operator/broker",  # pragma: allowlist secret
        },
        "authority_ceiling": {
            "state": "proposal_only",
            "workspace": "scoped_write",
            "git": "read_metadata",
            "external_effects": "none",
            "credentials": "reference_only",
        },
        "provider_options": {
            "provider_kind": "codex",
            "approval_mode": "broker_only",
            "reasoning_summary": "auto",
        },
    }
    document.update(copy.deepcopy(overrides))
    return document


def task_route_document(**overrides: Any) -> dict[str, Any]:
    """Return the mutating task route with *overrides* applied."""
    document: dict[str, Any] = {
        "route_id": "mutating-task",
        "revision": 3,
        "source_ref": WORKSPACE_SOURCE,
        "match": {"scope_kind": "task", "purpose": ["implement", "repair"]},
        "allowed_profiles": ["fixture_codex"],
        "required_capabilities": ["semantic_tools", "event_replay", "managed_sandbox"],
        "fallback_causes": ["pre_acceptance_transport_failure"],
    }
    document.update(copy.deepcopy(overrides))
    return document


def review_route_document(**overrides: Any) -> dict[str, Any]:
    """Return the read-only review route, declared globally."""
    document: dict[str, Any] = {
        "route_id": "review",
        "revision": 1,
        "source_ref": GLOBAL_SOURCE,
        "match": {"purpose": ["review", "audit"]},
        "allowed_profiles": ["fixture_codex"],
    }
    document.update(copy.deepcopy(overrides))
    return document


def documents(
    *,
    profiles: list[dict[str, Any]] | None = None,
    routes: list[dict[str, Any]] | None = None,
    global_routes: list[dict[str, Any]] | None = None,
    repository: dict[str, Any] | None = None,
) -> dict[Any, Any]:
    """Return the three provider layer documents."""
    layers: dict[Any, Any] = {
        "global": {
            "routes": global_routes if global_routes is not None else [review_route_document()]
        },
        "workspace": {
            "profiles": profiles if profiles is not None else [profile_document()],
            "routes": routes if routes is not None else [task_route_document()],
        },
    }
    if repository is not None:
        layers["repository"] = repository
    return layers


def registry() -> ProviderRegistry:
    """Return the installed facts the fixture configuration relies on."""
    return ProviderRegistry(
        driver_refs=frozenset({DRIVER_REF, OTHER_DRIVER_REF}),
        auth_profile_refs=frozenset({AUTH_REF, OTHER_AUTH_REF}),
        certification_capabilities=frozenset({*REQUIRED, *OPTIONAL}),
    )


def configuration(**kwargs: Any) -> ProviderConfiguration:
    """Parse :func:`documents` built from *kwargs* against :func:`registry`."""
    return parse_provider_configuration(documents(**kwargs), registry=registry())


def manifest_document(**overrides: Any) -> dict[str, Any]:
    """Return a valid installed driver manifest document."""
    document: dict[str, Any] = {
        "schema_version": "driver-manifest/v1",
        "manifest_id": "codex-app-server",
        "provider_id": "codex",
        "driver_kind": "app_server",
        "distribution": {
            "kind": "npm",
            "package": "@openai/codex",
            "version": "0.44.0",
            "artifact_digest": digest("b"),
            "entrypoint_ref": "component://codex/app-server",
            "lockfile_ref": "artifact://lock/codex-0.44.0",
        },
        "manifest_digest": digest("a"),
        "worker_protocol_versions": ["2.1.0", "2.0.0", "1.9.0-rc1"],
        "semantic_protocol_versions": ["1.0.0"],
        "event_codec": {
            "codec_id": "codex-events",
            "schema_version": "1.0.0",
            "schema_ref": "schema://codex/events/v1",
            "supports_replay": True,
            "provider_sequence_kind": "integer",
        },
        "auth_kinds": ["subscription", "api_key"],
        "control_schema_ref": "schema://codex/control/v1",
        "provider_options_schema_ref": "schema://codex/options/v1",
        "capabilities": [
            {
                "capability_id": capability_id,
                "level_kind": "boolean",
                "allowed_levels": [True, False],
                "default_level": True,
            }
            for capability_id in (*REQUIRED, "usage_receipts")
        ],
        "installation_ref": "installation://codex/2026-09-01",
        "installed_at": "2026-09-01T00:00:00Z",
    }
    document.update(copy.deepcopy(overrides))
    return document


def observation(capability_id: str, **overrides: Any) -> CapabilityObservation:
    """Return a verified, unexpired, natively observed capability."""
    document: dict[str, Any] = {
        "capability_id": capability_id,
        "effective_level": True,
        "status": "verified",
        "basis": "native",
        "basis_refs": [CERTIFICATION_REF],
        "certification_evidence_ref": f"artifact://evidence/{capability_id}",
        "verified_at": "2026-09-01T00:00:00Z",
        "expires_at": "2026-12-01T00:00:00Z",
    }
    document.update(overrides)
    return CapabilityObservation.model_validate(document)


def binding(**overrides: Any) -> RuntimeBinding:
    """Return a certified binding for the fixture driver."""
    document: dict[str, Any] = {
        "driver_ref": DRIVER_REF,
        "manifest": manifest_document(),
        "certification_ref": CERTIFICATION_REF,
        "certification_digest": digest("c"),
        "auth_kind": "subscription",
        "listed_models": [CERTIFIED_MODEL, UNCERTIFIED_MODEL],
        "certified_models": [CERTIFIED_MODEL],
        "capabilities": [
            observation(capability_id).model_dump(mode="json")
            for capability_id in (*REQUIRED, "usage_receipts")
        ],
    }
    document.update(overrides)
    return RuntimeBinding.model_validate(document)


def task_request(**overrides: Any) -> RunCompileRequest:
    """Return an unattended executor request to implement one Task."""
    document: dict[str, Any] = {
        "run_ref": RUN_URN,
        "run_scope": {
            "scope_kind": "task",
            "purpose": "implement",
            "task_ref": TASK_URN,
            "write_set": ["src/eawf/kernel/runtime/provider.py"],
        },
        "purpose": "implement",
        "agent_role": "executor",
    }
    document.update(overrides)
    return RunCompileRequest.model_validate(document)


def review_request(**overrides: Any) -> RunCompileRequest:
    """Return an unattended reviewer request against one Batch."""
    document: dict[str, Any] = {
        "run_ref": RUN_URN,
        "run_scope": {"scope_kind": "batch", "purpose": "review", "batch_ref": BATCH_URN},
        "purpose": "review",
        "agent_role": "reviewer",
    }
    document.update(overrides)
    return RunCompileRequest.model_validate(document)
