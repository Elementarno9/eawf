"""Render bash hook wrappers for Eä events.

:func:`render_hook_sh` is a pure renderer: it produces the wrapper script
for any :class:`~eawf.runtime.hooks.event.HookEventType`. Which events an
installer actually writes to disk is the installer's decision — the Claude
installer subscribes only handler-backed events (:attr:`HookSpec.has_handler`,
today just ``SESSION_END``), while the Codex installer renders every event
with ``runtime="codex"``.

The output is a small POSIX-bash wrapper that:

1. Reads the JSON document Claude Code delivers on stdin and forwards it
   verbatim — SessionEnd / Stop / SubagentStop stdin carries the session
   cost + token usage totals that feed EU capture, so a lossless forward
   is the primary contract.
2. Falls back — only when stdin is empty (or a TTY, for interactive smoke
   runs) — to synthesising a minimal payload of shape
   ``{"hook_event_name": <claude-name>, "claude_event_name":
   <eawf-event>, "args": [<arg1>, <arg2>, <arg3>, <arg4>]}`` from up to
   four positional arguments (``$1..$4``).
3. Pipes the payload to ``uv run eawf hook run <event_type> --runtime
   <runtime>`` (``<runtime>`` is ``claude`` by default, ``codex`` for the
   Codex install/doctor/package surfaces). The CLI's exit code (0 ok, 9
   blocked) is the wrapper's exit code.

The read is fail-open: a hook that dies breaks the operator's session,
so an unreadable / empty stdin degrades to the positional-arg fallback
rather than aborting.

The wrapper deliberately stays in pure POSIX bash (no ``read -r``
options, no ``${@:1:4}`` parameter expansions) so it works on macOS
bash 3.2 and Linux distros without requiring bash 4+.

Public API::

    HOOK_REGISTRY                # frozen tuple of every event Eä installs
    render_hook_sh(event_type)   # pure: returns the rendered bash script
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from importlib.resources import files

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from eawf.runtime.hooks.event import HookEventType

logger = logging.getLogger(__name__)


_TEMPLATE_NAME: str = "hook.sh.j2"
_TEMPLATES_PACKAGE: str = "eawf.platform.templates.claude"


@dataclass(frozen=True)
class HookSpec:
    """Frozen v0.1 hook spec used by :data:`HOOK_REGISTRY`.

    Attributes:
        event_type: The :class:`HookEventType` value (e.g.
            ``HookEventType.PRE_COMMIT``). Drives the file name
            (``<event_type.value>.sh``) and the ``eawf hook run``
            CLI argument.
        claude_event_name: Friendly Claude-side display name written
            into the synthesised payload's ``hook_event_name`` field.
            Mirrors the Claude Code hooks reference values so the
            router (W04) dispatches correctly.
        has_handler: ``True`` when a runner-registered handler actually
            consumes this event. Only :data:`HookEventType.SESSION_END`
            has one — ``runtime.capture``, wired by
            :func:`eawf.runtime.hooks.runner.register_runtime_capture_hooks`.
            Every other event's wrapper exits ``0`` with an empty result
            list (an idle contract), so the Claude installer subscribes
            only handler-backed events and never wires the operator's
            session to a no-op script.
        version: Schema version pin (``"1.0"``).
    """

    event_type: HookEventType
    claude_event_name: str
    has_handler: bool = False
    version: str = "1.0"


def _load_environment() -> Environment:
    """Load a Jinja2 environment rooted at the bundled claude templates dir."""
    templates_dir = files(_TEMPLATES_PACKAGE)
    templates_path = str(templates_dir)
    env = Environment(
        loader=FileSystemLoader(templates_path),
        undefined=StrictUndefined,
        keep_trailing_newline=False,
        autoescape=False,
    )
    return env


def render_hook_sh(event_type: HookEventType, *, runtime: str = "claude") -> str:
    """Render a bash hook wrapper for *event_type* under *runtime*.

    Args:
        event_type: A :class:`HookEventType` member. The wrapper's
            ``eawf hook run`` argument is the event's lowercase value
            (``HookEventType.PRE_COMMIT.value == "pre_commit"``).
        runtime: The ``--runtime`` value baked into the wrapper's
            ``eawf hook run`` invocation. Defaults to ``"claude"``; the
            Codex installer/doctor/packager pass ``"codex"`` so a
            Codex-hosted hook routes through the Codex payload translator
            rather than the Claude one.

    Returns:
        The rendered bash script, terminated by ``\\n``. Callers are
        responsible for setting the file mode to executable
        (``0o755``) — this function is pure.

    Raises:
        KeyError: *event_type* has no entry in :data:`HOOK_REGISTRY`.
    """
    spec = _spec_for(event_type)
    env = _load_environment()
    template = env.get_template(_TEMPLATE_NAME)
    rendered = template.render(
        event_type=spec.event_type.value,
        claude_event_name=spec.claude_event_name,
        runtime=runtime,
    )
    if not rendered.endswith("\n"):
        rendered = rendered + "\n"
    return rendered


def _spec_for(event_type: HookEventType) -> HookSpec:
    """Look up the :class:`HookSpec` for *event_type* in the registry."""
    for spec in HOOK_REGISTRY:
        if spec.event_type == event_type:
            return spec
    raise KeyError(f"no HookSpec registered for event_type={event_type!r}")


# ---------------------------------------------------------------------------
# Frozen v0.1 hook registry. Maps every Eä :class:`HookEventType` to the
# Claude-side display name. The router (:mod:`eawf.runtime.runtimes.claude.
# hooks_router`) uses ``hook_event_name`` to dispatch incoming payloads;
# the wrappers below set it to the matching Claude Code hook name so a
# wrapper-fired event survives the round-trip through ``eawf hook run``.
#
# Mapping notes:
# - PreToolUse / PostToolUse: Claude fires one hook per tool call; the
#   router resolves git commit/push commands to PRE_COMMIT/PRE_PUSH /
#   POST_COMMIT/POST_PUSH via ``_BASH_PREFIX_TO_PRE/POST``.
# - SessionStart / SessionEnd / Stop: routed verbatim; ``Stop`` collapses
#   to SESSION_END.
# - Lifecycle events without a Claude counterpart (PRE_AUDIT, POST_AUDIT,
#   WAVE_OPEN, WAVE_CLOSE, ITER_OPEN, ITER_CLOSE, PHASE_OPEN, PHASE_CLOSE,
#   AGENT_END) carry the eawf-native value as ``hook_event_name`` so the
#   router's warning path triggers a clear "skipped: unknown" log line if
#   the wrapper is ever invoked outside an Eä CLI context.
# - SubagentStop and PreCompact map to Claude's native event names so
#   hook manifests can subscribe to them directly.
# ---------------------------------------------------------------------------
HOOK_REGISTRY: tuple[HookSpec, ...] = (
    HookSpec(event_type=HookEventType.PRE_COMMIT, claude_event_name="PreToolUse"),
    HookSpec(event_type=HookEventType.POST_COMMIT, claude_event_name="PostToolUse"),
    HookSpec(event_type=HookEventType.PRE_PUSH, claude_event_name="PreToolUse"),
    HookSpec(event_type=HookEventType.POST_PUSH, claude_event_name="PostToolUse"),
    HookSpec(event_type=HookEventType.PRE_AUDIT, claude_event_name="pre_audit"),
    HookSpec(event_type=HookEventType.POST_AUDIT, claude_event_name="post_audit"),
    HookSpec(event_type=HookEventType.SESSION_START, claude_event_name="SessionStart"),
    HookSpec(
        event_type=HookEventType.SESSION_END,
        claude_event_name="SessionEnd",
        has_handler=True,
    ),
    HookSpec(event_type=HookEventType.WAVE_OPEN, claude_event_name="wave_open"),
    HookSpec(event_type=HookEventType.WAVE_CLOSE, claude_event_name="wave_close"),
    HookSpec(event_type=HookEventType.ITER_OPEN, claude_event_name="iter_open"),
    HookSpec(event_type=HookEventType.ITER_CLOSE, claude_event_name="iter_close"),
    HookSpec(event_type=HookEventType.PHASE_OPEN, claude_event_name="phase_open"),
    HookSpec(event_type=HookEventType.PHASE_CLOSE, claude_event_name="phase_close"),
    HookSpec(event_type=HookEventType.AGENT_END, claude_event_name="agent_end"),
    HookSpec(event_type=HookEventType.SUBAGENT_STOP, claude_event_name="SubagentStop"),
    HookSpec(event_type=HookEventType.PRE_COMPACT, claude_event_name="PreCompact"),
)


__all__ = [
    "HOOK_REGISTRY",
    "HookSpec",
    "render_hook_sh",
]
