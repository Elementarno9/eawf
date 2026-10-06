"""``state.read`` -- the daemon's read of an epoch-1 ``state.json``.

The read takes no lock, mutates no ``ctx`` field and publishes nothing, so
the server may run it on a worker thread (see
:data:`eawf.runtime.daemon.server.OFFLOADED_METHODS`). Epoch-1 writes that
outlive the cutover go through :mod:`~eawf.runtime.daemon.methods.state_upkeep`.
"""

from __future__ import annotations

import logging
from typing import Any

from eawf.runtime.daemon.methods import MethodContext, register
from eawf.runtime.daemon.methods.state_context import (
    read_state,
    resolve_state_path,
    state_version,
)
from eawf.runtime.daemon.methods.state_models import ReadParams, ReadResult

logger = logging.getLogger(__name__)


@register("state.read")
async def read(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return the full ``state.json`` payload + digest version.

    Args:
        ctx: Server context — ``ctx.state_path`` is consulted only as a
            legacy fallback when *params* omits ``repo_root``.
        params: JSON-RPC params per :class:`ReadParams`.

    Returns:
        Dict matching :class:`ReadResult` — state payload as JSON-mode
        dict and the 16-hex-char digest.

    Raises:
        RuntimeError: when neither *params* nor the legacy ``ctx``
            fields resolve to a state path.
        ValueError: when the on-disk payload fails schema validation;
            the server maps this to ``-32602 invalid_params`` per
            :func:`eawf.runtime.daemon.server._process_frame`.
    """
    args = ReadParams.model_validate(params)
    state_path = resolve_state_path(repo_root=args.repo_root, ctx=ctx)
    _, payload = read_state(state_path)
    version = state_version(payload)
    return ReadResult(state=payload, version=version).model_dump(mode="json")


__all__ = ["read"]
