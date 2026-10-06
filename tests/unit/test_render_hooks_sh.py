"""Unit tests for ``eawf.surfaces.render.hooks``.

Covers:

- Bash hook wrappers shell-out to ``eawf hook run <event>``.
- Every :class:`HookEventType` member has a registry entry.
- The wrapper synthesises a JSON payload with the expected keys
  (``hook_event_name``, ``claude_event_name``, ``args``).
- The wrapper sets ``--runtime claude`` so the hook router translates
  the payload via :mod:`eawf.runtime.runtimes.claude.hooks_router`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from eawf.runtime.hooks.event import HookEventType
from eawf.surfaces.render.hooks import HOOK_REGISTRY, render_hook_sh


def test_render_hook_sh_starts_with_shebang() -> None:
    """Every wrapper opens with the canonical bash shebang."""
    output = render_hook_sh(HookEventType.PRE_COMMIT)
    assert output.startswith("#!/usr/bin/env bash\n")


def test_render_hook_sh_pipes_to_eawf_hook_run() -> None:
    """The wrapper pipes the synthesised payload into ``eawf hook run <event>``."""
    output = render_hook_sh(HookEventType.PRE_COMMIT)
    assert "eawf hook run pre_commit" in output
    assert "--runtime claude" in output


def test_render_hook_sh_codex_runtime_sets_runtime_codex() -> None:
    """``runtime="codex"`` bakes ``--runtime codex`` — never the Claude value."""
    output = render_hook_sh(HookEventType.SESSION_END, runtime="codex")
    assert "eawf hook run session_end --runtime codex" in output
    assert "--runtime claude" not in output
    assert "--runtime codex --target-epoch 2 >/dev/null" in output


def test_render_hook_sh_bootstraps_path_and_resolves_uv() -> None:
    """The wrapper resolves an absolute ``uv`` and bootstraps PATH for worktrees.

    A hook fired from a git worktree can inherit a stripped PATH; without the
    bootstrap the bare ``uv`` dies with exit 127. The wrapper prepares PATH and
    resolves ``uv`` to an absolute path before the final ``exec``.
    """
    output = render_hook_sh(HookEventType.SESSION_END)
    assert "command -v uv" in output
    assert "export PATH" in output
    assert ".local/bin" in output
    # The exec uses the resolved interpreter, not a bare ``uv``.
    assert 'exec "${_eawf_uv}" "${_eawf_uv_run[@]}" eawf hook run session_end' in output


_UV_RUN_CONDITIONAL: str = (
    "if command -v eawf >/dev/null 2>&1; then\n"
    "    _eawf_uv_run=(run --no-project)\n"
    "else\n"
    "    _eawf_uv_run=(run)\n"
    "fi\n"
)


@pytest.mark.parametrize("runtime", ["claude", "codex"])
@pytest.mark.parametrize("event_type", list(HookEventType))
def test_render_hook_sh_skips_host_project_only_when_eawf_on_path(
    event_type: HookEventType, runtime: str
) -> None:
    """Every ``uv run`` goes through the PATH-conditional argument array.

    ``--no-project`` keeps uv from syncing the host repository's environment
    before each hook, but hides an eawf installed only as that project's
    dependency; the wrapper therefore passes it only when ``eawf`` is on PATH.
    Exactly one ``uv`` invocation means the wrapper never retries.
    """
    output = render_hook_sh(event_type, runtime=runtime)
    assert output.count(_UV_RUN_CONDITIONAL) == 1, output
    runs = re.findall(r'"\$\{_eawf_uv\}" (\S+) eawf hook run (\S+)', output)
    assert runs == [('"${_eawf_uv_run[@]}"', event_type.value)], output
    assert "run --no-project eawf" not in output


_GOLDEN_HOOKS: Path = (
    Path(__file__).resolve().parents[1] / "golden" / "plugin_install" / "claude" / "hooks"
)


def test_claude_hook_goldens_carry_the_uv_run_conditional() -> None:
    """All ten installed Claude hook goldens carry the PATH conditional."""
    goldens = sorted(_GOLDEN_HOOKS.glob("*.sh"))
    assert len(goldens) == 10
    for golden in goldens:
        assert _UV_RUN_CONDITIONAL in golden.read_text(encoding="utf-8"), golden.name


def test_render_hook_sh_synthesises_json_payload_keys() -> None:
    """The wrapper emits a JSON payload with the documented key set."""
    output = render_hook_sh(HookEventType.SESSION_START)
    # The payload printf format string must include all three keys.
    assert '"hook_event_name":' in output
    assert '"claude_event_name":' in output
    assert '"args":' in output


def test_render_hook_sh_sets_strict_bash() -> None:
    """``set -euo pipefail`` is mandatory for safe wrapper execution."""
    output = render_hook_sh(HookEventType.POST_COMMIT)
    assert "set -euo pipefail" in output


@pytest.mark.parametrize("event_type", list(HookEventType))
def test_every_hook_event_type_has_registry_entry(event_type: HookEventType) -> None:
    """Every :class:`HookEventType` member must have a renderable spec."""
    output = render_hook_sh(event_type)
    assert f"eawf hook run {event_type.value}" in output


def test_hook_registry_covers_all_event_types() -> None:
    registry_events = {spec.event_type for spec in HOOK_REGISTRY}
    assert registry_events == set(HookEventType)


def test_render_hook_sh_pre_commit_carries_pretooluse_claude_name() -> None:
    """The router maps Claude ``PreToolUse`` → :data:`HookEventType.PRE_COMMIT`."""
    output = render_hook_sh(HookEventType.PRE_COMMIT)
    # The wrapper announces ``hook_event_name`` as ``PreToolUse`` so the
    # router dispatches via ``_BASH_PREFIX_TO_PRE``.
    assert '"PreToolUse"' in output


def test_render_hook_sh_session_start_carries_verbatim_claude_name() -> None:
    """SessionStart maps verbatim — no router transformation."""
    output = render_hook_sh(HookEventType.SESSION_START)
    assert '"SessionStart"' in output


def test_render_hook_sh_unknown_event_raises() -> None:
    """``render_hook_sh`` raises :class:`KeyError` for an unmapped event."""
    # Build a fake HookEventType-shaped object that bypasses the enum.
    # Easier: monkeypatch the registry to be empty, then call.
    from eawf.surfaces.render import hooks as hooks_module

    saved = hooks_module.HOOK_REGISTRY
    hooks_module.HOOK_REGISTRY = ()
    try:
        with pytest.raises(KeyError):
            render_hook_sh(HookEventType.PRE_COMMIT)
    finally:
        hooks_module.HOOK_REGISTRY = saved


def test_render_hook_sh_terminates_with_newline() -> None:
    output = render_hook_sh(HookEventType.PRE_COMMIT)
    assert output.endswith("\n")
