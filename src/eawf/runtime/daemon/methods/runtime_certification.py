"""``runtime.certification.certify``: probe an installed runtime and record the outcome.

The command-line twin of the daemon's background certification. It probes the
binary installed now, after any background probe of the tree that is running,
and answers with the row it recorded: a certification, or a quarantine naming
what the probe did not find. A runtime that cannot be probed at all, because
its binary is absent or reports no version, records nothing and is refused.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.native_guard import native_mutator, native_params
from eawf.runtime.daemon.runtime_certifier import RuntimeNotProbedError, certifier_for
from eawf.runtime.runtimes.capabilities import RUNTIME_IDS

logger = logging.getLogger(__name__)

#: The dotted JSON-RPC name ``eawf runtime certify`` sends.
RUNTIME_CERTIFY_METHOD: Final = "runtime.certification.certify"


class _CertifyParams(BaseModel):
    """Wire contract of ``runtime.certification.certify``."""

    model_config = ConfigDict(extra="forbid")

    runtime: str


@native_mutator(RUNTIME_CERTIFY_METHOD)
async def _certify_runtime(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Probe the installed runtime the request names and return the recorded row.

    Raises:
        DaemonValidationError: The runtime is not one the probe knows, or it
            could not be probed; nothing was recorded.
    """
    args = native_params(_CertifyParams, params)
    if args.runtime not in RUNTIME_IDS:
        raise DaemonValidationError(
            f"validation_failed: runtime_unknown: {args.runtime!r} is not one of "
            f"{', '.join(RUNTIME_IDS)}"
        )
    certifier = certifier_for(authority.root)
    try:
        row = await asyncio.to_thread(certifier.certify, args.runtime)
    except RuntimeNotProbedError as error:
        raise DaemonValidationError(f"validation_failed: {error}") from error
    logger.info(
        f"_certify_runtime runtime={args.runtime!r} version={row.harness_version!r} "
        f"outcome={row.outcome}"
    )
    return row.model_dump(mode="json")


__all__ = ["RUNTIME_CERTIFY_METHOD"]
