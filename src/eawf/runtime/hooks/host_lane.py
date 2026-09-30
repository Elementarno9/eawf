"""Host-lane hooks: compaction boundaries and skill invocations, judged at the host.

Two things the host does outside any tool call reach eawf only through its hooks. A
compaction rewrites what the model remembers, so the pre-compaction hook snapshots the
Run's contract anchors and the post-compaction session start reads them back from the
store (:data:`HOST_CONTEXT_HOOK`). A skill invocation, typed by the operator or made by
the model through the host's skill tool, is judged against the skill's argument schema
before the skill's text reaches the model (:data:`HOST_SKILL_HOOK`).
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from eawf.runtime.hooks.event import HOST_HARNESSES, HookEvent, HookEventType
from eawf.runtime.hooks.runner import (
    DaemonClientFactory,
    HookResult,
    HookRunner,
    _default_daemon_client_factory,
    _session_end_payload,
)

if TYPE_CHECKING:
    from eawf.workflow.skills.catalog import Lane

#: The verb a host compaction is stated through.
_HOST_CONTEXT_METHOD: Final = "runtime.host.context.boundary"

#: The hook that states a host compaction on its Run.
HOST_CONTEXT_HOOK: Final = "runtime.host_context"

#: The boundary each context event reports.
_HOST_CONTEXT_BOUNDARIES: Final[dict[HookEventType, str]] = {
    HookEventType.PRE_COMPACT: "compacting",
    HookEventType.SESSION_START: "resumed",
}


def record_host_context_boundary(
    event: HookEvent,
    *,
    daemon_client_factory: DaemonClientFactory | None = None,
    repo_root: Path | None = None,
) -> HookResult:
    """State a host compaction on the Run the host's session runs as.

    Before the host compacts, the Run's contract anchors are snapshotted on its
    stream; when the session starts again they are read back from the store and
    compared, and the restatement the daemon answers with is what the host hands
    its model. A session with no live Run gets the daemon's typed refusal.

    Args:
        event: The PRE_COMPACT or SESSION_START event.
        daemon_client_factory: Opens the daemon client; the default one when ``None``.
        repo_root: The repository the harness runs in; the process working directory
            when ``None``.

    Returns:
        A non-blocking :class:`HookResult` whose first line names the Run and the
        drift found, followed by the restatement; or why no boundary was stated.
    """
    name = HOST_CONTEXT_HOOK
    harness = HOST_HARNESSES.get(event.runtime)
    payload = _session_end_payload(event)
    session = payload.get("session_id")
    if harness is None or not isinstance(session, str) or not session:
        return HookResult(name=name, output=f"{name} skipped: no host session")
    params: dict[str, Any] = {
        "harness": harness,
        "host_session_id": session,
        "boundary": _HOST_CONTEXT_BOUNDARIES[event.event_type],
        "repo_root": str(repo_root if repo_root is not None else Path.cwd()),
    }
    trigger = payload.get("trigger")
    if trigger in ("manual", "auto"):
        params["trigger"] = trigger
    factory = daemon_client_factory or _default_daemon_client_factory
    try:
        with factory() as client:
            answer = client.call(_HOST_CONTEXT_METHOD, params)
    except Exception as exc:
        return HookResult(name=name, output=repr(exc))
    drift = ",".join(answer.get("drift", ())) or "none"
    head = f"{name} ok run={answer.get('run_ref')} drift={drift}"
    return HookResult(name=name, output=f"{head}\n{answer.get('restatement', '')}")


def host_context_restatement(results: Iterable[HookResult]) -> str | None:
    """Return the anchors the context hook restated for the host's model, if it did.

    Args:
        results: The results one SESSION_START dispatch returned.

    Returns:
        The restatement when :func:`record_host_context_boundary` stated a
        boundary; ``None`` when it was skipped or refused.
    """
    for result in results:
        head, _, restatement = result.output.partition("\n")
        if result.name == HOST_CONTEXT_HOOK and head.startswith(f"{HOST_CONTEXT_HOOK} ok "):
            return restatement or None
    return None


#: The hook that validates a host-lane skill invocation before its Run starts.
HOST_SKILL_HOOK: Final = "skill.invocation"

#: The host tool a model invokes a skill through.
HOST_SKILL_TOOL: Final = "Skill"

#: The namespace a packaged plugin's skills are invoked under.
_PLUGIN_SKILL_PREFIX: Final = "eawf:"


def host_skill_invocation(event: HookEvent) -> tuple[str, str, Lane] | None:
    """Return the skill a host event invokes, its argument text and its lane.

    The operator invokes a skill by submitting ``/<name> <arguments>``; a model
    invokes one through the host's skill tool. Either names the skill bare or
    under the plugin's namespace.

    Args:
        event: A USER_PROMPT_SUBMIT or PRE_TOOL_USE event.

    Returns:
        The skill name, the argument text and the lane; ``None`` when the event
        invokes no skill.
    """
    payload = _session_end_payload(event)
    if event.event_type is HookEventType.USER_PROMPT_SUBMIT:
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.startswith("/"):
            return None
        name, _, text = prompt[1:].strip().partition(" ")
        lane: Lane = "operator"
    else:
        tool_input = payload.get("tool_input")
        if payload.get("tool_name") != HOST_SKILL_TOOL or not isinstance(tool_input, dict):
            return None
        name, text, lane = (
            str(tool_input.get("skill", "")),
            str(tool_input.get("args", "")),
            "agent",
        )
    name = name.removeprefix("/").removeprefix(_PLUGIN_SKILL_PREFIX)
    return (name, text, lane) if name else None


def judge_host_skill_invocation(event: HookEvent) -> HookResult:
    """Refuse a host-lane skill invocation its argument schema or lanes do not admit.

    The host fires this before the skill's text reaches the model, so a refused
    invocation starts no Run. A skill the catalog does not ship is the host's own
    and is left alone.

    Args:
        event: A USER_PROMPT_SUBMIT or PRE_TOOL_USE event.

    Returns:
        A blocking :class:`HookResult` naming the refusal, or a non-blocking one
        naming the admitted invocation or why none was judged.
    """
    from eawf.workflow.skills.arguments import (
        InvocationRefusedError,
        check_invocation,
        parse_host_arguments,
    )
    from eawf.workflow.skills.catalog import SKILL_CATALOG

    name = HOST_SKILL_HOOK
    invocation = host_skill_invocation(event)
    entry = SKILL_CATALOG.entry(invocation[0]) if invocation is not None else None
    if invocation is None or entry is None:
        return HookResult(name=name, output=f"{name} skipped: no eawf skill invoked")
    _skill, text, lane = invocation
    try:
        check_invocation(entry, parse_host_arguments(entry, text), lane=lane)
    except InvocationRefusedError as exc:
        return HookResult(name=name, block=True, output=str(exc))
    return HookResult(name=name, output=f"{name} admitted {entry.invocation_name} lane={lane}")


def register_host_lane_hooks(
    runner: HookRunner,
    *,
    daemon_client_factory: DaemonClientFactory | None = None,
    repo_root: Path | None = None,
) -> None:
    """Register the compaction boundary hooks and the skill invocation check on *runner*.

    Args:
        runner: The runner dispatching host events.
        daemon_client_factory: Opens the daemon client; the default one when ``None``.
        repo_root: The repository the harness runs in; the process working directory
            when ``None``.
    """

    def _context_hook(event: HookEvent) -> HookResult:
        return record_host_context_boundary(
            event, daemon_client_factory=daemon_client_factory, repo_root=repo_root
        )

    for event_type in _HOST_CONTEXT_BOUNDARIES:
        runner.register(event_type, _context_hook, name=HOST_CONTEXT_HOOK)
    for event_type in (HookEventType.USER_PROMPT_SUBMIT, HookEventType.PRE_TOOL_USE):
        runner.register(event_type, judge_host_skill_invocation, name=HOST_SKILL_HOOK)


__all__ = [
    "HOST_CONTEXT_HOOK",
    "HOST_SKILL_HOOK",
    "HOST_SKILL_TOOL",
    "host_context_restatement",
    "host_skill_invocation",
    "judge_host_skill_invocation",
    "record_host_context_boundary",
    "register_host_lane_hooks",
]
