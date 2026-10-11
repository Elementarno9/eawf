"""How a native Run's launcher gets its per-Run MCP configuration.

:func:`~eawf.runtime.mcp.semantic_stdio.run_server_config_for` already
renders a lane's configuration from a :class:`~eawf.runtime.mcp.semantic_stdio.RunServerBinding`,
and :func:`~eawf.runtime.mcp.semantic_stdio.materialize_run_server_config`
already writes it to disk -- neither was ever called from the native launch
path, so a native Run's provider started with no MCP server entry and no
route to its granted semantic tools.

This module is the seam that calls them: it builds the binding a native
launch can supply on its own (the sealed capsule and the repository root,
both already on the :class:`~eawf.runtime.runtimes.adapter.NativeLaunchRequest`),
renders the lane's configuration, and refuses the launch outright when the
rendered configuration does not actually register the Run's server --
because a provider started on a configuration that dropped the entry would
run silently without its semantic tool catalog rather than being told so.

The capsule and the binding are written under the Run's own leased
workspace rather than a daemon-side path, because that is the one
directory both filesystem jails (bwrap's read-write bind, seatbelt's
``file-write*`` allow) leave writable AND readable to the jailed child --
anywhere else, a jailed provider could not read its own MCP configuration.
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
from pathlib import Path

from eawf.runtime.daemon.runtime_dir import runtime_dir
from eawf.runtime.mcp.grant import assert_capsule_within_intersection
from eawf.runtime.mcp.semantic_stdio import (
    RunServerBinding,
    RunServerConfig,
    ServerTableError,
    materialize_run_server_config,
    run_server_config_for,
)
from eawf.runtime.runtimes.adapter import NativeLaunchRequest, RuntimeSpawnError
from eawf.runtime.workspace.lease import MCP_ARTIFACT_DIRNAME

logger = logging.getLogger(__name__)

#: The subdirectory of the per-Run artifact directory a native Run's
#: temporary files go to, in place of the host temp areas its jail closes.
RUN_TMP_DIRNAME = "tmp"

#: The variables naming the temporary directory, set to the Run's own.
RUN_TMP_ENV_KEYS: tuple[str, ...] = ("TMPDIR", "TMP", "TEMP")

#: The exact per-Run server-name spellings a rendered configuration must
#: carry for the entry to be considered registered: claude's mcp.json keys
#: the server under the hyphenated :data:`~eawf.runtime.mcp.semantic_stdio.SERVER_NAME`,
#: codex's ``-c`` overrides key it under the dotted, underscored
#: :data:`~eawf.runtime.runtimes.codex.mcp_config.SERVER_CONFIG_KEY`. A
#: configuration naming neither spelling registers no server, whatever else
#: it contains.
_ENTRY_MARKERS: tuple[str, ...] = ("eawf-semantic", "eawf_semantic")

#: The server command every lane's per-Run config points its provider at.
#: The path to the written binding is appended by the caller. The server
#: runs on the daemon's own interpreter, named absolutely, because the
#: provider resolves a bare ``eawf`` on its scrubbed ``PATH``, where it
#: finds another install (or none) rather than the release serving the Run.
#: ``-I`` keeps the Run's workspace, which is the server's working
#: directory, off ``sys.path``, so a checkout cannot shadow the package.
_SERVE_COMMAND_PREFIX: tuple[str, ...] = (
    sys.executable,
    "-I",
    "-m",
    "eawf",
    "mcp",
    "serve",
    "--binding",
)


def native_run_server_config(request: NativeLaunchRequest, *, runtime_id: str) -> RunServerConfig:
    """Build, materialize, and verify one lane's per-Run MCP configuration.

    Writes the sealed capsule and a binding naming it under the Run's own
    workspace, renders *runtime_id*'s configuration from that binding, and
    materializes any files the lane needs written to disk. The binding
    routes calls to the repository root, not the workspace: the daemon
    admits a semantic call only for a tree holding a generation, and a
    leased worktree holds none.

    Args:
        request: The native launch request the config is built for.
        runtime_id: The dispatch lane the config is rendered for (e.g.
            ``"claude-code"`` or ``"codex"``).

    Returns:
        The rendered, already-materialized configuration.

    Raises:
        RuntimeSpawnError: The capsule reaches a tool outside the
            intersection of the Run's role, task and certification; no
            configuration could be rendered for *runtime_id*; or the
            configuration that was rendered does not register the Run's
            server, so starting the child on it would run without the
            semantic tool catalog rather than refuse.
    """
    spec = request.spec
    try:
        assert_capsule_within_intersection(spec=spec, capsule=request.capsule)
    except ValueError as error:
        raise RuntimeSpawnError(str(error)) from error
    artifact_dir = request.workspace / MCP_ARTIFACT_DIRNAME
    artifact_dir.mkdir(parents=True, exist_ok=True)
    capsule_path = artifact_dir / "capsule.json"
    capsule_path.write_text(
        json.dumps(request.capsule.model_dump(mode="json"), sort_keys=True), encoding="utf-8"
    )
    binding_path = artifact_dir / "binding.json"
    binding = RunServerBinding(
        run_ref=spec.run_ref,
        repo_root=str(request.repo_root),
        capsule_path=str(capsule_path),
        # The provider's environment carries no runtime pin, and resolving
        # the default from the workspace names another tree's daemon.
        runtime_dir=str(runtime_dir()),
    )
    binding_path.write_text(binding.model_dump_json(), encoding="utf-8")
    try:
        config = run_server_config_for(
            runtime_id,
            binding=binding,
            config_dir=artifact_dir,
            server_command=(*_SERVE_COMMAND_PREFIX, str(binding_path)),
            capsule=request.capsule,
        )
    except (ServerTableError, ValueError) as error:
        raise RuntimeSpawnError(
            f"mcp_config_unbuildable: {runtime_id!r} raised {error}; run={spec.run_ref!r}"
        ) from error
    _assert_entry_registered(config, run_ref=str(spec.run_ref))
    materialize_run_server_config(config)
    logger.info(
        f"native_run_server_config runtime={runtime_id!r} tools={len(config.exposed_tools)}"
    )
    return config


def _assert_entry_registered(config: RunServerConfig, *, run_ref: str) -> None:
    """Refuse a configuration that does not actually register the server.

    Args:
        config: The rendered lane configuration.
        run_ref: The Run the configuration was rendered for, named in the
            refusal so an operator reading the launch failure knows which
            Run was refused.

    Raises:
        RuntimeSpawnError: Neither the written files nor the argv flags
            name the per-Run server, so a provider spawned on this
            configuration would start without ever hearing about it.
    """
    haystack = " ".join((*config.files.values(), *config.argv_flags))
    if any(marker in haystack for marker in _ENTRY_MARKERS):
        return
    raise RuntimeSpawnError(
        f"mcp_entry_dropped: the rendered {config.runtime_id!r} configuration names no "
        f"per-run server, so the launch is refused rather than started without semantic "
        f"tools; run={run_ref!r}"
    )


def prepare_run_tmpdir(run_dir: Path) -> Path:
    """Create a native Run's temporary directory, empty, under *run_dir*.

    It is emptied on every launch so a retry of the same Run starts without
    what an earlier attempt left there.

    Args:
        run_dir: The Run's per-Run artifact directory.

    Returns:
        The temporary directory.

    Raises:
        OSError: The directory could not be removed or created.
    """
    tmpdir = run_dir / RUN_TMP_DIRNAME
    if tmpdir.exists():
        shutil.rmtree(tmpdir)
    tmpdir.mkdir(parents=True, mode=0o700)
    return tmpdir


__all__ = [
    "MCP_ARTIFACT_DIRNAME",
    "RUN_TMP_DIRNAME",
    "RUN_TMP_ENV_KEYS",
    "native_run_server_config",
    "prepare_run_tmpdir",
]
