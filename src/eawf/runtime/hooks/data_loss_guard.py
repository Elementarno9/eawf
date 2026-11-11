"""The pre-tool hook that refuses a data-loss call and records the refusal.

The host's ``PreToolUse`` hook hands every tool call to ``eawf hook run
pre_tool_use`` before the call runs. :func:`guard_pre_tool_use` judges it with
:func:`~eawf.runtime.sandbox.data_loss.judge_tool_call` when the session is in a
managed tree and, on a denial, files it with the daemon as a sandbox decision.
The judgement never waits on the daemon: an unreachable daemon costs the
record, kept as a health row instead, never the refusal. A payload the hook
cannot read is a host fault and passes.

A host enforces the refusal only when it reads the deny this hook prints, so the
guard runs only on :data:`DENY_EMISSION_RUNTIMES`, the hosts whose pre-tool decision
wire the emitted document is proven against.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from eawf.runtime.hooks import host_calls, runner
from eawf.runtime.hooks.context import HookContext, resolve_hook_context
from eawf.runtime.hooks.event import HOST_HARNESSES, HookEvent
from eawf.runtime.hooks.health import HookFailure, record_failures
from eawf.runtime.sandbox.data_loss import DataLossDenial, judge_tool_call

logger = logging.getLogger(__name__)

#: The hook name a guard outcome is reported under.
DATA_LOSS_GUARD_HOOK: Final = "runtime.data_loss_guard"

#: The health code of a refusal the daemon could not be told about.
DENIAL_UNRECORDED: Final = "denial_unrecorded"

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


def guard_pre_tool_use(
    event: HookEvent, *, context: HookContext, daemon_client_factory: Any = None
) -> DataLossDenial | None:
    """Judge one pre-tool call; record and return its denial, or return ``None``.

    The guard is on for a call when the session is in a managed tree (see
    :func:`managed_tree`): the payload ``cwd`` lies in one, or the host's
    project directory does, each read through its symlinks. Then the judge
    runs on the call and every denial it makes stands. Otherwise the guard is
    off, and nothing about the call decides that: a session that has nothing
    to do with a managed tree is not judged, even when its command names a
    path inside one.

    Args:
        event: The PRE_TOOL_USE event.
        context: The repository context the hook resolved once for this call.
        daemon_client_factory: Opens the daemon client the denial is recorded
            through; the hook runner's default when ``None``.

    Returns:
        The denial the host must be handed, or ``None`` when the call may run,
        including when the payload names no tool, which is a host fault rather
        than a call to judge.
    """
    payload = runner._session_end_payload(event)
    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name:
        logger.warning("guard_pre_tool_use status=passed reason=no_tool_name")
        return None
    raw_cwd = payload.get("cwd")
    # The wrapper falls back to its own directory for a cwd that is not one;
    # judging against the same directory keeps the two sides on one answer.
    cwd = raw_cwd if isinstance(raw_cwd, str) and os.path.isdir(raw_cwd) else os.getcwd()
    project_dir = os.environ.get(_PROJECT_DIR_ENV) if event.runtime == "claude" else None
    root = _session_tree(context, cwd, project_dir)
    if root is None:
        return None
    # The kernel resolves a relative path or a .. through the real directory,
    # so the judge reads the session's directories the same way.
    real_cwd = Path(os.path.realpath(cwd))
    anchor = Path(os.path.realpath(project_dir)) if project_dir else real_cwd
    denial = judge_tool_call(tool_name, payload.get("tool_input"), cwd=real_cwd, anchor=anchor)
    if denial is None:
        return None
    outcome = _record(event, payload, tool_name, denial, root, daemon_client_factory)
    logger.warning(f"guard_pre_tool_use tool={tool_name} rule={denial.rule} record={outcome}")
    return denial


def _session_tree(context: HookContext, cwd: str, project_dir: str | None) -> Path | None:
    """Return the managed tree the session's directory or project directory lies in."""
    if context.kind == "managed":
        return context.main
    root = managed_tree(Path(os.path.realpath(cwd)))
    if root is None and project_dir and os.path.isdir(project_dir):
        root = managed_tree(Path(os.path.realpath(project_dir)))
    return root


def managed_tree(directory: Path) -> Path | None:
    """Return the root of the managed tree *directory* lies in, or ``None``.

    A managed tree is a git repository whose main checkout holds ``.ea/``, with
    its subfolders and linked worktrees, or a directory outside any git
    repository that holds ``.ea/state.json``, with everything under it. The
    walk goes up to the filesystem root, as the hook wrapper's does: a level
    holding ``.ea/state.json`` is kept, and a level holding ``.git`` asks git
    once for its main checkout. A ``.git`` git does not take, and a
    repository whose main checkout holds no ``.ea/``, are passed, since a
    nested clone or a submodule may lie in a managed checkout further up.

    Args:
        directory: An absolute directory, its symlinks already resolved.

    Returns:
        The first managed main checkout upward, else the nearest directory
        holding ``.ea/state.json``.
    """
    holder: Path | None = None
    for candidate in (directory, *directory.parents):
        if holder is None and (candidate / ".ea" / "state.json").is_file():
            holder = candidate
        if (candidate / ".git").exists():
            context = resolve_hook_context(candidate)
            if context.kind != "no-git" and (context.main / ".ea").is_dir():
                return context.main
    return holder


def _record(
    event: HookEvent,
    payload: dict[str, Any],
    tool_name: str,
    denial: DataLossDenial,
    repo_root: Path,
    daemon_client_factory: Any,
) -> str:
    """File the denial with the daemon; return what happened, never raise.

    A denial the daemon does not take leaves a health row in *repo_root*'s ``.ea``.
    """
    ea_dir = repo_root / ".ea"
    harness = HOST_HARNESSES.get(event.runtime)
    session = host_calls._host_session_of(payload)
    if harness is None or session is None:
        return "skipped: no host session"
    params = {
        "harness": harness,
        "host_session_id": session[:256],
        "tool_name": tool_name[:256],
        "host_call_key": host_calls._host_call_key(payload, tool_name),
        "rule": denial.rule,
        "repo_root": str(repo_root),
    }
    factory = daemon_client_factory or runner._default_daemon_client_factory
    try:
        with factory() as client:
            answer = client.call(_HOST_TOOL_DENY_METHOD, params)
    except Exception as exc:
        failure = HookFailure(
            event=event.event_type.value,
            hook=DATA_LOSS_GUARD_HOOK,
            code=DENIAL_UNRECORDED,
            error=type(exc).__name__,
            detail=repr(exc),
            at=datetime.now(UTC),
        )
        try:
            record_failures(ea_dir, [failure], session_id=session)
        except OSError as write_error:
            return f"unrecorded: {exc!r}; health row not written: {write_error!r}"
        return f"unrecorded: {exc!r}"
    return f"recorded {answer.get('decision_key')}"


__all__ = [
    "DATA_LOSS_GUARD_HOOK",
    "DENIAL_UNRECORDED",
    "DENY_EMISSION_RUNTIMES",
    "UNENFORCED_RUNTIMES",
    "deny_document",
    "guard_pre_tool_use",
    "managed_tree",
]
