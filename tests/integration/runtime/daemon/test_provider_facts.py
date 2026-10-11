"""Native dispatch compiles against facts read off the installation, not hand-written.

The registry lists the shipped launchers, a runtime is bound only under its
latest current machine certification, and the built-in profiles and route let
a fresh tree compile a Task Run with no provider configuration written. Every
certification row here is a fixture appended to a temporary tree, and the
operator's home is a temporary directory, so no real binary or home is read.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from typer.testing import CliRunner

from eawf.kernel.config.defaults import BUILT_IN_DEFAULTS, BUILT_IN_PROVIDER_SOURCE
from eawf.kernel.config.providers import (
    ProviderConfigError,
    ProviderConfigRejection,
    parse_provider_configuration,
)
from eawf.kernel.config.registry import LEAF_KEY_REGISTRY
from eawf.kernel.identity.urn import parse_qualified_urn
from eawf.kernel.runtime.certification import (
    CapabilityCertification,
    CertificationFailureCode,
    ConformanceStageRecord,
)
from eawf.kernel.runtime.compiled import RunCompileRequest
from eawf.kernel.runtime.provider import AuthKind
from eawf.kernel.store.kinds.runtime_certification import MachineCertification
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.native_dispatch import compile_for_dispatch
from eawf.runtime.daemon.provider_facts import (
    InstalledProviderFacts,
    installed_provider_facts,
    shipped_launcher_manifests,
)
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app
from eawf.workflow.evidence.machine_certification import append_machine_certification
from eawf.workflow.runtime.compile import CompileRejection, RunCompileError, compile_run_spec
from eawf.workflow.runtime.task_request import compile_task_request
from tests import _provider_helpers as fx
from tests.unit.workflow.runtime.test_task_request import make_task

pytestmark = pytest.mark.integration

NOW: Final = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
CLAUDE_VERSION: Final = "2.1.288"
CODEX_VERSION: Final = "0.159.2"
NOT_OBSERVED: Final = CertificationFailureCode.CAPABILITY_NOT_OBSERVED

runner = CliRunner()


@pytest.fixture(autouse=True)
def private_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the global config layer at an empty temporary home."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """Return a bare ``.ea`` directory under its own repository root."""
    root = tmp_path / "repo" / ".ea"
    root.mkdir(parents=True)
    return root


def row(
    runtime_id: str = "claude-code",
    version: str = CLAUDE_VERSION,
    *,
    verified_at: datetime = NOW - timedelta(days=2),
    lifetime: timedelta = timedelta(days=90),
    outcome: str = "certified",
    capabilities: tuple[tuple[str, str], ...] = (
        ("tool_use", "verified"),
        ("streaming", "verified"),
        ("session_resume", "unsupported"),
    ),
) -> MachineCertification:
    """Return one machine certification row shaped as the certifier writes it."""
    evidence = f"artifact://runtime-certification/{runtime_id}/{version}"
    passed = outcome == "certified"
    probe = ConformanceStageRecord(
        stage="probe",
        outcome="passed" if passed else "failed",
        reason_code=None if passed else NOT_OBSERVED,
        evidence_ref=evidence,
        started_at=verified_at,
        completed_at=verified_at,
    )
    fields: dict[str, Any] = {
        "certification_urn": f"certification://{runtime_id}/{version}/{verified_at:%Y-%m-%d}",
        "runtime_id": runtime_id,
        "harness_version": version,
        "tuple_digest": fx.digest("d"),
        "outcome": outcome,
        "probe": probe,
        "verified_at": verified_at,
        "reason": f"{runtime_id} {version} probed",
    }
    if passed:
        fields["expires_at"] = verified_at + lifetime
        fields["capabilities"] = tuple(
            CapabilityCertification(
                capability_id=capability_id,
                level=status == "verified",
                status=status,  # type: ignore[arg-type]
                basis="native",
                evidence_ref=evidence,
                verified_at=verified_at,
                expires_at=verified_at + lifetime,
                failure_code=NOT_OBSERVED if status == "failed" else None,
            )
            for capability_id, status in capabilities
        )
    else:
        fields["reason_code"] = "capability_not_observed"
        fields["findings"] = ("tool_use: no --allowedTools in --help",)
    return MachineCertification.model_validate(fields)


def record(tree_root: Path, *rows: MachineCertification) -> None:
    """Append *rows* to the tree's machine-local certification store."""
    for item in rows:
        append_machine_certification(tree_root / "state.json", item)


def facts(tree_root: Path, now: datetime = NOW) -> InstalledProviderFacts:
    """Read the installed facts of the tree at *tree_root*."""
    return installed_provider_facts(tree_root, now=now)


def compile_for(provider: str, found: InstalledProviderFacts) -> Any:
    """Compile the fixture Task's dispatch request on *provider* from *found*."""
    return compile_task_request(
        make_task(),
        parse_qualified_urn(fx.RUN_URN),
        provider=provider,
        config=found.config,
        registry=found.registry,
        bindings=found.bindings,
        unbound=found.unbound,
        budget=None,
        base="a" * 40,
        actor="OP-0001",
        now=NOW,
    )


def write_repository_runtime(tree_root: Path, runtime: dict[str, Any]) -> None:
    """Replace the ``runtime`` section of the tree's own config file."""
    path = tree_root / "config.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    document["runtime"] = runtime
    path.write_text(yaml.safe_dump(document), encoding="utf-8")


# ---- bindings ---------------------------------------------------------------


def test_bindings_cite_the_latest_certified_row_by_urn_and_digest(tree: Path) -> None:
    older = row(verified_at=NOW - timedelta(days=30))
    latest = row(verified_at=NOW - timedelta(days=1))
    record(tree, older, latest)
    (binding,) = facts(tree).bindings
    body = json.dumps(
        latest.model_dump(mode="json"), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    assert binding.certification_ref == latest.certification_urn
    assert binding.certification_digest == f"sha256:{hashlib.sha256(body.encode()).hexdigest()}"
    assert binding.driver_ref == "driver://claude-agent-sdk/v1"
    assert binding.manifest.provider_id == "claude"


def test_bindings_one_per_certified_runtime_in_launcher_order(tree: Path) -> None:
    record(tree, row("codex", CODEX_VERSION), row())
    found = facts(tree)
    assert [b.manifest.provider_id for b in found.bindings] == ["claude", "codex"]
    assert found.registry.driver_refs == {b.driver_ref for b in found.bindings}
    assert found.registry.auth_profile_refs == {
        "auth://local-session/claude",
        "auth://local-session/codex",
    }


def test_bindings_map_each_certified_capability_from_the_row(tree: Path) -> None:
    certified = row(capabilities=(("tool_use", "verified"), ("session_resume", "unsupported")))
    record(tree, certified)
    (binding,) = facts(tree).bindings
    observed = {o.capability_id: o for o in binding.capabilities}
    assert set(observed) == {"tool_use", "session_resume"}
    assert observed["tool_use"].status == "verified"
    assert observed["tool_use"].basis_refs == (certified.certification_urn,)
    assert observed["tool_use"].expires_at == certified.expires_at
    assert observed["session_resume"].status == "unsupported"
    assert observed["session_resume"].reason_code == "provider_unsupported"


def test_bindings_read_a_failed_capability_as_unknown(tree: Path) -> None:
    record(tree, row(capabilities=(("tool_use", "verified"), ("streaming", "failed"))))
    (binding,) = facts(tree).bindings
    streaming = next(o for o in binding.capabilities if o.capability_id == "streaming")
    assert streaming.status == "unknown"
    assert streaming.reason_code == "observation_unknown"


def test_bindings_take_installation_facts_from_the_installed_sdk(tree: Path) -> None:
    record(tree, row())
    (binding,) = facts(tree).bindings
    distribution = binding.manifest.distribution
    assert distribution.package == "claude-agent-sdk"
    assert distribution.version == importlib.metadata.version("claude-agent-sdk")
    assert binding.manifest.manifest_digest != fx.digest("0")
    # the row records no auth kind or listing: the session kind and the shipped listing stand
    assert binding.auth_kind is AuthKind.LOCAL_SESSION
    (claude, _) = shipped_launcher_manifests()
    assert binding.listed_models == binding.certified_models == claude.listed_models


def test_bindings_expire_exactly_at_expires_at(tree: Path) -> None:
    certified = row(verified_at=NOW - timedelta(days=90), lifetime=timedelta(days=90))
    record(tree, certified)
    assert facts(tree).bindings == ()
    assert len(facts(tree, now=NOW - timedelta(microseconds=1)).bindings) == 1


def test_bindings_empty_store_binds_nothing(tree: Path) -> None:
    found = facts(tree)
    assert found.bindings == ()
    assert len(found.registry.driver_refs) == 2


def test_bindings_refuse_a_naive_clock(tree: Path) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        installed_provider_facts(tree, now=NOW.replace(tzinfo=None))


def test_bindings_skip_a_runtime_whose_sdk_is_not_installed(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def absent(name: str) -> importlib.metadata.Distribution:
        raise importlib.metadata.PackageNotFoundError(name)

    record(tree, row(), row("codex", CODEX_VERSION))
    monkeypatch.setattr(importlib.metadata, "distribution", absent)
    found = facts(tree)
    assert found.bindings == ()
    assert found.unbound["claude"].startswith(
        "runtime claude-code is unbound (driver_not_installed"
    )
    assert found.unbound["claude"].endswith("; install the claude-agent-sdk package")


def test_bindings_ignore_a_runtime_with_no_shipped_launcher(tree: Path) -> None:
    record(tree, row("opencode", "1.0.0"))
    assert facts(tree).bindings == ()


# ---- refusals ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("rows", "reason"),
    [
        pytest.param((), "never_certified", id="never-certified"),
        pytest.param((row(outcome="quarantined"),), "quarantined", id="quarantined"),
        pytest.param(
            (row(), row(outcome="quarantined", verified_at=NOW)),
            "quarantined",
            id="later-quarantine",
        ),
        pytest.param((row(verified_at=NOW - timedelta(days=91)),), "expired", id="expired"),
    ],
)
def test_refuses_a_runtime_without_a_current_certification(
    tree: Path, rows: tuple[MachineCertification, ...], reason: str
) -> None:
    record(tree, *rows, row("codex", CODEX_VERSION))
    found = facts(tree)
    assert [b.manifest.provider_id for b in found.bindings] == ["codex"]
    with pytest.raises(DaemonValidationError) as caught:
        compile_for("claude", found)
    message = str(caught.value)
    assert CompileRejection.NO_ELIGIBLE_PROFILE.value in message
    assert f"runtime claude-code is unbound ({reason}:" in message
    assert message.endswith("; run `eawf runtime certify claude-code`")


def test_a_bound_provider_has_no_unbound_cause(tree: Path) -> None:
    record(tree, row("codex", CODEX_VERSION))
    found = facts(tree)
    assert "codex" not in found.unbound
    assert set(found.unbound) == {"claude"}


def test_an_empty_store_names_every_shipped_provider_unbound(tree: Path) -> None:
    assert set(facts(tree).unbound) == {"claude", "codex"}


def test_refuses_at_the_compiler_when_no_profile_has_a_binding(tree: Path) -> None:
    found = facts(tree)
    configuration = parse_provider_configuration(
        {layer: document["runtime"] for layer, document in found.config.items()},  # type: ignore[misc]
        registry=found.registry,
    )
    with pytest.raises(RunCompileError) as caught:
        compile_run_spec(
            RunCompileRequest.model_validate(
                {
                    "run_ref": fx.RUN_URN,
                    "run_scope": {
                        "scope_kind": "task",
                        "purpose": "implement",
                        "task_ref": fx.TASK_URN,
                        "write_set": ["src"],
                    },
                    "purpose": "implement",
                    "agent_role": "executor",
                }
            ),
            configuration=configuration,
            bindings=(),
            compiled_at=NOW,
        )
    assert caught.value.code is CompileRejection.NO_ELIGIBLE_PROFILE


def test_refuses_an_unreadable_repository_layer(tree: Path) -> None:
    (tree / "config.yaml").write_text("runtime: [unclosed", encoding="utf-8")
    with pytest.raises(ProviderConfigError) as caught:
        facts(tree)
    assert caught.value.code is ProviderConfigRejection.LAYER_UNREADABLE


# ---- a fresh tree -----------------------------------------------------------


@pytest.fixture
def fresh_tree(tmp_path: Path) -> Path:
    """Return the ``.ea`` directory ``eawf init --quick`` creates, certified for Claude."""
    root = tmp_path / "fresh"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    result = runner.invoke(app, ["init", "--quick", "--target", str(root)])
    assert result.exit_code == exit_codes.OK, result.output
    record(root / ".ea", row())
    return root / ".ea"


def test_fresh_tree_compiles_a_task_run_with_no_written_profile(fresh_tree: Path) -> None:
    written = yaml.safe_load((fresh_tree / "config.yaml").read_text(encoding="utf-8"))
    assert not {"profiles", "routes"} & set(written.get("runtime", {}))
    found = facts(fresh_tree)
    params = compile_for("claude", found)
    spec = compile_for_dispatch(params, now=NOW)
    (binding,) = found.bindings
    assert spec.profile_ref == "profile://claude-default"
    assert spec.route_policy_ref == "route://task"
    assert spec.certification_ref == binding.certification_ref
    assert spec.certification_digest == binding.certification_digest
    assert spec.model_id == "claude-sonnet-4-6"
    assert spec.sandbox.mode == "workspace_write"
    assert {entry.source_ref for entry in spec.source_map} >= {BUILT_IN_PROVIDER_SOURCE}


def test_fresh_tree_catalogues_profiles_and_routes_for_the_operator_layers() -> None:
    profiles = LEAF_KEY_REGISTRY["runtime.profiles"]
    routes = LEAF_KEY_REGISTRY["runtime.routes"]
    assert profiles.writable_layers == ("global", "workspace", "repo")
    assert routes.writable_layers == ("global", "workspace")
    assert profiles.default == BUILT_IN_DEFAULTS["runtime"]["profiles"]
    assert routes.default == BUILT_IN_DEFAULTS["runtime"]["routes"]
    assert profiles.consumer == routes.consumer
    assert profiles.consumer is not None
    assert profiles.consumer.endswith("provider_facts.installed_provider_facts")


def test_fresh_tree_built_in_profiles_ask_only_for_listed_models() -> None:
    listings = {m.driver_ref: set(m.listed_models) for m in shipped_launcher_manifests()}
    for profile in BUILT_IN_DEFAULTS["runtime"]["profiles"]:
        assert set(profile["model_policy"]["allowed"]) <= listings[profile["driver_ref"]]


def test_fresh_tree_repository_patch_narrows_the_built_in_profile(fresh_tree: Path) -> None:
    patch = {
        "profile_id": "claude-default",
        "source_ref": "config://repository/runtime",
        "limits": {"wall_seconds": 900},
    }
    write_repository_runtime(fresh_tree, {"preference": ["claude-code"], "profiles": [patch]})
    spec = compile_for_dispatch(compile_for("claude", facts(fresh_tree)), now=NOW)
    assert spec.limits.wall_seconds == 900


@pytest.mark.parametrize(
    ("runtime", "code"),
    [
        pytest.param(
            {"profiles": [{"profile_id": "mine", "source_ref": "config://repository/runtime"}]},
            ProviderConfigRejection.REPOSITORY_ADDS_PROFILE,
            id="adds-profile",
        ),
        pytest.param(
            {
                "profiles": [
                    {
                        "profile_id": "claude-default",
                        "source_ref": "config://repository/runtime",
                        "limits": {"wall_seconds": 7200},
                    }
                ]
            },
            ProviderConfigRejection.REPOSITORY_WIDENS_AUTHORITY,
            id="widens-limit",
        ),
        pytest.param(
            {"routes": [BUILT_IN_DEFAULTS["runtime"]["routes"][0]]},
            ProviderConfigRejection.REPOSITORY_ADDS_ROUTE,
            id="adds-route",
        ),
        pytest.param(
            {"profiles": {"claude-default": {}}},
            ProviderConfigRejection.SCHEMA_INVALID,
            id="not-a-list",
        ),
    ],
)
def test_fresh_tree_repository_layer_cannot_widen(
    fresh_tree: Path, runtime: dict[str, Any], code: ProviderConfigRejection
) -> None:
    write_repository_runtime(fresh_tree, runtime)
    with pytest.raises(DaemonValidationError, match=code.value):
        compile_for_dispatch(compile_for("claude", facts(fresh_tree)), now=NOW)


def test_fresh_tree_global_record_replaces_the_built_in_one(
    fresh_tree: Path, private_home: Path
) -> None:
    built_in = BUILT_IN_DEFAULTS["runtime"]["profiles"][0]
    mine = {**built_in, "source_ref": "config://global/runtime", "revision": 2}
    mine["limits"] = {"wall_seconds": 1200, "output_bytes": 1_048_576}
    config = private_home / ".config" / "eawf" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(yaml.safe_dump({"runtime": {"profiles": [mine]}}), encoding="utf-8")
    found = facts(fresh_tree)
    profiles = found.config["global"]["runtime"]["profiles"]
    assert [p["profile_id"] for p in profiles] == ["codex-default", "claude-default"]
    spec = compile_for_dispatch(compile_for("claude", found), now=NOW)
    assert spec.profile_revision == 2
    assert spec.limits.wall_seconds == 1200
