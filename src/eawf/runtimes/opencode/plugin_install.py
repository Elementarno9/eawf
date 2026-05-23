"""Render the Eä-owned OpenCode plugin (P14-I02-W01).

Native OpenCode plugin layout. The renderer drops the legacy
workspace-root dump (``<ws>/plugin.js`` + ``<ws>/opencode.json`` with a
``plugins:[...]`` array entry) in favour of OpenCode's native
auto-discovery dirs:

- ``scope="project"`` → ``<target>/.opencode/plugins/eawf.js`` with
  sidecar ``<target>/.opencode/plugins/.eawf-managed.json`` and
  ``<target>/opencode.json`` patched only in its ``mcp`` block.
- ``scope="user"`` → ``$OPENCODE_CONFIG_DIR/plugins/eawf.js`` (or
  ``<home>/.config/opencode/plugins/eawf.js`` when the env var is
  unset) with the corresponding sidecar and config patched at the
  same scope.

The ``plugins:[...]`` array inside ``opencode.json`` is reserved for
npm package plugins; we no longer add ``"plugin.js"`` to it. Auto-load
handles discovery from the plugins directory.

The renderer is idempotent — two runs against the same target produce
byte-identical output. ``plugin.js`` is read from the bundled template
asset and version-stamped.

Public API mirrors the Codex adapter:

    InstallResult, install_plugin, expected_paths, IntegrityViolation,
    Scope
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

from eawf.render._atomic import atomic_write_text
from eawf.render.agents import AGENT_REGISTRY, AgentSpec
from eawf.render.skills import SKILL_REGISTRY, SkillSpec

logger = logging.getLogger(__name__)


Scope = Literal["project", "user"]

_PLUGIN_NAME: str = "eawf"
_PLUGIN_VERSION: str = "1.0"
_GENERATOR: str = "eawf-plugin-opencode"
_DEFAULT_TIMESTAMP: str = "1970-01-01T00:00:00+00:00"
_PLUGIN_TEMPLATE_PACKAGE: str = "eawf.runtimes.opencode.templates"
_PLUGIN_TEMPLATE_RESOURCE: str = "plugin.js"
_PLUGIN_VERSION_PLACEHOLDER: str = "__EAWF_PLUGIN_VERSION__"
_PLUGIN_FILENAME: str = "eawf.js"
_SIDECAR_FILENAME: str = ".eawf-managed.json"
_OPENCODE_CONFIG_DIR_ENV: str = "OPENCODE_CONFIG_DIR"


@dataclass(frozen=True)
class FileDelta:
    """One file the installer wrote / would have written."""

    path: Path
    action: str  # Literal["created", "updated", "unchanged"]


@dataclass(frozen=True)
class InstallResult:
    """Summary of one :func:`install_plugin` call."""

    target_dir: Path
    scope: Scope = "project"
    config: FileDelta | None = None
    plugin_js: FileDelta | None = None
    sidecar: FileDelta | None = None
    agents: list[FileDelta] = field(default_factory=list)
    commands: list[FileDelta] = field(default_factory=list)
    dry_run: bool = False
    deltas: list[FileDelta] = field(default_factory=list)


class IntegrityViolation(Exception):  # noqa: N818 — mirrors the kind="IntegrityViolation" CLI error bucket
    """Raised when a managed plugin file has drifted from its recorded hash."""


def _classify(path: Path, payload: bytes) -> str:
    if not path.exists():
        return "created"
    if path.read_bytes() == payload:
        return "unchanged"
    return "updated"


def _user_config_root(
    *,
    home: Path | None = None,
    opencode_config_dir: str | None = None,
) -> Path:
    """Return the user-scope OpenCode config root.

    Precedence: explicit *opencode_config_dir* kwarg → ``$OPENCODE_CONFIG_DIR``
    env var → ``<home>/.config/opencode/``. ``home`` defaults to
    :func:`pathlib.Path.home`.
    """
    if opencode_config_dir is not None:
        return Path(opencode_config_dir)
    env_value = os.environ.get(_OPENCODE_CONFIG_DIR_ENV)
    if env_value:
        return Path(env_value)
    base = home if home is not None else Path.home()
    return base / ".config" / "opencode"


def _plugins_dir(
    target_dir: Path,
    *,
    scope: Scope,
    home: Path | None = None,
    opencode_config_dir: str | None = None,
) -> Path:
    """Return the directory holding ``eawf.js`` + sidecar at *scope*."""
    if scope == "project":
        return target_dir / ".opencode" / "plugins"
    return _user_config_root(home=home, opencode_config_dir=opencode_config_dir) / "plugins"


def _config_target(
    target_dir: Path,
    *,
    scope: Scope,
    home: Path | None = None,
    opencode_config_dir: str | None = None,
) -> Path:
    """Return the scope-correct ``opencode.json`` path."""
    if scope == "project":
        return target_dir / "opencode.json"
    return _user_config_root(home=home, opencode_config_dir=opencode_config_dir) / "opencode.json"


def _plugin_js_target(
    target_dir: Path,
    *,
    scope: Scope,
    home: Path | None = None,
    opencode_config_dir: str | None = None,
) -> Path:
    return (
        _plugins_dir(target_dir, scope=scope, home=home, opencode_config_dir=opencode_config_dir)
        / _PLUGIN_FILENAME
    )


def _sidecar_target(
    target_dir: Path,
    *,
    scope: Scope,
    home: Path | None = None,
    opencode_config_dir: str | None = None,
) -> Path:
    return (
        _plugins_dir(target_dir, scope=scope, home=home, opencode_config_dir=opencode_config_dir)
        / _SIDECAR_FILENAME
    )


def _opencode_base(
    target_dir: Path,
    *,
    scope: Scope,
    home: Path | None = None,
    opencode_config_dir: str | None = None,
) -> Path:
    """Return the scope root that holds ``agents/``, ``commands/``, ``plugins/``."""
    if scope == "project":
        return target_dir / ".opencode"
    return _user_config_root(home=home, opencode_config_dir=opencode_config_dir)


def _agent_target(
    target_dir: Path,
    spec: AgentSpec,
    *,
    scope: Scope,
    home: Path | None = None,
    opencode_config_dir: str | None = None,
) -> Path:
    base = _opencode_base(
        target_dir, scope=scope, home=home, opencode_config_dir=opencode_config_dir
    )
    return base / "agents" / f"{spec.role}.md"


def _command_target(
    target_dir: Path,
    spec: SkillSpec,
    *,
    scope: Scope,
    home: Path | None = None,
    opencode_config_dir: str | None = None,
) -> Path:
    base = _opencode_base(
        target_dir, scope=scope, home=home, opencode_config_dir=opencode_config_dir
    )
    return base / "commands" / f"{spec.skill_name}.md"


def _render_opencode_agent_md(spec: AgentSpec) -> str:
    """Render an opencode agent file from an :class:`AgentSpec`.

    Uses the opencode-native frontmatter schema (``description`` / ``mode``);
    Claude-Code ``name`` / ``tools`` keys are dropped because opencode
    derives the agent name from the filename and uses a separate
    ``permission`` object for tool ACLs (omitted here so the operator
    can layer their own policy in ``opencode.json`` or
    ``~/.config/opencode/opencode.json``).
    """
    lines = [
        "---",
        f"description: {spec.description}",
        "mode: subagent",
        "---",
        "",
        spec.body.rstrip() + "\n",
    ]
    return "\n".join(lines)


def _render_opencode_command_md(spec: SkillSpec) -> str:
    """Render an opencode command file from a :class:`SkillSpec`.

    Maps each ``user_invocable=True`` Eä skill to ``/<skill_name>``.
    The body always closes with ``ARGUMENTS: $ARGUMENTS`` so opencode
    substitutes the invocation args into the prompt (opencode commands
    use the literal ``$ARGUMENTS`` placeholder — there is no
    frontmatter ``argument-hint`` key like Claude Code). The argument
    hint is rendered as a comment before the placeholder so a human
    reader can still see the expected shape.
    """
    lines = [
        "---",
        f"description: {spec.description}",
        "---",
        "",
        spec.body.rstrip(),
        "",
    ]
    if spec.argument_hint:
        lines.append(f"<!-- argument hint: {spec.argument_hint} -->")
    lines.append("ARGUMENTS: $ARGUMENTS")
    lines.append("")
    return "\n".join(lines)


def _load_plugin_js_template() -> str:
    return (
        files(_PLUGIN_TEMPLATE_PACKAGE)
        .joinpath(_PLUGIN_TEMPLATE_RESOURCE)
        .read_text(encoding="utf-8")
    )


def _render_plugin_js() -> str:
    template = _load_plugin_js_template()
    return template.replace(_PLUGIN_VERSION_PLACEHOLDER, _PLUGIN_VERSION)


def _render_sidecar_body(timestamp: str, plugin_js_bytes: bytes) -> dict[str, Any]:
    agents_payload = [{"name": spec.role, "version": spec.version} for spec in AGENT_REGISTRY]
    commands_payload = [
        {"name": spec.skill_name, "version": spec.version}
        for spec in SKILL_REGISTRY
        if spec.user_invocable
    ]
    body: dict[str, Any] = {
        "version": _PLUGIN_VERSION,
        "generated_at": timestamp,
        "generator": _GENERATOR,
        "plugin_js_hash": hashlib.blake2b(plugin_js_bytes, digest_size=8).hexdigest(),
        "plugin_js_path": _PLUGIN_FILENAME,
        "agents": agents_payload,
        "commands": commands_payload,
    }
    body_json = json.dumps(body, sort_keys=True, separators=(",", ":"))
    body["hash"] = hashlib.blake2b(body_json.encode("utf-8"), digest_size=8).hexdigest()
    return body


def _render_sidecar(timestamp: str, plugin_js_bytes: bytes) -> bytes:
    body = _render_sidecar_body(timestamp, plugin_js_bytes)
    return (json.dumps(body, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _sidecar_fingerprint(payload: bytes) -> str:
    """Return a semantic fingerprint for sidecar bytes.

    ``generated_at`` and the derived ``hash`` field are ignored so a
    timestamp-only refresh does not look like managed-file drift.
    """
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except UnicodeDecodeError, json.JSONDecodeError:
        return hashlib.blake2b(b"raw:" + payload, digest_size=8).hexdigest()
    if not isinstance(parsed, dict):
        return hashlib.blake2b(b"raw:" + payload, digest_size=8).hexdigest()
    comparable = dict(parsed)
    comparable.pop("generated_at", None)
    comparable.pop("hash", None)
    body = json.dumps(comparable, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.blake2b(body, digest_size=8).hexdigest()


def _patch_config_json(target_path: Path) -> bytes:
    """Return rewritten ``opencode.json`` bytes with only the ``mcp`` block ensured.

    User-authored top-level keys are preserved verbatim. When the file
    does not exist, it is seeded with ``{"mcp": {}}`` only — no
    ``plugins`` array entry, no managed namespace. (Per OpenCode docs,
    auto-loaded plugins live under ``.opencode/plugins/`` /
    ``$OPENCODE_CONFIG_DIR/plugins/``; the ``plugins:[...]`` array is
    reserved for npm packages.)
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
    parsed.setdefault("mcp", {})
    rendered = json.dumps(parsed, sort_keys=True, indent=2) + "\n"
    return rendered.encode("utf-8")


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def install_plugin(
    target_dir: Path,
    *,
    scope: Scope = "project",
    force: bool = False,
    dry_run: bool = False,
    timestamp: str | None = None,
    home: Path | None = None,
    opencode_config_dir: str | None = None,
) -> InstallResult:
    """Render the OpenCode plugin at *scope*.

    Args:
        target_dir: Workspace root (used only for ``scope="project"``).
        scope: ``"project"`` (default) writes under
            ``<target_dir>/.opencode/plugins/``; ``"user"`` writes under
            ``$OPENCODE_CONFIG_DIR/plugins/`` or
            ``<home>/.config/opencode/plugins/``.
        force: When ``True``, hand-edits to ``eawf.js`` are overwritten
            silently. The ``opencode.json`` ``mcp`` block is always
            ensured; user-owned keys elsewhere are preserved.
        dry_run: When ``True``, returns the :class:`InstallResult`
            describing what would be written but writes nothing.
        timestamp: ISO 8601 UTC timestamp baked into the sidecar.
            Defaults to ``"1970-01-01T00:00:00+00:00"``.
        home: Override for ``Path.home()`` (tests pass ``tmp_path``).
        opencode_config_dir: Override for ``$OPENCODE_CONFIG_DIR``
            (tests pass an explicit path).

    Raises:
        IntegrityViolation: when ``eawf.js`` has been hand-edited and
            ``force`` is not set.
        ValueError: when ``opencode.json`` exists but is not a JSON
            object, or is malformed JSON.
    """
    target_dir = Path(target_dir).resolve()
    ts = timestamp or _DEFAULT_TIMESTAMP
    plugin_js_path = _plugin_js_target(
        target_dir, scope=scope, home=home, opencode_config_dir=opencode_config_dir
    )
    plugin_js_payload = _render_plugin_js().encode("utf-8")
    if plugin_js_path.exists() and not force and plugin_js_path.read_bytes() != plugin_js_payload:
        raise IntegrityViolation(
            f"managed file {plugin_js_path} differs from rendered body; "
            f"rerun with --force to overwrite"
        )
    plugin_js_action = _classify(plugin_js_path, plugin_js_payload)
    if not dry_run:
        _ensure_dir(plugin_js_path.parent)
        atomic_write_text(plugin_js_path, plugin_js_payload.decode("utf-8"))
    plugin_js_delta = FileDelta(path=plugin_js_path, action=plugin_js_action)

    sidecar_path = _sidecar_target(
        target_dir, scope=scope, home=home, opencode_config_dir=opencode_config_dir
    )
    sidecar_payload = _render_sidecar(ts, plugin_js_payload)
    if (
        sidecar_path.exists()
        and not force
        and _sidecar_fingerprint(sidecar_path.read_bytes()) != _sidecar_fingerprint(sidecar_payload)
    ):
        raise IntegrityViolation(
            f"managed file {sidecar_path} differs from rendered body; "
            f"rerun with --force to overwrite"
        )
    sidecar_action = _classify(sidecar_path, sidecar_payload)
    if not dry_run:
        _ensure_dir(sidecar_path.parent)
        atomic_write_text(sidecar_path, sidecar_payload.decode("utf-8"))
    sidecar_delta = FileDelta(path=sidecar_path, action=sidecar_action)

    config_path = _config_target(
        target_dir, scope=scope, home=home, opencode_config_dir=opencode_config_dir
    )
    config_bytes = _patch_config_json(config_path)
    config_action = _classify(config_path, config_bytes)
    if not dry_run:
        _ensure_dir(config_path.parent)
        atomic_write_text(config_path, config_bytes.decode("utf-8"))
    config_delta = FileDelta(path=config_path, action=config_action)

    agent_deltas: list[FileDelta] = []
    for agent_spec in AGENT_REGISTRY:
        agent_path = _agent_target(
            target_dir,
            agent_spec,
            scope=scope,
            home=home,
            opencode_config_dir=opencode_config_dir,
        )
        agent_payload = _render_opencode_agent_md(agent_spec).encode("utf-8")
        agent_action = _classify(agent_path, agent_payload)
        if not dry_run:
            _ensure_dir(agent_path.parent)
            atomic_write_text(agent_path, agent_payload.decode("utf-8"))
        agent_deltas.append(FileDelta(path=agent_path, action=agent_action))

    command_deltas: list[FileDelta] = []
    for skill_spec in SKILL_REGISTRY:
        if not skill_spec.user_invocable:
            continue
        command_path = _command_target(
            target_dir,
            skill_spec,
            scope=scope,
            home=home,
            opencode_config_dir=opencode_config_dir,
        )
        command_payload = _render_opencode_command_md(skill_spec).encode("utf-8")
        command_action = _classify(command_path, command_payload)
        if not dry_run:
            _ensure_dir(command_path.parent)
            atomic_write_text(command_path, command_payload.decode("utf-8"))
        command_deltas.append(FileDelta(path=command_path, action=command_action))

    logger.info(
        f"install_plugin runtime=opencode scope={scope} plugin_js={plugin_js_action} "
        f"sidecar={sidecar_action} config={config_action} "
        f"agents={len(agent_deltas)} commands={len(command_deltas)} dry_run={dry_run}"
    )
    return InstallResult(
        target_dir=target_dir,
        scope=scope,
        config=config_delta,
        plugin_js=plugin_js_delta,
        sidecar=sidecar_delta,
        agents=agent_deltas,
        commands=command_deltas,
        dry_run=dry_run,
        deltas=[
            plugin_js_delta,
            sidecar_delta,
            config_delta,
            *agent_deltas,
            *command_deltas,
        ],
    )


def expected_paths(
    target_dir: Path,
    *,
    scope: Scope = "project",
    home: Path | None = None,
    opencode_config_dir: str | None = None,
) -> tuple[Mapping[str, Path], Path]:
    """Return ``({region_id: path}, config_path)`` for *target_dir* at *scope*."""
    target_dir = Path(target_dir).resolve()
    paths: dict[str, Path] = {
        "plugin.opencode.plugin_js": _plugin_js_target(
            target_dir, scope=scope, home=home, opencode_config_dir=opencode_config_dir
        ),
        "plugin.opencode.sidecar": _sidecar_target(
            target_dir, scope=scope, home=home, opencode_config_dir=opencode_config_dir
        ),
    }
    for agent_spec in AGENT_REGISTRY:
        paths[f"plugin.opencode.agent.{agent_spec.role}"] = _agent_target(
            target_dir,
            agent_spec,
            scope=scope,
            home=home,
            opencode_config_dir=opencode_config_dir,
        )
    for skill_spec in SKILL_REGISTRY:
        if not skill_spec.user_invocable:
            continue
        paths[f"plugin.opencode.command.{skill_spec.skill_name}"] = _command_target(
            target_dir,
            skill_spec,
            scope=scope,
            home=home,
            opencode_config_dir=opencode_config_dir,
        )
    return (
        paths,
        _config_target(target_dir, scope=scope, home=home, opencode_config_dir=opencode_config_dir),
    )


def expected_plugin_js_bytes() -> bytes:
    """Return the rendered ``eawf.js`` bytes (used by the doctor)."""
    return _render_plugin_js().encode("utf-8")


def expected_agent_bodies() -> dict[str, bytes]:
    """Return ``{role: rendered_bytes}`` for every emitted agent file."""
    return {spec.role: _render_opencode_agent_md(spec).encode("utf-8") for spec in AGENT_REGISTRY}


def expected_command_bodies() -> dict[str, bytes]:
    """Return ``{skill_name: rendered_bytes}`` for every emitted command file."""
    return {
        spec.skill_name: _render_opencode_command_md(spec).encode("utf-8")
        for spec in SKILL_REGISTRY
        if spec.user_invocable
    }


__all__ = [
    "FileDelta",
    "InstallResult",
    "IntegrityViolation",
    "Scope",
    "expected_agent_bodies",
    "expected_command_bodies",
    "expected_paths",
    "expected_plugin_js_bytes",
    "install_plugin",
]
