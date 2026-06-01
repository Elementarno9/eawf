"""Drift test: the ``/memory`` skill body must match the ``eawf memory`` CLI.

Pins the W05 reconciliation between the skill surface
(:mod:`eawf.workflow.skills.memory` + its typed body
:mod:`eawf.workflow.skills.bodies.memory`) and the operator-facing CLI
(:data:`eawf.surfaces.cli.commands.memory.memory_app`):

- every CLI command the skill routes to (``_VERB_TO_CLI`` leaf verb) is a
  registered ``memory_app`` command;
- the skill's accepted verb set equals the typed ``MemoryBody`` verb literal;
- the emitted ``next_valid_actions`` hints use real CLI flags, not a bare
  positional name that ``eawf memory add`` / ``prune`` would reject.
"""

from __future__ import annotations

import typing
from pathlib import Path

import pytest

from eawf.surfaces.cli.commands.memory import memory_app
from eawf.workflow.skills.bodies.memory import MemoryVerb
from eawf.workflow.skills.engine import SkillContext, run_skill
from eawf.workflow.skills.memory import (
    _VALID_VERBS,
    _VERB_TO_CLI,
    MemorySkill,
    _next_action_for,
)


def _cli_verbs() -> set[str]:
    return {command.name for command in memory_app.registered_commands}


def _ctx() -> SkillContext:
    return SkillContext(
        scope="urn:eawf:v1:state:QR/P00",
        session="urn:eawf:v1:store:QR/sessions/SES-1",
    )


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state_dir = tmp_path / ".ea"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("EA_STATE", str(state_dir / "state.json"))
    monkeypatch.setenv("EA_INSTRUMENT_PROBE", str(state_dir / "instrument-probe.json"))
    return state_dir


def test_skill_routed_cli_verbs_are_real_memory_commands() -> None:
    cli_verbs = _cli_verbs()
    for verb, full_command in _VERB_TO_CLI.items():
        leaf = full_command.split()[-1]
        assert leaf in cli_verbs, (
            f"skill verb {verb!r} routes to {full_command!r} but leaf {leaf!r} "
            f"is not a registered memory CLI command ({sorted(cli_verbs)})"
        )


def test_skill_valid_verbs_match_body_literal() -> None:
    body_verbs = set(typing.get_args(MemoryVerb))
    assert frozenset(body_verbs) == _VALID_VERBS
    assert frozenset(_VERB_TO_CLI) == _VALID_VERBS


def test_next_action_save_uses_title_flag() -> None:
    assert _next_action_for("save", "prefs") == "eawf memory add --title prefs"
    assert _next_action_for("save", None) == "eawf memory add --title <name>"


def test_next_action_forget_uses_scope_filter() -> None:
    # ``eawf memory prune`` is scope/age filtered; no positional entry name.
    assert _next_action_for("forget", "prefs") == "eawf memory prune --scope <scope>"


def test_next_action_list_is_bare_command() -> None:
    assert _next_action_for("list", None) == "eawf memory list"


def test_skill_save_hint_does_not_emit_bare_positional(state_dir: Path) -> None:
    ctx = _ctx()
    ctx.args = {"verb": "save", "name": "prefs"}
    env = run_skill(MemorySkill(), ctx)
    assert env.header.status == "ok"
    actions = env.footer.next_valid_actions
    assert actions == ["eawf memory add --title prefs"]
    # The historical drift: a bare ``eawf memory add prefs`` positional.
    assert "eawf memory add prefs" not in actions


def test_skill_forget_needs_user_hint_uses_scope(state_dir: Path) -> None:
    ctx = _ctx()
    ctx.args = {"verb": "forget"}
    env = run_skill(MemorySkill(), ctx)
    assert env.header.status == "needs_user"
    assert env.footer.next_valid_actions == ["eawf memory prune --scope <scope>"]
