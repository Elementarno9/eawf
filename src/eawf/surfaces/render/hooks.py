"""Render bash hook wrappers for Eä events.

:func:`render_hook_sh` is a pure renderer: it produces the wrapper script
for any :class:`~eawf.runtime.hooks.event.HookEventType`. Which events an
installer actually writes to disk is the installer's decision — the Claude
installer subscribes only handler-backed events (:attr:`HookSpec.has_handler`,
today ``SESSION_START``, ``SESSION_END``, ``SUBAGENT_START``, ``SUBAGENT_STOP``,
``PRE_COMPACT``, ``PERMISSION_REQUEST`` and the three tool events), while
the Codex installer renders every event with ``runtime="codex"``.

:func:`render_hook_wrapper` renders the plugin's one wrapper, which takes the
event as its first argument; :func:`render_hook_sh` bakes one event in for
the installers that write a file per event.

The output is a small bash wrapper that:

1. Exits 0 at once when ``EAWF_HOOKS=off``.
2. Reads the JSON document the host delivers on stdin (nothing on a TTY).
3. Probes the repository context in bash from the payload's ``cwd``: a
   repository is managed when its main checkout holds one of
   :data:`~eawf.runtime.hooks.context.MANAGED_ANCHORS`, so a subfolder or a
   linked worktree answers for it. Outside one the wrapper prints nothing,
   except the not-managed refusal of a managed-only skill (from the
   ``skill-classes.tsv`` beside it), and exits 0.
4. In a managed repository, pipes the payload to ``uv run <mode> eawf -w
   <main> hook run <event> --runtime <runtime>``. An eawf in the working
   tree's or the main checkout's ``.venv`` runs with ``--project <root>
   --no-sync``; else an eawf on PATH runs with ``--no-project``.
5. Fails open: a missing uv or eawf, or a child that exits non-zero, appends
   one row to the repository's hook health record and exits 0 with the
   child's stdout discarded.

The wrapper stays bash 3.2 compatible so it runs on macOS's system bash.

Public API::

    HOOK_REGISTRY                # frozen tuple of every event Eä installs
    render_hook_sh(event_type)   # pure: one event's wrapper
    render_hook_wrapper()        # pure: the plugin's one wrapper
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from eawf.runtime.hooks.context import MANAGED_ANCHORS
from eawf.runtime.hooks.event import HookEventType
from eawf.runtime.hooks.health import HEALTH_RECORD
from eawf.workflow.skills.catalog import NOT_MANAGED_REFUSAL

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
            router dispatches correctly.
        has_handler: ``True`` when a runner-registered handler actually
            consumes this event: :data:`HookEventType.SESSION_END` has
            ``session.end_stamp``, wired by
            :func:`eawf.runtime.hooks.runner.register_runtime_capture_hooks`,
            :data:`HookEventType.SESSION_START` has the rule-projection
            staleness check ``eawf hook run`` registers, and the two subagent
            events have ``runtime.host_subagent``, which adopts a
            harness-spawned subagent as a Run, and
            :data:`HookEventType.PERMISSION_REQUEST` has
            ``runtime.host_permission``, which records the held call as a
            provider permission, and the three tool events have
            ``runtime.host_tool``, which states each host tool call on its Run's
            transcript, and :data:`HookEventType.PRE_COMPACT` has
            ``runtime.host_context``, which snapshots the Run's contract anchors
            before the host compacts, and
            :data:`HookEventType.USER_PROMPT_SUBMIT` has ``skill.invocation``,
            which refuses a skill invocation its argument schema refuses. Every other
            event's wrapper exits ``0`` with an empty result list (an idle
            contract), so the Claude installer subscribes
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
    return _render(_spec_for(event_type).event_type.value, runtime=runtime)


def render_hook_wrapper(*, runtime: str = "claude") -> str:
    """Render the one wrapper a plugin ships for every event it subscribes.

    The wrapper takes the event as its first argument, so a manifest entry
    reads ``bash <plugin root>/hooks/eawf-hook.sh <event>``.

    Args:
        runtime: The ``--runtime`` value baked into the wrapper's
            ``eawf hook run`` invocation.

    Returns:
        The rendered bash script, terminated by ``\\n``.
    """
    return _render("", runtime=runtime)


def _render(event_type: str, *, runtime: str) -> str:
    """Render the wrapper template with *event_type* baked in, or taken from ``$1`` when empty."""
    tool_refusal = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": NOT_MANAGED_REFUSAL,
        }
    }
    env = _load_environment()
    template = env.get_template(_TEMPLATE_NAME)
    rendered = template.render(
        event_type=event_type,
        runtime=runtime,
        managed_anchors=[anchor.as_posix() for anchor in MANAGED_ANCHORS],
        health_record=(Path(".ea") / HEALTH_RECORD).as_posix(),
        prompt_refusal=json.dumps(
            {"decision": "block", "reason": NOT_MANAGED_REFUSAL}, separators=(",", ":")
        ),
        tool_refusal=json.dumps(tool_refusal, separators=(",", ":")),
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
# - SessionStart / SessionEnd: routed verbatim. ``Stop`` fires after every
#   turn, so the router skips it rather than read it as the session's end.
# - Lifecycle events without a Claude counterpart (PRE_AUDIT, POST_AUDIT,
#   WAVE_OPEN, WAVE_CLOSE, ITER_OPEN, ITER_CLOSE, PHASE_OPEN, PHASE_CLOSE,
#   AGENT_END) carry the eawf-native value as ``hook_event_name`` so the
#   router's warning path triggers a clear "skipped: unknown" log line if
#   the wrapper is ever invoked outside an Eä CLI context.
# - SubagentStart, SubagentStop, and PreCompact map to native event names so
#   hook manifests can subscribe to them directly.
# ---------------------------------------------------------------------------
HOOK_REGISTRY: tuple[HookSpec, ...] = (
    HookSpec(event_type=HookEventType.PRE_COMMIT, claude_event_name="PreToolUse"),
    HookSpec(event_type=HookEventType.POST_COMMIT, claude_event_name="PostToolUse"),
    HookSpec(event_type=HookEventType.PRE_PUSH, claude_event_name="PreToolUse"),
    HookSpec(event_type=HookEventType.POST_PUSH, claude_event_name="PostToolUse"),
    HookSpec(event_type=HookEventType.PRE_AUDIT, claude_event_name="pre_audit"),
    HookSpec(event_type=HookEventType.POST_AUDIT, claude_event_name="post_audit"),
    HookSpec(
        event_type=HookEventType.SESSION_START,
        claude_event_name="SessionStart",
        has_handler=True,
    ),
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
    HookSpec(
        event_type=HookEventType.SUBAGENT_START,
        claude_event_name="SubagentStart",
        has_handler=True,
    ),
    HookSpec(
        event_type=HookEventType.SUBAGENT_STOP,
        claude_event_name="SubagentStop",
        has_handler=True,
    ),
    HookSpec(
        event_type=HookEventType.PRE_COMPACT, claude_event_name="PreCompact", has_handler=True
    ),
    HookSpec(
        event_type=HookEventType.PERMISSION_REQUEST,
        claude_event_name="PermissionRequest",
        has_handler=True,
    ),
    HookSpec(
        event_type=HookEventType.PRE_TOOL_USE, claude_event_name="PreToolUse", has_handler=True
    ),
    HookSpec(
        event_type=HookEventType.POST_TOOL_USE, claude_event_name="PostToolUse", has_handler=True
    ),
    HookSpec(
        event_type=HookEventType.POST_TOOL_USE_FAILURE,
        claude_event_name="PostToolUseFailure",
        has_handler=True,
    ),
    HookSpec(
        event_type=HookEventType.USER_PROMPT_SUBMIT,
        claude_event_name="UserPromptSubmit",
        has_handler=True,
    ),
)


__all__ = [
    "HOOK_REGISTRY",
    "HookSpec",
    "render_hook_sh",
    "render_hook_wrapper",
]
