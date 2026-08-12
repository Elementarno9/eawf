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
    CONFIG_REGISTRY,
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
    _write_yaml(repo / ".ea" / "config.yaml", "planning:\n  approval: repo_val\n")
    _write_yaml(
        repo / ".ea" / "branches" / "main.yaml",
        "planning:\n  approval: branch_val\n",
    )
    merged, sources = merge_config(
        workspace=None,
        repo=repo,
        env={},
        cli_overrides={},
        branch="main",
    )
    assert merged["planning"]["approval"] == "branch_val"
    assert sources["planning.approval"] == "branch"


def test_branch_layer_subdir_form_loaded(tmp_path: Path) -> None:
    """Branch names containing ``/`` resolve to nested files."""
    repo = tmp_path / "repo"
    _write_yaml(repo / ".ea" / "config.yaml", "planning:\n  approval: repo_val\n")
    _write_yaml(
        repo / ".ea" / "branches" / "feature" / "x.yaml",
        "planning:\n  approval: feature_x\n",
    )
    merged, sources = merge_config(
        workspace=None,
        repo=repo,
        env={},
        cli_overrides={},
        branch="feature/x",
    )
    assert merged["planning"]["approval"] == "feature_x"
    assert sources["planning.approval"] == "branch"


def test_branch_layer_missing_file_silently_skipped(tmp_path: Path) -> None:
    """Branch file absent → loader skips it, lower layer wins."""
    repo = tmp_path / "repo"
    _write_yaml(repo / ".ea" / "config.yaml", "planning:\n  approval: repo_val\n")
    merged, sources = merge_config(
        workspace=None,
        repo=repo,
        env={},
        cli_overrides={},
        branch="some-branch-with-no-file",
    )
    assert merged["planning"]["approval"] == "repo_val"
    assert sources["planning.approval"] == "repo"


def test_branch_layer_loses_to_local(tmp_path: Path) -> None:
    """Local layer is higher precedence than branch."""
    repo = tmp_path / "repo"
    _write_yaml(repo / ".ea" / "config.yaml", "planning:\n  approval: r\n")
    _write_yaml(repo / ".ea" / "branches" / "main.yaml", "planning:\n  approval: b\n")
    _write_yaml(repo / ".ea" / "local" / "config.yaml", "planning:\n  approval: l\n")
    merged, sources = merge_config(
        workspace=None,
        repo=repo,
        env={},
        cli_overrides={},
        branch="main",
    )
    assert merged["planning"]["approval"] == "l"
    assert sources["planning.approval"] == "local"


# --- Wave overlay -----------------------------------------------------------


def test_wave_overlay_overrides_local(tmp_path: Path) -> None:
    """Wave layer sits above local; daemon RAM wins."""
    repo = tmp_path / "repo"
    _write_yaml(repo / ".ea" / "local" / "config.yaml", "planning:\n  approval: local\n")
    merged, sources = merge_config(
        workspace=None,
        repo=repo,
        env={},
        cli_overrides={},
        wave_overlay={"planning": {"approval": "wave_val"}},
    )
    assert merged["planning"]["approval"] == "wave_val"
    assert sources["planning.approval"] == "wave"


def test_wave_overlay_loses_to_env() -> None:
    """Env is higher precedence than the wave RAM layer."""
    merged, sources = merge_config(
        workspace=None,
        repo=None,
        env={"EAWF_PLANNING__APPROVAL": "env_val"},
        cli_overrides={},
        wave_overlay={"planning": {"approval": "wave_val"}},
    )
    assert merged["planning"]["approval"] == "env_val"
    assert sources["planning.approval"] == "env"


def test_wave_overlay_loses_to_cli() -> None:
    merged, sources = merge_config(
        workspace=None,
        repo=None,
        env={},
        cli_overrides={"planning": {"approval": "cli_val"}},
        wave_overlay={"planning": {"approval": "wave_val"}},
    )
    assert merged["planning"]["approval"] == "cli_val"
    assert sources["planning.approval"] == "cli"


def test_empty_wave_overlay_noop(tmp_path: Path) -> None:
    """Falsy / empty wave_overlay is a no-op (no source map entry)."""
    repo = tmp_path / "repo"
    _write_yaml(repo / ".ea" / "config.yaml", "planning:\n  approval: r\n")
    merged, sources = merge_config(
        workspace=None,
        repo=repo,
        env={},
        cli_overrides={},
        wave_overlay={},
    )
    assert merged["planning"]["approval"] == "r"
    assert sources["planning.approval"] == "repo"


@pytest.mark.parametrize(
    "layer",
    ["global", "workspace", "repo", "branch", "local", "wave"],
)
def test_legacy_flow_transition_normalizes_in_memory_per_layer(
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

    assert merged["flow"]["advance_after"]["audit"] is True
    assert "auto_accept" not in merged["flow"]
    assert sources["flow.advance_after.audit"] == layer
    if source_path is not None:
        assert source_path.read_text(encoding="utf-8") == body


# --- Full nine-layer ordering ------------------------------------------------


def test_full_stack_ordering_with_branch_and_wave(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All nine layers active; CLI wins, branch/wave correctly placed."""
    fake_global = tmp_path / "g.yaml"
    monkeypatch.setattr(layered, "global_config_path", lambda: fake_global)
    _write_yaml(fake_global, "planning:\n  approval: g\n")
    workspace = tmp_path / "ws"
    _write_yaml(workspace / ".ea" / "config.yaml", "planning:\n  approval: w\n")
    repo = tmp_path / "repo"
    _write_yaml(repo / ".ea" / "config.yaml", "planning:\n  approval: r\n")
    _write_yaml(repo / ".ea" / "branches" / "main.yaml", "planning:\n  approval: b\n")
    _write_yaml(repo / ".ea" / "local" / "config.yaml", "planning:\n  approval: l\n")
    merged, sources = merge_config(
        workspace=workspace,
        repo=repo,
        env={"EAWF_PLANNING__APPROVAL": "e"},
        cli_overrides={"planning": {"approval": "c"}},
        branch="main",
        wave_overlay={"planning": {"approval": "wv"}},
    )
    assert merged["planning"]["approval"] == "c"
    assert sources["planning.approval"] == "cli"


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


def test_leaf_key_registry_has_full_catalog() -> None:
    """The catalog covers ~140+ keys (success criterion 3)."""
    # ~140 is the brief's nominal target; the canonical defaults already
    # carry more, so a floor check is the right contract.
    assert len(LEAF_KEY_REGISTRY) >= 140


def test_leaf_key_registry_includes_canonical_c08_keys() -> None:
    """The C08-new keys named in brief §5.2 are present."""
    must_have = {
        "config.layers_visible",
        "project.default_track",
        "project.goals",
        "project.success_metrics",
        "profiles.trusted",
        "runtime.preference",
        "runtime.fallback.on_errors",
        "runtime.fallback.retry_policy",
        "runtime.fallback.max_backoff_seconds",
        "telemetry.enabled",
        "telemetry.export.format",
        "telemetry.window_default",
        "telemetry.aggregate_window",
        "telemetry.db_kind",
        "dispatch.session_policy_default",
        "dispatch.session_handle_ttl_seconds",
        "language.runtime",
        "language.fast_extras",
        "vcs.conventions.release.agent_driven",
        "vcs.conventions.release.cadence",
    }
    missing = must_have - set(LEAF_KEY_REGISTRY)
    assert not missing, f"missing canonical C08 leaf keys: {sorted(missing)}"
    assert "project.default_subproject" not in LEAF_KEY_REGISTRY


def test_behavioral_config_leaves_have_exactly_one_binding() -> None:
    """Every interactive and hooks policy leaf is consumed or explicitly reserved."""
    behavior_keys = {entry.key for entry in CONFIG_REGISTRY} | {
        key for key in LEAF_KEY_REGISTRY if key.startswith("hooks.")
    }
    for key in behavior_keys:
        entry = leaf_key_lookup(key)
        assert (entry.consumer is not None) ^ entry.reserved, key


_EXPECTED_CONFIG_CONSUMERS: dict[str, str] = {
    "audit.default_level": "eawf.workflow.skills.audit._resolve_level",
    "daemon.proxy_enabled": "eawf.surfaces.cli._mutation._proxy_enabled",
    "dispatch.role_tier_token_cap": "eawf.workflow.dispatch.renderer.resolve_role_blocks",
    "estimation.eu_basis": "eawf.runtime.daemon.methods.state._wave_close_rollup_config",
    "estimation.eu_minutes": "eawf.runtime.daemon.methods.state._wave_close_rollup_config",
    "flow.advance_after.audit": "eawf.workflow.skills.flow.FlowSkill._run_steps",
    "flow.advance_after.polish": "eawf.workflow.skills.flow.FlowSkill._run_steps",
    "flow.advance_after.prep": "eawf.workflow.skills.flow.FlowSkill._run_steps",
    "flow.advance_after.research": "eawf.workflow.skills.flow.FlowSkill._run_steps",
    "flow.budget.enforce": "eawf.runtime.daemon.methods.agent._resolve_budget_enforce",
    "flow.max_repair_cycles": "eawf.workflow.skills.flow._config_max_repair_cycles",
    "planning.max_parallel_waves": ("eawf.workflow.lifecycle._capacity.resolve_max_parallel_waves"),
    "prep.auto_resume": "eawf.workflow.skills.prep.PrepSkill._resolve_auto_resume",
    "research.agent_count": "eawf.workflow.skills.research.ResearchSkill._resolve_agents",
    "research.auto_save": "eawf.workflow.skills.research.ResearchSkill._gather",
    "research.default_depth": "eawf.workflow.skills.research.ResearchSkill._resolve_depth",
    "review.default_level": "eawf.workflow.skills.review.ReviewSkill.action",
    "ship.gauntlet": "eawf.workflow.skills.ship._resolve_gauntlet",
    "telemetry.db_kind": "eawf.surfaces.cli.commands.metrics._read_telemetry_config",
    "telemetry.enabled": "eawf.surfaces.cli.commands.metrics._read_telemetry_config",
    "ui.glyphs": "eawf.surfaces.tui.app._persisted_glyphs",
    "ui.theme": "eawf.surfaces.tui.app._persisted_theme",
    "vcs.conventions.release.cadence": (
        "eawf.runtime.vcs.coauthor.requires_phase_release_preflight"
    ),
    "verify.require_iter_audit_accepted": "eawf.workflow.lifecycle.iter_.close_iter",
    "verify.waiver_mode": "eawf.workflow.lifecycle.waivers.resolve_waiver_mode",
}

_EXPECTED_CATALOG_ONLY_CONSUMERS: dict[str, str] = {
    "runtime.models.claude": "eawf.kernel.config.layered.resolve_runtime_tier_models",
    "runtime.models.codex": "eawf.kernel.config.layered.resolve_runtime_tier_models",
    "runtime.models.opencode": "eawf.kernel.config.layered.resolve_runtime_tier_models",
    "verify.juror_wall_clock_seconds": (
        "eawf.workflow.verify.readiness._overlay_repo_verify_leaves"
    ),
    "verify.odr_blocking": "eawf.workflow.verify.readiness._overlay_repo_verify_leaves",
}

_DECLARATIVE_CONSUMER = "eawf.kernel.config.layered.merge_config"
_EXPECTED_DECLARATIVE_CONSUMERS: dict[str, str] = {
    entry.key: _DECLARATIVE_CONSUMER
    for entry in CONFIG_REGISTRY
    if entry.key not in _EXPECTED_CONFIG_CONSUMERS and entry.key not in DEPRECATED_LEAF_KEYS
}


def test_config_consumer_set_is_exact_and_importable() -> None:
    """Every claimed consumer names the exact callable that reads or applies the leaf."""
    actual = {
        key: entry.consumer
        for key, entry in LEAF_KEY_REGISTRY.items()
        if entry.consumer is not None
    }

    expected = (
        _EXPECTED_CONFIG_CONSUMERS
        | _EXPECTED_CATALOG_ONLY_CONSUMERS
        | _EXPECTED_DECLARATIVE_CONSUMERS
    )
    assert actual == expected
    assert {
        key: value for key, value in actual.items() if key in {row.key for row in CONFIG_REGISTRY}
    } == _EXPECTED_CONFIG_CONSUMERS | _EXPECTED_DECLARATIVE_CONSUMERS
    for key, consumer in actual.items():
        resolved = pydoc.locate(consumer)
        assert resolved is not None, (key, consumer)
        assert callable(resolved), (key, consumer, resolved)


def test_every_config_leaf_has_exact_consumer_classification() -> None:
    kinds = {entry.consumer_kind for entry in LEAF_KEY_REGISTRY.values()}
    assert kinds == {"engine", "skill", "declarative", "deprecated", "reserved"}
    assert {
        key for key, entry in LEAF_KEY_REGISTRY.items() if entry.consumer_kind == "deprecated"
    } == DEPRECATED_LEAF_KEYS & set(LEAF_KEY_REGISTRY)
    assert {key for key, entry in LEAF_KEY_REGISTRY.items() if entry.consumer_kind == "skill"} == {
        "audit.default_level",
        "flow.advance_after.audit",
        "flow.advance_after.polish",
        "flow.advance_after.prep",
        "flow.advance_after.research",
        "flow.max_repair_cycles",
        "prep.auto_resume",
        "research.agent_count",
        "research.auto_save",
        "research.default_depth",
        "review.default_level",
        "ship.gauntlet",
    }
    for key, entry in LEAF_KEY_REGISTRY.items():
        if entry.consumer_kind in {"engine", "skill"}:
            assert entry.consumer is not None, key
            assert entry.reserved is False, key
        elif entry.consumer_kind in {"deprecated", "reserved"}:
            assert entry.consumer is None, key
            assert entry.reserved is True, key
        else:
            assert entry.consumer_kind == "declarative", key
            assert entry.reserved is False, key
            expected_consumer = _EXPECTED_DECLARATIVE_CONSUMERS.get(key)
            assert entry.consumer == expected_consumer, key


def test_reserved_config_leaf_set_is_exact() -> None:
    hook_keys = {key for key in LEAF_KEY_REGISTRY if key.startswith("hooks.")}
    expected = hook_keys | (DEPRECATED_LEAF_KEYS & set(LEAF_KEY_REGISTRY))
    reserved = {key for key, entry in LEAF_KEY_REGISTRY.items() if entry.reserved}

    assert reserved == expected
    assert {
        "audit.fix_safe",
        "hooks.enabled",
        "runtime.adapter_catalog.claude.enabled",
        "runtime.adapter_catalog.codex.enabled",
        "runtime.adapter_catalog.opencode.enabled",
    } <= reserved
    for key in ("planning.approval", "planning.auto_plan"):
        entry = leaf_key_lookup(key)
        assert entry.consumer == _DECLARATIVE_CONSUMER
        assert entry.reserved is False
    repair_cycles = leaf_key_lookup("flow.max_repair_cycles")
    assert repair_cycles.consumer == "eawf.workflow.skills.flow._config_max_repair_cycles"
    assert repair_cycles.reserved is False
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
    assert is_known_leaf_key("planning.approval") is True
    assert is_known_leaf_key("planning.does_not_exist") is False


def test_leaf_keys_by_domain_groups_runtime() -> None:
    """The domain filter helps audits group by section."""
    runtime_keys = leaf_keys_by_domain("runtime")
    runtime_names = {entry.key for entry in runtime_keys}
    assert "runtime.preference" in runtime_names
    assert "runtime.fallback.retry_policy" in runtime_names
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


def test_language_runtime_is_locked() -> None:
    """``language.runtime`` is locked at python per D6."""
    entry = leaf_key_lookup("language.runtime")
    assert entry.writable_layers == ()
    assert entry.choices == ("python",)


# --- runtime.models tier-ladder override ------------------------------------


def test_resolve_runtime_tier_models_none_when_unconfigured(tmp_path: Path) -> None:
    """No ``runtime.models`` block resolves to ``None`` (built-in ladder wins)."""
    repo = tmp_path / "repo"
    _write_yaml(repo / ".ea" / "config.yaml", "planning:\n  approval: repo_val\n")
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
