"""Daemon-proxy predicates for mutating verbs.

* :func:`_proxy_enabled` reads ``daemon.proxy_enabled`` (and the
  ``EAWF_DAEMONLESS=1`` hatch) through the layered configuration.
* :func:`_daemon_reachable` probes the daemon with ``daemon.ping``.

A proxying verb consults both before it sends a mutation to the daemon.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _proxy_enabled(workspace: Path | None) -> bool:
    """Return True when the merged config enables daemon proxying.

    Resolves ``daemon.proxy_enabled`` through the layered-config merge
    (built-in → global → workspace → repo → local → env). Default since
    P24-W10 is ``True`` — the V1 daemonless carve-out runs only when
    the merged config explicitly opts out OR the process has
    ``EAWF_DAEMONLESS=1`` set.
    """
    import os

    if os.environ.get("EAWF_DAEMONLESS", "") == "1":
        return False
    from eawf.kernel.config.layered import merge_config

    repo = workspace if workspace is not None else Path.cwd()
    try:
        merged, _ = merge_config(workspace=workspace, repo=repo)
    except (OSError, ValueError, KeyError) as exc:
        logger.debug(f"_proxy_enabled False reason={exc!s}")
        return False
    daemon_cfg = merged.get("daemon")
    if not isinstance(daemon_cfg, dict):
        return False
    value = daemon_cfg.get("proxy_enabled", False)
    return bool(value)


def _daemon_reachable(runtime_dir: Path | None = None) -> bool:
    """Return True when the daemon is up and answering ``daemon.ping``.

    Used by the proxying verbs to decide between the proxy path and the
    daemonless fallback (per V1 carve-out: reads + recovery shell + CI).
    Probes via
    :class:`~eawf.surfaces.cli._daemon_client.DaemonClient`
    so the helper exercises the same wire format as the production
    path; transport errors map to a False return without raising so
    the caller can fall through to the in-process path on a clean
    "daemon down" verdict.
    """
    from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError

    try:
        with DaemonClient(runtime_dir=runtime_dir) as client:
            client.call("daemon.ping")
    except (DaemonRpcError, RuntimeError, OSError, TimeoutError, NotImplementedError) as exc:
        logger.debug(f"_daemon_reachable False reason={exc!s}")
        return False
    return True
