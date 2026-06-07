"""Pure installer for the global Claude Code statusline integration (W41).

The ``eawf cc statusline install`` wizard (the CLI surface in
:mod:`eawf.surfaces.cli.commands.statusline`) wires the Eä statusline into the
operator's global Claude Code setup by patching the ``statusLine`` key of
``~/.claude/settings.json`` to invoke ``eawf cc statusline``. This module owns
only the pure, side-effect-scoped install logic -- path resolution, the
settings patch, and the write -- so the CLI handler stays a thin dispatch
layer and the patch is unit-testable without a live terminal.

The patch is namespace-narrow: only the ``statusLine`` key is replaced (or
inserted); every other key in ``settings.json`` is preserved verbatim, and
the result is rendered as deterministic JSON (sorted keys, 2-space indent,
trailing newline) so a re-run is byte-stable.

Public surface:

- :func:`global_settings_path` -- resolve ``~/.claude/settings.json``.
- :func:`build_statusline_command` -- the ``statusLine`` command payload.
- :func:`patch_settings` -- patch a settings mapping, return the new mapping.
- :func:`render_settings` -- deterministic-JSON-encode a settings mapping.
- :func:`install_statusline` -- read / patch / write the settings file.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_STATUSLINE_KEY: str = "statusLine"
"""The Claude Code settings key that names the statusline command."""

_STATUSLINE_TYPE: str = "command"
"""Claude reads the statusline from a shell command (vs a static string)."""

_STATUSLINE_COMMAND: str = "eawf cc statusline"
"""The command Claude runs each redraw to fetch one statusline line."""


def global_settings_path() -> Path:
    """Return the operator-global Claude settings file path.

    Returns:
        ``~/.claude/settings.json``.
    """
    return Path.home() / ".claude" / "settings.json"


def build_statusline_command() -> dict[str, str]:
    """Return the ``statusLine`` payload that invokes the Eä statusline.

    Returns:
        The Claude ``statusLine`` object: a ``command``-type entry whose
        command is ``eawf cc statusline``.
    """
    return {"type": _STATUSLINE_TYPE, "command": _STATUSLINE_COMMAND}


def read_settings(path: Path) -> dict[str, Any]:
    """Read *path* as a JSON settings object.

    Args:
        path: Settings file to read. A missing or empty file yields an empty
            mapping so a fresh install starts from a clean object.

    Returns:
        The parsed settings mapping (empty when the file is absent / empty).

    Raises:
        ValueError: When the file content is not valid JSON, or parses to
            something other than a JSON object.
    """
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return {}
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"settings is not valid json: {path!r}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"settings must be a json object: {path!r}")
    return dict(parsed)


def patch_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *settings* with the ``statusLine`` key patched in.

    Only the ``statusLine`` key is replaced (or inserted); every other key
    is preserved verbatim so the patch never writes outside its own field.

    Args:
        settings: The current settings mapping.

    Returns:
        A new mapping with the statusline command applied.
    """
    patched = dict(settings)
    patched[_STATUSLINE_KEY] = build_statusline_command()
    return patched


def is_already_installed(settings: dict[str, Any]) -> bool:
    """Return ``True`` when *settings* already names the Eä statusline command.

    Args:
        settings: The current settings mapping.

    Returns:
        ``True`` when the ``statusLine`` entry matches the command this
        installer would write, else ``False``.
    """
    return settings.get(_STATUSLINE_KEY) == build_statusline_command()


def render_settings(settings: dict[str, Any]) -> str:
    """Encode *settings* as deterministic JSON (sorted keys, 2-space indent).

    Args:
        settings: The settings mapping to encode.

    Returns:
        The JSON text with a trailing newline so a re-run is byte-stable.
    """
    return json.dumps(settings, sort_keys=True, indent=2) + "\n"


def install_statusline(path: Path) -> dict[str, Any]:
    """Patch *path*'s settings to invoke the Eä statusline and write it back.

    Reads the current settings (empty on a fresh file), patches the
    ``statusLine`` key, and writes the deterministic-JSON result, creating
    the parent directory when missing.

    Args:
        path: Settings file to patch (typically :func:`global_settings_path`).

    Returns:
        The patched settings mapping that was written.

    Raises:
        ValueError: When the existing settings file is not valid JSON / not a
            JSON object (propagated from :func:`read_settings`).
    """
    settings = read_settings(path)
    patched = patch_settings(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_settings(patched), encoding="utf-8")
    logger.info(f"install_statusline wrote-statusline path={path!r}")
    return patched


__all__ = [
    "build_statusline_command",
    "global_settings_path",
    "install_statusline",
    "is_already_installed",
    "patch_settings",
    "read_settings",
    "render_settings",
]
