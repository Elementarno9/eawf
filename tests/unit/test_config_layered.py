"""Unit tests for :mod:`eawf.kernel.config.layered`.

The layered merge engine is the core of ``eawf config get/set/validate``. The
contract under test:

1. Built-in defaults supply every required key with source ``"built-in"``.
2. Layer precedence (lowest → highest): built-in → global → workspace → repo
   → local → env → cli. Later layers override earlier; later layers contribute
   the source label for the keys they touch.
3. Maps deep-merge; lists and scalars replace.
4. ``EAWF_*`` env vars become dotted overrides; double-underscore is the
   key separator.
5. CLI overrides win over everything else.
6. Source map keys are dotted-path-of-leaf (``planning.approval``), not
   nested-section keys.
7. Layered merge is idempotent: calling :func:`merge_config` with the same
   inputs twice yields equal outputs.

The tests use ``monkeypatch`` to redirect every layer file to ``tmp_path`` so
the host's actual ``~/.config/eawf/config.yaml`` is never touched.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from eawf.kernel.config import layered
from eawf.kernel.config.defaults import BUILT_IN_DEFAULTS
from eawf.kernel.config.layered import LAYER_ORDER, merge_config


@pytest.fixture(autouse=True)
def _isolate_global(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Redirect global-config lookups into a per-test tmp dir."""
    fake_global = tmp_path / "fake-global.yaml"
    monkeypatch.setattr(layered, "global_config_path", lambda: fake_global)
    yield


# --- Layer-order contract ---------------------------------------------------


def test_layer_order_canonical() -> None:
    """P25-W14 (C08) extends the six durable layers with branch + wave."""
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


def test_built_in_defaults_have_every_required_top_level_section() -> None:
    """Every section in docs/architecture/envelope.md 'Config schema required sections'."""
    required = {
        "cli",
        "project",
        "workspace",
        "profiles",
        "runtime",
        "ui",
        "storage",
        "research",
        "planning",
        "estimation",
        "audit",
        "ship",
        "review",
        "polish",
        "flow",
        "memory",
        "vcs",
        "worktrees",
        "acceptance",
        "security",
        "hooks",
        "mcp",
        "statusline",
        "docs",
        "commands",
        "state_schema",
    }
    assert required.issubset(BUILT_IN_DEFAULTS)


def test_only_builtin_layer_contributes_for_empty_stack() -> None:
    merged, sources = merge_config(workspace=None, repo=None, env={}, cli_overrides={})
    # Sample several keys.
    for dotted in (
        "cli.canonical_command",
        "estimation.eu_minutes",
        "planning.approval",
        "vcs.conventions.subject_style",
        "vcs.conventions.release.cadence",
        "vcs.conventions.release.agent_driven",
    ):
        assert sources[dotted] == "built-in"
    # Default values match.
    assert merged["estimation"]["eu_minutes"] == 30
    assert merged["planning"]["approval"] == "ask"
    assert merged["vcs"]["conventions"]["subject_style"] == "bracket"
    assert merged["vcs"]["conventions"]["release"] == {
        "cadence": "manual",
        "agent_driven": "per-phase",
    }


# --- Layer precedence -------------------------------------------------------


def _write_yaml(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_global_layer_overrides_builtin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_global = tmp_path / "g.yaml"
    monkeypatch.setattr(layered, "global_config_path", lambda: fake_global)
    _write_yaml(fake_global, "planning:\n  approval: auto\n")
    merged, sources = merge_config(workspace=None, repo=None, env={}, cli_overrides={})
    assert merged["planning"]["approval"] == "auto"
    assert sources["planning.approval"] == "global"


def test_workspace_overrides_global(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_global = tmp_path / "g.yaml"
    monkeypatch.setattr(layered, "global_config_path", lambda: fake_global)
    _write_yaml(fake_global, "planning:\n  approval: global_val\n")
    workspace = tmp_path / "ws"
    _write_yaml(workspace / ".ea" / "config.yaml", "planning:\n  approval: ws_val\n")
    merged, sources = merge_config(workspace=workspace, repo=None, env={}, cli_overrides={})
    assert merged["planning"]["approval"] == "ws_val"
    assert sources["planning.approval"] == "workspace"


def test_repo_overrides_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "ws"
    _write_yaml(workspace / ".ea" / "config.yaml", "planning:\n  approval: ws\n")
    repo = tmp_path / "repo"
    _write_yaml(repo / ".ea" / "config.yaml", "planning:\n  approval: repo\n")
    merged, sources = merge_config(workspace=workspace, repo=repo, env={}, cli_overrides={})
    assert merged["planning"]["approval"] == "repo"
    assert sources["planning.approval"] == "repo"


def test_local_overrides_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_yaml(repo / ".ea" / "config.yaml", "planning:\n  approval: repo\n")
    _write_yaml(repo / ".ea" / "local" / "config.yaml", "planning:\n  approval: local\n")
    merged, sources = merge_config(workspace=None, repo=repo, env={}, cli_overrides={})
    assert merged["planning"]["approval"] == "local"
    assert sources["planning.approval"] == "local"


def test_vcs_conventions_subject_style_overlays_across_repo_and_local(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_yaml(
        repo / ".ea" / "config.yaml",
        "vcs:\n  conventions:\n    subject_style: trailer\n",
    )
    _write_yaml(
        repo / ".ea" / "local" / "config.yaml",
        "vcs:\n  conventions:\n    subject_style: bracket\n",
    )
    merged, sources = merge_config(workspace=None, repo=repo, env={}, cli_overrides={})
    assert merged["vcs"]["conventions"]["subject_style"] == "bracket"
    assert sources["vcs.conventions.subject_style"] == "local"


def test_env_overrides_local(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_yaml(repo / ".ea" / "local" / "config.yaml", "planning:\n  approval: local\n")
    merged, sources = merge_config(
        workspace=None,
        repo=repo,
        env={"EAWF_PLANNING__APPROVAL": "env_val"},
        cli_overrides={},
    )
    assert merged["planning"]["approval"] == "env_val"
    assert sources["planning.approval"] == "env"


def test_cli_overrides_env() -> None:
    merged, sources = merge_config(
        workspace=None,
        repo=None,
        env={"EAWF_PLANNING__APPROVAL": "from_env"},
        cli_overrides={"planning": {"approval": "from_cli"}},
    )
    assert merged["planning"]["approval"] == "from_cli"
    assert sources["planning.approval"] == "cli"


def test_full_stack_ordering(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """All seven layers active simultaneously — CLI wins, env loses, etc."""
    fake_global = tmp_path / "g.yaml"
    monkeypatch.setattr(layered, "global_config_path", lambda: fake_global)
    _write_yaml(fake_global, "planning:\n  approval: g\n")
    workspace = tmp_path / "ws"
    _write_yaml(workspace / ".ea" / "config.yaml", "planning:\n  approval: ws\n")
    repo = tmp_path / "repo"
    _write_yaml(repo / ".ea" / "config.yaml", "planning:\n  approval: r\n")
    _write_yaml(repo / ".ea" / "local" / "config.yaml", "planning:\n  approval: l\n")
    merged, sources = merge_config(
        workspace=workspace,
        repo=repo,
        env={"EAWF_PLANNING__APPROVAL": "e"},
        cli_overrides={"planning": {"approval": "c"}},
    )
    assert merged["planning"]["approval"] == "c"
    assert sources["planning.approval"] == "cli"


# --- Deep-merge of maps -----------------------------------------------------


def test_deep_merge_preserves_sibling_keys(tmp_path: Path) -> None:
    """A repo override of ``estimation.eu_minutes`` keeps ``estimation.idle_policy``."""
    repo = tmp_path / "repo"
    _write_yaml(repo / ".ea" / "config.yaml", "estimation:\n  eu_minutes: 45\n")
    merged, sources = merge_config(workspace=None, repo=repo, env={}, cli_overrides={})
    assert merged["estimation"]["eu_minutes"] == 45
    assert sources["estimation.eu_minutes"] == "repo"
    # idle_policy was NOT overridden — it stays from built-in.
    assert merged["estimation"]["idle_policy"] == "D30_non_agent_gap"
    assert sources["estimation.idle_policy"] == "built-in"


def test_lists_replace_not_concat(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_yaml(
        repo / ".ea" / "config.yaml",
        "audit:\n  default_checks: [tests]\n",
    )
    merged, _ = merge_config(workspace=None, repo=repo, env={}, cli_overrides={})
    assert merged["audit"]["default_checks"] == ["tests"]


# --- Env coercion -----------------------------------------------------------


def test_env_double_underscore_is_separator() -> None:
    merged, sources = merge_config(
        workspace=None,
        repo=None,
        env={"EAWF_ESTIMATION__EU_MINUTES": "60"},
        cli_overrides={},
    )
    assert merged["estimation"]["eu_minutes"] == 60
    assert sources["estimation.eu_minutes"] == "env"


def test_env_boolean_coercion() -> None:
    merged, _ = merge_config(
        workspace=None,
        repo=None,
        env={"EAWF_ESTIMATION__ENABLED": "false"},
        cli_overrides={},
    )
    assert merged["estimation"]["enabled"] is False


def test_env_unknown_prefix_ignored() -> None:
    merged, sources = merge_config(
        workspace=None,
        repo=None,
        env={"NOT_EAWF_PLANNING__APPROVAL": "ignored"},
        cli_overrides={},
    )
    assert merged["planning"]["approval"] == "ask"
    assert sources["planning.approval"] == "built-in"


def test_env_strips_prefix_only_no_value_segment() -> None:
    """``EAWF_`` with nothing after is ignored (no key to set)."""
    _merged, sources = merge_config(
        workspace=None,
        repo=None,
        env={"EAWF_": "ignored"},
        cli_overrides={},
    )
    # Built-ins still reachable.
    assert sources["planning.approval"] == "built-in"


def test_env_blitz_recursion_knobs_are_reserved() -> None:
    """Blitz recursion-guard env knobs must not leak into the config layer.

    The blitz skill writes ``EAWF_BLITZ_DEPTH_COUNTER`` into ``os.environ`` as
    its recursion counter (and ``EAWF_BLITZ_DEPTH`` is the user-facing cap).
    Both are runtime control knobs, not config overrides — without reserving
    them the env layer injects a phantom ``blitz_depth_counter`` top-level key
    that the strict config schema then rejects with ``extra_forbidden``.
    """
    merged, sources = merge_config(
        workspace=None,
        repo=None,
        env={"EAWF_BLITZ_DEPTH": "8", "EAWF_BLITZ_DEPTH_COUNTER": "1"},
        cli_overrides={},
    )
    assert "blitz_depth" not in merged
    assert "blitz_depth_counter" not in merged
    assert "blitz_depth_counter" not in sources
    # Built-ins remain reachable — the reserved knobs did not poison the merge.
    assert sources["planning.approval"] == "built-in"


# --- Idempotence ------------------------------------------------------------


def test_merge_is_idempotent_on_same_inputs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_yaml(repo / ".ea" / "config.yaml", "planning:\n  approval: x\n")
    a = merge_config(workspace=None, repo=repo, env={}, cli_overrides={})
    b = merge_config(workspace=None, repo=repo, env={}, cli_overrides={})
    assert a == b


def test_merge_does_not_mutate_built_in_defaults() -> None:
    """Repeated merges must not mutate the package-level constants."""
    snapshot = repr(BUILT_IN_DEFAULTS)
    merge_config(
        workspace=None,
        repo=None,
        env={"EAWF_PLANNING__APPROVAL": "from_env"},
        cli_overrides={"planning": {"approval": "from_cli"}},
    )
    assert repr(BUILT_IN_DEFAULTS) == snapshot


# --- get_dotted helper ------------------------------------------------------


def test_get_dotted_returns_value_for_known_key() -> None:
    merged, _ = merge_config(workspace=None, repo=None, env={}, cli_overrides={})
    assert layered.get_dotted(merged, "estimation.eu_minutes") == 30


def test_get_dotted_raises_for_missing_key() -> None:
    merged, _ = merge_config(workspace=None, repo=None, env={}, cli_overrides={})
    with pytest.raises(KeyError):
        layered.get_dotted(merged, "no.such.key")


def test_get_dotted_partial_path_into_scalar_raises() -> None:
    merged, _ = merge_config(workspace=None, repo=None, env={}, cli_overrides={})
    # estimation.eu_minutes is a scalar; you can't descend into it.
    with pytest.raises(KeyError):
        layered.get_dotted(merged, "estimation.eu_minutes.further")


# --- Layer-path helpers -----------------------------------------------------


def test_layer_path_global_returns_resolved_global(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(layered, "global_config_path", lambda: Path("/tmp/g.yaml"))
    assert layered.layer_path("global", workspace=None, repo=None) == Path("/tmp/g.yaml")


def test_layer_path_workspace_requires_workspace() -> None:
    with pytest.raises(ValueError):
        layered.layer_path("workspace", workspace=None, repo=None)


def test_layer_path_repo_requires_repo() -> None:
    with pytest.raises(ValueError):
        layered.layer_path("repo", workspace=None, repo=None)


def test_layer_path_local_requires_repo() -> None:
    with pytest.raises(ValueError):
        layered.layer_path("local", workspace=None, repo=None)


def test_layer_path_built_in_is_not_writable() -> None:
    with pytest.raises(ValueError):
        layered.layer_path("built-in", workspace=None, repo=None)


def test_layer_path_returns_repo_local_for_local() -> None:
    repo = Path("/tmp/repo")
    assert layered.layer_path("local", workspace=None, repo=repo) == (
        repo / ".ea" / "local" / "config.yaml"
    )
