"""Translate eawf hook events into the Claude Code plugin ``hooks.json``.

Per Phase 13 W05 (B015), the Claude Code plugin tree emitted by
:func:`eawf.runtime.runtimes.claude.plugin_package.package_plugin` carries a
``hooks.json`` manifest that subscribes only to **session-level** Claude
Code events. Workflow-internal lifecycle events (``wave_*``, ``iter_*``,
``phase_*``, ``*_audit``) stay fired by explicit ``eawf hook run`` calls
from the lifecycle surfaces — Claude Code's ``UserPromptSubmit`` matcher
cannot observe slash-command sub-skill dispatch, and daemon-proxied state
mutations never emit a prompt at all, so a manifest-level subscription
would be lossy in both directions.

The six session-level events Claude Code CAN observe reliably:

==================  =====================  ====================================
HookEventType       CC event               Matcher
==================  =====================  ====================================
``SESSION_START``   ``SessionStart``       (none)
``SESSION_END``     ``Stop``               (none)
``PRE_COMMIT``      ``PreToolUse``         ``Bash`` (cmd starts ``git commit``)
``POST_COMMIT``     ``PostToolUse``        ``Bash`` (cmd starts ``git commit``)
``PRE_PUSH``        ``PreToolUse``         ``Bash`` (cmd starts ``git push``)
``POST_PUSH``       ``PostToolUse``        ``Bash`` (cmd starts ``git push``)
==================  =====================  ====================================

Each entry resolves to a ``${CLAUDE_PLUGIN_ROOT}/hooks/<event>.sh``
wrapper. CC expands ``CLAUDE_PLUGIN_ROOT`` to the plugin's install root
at runtime, so the manifest stays portable across user installs.

Public API::

    PluginHookSpec                  # dataclass: event_type, cc_event, matcher
    PLUGIN_HOOK_REGISTRY            # frozen tuple of every session-level entry
    build_plugin_hooks_json()       # pure: assembled dict, ready for json.dumps
    render_plugin_hooks_json()      # pure: canonical JSON text + trailing newline
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from eawf.runtime.hooks.event import HookEventType

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


# Frozen v0.2 registry. Only the six session-level entries Claude Code's
# plugin event surface can observe reliably; see the module docstring
# for the rationale.
PLUGIN_HOOK_REGISTRY: tuple[PluginHookSpec, ...] = (
    PluginHookSpec(event_type=HookEventType.SESSION_START, cc_event="SessionStart"),
    PluginHookSpec(event_type=HookEventType.SESSION_END, cc_event="Stop"),
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


def build_plugin_hooks_json() -> dict[str, Any]:
    """Return the assembled ``hooks.json`` dict for the plugin tree.

    Walks :data:`PLUGIN_HOOK_REGISTRY`, grouping entries by ``cc_event``
    in registry order. Each entry produces a ``{"matcher": <str>,
    "hooks": [{"type": "command", "command": <str>}]}`` block. The dict
    wraps everything under the top-level ``"hooks"`` key, matching the
    Claude Code plugin manifest schema.

    Returns:
        A nested dict ready for :func:`json.dumps`. Iteration order
        within each CC event mirrors :data:`PLUGIN_HOOK_REGISTRY` —
        callers that need byte-stability should pass ``sort_keys=True``
        to :func:`json.dumps`.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for spec in PLUGIN_HOOK_REGISTRY:
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
    "render_plugin_hooks_json",
]
