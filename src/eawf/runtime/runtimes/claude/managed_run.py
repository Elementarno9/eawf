"""Isolate a managed ``claude`` child from the operator's own configuration.

A Claude Code child finds its configuration home through
``CLAUDE_CONFIG_DIR``, falling back to ``~/.claude`` under ``HOME``. That
home holds the operator's hooks, installed plugins, user settings, user
``CLAUDE.md``, auto-memory and user-scoped MCP servers, and a managed Run
that inherited it would execute the operator's hooks and read the
operator's instructions. So a managed Run gets its own configuration home
under the Run's root, created empty on every launch, and three further
switches close the sources a configuration home does not cover:

- ``--setting-sources ""`` loads no user, project or local settings file,
  which is where project hooks and plugin enablement live in the
  workspace checkout;
- ``CLAUDE_CODE_DISABLE_CLAUDE_MDS`` stops project, nested and user
  ``CLAUDE.md`` discovery;
- ``CLAUDE_CODE_DISABLE_AUTO_MEMORY`` stops auto-memory reads and writes.

MCP servers are closed by the lane's ``--strict-mcp-config``, which the
per-Run MCP configuration already passes, and the approved set is restored
explicitly by the same flags: the per-Run server through ``--mcp-config``,
the permission mode and the tool denials through their own argv flags.

Authentication is the one thing the child still needs from the operator,
and nothing is copied for it. Claude Code resolves auth in this order: an
``ANTHROPIC_API_KEY`` / ``ANTHROPIC_AUTH_TOKEN`` variable, a
``CLAUDE_CODE_OAUTH_TOKEN`` variable (both admitted by the environment
scrub), then the stored login. The stored login lives in the macOS login
keychain under a service name derived from the secure-storage directory,
or in ``.credentials.json`` inside that directory elsewhere; the
secure-storage directory defaults to the configuration home.
``CLAUDE_SECURESTORAGE_CONFIG_DIR`` pins it to the operator's own
directory -- the empty value meaning the default ``~/.claude`` -- so the
child reads the operator's existing login in place while every other
configuration read goes to the clean home.

Every ``claude`` child eawf starts is managed: the daemon's jury, research,
fleet-repair and agent dispatches and the native Run launcher all spawn a
headless ``claude -p`` turn, and eawf never starts the operator's own
interactive session. A native Run keeps its home under the Run's root for
the Run's lifetime; any other spawn gets a per-spawn home from
:func:`ephemeral_managed_isolation`, which is removed when the turn ends.
Those spawns carry no per-Run MCP server, so they close MCP with
``--strict-mcp-config`` and no ``--mcp-config``, which loads none.
"""

from __future__ import annotations

import dataclasses
import logging
import shutil
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from eawf.runtime.mcp.native_launch import MCP_ARTIFACT_DIRNAME

logger = logging.getLogger(__name__)

#: The directory, under the Run's per-Run artifact directory, that the
#: child is given as its configuration home.
MANAGED_CONFIG_DIRNAME: Final = "claude-config"

#: The variable naming the child's configuration home.
CONFIG_DIR_ENV: Final = "CLAUDE_CONFIG_DIR"

#: The variable naming where the child's stored login is read from.
SECURE_STORAGE_ENV: Final = "CLAUDE_SECURESTORAGE_CONFIG_DIR"

#: The instruction-discovery switches every managed child is started with.
INSTRUCTION_SUPPRESSION_ENV: Final[Mapping[str, str]] = {
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
}

#: The argv that loads no settings file, so no project or user hook runs.
SETTING_SOURCES_FLAGS: Final[tuple[str, ...]] = ("--setting-sources", "")

#: The flag that drops every MCP server not named by ``--mcp-config``.
STRICT_MCP_FLAG: Final = "--strict-mcp-config"

#: The prefix of a per-spawn directory under the spawn's working directory.
EPHEMERAL_RUN_PREFIX: Final = "spawn-"


@dataclass(frozen=True, slots=True)
class ManagedClaudeIsolation:
    """What a managed ``claude`` child is started with to stay isolated.

    Attributes:
        config_dir: The child's own, freshly created configuration home.
        env: The variables layered over the scrubbed child environment.
        argv_flags: The flags added ahead of the lane's own arguments.
    """

    config_dir: Path
    env: Mapping[str, str]
    argv_flags: tuple[str, ...]


def _secure_storage_dir(operator_env: Mapping[str, str]) -> str:
    """Return the directory the operator's stored login is read from.

    An explicit secure-storage pin wins, then the operator's own
    configuration home; the empty string selects the host default,
    ``~/.claude``, which is where a login made without either lives.
    """
    if SECURE_STORAGE_ENV in operator_env:
        return operator_env[SECURE_STORAGE_ENV]
    return operator_env.get(CONFIG_DIR_ENV, "")


def prepare_managed_isolation(
    run_dir: Path, *, operator_env: Mapping[str, str]
) -> ManagedClaudeIsolation:
    """Create a clean configuration home under *run_dir* and describe it.

    The home is emptied and recreated on every launch, so a retry of the
    same Run starts from the same empty home rather than from whatever an
    earlier attempt left there.

    Args:
        run_dir: The Run's own artifact directory inside its leased
            workspace -- the directory both filesystem jails leave
            writable to the child.
        operator_env: The environment the daemon was started with, read
            only to locate the operator's stored login.

    Returns:
        The configuration home, the environment overlay and the flags.

    Raises:
        OSError: The configuration home could not be removed or created.
    """
    config_dir = run_dir / MANAGED_CONFIG_DIRNAME
    if config_dir.exists():
        shutil.rmtree(config_dir)
    config_dir.mkdir(parents=True, mode=0o700)
    env = {
        CONFIG_DIR_ENV: str(config_dir),
        SECURE_STORAGE_ENV: _secure_storage_dir(operator_env),
        **INSTRUCTION_SUPPRESSION_ENV,
    }
    logger.info(f"prepare_managed_isolation config_dir={config_dir!s}")
    return ManagedClaudeIsolation(config_dir=config_dir, env=env, argv_flags=SETTING_SOURCES_FLAGS)


@contextmanager
def ephemeral_managed_isolation(
    cwd: Path, *, operator_env: Mapping[str, str]
) -> Iterator[ManagedClaudeIsolation]:
    """Isolate one headless spawn that has no Run root of its own.

    The home sits under *cwd* because the working directory is the one
    place both filesystem jails leave writable to the child, and a
    per-spawn name keeps concurrent spawns in the same directory (a jury's
    panelists) from emptying each other's home. The directory is removed
    when the spawn ends, whether it succeeded or raised.

    Args:
        cwd: The child's working directory.
        operator_env: The environment the daemon was started with, read
            only to locate the operator's stored login.

    Yields:
        The isolation of :func:`prepare_managed_isolation`, with MCP closed.

    Raises:
        FileNotFoundError: *cwd* is not an existing directory; the home is
            not created, so a missing working directory is not made on the
            child's behalf.
        OSError: The configuration home could not be created.
    """
    if not cwd.is_dir():
        raise FileNotFoundError(f"spawn working directory is not a directory: {cwd}")
    run_dir = cwd / MCP_ARTIFACT_DIRNAME / f"{EPHEMERAL_RUN_PREFIX}{uuid.uuid4().hex}"
    isolation = prepare_managed_isolation(run_dir, operator_env=operator_env)
    try:
        yield dataclasses.replace(isolation, argv_flags=(*isolation.argv_flags, STRICT_MCP_FLAG))
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


__all__ = [
    "CONFIG_DIR_ENV",
    "EPHEMERAL_RUN_PREFIX",
    "INSTRUCTION_SUPPRESSION_ENV",
    "MANAGED_CONFIG_DIRNAME",
    "SECURE_STORAGE_ENV",
    "SETTING_SOURCES_FLAGS",
    "STRICT_MCP_FLAG",
    "ManagedClaudeIsolation",
    "ephemeral_managed_isolation",
    "prepare_managed_isolation",
]
