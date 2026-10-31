"""``runtime.certification.certify``: certify an installed runtime and record the outcome.

The command-line twin of the daemon's background certification. It probes the
binary installed now, after any background probe of the tree that is running,
and answers with the row it recorded: a certification, or a quarantine naming
what the probe did not find. A runtime that cannot be probed at all, because
its binary is absent or reports no version, records nothing and is refused.

Naming a canary Task walks the rest: the containment canary is dispatched as a
real Run under that Task and the certify stage follows, so the answer is the
row the whole walk recorded. That costs the canary's tokens, which is why only
an operator's request walks it and the background probe never does.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from eawf.kernel.runtime.certification import CertifiedVersionRange
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import PrincipalKey
from eawf.kernel.store.envelope import Envelope
from eawf.runtime.daemon.epoch2_recovery import publish_projection
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.projection import document_path
from eawf.runtime.daemon.methods.task_dispatch import task_urn
from eawf.runtime.daemon.native_guard import native_mutator, native_params
from eawf.runtime.daemon.runtime_certifier import (
    RuntimeNotProbedError,
    certifier_for,
    walk_certification,
)
from eawf.runtime.runtimes.capabilities import RUNTIME_IDS

logger = logging.getLogger(__name__)

#: The dotted JSON-RPC name ``eawf runtime certify`` sends.
RUNTIME_CERTIFY_METHOD: Final = "runtime.certification.certify"


class _CertifyParams(BaseModel):
    """Wire contract of ``runtime.certification.certify``.

    Attributes:
        runtime: The runtime to certify.
        task: The Task a canary Run is placed under, by key or URN; ``None``
            runs the probe stage alone.
        actor: Who asked, which the canary Run is recorded against; named
            exactly when *task* is.
        version_range: The versions the walk's driver certification stands
            for once each passes a re-probe; named only with *task*, since
            only a canary walk keeps a driver certification.
    """

    model_config = ConfigDict(extra="forbid")

    runtime: str
    task: str | None = None
    actor: PrincipalKey | None = None
    version_range: CertifiedVersionRange | None = None


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
    if (args.task is None) != (args.actor is None):
        raise DaemonValidationError(
            "validation_failed: schema_validation_failed: a canary walk names both task and actor"
        )
    if args.version_range is not None and args.task is None:
        raise DaemonValidationError(
            "validation_failed: schema_validation_failed: a version range is certified only "
            "by a canary walk, which names a task"
        )
    try:
        if args.task is None or args.actor is None:
            row = await asyncio.to_thread(certifier_for(authority.root).certify, args.runtime)
        else:
            task = await asyncio.to_thread(task_urn, document_path(authority), args.task)

            def publish(envelope: Envelope) -> bool:
                return publish_projection(ctx.bus, envelope)

            row = await walk_certification(
                ctx.native_root_context(authority.root),
                args.runtime,
                task=task,
                actor=args.actor,
                publish=publish,
                version_range=args.version_range,
            )
    except RuntimeNotProbedError as error:
        raise DaemonValidationError(f"validation_failed: {error}") from error
    logger.info(
        f"_certify_runtime runtime={args.runtime!r} version={row.harness_version!r} "
        f"outcome={row.outcome}"
    )
    return row.model_dump(mode="json")


__all__ = ["RUNTIME_CERTIFY_METHOD"]
