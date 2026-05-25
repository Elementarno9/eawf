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
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eawf.kernel.state.models import McpServer
from eawf.runtime.mcp.env_ref import assert_no_expansion, render_env_block
from eawf.surfaces.render._atomic import atomic_write_text

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


class IntegrityViolation(Exception):  # noqa: N818 — canonical CLI error name (mirrors eawf.surfaces.cli.errors)
    """Raised when a runtime config write would clobber a user entry.

    The CLI layer maps this to :class:`eawf.surfaces.cli.errors.StateConflict`
    (``kind="IntegrityViolation"``, exit code 8). The library raises a
    plain Python error so it can be reused outside the Typer surface
    (e.g. by ``eawf doctor`` in v0.1.1).
    """


class VerifyFailure(Exception):  # noqa: N818 — canonical CLI error name (mirrors eawf.surfaces.cli.errors)
    """Raised when a post-write read-back does not match the intended grant.

    The installer re-reads every runtime config file immediately after it
    writes and asserts the just-materialised entry parses back to the
    command / args / env it intended to grant. A mismatch means the write
    silently corrupted (atomic-write race, encoding bug, or a TOML/JSON
    round-trip defect) — surfacing it loudly is the whole point of the
    emit-and-verify hardening. The CLI maps this to
    :class:`eawf.surfaces.cli.errors.StateConflict` (exit code 8).
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
        # install renders. ``_install_codex_entry`` / ``_remove_codex_entry``
        # splice an ``[mcp_servers.<id>]`` table into a marker-wrapped region
        # of this file, preserving the plugin block and user TOML verbatim.
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


# ---------------------------------------------------------------------------
# Post-write verification (emit-and-verify)
# ---------------------------------------------------------------------------


def _grant_fields(body: dict[str, Any]) -> tuple[str, list[str], dict[str, str]]:
    """Return the (command, args, env) grant triple from a parsed *body*.

    Verification compares only the grant content — the launcher command,
    its argv tail, and the literal env-ref block. Bookkeeping keys
    (``transport``, ``__eawf_owner``, ``__eawf_managed_at``) are excluded
    so a timestamp difference never trips the verify guard.
    """
    return (
        str(body.get("command", "")),
        [str(a) for a in (body.get("args") or [])],
        {str(k): str(v) for k, v in (body.get("env") or {}).items()},
    )


def _verify_json_entry(path: Path, server: McpServer, intended: dict[str, Any]) -> None:
    """Re-read *path* and assert ``mcpServers[id]`` matches *intended*.

    Raises:
        VerifyFailure: The on-disk entry is missing or its grant triple
            diverges from what the installer intended to write.
    """
    parsed = _read_settings(path)
    raw_servers = parsed.get(_MCP_SERVERS_KEY, {})
    written = raw_servers.get(server.id) if isinstance(raw_servers, dict) else None
    if not isinstance(written, dict) or _grant_fields(written) != _grant_fields(intended):
        raise VerifyFailure(
            f"post-write verify failed for {server.id!r} in {path}; "
            "on-disk entry does not match the intended grant"
        )


# ---------------------------------------------------------------------------
# Codex TOML emit — writes real ``[mcp_servers.<id>]`` tables into
# ``.codex/config.toml``
# ---------------------------------------------------------------------------

# Codex declares MCP servers as ``[mcp_servers.<id>]`` tables (stdio
# transport implied by command/args/env). Eä owns a single marker-wrapped
# region holding every owner=eawf table; user-authored TOML outside the
# markers is preserved verbatim. The marker text is distinct from the
# plugin installer's ``__eawf_managed`` block (which carries
# ``[plugins.eawf] enabled = true`` in the same file) so the two managed
# regions never collide.
_CODEX_MCP_TABLE_KEY: str = "mcp_servers"
_CODEX_MCP_BEGIN: str = "# ---- __eawf_mcp begin ----"
_CODEX_MCP_END: str = "# ---- __eawf_mcp end ----"
_CODEX_MCP_BLOCK_RE = re.compile(
    rf"(?ms)^{re.escape(_CODEX_MCP_BEGIN)}.*?^{re.escape(_CODEX_MCP_END)}\n?"
)


def _toml_basic_string(value: str) -> str:
    """Render *value* as a fully-escaped TOML basic string."""
    out = ['"']
    for ch in value:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\b":
            out.append("\\b")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\f":
            out.append("\\f")
        elif ch == "\r":
            out.append("\\r")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _toml_array(items: list[str]) -> str:
    """Render a list of strings as a single-line TOML array."""
    return "[" + ", ".join(_toml_basic_string(i) for i in items) + "]"


def _toml_inline_table(mapping: dict[str, str]) -> str:
    """Render a str->str mapping as a single-line TOML inline table."""
    if not mapping:
        return "{}"
    body = ", ".join(f"{key} = {_toml_basic_string(val)}" for key, val in mapping.items())
    return "{ " + body + " }"


def _codex_body_from_server(server: McpServer, *, timestamp: str) -> dict[str, Any]:
    """Return the Eä-owned codex table body for *server*.

    Postconditions:
        - The ``env`` block holds literal ``${ENV:NAME}`` strings;
          :func:`assert_no_expansion` runs before return so a regression
          cannot leak a secret to disk (parity with
          :func:`_build_entry_body`).
    """
    env_block = render_env_block(server.env_refs)
    assert_no_expansion(env_block)
    return {
        "command": server.command,
        "args": list(server.args),
        "env": env_block,
        _OWNER_MARKER_KEY: _OWNER_MARKER_VALUE,
        _MANAGED_AT_KEY: timestamp,
    }


def _render_codex_table(server_id: str, body: dict[str, Any]) -> list[str]:
    """Render one ``[mcp_servers."<id>"]`` table as TOML lines.

    The id segment is always quoted so ids containing ``.`` / ``-`` stay
    a single key rather than a dotted path.
    """
    env = {str(k): str(v) for k, v in (body.get("env") or {}).items()}
    args = [str(a) for a in (body.get("args") or [])]
    return [
        f"[{_CODEX_MCP_TABLE_KEY}.{_toml_basic_string(server_id)}]",
        f"command = {_toml_basic_string(str(body['command']))}",
        f"args = {_toml_array(args)}",
        f"env = {_toml_inline_table(env)}",
        f"{_OWNER_MARKER_KEY} = {_toml_basic_string(_OWNER_MARKER_VALUE)}",
        f"{_MANAGED_AT_KEY} = {_toml_basic_string(str(body[_MANAGED_AT_KEY]))}",
    ]


def _render_codex_mcp_block(bodies: dict[str, dict[str, Any]]) -> str:
    """Render the marker-wrapped region holding every eawf-owned table.

    Tables are emitted in sorted id order so re-installs are byte-stable.
    """
    lines: list[str] = [_CODEX_MCP_BEGIN]
    for sid in sorted(bodies):
        lines.extend(_render_codex_table(sid, bodies[sid]))
    lines.append(_CODEX_MCP_END)
    return "\n".join(lines) + "\n"


def _read_codex_config(path: Path) -> tuple[str, dict[str, Any]]:
    """Return ``(raw_text, parsed)`` for the codex config at *path*.

    Missing file → ``("", {})``. Empty file → ``(raw, {})``.

    Raises:
        ValueError: The file content is non-empty and not valid TOML.
    """
    if not path.exists():
        return "", {}
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return raw, {}
    try:
        parsed = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"config.toml at {path} is not valid TOML: {exc}") from exc
    return raw, parsed


def _codex_servers(parsed: dict[str, Any]) -> dict[str, Any]:
    """Return the ``mcp_servers`` table from *parsed* (empty when absent).

    Raises:
        ValueError: ``mcp_servers`` is present but not a table.
    """
    raw = parsed.get(_CODEX_MCP_TABLE_KEY, {})
    if not isinstance(raw, dict):
        raise ValueError(
            f"config.toml {_CODEX_MCP_TABLE_KEY!r} is not a table; got {type(raw).__name__}"
        )
    return raw


def _codex_eawf_bodies(servers: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return the owner=eawf tables, normalised to the rendered key shape."""
    out: dict[str, dict[str, Any]] = {}
    for sid, body in servers.items():
        if isinstance(body, dict) and body.get(_OWNER_MARKER_KEY) == _OWNER_MARKER_VALUE:
            out[sid] = {
                "command": body.get("command", ""),
                "args": list(body.get("args", []) or []),
                "env": dict(body.get("env", {}) or {}),
                _OWNER_MARKER_KEY: _OWNER_MARKER_VALUE,
                _MANAGED_AT_KEY: body.get(_MANAGED_AT_KEY, _DEFAULT_TIMESTAMP),
            }
    return out


def _codex_user_keys(servers: dict[str, Any]) -> list[str]:
    """Return sorted ids of non-eawf-owned ``mcp_servers`` tables."""
    return sorted(
        sid
        for sid, body in servers.items()
        if not (isinstance(body, dict) and body.get(_OWNER_MARKER_KEY) == _OWNER_MARKER_VALUE)
    )


def _splice_codex_block(raw: str, bodies: dict[str, dict[str, Any]]) -> str:
    """Return *raw* with the eawf mcp marker block replaced / inserted / dropped.

    Everything outside the marker region (user TOML, the plugin
    ``__eawf_managed`` block) is preserved verbatim.
    """
    stripped = _CODEX_MCP_BLOCK_RE.sub("", raw)
    if not bodies:
        trimmed = stripped.rstrip("\n")
        return (trimmed + "\n") if trimmed else ""
    block = _render_codex_mcp_block(bodies)
    prefix = stripped.rstrip("\n")
    return (prefix + "\n\n" + block) if prefix else block


def _verify_codex_entry(path: Path, server: McpServer) -> None:
    """Re-read *path* and assert ``mcp_servers[id]`` matches the grant.

    Raises:
        VerifyFailure: The on-disk codex table is missing or its grant
            triple diverges from the intended command / args / env.
    """
    _raw, parsed = _read_codex_config(path)
    written = _codex_servers(parsed).get(server.id)
    intended = {
        "command": server.command,
        "args": list(server.args),
        "env": render_env_block(server.env_refs),
    }
    if not isinstance(written, dict) or _grant_fields(written) != _grant_fields(intended):
        raise VerifyFailure(
            f"post-write verify failed for {server.id!r} in {path}; "
            "on-disk codex table does not match the intended grant"
        )


def _install_codex_entry(
    *, server: McpServer, target_dir: Path, force: bool, timestamp: str
) -> InstallEntryResult:
    """Materialise *server* into ``.codex/config.toml`` (TOML splice path)."""
    config_path = _settings_path("codex", target_dir)
    raw, parsed = _read_codex_config(config_path)
    servers = _codex_servers(parsed)

    existing = servers.get(server.id)
    if existing is not None and not (
        isinstance(existing, dict) and existing.get(_OWNER_MARKER_KEY) == _OWNER_MARKER_VALUE
    ):
        if not force:
            raise IntegrityViolation(
                f"codex config has user-owned mcp_servers entry for {server.id!r}; "
                "rename or remove before `eawf mcp install`, or pass --force"
            )
        raise ValueError(
            f"cannot force-overwrite user-owned codex mcp_servers.{server.id} via splice; "
            "remove the table from config.toml manually first"
        )

    bodies = _codex_eawf_bodies(servers)
    bodies[server.id] = _codex_body_from_server(server, timestamp=timestamp)
    new_text = _splice_codex_block(raw, bodies)
    payload = new_text.encode("utf-8")
    action = _classify(config_path, payload)
    if action != "unchanged":
        atomic_write_text(config_path, new_text)
    _verify_codex_entry(config_path, server)
    user_entries = _codex_user_keys(_codex_servers(_read_codex_config(config_path)[1]))
    logger.info(
        f"mcp install_runtime_entry id={server.id} runtime=codex "
        f"path={config_path} action={action} preserved={len(user_entries)}"
    )
    return InstallEntryResult(
        target_path=config_path, action=action, user_entries_preserved=user_entries
    )


def _list_codex_entries(target_dir: Path) -> list[RuntimeEntry]:
    """Enumerate ``mcp_servers`` tables in ``.codex/config.toml`` with owners."""
    config_path = _settings_path("codex", target_dir)
    if not config_path.exists():
        return []
    _raw, parsed = _read_codex_config(config_path)
    rows: list[RuntimeEntry] = []
    for server_id, body in sorted(_codex_servers(parsed).items()):
        owner = "eawf" if _is_eawf_owned(body) else "user"
        cmd = ""
        if isinstance(body, dict):
            raw_cmd = body.get("command", "")
            if isinstance(raw_cmd, str):
                cmd = raw_cmd
        rows.append(RuntimeEntry(id=server_id, owner=owner, command=cmd))
    return rows


def _remove_codex_entry(*, server_id: str, target_dir: Path, force: bool) -> RemoveEntryResult:
    """Delete *server_id* from ``.codex/config.toml`` (TOML splice path)."""
    config_path = _settings_path("codex", target_dir)
    raw, parsed = _read_codex_config(config_path)
    servers = _codex_servers(parsed)

    existing = servers.get(server_id)
    if existing is None:
        return RemoveEntryResult(
            target_path=config_path,
            action="absent",
            user_entries_preserved=_codex_user_keys(servers),
        )
    if not (isinstance(existing, dict) and existing.get(_OWNER_MARKER_KEY) == _OWNER_MARKER_VALUE):
        if not force:
            raise IntegrityViolation(
                f"codex config entry {server_id!r} is user-owned; refusing to remove. "
                "Pass --keep-runtime-entry to drop only the state row "
                "(the runtime entry stays untouched; remove it manually if needed)."
            )
        raise ValueError(
            f"cannot force-remove user-owned codex mcp_servers.{server_id} via splice; "
            "remove the table from config.toml manually"
        )

    bodies = _codex_eawf_bodies(servers)
    bodies.pop(server_id, None)
    new_text = _splice_codex_block(raw, bodies)
    payload = new_text.encode("utf-8")
    action = _classify(config_path, payload)
    if action != "unchanged":
        atomic_write_text(config_path, new_text)
    user_entries = _codex_user_keys(_codex_servers(_read_codex_config(config_path)[1]))
    logger.info(
        f"mcp remove_runtime_entry id={server_id} runtime=codex "
        f"path={config_path} action={action} preserved={len(user_entries)}"
    )
    return RemoveEntryResult(
        target_path=config_path, action="removed", user_entries_preserved=user_entries
    )


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
        VerifyFailure: The post-write read-back does not match the
            intended grant.
    """
    if runtime == "codex":
        return _install_codex_entry(
            server=server, target_dir=target_dir, force=force, timestamp=timestamp
        )
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
    _verify_json_entry(settings_path, server, new_body)
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
    if runtime == "codex":
        return _remove_codex_entry(server_id=server_id, target_dir=target_dir, force=force)
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
    if runtime == "codex":
        return _list_codex_entries(target_dir)
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


def runtime_config_path(runtime: str, target_dir: Path) -> Path:
    """Return the on-disk runtime config path Eä writes for *runtime*.

    Public resolver so CLI surfaces can name the target file (in confirm
    prompts and ``--owner user`` notes) without duplicating the
    per-runtime routing table.

    Raises:
        ValueError: ``runtime`` is unsupported.
        NotImplementedError: ``runtime`` is render-only
            (``claude-agent-sdk``).
    """
    return _settings_path(runtime, target_dir)


__all__ = [
    "InstallEntryResult",
    "IntegrityViolation",
    "RemoveEntryResult",
    "RuntimeEntry",
    "VerifyFailure",
    "install_runtime_entry",
    "list_runtime_entries",
    "remove_runtime_entry",
    "runtime_config_path",
]
