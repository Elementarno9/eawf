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
6. Source map keys are dotted-path-of-leaf (``research.default_depth``), not
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
from pydantic import ValidationError

from eawf.kernel.config import layered
from eawf.kernel.config.defaults import BUILT_IN_DEFAULTS
from eawf.kernel.config.layered import LAYER_ORDER, merge_config
from eawf.kernel.config.schema import EstimationConfig, VerifyConfig


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


def test_built_in_defaults_hold_a_section_for_every_catalogued_leaf_with_a_value() -> None:
    """Every section a catalogued leaf with a shipped value lives under is built in."""
    required = {
        "profiles",
        "runtime",
        "ui",
        "telemetry",
        "agents",
        "research",
        "preferences",
        "verify",
        "estimation",
        "flow",
        "vcs",
        "daemon",
        "local",
        "worktrees",
    }
    assert required == set(BUILT_IN_DEFAULTS) - {"schema_version"}


def test_only_builtin_layer_contributes_for_empty_stack() -> None:
    merged, sources = merge_config(workspace=None, repo=None, env={}, cli_overrides={})
    # Sample several keys.
    for dotted in (
        "estimation.eu_minutes",
        "research.default_depth",
        "vcs.conventions.subject_style",
    ):
        assert sources[dotted] == "built-in"
    # Default values match.
    assert merged["estimation"]["eu_minutes"] == 30
    assert merged["research"]["default_depth"] == "medium"
    assert "project" not in merged
    assert "memory" not in merged
    assert merged["vcs"]["conventions"]["subject_style"] == "trailer"
    assert merged["verify"] == {
        "jury_max_brier": 0.25,
        "jury_max_co_error": 0.10,
        "juror_wall_clock_seconds": 600.0,
        "odr_blocking": False,
        "retyped_rule_threshold": 3,
        "waiver_mode": "B",
    }
    assert sources["verify.jury_max_brier"] == "built-in"
    assert sources["verify.jury_max_co_error"] == "built-in"
    assert sources["verify.juror_wall_clock_seconds"] == "built-in"
    assert sources["verify.odr_blocking"] == "built-in"
    assert sources["verify.retyped_rule_threshold"] == "built-in"
    assert sources["verify.waiver_mode"] == "built-in"


# --- Layer precedence -------------------------------------------------------


def _write_yaml(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_global_layer_overrides_builtin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_global = tmp_path / "g.yaml"
    monkeypatch.setattr(layered, "global_config_path", lambda: fake_global)
    _write_yaml(fake_global, "research:\n  default_depth: auto\n")
    merged, sources = merge_config(workspace=None, repo=None, env={}, cli_overrides={})
    assert merged["research"]["default_depth"] == "auto"
    assert sources["research.default_depth"] == "global"


def test_workspace_overrides_global(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_global = tmp_path / "g.yaml"
    monkeypatch.setattr(layered, "global_config_path", lambda: fake_global)
    _write_yaml(fake_global, "research:\n  default_depth: global_val\n")
    workspace = tmp_path / "ws"
    _write_yaml(workspace / ".ea" / "config.yaml", "research:\n  default_depth: ws_val\n")
    merged, sources = merge_config(workspace=workspace, repo=None, env={}, cli_overrides={})
    assert merged["research"]["default_depth"] == "ws_val"
    assert sources["research.default_depth"] == "workspace"


def test_repo_overrides_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "ws"
    _write_yaml(workspace / ".ea" / "config.yaml", "research:\n  default_depth: ws\n")
    repo = tmp_path / "repo"
    _write_yaml(repo / ".ea" / "config.yaml", "research:\n  default_depth: repo\n")
    merged, sources = merge_config(workspace=workspace, repo=repo, env={}, cli_overrides={})
    assert merged["research"]["default_depth"] == "repo"
    assert sources["research.default_depth"] == "repo"


def test_verify_strict_leaves_report_source_layer(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_yaml(
        repo / ".ea" / "config.yaml",
        (
            "verify:\n"
            "  juror_wall_clock_seconds: 900\n"
            "  odr_blocking: true\n"
            "  require_iter_audit_accepted: true\n"
            "  waiver_mode: disabled\n"
        ),
    )

    merged, sources = merge_config(workspace=None, repo=repo, env={}, cli_overrides={})

    verify = VerifyConfig.model_validate(merged["verify"])
    assert verify.juror_wall_clock_seconds == pytest.approx(900.0)
    assert verify.odr_blocking is True
    assert "require_iter_audit_accepted" not in merged["verify"]
    assert verify.waiver_mode == "disabled"
    assert sources["verify.juror_wall_clock_seconds"] == "repo"
    assert sources["verify.odr_blocking"] == "repo"
    assert sources["verify.waiver_mode"] == "repo"


@pytest.mark.parametrize("value", ["disable", "DISABLED", 1, True, None])
def test_verify_config_rejects_invalid_waiver_mode(value: object) -> None:
    with pytest.raises(ValidationError):
        VerifyConfig.model_validate({"waiver_mode": value})


def test_verify_config_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        VerifyConfig.model_validate({"require_iter_audit_acceptd": True})


def test_local_overrides_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_yaml(repo / ".ea" / "config.yaml", "research:\n  default_depth: repo\n")
    _write_yaml(repo / ".ea" / "local" / "config.yaml", "research:\n  default_depth: local\n")
    merged, sources = merge_config(workspace=None, repo=repo, env={}, cli_overrides={})
    assert merged["research"]["default_depth"] == "local"
    assert sources["research.default_depth"] == "local"


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


def test_env_overrides_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_yaml(repo / ".ea" / "config.yaml", "ui:\n  theme: cb\n")
    merged, sources = merge_config(
        workspace=None,
        repo=repo,
        env={"EAWF_UI__THEME": "light"},
        cli_overrides={},
    )
    assert merged["ui"]["theme"] == "light"
    assert sources["ui.theme"] == "env"


def test_cli_overrides_env() -> None:
    merged, sources = merge_config(
        workspace=None,
        repo=None,
        env={"EAWF_UI__THEME": "light"},
        cli_overrides={"ui": {"theme": "cb"}},
    )
    assert merged["ui"]["theme"] == "cb"
    assert sources["ui.theme"] == "cli"


def test_full_stack_ordering(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """All seven layers active simultaneously — CLI wins, env loses, etc."""
    fake_global = tmp_path / "g.yaml"
    monkeypatch.setattr(layered, "global_config_path", lambda: fake_global)
    _write_yaml(fake_global, "ui:\n  theme: dark\n")
    workspace = tmp_path / "ws"
    _write_yaml(workspace / ".ea" / "config.yaml", "ui:\n  theme: light\n")
    repo = tmp_path / "repo"
    _write_yaml(repo / ".ea" / "config.yaml", "ui:\n  theme: cb\n")
    _write_yaml(repo / ".ea" / "local" / "config.yaml", "ui:\n  theme: light\n")
    merged, sources = merge_config(
        workspace=workspace,
        repo=repo,
        env={"EAWF_UI__THEME": "auto"},
        cli_overrides={"ui": {"theme": "cb"}},
    )
    assert merged["ui"]["theme"] == "cb"
    assert sources["ui.theme"] == "cli"


# --- Deep-merge of maps -----------------------------------------------------


def test_deep_merge_preserves_sibling_keys(tmp_path: Path) -> None:
    """A repo override of ``estimation.eu_minutes`` keeps ``estimation.eu_basis``."""
    repo = tmp_path / "repo"
    _write_yaml(repo / ".ea" / "config.yaml", "estimation:\n  eu_minutes: 45\n")
    merged, sources = merge_config(workspace=None, repo=repo, env={}, cli_overrides={})
    assert merged["estimation"]["eu_minutes"] == 45
    assert sources["estimation.eu_minutes"] == "repo"
    # eu_basis was NOT overridden — it stays from built-in.
    assert merged["estimation"]["eu_basis"] == "api_duration"
    assert sources["estimation.eu_basis"] == "built-in"


def test_lists_replace_not_concat(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_yaml(
        repo / ".ea" / "config.yaml",
        "runtime:\n  preference: [codex]\n",
    )
    merged, _ = merge_config(workspace=None, repo=repo, env={}, cli_overrides={})
    assert merged["runtime"]["preference"] == ["codex"]


# --- Env coercion -----------------------------------------------------------


def test_env_double_underscore_is_separator() -> None:
    merged, sources = merge_config(
        workspace=None,
        repo=None,
        env={"EAWF_UI__GLYPHS": "ascii"},
        cli_overrides={},
    )
    assert merged["ui"]["glyphs"] == "ascii"
    assert sources["ui.glyphs"] == "env"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", True), ("FALSE", False), ("null", None), ("60", 60), ("1.5", 1.5), ("x", "x")],
)
def test_env_scalar_coercion(raw: str, expected: object) -> None:
    assert layered._coerce_scalar(raw) == expected


def test_env_unknown_prefix_ignored() -> None:
    merged, sources = merge_config(
        workspace=None,
        repo=None,
        env={"NOT_EAWF_RESEARCH__DEFAULT_DEPTH": "ignored"},
        cli_overrides={},
    )
    assert merged["research"]["default_depth"] == "medium"
    assert sources["research.default_depth"] == "built-in"


# --- Env overrides held to the leaf catalog ---------------------------------


@pytest.fixture
def _fresh_env_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forget which variables this process already warned about."""
    monkeypatch.setattr(layered, "_WARNED_ENV_VARS", set())


@pytest.mark.usefixtures("_fresh_env_warnings")
def test_env_override_of_a_leaf_not_env_writable_is_ignored_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING", logger=layered.__name__):
        merged, sources = merge_config(
            workspace=None,
            repo=None,
            env={"EAWF_RESEARCH__AGENT_COUNT": "9"},
            cli_overrides={},
        )
    assert merged["research"]["agent_count"] == 4
    assert sources["research.agent_count"] == "built-in"
    assert "EAWF_RESEARCH__AGENT_COUNT" in caplog.text
    assert "not writable from the env layer" in caplog.text


@pytest.mark.usefixtures("_fresh_env_warnings")
def test_env_override_warning_is_logged_once_per_variable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    env = {"EAWF_PLANNING__MAX_PARALLEL_WAVES": "9"}
    with caplog.at_level("WARNING", logger=layered.__name__):
        merge_config(workspace=None, repo=None, env=env, cli_overrides={})
        merge_config(workspace=None, repo=None, env=env, cli_overrides={})
    assert caplog.text.count("EAWF_PLANNING__MAX_PARALLEL_WAVES") == 1


@pytest.mark.usefixtures("_fresh_env_warnings")
@pytest.mark.parametrize("variable", ["EAWF_UI__TEHME", "EAWF_NOT_A_SECTION", "EAWF_RUNTIME__KIND"])
def test_env_override_of_an_unknown_key_is_ignored_with_a_warning(
    variable: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING", logger=layered.__name__):
        merged, sources = merge_config(
            workspace=None, repo=None, env={variable: "light"}, cli_overrides={}
        )
    assert not [key for key, layer in sources.items() if layer == "env"]
    assert "not_a_section" not in merged
    assert "unknown config key" in caplog.text


@pytest.mark.usefixtures("_fresh_env_warnings")
def test_env_override_reserved_control_var_is_skipped_silently(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING", logger=layered.__name__):
        _merged, sources = merge_config(
            workspace=None, repo=None, env={"EAWF_VERBOSE": "1", "EAWF_": "x"}, cli_overrides={}
        )
    assert "env" not in sources.values()
    assert caplog.text == ""


@pytest.mark.parametrize(
    ("variable", "raw", "reason"),
    [
        ("EAWF_UI__THEME", "zzz", "is not one of"),
        ("EAWF_UI__THEME", "null", "must not be null"),
        ("EAWF_UI__THEME", "7", "is not one of"),
        ("EAWF_RUNTIME__PREFERENCE", "codex", "is not of type list_str"),
    ],
)
def test_env_override_with_a_value_its_leaf_refuses_is_refused(
    variable: str, raw: str, reason: str
) -> None:
    with pytest.raises(layered.EnvOverrideError) as excinfo:
        merge_config(workspace=None, repo=None, env={variable: raw}, cli_overrides={})
    assert excinfo.value.variable == variable
    assert reason in excinfo.value.reason
    assert str(excinfo.value).startswith(f"config_env_override_invalid: {variable}: ")


def test_env_override_error_is_the_cli_validation_error() -> None:
    from eawf.surfaces.cli.errors import ValidationError as CliValidationError

    assert issubclass(layered.EnvOverrideError, CliValidationError)


@pytest.mark.parametrize("theme", ["dark", "light", "cb", "auto"])
def test_env_override_accepts_every_choice_of_an_env_writable_leaf(theme: str) -> None:
    merged, sources = merge_config(
        workspace=None, repo=None, env={"EAWF_UI__THEME": theme}, cli_overrides={}
    )
    assert merged["ui"]["theme"] == theme
    assert sources["ui.theme"] == "env"


@pytest.mark.usefixtures("_fresh_env_warnings")
def test_env_override_cannot_loosen_a_verify_gate(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    repo = tmp_path / "repo"
    _write_yaml(repo / ".ea" / "config.yaml", "verify:\n  odr_blocking: true\n")
    with caplog.at_level("WARNING", logger=layered.__name__):
        merged, sources = merge_config(
            workspace=None,
            repo=repo,
            env={"EAWF_VERIFY__ODR_BLOCKING": "false"},
            cli_overrides={},
        )
    assert merged["verify"]["odr_blocking"] is True
    assert sources["verify.odr_blocking"] == "repo"
    assert "EAWF_VERIFY__ODR_BLOCKING" in caplog.text


def test_no_verify_leaf_is_env_writable() -> None:
    from eawf.kernel.config.registry import LEAF_KEY_REGISTRY

    env_writable = [
        key
        for key, entry in LEAF_KEY_REGISTRY.items()
        if key.startswith("verify.") and "env" in entry.writable_layers
    ]
    assert env_writable == []


def test_env_strips_prefix_only_no_value_segment() -> None:
    """``EAWF_`` with nothing after is ignored (no key to set)."""
    _merged, sources = merge_config(
        workspace=None,
        repo=None,
        env={"EAWF_": "ignored"},
        cli_overrides={},
    )
    # Built-ins still reachable.
    assert sources["research.default_depth"] == "built-in"


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
    assert sources["research.default_depth"] == "built-in"


# --- Idempotence ------------------------------------------------------------


def test_merge_is_idempotent_on_same_inputs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_yaml(repo / ".ea" / "config.yaml", "research:\n  default_depth: x\n")
    a = merge_config(workspace=None, repo=repo, env={}, cli_overrides={})
    b = merge_config(workspace=None, repo=repo, env={}, cli_overrides={})
    assert a == b


def test_merge_does_not_mutate_built_in_defaults() -> None:
    """Repeated merges must not mutate the package-level constants."""
    snapshot = repr(BUILT_IN_DEFAULTS)
    merge_config(
        workspace=None,
        repo=None,
        env={"EAWF_UI__THEME": "light"},
        cli_overrides={"research": {"default_depth": "from_cli"}},
    )
    assert repr(BUILT_IN_DEFAULTS) == snapshot


# --- get_dotted helper ------------------------------------------------------


def test_get_dotted_returns_value_for_known_key() -> None:
    merged, _ = merge_config(workspace=None, repo=None, env={}, cli_overrides={})
    assert layered.get_dotted(merged, "estimation.eu_minutes") == 30


def test_eu_basis_rejects_unknown() -> None:
    with pytest.raises(ValidationError):
        EstimationConfig.model_validate({"eu_basis": "idle_calendar"})


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
