"""Doctor check comparing the installed eawf host plugins with the package version.

A host loads the skill and agent text of the plugin it has installed, not of
the package on the path, so a plugin left at an older release steers every
session with stale text while ``eawf`` itself reports the new version. The
registries are read the way the hosts write them:

- Claude Code: ``~/.claude/plugins/installed_plugins.json`` maps
  ``"<plugin>@<marketplace>"`` to install records, each carrying ``version``.
- Codex: ``~/.codex/plugins/cache/<marketplace>/<plugin>/<version>/`` holds
  each cached install, its ``.codex-plugin/plugin.json`` naming the version.
  An upgrade leaves the older versions cached, so a cached version is not by
  itself the one Codex loads. ``codex plugin list --json`` names the enabled
  one, and the doctor check asks it. The session-start hook may not start a
  subprocess, and Codex's ``config.toml`` records that ``eawf@eawf`` is
  enabled but not at which version, so the hook takes the cached version
  whose manifest was written last: the one the latest install put there.

The session-start hook runs this on every session, so the module imports no
pydantic model at load: :class:`CheckResult` is imported where it is built.
The registries belong to the hosts, which add keys freely, so they are read
leniently rather than through a closed model.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from eawf import __version__

if TYPE_CHECKING:
    from eawf.observability.doctor.models import CheckResult

CHECK_NAME: Final = "plugin_version"

#: The first words of the session-start line a mismatch prints.
MISMATCH_PREFIX: Final = "eawf plugin version mismatch:"

_PLUGIN_NAME: Final = "eawf"

#: PEP 440's version grammar, as its appendix spells it, with the lenient
#: separators the specification normalises away.
_PEP440: Final = re.compile(
    r"""
    ^\s*v?
    (?:(?P<epoch>[0-9]+)!)?
    (?P<release>[0-9]+(?:\.[0-9]+)*)
    (?:[-_.]?(?P<pre_l>alpha|a|beta|b|preview|pre|c|rc)[-_.]?(?P<pre_n>[0-9]+)?)?
    (?:-(?P<post_n1>[0-9]+)|[-_.]?(?P<post_l>post|rev|r)[-_.]?(?P<post_n2>[0-9]+)?)?
    (?:[-_.]?(?P<dev_l>dev)[-_.]?(?P<dev_n>[0-9]+)?)?
    (?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?
    \s*$
    """,
    re.VERBOSE | re.IGNORECASE,
)

_PRE_SPELLINGS: Final = {"alpha": "a", "beta": "b", "c": "rc", "pre": "rc", "preview": "rc"}


@dataclass(frozen=True)
class _InstalledPlugin:
    """One eawf plugin install a host registry records."""

    runtime: str
    version: str


def _registry_home() -> Path:
    """Return the home directory the host registries live under."""
    return Path.home()


def _normalized(version: str) -> str:
    """Return *version* in PEP 440 normal form, trailing release zeros dropped.

    Dropping the zeros makes ``0.7`` and ``0.7.0`` compare equal, as PEP 440
    orders them. A string outside the grammar is returned stripped, so it only
    equals itself.
    """
    match = _PEP440.fullmatch(version)
    if match is None:
        return version.strip()
    release = [int(part) for part in match["release"].split(".")]
    while len(release) > 1 and release[-1] == 0:
        release.pop()
    parts = [f"{int(match['epoch'])}!" if match["epoch"] and int(match["epoch"]) else ""]
    parts.append(".".join(str(part) for part in release))
    if match["pre_l"]:
        label = match["pre_l"].lower()
        parts.append(f"{_PRE_SPELLINGS.get(label, label)}{int(match['pre_n'] or 0)}")
    if match["post_n1"] or match["post_l"]:
        parts.append(f".post{int(match['post_n1'] or match['post_n2'] or 0)}")
    if match["dev_l"]:
        parts.append(f".dev{int(match['dev_n'] or 0)}")
    if match["local"]:
        parts.append(f"+{re.sub(r'[-_]', '.', match['local'].lower())}")
    return "".join(parts)


def _read_json(path: Path) -> Any:
    """Return the JSON document at *path*, or ``None`` when it is absent or unreadable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError, UnicodeDecodeError, json.JSONDecodeError:
        return None


def _claude_versions(home: Path, workspace: Path | None) -> list[str]:
    """Return the version of every Claude Code eawf install that loads in *workspace*.

    A project- or local-scoped record loads only in the project it names, so a
    record for another checkout is not this workspace's plugin.
    """
    document = _read_json(home / ".claude" / "plugins" / "installed_plugins.json")
    plugins = document.get("plugins") if isinstance(document, dict) else None
    versions: list[str] = []
    for key, records in sorted(plugins.items()) if isinstance(plugins, dict) else ():
        if key.partition("@")[0] != _PLUGIN_NAME or not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("version"), str):
                continue
            project = record.get("projectPath")
            if isinstance(project, str) and (
                workspace is None or Path(project).resolve() != workspace.resolve()
            ):
                continue
            versions.append(record["version"])
    return versions


def _last_cached_codex_version(home: Path) -> list[str]:
    """Return the version of the cached Codex eawf plugin installed last, if any.

    Args:
        home: The home directory Codex keeps its plugin cache under.

    Returns:
        The version the most recently written cached manifest names, as a
        one-item list; empty when no cached manifest names a version.
    """
    cache = home / ".codex" / "plugins" / "cache" / _PLUGIN_NAME / _PLUGIN_NAME
    if not cache.is_dir():
        return []
    written: list[tuple[float, str]] = []
    for root in sorted(cache.iterdir()):
        path = root / ".codex-plugin" / "plugin.json"
        manifest = _read_json(path)
        if isinstance(manifest, dict) and isinstance(manifest.get("version"), str):
            written.append((path.stat().st_mtime, manifest["version"]))
    return [max(written)[1]] if written else []


def _enabled_codex_version(home: Path) -> list[str]:
    """Return the version of the Codex eawf plugin ``codex plugin list`` reports enabled.

    Args:
        home: The home directory Codex runs under.

    Returns:
        The enabled install's version as a one-item list; empty when Codex is
        not installed or enables no eawf plugin. When Codex's answer is
        malformed or names an invalid cache, the cached version installed
        last stands in for it.
    """
    from eawf.runtime.runtimes.codex.plugin_install import (
        IntegrityViolation,
        _marketplace_plugin_root,
    )

    try:
        root = _marketplace_plugin_root(home)
    except IntegrityViolation:
        return _last_cached_codex_version(home)
    # The installer has checked that the cache root is named for the version
    # and that its manifest names the same one.
    return [root.name] if root is not None else []


def _installed_plugins(workspace: Path | None, *, ask_codex: bool) -> list[_InstalledPlugin]:
    """Return every eawf plugin install the Claude Code and Codex registries record.

    Args:
        workspace: The workspace a project-scoped Claude Code install must name.
        ask_codex: Ask ``codex plugin list`` which Codex install is enabled;
            otherwise take the cached version installed last, which starts no
            subprocess.

    Returns:
        The Claude Code installs, then the Codex one.
    """
    home = _registry_home()
    codex = _enabled_codex_version(home) if ask_codex else _last_cached_codex_version(home)
    return [
        *(_InstalledPlugin("claude", version) for version in _claude_versions(home, workspace)),
        *(_InstalledPlugin("codex", version) for version in codex),
    ]


def _stale(installs: list[_InstalledPlugin]) -> list[_InstalledPlugin]:
    """Return the installs whose version differs from the package's."""
    package = _normalized(__version__)
    return [install for install in installs if _normalized(install.version) != package]


def _describe(stale: list[_InstalledPlugin]) -> str:
    """Name each stale install and the package version it differs from."""
    named = ", ".join(f"{install.runtime} plugin {install.version}" for install in stale)
    return f"{named} installed, eawf package {__version__}; update the plugin to match"


def check_plugin_version(*, workspace: Path | None) -> CheckResult:
    """Report whether every installed eawf host plugin matches the package version.

    Args:
        workspace: The workspace anchor, already resolved by the caller; a
            project-scoped Claude Code install counts only when it names it.
            ``None`` when no anchor exists.

    Returns:
        ``fail`` naming each differing install and the package version, ``ok``
        when every install matches, and ``ok`` reporting the check skipped when
        no eawf plugin is installed.
    """
    from eawf.observability.doctor.models import CheckResult

    installs = _installed_plugins(workspace, ask_codex=True)
    if not installs:
        return CheckResult(name=CHECK_NAME, status="ok", detail="skipped: no eawf plugin installed")
    stale = _stale(installs)
    if stale:
        return CheckResult(name=CHECK_NAME, status="fail", detail=_describe(stale))
    runtimes = ", ".join(sorted({install.runtime for install in installs}))
    return CheckResult(
        name=CHECK_NAME, status="ok", detail=f"{runtimes} plugin at eawf {__version__}"
    )


def plugin_version_mismatch_line(*, workspace: Path | None) -> str | None:
    """Return the session-start line naming a stale installed plugin, if one is.

    Args:
        workspace: The repository the host session runs in.

    Returns:
        One line starting with :data:`MISMATCH_PREFIX`; ``None`` when every
        install matches the package or none is installed.
    """
    stale = _stale(_installed_plugins(workspace, ask_codex=False))
    return f"{MISMATCH_PREFIX} {_describe(stale)}" if stale else None


__all__ = [
    "CHECK_NAME",
    "MISMATCH_PREFIX",
    "check_plugin_version",
    "plugin_version_mismatch_line",
]
