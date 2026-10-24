"""Golden-output regression tests for the role library.

Two fixture families under ``tests/golden/subagent_spec/``:

- ``roles/<role>.<runtime>.md`` — every role's contract rendered to
  every kept runtime (8 roles x 3 runtimes = 24 fixtures).
- ``role_surface_parity/executor.system_prompt.txt`` — the executor role
  body every kept runtime renders byte for byte.

A failure means a section format or a sourced role body drifted.
Regenerate the fixtures deliberately and commit the new bytes alongside
the change.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.kernel.state.enums import AgentSessionRole
from eawf.workflow.agents.specs.roles import KEPT_RUNTIMES, render_role_contract

_FIXTURE_DIR: Path = Path(__file__).parent / "subagent_spec"
_ROLE_PARITY_FIXTURE: Path = _FIXTURE_DIR / "role_surface_parity" / "executor.system_prompt.txt"


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


def _role_contract_body(runtime: str) -> str:
    rendered = render_role_contract(AgentSessionRole.EXECUTOR, runtime)  # type: ignore[arg-type]
    start = rendered.index("# Executor")
    end = rendered.rindex("\n\nOn completion emit")
    return rendered[start:end]


@pytest.mark.golden
def test_executor_role_body_byte_equal_across_the_kept_runtimes() -> None:
    """One RoleSpec body is byte-identical across the three kept runtimes."""
    expected = _ROLE_PARITY_FIXTURE.read_text(encoding="utf-8").rstrip("\n")
    surfaces = {
        "claude-code": _role_contract_body("claude-code"),
        "codex": _role_contract_body("codex"),
        "opencode": _role_contract_body("opencode"),
    }
    assert surfaces == dict.fromkeys(surfaces, expected)
