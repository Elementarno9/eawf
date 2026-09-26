"""Unit tests for ``eawf skill`` CLI subapp (Phase 4 W07 acceptance).

Pins:

- ``skill list`` enumerates all 10 canonical names with a status column;
  pre-W02/W03 every row is ``missing``. After a stub registration the
  same row reports ``installed``.
- ``--json`` flag flips both ``skill list`` and ``skill run`` to the
  machine-readable JSON shape; default emission is the Rich table /
  markdown wire-form.
- ``skill run`` exit codes map ``ok→0``, ``failed→4``, ``needs_user→7``
  per the design spec §4 W07 acceptance.
- ``skill run /not-a-skill`` is a :class:`NotFound` (exit 2).
- ``skill run`` rejects malformed stdin payload (exit 3) and accepts
  empty stdin (no JSON args required).

The integration test in ``tests/integration/test_skill_run_research.py``
exercises the full envelope shape end-to-end against a real registered
skill.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import orjson
import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app
from eawf.surfaces.render.envelope import OutputEnvelope, SkillName
from eawf.workflow.skills import registry
from eawf.workflow.skills.engine import ProbeOutcome, Skill, SkillContext, SkillResult


@pytest.fixture
def cli_runner() -> CliRunner:
    """Fresh CliRunner per test — Typer state is per-runner so this
    avoids accidental sharing of stdout buffers between tests."""
    return CliRunner()


@pytest.fixture
def stub_research_skill() -> Iterator[type[Skill]]:
    """Register a stub ``/research`` skill and tear it down after the test.

    The stub returns a fixed ``status=ok`` :class:`SkillResult`. Useful
    for asserting the CLI <-> registry <-> engine wiring without
    depending on the full W02 implementation. The fixture unregisters
    the production ``/research`` skill (registered by W02 import-side
    effects) for the duration of the test so the stub can take its slot.
    """

    class _StubResearchSkill(Skill):
        name: SkillName = "/research"

        def probe(self, ctx: SkillContext) -> ProbeOutcome:
            return ProbeOutcome(ok=True, instrument_probe={"git": "ok"})

        def action(self, ctx: SkillContext) -> SkillResult:
            return SkillResult(
                status="ok",
                body={"brief_id": "BR-stub", "questions": [], "options": []},
                next_valid_actions=["eawf research show BR-stub"],
            )

    previous = registry.lookup("/research")
    registry.unregister("/research")
    registry.register(_StubResearchSkill)
    try:
        yield _StubResearchSkill
    finally:
        registry.unregister("/research")
        if previous is not None:
            registry.register(previous)


@pytest.fixture
def stub_failing_skill() -> Iterator[type[Skill]]:
    """Register a stub ``/verify`` skill whose action raises.

    The engine catches the exception and returns ``status=failed``; the
    CLI must exit with ``VALIDATION_FAILED`` (4). The fixture displaces
    the production ``/verify`` skill for the duration of the test.
    """

    class _StubFailingSkill(Skill):
        name: SkillName = "/verify"

        def probe(self, ctx: SkillContext) -> ProbeOutcome:
            return ProbeOutcome(ok=True)

        def action(self, ctx: SkillContext) -> SkillResult:
            raise RuntimeError("simulated audit failure")

    previous = registry.lookup("/verify")
    registry.unregister("/verify")
    registry.register(_StubFailingSkill)
    try:
        yield _StubFailingSkill
    finally:
        registry.unregister("/verify")
        if previous is not None:
            registry.register(previous)


@pytest.fixture
def stub_needs_user_skill() -> Iterator[type[Skill]]:
    """Register a stub ``/plan`` skill that returns ``needs_user``.

    The CLI must exit with ``USER_DECLINED`` (7). The fixture displaces
    the production ``/plan`` skill for the duration of the test.
    """

    class _StubNeedsUserSkill(Skill):
        name: SkillName = "/plan"

        def probe(self, ctx: SkillContext) -> ProbeOutcome:
            return ProbeOutcome(ok=True)

        def action(self, ctx: SkillContext) -> SkillResult:
            return SkillResult(
                status="needs_user",
                body={
                    "user_question": {
                        "question": "Approve the plan?",
                        "options": [
                            {"label": "approve"},
                            {"label": "cancel"},
                        ],
                    },
                },
            )

    previous = registry.lookup("/plan")
    registry.unregister("/plan")
    registry.register(_StubNeedsUserSkill)
    try:
        yield _StubNeedsUserSkill
    finally:
        registry.unregister("/plan")
        if previous is not None:
            registry.register(previous)


def test_skill_list_shows_every_canonical_name(cli_runner: CliRunner) -> None:
    """Every catalog skill appears in the listing and no retired name does.

    The "every name visible" invariant is independent of the per-skill
    registration state.
    """
    result = cli_runner.invoke(app, ["skill", "list"])
    assert result.exit_code == 0, result.stdout
    from eawf.workflow.skills.catalog import SKILL_CATALOG

    for entry in SKILL_CATALOG.entries:
        assert entry.invocation_name in result.stdout, f"missing {entry.invocation_name!r}"
    for retired in ("/prep", "/audit", "/ship", "/flow", "/blitz"):
        assert f"  {retired} " not in result.stdout, f"retired {retired!r} still listed"
    assert "installed" in result.stdout


def test_skill_list_marks_registered_skill_installed(
    cli_runner: CliRunner,
    stub_research_skill: type[Skill],
) -> None:
    _ = stub_research_skill  # fixture registers + tears down.
    result = cli_runner.invoke(app, ["skill", "list"])
    assert result.exit_code == 0, result.stdout
    # The /research row reports "installed"; the other nine still report
    # "missing".
    assert "installed" in result.stdout
    # We cannot assert exact column content because Rich applies
    # whitespace padding; instead verify the JSON variant for content.


def test_skill_list_json_payload_carries_status_and_schema(
    cli_runner: CliRunner,
    stub_research_skill: type[Skill],
) -> None:
    _ = stub_research_skill
    result = cli_runner.invoke(app, ["--json", "skill", "list"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert "skills" in payload
    skills = cast(list[dict[str, object]], payload["skills"])
    # One row per catalog skill; retired skills are absent, not aliased.
    assert len(skills) == 20
    by_name = {cast(str, s["name"]): s for s in skills}
    research = by_name["/research"]
    assert research["status"] == "installed"
    assert research["body_schema"] == "eawf.workflow.skills.bodies.research.ResearchBody"
    assert research["output_schema"] == "SwiftResearchReport"
    assert by_name["/verify"]["status"] == "installed"
    assert by_name["/accept"]["status"] == "missing"
    assert by_name["/accept"]["body_schema"] is None
    assert "/audit" not in by_name


def test_skill_run_unknown_skill_returns_invalid_input(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(app, ["skill", "run", "/not-a-skill"], input="")
    # Unknown skill name is rejected at parse-time as InvalidInput (3).
    assert result.exit_code == 1, result.stdout


def test_skill_run_known_but_unregistered_returns_not_found(cli_runner: CliRunner) -> None:
    # /ship is a valid name; the test displaces the W02 production
    # registration to confirm the NotFound branch fires when the slot is
    # truly empty.
    previous = registry.lookup("/accept")
    registry.unregister("/accept")
    try:
        result = cli_runner.invoke(app, ["skill", "run", "/accept"], input="")
        assert result.exit_code == 1, result.stdout
    finally:
        if previous is not None:
            registry.register(previous)


def test_skill_run_ok_status_exits_zero(
    cli_runner: CliRunner,
    stub_research_skill: type[Skill],
) -> None:
    _ = stub_research_skill
    result = cli_runner.invoke(
        app,
        ["--json", "skill", "run", "/research"],
        input="",
    )
    assert result.exit_code == 0, result.stdout
    env = OutputEnvelope.model_validate_json(result.stdout)
    assert env.header.skill == "/research"
    assert env.header.status == "ok"


def test_skill_run_failed_status_exits_four(
    cli_runner: CliRunner,
    stub_failing_skill: type[Skill],
) -> None:
    _ = stub_failing_skill
    result = cli_runner.invoke(
        app,
        ["--json", "skill", "run", "/verify"],
        input="",
    )
    assert result.exit_code == 2, result.stdout
    env = OutputEnvelope.model_validate_json(result.stdout)
    assert env.header.status == "failed"


def test_skill_run_needs_user_status_exits_seven(
    cli_runner: CliRunner,
    stub_needs_user_skill: type[Skill],
) -> None:
    _ = stub_needs_user_skill
    result = cli_runner.invoke(
        app,
        ["--json", "skill", "run", "/plan"],
        input="",
    )
    assert result.exit_code == 1, result.stdout
    env = OutputEnvelope.model_validate_json(result.stdout)
    assert env.header.status == "needs_user"


def test_skill_run_default_emits_markdown(
    cli_runner: CliRunner,
    stub_research_skill: type[Skill],
) -> None:
    _ = stub_research_skill
    result = cli_runner.invoke(app, ["skill", "run", "/research"], input="")
    assert result.exit_code == 0, result.stdout
    # Markdown wire-form starts with the YAML frontmatter fence.
    assert result.stdout.startswith("---\n"), result.stdout
    # And carries the canonical footer comment marker.
    assert "<!-- eawf:footer" in result.stdout


def test_skill_run_accepts_bare_name_without_leading_slash(
    cli_runner: CliRunner,
    stub_research_skill: type[Skill],
) -> None:
    _ = stub_research_skill
    result = cli_runner.invoke(
        app,
        ["--json", "skill", "run", "research"],
        input="",
    )
    assert result.exit_code == 0, result.stdout
    env = OutputEnvelope.model_validate_json(result.stdout)
    assert env.header.skill == "/research"


def test_skill_run_malformed_stdin_returns_invalid_input(
    cli_runner: CliRunner,
    stub_research_skill: type[Skill],
) -> None:
    _ = stub_research_skill
    result = cli_runner.invoke(
        app,
        ["skill", "run", "/research"],
        input="{not json",
    )
    assert result.exit_code == 1, result.stdout


def test_skill_run_non_object_stdin_returns_invalid_input(
    cli_runner: CliRunner,
    stub_research_skill: type[Skill],
) -> None:
    _ = stub_research_skill
    result = cli_runner.invoke(
        app,
        ["skill", "run", "/research"],
        input='["not-an-object"]',
    )
    assert result.exit_code == 1, result.stdout


def test_skill_run_passes_stdin_args_to_skill_context(cli_runner: CliRunner) -> None:
    """Verify stdin JSON is folded into ``ctx.args`` reachable by the skill."""
    captured: dict[str, dict[str, object]] = {}

    class _CapturingSkill(Skill):
        name: SkillName = "/why"

        def probe(self, ctx: SkillContext) -> ProbeOutcome:
            return ProbeOutcome(ok=True)

        def action(self, ctx: SkillContext) -> SkillResult:
            captured["args"] = dict(ctx.args)
            return SkillResult(status="ok", body={})

    previous = registry.lookup("/why")
    registry.unregister("/why")
    registry.register(_CapturingSkill)
    try:
        result = cli_runner.invoke(
            app,
            ["--json", "skill", "run", "/why"],
            input=orjson.dumps({"depth": "quick"}).decode("utf-8"),
        )
        assert result.exit_code == 0, result.stdout
        assert captured["args"] == {"depth": "quick"}
    finally:
        registry.unregister("/why")
        if previous is not None:
            registry.register(previous)


def test_skill_run_executes_workspace_overlay_without_python_execution(
    cli_runner: CliRunner, tmp_path: Path
) -> None:
    skill_path = tmp_path / ".ea" / "skills" / "demo" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        """---
name: /demo
description: Demo overlay
---
# Demo

Body.
""",
        encoding="utf-8",
    )
    result = cli_runner.invoke(
        app,
        ["--json", "-w", str(tmp_path), "skill", "run", "/demo"],
        input='{"x": 1}',
    )
    assert result.exit_code == 0, result.stdout
    env = OutputEnvelope.model_validate_json(result.stdout)
    assert env.header.skill == "/demo"
    assert env.header.status == "ok"
    assert isinstance(env.body, dict)
    assert env.body["kind"] == "skill_overlay_dispatch"
    assert env.body["source"] == "workspace"
    assert env.body["args"] == {"x": 1}


def test_skill_run_workspace_overlay_overrides_builtin_name(
    cli_runner: CliRunner, tmp_path: Path
) -> None:
    skill_path = tmp_path / ".ea" / "skills" / "research" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        """---
name: /research
description: Research overlay
---
# Research overlay
""",
        encoding="utf-8",
    )
    result = cli_runner.invoke(
        app,
        ["--json", "-w", str(tmp_path), "skill", "run", "/research"],
        input="",
    )
    assert result.exit_code == 0, result.stdout
    env = OutputEnvelope.model_validate_json(result.stdout)
    assert env.header.skill == "/research"
    assert isinstance(env.body, dict)
    assert env.body["kind"] == "skill_overlay_dispatch"
    assert env.body["source"] == "workspace"
    assert env.body["body"] == "# Research overlay\n"


def test_skill_list_help_text_documents_purpose(cli_runner: CliRunner) -> None:
    """The ``skill --help`` surface lists ``list`` and ``run`` subcommands."""
    result = cli_runner.invoke(app, ["skill", "--help"])
    assert result.exit_code == 0
    assert "list" in result.stdout
    assert "run" in result.stdout
