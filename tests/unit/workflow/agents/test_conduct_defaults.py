"""The report cap, Task marker and conduct defaults every rendered prompt carries."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

from eawf.runtime.runtimes.claude.plugin_install import _render_agent
from eawf.runtime.runtimes.codex.plugin_install import _render_agent_toml
from eawf.runtime.runtimes.opencode.plugin_install import _render_opencode_agent_md
from eawf.surfaces.render.agents import AGENT_REGISTRY, AgentSpec
from eawf.workflow.agents.specs.roles import KEPT_RUNTIMES, ROLE_REGISTRY
from eawf.workflow.skills.bodies.chassis import shipped_skill_page
from eawf.workflow.skills.catalog import resolve_skill
from eawf.workflow.skills.engine import ActionRun, SkillContext
from eawf.workflow.skills.research import ResearchSkill

CAP_LINE = "Report cap: 5000 characters."
MARKER = "`[eawf task=<KEY>]`"
CONDUCT_LINE = "Continue without mid-task pauses."
PARALLEL = "separate subagents in one message"


def _agent_texts(spec: AgentSpec) -> dict[str, str]:
    """Return every runtime's rendered definition of *spec*, keyed by surface."""
    return {
        "claude": _render_agent(spec, {}),
        "codex": tomllib.loads(_render_agent_toml(spec))["developer_instructions"],
        "opencode": _render_opencode_agent_md(spec),
    }


def _skill(skill_id: str) -> str:
    return shipped_skill_page(resolve_skill(skill_id))


def _rounds(tmp_path: Path, **args: Any) -> int:
    ctx = SkillContext(scope="scope", session="session", args=dict(args))
    run = ActionRun(ctx=ctx, state_path=tmp_path / "state.json", scope_id="scope", args=args)
    return ResearchSkill._resolve_rounds(run)


@pytest.mark.parametrize("spec", AGENT_REGISTRY, ids=lambda spec: spec.role)
def test_report_cap_and_marker(spec: AgentSpec) -> None:
    for surface, text in _agent_texts(spec).items():
        assert CAP_LINE in text.splitlines(), surface
        assert MARKER in text, surface
    for role_spec in ROLE_REGISTRY.values():
        for runtime in KEPT_RUNTIMES:
            rendered = role_spec.render(runtime)
            assert CAP_LINE in rendered.splitlines(), (role_spec.role, runtime)
            assert MARKER in rendered, (role_spec.role, runtime)
    template = _skill("dispatch")
    assert "a first line [eawf task=<KEY>]" in template
    assert f"`{CAP_LINE}`" in template


def test_report_cap_and_marker_every_role_is_covered() -> None:
    assert {spec.role for spec in AGENT_REGISTRY} == {r.value for r in ROLE_REGISTRY}


def test_research_defaults(tmp_path: Path) -> None:
    assert _rounds(tmp_path) == 3
    page = _skill("research")
    assert "With neither `--rounds` nor `--depth`, the mode is rounds mode with at most 3" in page
    assert "a concrete preview on every option" in page


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ({}, 3),
        ({"depth": "deep"}, 1),
        ({"rounds": 1}, 1),
        ({"rounds": "5"}, 5),
        ({"rounds": 2, "depth": "shallow"}, 2),
    ],
    ids=["no-flags", "depth-only", "single-round", "max-grammar", "both-flags"],
)
def test_research_defaults_boundaries(tmp_path: Path, args: dict[str, Any], expected: int) -> None:
    assert _rounds(tmp_path, **args) == expected


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ({"rounds": 0}, 3),
        ({"rounds": -1}, 3),
        ({"rounds": "many"}, 3),
        ({"rounds": True}, 3),
        ({"rounds": None}, 3),
        ({"rounds": 0, "depth": "deep"}, 1),
    ],
    ids=["zero", "negative", "non-numeric", "bool", "none", "zero-with-depth"],
)
def test_research_defaults_malformed_rounds_degrade(
    tmp_path: Path, args: dict[str, Any], expected: int
) -> None:
    assert _rounds(tmp_path, **args) == expected


@pytest.mark.parametrize("skill_id", ["research", "plan", "dispatch"])
def test_conduct_lines(skill_id: str) -> None:
    page = _skill(skill_id)
    assert CONDUCT_LINE in page
    assert PARALLEL in page


def test_conduct_lines_executor_agent() -> None:
    executor = next(spec for spec in AGENT_REGISTRY if spec.role == "executor")
    for surface, text in _agent_texts(executor).items():
        assert CONDUCT_LINE in text, surface
        assert PARALLEL in text, surface
