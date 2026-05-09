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
from typing import cast

import orjson
import pytest
from typer.testing import CliRunner

from eawf.cli.app import app
from eawf.render.envelope import OutputEnvelope, SkillName
from eawf.skills import registry
from eawf.skills.engine import ProbeOutcome, Skill, SkillContext, SkillResult


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
    depending on the full W02 implementation.
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

    registry.register(_StubResearchSkill)
    try:
        yield _StubResearchSkill
    finally:
        registry.unregister("/research")


@pytest.fixture
def stub_failing_skill() -> Iterator[type[Skill]]:
    """Register a stub ``/audit`` skill whose action raises.

    The engine catches the exception and returns ``status=failed``; the
    CLI must exit with ``VALIDATION_FAILED`` (4).
    """

    class _StubFailingSkill(Skill):
        name: SkillName = "/audit"

        def probe(self, ctx: SkillContext) -> ProbeOutcome:
            return ProbeOutcome(ok=True)

        def action(self, ctx: SkillContext) -> SkillResult:
            raise RuntimeError("simulated audit failure")

    registry.register(_StubFailingSkill)
    try:
        yield _StubFailingSkill
    finally:
        registry.unregister("/audit")


@pytest.fixture
def stub_needs_user_skill() -> Iterator[type[Skill]]:
    """Register a stub ``/prep`` skill that returns ``needs_user``.

    The CLI must exit with ``USER_DECLINED`` (7).
    """

    class _StubNeedsUserSkill(Skill):
        name: SkillName = "/prep"

        def probe(self, ctx: SkillContext) -> ProbeOutcome:
            return ProbeOutcome(ok=True)

        def action(self, ctx: SkillContext) -> SkillResult:
            return SkillResult(
                status="needs_user",
                body={"questions": []},
            )

    registry.register(_StubNeedsUserSkill)
    try:
        yield _StubNeedsUserSkill
    finally:
        registry.unregister("/prep")


def test_skill_list_shows_all_ten_names_missing_by_default(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(app, ["skill", "list"])
    assert result.exit_code == 0, result.stdout
    expected_names = [
        "/research",
        "/prep",
        "/audit",
        "/ship",
        "/review",
        "/polish",
        "/init",
        "/roadmap",
        "/differentiate",
        "/flow",
    ]
    for name in expected_names:
        assert name in result.stdout, f"missing {name!r} in: {result.stdout}"
    # No registry entries by default → every row is "missing".
    assert "missing" in result.stdout
    assert "installed" not in result.stdout


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
    assert len(skills) == 10
    by_name = {cast(str, s["name"]): s for s in skills}
    research = by_name["/research"]
    assert research["status"] == "installed"
    assert research["body_schema"] == "eawf.skills.bodies.research.ResearchBody"
    # Other skills stay missing.
    assert by_name["/audit"]["status"] == "missing"
    assert by_name["/flow"]["body_schema"] == "eawf.skills.bodies.flow.FlowBody"


def test_skill_run_unknown_skill_returns_invalid_input(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(app, ["skill", "run", "/not-a-skill"], input="")
    # Unknown skill name is rejected at parse-time as InvalidInput (3).
    assert result.exit_code == 3, result.stdout


def test_skill_run_known_but_unregistered_returns_not_found(cli_runner: CliRunner) -> None:
    # /ship is a valid name but no concrete skill is registered.
    registry.unregister("/ship")
    result = cli_runner.invoke(app, ["skill", "run", "/ship"], input="")
    assert result.exit_code == 2, result.stdout


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
        ["--json", "skill", "run", "/audit"],
        input="",
    )
    assert result.exit_code == 4, result.stdout
    env = OutputEnvelope.model_validate_json(result.stdout)
    assert env.header.status == "failed"


def test_skill_run_needs_user_status_exits_seven(
    cli_runner: CliRunner,
    stub_needs_user_skill: type[Skill],
) -> None:
    _ = stub_needs_user_skill
    result = cli_runner.invoke(
        app,
        ["--json", "skill", "run", "/prep"],
        input="",
    )
    assert result.exit_code == 7, result.stdout
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
    assert result.exit_code == 3, result.stdout


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
    assert result.exit_code == 3, result.stdout


def test_skill_run_passes_stdin_args_to_skill_context(cli_runner: CliRunner) -> None:
    """Verify stdin JSON is folded into ``ctx.args`` reachable by the skill."""
    captured: dict[str, dict[str, object]] = {}

    class _CapturingSkill(Skill):
        name: SkillName = "/polish"

        def probe(self, ctx: SkillContext) -> ProbeOutcome:
            return ProbeOutcome(ok=True)

        def action(self, ctx: SkillContext) -> SkillResult:
            captured["args"] = dict(ctx.args)
            return SkillResult(status="ok", body={})

    registry.register(_CapturingSkill)
    try:
        result = cli_runner.invoke(
            app,
            ["--json", "skill", "run", "/polish"],
            input=orjson.dumps({"depth": "quick"}).decode("utf-8"),
        )
        assert result.exit_code == 0, result.stdout
        assert captured["args"] == {"depth": "quick"}
    finally:
        registry.unregister("/polish")


def test_skill_list_help_text_documents_purpose(cli_runner: CliRunner) -> None:
    """The ``skill --help`` surface lists ``list`` and ``run`` subcommands."""
    result = cli_runner.invoke(app, ["skill", "--help"])
    assert result.exit_code == 0
    assert "list" in result.stdout
    assert "run" in result.stdout
