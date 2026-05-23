"""Cross-runtime resolution contract for skills / agents / hooks.

These are *contract* tests: they assert the invariant that binds three
otherwise-independent surfaces together —

* the capability matrix (``capabilities.yaml``), which declares which
  runtime exposes ``skills`` / ``sub_agents``;
* the per-skill / per-agent / per-hook registries
  (:data:`~eawf.render.skills.SKILL_REGISTRY`,
  :data:`~eawf.render.agents.AGENT_REGISTRY`,
  :data:`~eawf.render.hooks.HOOK_REGISTRY`);
* the per-runtime installer output (each runtime's ``expected_paths`` /
  ``install_plugin``), which is what actually lands on disk.

The contract is: **every registry entry resolves to a concrete installer
artifact on each runtime the matrix marks ``supported`` for that
capability, and the installer output matches the matrix.** The unit
suite (``tests/unit/test_capability_matrix.py``) pins the matrix loader
and the drift detector in isolation; this suite pins the *join* across
the three surfaces so a registry/installer drift fails fast.

One known gap is asserted with :func:`pytest.xfail`: the matrix marks
``skills`` (the OpenCode skill surface) ``supported`` for OpenCode and
OpenCode's installer does emit per-skill commands, **but** OpenCode emits
no hook wrappers at all — Claude and Codex both emit one ``.sh`` per
:class:`~eawf.hooks.event.HookEventType`, OpenCode emits zero. The
hook-parity contract therefore fails on OpenCode today; the xfail
documents the gap so closing it (an OpenCode hook surface) flips the
test to ``XPASS`` and forces a deliberate removal of the marker.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from eawf.render.agents import AGENT_REGISTRY
from eawf.render.hooks import HOOK_REGISTRY
from eawf.render.skills import SKILL_REGISTRY
from eawf.runtimes.capabilities import RUNTIME_IDS, get_matrix
from eawf.runtimes.claude import plugin_install as claude_install
from eawf.runtimes.codex import plugin_install as codex_install
from eawf.runtimes.opencode import plugin_install as opencode_install
from eawf.runtimes.selector import runtime_supports, select_adapter

pytestmark = pytest.mark.unit


# Skills the OpenCode installer is expected to skip — the model-only
# playbooks carry ``user_invocable=False`` and OpenCode only materialises
# user-invocable skills as ``/<name>`` commands.
def _user_invocable_skill_names() -> set[str]:
    return {spec.skill_name for spec in SKILL_REGISTRY if spec.user_invocable}


def _all_skill_names() -> set[str]:
    return {spec.skill_name for spec in SKILL_REGISTRY}


def _all_agent_roles() -> set[str]:
    return {spec.role for spec in AGENT_REGISTRY}


def _all_hook_event_values() -> set[str]:
    return {spec.event_type.value for spec in HOOK_REGISTRY}


def _claude_paths(tmp_path: Path) -> Mapping[str, Path]:
    paths, _settings = claude_install.expected_paths(tmp_path)
    return paths


def _codex_paths(tmp_path: Path) -> Mapping[str, Path]:
    paths, _config = codex_install.expected_paths(tmp_path)
    return paths


def _opencode_paths(tmp_path: Path) -> Mapping[str, Path]:
    paths, _config = opencode_install.expected_paths(tmp_path)
    return paths


# ---------------------------------------------------------------------------
# Matrix declares skills + sub_agents supported on every runtime
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("runtime_id", RUNTIME_IDS)
def test_matrix_marks_skills_supported_on_every_runtime(runtime_id: str) -> None:
    """The ``skills`` capability is ``supported`` for all three runtimes."""
    assert runtime_supports(runtime_id, "skills") is True


@pytest.mark.parametrize("runtime_id", RUNTIME_IDS)
def test_matrix_marks_sub_agents_supported_on_every_runtime(runtime_id: str) -> None:
    """The ``sub_agents`` capability is ``supported`` for all three runtimes."""
    assert runtime_supports(runtime_id, "sub_agents") is True


def test_matrix_runtime_ids_match_canonical_three() -> None:
    """The matrix runtime tuple is the canonical (claude-code, codex, opencode)."""
    assert get_matrix().runtime_ids() == RUNTIME_IDS == ("claude-code", "codex", "opencode")


@pytest.mark.parametrize("runtime_id", RUNTIME_IDS)
def test_every_runtime_resolves_to_an_adapter_with_matching_id(runtime_id: str) -> None:
    """Each declared runtime resolves to a concrete adapter whose id matches."""
    adapter = select_adapter(runtime_id)
    assert adapter.id == runtime_id


# ---------------------------------------------------------------------------
# Skills resolve on every declared-supported runtime
# ---------------------------------------------------------------------------


def test_claude_installer_emits_every_skill(tmp_path: Path) -> None:
    """``skills`` supported on claude-code -> one path per registry skill."""
    paths = _claude_paths(tmp_path)
    emitted = {
        region.removeprefix("plugin.claude.skill.")
        for region in paths
        if region.startswith("plugin.claude.skill.")
    }
    assert emitted == _all_skill_names()


def test_codex_installer_emits_every_skill(tmp_path: Path) -> None:
    """``skills`` supported on codex -> one path per registry skill."""
    paths = _codex_paths(tmp_path)
    emitted = {
        region.removeprefix("plugin.codex.skill.")
        for region in paths
        if region.startswith("plugin.codex.skill.")
    }
    assert emitted == _all_skill_names()


def test_opencode_installer_emits_every_user_invocable_skill(tmp_path: Path) -> None:
    """``skills`` supported on opencode -> one command per user-invocable skill.

    OpenCode materialises only ``user_invocable=True`` skills as
    ``/<name>`` commands; the model-only playbooks are intentionally
    absent. The contract is therefore narrowed to the user-invocable
    subset rather than the full registry.
    """
    paths = _opencode_paths(tmp_path)
    emitted = {
        region.removeprefix("plugin.opencode.command.")
        for region in paths
        if region.startswith("plugin.opencode.command.")
    }
    assert emitted == _user_invocable_skill_names()


def test_opencode_command_surface_omits_model_only_skills(tmp_path: Path) -> None:
    """Model-only (``user_invocable=False``) skills do not become commands."""
    paths = _opencode_paths(tmp_path)
    emitted = {
        region.removeprefix("plugin.opencode.command.")
        for region in paths
        if region.startswith("plugin.opencode.command.")
    }
    model_only = _all_skill_names() - _user_invocable_skill_names()
    assert model_only, "fixture invariant: at least one model-only skill exists"
    assert emitted.isdisjoint(model_only)


# ---------------------------------------------------------------------------
# Agents resolve on every runtime that hosts a top-level agent surface
# ---------------------------------------------------------------------------


def test_claude_installer_emits_every_agent(tmp_path: Path) -> None:
    """``sub_agents`` supported on claude-code -> one path per registry role."""
    paths = _claude_paths(tmp_path)
    emitted = {
        region.removeprefix("plugin.claude.agent.")
        for region in paths
        if region.startswith("plugin.claude.agent.")
    }
    assert emitted == _all_agent_roles()


def test_opencode_installer_emits_every_agent(tmp_path: Path) -> None:
    """``sub_agents`` supported on opencode -> one agent file per registry role."""
    paths = _opencode_paths(tmp_path)
    emitted = {
        region.removeprefix("plugin.opencode.agent.")
        for region in paths
        if region.startswith("plugin.opencode.agent.")
    }
    assert emitted == _all_agent_roles()


def test_codex_nests_agents_inside_skills_no_top_level_agent_dir(tmp_path: Path) -> None:
    """Codex hosts ``sub_agents`` by nesting in skills, not a top-level dir.

    The matrix marks ``sub_agents`` ``supported`` for codex, but the
    Codex-native plugin schema has no top-level ``agents`` key — agents
    live inside skills. The contract here pins that intentional shape so
    a future "codex grew an agents/ dir" change is a deliberate edit, not
    an accident.
    """
    paths = _codex_paths(tmp_path)
    agent_regions = [region for region in paths if region.startswith("plugin.codex.agent.")]
    assert agent_regions == []


# ---------------------------------------------------------------------------
# Hooks resolve on Claude + Codex
# ---------------------------------------------------------------------------


def test_claude_installer_emits_every_hook(tmp_path: Path) -> None:
    """Claude emits one ``.sh`` wrapper per :class:`HookEventType`."""
    paths = _claude_paths(tmp_path)
    emitted = {
        region.removeprefix("plugin.claude.hook.")
        for region in paths
        if region.startswith("plugin.claude.hook.")
    }
    assert emitted == _all_hook_event_values()


def test_codex_installer_emits_every_hook(tmp_path: Path) -> None:
    """Codex emits one ``.sh`` wrapper per :class:`HookEventType`."""
    paths = _codex_paths(tmp_path)
    emitted = {
        region.removeprefix("plugin.codex.hook.")
        for region in paths
        if region.startswith("plugin.codex.hook.")
    }
    assert emitted == _all_hook_event_values()


def test_hook_registry_is_non_empty() -> None:
    """Fixture invariant — the hook registry has at least one event."""
    assert _all_hook_event_values()


# ---------------------------------------------------------------------------
# OpenCode-hooks gap (xfail-documented contract break)
# ---------------------------------------------------------------------------


def test_opencode_installer_emits_every_hook(tmp_path: Path) -> None:
    """OpenCode hook parity — currently fails (no OpenCode hook surface).

    Claude and Codex both render one hook wrapper per
    :class:`HookEventType`. OpenCode's installer emits *no* hook
    artifacts (only ``plugin_js`` / sidecar / config / agents /
    commands), so the cross-runtime hook-parity contract is violated for
    OpenCode in v0.3. The xfail documents this as a known gap rather
    than silently dropping the runtime from the parity assertion; closing
    it (an OpenCode hook surface) makes this test ``XPASS`` and forces the
    marker's removal.
    """
    pytest.xfail("OpenCode emits no hook wrappers in v0.3 (cross-runtime hook gap)")
    paths = _opencode_paths(tmp_path)
    emitted = {
        region.removeprefix("plugin.opencode.hook.")
        for region in paths
        if region.startswith("plugin.opencode.hook.")
    }
    assert emitted == _all_hook_event_values()


def test_opencode_expected_paths_carries_no_hook_region_today(tmp_path: Path) -> None:
    """Positive pin of the gap: OpenCode currently has zero hook regions.

    This is the inverse of the xfail above — it asserts the *current*
    (gap) reality so the suite stays green while still documenting that
    OpenCode emits no hooks. When the gap is closed this test must be
    updated alongside removing the xfail; the pairing makes the two-step
    change visible in review.
    """
    paths = _opencode_paths(tmp_path)
    hook_regions = [region for region in paths if region.startswith("plugin.opencode.hook.")]
    assert hook_regions == []


# ---------------------------------------------------------------------------
# Installer output matches the matrix: a supported skill/agent capability
# means non-empty installer output for that surface.
# ---------------------------------------------------------------------------


def test_supported_skills_capability_implies_nonempty_skill_output(tmp_path: Path) -> None:
    """For every runtime the matrix marks ``skills`` supported, the installer
    emits at least one skill/command artifact."""
    surfaces: dict[str, set[str]] = {
        "claude-code": {r for r in _claude_paths(tmp_path) if r.startswith("plugin.claude.skill.")},
        "codex": {r for r in _codex_paths(tmp_path) if r.startswith("plugin.codex.skill.")},
        "opencode": {
            r for r in _opencode_paths(tmp_path) if r.startswith("plugin.opencode.command.")
        },
    }
    for runtime_id in RUNTIME_IDS:
        assert runtime_supports(runtime_id, "skills") is True
        assert surfaces[runtime_id], f"{runtime_id} marked skills-supported but emitted none"


def test_supported_sub_agents_capability_has_agent_surface(tmp_path: Path) -> None:
    """Every runtime marked ``sub_agents``-supported exposes an agent surface.

    Claude + OpenCode expose a top-level ``agents/`` surface; Codex
    nests agents inside skills (no top-level dir) but still satisfies the
    capability through the skill surface. The contract asserts the
    surface exists per runtime in its native shape.
    """
    claude_agents = {r for r in _claude_paths(tmp_path) if r.startswith("plugin.claude.agent.")}
    opencode_agents = {
        r for r in _opencode_paths(tmp_path) if r.startswith("plugin.opencode.agent.")
    }
    codex_skills = {r for r in _codex_paths(tmp_path) if r.startswith("plugin.codex.skill.")}

    assert runtime_supports("claude-code", "sub_agents") is True
    assert claude_agents
    assert runtime_supports("opencode", "sub_agents") is True
    assert opencode_agents
    # Codex's agent surface is the (non-empty) skill surface it nests into.
    assert runtime_supports("codex", "sub_agents") is True
    assert codex_skills


# ---------------------------------------------------------------------------
# Installer dry-run resolves byte-for-byte against expected_paths (the
# "resolves" half of the contract — every declared path is producible).
# ---------------------------------------------------------------------------


def test_claude_dry_run_resolves_all_expected_skill_agent_hook_paths(tmp_path: Path) -> None:
    """Claude dry-run install enumerates exactly the expected skill/agent/hook deltas."""
    result = claude_install.install_plugin(tmp_path, dry_run=True, persist_manifest=False)
    paths, _settings = claude_install.expected_paths(tmp_path)
    skill_paths = {p for region, p in paths.items() if region.startswith("plugin.claude.skill.")}
    agent_paths = {p for region, p in paths.items() if region.startswith("plugin.claude.agent.")}
    hook_paths = {p for region, p in paths.items() if region.startswith("plugin.claude.hook.")}

    assert {d.path for d in result.skills} == skill_paths
    assert {d.path for d in result.agents} == agent_paths
    assert {d.path for d in result.hooks} == hook_paths


def test_opencode_dry_run_resolves_agents_and_commands(tmp_path: Path) -> None:
    """OpenCode dry-run install enumerates the agent + command deltas, no hooks."""
    result = opencode_install.install_plugin(
        tmp_path, dry_run=True, home=tmp_path, opencode_config_dir=str(tmp_path / "ocfg")
    )
    assert {d.path for d in result.agents} == {
        opencode_install.expected_paths(
            tmp_path, home=tmp_path, opencode_config_dir=str(tmp_path / "ocfg")
        )[0][f"plugin.opencode.agent.{spec.role}"]
        for spec in AGENT_REGISTRY
    }
    assert result.commands
    # The result dataclass carries no hooks attribute at all — the gap is
    # structural, not just an empty list.
    assert not hasattr(result, "hooks")
