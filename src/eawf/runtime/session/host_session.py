"""The vendor session a command runs inside, read from its host's environment.

A root Run is started by a command an agent runs inside its host session,
so that session is the one whose transcript counts the Run's work. The host
names it only to the processes it spawns: Claude Code exports its session id
to every shell command it runs. The command that starts a Run reads it here
and presents it on the start edge, which is how a root Run comes to carry a
:class:`~eawf.kernel.state.epoch2.measurement.VendorSessionRef` at all.

The same environment says who serves the host's model, which the daemon
cannot see from its own: Claude Code routes to Bedrock or Vertex only when
the operator's environment says so, and to Anthropic otherwise unless a
base URL points it at a gateway whose upstream nobody states.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Final

from eawf.kernel.state.epoch2.measurement import VendorSessionRef
from eawf.kernel.state.epoch2.run import RunRuntimeTuple

logger = logging.getLogger(__name__)

#: The environment variable each host names its session in, and the harness
#: the session belongs to. Capture reads transcripts and sidecars only for
#: ``claude-code``, so it is the one host listed.
HOST_SESSION_VARIABLES: Final[Mapping[str, str]] = {"CLAUDE_CODE_SESSION_ID": "claude-code"}

#: The switches that route Claude Code to a cloud provider, and who that is.
_CLAUDE_CODE_PROVIDER_SWITCHES: Final[Mapping[str, str]] = {
    "CLAUDE_CODE_USE_BEDROCK": "amazon-bedrock",
    "CLAUDE_CODE_USE_VERTEX": "google-vertex",
}

#: The variable that points Claude Code at a gateway rather than Anthropic.
_CLAUDE_CODE_BASE_URL: Final = "ANTHROPIC_BASE_URL"

#: The values a switch is off at. Claude Code reads any other value as on.
_OFF: Final = frozenset({"", "0", "false", "no", "off"})


def claude_code_provider(environ: Mapping[str, str]) -> str | None:
    """Return who serves the model a Claude Code process with *environ* runs.

    Args:
        environ: The Claude Code process's environment.

    Returns:
        The provider, or ``None`` when a base URL points at a gateway whose
        upstream the environment does not state.
    """
    for variable, provider in _CLAUDE_CODE_PROVIDER_SWITCHES.items():
        if environ.get(variable, "").strip().lower() not in _OFF:
            return provider
    if environ.get(_CLAUDE_CODE_BASE_URL, "").strip():
        return None
    return "anthropic"


def host_vendor_session(environ: Mapping[str, str]) -> VendorSessionRef | None:
    """Return the vendor session *environ* says the command runs inside.

    Args:
        environ: The command's process environment.

    Returns:
        The session, its id already hashed, or ``None`` when no host named
        one.
    """
    for variable, harness in HOST_SESSION_VARIABLES.items():
        session_id = environ.get(variable, "").strip()
        if session_id:
            return VendorSessionRef(harness=harness, session_digest=session_id)
    return None


def with_host_session(updates: Mapping[str, Any], environ: Mapping[str, str]) -> dict[str, Any]:
    """Return a Run start's *updates*, presenting the host session they lack.

    A session or runtime tuple the caller named explicitly is kept: an agent
    may start a Run on behalf of a session other than its own. The tuple
    presented names the host's harness and provider only; the daemon reads
    the version and model off the session's transcript.

    Args:
        updates: The start edge's caller-supplied updates.
        environ: The starting command's process environment.

    Returns:
        *updates*, joined by ``vendor_session`` and ``runtime_tuple`` when
        they carry none and the host named a session.
    """
    if updates.get("vendor_session") is not None:
        return dict(updates)
    session = host_vendor_session(environ)
    if session is None:
        return dict(updates)
    logger.debug(f"with_host_session harness={session.harness}")
    joined = {**updates, "vendor_session": session.model_dump(mode="json")}
    if updates.get("runtime_tuple") is None:
        runtime = RunRuntimeTuple(harness=session.harness, provider=claude_code_provider(environ))
        joined["runtime_tuple"] = runtime.model_dump(mode="json", exclude_none=True)
    return joined


__all__ = [
    "HOST_SESSION_VARIABLES",
    "claude_code_provider",
    "host_vendor_session",
    "with_host_session",
]
