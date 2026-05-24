"""Golden-output regression tests for the subagent spec + role library.

Two fixture families under ``tests/golden/subagent_spec/``:

- ``full_wave.md`` — a fully-populated :class:`SubagentSpec` rendered
  prompt that exercises every section (deps incl. a dangling one,
  decisions, hypotheses, audits, references, worktree).
- ``roles/<role>.<runtime>.md`` — every role's contract rendered to
  every kept runtime (8 roles x 3 runtimes = 24 fixtures).

A failure means the spec renderer, a section format, or a sourced role
body drifted. Regenerate the fixtures deliberately and commit the new
bytes alongside the change (see the generation snippet in the wave's
commit body).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.kernel.state.enums import AgentSessionRole
from eawf.workflow.agents.specs.models import (
    SpecAudit,
    SpecDecision,
    SpecDependency,
    SpecHypothesis,
    SpecWorktree,
    SubagentSpec,
)
from eawf.workflow.agents.specs.roles import KEPT_RUNTIMES, render_role_contract

_FIXTURE_DIR: Path = Path(__file__).parent / "subagent_spec"


def _full_spec() -> SubagentSpec:
    """Return the deterministic, fully-populated spec backing ``full_wave.md``."""
    return SubagentSpec(
        wave_id="P27-I03-W14",
        iter_id="P27-I03",
        title="Typed subagent-spec library + roles",
        scope_id="EAWF",
        agent_role="executor",
        effort_bucket="XL",
        success_criteria=[
            "a SubagentSpec model and a role registry exist",
            "a wave dispatch renders from a typed spec rather than an ad-hoc prompt",
        ],
        file_scopes=["src/eawf/agents/specs/**", "src/eawf/dispatch/renderer.py"],
        dependencies=[
            SpecDependency(
                wave_id="P27-I03-W12",
                title="Model-only code-quality skills",
                status="closed",
            ),
            SpecDependency(wave_id="P27-I03-W99"),
        ],
        decisions=[
            SpecDecision(
                decision_id="D12",
                title="v0.3 harness scope: Claude + Codex + OpenCode only",
                rationale="Goose/Aider/Cursor/Cline deferred to v0.4 to lock scope.",
            ),
        ],
        hypotheses=[
            SpecHypothesis(
                hypothesis_id="H27-01",
                metric="render_drift_count",
                confirm="drift == 0",
                reject="drift > 0",
                verdict="confirmed",
            ),
        ],
        recent_audits=[
            SpecAudit(audit_id="A35", kind="evaluation"),
            SpecAudit(audit_id="A30", kind="ship-gate", verdict="minor"),
        ],
        references=[".ea/local/research/2026-05-23-p27-i03-subagent-spec.md"],
        worktree=SpecWorktree(
            branch="feature/eawf-v0.3-p27-w14",
            path=".ea/worktrees/p27-w14",
            base_branch="feature/eawf-v0.3-p27",
        ),
    )


@pytest.mark.golden
def test_full_wave_spec_matches_golden() -> None:
    """The fully-populated spec renders byte-identical to ``full_wave.md``."""
    actual = _full_spec().render().encode("utf-8")
    expected = (_FIXTURE_DIR / "full_wave.md").read_bytes()
    assert actual == expected, (
        "SubagentSpec render drifted from golden fixture full_wave.md. "
        "If intentional, regenerate the fixture and commit the new bytes."
    )


@pytest.mark.golden
def test_full_wave_render_is_deterministic() -> None:
    """Two renders of the same spec produce identical bytes (no hidden state)."""
    assert _full_spec().render() == _full_spec().render()


@pytest.mark.golden
@pytest.mark.parametrize("role", list(AgentSessionRole), ids=lambda r: r.value)
@pytest.mark.parametrize("runtime", list(KEPT_RUNTIMES))
def test_role_contract_matches_golden(role: AgentSessionRole, runtime: str) -> None:
    """Each role x runtime contract renders byte-identical to its fixture."""
    actual = (render_role_contract(role, runtime) + "\n").encode("utf-8")  # type: ignore[arg-type]
    fixture = _FIXTURE_DIR / "roles" / f"{role.value}.{runtime}.md"
    expected = fixture.read_bytes()
    assert actual == expected, (
        f"role contract render drifted from golden fixture {fixture.name!r}. "
        "If intentional, regenerate the fixture and commit the new bytes."
    )
