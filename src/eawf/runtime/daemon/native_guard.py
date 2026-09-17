"""The daemon's fence in front of every native epoch-2 mutation.

A native mutator is registered through :func:`native_mutator` rather than
the plain method registry. The decorator registers a wrapper, not the
handler: the wrapper resolves the request's tree, asks the authority
resolver whether that tree is in epoch 2, and only then calls the
handler -- handing it the epoch-2 answer, whose fence-cleared target is
the only thing the handler can derive a write path from. A handler
therefore cannot be reached on an epoch-1 tree at all, and the refusal
happens before the handler has read a byte.

The refusal is one typed code, ``native_authority_required``, whatever
was missing. The resolver keeps the finer reason in its answer and the
message names it, but a caller routes on the code: every variant means
the same thing to it -- this tree is not a canary that has been
activated, so no native write reaches it.

The tree a request addresses is the repository root it names, or the
tree the daemon was bound to when it names none. A request that can name
neither is refused with the same code, because a tree nobody can point
at is not one anybody has shown to be in epoch 2.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, ClassVar, Final

from eawf.kernel.state.epoch2.authority import (
    NATIVE_AUTHORITY_REQUIRED,
    NativeAuthorityRequiredError,
    RootAuthority,
    require_native_authority,
)
from eawf.runtime.daemon.methods import DaemonValidationError, Handler, MethodContext, register

logger = logging.getLogger(__name__)


#: The request parameter a mutation names its repository root by, the
#: same key the epoch-1 mutation path routes on.
REPO_ROOT_PARAM: Final = "repo_root"

#: The directory under a repository root that the authority files live in.
EA_DIRNAME: Final = ".ea"

#: A native handler: the plain handler shape plus the epoch-2 answer the
#: fence resolved for the request's tree.
NativeHandler = Callable[[MethodContext, dict[str, Any], RootAuthority], Awaitable[dict[str, Any]]]


class NativeAuthorityRefusedError(DaemonValidationError):
    """The wire form of a native mutation refused for want of epoch 2.

    A :class:`~eawf.runtime.daemon.methods.DaemonValidationError`, so the
    server answers ``-32002`` and the client maps it to a validation
    failure, with the stable code leading the message.

    Attributes:
        code: The stable refusal code.
    """

    code: ClassVar[str] = NATIVE_AUTHORITY_REQUIRED


def native_root(ctx: MethodContext, params: dict[str, Any]) -> Path | None:
    """Return the tree root one native request addresses.

    Args:
        ctx: Server context, whose bound state path is the fallback.
        params: The request parameters.

    Returns:
        ``<repo_root>/.ea`` when the request names a repository root, the
        bound state file's directory when it does not, and ``None`` when
        neither is available or the named root is not a non-empty string.
    """
    if REPO_ROOT_PARAM in params:
        repo_root = params[REPO_ROOT_PARAM]
        if isinstance(repo_root, str) and repo_root:
            return Path(repo_root) / EA_DIRNAME
        return None
    if ctx.state_path is None:
        return None
    return Path(ctx.state_path).parent


def require_native_call(ctx: MethodContext, params: dict[str, Any]) -> RootAuthority:
    """Return the epoch-2 answer for one native request, or refuse it.

    Args:
        ctx: Server context.
        params: The request parameters.

    Returns:
        The epoch-2 answer for the request's tree.

    Raises:
        NativeAuthorityRefusedError: The request addresses no tree, or its
            tree resolves to epoch 1. Nothing has been written.
    """
    root = native_root(ctx, params)
    if root is None:
        raise NativeAuthorityRefusedError(
            f"validation_failed: {NATIVE_AUTHORITY_REQUIRED}: the request names no usable "
            f"{REPO_ROOT_PARAM} and the daemon is bound to no tree, so no tree can be shown "
            "to hold epoch-2 authority"
        )
    try:
        return require_native_authority(root)
    except NativeAuthorityRequiredError as error:
        logger.info(f"require_native_call refused root={root.name} gap={error.gap.value}")
        raise NativeAuthorityRefusedError(f"validation_failed: {error.code}: {error}") from error


def native_mutator(name: str) -> Callable[[NativeHandler], Handler]:
    """Register a native mutator behind the epoch-2 fence.

    Args:
        name: The dotted JSON-RPC method name.

    Returns:
        A decorator that registers the fenced wrapper under ``name`` and
        returns that wrapper, so the registered callable and the returned
        one are the same object.

    Raises:
        ValueError: ``name`` is already registered.
    """

    def decorator(handler: NativeHandler) -> Handler:
        async def fenced(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
            authority = require_native_call(ctx, params)
            return await handler(ctx, params, authority)

        fenced.__name__ = handler.__name__
        fenced.__doc__ = handler.__doc__
        return register(name)(fenced)

    return decorator


__all__ = [
    "EA_DIRNAME",
    "REPO_ROOT_PARAM",
    "NativeAuthorityRefusedError",
    "NativeHandler",
    "native_mutator",
    "native_root",
    "require_native_call",
]
