"""No rendered instruction surface names a model or recommends choosing one.

SURF-164: the steering renderer fails on a model-naming span in any output it
writes; a model named only inside a fenced example is not prose a reader is
told to follow.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from eawf.platform.rules.render import POLICY_TARGET, plan_rule_projections, render_rule_projections
from eawf.platform.rules.render_checks import (
    RuleProjectionModelNamingError,
    model_naming_spans,
    refuse_model_naming,
)


def _repo(tmp_path: Path, description: str) -> Path:
    root = tmp_path / "demo"
    source = root / ".ea" / "rules.yaml"
    source.parent.mkdir(parents=True)
    source.write_text(yaml.safe_dump({"schema_version": 1, "rules": []}), encoding="utf-8")
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "demo"\ndescription = "{description}"\n', encoding="utf-8"
    )
    return root


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


@pytest.mark.parametrize(
    "prose",
    [
        "Run the planner on Claude Opus 5.",
        "Prefer sonnet-4.5 for reviews.",
        "The default is gpt-5.",
        "Try gemini 2.5 pro first.",
        "Hand long proofs to o3-mini.",
        "Use a cheaper model for mechanical edits.",
        "claude-haiku handles triage.",
    ],
)
def test_surf_164_a_model_naming_span_is_found(prose: str) -> None:
    assert model_naming_spans("AGENTS.md", f"# Title\n\n{prose}\n") != ()


def test_surf_164_a_model_named_in_the_brief_fails_the_render(tmp_path: Path) -> None:
    root = _repo(tmp_path, "Built for Claude Opus 5 sessions")
    with pytest.raises(RuleProjectionModelNamingError, match="Claude Opus") as caught:
        render_rule_projections(root)
    assert caught.value.code == "rule_projection_model_naming"
    assert f"{POLICY_TARGET}:" in str(caught.value)
    assert not (root / POLICY_TARGET).exists()


@pytest.mark.parametrize(
    "prose",
    [
        "Validate every YAML input with a closed Pydantic model.",
        "Write a magnum opus of a docstring only when the module earns it.",
        "The model of the domain lives in the kernel.",
        "Use the data model for every boundary.",
    ],
)
def test_surf_164_ordinary_prose_is_not_a_model_name(prose: str) -> None:
    assert model_naming_spans("AGENTS.md", prose) == ()


def test_surf_164_ordinary_prose_renders(tmp_path: Path) -> None:
    root = _repo(tmp_path, "A project whose domain model lives in the kernel")
    assert plan_rule_projections(root).projections


def test_surf_164_a_model_named_only_inside_a_fenced_example_passes() -> None:
    text = "Configure the model as a setting:\n\n```yaml\nmodel: claude-opus-5\n```\n\nDone.\n"
    assert model_naming_spans("AGENTS.md", text) == ()
    refuse_model_naming({"AGENTS.md": text})


def test_surf_164_prose_after_a_closed_fence_is_scanned_again() -> None:
    text = "~~~\ngpt-5\n~~~\nPrefer gpt-5 here.\n"
    assert model_naming_spans("AGENTS.md", text) == ("AGENTS.md:4: gpt-5",)


def test_surf_164_an_unclosed_fence_hides_the_rest_of_the_file() -> None:
    assert model_naming_spans("AGENTS.md", "````\ngpt-5\n```\nclaude opus\n") == ()


def test_surf_164_every_span_is_named_with_its_location() -> None:
    outputs = {"a.md": "fine\nclaude-opus here\n", "b.md": "gpt-4o\n"}
    with pytest.raises(RuleProjectionModelNamingError) as caught:
        refuse_model_naming(outputs)
    assert "a.md:2: claude-opus" in str(caught.value)
    assert "b.md:1: gpt-4o" in str(caught.value)


def test_surf_164_empty_outputs_pass() -> None:
    refuse_model_naming({})
    assert model_naming_spans("a.md", "") == ()
