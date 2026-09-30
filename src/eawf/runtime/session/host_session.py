"""The vendor session a command runs inside, read from its host's environment.

A root Run is started by a command an agent runs inside its host session,
so that session is the one whose transcript counts the Run's work. The host
names it only to the processes it spawns: Claude Code exports its session id
to every shell command it runs. The command that starts a Run reads it here
and presents it on the start edge, which is how a root Run comes to carry a
:class:`~eawf.kernel.state.epoch2.measurement.VendorSessionRef` at all.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Final

from eawf.kernel.state.epoch2.measurement import VendorSessionRef

logger = logging.getLogger(__name__)

#: The environment variable each host names its session in, and the harness
#: the session belongs to. Capture reads transcripts and sidecars only for
#: ``claude-code``, so it is the one host listed.
HOST_SESSION_VARIABLES: Final[Mapping[str, str]] = {"CLAUDE_CODE_SESSION_ID": "claude-code"}


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

    A session the caller named explicitly is kept: an agent may start a Run
    on behalf of a session other than its own.

    Args:
        updates: The start edge's caller-supplied updates.
        environ: The starting command's process environment.

    Returns:
        *updates*, joined by ``vendor_session`` when they carry none and the
        host named a session.
    """
    if updates.get("vendor_session") is not None:
        return dict(updates)
    session = host_vendor_session(environ)
    if session is None:
        return dict(updates)
    logger.debug(f"with_host_session harness={session.harness}")
    return {**updates, "vendor_session": session.model_dump(mode="json")}


__all__ = ["HOST_SESSION_VARIABLES", "host_vendor_session", "with_host_session"]
