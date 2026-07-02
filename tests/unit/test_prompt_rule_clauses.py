"""Rendered-prompt lint: no eawf commands in worktree sections (P30-I23-W39).

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
from eawf.surfaces.render.skills.registry import (
    _AGENT_DISPATCH_BODY,
    _AUDIT_BODY,
    _FLOW_BODY,
    _GOTCHA_DAEMON_RESTART,
    _GOTCHA_DISPATCH_RESUME,
    _GOTCHA_FULL_TREE_GAUNTLET,
    _GOTCHA_NO_EAWF_IN_WORKTREE,
    _GOTCHA_OUT_OF_ORDER,
    _GOTCHA_RECONCILE_FILE_SCOPES,
    _GOTCHA_STATE_BOOKKEEPING,
    _PREP_BODY,
    _SHIP_BODY,
)
from eawf.workflow.agents.specs.models import SubagentSpec

#: The banned directives: any of these inside a worktree-facing prompt
#: section teaches the agent the self-close / state-mutation anti-pattern.
_BANNED_EAWF_COMMANDS: tuple[str, ...] = (
    "uv run eawf wave close",
    "uv run eawf state",
)

#: The worktree-facing prompt sections the composer lint inspects.
_LINTED_SECTIONS: tuple[str, ...] = ("## Workflow", "## Out of scope")

#: The five skill bodies the operator-gotcha clauses are injected into,
#: keyed by their ``SkillSpec.skill_name``.
_SKILL_BODY_BY_NAME: dict[str, str] = {
    "prep": _PREP_BODY,
    "flow": _FLOW_BODY,
    "agent-dispatch": _AGENT_DISPATCH_BODY,
    "ship": _SHIP_BODY,
    "audit": _AUDIT_BODY,
}

#: Each operator-gotcha clause (Roman-numeral id, verbatim clause constant,
#: and the skill bodies that MUST carry it) per the belt-and-braces
#: skill-registry homes in the skills-agents-hardening spec section 3.
_GOTCHA_HOMES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("i", _GOTCHA_DISPATCH_RESUME, ("prep", "flow", "agent-dispatch")),
    ("ii", _GOTCHA_OUT_OF_ORDER, ("prep", "agent-dispatch")),
    ("iii", _GOTCHA_NO_EAWF_IN_WORKTREE, ("prep",)),
    ("iv", _GOTCHA_STATE_BOOKKEEPING, ("prep", "flow", "ship")),
    ("v", _GOTCHA_FULL_TREE_GAUNTLET, ("ship", "audit")),
    ("vi", _GOTCHA_RECONCILE_FILE_SCOPES, ("prep",)),
    ("vii", _GOTCHA_DAEMON_RESTART, ("prep",)),
)

#: Flattened (numeral, clause, skill) rows so each gotcha home is one
#: parametrized case.
_GOTCHA_HOME_CASES: tuple[tuple[str, str, str], ...] = tuple(
    (numeral, clause, skill) for numeral, clause, skills in _GOTCHA_HOMES for skill in skills
)


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


# ---- P30-I23-W42: operator-gotcha clauses land in the skill bodies ----------


@pytest.mark.parametrize(
    ("numeral", "clause", "skill"),
    _GOTCHA_HOME_CASES,
    ids=[f"{numeral}-{skill}" for numeral, _clause, skill in _GOTCHA_HOME_CASES],
)
def test_gotcha_clause_present_in_declared_home(numeral: str, clause: str, skill: str) -> None:
    """Each operator gotcha's verbatim clause lands in every declared body.

    The clause constant is the single source, so a substring check pins
    that the same verbatim text reaches every belt-and-braces home; a
    clause dropped from one body (or edited in one home and not the others)
    fails here.
    """
    body = _SKILL_BODY_BY_NAME[skill]
    assert clause in body, f"gotcha ({numeral}) clause is missing from the {skill!r} skill body"


def test_seven_distinct_gotchas_are_all_homed() -> None:
    """Exactly the seven gotchas are covered, each with at least one home."""
    numerals = [numeral for numeral, _clause, _skills in _GOTCHA_HOMES]
    assert numerals == ["i", "ii", "iii", "iv", "v", "vi", "vii"]
    for numeral, clause, skills in _GOTCHA_HOMES:
        assert clause.strip(), f"gotcha ({numeral}) clause is empty"
        assert skills, f"gotcha ({numeral}) names no home body"


def test_daemon_restart_gotcha_is_a_prep_preflight_row() -> None:
    """Gotcha (vii) is a NEW row in the ``/prep`` pre-flight checklist.

    CR-01 pins the daemon-restart rule to the pre-flight checklist
    specifically (not merely somewhere in the body), so it is asserted
    against the extracted section, and as a checklist ``- [ ]`` row.
    """
    preflight = _section(_PREP_BODY, "## Pre-flight checklist")
    assert _GOTCHA_DAEMON_RESTART in preflight
    rows = [line for line in preflight.splitlines() if line.lstrip().startswith("- [ ]")]
    assert any("daemon is restarted" in row for row in rows)


def test_command_bearing_gotchas_absent_from_executor_body() -> None:
    """The command-bearing gotchas stay out of the worktree executor body.

    A worktree executor runs no eawf command (gotcha iii), so the
    operator-side clauses that name ``eawf`` commands must not leak into
    ``_EXECUTOR_BODY`` -- that would re-teach the self-mutation
    anti-pattern the composer lint above guards against.
    """
    for clause in (
        _GOTCHA_DISPATCH_RESUME,
        _GOTCHA_OUT_OF_ORDER,
        _GOTCHA_STATE_BOOKKEEPING,
        _GOTCHA_RECONCILE_FILE_SCOPES,
        _GOTCHA_DAEMON_RESTART,
    ):
        assert clause not in _EXECUTOR_BODY


# ---- W43: registry-body token accounting ------------------------------------

#: Pinned per-body count_tokens budgets (spec section-6 table). The
#: role-tier cap cannot police registry bodies — _enforce_role_tier_budget
#: measures only injected profile blocks — so growth past these pins must
#: be deliberate: re-pin in the same commit that grows the body.
_BODY_TOKEN_BUDGETS = {
    "_RESEARCHER_BODY": 320,
    "_PLANNER_BODY": 430,
    # Headroom for W49's evidence_refs DoD bullet.
    "_EXECUTOR_BODY": 500,
    "_AUDITOR_BODY": 330,
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
