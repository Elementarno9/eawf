"""``/blitz`` skill scaffold + recursion guard (P14-W11 / D22).

``/blitz`` is the auto-invoked follow-up skill the ``/research`` body
spawns when the residual-unknowns count exceeds 1. It chains additional
research passes until the unknowns drain or the depth cap fires.

Recursion guard
---------------

The cap is exposed via the ``EAWF_BLITZ_DEPTH`` environment variable so
operators can override the depth from the shell without editing source.
Default cap: ``8``. The :class:`BlitzRecursionExhausted` exception is
raised by :func:`bump_depth` when the cap fires — callers map it to a
``status="blocked"`` envelope.

Trigger heuristic
-----------------

:func:`should_auto_invoke` returns ``True`` when the supplied
``residual_unknowns`` count is greater than 1; ``/research`` bodies
consult this helper at the end of their probe pass to decide whether
to register a follow-up wave under the blitz skill.
"""

from __future__ import annotations

import logging
import os
from typing import Final

logger = logging.getLogger(__name__)


_DEPTH_ENV_VAR: Final[str] = "EAWF_BLITZ_DEPTH"
_DEFAULT_DEPTH_CAP: Final[int] = 8
_DEPTH_COUNTER_ENV_VAR: Final[str] = "EAWF_BLITZ_DEPTH_COUNTER"


class BlitzRecursionExhausted(Exception):
    """Raised when the blitz depth cap fires."""


def depth_cap() -> int:
    """Return the active depth cap.

    Reads :data:`EAWF_BLITZ_DEPTH` from the environment; falls back to
    ``8`` when the variable is unset or unparseable.
    """
    raw = os.environ.get(_DEPTH_ENV_VAR)
    if raw is None:
        return _DEFAULT_DEPTH_CAP
    try:
        parsed = int(raw)
    except ValueError:
        logger.warning(
            f"EAWF_BLITZ_DEPTH invalid value {raw!r}; using default {_DEFAULT_DEPTH_CAP}"
        )
        return _DEFAULT_DEPTH_CAP
    return max(0, parsed)


def current_depth() -> int:
    """Return the current blitz recursion depth (per-process counter).

    ``EAWF_BLITZ_DEPTH_COUNTER`` is the only writable surface for tests
    + nested invocations. Production callers should always go through
    :func:`bump_depth` to keep the counter coherent.
    """
    raw = os.environ.get(_DEPTH_COUNTER_ENV_VAR, "0")
    try:
        return int(raw)
    except ValueError:
        return 0


def reset_depth() -> None:
    """Clear the blitz depth counter; used by tests + outer dispatchers."""
    os.environ.pop(_DEPTH_COUNTER_ENV_VAR, None)


def bump_depth() -> int:
    """Increment the blitz depth counter; raise when the cap fires.

    Returns:
        The new depth value.

    Raises:
        BlitzRecursionExhausted: Incrementing past :func:`depth_cap`
            would loop forever; the caller must back off to the human.
    """
    cap = depth_cap()
    new_depth = current_depth() + 1
    if new_depth > cap:
        raise BlitzRecursionExhausted(f"blitz recursion exhausted: depth={new_depth} > cap={cap}")
    os.environ[_DEPTH_COUNTER_ENV_VAR] = str(new_depth)
    return new_depth


def should_auto_invoke(*, residual_unknowns: int) -> bool:
    """Return ``True`` when ``/research`` should auto-spawn a blitz follow-up."""
    return residual_unknowns > 1


__all__ = [
    "BlitzRecursionExhausted",
    "bump_depth",
    "current_depth",
    "depth_cap",
    "reset_depth",
    "should_auto_invoke",
]
