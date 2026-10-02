"""End-to-end CliRunner tests for ``eawf config``.

Verifies the full CLI surface against a tmp-path repo:

- ``config set foo.bar 42 --scope local`` → writes to local layer.
- ``config get foo.bar`` returns ``42`` and source ``local``.
- ``config get foo.bar --json`` returns ``{key, value, source}`` envelope.
- ``config validate`` on a malformed file exits ``4``.
- ``config profile enable python`` materialises required state keys.
- ``config set built-in.x y --scope built-in`` exits ``3`` (built-in is
  read-only).

To keep the host's actual ``~/.config/eawf/config.yaml`` out of every run, the
fixture redirects :func:`eawf.kernel.config.layered.global_config_path` to a per-test
tmp path. ``Path.cwd()`` is the anchor for the ``repo`` layer; the test changes
into the tmp repo via ``monkeypatch.chdir``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import orjson
import pytest
import yaml
from typer.testing import CliRunner

from eawf.kernel.config import layered
from eawf.platform.rules.carriers import builtin_carrier_roles, carrier_target
from eawf.runtime.daemon.churn import SUITE_SESSION_ENV
from eawf.surfaces.cli.app import app

runner = CliRunner()


@pytest.fixture
def repo_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Provide an isolated repo root + sandboxed global-config path."""
    repo = tmp_path / "repo"
    (repo / ".ea").mkdir(parents=True)
    fake_global = tmp_path / "global.yaml"
    monkeypatch.setattr(layered, "global_config_path", lambda: fake_global)
    monkeypatch.chdir(repo)
    yield repo


# --- config set + get round-trip --------------------------------------------


def test_set_then_get_returns_source_local(repo_root: Path) -> None:
    set_result = runner.invoke(app, ["config", "set", "foo.bar", "42", "--scope", "local"])
    assert set_result.exit_code == 0, set_result.output

    get_result = runner.invoke(app, ["config", "get", "foo.bar"])
    assert get_result.exit_code == 0, get_result.output
    assert "42" in get_result.output
    assert "local" in get_result.output


def test_get_json_envelope_shape(repo_root: Path) -> None:
    runner.invoke(app, ["config", "set", "foo.bar", "42", "--scope", "local"])
    get_result = runner.invoke(app, ["--json", "config", "get", "foo.bar"])
    assert get_result.exit_code == 0, get_result.output
    body = json.loads(get_result.output)["result"]
    assert body == {"key": "foo.bar", "value": 42, "source": "local"}


def test_set_to_repo_writes_to_repo_layer(repo_root: Path) -> None:
    result = runner.invoke(
        app, ["config", "set", "research.default_depth", "deep", "--scope", "repo"]
    )
    assert result.exit_code == 0, result.output
    contents = (repo_root / ".ea" / "config.yaml").read_text(encoding="utf-8")
    parsed = yaml.safe_load(contents)
    assert parsed["research"]["default_depth"] == "deep"


def test_set_to_local_writes_to_local_layer(repo_root: Path) -> None:
    runner.invoke(app, ["config", "set", "foo.bar", "1", "--scope", "local"])
    target = repo_root / ".ea" / "local" / "config.yaml"
    assert target.exists()


def test_get_unknown_key_returns_exit_code_2(repo_root: Path) -> None:
    result = runner.invoke(app, ["config", "get", "no.such.key"])
    # Post C05 § 5.3: NotFound bucket = USER_ERROR (1) under new 0..5 surface.
    assert result.exit_code == 1


def test_get_returns_built_in_default_with_built_in_source(repo_root: Path) -> None:
    """A key that was never overridden returns its built-in default + source."""
    result = runner.invoke(app, ["--json", "config", "get", "estimation.eu_minutes"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)["result"]
    assert body == {"key": "estimation.eu_minutes", "value": 30, "source": "built-in"}


def test_get_eu_basis_returns_api_duration_default(repo_root: Path) -> None:
    result = runner.invoke(app, ["--json", "config", "get", "estimation.eu_basis"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)["result"]
    assert body == {"key": "estimation.eu_basis", "value": "api_duration", "source": "built-in"}


def test_validate_rejects_unknown_eu_basis(repo_root: Path) -> None:
    (repo_root / ".ea" / "config.yaml").write_text(
        "estimation:\n  eu_basis: idle_calendar\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["config", "validate"])

    assert result.exit_code == 2, result.output
    assert "eu_basis" in result.output


def test_set_overrides_built_in_via_repo_layer(repo_root: Path) -> None:
    runner.invoke(app, ["config", "set", "estimation.eu_minutes", "60", "--scope", "repo"])
    result = runner.invoke(app, ["--json", "config", "get", "estimation.eu_minutes"])
    body = json.loads(result.output)["result"]
    assert body == {"key": "estimation.eu_minutes", "value": 60, "source": "repo"}


# --- config unset ----------------------------------------------------------


def test_unset_removes_known_repo_leaf_and_prunes_parent(repo_root: Path) -> None:
    config_path = repo_root / ".ea" / "config.yaml"
    config_path.write_text("audit:\n  fix_safe: true\nvcs:\n  auto_commit: false\n")

    result = runner.invoke(
        app,
        ["--json", "config", "unset", "audit.fix_safe", "--scope", "repo"],
        env={"EAWF_DAEMONLESS": "1"},
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["result"]["removed"] is True
    assert yaml.safe_load(config_path.read_text()) == {"vcs": {"auto_commit": False}}


def test_unset_absent_known_leaf_is_noop(repo_root: Path) -> None:
    config_path = repo_root / ".ea" / "config.yaml"
    config_path.write_text("vcs:\n  auto_commit: false\n")
    before = config_path.read_bytes()

    result = runner.invoke(
        app,
        ["--json", "config", "unset", "audit.fix_safe", "--scope", "repo"],
        env={"EAWF_DAEMONLESS": "1"},
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["result"]["removed"] is False
    assert config_path.read_bytes() == before


def test_unset_rejects_unknown_or_locked_leaf(repo_root: Path) -> None:
    unknown = runner.invoke(
        app,
        ["config", "unset", "not.real", "--scope", "repo"],
        env={"EAWF_DAEMONLESS": "1"},
    )
    locked = runner.invoke(
        app,
        ["config", "unset", "schema_version", "--scope", "repo"],
        env={"EAWF_DAEMONLESS": "1"},
    )

    assert unknown.exit_code == 1
    assert "unknown config key" in unknown.output
    assert locked.exit_code == 1
    assert "not writable from the repo" in locked.output


# --- built-in is read-only --------------------------------------------------


def test_set_with_built_in_scope_exits_3(repo_root: Path) -> None:
    result = runner.invoke(app, ["config", "set", "built-in.x", "y", "--scope", "built-in"])
    assert result.exit_code == 1, result.output


def test_set_with_built_in_scope_exits_3_json_envelope(repo_root: Path) -> None:
    result = runner.invoke(
        app,
        ["--json", "config", "set", "built-in.x", "y", "--scope", "built-in"],
    )
    assert result.exit_code == 1
    body = json.loads(result.output)
    assert body["error"] == "UserError"
    assert body["exit_code"] == 1
    assert body["exit_name"] == "USER_ERROR"
    assert "read-only" in body["message"]


def test_set_with_unknown_scope_exits_3(repo_root: Path) -> None:
    result = runner.invoke(app, ["config", "set", "foo", "bar", "--scope", "moonbase"])
    assert result.exit_code == 1


# --- validate ---------------------------------------------------------------


def test_validate_ok_on_clean_repo(repo_root: Path) -> None:
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 0, result.output


def test_validate_ignores_reserved_runtime_env_vars(repo_root: Path) -> None:
    env = dict.fromkeys(layered._RESERVED_ENV_VARS | {"EAWF_SKIP_PERF"}, "1")

    result = runner.invoke(app, ["config", "validate"], env=env)

    assert result.exit_code == 0, result.output


def test_validate_ignores_the_suite_session_tag(repo_root: Path) -> None:
    """The tag the suite exports for its daemons is not a ``suite_session`` config key.

    The reserved set spells the name out, so this pins it to the owning
    constant: renaming one without the other reds here.
    """
    assert SUITE_SESSION_ENV in layered._RESERVED_ENV_VARS

    result = runner.invoke(app, ["config", "validate"], env={SUITE_SESSION_ENV: "tag"})

    assert result.exit_code == 0, result.output


def test_env_overrides_skip_a_gate_files_value_over_the_settings_cap() -> None:
    """A long ``EAWF_GATE_FILES`` value composes into no config key at all.

    The gate runner exports the touched-file list under this name -- easily
    past the 2000-character cap a settings entry's value is typed to -- so
    it is a runtime knob (:data:`layered._RESERVED_ENV_VARS`), not a config
    override. Unreserved, the composed config would carry a ``gate_files``
    key whose value overflows the cap the moment a settings read surfaces it.
    """
    huge = "a" * 2001
    assert "EAWF_GATE_FILES" in layered._RESERVED_ENV_VARS

    overrides = layered._collect_env_overrides({"EAWF_GATE_FILES": huge})

    assert overrides == {}


def test_validate_ignores_a_gate_files_value_over_the_settings_cap(repo_root: Path) -> None:
    result = runner.invoke(app, ["config", "validate"], env={"EAWF_GATE_FILES": "a" * 2001})

    assert result.exit_code == 0, result.output


def test_validate_ok_json_envelope(repo_root: Path) -> None:
    result = runner.invoke(app, ["--json", "config", "validate"])
    assert result.exit_code == 0
    body = json.loads(result.output)["result"]
    assert body == {"ok": True}


def test_validate_exits_4_on_malformed_yaml(repo_root: Path) -> None:
    (repo_root / ".ea" / "config.yaml").write_text(
        "planning:\n  max_parallel_waves: [unclosed\n", encoding="utf-8"
    )
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 2, result.output


def test_validate_exits_4_when_required_section_overwritten_with_scalar(
    repo_root: Path,
) -> None:
    """Schema requires ``planning`` to be a mapping; setting it to a scalar fails."""
    # Bypass the layered helper to plant a hostile override the schema rejects.
    (repo_root / ".ea" / "config.yaml").write_text("planning: not_a_mapping\n", encoding="utf-8")
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 2, result.output


def test_validate_rejects_unknown_verify_waiver_mode(repo_root: Path) -> None:
    (repo_root / ".ea" / "config.yaml").write_text(
        "verify:\n  waiver_mode: Disable\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["config", "validate"])

    assert result.exit_code == 2, result.output
    assert "waiver_mode" in result.output


def test_validate_rejects_non_string_verify_waiver_mode(repo_root: Path) -> None:
    (repo_root / ".ea" / "config.yaml").write_text(
        "verify:\n  waiver_mode: true\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["config", "validate"])

    assert result.exit_code == 2, result.output
    assert "waiver_mode" in result.output


# --- profile enable ---------------------------------------------------------


def test_profile_enable_python_writes_to_repo_layer(repo_root: Path) -> None:
    result = runner.invoke(app, ["config", "profile", "enable", "python"])
    assert result.exit_code == 0, result.output
    contents = yaml.safe_load((repo_root / ".ea" / "config.yaml").read_text(encoding="utf-8"))
    assert "python" in contents["profiles"]["enabled"]


def test_profile_enable_research_refuses_on_a_plain_epoch1_tree(repo_root: Path) -> None:
    """REL-021: materialising state keys writes epoch-1 state, so the flag day refuses it."""
    state_path = repo_root / ".ea" / "state.json"
    state_path.write_bytes(orjson.dumps({"schema_version": "1.0"}))
    before = state_path.read_bytes()

    result = runner.invoke(app, ["--json", "config", "profile", "enable", "research"])
    assert result.exit_code == 4, result.output
    assert json.loads(result.output)["data"]["kind"] == "MigrationRequired"
    assert state_path.read_bytes() == before


def test_profile_enable_unknown_id_exits_3(repo_root: Path) -> None:
    result = runner.invoke(app, ["config", "profile", "enable", "no-such-profile"])
    assert result.exit_code == 1, result.output


def test_profile_enable_built_in_scope_exits_3(repo_root: Path) -> None:
    result = runner.invoke(app, ["config", "profile", "enable", "python", "--scope", "built-in"])
    assert result.exit_code == 1, result.output


def test_profile_enable_idempotent(repo_root: Path) -> None:
    runner.invoke(app, ["config", "profile", "enable", "python"])
    second = runner.invoke(app, ["--json", "config", "profile", "enable", "python"])
    assert second.exit_code == 0, second.output
    body = json.loads(second.output)["result"]
    assert body["already_enabled"] is True


def test_profile_enable_json_envelope(repo_root: Path) -> None:
    result = runner.invoke(app, ["--json", "config", "profile", "enable", "python"])
    assert result.exit_code == 0
    body = json.loads(result.output)["result"]
    assert set(body) == {
        "profile",
        "layer",
        "layer_path",
        "already_enabled",
        "state_keys_required",
        "state_keys_materialised",
        "projections_changed",
    }
    assert body["profile"] == "python"
    assert body["projections_changed"] == []


def test_profile_enable_renders_rule_projections(repo_root: Path) -> None:
    (repo_root / ".ea" / "rules.yaml").write_text(
        "schema_version: 1\nmodules: []\nrules: []\n", encoding="utf-8"
    )
    result = runner.invoke(app, ["--json", "config", "profile", "enable", "python"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)["result"]
    assert body["projections_changed"] == [
        "AGENTS.md",
        "AGENTS.override.md",
        "CLAUDE.md",
        *(carrier_target(role) for role in builtin_carrier_roles()),
        ".gitignore",
    ]
    assert (
        (repo_root / "AGENTS.md")
        .read_text(encoding="utf-8")
        .startswith("<!-- eawf:projection kind=card ")
    )


# --- env-layer override visible via config get ------------------------------


def test_env_var_takes_precedence_over_repo(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner.invoke(app, ["config", "set", "estimation.eu_minutes", "60", "--scope", "repo"])
    monkeypatch.setenv("EAWF_ESTIMATION__EU_MINUTES", "90")
    result = runner.invoke(app, ["--json", "config", "get", "estimation.eu_minutes"])
    body = json.loads(result.output)["result"]
    assert body == {"key": "estimation.eu_minutes", "value": 90, "source": "env"}


# --- malformed-YAML envelope handling (RX-B regression) ---------------------


def test_set_with_malformed_yaml_emits_envelope_text_mode(repo_root: Path) -> None:
    """Text-mode `config set` on malformed YAML must surface a clean envelope.

    Regression: previously the ValidationFailed bubbled up uncaught from
    load_yaml_layer and Typer printed a 200-line traceback at exit 1.
    """
    (repo_root / ".ea" / "config.yaml").write_text(": [bad\n", encoding="utf-8")
    result = runner.invoke(app, ["config", "set", "foo", "bar", "--scope", "repo"])
    # Post C05 § 5.3: VALIDATION_FAILED bucket = VALIDATION_ERROR (2).
    assert result.exit_code == 2, result.output
    assert "Traceback" not in result.output
    assert "Traceback" not in (result.stderr or "")


def test_set_with_malformed_yaml_emits_envelope_json_mode(repo_root: Path) -> None:
    """JSON-mode `config set` on malformed YAML must emit a single envelope."""
    (repo_root / ".ea" / "config.yaml").write_text(": [bad\n", encoding="utf-8")
    result = runner.invoke(
        app,
        ["--json", "config", "set", "foo", "bar", "--scope", "repo"],
    )
    assert result.exit_code == 2, result.output
    assert "Traceback" not in result.output
    body = json.loads(result.output)
    assert body["error"] == "ValidationError"
    assert body["exit_code"] == 2
    assert body["exit_name"] == "VALIDATION_ERROR"


def test_profile_enable_with_malformed_yaml_emits_envelope_json_mode(
    repo_root: Path,
) -> None:
    """`config profile enable` on malformed YAML must emit a clean envelope."""
    (repo_root / ".ea" / "config.yaml").write_text(": [bad\n", encoding="utf-8")
    result = runner.invoke(
        app,
        ["--json", "config", "profile", "enable", "research", "--scope", "repo"],
    )
    assert result.exit_code == 2, result.output
    assert "Traceback" not in result.output
    body = json.loads(result.output)
    assert body["error"] == "ValidationError"
    assert body["exit_code"] == 2
    assert body["exit_name"] == "VALIDATION_ERROR"


# --- validate --composed ----------------------------------------------------


def test_validate_composed_default_enables_core(repo_root: Path) -> None:
    """``--composed`` on a clean repo composes the built-in default (just ``core``)."""
    result = runner.invoke(app, ["--json", "config", "validate", "--composed"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)["result"]
    assert body["ok"] is True
    # Built-in default has profiles.enabled == ["core"].
    assert body["enabled_profiles"] == ["core"]
    assert body["composed"]["name"] == "core"
    # All 14 profile ids surface in the available list (P14-W11 adds a11y;
    # P27-I03-W02 adds the opt-in quality code-craft profile; P27-I03-W03 adds
    # the opt-in agent_driven profile).
    assert len(body["available_profiles"]) == 14


def test_validate_composed_with_three_profiles(repo_root: Path) -> None:
    """``--composed`` with core+python+research yields the merged view.

    ``profile enable`` writes to ``profiles.enabled`` in the repo layer; the
    layered merge replaces the built-in default list wholesale ("ordinary
    lists replace" per the layered-config rules in
    ``docs/architecture/envelope.md``), so the test plants the full enabled
    list directly via the YAML to control the order.
    """
    (repo_root / ".ea" / "config.yaml").write_text(
        "profiles:\n  enabled: [core, python, research]\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["--json", "config", "validate", "--composed"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)["result"]
    assert body["ok"] is True
    assert body["enabled_profiles"] == ["core", "python", "research"]
    assert body["composed"]["name"] == "core+python+research"
    # research populates state_extensions.fields_required.
    assert set(body["composed"]["state_extensions"]["fields_required"]) == {
        "hypotheses",
        "audits",
    }
    # Provenance traces back to the contributing profiles.
    assert body["composed"]["provenance"]["state_extensions"] == ["research"]


def test_validate_composed_deterministic_output(repo_root: Path) -> None:
    """Repeated invocations produce byte-identical JSON envelopes."""
    (repo_root / ".ea" / "config.yaml").write_text(
        "profiles:\n  enabled: [core, python, research]\n",
        encoding="utf-8",
    )

    one = runner.invoke(app, ["--json", "config", "validate", "--composed"])
    two = runner.invoke(app, ["--json", "config", "validate", "--composed"])
    assert one.exit_code == 0
    assert two.exit_code == 0
    assert one.output == two.output


def test_validate_composed_unknown_profile_exits_3(repo_root: Path) -> None:
    """An unknown profile id in ``profiles.enabled`` exits with InvalidInput (3)."""
    (repo_root / ".ea" / "config.yaml").write_text(
        "profiles:\n  enabled: [bogus]\n", encoding="utf-8"
    )
    result = runner.invoke(app, ["--json", "config", "validate", "--composed"])
    assert result.exit_code == 1, result.output
    body = json.loads(result.output)
    assert body["error"] == "UserError"


# --- a write is held to its whole section ------------------------------------

_NAME = "vcs.coauthor.project.name"
_EMAIL = "vcs.coauthor.project.email"
_DAEMONLESS = {"EAWF_DAEMONLESS": "1"}


def test_set_a_coauthor_name_without_its_email_is_refused_with_how_to_set_both(
    repo_root: Path,
) -> None:
    result = runner.invoke(app, ["config", "set", _NAME, "Jane Doe"], env=_DAEMONLESS)

    assert result.exit_code == 2, result.output
    assert f"{_EMAIL}: Field required" in result.output
    assert f"eawf config set {_NAME} <value> --with {_EMAIL}=<value>" in result.output
    assert not (repo_root / ".ea" / "config.yaml").exists()


def test_set_the_pair_at_once_writes_both(repo_root: Path) -> None:
    result = runner.invoke(
        app,
        ["--json", "config", "set", _NAME, "Jane Doe", "--with", f"{_EMAIL}=jane@example.com"],
        env=_DAEMONLESS,
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["result"]["with"] == {_EMAIL: "jane@example.com"}
    written = yaml.safe_load((repo_root / ".ea" / "config.yaml").read_text())
    assert written == {
        "vcs": {"coauthor": {"project": {"name": "Jane Doe", "email": "jane@example.com"}}}
    }


def test_unset_one_leaf_of_the_pair_is_refused_and_both_at_once_is_not(repo_root: Path) -> None:
    config_path = repo_root / ".ea" / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {"vcs": {"coauthor": {"project": {"name": "Jane", "email": "jane@example.com"}}}}
        )
    )

    alone = runner.invoke(app, ["config", "unset", _EMAIL], env=_DAEMONLESS)
    both = runner.invoke(app, ["config", "unset", _EMAIL, "--with", _NAME], env=_DAEMONLESS)

    assert alone.exit_code == 2, alone.output
    assert f"eawf config unset {_EMAIL} --with {_NAME}" in alone.output
    assert both.exit_code == 0, both.output
    assert yaml.safe_load(config_path.read_text()) == {}


def test_set_with_takes_key_equals_value(repo_root: Path) -> None:
    result = runner.invoke(app, ["config", "set", _NAME, "Jane", "--with", _EMAIL], env=_DAEMONLESS)

    assert result.exit_code == 1, result.output
    assert "--with takes KEY=VALUE" in result.output


def test_set_a_daemon_section_refusal_says_how_to_set_both(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eawf.surfaces.cli._daemon_client import DaemonRpcError
    from eawf.surfaces.cli.commands import config as config_cmd

    refusal = f"validation_failed: config_section_invalid: {_EMAIL}: Field required (section vcs)"

    def _refuse(**_kwargs: object) -> None:
        raise DaemonRpcError(-32602, refusal)

    monkeypatch.setattr(config_cmd, "_save_value_to_layer", _refuse)

    result = runner.invoke(app, ["--json", "config", "set", _NAME, "Jane Doe"])

    assert result.exit_code == 2, result.output
    body = json.loads(result.output)
    assert body["message"].startswith(refusal)
    assert f"--with {_EMAIL}=<value>" in body["message"]
