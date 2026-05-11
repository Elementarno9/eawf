"""Render the Eä-owned OpenCode plugin (P14-W07 / D12 + D13).

Outputs under *target_dir*:

::

    opencode.json                      # managed JSON config with mcp block
    plugin.js                          # untyped JS bridge (template asset)

The renderer is idempotent — two runs against the same target produce
byte-identical output. ``plugin.js`` is read from the bundled template
asset and version-stamped; ``opencode.json`` carries the
``__eawf_managed`` namespace alongside any user-authored top-level
fields.

Public API mirrors the Claude / Codex adapters:

    InstallResult, install_plugin, expected_paths, IntegrityViolation
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any

from eawf.render._atomic import atomic_write_text

logger = logging.getLogger(__name__)


_PLUGIN_VERSION: str = "1.0"
_GENERATOR: str = "eawf-plugin-opencode"
_MANAGED_KEY: str = "__eawf_managed"
_DEFAULT_TIMESTAMP: str = "1970-01-01T00:00:00+00:00"
_PLUGIN_TEMPLATE_PACKAGE: str = "eawf.runtimes.opencode.templates"
_PLUGIN_TEMPLATE_RESOURCE: str = "plugin.js"
_PLUGIN_VERSION_PLACEHOLDER: str = "__EAWF_PLUGIN_VERSION__"


@dataclass(frozen=True)
class FileDelta:
    """One file the installer wrote / would have written."""

    path: Path
    action: str  # Literal["created", "updated", "unchanged"]


@dataclass(frozen=True)
class InstallResult:
    """Summary of one :func:`install_plugin` call."""

    target_dir: Path
    config: FileDelta | None = None
    plugin_js: FileDelta | None = None
    dry_run: bool = False
    deltas: list[FileDelta] = field(default_factory=list)


class IntegrityViolation(Exception):  # noqa: N818 — mirrors eawf.cli.errors.IntegrityViolation
    """Raised when a managed plugin file has drifted from its recorded hash."""


def _classify(path: Path, payload: bytes) -> str:
    if not path.exists():
        return "created"
    if path.read_bytes() == payload:
        return "unchanged"
    return "updated"


def _config_target(target_dir: Path) -> Path:
    return target_dir / "opencode.json"


def _plugin_js_target(target_dir: Path) -> Path:
    return target_dir / "plugin.js"


def _load_plugin_js_template() -> str:
    return (
        files(_PLUGIN_TEMPLATE_PACKAGE)
        .joinpath(_PLUGIN_TEMPLATE_RESOURCE)
        .read_text(encoding="utf-8")
    )


def _render_plugin_js() -> str:
    template = _load_plugin_js_template()
    return template.replace(_PLUGIN_VERSION_PLACEHOLDER, _PLUGIN_VERSION)


def _render_managed_body(timestamp: str, plugin_js_bytes: bytes) -> dict[str, Any]:
    body: dict[str, Any] = {
        "version": _PLUGIN_VERSION,
        "generated_at": timestamp,
        "generator": _GENERATOR,
        "plugin_js_hash": hashlib.blake2b(plugin_js_bytes, digest_size=8).hexdigest(),
        "plugin_js_path": "plugin.js",
    }
    body_json = json.dumps(body, sort_keys=True, separators=(",", ":"))
    body["hash"] = hashlib.blake2b(body_json.encode("utf-8"), digest_size=8).hexdigest()
    return body


def _patch_config_json(target_path: Path, managed_body: dict[str, Any]) -> bytes:
    """Return rewritten ``opencode.json`` bytes with the managed namespace patched in.

    User-authored top-level keys are preserved verbatim. When the file
    does not exist, the managed namespace + a minimal ``mcp`` block
    seed the new file.
    """
    parsed: dict[str, Any] = {}
    if target_path.exists():
        raw = target_path.read_text(encoding="utf-8")
        if raw.strip():
            try:
                parsed_any: Any = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"opencode.json at {target_path} is not valid JSON: {exc}"
                ) from exc
            if not isinstance(parsed_any, dict):
                raise ValueError(
                    f"opencode.json at {target_path} must be a JSON object; "
                    f"got {type(parsed_any).__name__}"
                )
            parsed = dict(parsed_any)
    parsed[_MANAGED_KEY] = managed_body
    parsed.setdefault("mcp", {})
    parsed.setdefault("plugins", [])
    if "plugin.js" not in parsed["plugins"]:
        parsed["plugins"] = list(parsed["plugins"]) + ["plugin.js"]
    rendered = json.dumps(parsed, sort_keys=True, indent=2) + "\n"
    return rendered.encode("utf-8")


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def install_plugin(
    target_dir: Path,
    *,
    force: bool = False,
    dry_run: bool = False,
    timestamp: str | None = None,
) -> InstallResult:
    """Render the OpenCode plugin under *target_dir*.

    Args:
        target_dir: Workspace root that hosts ``opencode.json`` + ``plugin.js``.
        force: When ``True``, hand-edits to ``plugin.js`` are overwritten
            silently. The ``__eawf_managed`` namespace inside
            ``opencode.json`` is always rewritten; user-owned keys
            elsewhere are preserved.
        dry_run: When ``True``, returns the :class:`InstallResult`
            describing what would be written but writes nothing.
        timestamp: ISO 8601 UTC timestamp baked into the managed body.
            Defaults to ``"1970-01-01T00:00:00+00:00"``.
    """
    target_dir = Path(target_dir).resolve()
    ts = timestamp or _DEFAULT_TIMESTAMP
    plugin_js_path = _plugin_js_target(target_dir)
    plugin_js_payload = _render_plugin_js().encode("utf-8")
    if plugin_js_path.exists() and not force:
        if plugin_js_path.read_bytes() != plugin_js_payload:
            raise IntegrityViolation(
                f"managed file {plugin_js_path} differs from rendered body; "
                f"rerun with --force to overwrite"
            )
    plugin_js_action = _classify(plugin_js_path, plugin_js_payload)
    if not dry_run:
        _ensure_dir(plugin_js_path.parent)
        atomic_write_text(plugin_js_path, plugin_js_payload.decode("utf-8"))
    plugin_js_delta = FileDelta(path=plugin_js_path, action=plugin_js_action)

    config_path = _config_target(target_dir)
    managed_body = _render_managed_body(ts, plugin_js_payload)
    config_bytes = _patch_config_json(config_path, managed_body)
    config_action = _classify(config_path, config_bytes)
    if not dry_run:
        _ensure_dir(config_path.parent)
        atomic_write_text(config_path, config_bytes.decode("utf-8"))
    config_delta = FileDelta(path=config_path, action=config_action)

    logger.info(
        f"install_plugin runtime=opencode target={target_dir} "
        f"plugin_js={plugin_js_action} config={config_action} dry_run={dry_run}"
    )
    return InstallResult(
        target_dir=target_dir,
        config=config_delta,
        plugin_js=plugin_js_delta,
        dry_run=dry_run,
        deltas=[plugin_js_delta, config_delta],
    )


def expected_paths(target_dir: Path) -> tuple[Mapping[str, Path], Path]:
    """Return ``({region_id: path}, config_path)`` for *target_dir*."""
    target_dir = Path(target_dir).resolve()
    return (
        {
            "plugin.opencode.plugin_js": _plugin_js_target(target_dir),
        },
        _config_target(target_dir),
    )


def expected_plugin_js_bytes() -> bytes:
    """Return the rendered ``plugin.js`` bytes (used by the doctor)."""
    return _render_plugin_js().encode("utf-8")


__all__ = [
    "FileDelta",
    "InstallResult",
    "IntegrityViolation",
    "expected_paths",
    "expected_plugin_js_bytes",
    "install_plugin",
]
