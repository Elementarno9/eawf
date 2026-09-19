"""A native launcher refuses a deny it cannot express, rather than widening.

``CompiledRunSpec.tool_policy`` carries semantic tool ids; the spawn path's
``denied_tools`` carries ambient names from :data:`TOOL_UNIVERSE`. The two
vocabularies do not overlap, and the codex lane expresses a deny as its
inverted allowlist, so forwarding a semantic id subtracts nothing and grants
the child the whole universe -- strictly more authority than an empty
deny-list, which emits no override at all.

The widening mechanism is pinned here alongside the refusal, so a future
change that re-enables the forward reds on the mechanism rather than on the
guard's message.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.config.providers import ProviderConfigError
from eawf.kernel.runtime.compiled import CompiledRunSpec
from eawf.runtime.runtimes.adapter import RuntimeSpawnError, ambient_denied_tools
from eawf.runtime.sandbox.policy import TOOL_UNIVERSE, invert_deny_to_allow
from eawf.workflow.runtime.compile import compile_run_spec
from tests import _provider_helpers as fx

pytestmark = pytest.mark.unit

#: A real semantic tool id, which is what a merged tool policy holds.
SEMANTIC_DENIAL = "run_scoped_command"

#: A real ambient tool name, which is what ``denied_tools`` holds.
AMBIENT_DENIAL = "Bash"


def _spec(*, deny: list[str]) -> CompiledRunSpec:
    """Compile a spec whose merged tool policy denies *deny*."""
    profile = fx.profile_document(
        tool_policy={
            "allow": ["repo_read", "repo_search", "workspace_apply_patch", "submit_report"],
            "deny": deny,
        }
    )
    return compile_run_spec(
        fx.task_request(),
        configuration=fx.configuration(profiles=[profile]),
        bindings=[fx.binding()],
        compiled_at=fx.COMPILED_AT,
    )


# ---- the widening this guard exists to stop --------------------------------


def test_semantic_denial_shares_no_name_with_the_ambient_universe() -> None:
    """The two vocabularies are disjoint, which is why a forward cannot work."""
    assert SEMANTIC_DENIAL not in TOOL_UNIVERSE
    assert AMBIENT_DENIAL in TOOL_UNIVERSE


def test_inverting_a_semantic_denial_would_grant_the_whole_universe() -> None:
    """Forwarding a semantic id grants every ambient tool instead of denying one."""
    assert invert_deny_to_allow([SEMANTIC_DENIAL]) == sorted(TOOL_UNIVERSE)


def test_inverting_an_ambient_denial_withholds_exactly_that_tool() -> None:
    """The same helper is correct for the vocabulary it was written for."""
    allowed = invert_deny_to_allow([AMBIENT_DENIAL])
    assert AMBIENT_DENIAL not in allowed
    assert len(allowed) == len(TOOL_UNIVERSE) - 1


# ---- the guard ------------------------------------------------------------


def test_ambient_denied_tools_refuses_a_semantic_denial() -> None:
    """A denial naming no ambient tool is refused before any spawn."""
    spec = _spec(deny=[SEMANTIC_DENIAL])
    with pytest.raises(RuntimeSpawnError, match="tool_vocabulary_mismatch"):
        ambient_denied_tools(spec)


def test_ambient_denied_tools_names_the_offending_denial() -> None:
    """The refusal names the denial, so the caller can see which vocabulary it used."""
    spec = _spec(deny=[SEMANTIC_DENIAL])
    with pytest.raises(RuntimeSpawnError) as excinfo:
        ambient_denied_tools(spec)
    assert SEMANTIC_DENIAL in str(excinfo.value)


def test_ambient_denied_tools_passes_an_empty_denial_through() -> None:
    """Denying nothing is expressible, and stays the byte-equivalent no-op."""
    assert ambient_denied_tools(_spec(deny=[])) == []


def test_ambient_denied_tools_is_sorted() -> None:
    """The deny-list reaches the argv in a deterministic order."""
    spec = _spec(deny=[])
    assert ambient_denied_tools(spec) == sorted(ambient_denied_tools(spec))


@pytest.mark.parametrize(
    "deny",
    [
        pytest.param([SEMANTIC_DENIAL], id="single-semantic"),
        pytest.param([SEMANTIC_DENIAL, "repo_read"], id="several-semantic"),
    ],
)
def test_ambient_denied_tools_refuses_any_stray_name(deny: list[str]) -> None:
    """One unexpressible denial refuses the whole list; a partial deny is not a deny."""
    with pytest.raises(RuntimeSpawnError, match="tool_vocabulary_mismatch"):
        ambient_denied_tools(_spec(deny=deny))


def test_tool_policy_cannot_express_an_ambient_name_at_all() -> None:
    """The id grammar forbids the ambient vocabulary, so no valid deny can be forwarded.

    ``ToolCapabilityId`` is anchored lowercase, so an ambient name can never
    reach ``tool_policy.deny``. Every value the field CAN hold is therefore
    unexpressible as an ambient denial, which is why the guard refuses rather
    than translating: the forward had no correct input, not merely a wrong one.
    """
    with pytest.raises(ProviderConfigError, match="schema_invalid"):
        _spec(deny=[AMBIENT_DENIAL])


def test_ambient_denied_tools_rejects_a_spec_whose_policy_is_absent() -> None:
    """A spec is the only input; nothing else can supply a deny-list."""
    with pytest.raises((AttributeError, TypeError)):
        ambient_denied_tools(object())  # type: ignore[arg-type]


def test_ambient_denied_tools_reads_only_the_merged_policy() -> None:
    """The helper answers off the spec, not off any ambient default."""
    spec = _spec(deny=[])
    assert ambient_denied_tools(spec) == sorted(spec.tool_policy.deny)


def _launcher_sites() -> list[tuple[str, Any]]:
    """Return the launcher modules that must route through the guard."""
    from eawf.runtime.runtimes.claude import adapter as claude_adapter
    from eawf.runtime.runtimes.codex import adapter as codex_adapter

    return [("claude", claude_adapter), ("codex", codex_adapter)]


@pytest.mark.parametrize("name,module", _launcher_sites(), ids=lambda value: str(value)[:12])
def test_launcher_module_forwards_through_the_guard(name: str, module: Any) -> None:
    """Neither launcher may reach the spawn with a raw ``tool_policy.deny``."""
    source = module.__file__
    assert source is not None
    text = Path(source).read_text(encoding="utf-8")
    assert "ambient_denied_tools(spec)" in text, f"{name} launcher bypasses the guard"
    assert "denied_tools=sorted(spec.tool_policy.deny)" not in text
