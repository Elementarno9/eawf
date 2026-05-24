"""Runtime-config write logic for ``eawf mcp install/remove``.

Public API:

- :func:`install_runtime_entry` — write one Eä-owned MCP entry into
  ``<target_dir>/.claude/settings.json``. Preserves every non-Eä
  ``mcpServers`` key byte-equal.
- :func:`remove_runtime_entry` — delete one Eä-owned MCP entry from
  the same file. Refuses to touch user-owned entries.
- :func:`list_runtime_entries` — read-only enumeration of the
  ``mcpServers`` map with owner annotations.

All three operate purely on disk — no state mutation. The CLI layer
combines a state-transaction (``cli/_mutation.py``) with one of these
calls so a crash between the state write and the runtime write is
detectable by ``eawf doctor`` (v0.1.1).

This module **does not** read ``os.environ`` for any env-ref name.
Env-ref tokens stay literal on the wire; the MCP launcher resolves
them at spawn time.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eawf.kernel.state.models import McpServer
from eawf.render._atomic import atomic_write_text
from eawf.runtime.mcp.env_ref import assert_no_expansion, render_env_block

logger = logging.getLogger(__name__)


# Top-level key inside .claude/settings.json holding the MCP map. Hard-
# coded by Claude Code's documented schema. Intentionally not a constant
# in :mod:`eawf.runtime.runtimes.claude.plugin_install` because that module owns
# a different namespace (``__eawf_managed``); the two never overlap.
_MCP_SERVERS_KEY: str = "mcpServers"

# Per-entry owner marker. When present and equal to ``"eawf"``, this
# entry is Eä-managed; the CLI may rewrite or delete it. Any other
# value (including absence) marks the entry as user-owned and
# off-limits for non-``--force`` operations. Mirrors the
# ``__eawf_managed`` namespace pattern in
# ``runtimes/claude/plugin_install.py:60`` but at per-entry granularity.
_OWNER_MARKER_KEY: str = "__eawf_owner"
_OWNER_MARKER_VALUE: str = "eawf"
_MANAGED_AT_KEY: str = "__eawf_managed_at"

# Hard-coded transport for v0.1. The Claude MCP schema also accepts
# ``"sse"`` and ``"http"``; deferred to v0.1.1 (spec §9). When the
# schema bumps, v0.1.1 adds a ``transport`` field to McpServer; the
# default for existing rows must remain ``"stdio"``.
_TRANSPORT_STDIO: str = "stdio"

# Stable timestamp default — matches ``plugin_install.py:65``. The
# CLI handler may override with ``datetime.now(UTC).isoformat()``;
# tests pin the value for byte-equality assertions.
_DEFAULT_TIMESTAMP: str = "1970-01-01T00:00:00+00:00"


class IntegrityViolation(Exception):  # noqa: N818 — canonical CLI error name (mirrors eawf.cli.errors)
    """Raised when a runtime config write would clobber a user entry.

    The CLI layer maps this to :class:`eawf.cli.errors.StateConflict`
    (``kind="IntegrityViolation"``, exit code 8). The library raises a
    plain Python error so it can be reused outside the Typer surface
    (e.g. by ``eawf doctor`` in v0.1.1).
    """


@dataclass(frozen=True)
class InstallEntryResult:
    """Outcome of one :func:`install_runtime_entry` call.

    Attributes:
        target_path: Absolute path of the runtime config file.
        action: ``"created" | "updated" | "unchanged"``.
        user_entries_preserved: Sorted list of ``mcpServers`` keys
            that were not Eä-owned. Each is byte-equal across the
            install (verified by integration test 5 in §7 of the
            wave spec).
    """

    target_path: Path
    action: str
    user_entries_preserved: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RemoveEntryResult:
    """Outcome of one :func:`remove_runtime_entry` call.

    Attributes:
        target_path: Absolute path of the runtime config file.
        action: ``"removed" | "absent"``. ``"absent"`` means the
            entry was not present on disk in the first place.
        user_entries_preserved: Sorted list of remaining non-Eä
            keys.
    """

    target_path: Path
    action: str
    user_entries_preserved: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RuntimeEntry:
    """One row produced by :func:`list_runtime_entries`.

    Attributes:
        id: ``mcpServers`` map key (the MCP id).
        owner: ``"eawf" | "user"``. The CLI ``--owner`` filter
            consumes this.
        command: ``mcpServers[id]["command"]`` if present, else ``""``.
        risk: For Eä-owned rows: not derivable from the runtime
            file (lives in state). Always ``""`` here; the CLI joins
            with state to fill it in for owner-eawf rows.
    """

    id: str
    owner: str
    command: str = ""
    risk: str = ""


# Supported runtimes for MCP install/remove. ``claude-agent-sdk`` is
# render-only (no on-disk install path); ``codex`` (D12) and ``opencode``
# (D13) emit into their respective adapter config files.
_SUPPORTED_RUNTIMES: tuple[str, ...] = (
    "claude",
    "claude-agent-sdk",
    "codex",
    "opencode",
)


def _validate_runtime(runtime: str) -> None:
    """Reject runtimes outside the v0.1 supported list."""
    if runtime not in _SUPPORTED_RUNTIMES:
        raise ValueError(
            f"unknown runtime {runtime!r}; expected one of {list(_SUPPORTED_RUNTIMES)}"
        )


def _settings_path(runtime: str, target_dir: Path) -> Path:
    """Resolve the on-disk runtime config for *runtime* under *target_dir*.

    ``claude-agent-sdk`` is render-only — it has no on-disk settings file
    and never participates in the install/remove paths. ``codex`` and
    ``opencode`` route to their respective adapter config files (D12 / D13).
    """
    _validate_runtime(runtime)
    if runtime == "claude-agent-sdk":
        raise NotImplementedError(
            f"runtime {runtime!r} has no on-disk settings file; "
            "dispatch via render_dispatch_envelope, not install_runtime_entry"
        )
    if runtime == "opencode":
        return target_dir / "opencode.json"
    if runtime == "codex":
        # Codex MCP entries live in the same TOML config file as the plugin
        # install renders. The TOML emit happens through the codex adapter
        # in v0.4; for v0.3 we sentinel the path so the doctor + install
        # surfaces stay coherent without yet writing TOML bytes.
        return target_dir / ".codex" / "config.toml"
    return target_dir / ".claude" / "settings.json"


def _read_settings(path: Path) -> dict[str, Any]:
    """Return the parsed top-level object at *path*, or ``{}``.

    Mirrors the patcher contract from ``plugin_install.py:220-223``.

    Raises:
        ValueError: When the file content is non-empty and not a JSON
            object, or when it fails to parse as JSON.
    """
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return {}
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"settings.json at {path} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(
            f"settings.json at {path} must be a JSON object; got {type(parsed).__name__}"
        )
    return dict(parsed)


def _is_eawf_owned(entry: Any) -> bool:
    """Return ``True`` if *entry* carries the per-row Eä owner marker."""
    return isinstance(entry, dict) and entry.get(_OWNER_MARKER_KEY) == _OWNER_MARKER_VALUE


def _build_entry_body(server: McpServer, *, timestamp: str) -> dict[str, Any]:
    """Return the per-entry dict Eä writes for *server*.

    Preconditions:
        - ``server.env_refs`` is already validated by the Pydantic
          model. :func:`render_env_block` re-validates defensively.

    Postconditions:
        - The ``env`` block contains literal ``${ENV:NAME}`` strings;
          :func:`assert_no_expansion` is invoked before return so a
          regression cannot leak a secret to disk.
    """
    env_block = render_env_block(server.env_refs)
    assert_no_expansion(env_block)
    return {
        "command": server.command,
        "args": list(server.args),
        "env": env_block,
        "transport": _TRANSPORT_STDIO,
        _OWNER_MARKER_KEY: _OWNER_MARKER_VALUE,
        _MANAGED_AT_KEY: timestamp,
    }


def _render_settings(parsed: dict[str, Any]) -> str:
    """Serialise *parsed* with the standard deterministic JSON shape.

    Two-space indent, sorted keys, trailing newline — matches
    ``plugin_install.py:226``.
    """
    return json.dumps(parsed, sort_keys=True, indent=2) + "\n"


def _classify(path: Path, payload: bytes) -> str:
    """Return ``"created" | "updated" | "unchanged"`` for *payload* at *path*."""
    if not path.exists():
        return "created"
    if path.read_bytes() == payload:
        return "unchanged"
    return "updated"


def _user_entry_keys(mcp_servers: dict[str, Any]) -> list[str]:
    """Return the sorted list of map keys whose value is not Eä-owned."""
    return sorted(k for k, v in mcp_servers.items() if not _is_eawf_owned(v))


def install_runtime_entry(
    *,
    server: McpServer,
    runtime: str,
    target_dir: Path,
    force: bool,
    timestamp: str = _DEFAULT_TIMESTAMP,
) -> InstallEntryResult:
    """Materialise *server* into the runtime config under *target_dir*.

    Procedure:

    1. Resolve the runtime config path (only ``claude`` in v0.1).
    2. Load existing settings.json (or start with ``{}``).
    3. If the target id is present and **not** Eä-owned, refuse with
       :class:`IntegrityViolation` unless *force* is set. (User
       opt-out: ``--force`` makes the user responsible for the
       overwrite, and the entry is rewritten as Eä-owned.)
    4. Build the per-entry body and re-assert no env-ref expansion
       leaked.
    5. Render deterministic JSON, atomically write back.
    6. Return the byte-equal preservation list so the CLI can echo it.

    Args:
        server: The state-side :class:`McpServer` to materialise.
            Must have ``owner == "eawf"`` — the CLI guards this; the
            installer does not re-check (defence-in-depth at the CLI
            layer is sufficient).
        runtime: Runtime identifier (``"claude"``).
        target_dir: Workspace root containing ``.claude/``.
        force: When ``True``, permits overwriting a pre-existing
            user-owned entry under the same id. Off by default.
        timestamp: ISO-8601 string written to ``__eawf_managed_at``.
            Defaults to the stable epoch sentinel for byte-equality
            tests; CLI passes ``datetime.now(UTC).isoformat()``.

    Returns:
        :class:`InstallEntryResult` with the action classification
        and the preservation list.

    Raises:
        ValueError: ``runtime`` is unsupported or settings.json is
            malformed (invalid JSON / non-object root).
        IntegrityViolation: Pre-existing user-owned entry present
            for the same id and *force* is ``False``.
    """
    settings_path = _settings_path(runtime, target_dir)
    parsed = _read_settings(settings_path)
    raw_servers = parsed.get(_MCP_SERVERS_KEY, {})
    if not isinstance(raw_servers, dict):
        raise ValueError(
            f"settings.json at {settings_path} has non-object {_MCP_SERVERS_KEY!r}; "
            f"got {type(raw_servers).__name__}"
        )
    mcp_servers: dict[str, Any] = dict(raw_servers)

    existing = mcp_servers.get(server.id)
    if existing is not None and not _is_eawf_owned(existing) and not force:
        raise IntegrityViolation(
            f"runtime config has user-owned entry for {server.id!r}; "
            "rename or remove before `eawf mcp install`, or pass --force"
        )

    new_body = _build_entry_body(server, timestamp=timestamp)
    mcp_servers[server.id] = new_body
    parsed[_MCP_SERVERS_KEY] = mcp_servers

    rendered = _render_settings(parsed)
    payload = rendered.encode("utf-8")
    action = _classify(settings_path, payload)
    if action != "unchanged":
        atomic_write_text(settings_path, rendered)
    user_entries = _user_entry_keys(mcp_servers)
    logger.info(
        f"mcp install_runtime_entry id={server.id} runtime={runtime} "
        f"path={settings_path} action={action} preserved={len(user_entries)}"
    )
    return InstallEntryResult(
        target_path=settings_path,
        action=action,
        user_entries_preserved=user_entries,
    )


def remove_runtime_entry(
    *,
    server_id: str,
    runtime: str,
    target_dir: Path,
    force: bool,
) -> RemoveEntryResult:
    """Delete *server_id* from the runtime config under *target_dir*.

    Procedure:

    1. Resolve the runtime config path.
    2. Load existing settings.json.
    3. If the entry is missing, return ``action="absent"``.
    4. If the entry is **not** Eä-owned, refuse with
       :class:`IntegrityViolation` unless *force* is set.
    5. Delete the entry. If ``mcpServers`` becomes empty, drop the
       key entirely.
    6. Render and atomically write back.

    Args:
        server_id: ``mcpServers`` map key to delete.
        runtime: Runtime identifier (``"claude"``).
        target_dir: Workspace root containing ``.claude/``.
        force: When ``True``, permits deleting a user-owned entry.
            Off by default — refuses with exit 8.

    Raises:
        ValueError: Runtime unsupported or settings.json malformed.
        IntegrityViolation: Pre-existing user-owned entry present
            for the same id and *force* is ``False``.
    """
    settings_path = _settings_path(runtime, target_dir)
    parsed = _read_settings(settings_path)
    raw_servers = parsed.get(_MCP_SERVERS_KEY, {})
    if not isinstance(raw_servers, dict):
        raise ValueError(
            f"settings.json at {settings_path} has non-object {_MCP_SERVERS_KEY!r}; "
            f"got {type(raw_servers).__name__}"
        )
    mcp_servers: dict[str, Any] = dict(raw_servers)

    existing = mcp_servers.get(server_id)
    if existing is None:
        return RemoveEntryResult(
            target_path=settings_path,
            action="absent",
            user_entries_preserved=_user_entry_keys(mcp_servers),
        )
    if not _is_eawf_owned(existing) and not force:
        raise IntegrityViolation(
            f"runtime config entry {server_id!r} is user-owned; "
            "refusing to remove. Pass --keep-runtime-entry to drop only the state row "
            "(the runtime entry stays untouched; remove it manually if needed)."
        )

    del mcp_servers[server_id]
    if mcp_servers:
        parsed[_MCP_SERVERS_KEY] = mcp_servers
    elif _MCP_SERVERS_KEY in parsed:
        del parsed[_MCP_SERVERS_KEY]

    rendered = _render_settings(parsed)
    payload = rendered.encode("utf-8")
    action = _classify(settings_path, payload)
    if action != "unchanged":
        atomic_write_text(settings_path, rendered)
    user_entries = _user_entry_keys(mcp_servers)
    logger.info(
        f"mcp remove_runtime_entry id={server_id} runtime={runtime} "
        f"path={settings_path} action={action} preserved={len(user_entries)}"
    )
    return RemoveEntryResult(
        target_path=settings_path,
        action="removed",
        user_entries_preserved=user_entries,
    )


def list_runtime_entries(*, runtime: str, target_dir: Path) -> list[RuntimeEntry]:
    """Return every ``mcpServers`` row under *target_dir* with owner annotation.

    No state mutation, no lock acquisition. Missing settings.json →
    empty list (the CLI surfaces a one-line warning text under
    ``--owner user|all``).

    Args:
        runtime: Runtime identifier (``"claude"``).
        target_dir: Workspace root containing ``.claude/``.

    Returns:
        Sorted-by-id list of :class:`RuntimeEntry` records.

    Raises:
        ValueError: Runtime unsupported or settings.json malformed.
    """
    settings_path = _settings_path(runtime, target_dir)
    if not settings_path.exists():
        return []
    parsed = _read_settings(settings_path)
    raw_servers = parsed.get(_MCP_SERVERS_KEY, {})
    if not isinstance(raw_servers, dict):
        raise ValueError(
            f"settings.json at {settings_path} has non-object {_MCP_SERVERS_KEY!r}; "
            f"got {type(raw_servers).__name__}"
        )
    rows: list[RuntimeEntry] = []
    for server_id, body in sorted(raw_servers.items()):
        owner = "eawf" if _is_eawf_owned(body) else "user"
        cmd = ""
        if isinstance(body, dict):
            raw_cmd = body.get("command", "")
            if isinstance(raw_cmd, str):
                cmd = raw_cmd
        rows.append(RuntimeEntry(id=server_id, owner=owner, command=cmd))
    return rows


__all__ = [
    "InstallEntryResult",
    "IntegrityViolation",
    "RemoveEntryResult",
    "RuntimeEntry",
    "install_runtime_entry",
    "list_runtime_entries",
    "remove_runtime_entry",
]
