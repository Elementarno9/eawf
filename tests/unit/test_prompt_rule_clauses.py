"""Rendered-prompt lint: no eawf commands in worktree sections.

Two-scope lint pinning the P30-I23-W38 rewrite so the self-close
anti-pattern cannot silently return:

* **composer scope** — an executor-role fixture prompt is rendered on
  BOTH the interactive and headless shapes and its ``## Workflow`` and
  ``## Out of scope`` sections must be free of ``uv run eawf wave
  close`` and ``uv run eawf state`` (a worktree agent never runs an
  eawf command; the parent or the daemon owns the close);
* **registry scope** — ``_EXECUTOR_BODY`` (the only registry body a
  worktree executor is dispatched with) carries no eawf-command
  directive. All other registry bodies are allowlisted: they drive
  operator-side surfaces where eawf commands are legitimate.

A self-test feeds a deliberately-broken composer string through the
lint and asserts it FAILS — proving the lint can reject a broken
artifact rather than vacuously passing.
"""

from __future__ import annotations

import pytest

from eawf.surfaces.render.agents import _EXECUTOR_BODY
from eawf.workflow.agents.specs.models import SubagentSpec

#: The banned directives: any of these inside a worktree-facing prompt
#: section teaches the agent the self-close / state-mutation anti-pattern.
_BANNED_EAWF_COMMANDS: tuple[str, ...] = (
    "uv run eawf wave close",
    "uv run eawf state",
)

#: The worktree-facing prompt sections the composer lint inspects.
_LINTED_SECTIONS: tuple[str, ...] = ("## Workflow", "## Out of scope")


def _section(rendered: str, heading: str) -> str:
    """Return *heading*'s section body from a rendered prompt.

    The section runs from *heading* to the next ``## `` heading (or the
    prompt's end). Raises when the heading is absent so a renamed
    section fails the lint loudly instead of vacuously passing.
    """
    if heading not in rendered:
        raise AssertionError(f"rendered prompt is missing the {heading!r} section")
    body = rendered.split(heading, 1)[1]
    lines: list[str] = []
    for line in body.splitlines()[1:]:
        if line.startswith("## "):
            break
        lines.append(line)
    return "\n".join(lines)


def lint_prompt_sections(rendered: str) -> list[str]:
    """Return the banned-command violations in a rendered prompt.

    One violation string per (section, banned command) hit, empty when
    the prompt is clean. Exposed as a helper (not just inline asserts)
    so the self-test can prove the lint rejects a broken artifact.
    """
    violations: list[str] = []
    for heading in _LINTED_SECTIONS:
        section_body = _section(rendered, heading)
        for banned in _BANNED_EAWF_COMMANDS:
            if banned in section_body:
                violations.append(f"{heading} contains {banned!r}")
    return violations


def _executor_spec() -> SubagentSpec:
    return SubagentSpec.model_validate(
        {
            "wave_id": "P01-I01-W01",
            "iter_id": "P01-I01",
            "title": "Executor fixture wave",
            "scope_id": "QR",
            "agent_role": "executor",
            "file_scopes": ["src/"],
        }
    )


# ---- composer scope ---------------------------------------------------------


@pytest.mark.parametrize("headless", [False, True], ids=["interactive", "headless"])
def test_composer_prompt_sections_free_of_eawf_commands(headless: bool) -> None:
    """Both render shapes keep the worktree sections free of eawf commands."""
    rendered = _executor_spec().render(headless=headless)
    assert lint_prompt_sections(rendered) == []


# ---- registry scope ---------------------------------------------------------


def test_registry_executor_body_free_of_eawf_commands() -> None:
    """The executor registry body carries no eawf-command directive.

    Only ``_EXECUTOR_BODY`` is linted: the other registry bodies
    (auditor / planner / operator / researcher) drive operator-side
    surfaces where eawf commands are legitimate, so they stay
    allowlisted by omission.
    """
    for banned in _BANNED_EAWF_COMMANDS:
        assert banned not in _EXECUTOR_BODY, f"_EXECUTOR_BODY contains {banned!r}"


# ---- self-test: the lint rejects a broken artifact ---------------------------


def test_lint_rejects_deliberately_broken_composer_prompt() -> None:
    """A prompt smuggling a self-close instruction fails the lint."""
    broken = (
        "# Wave P01-I01-W01: broken fixture\n\n"
        "## Workflow\n\n"
        "1. Do the work.\n"
        "2. Close it yourself: `uv run eawf wave close P01-I01-W01`.\n\n"
        "## Out of scope\n\n"
        "- Mutate state via `uv run eawf state set ...` when needed.\n"
    )
    violations = lint_prompt_sections(broken)
    assert violations == [
        "## Workflow contains 'uv run eawf wave close'",
        "## Out of scope contains 'uv run eawf state'",
    ]


def test_lint_raises_on_missing_section() -> None:
    """A prompt missing a linted section fails loudly, never vacuously."""
    with pytest.raises(AssertionError, match="missing the '## Workflow' section"):
        lint_prompt_sections("# Wave X\n\n## Out of scope\n\n- nothing\n")


# ---- W43: registry-body token accounting ------------------------------------

#: Pinned per-body count_tokens budgets (spec section-6 table). The
#: role-tier cap cannot police registry bodies — _enforce_role_tier_budget
#: measures only injected profile blocks — so growth past these pins must
#: be deliberate: re-pin in the same commit that grows the body.
_BODY_TOKEN_BUDGETS = {
    # Role-bound obligations live in the builtin role rules, delivered by
    # the role carrier, so these bodies keep only method and output shape.
    "_RESEARCHER_BODY": 200,
    "_PLANNER_BODY": 300,
    "_EXECUTOR_BODY": 260,
    "_AUDITOR_BODY": 220,
    "_OPERATOR_BODY": 360,
}


def test_enlarged_agent_bodies_stay_within_pinned_token_budgets() -> None:
    """CR-02: registry-body growth is deliberate and reviewed."""
    import eawf.surfaces.render.agents as agents_module
    from eawf.platform.lint.tools.agents_md_budget import count_tokens

    for body_name, budget in _BODY_TOKEN_BUDGETS.items():
        body = getattr(agents_module, body_name)
        weight = count_tokens(body)
        assert weight <= budget, (
            f"{body_name} weighs {weight} tokens, over its pinned budget "
            f"{budget}; growth must be deliberate — re-pin with rationale"
        )
        # The pin is honest: a body that shrank far below its budget means
        # the pin no longer documents real weight; keep them within 2x.
        assert weight * 2 >= budget, f"{body_name} budget {budget} is stale vs {weight}"


# ---- W48: new clauses verified on RENDERED surfaces, not registry constants -


def test_new_clauses_land_on_all_four_rendered_surfaces() -> None:
    """CR-02: the W38-W49 clause families appear in a rendered SKILL.md, the
    rendered claude executor agent, the rendered codex executor toml, and a
    rendered dispatch prompt — the committed golden trees ARE the rendered
    artifacts, so registry-constant drift cannot masquerade as delivery."""
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    golden = repo / "tests" / "golden"

    # /prep is retired from the shipped skill bundle; the dispatch-discipline
    # clause now reaches the operator through the rendered operator agent.
    operator_agent = (golden / "plugin_install" / "claude" / "agents" / "operator.md").read_text(
        encoding="utf-8"
    )
    assert "dispatch is paused" in operator_agent
    assert not (golden / "plugin_install" / "claude" / "skills" / "prep").exists()

    # The executor's readiness and evidence obligations are embedded in the
    # Claude agent and the Codex agent alike.
    readiness = "Start only a wave whose spec is ready to implement."
    claude_agent = (golden / "plugin_install" / "claude" / "agents" / "executor.md").read_text(
        encoding="utf-8"
    )
    assert readiness in claude_agent
    assert "skills:" not in claude_agent.split("\n---\n", 1)[0]

    codex_toml = (golden / "plugin_install" / "codex" / "agents" / "executor.toml").read_text(
        encoding="utf-8"
    )
    assert readiness in codex_toml

    dispatch_prompt = (golden / "dispatch" / "cc_prep.txt").read_text(encoding="utf-8")
    assert "## Role contract" in dispatch_prompt
