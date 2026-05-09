"""Integration test for ``eawf skill run /research --json``.

Phase 4 W02 will land the production ``ResearchSkill`` with the full
probe→action→envelope contract. Until then this integration test
registers a minimal in-test subclass that mimics the W02 surface
faithfully enough to gate the CLI plumbing:

- The probe records two instruments (git ok, gh missing).
- The action returns a valid :class:`ResearchBody` populated with one
  question and one option, and a recommendation.
- The envelope is emitted as JSON (``--json``), parsed back through
  :class:`OutputEnvelope`, and inspected for the expected shape.

The test is marked ``integration`` so it shows up in the
``-m integration`` selector but stays in the default suite (the suite
contains ~40 integration files already).
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from typer.testing import CliRunner

from eawf.cli.app import app
from eawf.render.envelope import OutputEnvelope, SkillName
from eawf.skills import registry
from eawf.skills.engine import ProbeOutcome, Skill, SkillContext, SkillResult


@pytest.fixture
def integration_research_skill() -> Iterator[type[Skill]]:
    """Register a faithful-enough stand-in for the W02 ``/research`` skill.

    The body matches :class:`~eawf.skills.bodies.research.ResearchBody`
    so the envelope round-trips through Pydantic without ``extra``
    rejection. The probe reports both an ``ok`` and a ``missing``
    instrument so the header carries a non-trivial map.
    """

    class _IntegrationResearchSkill(Skill):
        name: SkillName = "/research"

        def probe(self, ctx: SkillContext) -> ProbeOutcome:
            return ProbeOutcome(
                ok=True,
                instrument_probe={"git": "ok", "gh": "missing"},
            )

        def action(self, ctx: SkillContext) -> SkillResult:
            depth = str(ctx.args.get("depth", "deep"))
            return SkillResult(
                status="ok",
                body={
                    "brief_id": "BR-INT-01",
                    "questions": [
                        {
                            "q": "should we ship now?",
                            "answer": f"yes ({depth})",
                            "confidence": "high",
                            "sources": ["urn:eawf:v1:store:audit/AUD-001"],
                        }
                    ],
                    "options": [
                        {
                            "name": "ship",
                            "tradeoffs": "fast",
                            "complexity": "low",
                            "reversibility": "high",
                            "risks": [],
                        }
                    ],
                    "recommendation": {
                        "choice": "ship",
                        "confidence": "high",
                        "fallback": None,
                    },
                    "peer_review": None,
                    "persisted_brief": None,
                },
                next_valid_actions=["eawf hypothesis define --kind core"],
                evidence_refs=["urn:eawf:v1:store:audit/AUD-001"],
            )

    registry.register(_IntegrationResearchSkill)
    try:
        yield _IntegrationResearchSkill
    finally:
        registry.unregister("/research")


@pytest.mark.integration
def test_skill_run_research_emits_well_formed_envelope_json(
    integration_research_skill: type[Skill],
) -> None:
    _ = integration_research_skill
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["--json", "skill", "run", "/research"],
        input='{"depth": "quick"}',
    )
    assert result.exit_code == 0, result.stdout

    # Stdout parses as JSON and validates as an OutputEnvelope.
    payload = json.loads(result.stdout)
    assert set(payload.keys()) == {"header", "body", "footer"}
    env = OutputEnvelope.model_validate(payload)

    # Header carries the skill, status, and instrument probe.
    assert env.header.skill == "/research"
    assert env.header.status == "ok"
    assert env.header.instrument_probe == {"git": "ok", "gh": "missing"}

    # Body has the canonical /research shape.
    assert isinstance(env.body, dict)
    assert env.body["brief_id"] == "BR-INT-01"
    assert env.body["recommendation"]["choice"] == "ship"
    # The depth=quick stdin args propagated into the action.
    assert env.body["questions"][0]["answer"] == "yes (quick)"

    # Footer carries the next-action and evidence pointers.
    assert env.footer.next_valid_actions == [
        "eawf hypothesis define --kind core",
    ]
    assert env.footer.evidence_refs == ["urn:eawf:v1:store:audit/AUD-001"]


@pytest.mark.integration
def test_skill_run_research_plain_emits_markdown_envelope(
    integration_research_skill: type[Skill],
) -> None:
    _ = integration_research_skill
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["skill", "run", "/research"],
        input="",
    )
    assert result.exit_code == 0, result.stdout
    # Default emission is the markdown wire-form: YAML frontmatter +
    # a typed-body comment + the footer comment.
    assert result.stdout.startswith("---\n"), result.stdout
    assert "<!-- eawf:body" in result.stdout
    assert "<!-- eawf:footer" in result.stdout
