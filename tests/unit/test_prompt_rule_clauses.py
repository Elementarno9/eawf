"""Registry-body lint: no eawf commands in the worktree executor body.

``_EXECUTOR_BODY`` (the only registry body a worktree executor is
dispatched with) carries no eawf-command directive, so the self-close
anti-pattern cannot silently return. All other registry bodies are
allowlisted: they drive operator-side surfaces where eawf commands are
legitimate.
"""

from __future__ import annotations

from eawf.surfaces.render.agents import _EXECUTOR_BODY

#: The banned directives: any of these inside a worktree-facing prompt
#: section teaches the agent the self-close / state-mutation anti-pattern.
_BANNED_EAWF_COMMANDS: tuple[str, ...] = (
    "uv run eawf wave close",
    "uv run eawf state",
)

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


# ---- W43: registry-body token accounting ------------------------------------

#: Pinned per-body count_tokens budgets (spec section-6 table). Growth past
#: these pins must be deliberate: re-pin in the same commit that grows the
#: body.
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
    readiness = "Start only a Task whose spec is ready to implement."
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
