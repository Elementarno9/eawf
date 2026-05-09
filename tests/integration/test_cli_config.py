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
fixture redirects :func:`eawf.config.layered.global_config_path` to a per-test
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

from eawf.cli.app import app
from eawf.config import layered

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
    body = json.loads(get_result.output)
    assert body == {"key": "foo.bar", "value": 42, "source": "local"}


def test_set_to_repo_writes_to_repo_layer(repo_root: Path) -> None:
    result = runner.invoke(app, ["config", "set", "planning.approval", "auto", "--scope", "repo"])
    assert result.exit_code == 0, result.output
    contents = (repo_root / ".ea" / "config.yaml").read_text(encoding="utf-8")
    parsed = yaml.safe_load(contents)
    assert parsed["planning"]["approval"] == "auto"


def test_set_to_local_writes_to_local_layer(repo_root: Path) -> None:
    runner.invoke(app, ["config", "set", "foo.bar", "1", "--scope", "local"])
    target = repo_root / ".ea" / "local" / "config.yaml"
    assert target.exists()


def test_get_unknown_key_returns_exit_code_2(repo_root: Path) -> None:
    result = runner.invoke(app, ["config", "get", "no.such.key"])
    assert result.exit_code == 2


def test_get_returns_built_in_default_with_built_in_source(repo_root: Path) -> None:
    """A key that was never overridden returns its built-in default + source."""
    result = runner.invoke(app, ["--json", "config", "get", "estimation.eu_minutes"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body == {"key": "estimation.eu_minutes", "value": 30, "source": "built-in"}


def test_set_overrides_built_in_via_repo_layer(repo_root: Path) -> None:
    runner.invoke(app, ["config", "set", "estimation.eu_minutes", "60", "--scope", "repo"])
    result = runner.invoke(app, ["--json", "config", "get", "estimation.eu_minutes"])
    body = json.loads(result.output)
    assert body == {"key": "estimation.eu_minutes", "value": 60, "source": "repo"}


# --- built-in is read-only --------------------------------------------------


def test_set_with_built_in_scope_exits_3(repo_root: Path) -> None:
    result = runner.invoke(app, ["config", "set", "built-in.x", "y", "--scope", "built-in"])
    assert result.exit_code == 3, result.output


def test_set_with_built_in_scope_exits_3_json_envelope(repo_root: Path) -> None:
    result = runner.invoke(
        app,
        ["--json", "config", "set", "built-in.x", "y", "--scope", "built-in"],
    )
    assert result.exit_code == 3
    body = json.loads(result.output)
    assert body["error"] == "InvalidInput"
    assert body["exit_code"] == 3
    assert body["exit_name"] == "INVALID_INPUT"
    assert "read-only" in body["message"]


def test_set_with_unknown_scope_exits_3(repo_root: Path) -> None:
    result = runner.invoke(app, ["config", "set", "foo", "bar", "--scope", "moonbase"])
    assert result.exit_code == 3


# --- validate ---------------------------------------------------------------


def test_validate_ok_on_clean_repo(repo_root: Path) -> None:
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 0, result.output


def test_validate_ok_json_envelope(repo_root: Path) -> None:
    result = runner.invoke(app, ["--json", "config", "validate"])
    assert result.exit_code == 0
    body = json.loads(result.output)
    assert body == {"ok": True, "scope": None}


def test_validate_exits_4_on_malformed_yaml(repo_root: Path) -> None:
    (repo_root / ".ea" / "config.yaml").write_text(
        "planning:\n  approval: [unclosed\n", encoding="utf-8"
    )
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 4, result.output


def test_validate_exits_4_when_required_section_overwritten_with_scalar(
    repo_root: Path,
) -> None:
    """Schema requires ``planning`` to be a mapping; setting it to a scalar fails."""
    # Bypass the layered helper to plant a hostile override the schema rejects.
    (repo_root / ".ea" / "config.yaml").write_text("planning: not_a_mapping\n", encoding="utf-8")
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 4, result.output


# --- profile enable ---------------------------------------------------------


def test_profile_enable_python_writes_to_repo_layer(repo_root: Path) -> None:
    result = runner.invoke(app, ["config", "profile", "enable", "python"])
    assert result.exit_code == 0, result.output
    contents = yaml.safe_load((repo_root / ".ea" / "config.yaml").read_text(encoding="utf-8"))
    assert "python" in contents["profiles"]["enabled"]


def test_profile_enable_research_materialises_state_keys(repo_root: Path) -> None:
    state_path = repo_root / ".ea" / "state.json"
    state_path.write_bytes(orjson.dumps({"schema_version": "1.0"}))

    result = runner.invoke(app, ["--json", "config", "profile", "enable", "research"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["profile"] == "research"
    assert body["layer"] == "repo"
    assert set(body["state_keys_materialised"]) == {"hypotheses", "audits"}

    state_body = json.loads(state_path.read_text(encoding="utf-8"))
    assert state_body["hypotheses"] == {}
    assert state_body["audits"] == {}


def test_profile_enable_unknown_id_exits_3(repo_root: Path) -> None:
    result = runner.invoke(app, ["config", "profile", "enable", "no-such-profile"])
    assert result.exit_code == 3, result.output


def test_profile_enable_built_in_scope_exits_3(repo_root: Path) -> None:
    result = runner.invoke(app, ["config", "profile", "enable", "python", "--scope", "built-in"])
    assert result.exit_code == 3, result.output


def test_profile_enable_idempotent(repo_root: Path) -> None:
    runner.invoke(app, ["config", "profile", "enable", "python"])
    second = runner.invoke(app, ["--json", "config", "profile", "enable", "python"])
    assert second.exit_code == 0, second.output
    body = json.loads(second.output)
    assert body["already_enabled"] is True


def test_profile_enable_json_envelope(repo_root: Path) -> None:
    result = runner.invoke(app, ["--json", "config", "profile", "enable", "python"])
    assert result.exit_code == 0
    body = json.loads(result.output)
    assert set(body) == {
        "profile",
        "layer",
        "layer_path",
        "already_enabled",
        "state_keys_materialised",
    }
    assert body["profile"] == "python"


# --- env-layer override visible via config get ------------------------------


def test_env_var_takes_precedence_over_repo(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner.invoke(app, ["config", "set", "estimation.eu_minutes", "60", "--scope", "repo"])
    monkeypatch.setenv("EAWF_ESTIMATION__EU_MINUTES", "90")
    result = runner.invoke(app, ["--json", "config", "get", "estimation.eu_minutes"])
    body = json.loads(result.output)
    assert body == {"key": "estimation.eu_minutes", "value": 90, "source": "env"}


# --- malformed-YAML envelope handling (RX-B regression) ---------------------


def test_set_with_malformed_yaml_emits_envelope_text_mode(repo_root: Path) -> None:
    """Text-mode `config set` on malformed YAML must surface a clean envelope.

    Regression: previously the ValidationFailed bubbled up uncaught from
    load_yaml_layer and Typer printed a 200-line traceback at exit 1.
    """
    (repo_root / ".ea" / "config.yaml").write_text(": [bad\n", encoding="utf-8")
    result = runner.invoke(app, ["config", "set", "foo", "bar", "--scope", "repo"])
    assert result.exit_code == 4, result.output
    assert "Traceback" not in result.output
    assert "Traceback" not in (result.stderr or "")


def test_set_with_malformed_yaml_emits_envelope_json_mode(repo_root: Path) -> None:
    """JSON-mode `config set` on malformed YAML must emit a single envelope."""
    (repo_root / ".ea" / "config.yaml").write_text(": [bad\n", encoding="utf-8")
    result = runner.invoke(
        app,
        ["--json", "config", "set", "foo", "bar", "--scope", "repo"],
    )
    assert result.exit_code == 4, result.output
    assert "Traceback" not in result.output
    body = json.loads(result.output)
    assert body["error"] == "ValidationFailed"
    assert body["exit_code"] == 4
    assert body["exit_name"] == "VALIDATION_FAILED"


def test_profile_enable_with_malformed_yaml_emits_envelope_json_mode(
    repo_root: Path,
) -> None:
    """`config profile enable` on malformed YAML must emit a clean envelope."""
    (repo_root / ".ea" / "config.yaml").write_text(": [bad\n", encoding="utf-8")
    result = runner.invoke(
        app,
        ["--json", "config", "profile", "enable", "research", "--scope", "repo"],
    )
    assert result.exit_code == 4, result.output
    assert "Traceback" not in result.output
    body = json.loads(result.output)
    assert body["error"] == "ValidationFailed"
    assert body["exit_code"] == 4
    assert body["exit_name"] == "VALIDATION_FAILED"


# --- validate --composed ----------------------------------------------------


def test_validate_composed_default_enables_core(repo_root: Path) -> None:
    """``--composed`` on a clean repo composes the built-in default (just ``core``)."""
    result = runner.invoke(app, ["--json", "config", "validate", "--composed"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["ok"] is True
    # Built-in default has profiles.enabled == ["core"].
    assert body["enabled_profiles"] == ["core"]
    assert body["composed"]["name"] == "core"
    # All 11 profile ids surface in the available list.
    assert len(body["available_profiles"]) == 11


def test_validate_composed_with_three_profiles(repo_root: Path) -> None:
    """``--composed`` with core+python+research yields the merged view.

    ``profile enable`` writes to ``profiles.enabled`` in the repo layer; the
    layered merge replaces the built-in default list wholesale (per
    ea-proposal §"Settings schema": "ordinary lists replace"), so the test
    plants the full enabled list directly via the YAML to control the order.
    """
    (repo_root / ".ea" / "config.yaml").write_text(
        "profiles:\n  enabled: [core, python, research]\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["--json", "config", "validate", "--composed"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
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
    assert result.exit_code == 3, result.output
    body = json.loads(result.output)
    assert body["error"] == "InvalidInput"
