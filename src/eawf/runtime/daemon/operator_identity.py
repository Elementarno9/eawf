"""Refusing the operator's identity on a request an agent session sent.

An operator principal (an ``OP-`` key) is the one identity whose acts --
approving a plan, accepting a Milestone -- mean a human decided. An agent
session that names it as ``--actor`` records a human decision nobody
made, so the daemon refuses such a request before any handler runs.

The daemon is a separate process and cannot see the caller's
environment, which is the only place an agent session is visible: Claude
Code exports ``CLAUDECODE`` into every shell its tools run, and Codex
exports ``CODEX_THREAD_ID`` into every command it runs. A Run eawf
dispatches is one of those runtimes, so it is covered by the same
markers. The client therefore reads the environment and carries what it
found as :data:`AGENT_SESSION_PARAM`; the server takes the key off the
params before dispatch, so no verb's closed params model ever sees it.

This is a guard against an agent recording operator acts by habit, not a
security boundary: a caller that strips its own environment is not an
agent session as far as this check can tell.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, ClassVar, Final

from eawf.kernel.state.epoch2.base import OPERATOR_PRINCIPAL_PREFIX
from eawf.runtime.daemon.methods import DaemonValidationError

logger = logging.getLogger(__name__)

#: The request parameter a client names the agent runtime it runs in by.
AGENT_SESSION_PARAM: Final = "agent_session"

#: Each environment variable an agent runtime exports into the commands it
#: runs, and the runtime it marks. Checked in order; the first set one wins.
AGENT_SESSION_ENV_MARKERS: Final[tuple[tuple[str, str], ...]] = (
    ("CLAUDECODE", "claude"),
    ("CODEX_THREAD_ID", "codex"),
)

#: The refusal code an operator identity named from an agent session earns.
OPERATOR_IDENTITY_REFUSED: Final = "operator_identity_refused"


class OperatorIdentityRefusedError(DaemonValidationError):
    """An agent session named an operator principal on a request.

    Attributes:
        code: The stable refusal code.
    """

    code: ClassVar[str] = OPERATOR_IDENTITY_REFUSED


def agent_session_runtime(env: Mapping[str, str]) -> str | None:
    """Return the agent runtime *env* belongs to, if any.

    Args:
        env: The process environment of the caller.

    Returns:
        ``"claude"`` or ``"codex"`` when that runtime's marker is set to a
        non-empty value, ``None`` from an operator's own terminal.
    """
    for variable, runtime in AGENT_SESSION_ENV_MARKERS:
        if env.get(variable):
            return runtime
    return None


def names_acting_principal(params: Mapping[str, Any]) -> bool:
    """Return whether *params* name an actor or approver the check reads.

    The client carries the session marker only on such a request. Every
    other request, the daemon's own ``daemon.*`` methods among them, goes
    out as before, so a daemon from an earlier release whose closed params
    models know no marker still answers the version ping and a stop.

    Args:
        params: The request params about to go on the wire.

    Returns:
        ``True`` when *params* carry an ``actor`` or ``approved_by`` key.
    """
    return "actor" in params or "approved_by" in params


def _operator_identities(params: Mapping[str, Any]) -> list[str]:
    """Return every operator principal *params* names as the one acting.

    ``actor`` is who every mutating verb attributes the act to; a plan
    approval also names its approver apart from the actor, and that
    approver is the human the approval is recorded for.
    """
    named: list[Any] = [params.get("actor")]
    approved_by = params.get("approved_by")
    if isinstance(approved_by, Mapping):
        named.append(approved_by.get("principal_id"))
    return [
        key for key in named if isinstance(key, str) and key.startswith(OPERATOR_PRINCIPAL_PREFIX)
    ]


def admit_operator_identity(method: str, params: Mapping[str, Any]) -> dict[str, Any]:
    """Return *params* less the session marker, or refuse an agent acting as the operator.

    Args:
        method: The dotted JSON-RPC name, for the refusal message.
        params: The request params as decoded off the wire.

    Returns:
        A copy of *params* without :data:`AGENT_SESSION_PARAM`.

    Raises:
        DaemonValidationError: The marker is present but not a non-empty
            string.
        OperatorIdentityRefusedError: The request comes from an agent
            session and names an operator principal as actor or approver.
    """
    admitted = {key: value for key, value in params.items() if key != AGENT_SESSION_PARAM}
    if AGENT_SESSION_PARAM not in params:
        return admitted
    runtime = params[AGENT_SESSION_PARAM]
    if not isinstance(runtime, str) or not runtime:
        raise DaemonValidationError(
            f"validation_failed: schema_validation_failed: check {AGENT_SESSION_PARAM}"
        )
    operators = _operator_identities(params)
    if not operators:
        return admitted
    logger.info(f"admit_operator_identity refused method={method} runtime={runtime}")
    raise OperatorIdentityRefusedError(
        f"validation_failed: {OPERATOR_IDENTITY_REFUSED}: {method} names operator principal "
        f"{', '.join(operators)} from a {runtime} agent session; only the operator records "
        "an operator act, from their own terminal, and an agent acts under its own service "
        "principal"
    )


__all__ = [
    "AGENT_SESSION_ENV_MARKERS",
    "AGENT_SESSION_PARAM",
    "OPERATOR_IDENTITY_REFUSED",
    "OperatorIdentityRefusedError",
    "admit_operator_identity",
    "agent_session_runtime",
    "names_acting_principal",
]
