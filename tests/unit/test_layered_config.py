"""Unit tests for the C08 layered-config extension.

Covers the wave's success criteria 1, 2, and 3:

* Six durable layers + two runtime overlays + transient wave layer:
  layer order canonical; precedence end-to-end (built-in < global <
  workspace < repo < branch < local < wave < env < cli).
* Branch layer at ``.ea/branches/<branch>.yaml``; subdirectory form
  for slash-bearing branch names.
* Field-catalog lookup (LEAF_KEY_REGISTRY) by dotted path; unknown
  keys raise ``unknown config key: <key!r>``.
"""

from __future__ import annotations

import pydoc
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.kernel.config import layered
from eawf.kernel.config.layered import (
    LAYER_ORDER,
    Layer,
    branch_config_path,
    merge_config,
    resolve_runtime_tier_models,
    unset_dotted,
)
from eawf.kernel.config.registry import (
    LEAF_KEY_REGISTRY,
    LeafKey,
    is_known_leaf_key,
    leaf_key_lookup,
    leaf_keys_by_domain,
)
from eawf.kernel.config.registry.leaf_catalog import DEPRECATED_LEAF_KEYS


@pytest.fixture(autouse=True)
def _isolate_global(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Redirect global-config lookups into a per-test tmp dir."""
    fake_global = tmp_path / "fake-global.yaml"
    monkeypatch.setattr(layered, "global_config_path", lambda: fake_global)
    yield


def _write_yaml(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


# --- Layer order + enum -----------------------------------------------------


def test_layer_order_canonical_nine_layers() -> None:
    """Success criterion 1: six durable + two runtime + one transient."""
    assert LAYER_ORDER == (
        "built-in",
        "global",
        "workspace",
        "repo",
        "branch",
        "local",
        "wave",
        "env",
        "cli",
    )


def test_layer_enum_members_match_layer_order() -> None:
    """Every :class:`Layer` member appears in :data:`LAYER_ORDER`."""
    assert tuple(m.value for m in Layer) == LAYER_ORDER


def test_layer_enum_compares_equal_to_string() -> None:
    """The ``str`` mixin keeps existing string-typed call sites working."""
    assert Layer.REPO == "repo"
    assert Layer.BRANCH.value == "branch"


# --- Branch layer path ------------------------------------------------------


def test_branch_config_path_plain_name() -> None:
    repo = Path("/tmp/myrepo")
    got = branch_config_path(repo, "main")
    assert got == repo / ".ea" / "branches" / "main.yaml"


def test_branch_config_path_subdirectory_form() -> None:
    """Success criterion 2: branch names with ``/`` map to subdirs."""
    repo = Path("/tmp/myrepo")
    got = branch_config_path(repo, "feature/eawf-v0.3-p25-w14")
    assert got == (repo / ".ea" / "branches" / "feature" / "eawf-v0.3-p25-w14.yaml")


def test_branch_config_path_rejects_empty_branch() -> None:
    repo = Path("/tmp/myrepo")
    with pytest.raises(ValueError, match="branch name must be non-empty"):
        branch_config_path(repo, "")


def test_branch_config_path_rejects_slash_only_branch() -> None:
    repo = Path("/tmp/myrepo")
    with pytest.raises(ValueError, match="branch name must be non-empty"):
        branch_config_path(repo, "/")


# --- Branch layer in merge_config -------------------------------------------


def test_branch_layer_overrides_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_yaml(repo / ".ea" / "config.yaml", "research:\n  default_depth: repo_val\n")
    _write_yaml(
        repo / ".ea" / "branches" / "main.yaml",
        "research:\n  default_depth: branch_val\n",
    )
    merged, sources = merge_config(
        workspace=None,
        repo=repo,
        env={},
        cli_overrides={},
        branch="main",
    )
    assert merged["research"]["default_depth"] == "branch_val"
    assert sources["research.default_depth"] == "branch"


def test_branch_layer_subdir_form_loaded(tmp_path: Path) -> None:
    """Branch names containing ``/`` resolve to nested files."""
    repo = tmp_path / "repo"
    _write_yaml(repo / ".ea" / "config.yaml", "research:\n  default_depth: repo_val\n")
    _write_yaml(
        repo / ".ea" / "branches" / "feature" / "x.yaml",
        "research:\n  default_depth: feature_x\n",
    )
    merged, sources = merge_config(
        workspace=None,
        repo=repo,
        env={},
        cli_overrides={},
        branch="feature/x",
    )
    assert merged["research"]["default_depth"] == "feature_x"
    assert sources["research.default_depth"] == "branch"


def test_branch_layer_missing_file_silently_skipped(tmp_path: Path) -> None:
    """Branch file absent → loader skips it, lower layer wins."""
    repo = tmp_path / "repo"
    _write_yaml(repo / ".ea" / "config.yaml", "research:\n  default_depth: repo_val\n")
    merged, sources = merge_config(
        workspace=None,
        repo=repo,
        env={},
        cli_overrides={},
        branch="some-branch-with-no-file",
    )
    assert merged["research"]["default_depth"] == "repo_val"
    assert sources["research.default_depth"] == "repo"


def test_branch_layer_loses_to_local(tmp_path: Path) -> None:
    """Local layer is higher precedence than branch."""
    repo = tmp_path / "repo"
    _write_yaml(repo / ".ea" / "config.yaml", "research:\n  default_depth: r\n")
    _write_yaml(repo / ".ea" / "branches" / "main.yaml", "research:\n  default_depth: b\n")
    _write_yaml(repo / ".ea" / "local" / "config.yaml", "research:\n  default_depth: l\n")
    merged, sources = merge_config(
        workspace=None,
        repo=repo,
        env={},
        cli_overrides={},
        branch="main",
    )
    assert merged["research"]["default_depth"] == "l"
    assert sources["research.default_depth"] == "local"


# --- Wave overlay -----------------------------------------------------------


def test_wave_overlay_overrides_local(tmp_path: Path) -> None:
    """Wave layer sits above local; daemon RAM wins."""
    repo = tmp_path / "repo"
    _write_yaml(repo / ".ea" / "local" / "config.yaml", "research:\n  default_depth: local\n")
    merged, sources = merge_config(
        workspace=None,
        repo=repo,
        env={},
        cli_overrides={},
        wave_overlay={"research": {"default_depth": "wave_val"}},
    )
    assert merged["research"]["default_depth"] == "wave_val"
    assert sources["research.default_depth"] == "wave"


def test_wave_overlay_loses_to_env() -> None:
    """Env is higher precedence than the wave RAM layer."""
    merged, sources = merge_config(
        workspace=None,
        repo=None,
        env={"EAWF_RESEARCH__DEFAULT_DEPTH": "env_val"},
        cli_overrides={},
        wave_overlay={"research": {"default_depth": "wave_val"}},
    )
    assert merged["research"]["default_depth"] == "env_val"
    assert sources["research.default_depth"] == "env"


def test_wave_overlay_loses_to_cli() -> None:
    merged, sources = merge_config(
        workspace=None,
        repo=None,
        env={},
        cli_overrides={"research": {"default_depth": "cli_val"}},
        wave_overlay={"research": {"default_depth": "wave_val"}},
    )
    assert merged["research"]["default_depth"] == "cli_val"
    assert sources["research.default_depth"] == "cli"


def test_empty_wave_overlay_noop(tmp_path: Path) -> None:
    """Falsy / empty wave_overlay is a no-op (no source map entry)."""
    repo = tmp_path / "repo"
    _write_yaml(repo / ".ea" / "config.yaml", "research:\n  default_depth: r\n")
    merged, sources = merge_config(
        workspace=None,
        repo=repo,
        env={},
        cli_overrides={},
        wave_overlay={},
    )
    assert merged["research"]["default_depth"] == "r"
    assert sources["research.default_depth"] == "repo"


@pytest.mark.parametrize(
    "layer",
    ["global", "workspace", "repo", "branch", "local", "wave"],
)
def test_legacy_flow_transition_is_stripped_in_memory_per_layer(
    layer: str,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    repo = tmp_path / "repo"
    branch = "main"
    body = "flow:\n  auto_accept:\n    audit: true\n"
    paths = {
        "global": tmp_path / "fake-global.yaml",
        "workspace": workspace / ".ea" / "config.yaml",
        "repo": repo / ".ea" / "config.yaml",
        "branch": repo / ".ea" / "branches" / f"{branch}.yaml",
        "local": repo / ".ea" / "local" / "config.yaml",
    }
    source_path = paths.get(layer)
    if source_path is not None:
        _write_yaml(source_path, body)

    merged, sources = merge_config(
        workspace=workspace if layer == "workspace" else None,
        repo=repo if layer in {"repo", "branch", "local"} else None,
        env={},
        cli_overrides={},
        branch=branch,
        wave_overlay=({"flow": {"auto_accept": {"audit": True}}} if layer == "wave" else None),
    )

    assert "advance_after" not in merged["flow"]
    assert "auto_accept" not in merged["flow"]
    assert "flow.advance_after.audit" not in sources
    if source_path is not None:
        assert source_path.read_text(encoding="utf-8") == body


# --- Full nine-layer ordering ------------------------------------------------


def test_full_stack_ordering_with_branch_and_wave(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All nine layers active; CLI wins, branch/wave correctly placed."""
    fake_global = tmp_path / "g.yaml"
    monkeypatch.setattr(layered, "global_config_path", lambda: fake_global)
    _write_yaml(fake_global, "research:\n  default_depth: g\n")
    workspace = tmp_path / "ws"
    _write_yaml(workspace / ".ea" / "config.yaml", "research:\n  default_depth: w\n")
    repo = tmp_path / "repo"
    _write_yaml(repo / ".ea" / "config.yaml", "research:\n  default_depth: r\n")
    _write_yaml(repo / ".ea" / "branches" / "main.yaml", "research:\n  default_depth: b\n")
    _write_yaml(repo / ".ea" / "local" / "config.yaml", "research:\n  default_depth: l\n")
    merged, sources = merge_config(
        workspace=workspace,
        repo=repo,
        env={"EAWF_RESEARCH__DEFAULT_DEPTH": "e"},
        cli_overrides={"research": {"default_depth": "c"}},
        branch="main",
        wave_overlay={"research": {"default_depth": "wv"}},
    )
    assert merged["research"]["default_depth"] == "c"
    assert sources["research.default_depth"] == "cli"


# --- Layer-path helper ------------------------------------------------------


def test_layer_path_branch_returns_branch_file() -> None:
    repo = Path("/tmp/repo")
    got = layered.layer_path("branch", workspace=None, repo=repo, branch="main")
    assert got == repo / ".ea" / "branches" / "main.yaml"


def test_layer_path_branch_requires_repo() -> None:
    with pytest.raises(ValueError, match="repo path required"):
        layered.layer_path("branch", workspace=None, repo=None, branch="main")


def test_layer_path_branch_requires_branch_name() -> None:
    with pytest.raises(ValueError, match="branch name required"):
        layered.layer_path("branch", workspace=None, repo=Path("/tmp/r"), branch=None)


# --- detect_current_branch --------------------------------------------------


def test_detect_current_branch_none_for_missing_repo() -> None:
    assert layered.detect_current_branch(None) is None


def test_detect_current_branch_none_for_non_git_dir(tmp_path: Path) -> None:
    """A non-git directory yields ``None`` (silent skip semantics)."""
    not_a_repo = tmp_path / "no-git"
    not_a_repo.mkdir()
    assert layered.detect_current_branch(not_a_repo) is None


# --- LEAF_KEY_REGISTRY ------------------------------------------------------


def test_leaf_key_registry_holds_no_retired_leaf() -> None:
    """A leaf no code reads is retired: absent from the catalog, named for migration."""
    retired = DEPRECATED_LEAF_KEYS - set(LEAF_KEY_REGISTRY)
    assert {
        "config.layers_visible",
        "project.success_metrics",
        "workspace.repos",
        "runtime.fallback.retry_policy",
        "dispatch.session_handle_ttl_seconds",
        "dispatch.routing",
        "mcp.servers",
        "hooks.policy",
        "prose.level",
        "ui.bare_command",
        "estimation.display.eu_quantum",
    } <= retired
    assert not retired & set(LEAF_KEY_REGISTRY)


def test_leaf_key_registry_includes_canonical_c08_keys() -> None:
    """The consumed C08 keys and the policy tables catalogued since are present."""
    must_have = {
        "profiles.trusted",
        "runtime.preference",
        "telemetry.enabled",
        "telemetry.db_kind",
        "vcs.conventions.release.cadence",
        "daemon.idle_timeout_seconds",
        "daemon.session_handle_ttl_seconds",
        "economics.prompt_budget",
        "economics.governor",
        "economics.notice_policy",
        "tui.eu_view.density",
        "tui.eu_view.fields",
    }
    missing = must_have - set(LEAF_KEY_REGISTRY)
    assert not missing, f"missing canonical C08 leaf keys: {sorted(missing)}"
    assert "project.default_subproject" not in LEAF_KEY_REGISTRY


def test_behavioral_config_leaves_have_exactly_one_binding() -> None:
    """Every catalogued leaf is consumed or explicitly deprecated, never both."""
    for key, entry in LEAF_KEY_REGISTRY.items():
        assert (entry.consumer is not None) ^ entry.reserved, key


_REPO_ROOT = Path(__file__).resolve().parents[2]
_COAUTHOR = "eawf.runtime.vcs.coauthor.resolve_coauthor_trailer"
_LAYERED = "eawf.kernel.config.layered"
_ECONOMICS = "eawf.kernel.economics.governor.economics_policy_from"
_EU_VIEW = "eawf.surfaces.render.plan_view._eu_view_config"

#: The callable that reads each catalogued leaf's resolved value; the census every
#: leaf must appear in, so a leaf with no production reader fails here.
_EXPECTED_CONSUMERS: dict[str, str] = {
    "schema_version": "eawf.kernel.config.migration.migrate_config_payload",
    "agents.extra_tools": f"{_LAYERED}.resolve_agent_extra_tools",
    "daemon.idle_timeout_seconds": "eawf.runtime.daemon.main._resolve_idle_timeout",
    "daemon.proxy_enabled": "eawf.surfaces.cli._mutation._proxy_enabled",
    "daemon.session_handle_ttl_seconds": "eawf.runtime.daemon.main._resolve_session_ttl_seconds",
    "dispatch.role_tier_token_cap": "eawf.workflow.dispatch.renderer.resolve_role_blocks",
    "economics.governor": _ECONOMICS,
    "economics.notice_policy": _ECONOMICS,
    "economics.prompt_budget": _ECONOMICS,
    "estimation.eu_basis": "eawf.runtime.daemon.methods.state._wave_close_rollup_config",
    "estimation.eu_minutes": "eawf.runtime.daemon.methods.state._wave_close_rollup_config",
    "flow.budget.enforce": "eawf.runtime.daemon.methods.agent._resolve_budget_config",
    "flow.budget.multiplier": "eawf.runtime.daemon.methods.agent._resolve_budget_config",
    "planning.max_parallel_waves": "eawf.workflow.lifecycle._capacity.resolve_max_parallel_waves",
    "preferences.auto_choose": (
        "eawf.runtime.daemon.methods.question_decision.resolved_preferences"
    ),
    "profiles.certified": "eawf.workflow.dispatch.renderer.resolve_role_blocks",
    "profiles.enabled": "eawf.platform.profiles.selection.resolve_enabled_profiles",
    "profiles.trusted": "eawf.platform.profiles.trust.load_trust_ledger",
    "research.agent_count": "eawf.workflow.skills.research.ResearchSkill._resolve_agents",
    "research.default_depth": "eawf.workflow.skills.research.ResearchSkill._resolve_depth",
    "runtime.adapters": f"{_LAYERED}.resolve_dispatch_provider_tuple",
    "runtime.auto_certify": f"{_LAYERED}.resolve_auto_certify",
    "runtime.claude.permission_wait_s": f"{_LAYERED}.resolve_permission_wait_seconds",
    "runtime.claude.stall_interval_s": f"{_LAYERED}.resolve_stall_interval_seconds",
    "runtime.codex.stall_interval_s": f"{_LAYERED}.resolve_stall_interval_seconds",
    "runtime.models.claude": f"{_LAYERED}.resolve_runtime_tier_models",
    "runtime.models.codex": f"{_LAYERED}.resolve_runtime_tier_models",
    "runtime.models.opencode": f"{_LAYERED}.resolve_runtime_tier_models",
    "runtime.opencode.stall_interval_s": f"{_LAYERED}.resolve_stall_interval_seconds",
    "runtime.preference": f"{_LAYERED}.resolve_dispatch_provider_tuple",
    "telemetry.db_kind": "eawf.surfaces.cli.commands.metrics._read_telemetry_config",
    "telemetry.enabled": "eawf.surfaces.cli.commands.metrics._read_telemetry_config",
    "tui.eu_view.density": _EU_VIEW,
    "tui.eu_view.fields": _EU_VIEW,
    "ui.glyphs": "eawf.surfaces.tui.launch.persisted_glyphs",
    "ui.theme": "eawf.surfaces.tui.chassis.theme.persisted_theme",
    "ui.toasts": "eawf.surfaces.tui.launch.persisted_toast_verbosity",
    "vcs.checkpoint_requires_commit": "eawf.runtime.vcs.checkpoint.resolve_checkpoint_cadence",
    "vcs.coauthor.default_runtime": _COAUTHOR,
    "vcs.coauthor.mode": _COAUTHOR,
    "vcs.coauthor.project.email": _COAUTHOR,
    "vcs.coauthor.project.name": _COAUTHOR,
    "vcs.coauthor.require_trailer": _COAUTHOR,
    "vcs.coauthor.trailers.claude.email": _COAUTHOR,
    "vcs.coauthor.trailers.claude.name": _COAUTHOR,
    "vcs.coauthor.trailers.codex.email": _COAUTHOR,
    "vcs.coauthor.trailers.codex.name": _COAUTHOR,
    "vcs.conventions.release.cadence": "eawf.runtime.vcs.coauthor.requires_phase_release_preflight",
    "vcs.conventions.subject_style": "tools.commit_prefix_lint._configured_subject_style",
    "vcs.integration_commit_unit": "eawf.runtime.daemon.methods.delivery.integrate_delivery",
    "vcs.task_reference": "eawf.runtime.daemon.methods.delivery.integrate_delivery",
    "verify.juror_wall_clock_seconds": "eawf.workflow.verify.readiness._overlay_repo_verify_leaves",
    "verify.jury_max_brier": "eawf.runtime.daemon.verdict_observations.resolve_jury_thresholds",
    "verify.jury_max_co_error": (
        "eawf.runtime.daemon.verdict_observations.resolve_jury_thresholds"
    ),
    "verify.odr_blocking": "eawf.workflow.verify.readiness._overlay_repo_verify_leaves",
    "verify.require_iter_audit_accepted": "eawf.workflow.lifecycle.iter_.close_iter",
    "verify.retyped_rule_threshold": (
        "eawf.observability.reflect.retyped.resolve_retyped_rule_threshold"
    ),
    "verify.waiver_mode": "eawf.workflow.verify.readiness._overlay_repo_verify_leaves",
}


def test_config_consumer_set_is_exact_and_importable() -> None:
    """Every live leaf names the production callable that reads it, and that callable exists.

    The merger is never a consumer: naming ``merge_config`` would claim a reader for a
    leaf nothing reads. A repository tool runs as a script beside its sibling modules, so
    its consumer is found by its definition rather than imported.
    """
    actual = {
        key: entry.consumer
        for key, entry in LEAF_KEY_REGISTRY.items()
        if entry.consumer is not None
    }

    assert actual == _EXPECTED_CONSUMERS
    assert "eawf.kernel.config.layered.merge_config" not in actual.values()
    for key, consumer in actual.items():
        if consumer.startswith("tools."):
            module, _, name = consumer.rpartition(".")
            source = _REPO_ROOT.joinpath(*module.split(".")).with_suffix(".py")
            assert f"def {name}(" in source.read_text(encoding="utf-8"), (key, consumer)
            continue
        resolved = pydoc.locate(consumer)
        assert resolved is not None, (key, consumer)
        assert callable(resolved), (key, consumer, resolved)


def test_every_config_leaf_has_exact_consumer_classification() -> None:
    kinds = {entry.consumer_kind for entry in LEAF_KEY_REGISTRY.values()}
    assert kinds == {"engine", "skill", "deprecated"}
    assert {
        key for key, entry in LEAF_KEY_REGISTRY.items() if entry.consumer_kind == "deprecated"
    } == DEPRECATED_LEAF_KEYS & set(LEAF_KEY_REGISTRY)
    assert {key for key, entry in LEAF_KEY_REGISTRY.items() if entry.consumer_kind == "skill"} == {
        "research.agent_count",
        "research.default_depth",
    }
    for key, entry in LEAF_KEY_REGISTRY.items():
        if entry.consumer_kind in {"engine", "skill"}:
            assert entry.consumer is not None, key
            assert entry.reserved is False, key
        else:
            assert entry.consumer is None, key
            assert entry.reserved is True, key


def test_reserved_config_leaf_set_is_exact() -> None:
    reserved = {key for key, entry in LEAF_KEY_REGISTRY.items() if entry.reserved}

    assert reserved == DEPRECATED_LEAF_KEYS & set(LEAF_KEY_REGISTRY)
    assert {
        "audit.fix_safe",
        "runtime.adapter_catalog.claude.enabled",
        "runtime.adapter_catalog.codex.enabled",
        "runtime.adapter_catalog.opencode.enabled",
    } <= reserved
    strict_audit = leaf_key_lookup("verify.require_iter_audit_accepted")
    assert strict_audit.consumer == "eawf.workflow.lifecycle.iter_.close_iter"
    assert strict_audit.reserved is False


def test_leaf_key_lookup_known_key_returns_entry() -> None:
    entry = leaf_key_lookup("runtime.preference")
    assert isinstance(entry, LeafKey)
    assert entry.domain == "runtime"
    assert entry.type == "list_str"


def test_leaf_key_lookup_unknown_key_raises_canonical_message() -> None:
    """Success criterion 3: unknown keys raise the canonical error string."""
    with pytest.raises(ValueError) as exc_info:
        leaf_key_lookup("not.a.real.key")
    assert str(exc_info.value) == "unknown config key: 'not.a.real.key'"


def test_is_known_leaf_key_smoke() -> None:
    assert is_known_leaf_key("planning.max_parallel_waves") is True
    assert is_known_leaf_key("planning.approval") is False
    assert is_known_leaf_key("planning.does_not_exist") is False


def test_leaf_keys_by_domain_groups_runtime() -> None:
    """The domain filter helps audits group by section."""
    runtime_keys = leaf_keys_by_domain("runtime")
    runtime_names = {entry.key for entry in runtime_keys}
    assert "runtime.preference" in runtime_names
    assert "runtime.claude.stall_interval_s" in runtime_names
    # Telemetry must NOT appear under the runtime domain.
    assert all("telemetry" not in k.key for k in runtime_keys)


def test_leaf_keys_by_domain_unknown_returns_empty() -> None:
    """Unknown domain returns empty tuple (no exception)."""
    assert leaf_keys_by_domain("not-a-domain") == ()


def test_leaf_key_writable_layers_rejects_unknown() -> None:
    """LeafKey validator rejects unknown layer labels."""
    with pytest.raises(ValueError, match="unknown layer label"):
        LeafKey(
            key="bogus.example",
            domain="example",
            type="bool",
            default=True,
            writable_layers=("not-a-layer",),
        )


def test_leaf_key_choices_empty_rejected() -> None:
    """Empty choices tuple is unreachable; the validator refuses it."""
    with pytest.raises(ValueError, match="choices must be non-empty"):
        LeafKey(
            key="bogus.literal",
            domain="example",
            type="literal",
            default="x",
            writable_layers=(),
            choices=(),
        )


def test_leaf_key_rejects_consumer_and_reserved_together() -> None:
    with pytest.raises(ValueError, match="both consumer and reserved"):
        LeafKey(
            key="bogus.behavior",
            domain="example",
            type="bool",
            default=False,
            consumer="example.consume",
            reserved=True,
        )


def test_unset_dotted_removes_leaf_and_prunes_empty_maps() -> None:
    payload = {"verify": {"waiver_mode": "B"}, "planning": {"max_parallel_waves": 4}}

    assert unset_dotted(payload, ["verify", "waiver_mode"]) is True
    assert payload == {"planning": {"max_parallel_waves": 4}}


def test_unset_dotted_absent_path_is_noop() -> None:
    payload = {"planning": {"max_parallel_waves": 4}}
    before = dict(payload)

    assert unset_dotted(payload, ["verify", "waiver_mode"]) is False
    assert payload == before


def test_unset_dotted_rejects_empty_path() -> None:
    with pytest.raises(ValueError, match="at least one segment"):
        unset_dotted({}, [])


def test_runtime_preference_writable_in_every_runtime_layer() -> None:
    """Per brief §5.2.6: ``runtime.preference`` writable in every durable
    layer plus env/cli/wave."""
    entry = leaf_key_lookup("runtime.preference")
    assert set(entry.writable_layers) == {
        "global",
        "workspace",
        "repo",
        "branch",
        "local",
        "env",
        "cli",
        "wave",
    }


def test_schema_version_is_locked() -> None:
    """``schema_version`` is code-only; no operator-writable layer."""
    entry = leaf_key_lookup("schema_version")
    assert entry.writable_layers == ()
    assert entry.choices == ("1.0",)


# --- runtime.models tier-ladder override ------------------------------------


def test_resolve_runtime_tier_models_none_when_unconfigured(tmp_path: Path) -> None:
    """No ``runtime.models`` block resolves to ``None`` (built-in ladder wins)."""
    repo = tmp_path / "repo"
    _write_yaml(repo / ".ea" / "config.yaml", "research:\n  default_depth: high\n")
    assert resolve_runtime_tier_models(repo) is None


def test_resolve_runtime_tier_models_reads_codex_override(tmp_path: Path) -> None:
    """A ``runtime.models.codex`` block resolves to the 3-tier ladder."""
    repo = tmp_path / "repo"
    _write_yaml(
        repo / ".ea" / "config.yaml",
        "runtime:\n"
        "  models:\n"
        "    codex:\n"
        "      - gpt-5.3-codex-spark\n"
        "      - gpt-5.5\n"
        "      - gpt-5.5\n",
    )
    override = resolve_runtime_tier_models(repo)
    assert override == {"codex": ("gpt-5.3-codex-spark", "gpt-5.5", "gpt-5.5")}


def test_resolve_runtime_tier_models_rejects_short_ladder(tmp_path: Path) -> None:
    """Error path: a ladder that is not a 3-tuple fails validation."""
    repo = tmp_path / "repo"
    _write_yaml(
        repo / ".ea" / "config.yaml",
        "runtime:\n  models:\n    codex:\n      - gpt-5.5\n",
    )
    with pytest.raises(ValidationError):
        resolve_runtime_tier_models(repo)


def test_resolve_runtime_tier_models_rejects_unknown_runtime(tmp_path: Path) -> None:
    """Error path: an unknown runtime key trips extra=forbid."""
    repo = tmp_path / "repo"
    _write_yaml(
        repo / ".ea" / "config.yaml",
        "runtime:\n  models:\n    gemini:\n      - a\n      - b\n      - c\n",
    )
    with pytest.raises(ValidationError):
        resolve_runtime_tier_models(repo)


def test_resolve_auto_certify_is_on_when_no_layer_states_it(tmp_path: Path) -> None:
    assert layered.resolve_auto_certify(tmp_path / "repo") is True


def test_resolve_auto_certify_reads_the_repo_layer(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_yaml(repo / ".ea" / "config.yaml", "runtime:\n  auto_certify: false\n")
    assert layered.resolve_auto_certify(repo) is False


def test_resolve_auto_certify_rejects_a_non_boolean(tmp_path: Path) -> None:
    """Error path: a quoted switch is a string, not a boolean."""
    repo = tmp_path / "repo"
    _write_yaml(repo / ".ea" / "config.yaml", "runtime:\n  auto_certify: 'no'\n")
    with pytest.raises(ValidationError):
        layered.resolve_auto_certify(repo)
