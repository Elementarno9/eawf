"""Translate eawf hook events into the Claude Code plugin ``hooks.json``.

The Claude Code plugin tree emitted by
:func:`eawf.runtime.runtimes.claude.plugin_package.package_plugin` carries a
``hooks.json`` manifest that subscribes only to **session-level** Claude
Code events that are also handler-backed (see
:func:`handler_backed_plugin_hooks`) — today ``SESSION_START`` and
``SESSION_END``.
Workflow-internal lifecycle events (``wave_*``, ``iter_*``, ``phase_*``,
``*_audit``) stay fired by explicit ``eawf hook run`` calls from the
lifecycle surfaces — Claude Code's ``UserPromptSubmit`` matcher cannot
observe slash-command sub-skill dispatch, and daemon-proxied state
mutations never emit a prompt at all, so a manifest-level subscription
would be lossy in both directions.

:data:`PLUGIN_HOOK_REGISTRY` below is the full catalog of session-level
events Claude Code CAN observe reliably; it is not itself the emitted set
(see :func:`handler_backed_plugin_hooks`):

==================  =====================  ====================================
HookEventType       CC event               Matcher
==================  =====================  ====================================
``SESSION_START``   ``SessionStart``       (none)
``SESSION_END``     ``Stop``               (none)
``PRE_COMMIT``      ``PreToolUse``         ``Bash`` (cmd starts ``git commit``)
``POST_COMMIT``     ``PostToolUse``        ``Bash`` (cmd starts ``git commit``)
``PRE_PUSH``        ``PreToolUse``         ``Bash`` (cmd starts ``git push``)
``POST_PUSH``       ``PostToolUse``        ``Bash`` (cmd starts ``git push``)
``SUBAGENT_STOP``   ``SubagentStop``       (none)
``PRE_COMPACT``     ``PreCompact``         (none)
==================  =====================  ====================================

Each entry resolves to a ``${CLAUDE_PLUGIN_ROOT}/hooks/<event>.sh``
wrapper. CC expands ``CLAUDE_PLUGIN_ROOT`` to the plugin's install root
at runtime, so the manifest stays portable across user installs.

Public API::

    PluginHookSpec                  # dataclass: event_type, cc_event, matcher
    PLUGIN_HOOK_REGISTRY            # frozen tuple of every session-level entry
    handler_backed_plugin_hooks()   # pure: PLUGIN_HOOK_REGISTRY, handler-backed subset
    build_plugin_hooks_json()       # pure: assembled dict, ready for json.dumps
    render_plugin_hooks_json()      # pure: canonical JSON text + trailing newline
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Final

from eawf.runtime.hooks.event import HookEventType
from eawf.surfaces.render.hooks import HOOK_REGISTRY

logger = logging.getLogger(__name__)


# The CC plugin-root template variable. CC expands this to the plugin's
# install root at runtime; using the literal string here keeps the
# rendered manifest portable across user installs.
_PLUGIN_ROOT_VAR: str = "${CLAUDE_PLUGIN_ROOT}"


@dataclass(frozen=True)
class PluginHookSpec:
    """Frozen mapping from one eawf event to one CC plugin-manifest entry.

    Attributes:
        event_type: The :class:`~eawf.runtime.hooks.event.HookEventType` value
            whose wrapper script (``hooks/<value>.sh``) the manifest
            entry will invoke.
        cc_event: The Claude Code event name (one of ``SessionStart``,
            ``Stop``, ``PreToolUse``, ``PostToolUse``). Determines which
            top-level array in ``hooks.json`` the entry lands in.
        matcher: The CC matcher string. Empty string means "no matcher"
            (CC fires the hook on every event). ``"Bash"`` filters tool
            calls down to bash invocations; the wrapper itself further
            narrows by inspecting the command in the synthesised
            payload.
    """

    event_type: HookEventType
    cc_event: str
    matcher: str = ""


# Frozen v0.2 registry. Only entries Claude Code's plugin event surface
# can observe reliably are subscribed here; workflow-internal lifecycle
# events still fire through explicit ``eawf hook run`` calls.
PLUGIN_HOOK_REGISTRY: tuple[PluginHookSpec, ...] = (
    PluginHookSpec(event_type=HookEventType.SESSION_START, cc_event="SessionStart"),
    PluginHookSpec(event_type=HookEventType.SESSION_END, cc_event="Stop"),
    PluginHookSpec(event_type=HookEventType.SUBAGENT_STOP, cc_event="SubagentStop"),
    PluginHookSpec(event_type=HookEventType.PRE_COMPACT, cc_event="PreCompact"),
    PluginHookSpec(event_type=HookEventType.PRE_COMMIT, cc_event="PreToolUse", matcher="Bash"),
    PluginHookSpec(event_type=HookEventType.POST_COMMIT, cc_event="PostToolUse", matcher="Bash"),
    PluginHookSpec(event_type=HookEventType.PRE_PUSH, cc_event="PreToolUse", matcher="Bash"),
    PluginHookSpec(event_type=HookEventType.POST_PUSH, cc_event="PostToolUse", matcher="Bash"),
)


def _command_path(spec: PluginHookSpec) -> str:
    """Return the ``command`` field for *spec*'s manifest entry.

    Always resolves to ``${CLAUDE_PLUGIN_ROOT}/hooks/<event>.sh`` so the
    manifest is portable across user installs.
    """
    return f"{_PLUGIN_ROOT_VAR}/hooks/{spec.event_type.value}.sh"


# Events with a real runner-registered handler (:data:`HookSpec.has_handler`
# in :mod:`eawf.surfaces.render.hooks`) — today ``SESSION_START`` and
# ``SESSION_END``. Every
# other :data:`PLUGIN_HOOK_REGISTRY` entry would render an idle wrapper (exit
# 0, empty result list), so the packaged tree subscribes only this subset;
# see :func:`handler_backed_plugin_hooks`.
_HANDLER_BACKED_EVENT_TYPES: Final[frozenset[HookEventType]] = frozenset(
    spec.event_type for spec in HOOK_REGISTRY if spec.has_handler
)


def handler_backed_plugin_hooks() -> tuple[PluginHookSpec, ...]:
    """Return the :data:`PLUGIN_HOOK_REGISTRY` subset with a real handler.

    Derived from :data:`HOOK_REGISTRY`'s ``has_handler`` flag rather than a
    second hand-kept list, so this set can never silently drift from what
    the local ``eawf plugin install claude`` surface wires (its
    ``_INSTALLED_EVENTS`` is the same filter over the same registry).
    """
    return tuple(
        spec for spec in PLUGIN_HOOK_REGISTRY if spec.event_type in _HANDLER_BACKED_EVENT_TYPES
    )


def build_plugin_hooks_json() -> dict[str, Any]:
    """Return the assembled ``hooks.json`` dict for the plugin tree.

    Walks :func:`handler_backed_plugin_hooks`, grouping entries by
    ``cc_event`` in registry order. Each entry produces a ``{"matcher":
    <str>, "hooks": [{"type": "command", "command": <str>}]}`` block. The
    dict wraps everything under the top-level ``"hooks"`` key, matching the
    Claude Code plugin manifest schema. Handler-less events are omitted so
    the manifest never wires Claude Code to an idle no-op script.

    Returns:
        A nested dict ready for :func:`json.dumps`. Iteration order
        within each CC event mirrors :data:`PLUGIN_HOOK_REGISTRY` —
        callers that need byte-stability should pass ``sort_keys=True``
        to :func:`json.dumps`.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for spec in handler_backed_plugin_hooks():
        entry: dict[str, Any] = {
            "matcher": spec.matcher,
            "hooks": [
                {
                    "type": "command",
                    "command": _command_path(spec),
                }
            ],
        }
        grouped.setdefault(spec.cc_event, []).append(entry)
    return {"hooks": grouped}


def render_plugin_hooks_json() -> str:
    """Return the canonical ``hooks.json`` text (sorted keys, 2-space indent).

    Identical to ``json.dumps(build_plugin_hooks_json(), sort_keys=True,
    indent=2) + "\\n"`` — the trailing newline keeps POSIX text-file
    conventions and avoids spurious diffs from editors that auto-append
    one.
    """
    payload = build_plugin_hooks_json()
    return json.dumps(payload, sort_keys=True, indent=2) + "\n"


__all__ = [
    "PLUGIN_HOOK_REGISTRY",
    "PluginHookSpec",
    "build_plugin_hooks_json",
    "handler_backed_plugin_hooks",
    "render_plugin_hooks_json",
]
