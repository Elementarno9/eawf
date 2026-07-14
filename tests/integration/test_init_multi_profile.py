"""Integration tests for ``eawf init --profiles`` and ``--template`` (P25-W16).

Covers the C08 D7 surface contract:

- ``--profiles a,b,c`` parses a comma list and writes ``profiles.enabled``
  with the same composition order.
- ``--template <name>`` reads the bundled YAML and deep-merges its keys
  into the canonical ``.ea/config.yaml`` (notably
  ``dispatch.session_policy_default``).
- ``--profile``, ``--profiles``, and ``--template`` are mutually
  exclusive — pass at most one.
- ``--list-templates`` prints the bundled set and exits.

The pipeline also scaffolds materialised state keys from the resolved
profiles (existing wizard behaviour; verified end-to-end here so the
W16 surface is exercised against the live state writer).
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app

runner = CliRunner()


def _invoke(target: Path, *extra: str) -> object:
    """Run ``eawf --no-input init --project-code DEMO --target <tmp>`` plus *extra*."""
    args = ["--no-input", "init", "--project-code", "DEMO", "--target", str(target), *extra]
    return runner.invoke(app, args)


# ---- --profiles comma list -------------------------------------------------


def test_profiles_csv_writes_ordered_profile_list(tmp_path: Path) -> None:
    """``--profiles core,python`` writes ``profiles.enabled: [core, python]``."""
    res = _invoke(tmp_path, "--profiles", "core,python")
    assert res.exit_code == 0, res.stdout

    cfg = yaml.safe_load((tmp_path / ".ea" / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["profiles"]["enabled"] == ["core", "python"]


def test_quick_init_detects_python_and_installs_runtime_plugin(tmp_path: Path) -> None:
    """``eawf init --quick`` skips prompts, detects Python, and installs plugin files."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")

    res = runner.invoke(app, ["init", "--quick", "--target", str(tmp_path)])
    assert res.exit_code == 0, res.stdout

    cfg = yaml.safe_load((tmp_path / ".ea" / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["profiles"]["enabled"] == ["core", "python"]
    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert ".ea/local/" in gitignore
    assert ".claude/" in gitignore
    assert (tmp_path / ".claude" / "skills" / "research" / "SKILL.md").is_file()


def test_profiles_csv_dedupes_and_preserves_order(tmp_path: Path) -> None:
    """Duplicate entries collapse; first-seen order wins."""
    res = _invoke(tmp_path, "--profiles", "python,core,python,core")
    assert res.exit_code == 0, res.stdout

    cfg = yaml.safe_load((tmp_path / ".ea" / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["profiles"]["enabled"] == ["python", "core"]


def test_profiles_csv_tolerates_whitespace(tmp_path: Path) -> None:
    """Whitespace around the commas does not split into bogus tokens."""
    res = _invoke(tmp_path, "--profiles", "core, python")
    assert res.exit_code == 0, res.stdout

    cfg = yaml.safe_load((tmp_path / ".ea" / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["profiles"]["enabled"] == ["core", "python"]


def test_profiles_csv_rejects_empty_entries(tmp_path: Path) -> None:
    """Trailing comma → exit-3 (operator may have stripped a value by accident)."""
    res = _invoke(tmp_path, "--profiles", "core,")
    assert res.exit_code == 1, res.stdout
    assert not (tmp_path / ".ea").exists()


def test_profiles_csv_rejects_unknown_profile(tmp_path: Path) -> None:
    """Same membership gate as --profile: unknown name → exit-3."""
    res = _invoke(tmp_path, "--profiles", "core,bogus")
    assert res.exit_code == 1, res.stdout


# ---- --template surface ----------------------------------------------------


def test_template_research_writes_fresh_session_policy(tmp_path: Path) -> None:
    """``--template research`` writes dispatch.session_policy_default: fresh."""
    res = _invoke(tmp_path, "--template", "research")
    assert res.exit_code == 0, res.stdout

    cfg = yaml.safe_load((tmp_path / ".ea" / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["profiles"]["enabled"] == ["core", "research"]
    assert cfg["dispatch"]["session_policy_default"] == "fresh"
    assert cfg["planning"]["max_parallel_waves"] == 2


def test_template_engineering_writes_fresh_session_policy(tmp_path: Path) -> None:
    """``--template engineering`` writes dispatch.session_policy_default: fresh."""
    res = _invoke(tmp_path, "--template", "engineering")
    assert res.exit_code == 0, res.stdout

    cfg = yaml.safe_load((tmp_path / ".ea" / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["profiles"]["enabled"] == ["core", "python"]
    assert cfg["dispatch"]["session_policy_default"] == "fresh"
    assert cfg["planning"]["max_parallel_waves"] == 4
    assert cfg["acceptance"]["commands"]["tests"] == "uv run pytest"


def test_template_reverse_engineering_writes_fresh_session_policy(tmp_path: Path) -> None:
    """``--template reverse-engineering`` writes session_policy_default: fresh."""
    res = _invoke(tmp_path, "--template", "reverse-engineering")
    assert res.exit_code == 0, res.stdout

    cfg = yaml.safe_load((tmp_path / ".ea" / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["profiles"]["enabled"] == ["core", "research", "re"]
    assert cfg["dispatch"]["session_policy_default"] == "fresh"
    assert cfg["planning"]["max_parallel_waves"] == 1


def test_template_rejects_unknown_name(tmp_path: Path) -> None:
    """``--template spike`` (deferred) fails with exit-3."""
    res = _invoke(tmp_path, "--template", "spike")
    assert res.exit_code == 1, res.stdout
    assert "unknown init template" in res.stdout

    res = _invoke(tmp_path, "--template", "totally-bogus")
    assert res.exit_code == 1, res.stdout


def test_template_materialises_state_keys(tmp_path: Path) -> None:
    """``--template research`` materialises the research profile state keys.

    Verifies success criterion 2: the pipeline scaffolds materialised
    state keys (so the daemon doesn't choke on missing required fields).
    """
    res = runner.invoke(
        app,
        [
            "--json",
            "--no-input",
            "init",
            "--project-code",
            "DEMO",
            "--target",
            str(tmp_path),
            "--template",
            "research",
        ],
    )
    assert res.exit_code == 0, res.stdout

    payload = json.loads(res.stdout)
    # The research profile body declares ``hypotheses`` + ``audits`` as
    # required state extensions (see src/eawf/platform/profiles/data/research.yaml).
    assert "hypotheses" in payload["materialised_state_keys"]
    assert "audits" in payload["materialised_state_keys"]

    state = json.loads((tmp_path / ".ea" / "state.json").read_text(encoding="utf-8"))
    assert "hypotheses" in state
    assert "audits" in state


# ---- mutual exclusion ------------------------------------------------------


def test_profile_and_profiles_mutually_exclusive(tmp_path: Path) -> None:
    """Passing both --profile and --profiles → exit-3."""
    res = _invoke(tmp_path, "--profile", "core", "--profiles", "core,python")
    assert res.exit_code == 1, res.stdout
    assert "mutually exclusive" in res.stdout


def test_profile_and_template_mutually_exclusive(tmp_path: Path) -> None:
    """Passing both --profile and --template → exit-3 (D7)."""
    res = _invoke(tmp_path, "--profile", "core", "--template", "research")
    assert res.exit_code == 1, res.stdout
    assert "mutually exclusive" in res.stdout


def test_profiles_and_template_mutually_exclusive(tmp_path: Path) -> None:
    """Passing both --profiles and --template → exit-3 (D7)."""
    res = _invoke(tmp_path, "--profiles", "core,python", "--template", "research")
    assert res.exit_code == 1, res.stdout
    assert "mutually exclusive" in res.stdout


# ---- --list-templates ------------------------------------------------------


def test_list_templates_prints_three_v03_templates(tmp_path: Path) -> None:
    """``--list-templates`` surfaces the 3 D7-trimmed templates."""
    res = runner.invoke(
        app,
        ["init", "--list-templates", "--target", str(tmp_path)],
    )
    assert res.exit_code == 0, res.stdout
    names = res.stdout.strip().splitlines()
    assert names == ["engineering", "research", "reverse-engineering"], (
        f"v0.3 should list 3 templates per D7; got {names!r}"
    )


def test_list_templates_json_envelope(tmp_path: Path) -> None:
    """``--json --list-templates`` emits a typed envelope."""
    res = runner.invoke(
        app,
        ["--json", "init", "--list-templates", "--target", str(tmp_path)],
    )
    assert res.exit_code == 0, res.stdout
    payload = json.loads(res.stdout)
    assert payload == {
        "templates": ["engineering", "research", "reverse-engineering"],
    }
