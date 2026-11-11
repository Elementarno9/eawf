"""Unit tests for ``eawf.surfaces.render.hooks``.

Covers:

- A per-event wrapper bakes its event in; the plugin's one wrapper takes it
  as ``$1``. Both shell out to ``eawf hook run <event>``, naming the main
  checkout with ``-w`` only for a managed repository.
- Every :class:`HookEventType` member has a registry entry.
- The wrapper never runs with ``set -e`` and never passes ``--target-epoch``.
- The wrapper sets ``--runtime <runtime>`` so the hook router translates the
  payload for that host.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from eawf.runtime.hooks.event import HookEventType
from eawf.surfaces.render.hooks import HOOK_REGISTRY, render_hook_sh, render_hook_wrapper


def test_render_hook_sh_starts_with_shebang() -> None:
    """Every wrapper opens with the canonical bash shebang."""
    assert render_hook_sh(HookEventType.PRE_COMMIT).startswith("#!/usr/bin/env bash\n")
    assert render_hook_wrapper().startswith("#!/usr/bin/env bash\n")


_INVOCATION: str = (
    '"${_eawf_uv}" "${_eawf_run[@]}" eawf ${_eawf_named[@]+"${_eawf_named[@]}"}'
    ' hook run "${_eawf_event}" --runtime '
)


def test_render_hook_sh_pipes_to_eawf_hook_run() -> None:
    """The wrapper pipes the payload into ``eawf hook run <event>``.

    ``-w <main>`` is passed only for a managed repository; a call the guard
    merely reaches lets eawf resolve the session's directory itself.
    """
    output = render_hook_sh(HookEventType.PRE_COMMIT)
    assert '_eawf_event="pre_commit"' in output
    assert f"{_INVOCATION}claude" in output
    assert (
        'if [ "${_eawf_kind}" = "managed" ]; then\n    _eawf_named=(-w "${_eawf_main}")\nfi'
        in output
    )


def test_render_hook_wrapper_takes_the_event_as_its_argument() -> None:
    output = render_hook_wrapper()
    assert '_eawf_event="${1-}"' in output
    assert '_eawf_event="pre_commit"' not in output


def test_render_hook_sh_codex_runtime_sets_runtime_codex() -> None:
    """``runtime="codex"`` bakes ``--runtime codex``, never the Claude value."""
    output = render_hook_sh(HookEventType.SESSION_END, runtime="codex")
    assert f"{_INVOCATION}codex" in output
    assert "--runtime claude" not in output
    # Codex reads stdout as an answer, so only two events may print one.
    assert "session_start | pre_tool_use) ;;" in output


def test_render_hook_sh_codex_refuses_no_skill() -> None:
    """The managed-only skill refusal is a Claude answer; Codex has no skill hook."""
    assert "skill-classes.tsv" not in render_hook_sh(HookEventType.PRE_TOOL_USE, runtime="codex")
    assert "skill-classes.tsv" in render_hook_sh(HookEventType.PRE_TOOL_USE)


def test_render_hook_sh_bootstraps_path_and_resolves_uv() -> None:
    """The wrapper resolves an absolute ``uv`` and bootstraps PATH for worktrees."""
    output = render_hook_sh(HookEventType.SESSION_END)
    assert "command -v uv" in output
    assert "export PATH" in output
    assert ".local/bin" in output
    assert _INVOCATION in output


_RUNNER_CHOICE: str = (
    'if [ -x "${_eawf_top}/.venv/bin/eawf" ]'
    ' || [ -x "${_eawf_top}/.venv/Scripts/eawf.exe" ]; then\n'
    '    _eawf_run=(run --project "${_eawf_top}" --no-sync)\n'
    'elif [ -x "${_eawf_main}/.venv/bin/eawf" ]'
    ' || [ -x "${_eawf_main}/.venv/Scripts/eawf.exe" ]; then\n'
    '    _eawf_run=(run --project "${_eawf_main}" --no-sync)\n'
    "elif command -v eawf >/dev/null 2>&1; then\n"
    "    _eawf_run=(run --no-project)\n"
    "else\n"
)


@pytest.mark.parametrize("runtime", ["claude", "codex"])
@pytest.mark.parametrize("event_type", list(HookEventType))
def test_render_hook_sh_skips_host_project_only_when_eawf_on_path(
    event_type: HookEventType, runtime: str
) -> None:
    """Every ``uv run`` goes through the environment-conditional argument array.

    The project's own ``.venv`` eawf wins, so a global eawf of another version
    never answers the hook; ``--no-project`` applies only when the project has
    none and ``eawf`` is on PATH. Exactly one ``uv`` invocation means the
    wrapper never retries.
    """
    output = render_hook_sh(event_type, runtime=runtime)
    assert output.count(_RUNNER_CHOICE) == 1, output
    runs = re.findall(r'"\$\{_eawf_uv\}" (\S+) eawf ', output)
    assert runs == ['"${_eawf_run[@]}"'], output


_GOLDEN_HOOKS: Path = (
    Path(__file__).resolve().parents[1] / "golden" / "plugin_install" / "claude" / "hooks"
)


def test_claude_hook_goldens_carry_the_runner_choice() -> None:
    """All ten installed Claude hook goldens carry the environment conditional."""
    goldens = sorted(_GOLDEN_HOOKS.glob("*.sh"))
    assert len(goldens) == 10
    for golden in goldens:
        assert _RUNNER_CHOICE in golden.read_text(encoding="utf-8"), golden.name


@pytest.mark.parametrize(
    "output", [render_hook_sh(HookEventType.POST_COMMIT), render_hook_wrapper()]
)
def test_render_hook_sh_never_stops_on_the_first_error(output: str) -> None:
    """``set -e`` would turn any failed step into a non-zero exit the host acts on."""
    assert not re.search(r"^\s*set -[a-z]*e", output, re.MULTILINE)
    assert "--target-epoch" not in output
    assert "exit 2" not in output


@pytest.mark.parametrize("event_type", list(HookEventType))
def test_every_hook_event_type_has_registry_entry(event_type: HookEventType) -> None:
    """Every :class:`HookEventType` member must have a renderable spec."""
    assert f'_eawf_event="{event_type.value}"' in render_hook_sh(event_type)


def test_hook_registry_covers_all_event_types() -> None:
    registry_events = {spec.event_type for spec in HOOK_REGISTRY}
    assert registry_events == set(HookEventType)


def test_render_hook_sh_unknown_event_raises() -> None:
    """``render_hook_sh`` raises :class:`KeyError` for an unmapped event."""
    from eawf.surfaces.render import hooks as hooks_module

    saved = hooks_module.HOOK_REGISTRY
    hooks_module.HOOK_REGISTRY = ()
    try:
        with pytest.raises(KeyError):
            render_hook_sh(HookEventType.PRE_COMMIT)
    finally:
        hooks_module.HOOK_REGISTRY = saved


def test_render_hook_sh_terminates_with_newline() -> None:
    assert render_hook_sh(HookEventType.PRE_COMMIT).endswith("\n")
    assert render_hook_wrapper().endswith("\n")
