"""The pre-tool hook that refuses a data-loss call and records the refusal.

The host's ``PreToolUse`` hook hands every tool call to ``eawf hook run
pre_tool_use`` before the call runs. :func:`guard_pre_tool_use` judges it with
:func:`~eawf.runtime.sandbox.data_loss.judge_tool_call` and, on a denial, files it
with the daemon as a sandbox decision. The judgement never waits on the daemon: an
unreachable daemon costs the record, never the refusal, and a payload the hook cannot
read is refused.

A host enforces the refusal only when it reads the deny this hook prints, so the
guard runs only on :data:`DENY_EMISSION_RUNTIMES`, the hosts whose pre-tool decision
wire the emitted document is proven against.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Final

from eawf.runtime.hooks import runner
from eawf.runtime.hooks.event import HookEvent
from eawf.runtime.sandbox.data_loss import DataLossDenial, judge_tool_call

logger = logging.getLogger(__name__)

#: The hook name a guard outcome is reported under.
DATA_LOSS_GUARD_HOOK: Final = "runtime.data_loss_guard"

#: The runtimes whose pre-tool hook reads the deny document this guard prints.
DENY_EMISSION_RUNTIMES: Final = frozenset({"claude", "codex"})

#: The runtimes that cannot enforce a pre-tool deny, and why.
UNENFORCED_RUNTIMES: Final[dict[str, str]] = {
    "opencode": "its plugin bridge subscribes no pre-tool event, so no call can be refused",
}

#: The verb a refusal is recorded through.
_HOST_TOOL_DENY_METHOD: Final = "runtime.host.tool.deny"

#: The variable Claude Code sets to the directory a session was started in.
_PROJECT_DIR_ENV: Final = "CLAUDE_PROJECT_DIR"


def deny_document(denial: DataLossDenial) -> dict[str, Any]:
    """Return the pre-tool decision document that refuses the call.

    Claude Code and Codex both read ``hookSpecificOutput.permissionDecision`` from a
    ``PreToolUse`` hook's stdout and refuse the call on ``deny``.
    """
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"Eä data-loss guard ({denial.rule}): {denial.reason}",
        }
    }


def unreadable_payload_denial(error: str) -> DataLossDenial:
    """Return the refusal for a pre-tool payload the hook could not read."""
    return DataLossDenial(
        pattern=None,
        reason=f"the pre-tool payload could not be read ({error}), so the call is refused",
    )


def guard_pre_tool_use(
    event: HookEvent, *, repo_root: Path, daemon_client_factory: Any = None
) -> DataLossDenial | None:
    """Judge one pre-tool call; record and return its denial, or return ``None``.

    Args:
        event: The PRE_TOOL_USE event.
        repo_root: The directory the hook runs in.
        daemon_client_factory: Opens the daemon client the denial is recorded
            through; the hook runner's default when ``None``.

    Returns:
        The denial the host must be handed, or ``None`` when the call may run.
    """
    payload = runner._session_end_payload(event)
    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name:
        return unreadable_payload_denial("no tool_name")
    raw_cwd = payload.get("cwd")
    cwd = Path(raw_cwd) if isinstance(raw_cwd, str) and raw_cwd else repo_root
    project_dir = os.environ.get(_PROJECT_DIR_ENV) if event.runtime == "claude" else None
    anchor = Path(project_dir) if project_dir else repo_root
    denial = judge_tool_call(tool_name, payload.get("tool_input"), cwd=cwd, anchor=anchor)
    if denial is None:
        return None
    outcome = _record(event, payload, tool_name, denial, repo_root, daemon_client_factory)
    logger.warning(f"guard_pre_tool_use tool={tool_name} rule={denial.rule} record={outcome}")
    return denial


def _record(
    event: HookEvent,
    payload: dict[str, Any],
    tool_name: str,
    denial: DataLossDenial,
    repo_root: Path,
    daemon_client_factory: Any,
) -> str:
    """File the denial with the daemon; return what happened, never raise."""
    harness = runner._HOST_HARNESSES.get(event.runtime)
    session = runner._host_session_of(payload)
    if harness is None or session is None:
        return "skipped: no host session"
    params = {
        "harness": harness,
        "host_session_id": session[:256],
        "tool_name": tool_name[:256],
        "host_call_key": runner._host_call_key(payload, tool_name),
        "rule": denial.rule,
        "repo_root": str(repo_root),
    }
    factory = daemon_client_factory or runner._default_daemon_client_factory
    try:
        with factory() as client:
            answer = client.call(_HOST_TOOL_DENY_METHOD, params)
    except Exception as exc:
        return f"unrecorded: {exc!r}"
    return f"recorded {answer.get('decision_key')}"


__all__ = [
    "DATA_LOSS_GUARD_HOOK",
    "DENY_EMISSION_RUNTIMES",
    "UNENFORCED_RUNTIMES",
    "deny_document",
    "guard_pre_tool_use",
    "unreadable_payload_denial",
]
