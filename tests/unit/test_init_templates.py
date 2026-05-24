"""Unit tests for the C08 init bootstrap templates (P25-W16).

Per C08 D7 (revised 2026-05-18 per Q24): v0.3 ships exactly three init
templates — ``research``, ``engineering``, ``reverse-engineering``.
``spike`` and ``hybrid`` are deferred to v0.4+.

Per C08 D10: each template encodes ``dispatch.session_policy_default``
matching the profile's evidence-vs-PR character:

- ``research`` → ``continue``
- ``engineering`` → ``fresh``
- ``reverse-engineering`` → ``continue``
"""

from __future__ import annotations

from importlib.resources import files
from typing import Any

import pytest
import yaml

from eawf.profiles.discovery import list_init_templates, load_init_template
from eawf.surfaces.cli.errors import UserError, ValidationError

SHIPPED_TEMPLATES: tuple[str, ...] = (
    "engineering",
    "research",
    "reverse-engineering",
)


def test_list_init_templates_returns_exactly_three_v03_templates() -> None:
    """v0.3 ships exactly the 3 D7-trimmed templates. spike + hybrid deferred."""
    names = list_init_templates()
    assert names == SHIPPED_TEMPLATES, (
        f"v0.3 templates trimmed to 3 per D7 (Q24); got {names!r}. "
        "spike/hybrid belong to v0.4+ — do not ship them yet."
    )


def test_init_templates_do_not_include_spike_or_hybrid() -> None:
    """Explicit guard: spike + hybrid are deferred to v0.4+ (Q24)."""
    names = set(list_init_templates())
    assert "spike" not in names, "spike template deferred to v0.4+ per Q24"
    assert "hybrid" not in names, "hybrid template deferred to v0.4+ per Q24"


def test_init_template_files_count_matches_three() -> None:
    """Raw filesystem check: directory shows exactly 3 YAMLs."""
    data = files("eawf.templates.init")
    yaml_files = sorted(
        entry.name for entry in data.iterdir() if entry.is_file() and entry.name.endswith(".yaml")
    )
    assert yaml_files == [
        "engineering.yaml",
        "research.yaml",
        "reverse-engineering.yaml",
    ], f"expected 3 templates per D7; got {yaml_files}"


@pytest.mark.parametrize("template_name", SHIPPED_TEMPLATES)
def test_init_template_loads_and_validates(template_name: str) -> None:
    """Every shipped template parses cleanly and returns a mapping."""
    payload = load_init_template(template_name)
    assert isinstance(payload, dict)
    assert payload.get("schema_version") == "1.0"
    assert "profiles" in payload
    assert isinstance(payload["profiles"], dict)
    assert isinstance(payload["profiles"].get("enabled"), list)
    assert payload["profiles"]["enabled"], f"{template_name}: profiles.enabled must be non-empty"


def test_research_template_dispatch_session_policy_is_continue() -> None:
    """C08 D10: research → continue (evidence-driven session reuse)."""
    payload = load_init_template("research")
    assert payload["dispatch"]["session_policy_default"] == "continue"


def test_engineering_template_dispatch_session_policy_is_fresh() -> None:
    """C08 D10: engineering → fresh (PR-driven clean slate per wave)."""
    payload = load_init_template("engineering")
    assert payload["dispatch"]["session_policy_default"] == "fresh"


def test_reverse_engineering_template_dispatch_session_policy_is_continue() -> None:
    """C08 D10: reverse-engineering → continue (decompilation context)."""
    payload = load_init_template("reverse-engineering")
    assert payload["dispatch"]["session_policy_default"] == "continue"


def test_research_template_enables_core_and_research_profiles() -> None:
    """Per §5.7.1: research bundle is [core, research]."""
    payload = load_init_template("research")
    assert payload["profiles"]["enabled"] == ["core", "research"]


def test_engineering_template_enables_core_and_python_profiles() -> None:
    """Per §5.7.2: engineering bundle is [core, python]."""
    payload = load_init_template("engineering")
    assert payload["profiles"]["enabled"] == ["core", "python"]


def test_reverse_engineering_template_enables_core_research_re_profiles() -> None:
    """Per §5.7.3: RE bundle is [core, research, re] (re is name-only stub)."""
    payload = load_init_template("reverse-engineering")
    assert payload["profiles"]["enabled"] == ["core", "research", "re"]


def test_engineering_template_acceptance_commands_present() -> None:
    """Engineering template ships the canonical uv-run gauntlet."""
    payload = load_init_template("engineering")
    cmds = payload["acceptance"]["commands"]
    assert cmds["tests"] == "uv run pytest"
    assert cmds["lint"] == "uv run ruff check ."
    assert cmds["typecheck"] == "uv run mypy ."


def test_research_template_planning_max_parallel_waves_is_two() -> None:
    """Research bundles default to max_parallel_waves=2 per §5.7.1."""
    payload = load_init_template("research")
    assert payload["planning"]["max_parallel_waves"] == 2


def test_reverse_engineering_template_max_parallel_waves_is_one() -> None:
    """RE serialises: each decomp builds on prior (max_parallel_waves=1)."""
    payload = load_init_template("reverse-engineering")
    assert payload["planning"]["max_parallel_waves"] == 1


def test_engineering_template_max_parallel_waves_is_four() -> None:
    """Engineering bundles default to max_parallel_waves=4."""
    payload = load_init_template("engineering")
    assert payload["planning"]["max_parallel_waves"] == 4


@pytest.mark.parametrize("template_name", SHIPPED_TEMPLATES)
def test_init_templates_include_project_goals_scaffold(template_name: str) -> None:
    """Every template carries the empty project.goals + success_metrics scaffolding."""
    payload = load_init_template(template_name)
    assert payload["project"]["goals"] == []
    assert payload["project"]["success_metrics"] == {}


def test_load_init_template_rejects_unknown_name() -> None:
    """Unknown template names exit-3 with the canonical 'choose from' message."""
    with pytest.raises(UserError) as exc:
        load_init_template("nonexistent-template")
    assert "unknown init template" in str(exc.value)
    # spike + hybrid are deferred but should still be rejected at the loader
    # surface (the deferral is "do not ship", not "silently allow").
    with pytest.raises(UserError):
        load_init_template("spike")
    with pytest.raises(UserError):
        load_init_template("hybrid")


def test_load_init_template_rejects_malformed_yaml(tmp_path: Any, monkeypatch: Any) -> None:
    """If a future template ships malformed YAML, loader fails fast."""
    # We can't easily mock importlib.resources reads against package data
    # without a real file; this test exercises the YAMLError path by
    # monkeypatching ``yaml.safe_load`` to raise a YAMLError. The discovery
    # module's exception handler catches that and raises ValidationFailed
    # with the expected message shape.
    from eawf.profiles import discovery

    def _boom(_raw: str) -> dict[str, Any]:
        raise yaml.YAMLError("synthetic parse failure")

    monkeypatch.setattr(discovery.yaml, "safe_load", _boom)
    with pytest.raises(ValidationError) as exc:
        load_init_template("research")
    assert "malformed YAML" in str(exc.value)


def test_load_init_template_rejects_non_mapping_top_level(monkeypatch: Any) -> None:
    """A list or scalar at the top level is rejected with ValidationFailed."""
    from eawf.profiles import discovery

    def _return_list(_raw: str) -> list[str]:
        return ["not", "a", "mapping"]

    monkeypatch.setattr(discovery.yaml, "safe_load", _return_list)
    with pytest.raises(ValidationError) as exc:
        load_init_template("research")
    assert "top-level must be a mapping" in str(exc.value)
