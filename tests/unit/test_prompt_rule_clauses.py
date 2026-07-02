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
