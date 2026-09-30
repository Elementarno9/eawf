"""The first run's workspace step, performed in the console through the daemon's verbs.

A first run lands where no workspace is registered, so nothing can attach. The workspace
step makes the tree the console stands in resolvable: it lists the repository under its
project code (``registry.update``) and files a workspace of that one project, keyed by
the code (``registry.workspace.create``), exactly what ``eawf repo register`` and
``eawf workspace add`` do. The daemon is the registry's one writer, so both writes go
through it; the step is previewed on a consequence card first and nothing is written
until the operator confirms it.

The step never scans: it registers the one root the console was launched in, and a
workspace that already claims the project is left as it is rather than duplicated.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

from eawf.platform.registry import (
    Registry,
    WorkspaceRecord,
    project_codes_at_root,
    read_registry,
)
from eawf.surfaces.tui.console.chrome import EntryState

logger = logging.getLogger(__name__)

#: The consequence card's ``kind`` for a first-run step, which no projection row carries.
ONBOARDING_KIND: Final = "onboarding"

#: The verb the workspace step's consequence card names.
WORKSPACE_VERB: Final = "register the workspace"

#: What the workspace step never does, stated on its card.
WORKSPACE_NOT: Final = "no migration is applied, no agent is started and no tree file is written"


class RegistryCaller(Protocol):
    """The part of the daemon client the step calls through."""

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send one request and return its result."""
        ...


@dataclass(frozen=True, slots=True, kw_only=True)
class FirstRun:
    """The tree a first run stands in, and the daemon link its step writes through.

    Attributes:
        repo_root: The repository root the console was launched in.
        code: The project code the step registers the root and its workspace under.
        registry_path: The machine registry the daemon writes.
        client: Opens a daemon link for the length of one step.
    """

    repo_root: Path
    code: str
    registry_path: Path
    client: Callable[[], AbstractContextManager[RegistryCaller]]

    def effects(self) -> str:
        """Return what confirming the workspace step changes, in one clause."""
        return (
            f"{self.repo_root.name} is registered as project {self.code} and workspace "
            f"{self.code} is filed with it as its one member"
        )

    def card(self) -> dict[str, str]:
        """Return the consequence card the workspace step is previewed on."""
        return {
            "verb": WORKSPACE_VERB,
            "id": self.code,
            "kind": ONBOARDING_KIND,
            "effects": self.effects(),
            "not": WORKSPACE_NOT,
        }


def project_code(state_path: Path) -> str | None:
    """Return the project code the tree's state records, or ``None`` when it records none.

    Args:
        state_path: The tree's ``state.json``.
    """
    try:
        payload = json.loads(state_path.read_text("utf-8"))
    except OSError, ValueError:
        return None
    project = payload.get("project") if isinstance(payload, dict) else None
    code = project.get("code") if isinstance(project, dict) else None
    return code if isinstance(code, str) and code else None


def _registry(path: Path) -> Registry:
    """Return the registry at ``path``, empty when the machine has none yet.

    Raises:
        RegistryReadError: the file exists but does not parse.
    """
    return read_registry(path) if path.is_file() else Registry()


def register_workspace(first_run: FirstRun) -> str:
    """Register the root and file its workspace through the daemon, skipping what exists.

    Args:
        first_run: The tree and the daemon link.

    Returns:
        What the step did, in one line.

    Raises:
        RegistryReadError: the registry exists but does not parse.
        DaemonRpcError: the daemon refused a write.
    """
    registry = _registry(first_run.registry_path)
    root = first_run.repo_root
    code = first_run.code
    listed = code in project_codes_at_root(registry, root)
    claimed = any(code in ws.member_project_codes for ws in registry.workspaces.values())
    target = str(first_run.registry_path)
    with first_run.client() as client:
        if not listed:
            client.call(
                "registry.update",
                {
                    "operation": "add",
                    "repo_id": code,
                    "fields": {"path": str(root), "title": root.name},
                    "registry_path": target,
                },
            )
        if not claimed:
            record = WorkspaceRecord(
                key=code, member_project_codes=frozenset({code}), home_project_code=code
            )
            client.call(
                "registry.workspace.create",
                {"record": record.model_dump(mode="json"), "registry_path": target},
            )
    logger.info(f"register_workspace code={code} listed={listed} claimed={claimed}")
    return f"workspace {code} registered · the next eawf ui attaches to it"


def registered_state(state: EntryState, code: str) -> EntryState:
    """Return the first-run ``state`` with its workspace step stated as done.

    Args:
        state: The onboarding state as drawn.
        code: The workspace the step registered.
    """
    rows = list(state.rows or ())
    if rows:
        rows[0] = (rows[0][0], f"registered · {code}", *rows[0][2:])
    return state.model_copy(update={"rows": tuple(rows), "commands": ("", *state.commands[1:])})


__all__ = [
    "ONBOARDING_KIND",
    "WORKSPACE_NOT",
    "WORKSPACE_VERB",
    "FirstRun",
    "RegistryCaller",
    "project_code",
    "register_workspace",
    "registered_state",
]
