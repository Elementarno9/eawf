"""The RPC scope a lifecycle skill drives the native delivery path through.

``/dispatch``, ``/integrate`` and ``/verify`` are the three skills that
drive Tasks, candidates and Batches over the daemon's native verbs. Each
one declares the closed set of JSON-RPC methods its catalog row grants,
and every call it makes goes through :meth:`RpcScope.call`, which refuses
a method outside that set *before* the transport is touched. A widened
body cannot quietly reach a verb its row never granted, and a skill that
copied a sibling's scope fails on the first call rather than at review.

The transport itself is a plain callable (:data:`RpcCaller`), so the
production path and a test double are the same seam: production binds
:func:`daemon_rpc_caller`, which opens the daemon socket per call and
translates the daemon's typed refusal into :class:`RpcRefusedError`; a test
binds a recorder and reads back the exact method names that reached it.

Terminal outcomes are the catalog's vocabulary, not the envelope's.
:func:`status_for` is the one place the two are reconciled, so three
skills cannot drift into three spellings of "a stale head blocks".
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

from eawf.surfaces.render.envelope import EnvelopeStatus

logger = logging.getLogger(__name__)


#: One JSON-RPC round trip: a method name and its params in, the result object out.
RpcCaller = Callable[[str, dict[str, Any]], dict[str, Any]]

#: The rendering every skill accepts on ``--output``.
OutputRendering = Literal["human", "json", "markdown"]

#: The prefix the daemon puts in front of a refusal it wants a caller to route on.
_REFUSAL_PREFIX: Final = "validation_failed: "

#: The code carried when a refusal message does not name one.
UNTYPED_REFUSAL_CODE: Final = "rpc_refused"

#: How each catalog terminal outcome lands on the five-value envelope status.
#: The three skills share one table so "a stale head blocks" is spelled once.
LIFECYCLE_OUTCOME_STATUS: Final[Mapping[str, EnvelopeStatus]] = {
    "candidate_ready": "ok",
    "frontier_empty": "ok",
    "needs_operator": "needs_user",
    "budget_exhausted": "blocked",
    "cancelled": "blocked",
    "shown": "ok",
    "sealed": "ok",
    "selected": "ok",
    "integrated": "ok",
    "conflicted": "blocked",
    "stale": "blocked",
    "passed": "ok",
    "failed": "failed",
    "unverified": "blocked",
    "blocked": "blocked",
}


class RpcScopeViolationError(RuntimeError):
    """A skill addressed a method its declared RPC allowlist does not grant.

    Attributes:
        skill: The canonical skill name that made the call.
        method: The method name that was refused.
        granted: The methods the skill's allowlist does grant.
    """

    def __init__(self, *, skill: str, method: str, granted: tuple[str, ...]) -> None:
        """Keep the refused method beside the set that was actually granted.

        Args:
            skill: The canonical skill name that made the call.
            method: The method name that was refused.
            granted: The allowlist, in declaration order.
        """
        self.skill = skill
        self.method = method
        self.granted = granted
        listed = ", ".join(granted) if granted else "(none)"
        super().__init__(f"{skill} may not call {method!r}; its RPC allowlist grants {listed}")


class RpcRefusedError(RuntimeError):
    """The daemon refused a call the allowlist did grant.

    Attributes:
        method: The method that was called.
        code: The stable refusal code a skill routes its terminal outcome on.
        detail: The sentence an operator reads.
    """

    def __init__(self, *, method: str, code: str, detail: str) -> None:
        """Keep the routable code beside the sentence.

        Args:
            method: The method that was called.
            code: The stable refusal code.
            detail: One sentence naming what was refused and why.
        """
        self.method = method
        self.code = code
        self.detail = detail
        super().__init__(f"{method} refused: {code}: {detail}")


def refusal_code(message: str) -> str:
    """Return the stable code a daemon refusal message carries.

    The daemon spells a routable refusal ``validation_failed: <code>:
    <sentence>``. Anything else is a fault rather than a rule, and reads
    back as :data:`UNTYPED_REFUSAL_CODE` so a caller still has one field
    to branch on.

    Args:
        message: The refusal message as the daemon wrote it.

    Returns:
        The refusal code, or :data:`UNTYPED_REFUSAL_CODE`.
    """
    if not message.startswith(_REFUSAL_PREFIX):
        return UNTYPED_REFUSAL_CODE
    remainder = message[len(_REFUSAL_PREFIX) :]
    code, separator, _detail = remainder.partition(":")
    stripped = code.strip()
    if not separator or not stripped:
        return UNTYPED_REFUSAL_CODE
    return stripped


@dataclass(frozen=True)
class RpcScope:
    """The closed set of JSON-RPC methods one lifecycle skill may address.

    Attributes:
        skill: The canonical skill name the scope belongs to.
        methods: The allowlist, in the order the catalog row states it.
            A method absent from the tuple is refused.
    """

    skill: str
    methods: tuple[str, ...]

    def grants(self, method: str) -> bool:
        """Return whether *method* is inside this skill's allowlist.

        Args:
            method: A dotted JSON-RPC method name.

        Returns:
            ``True`` when the allowlist names *method*.
        """
        return method in self.methods

    def call(self, caller: RpcCaller, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """Send one allowlisted call through *caller* and return its result.

        The allowlist is checked before *caller* is touched, so a method
        outside the scope never reaches the transport and never reaches
        the daemon.

        Args:
            caller: The transport seam. Production binds
                :func:`daemon_rpc_caller`; a test binds a recorder.
            method: The dotted JSON-RPC method name to address.
            params: The request params.

        Returns:
            The JSON-RPC result object.

        Raises:
            RpcScopeViolationError: *method* is outside :attr:`methods`.
            RpcRefusedError: The daemon refused the call.
        """
        if not self.grants(method):
            raise RpcScopeViolationError(skill=self.skill, method=method, granted=self.methods)
        logger.debug(f"rpc_scope_call skill={self.skill!r} method={method!r}")
        return caller(method, dict(params))


def daemon_rpc_caller() -> RpcCaller:
    """Return the production transport: one daemon round trip per call.

    The daemon client is imported lazily so importing a lifecycle skill
    costs no socket module and no daemon spawn; a skill that never makes
    a call never opens one.

    Returns:
        A :data:`RpcCaller` that raises :class:`RpcRefusedError` when the
        daemon answers with an error envelope.
    """

    def call(method: str, params: dict[str, Any]) -> dict[str, Any]:
        from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError

        try:
            with DaemonClient() as client:
                return client.call(method, params)
        except DaemonRpcError as error:
            raise RpcRefusedError(
                method=method,
                code=refusal_code(error.message),
                detail=error.message,
            ) from error

    return call


def row_keys(read: dict[str, Any], *, status: str | None = None) -> list[str]:
    """Return the keys of a read model's rows, optionally filtered by status.

    A projection row states its lifecycle status as a truth field, so an
    unknown status is an absence rather than a blank; a status filter
    therefore keeps only rows whose value is known and equal.

    Args:
        read: A route projection as the daemon returned it.
        status: Keep only rows whose status is known and equal to this.
            ``None`` keeps every row.

    Returns:
        The row keys, in the order the projection rendered them.
    """
    keys: list[str] = []
    for row in read.get("rows", ()):
        truth = row.get("status", {})
        if status is not None and (truth.get("state") != "known" or truth.get("value") != status):
            continue
        key = str(row.get("key", ""))
        if key:
            keys.append(key)
    return keys


def status_for(outcome: str) -> EnvelopeStatus:
    """Return the envelope status one catalog terminal outcome lands on.

    Args:
        outcome: A terminal outcome named by a lifecycle skill's catalog row.

    Returns:
        The envelope status the outcome projects onto.

    Raises:
        KeyError: *outcome* is not a lifecycle terminal outcome. A skill
            that invents one is a bug rather than a new status.
    """
    try:
        return LIFECYCLE_OUTCOME_STATUS[outcome]
    except KeyError as error:
        raise KeyError(f"unknown lifecycle terminal outcome: {outcome!r}") from error


__all__ = [
    "LIFECYCLE_OUTCOME_STATUS",
    "UNTYPED_REFUSAL_CODE",
    "OutputRendering",
    "RpcCaller",
    "RpcRefusedError",
    "RpcScope",
    "RpcScopeViolationError",
    "daemon_rpc_caller",
    "refusal_code",
    "row_keys",
    "status_for",
]
