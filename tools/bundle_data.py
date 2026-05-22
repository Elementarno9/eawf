"""Hatchling custom build hook that populates ``src/eawf/_data/``.

The wheel ships per-OS daemon service templates (systemd unit, launchd
plist) under ``eawf/_data/service_templates/`` so an operator who
installed from PyPI can run ``eawf daemon enable`` without the repo
checkout. The templates live at the repo-root ``templates/`` directory
in version control (the daemon reads them from there in development);
this hook copies the configured subset into ``src/eawf/_data/`` right
before Hatchling assembles the wheel, and the wheel target's
``force-include`` carries the generated tree into the archive.

The hook is build-time only — ``hatchling`` is a ``[build-system]``
requirement, not a runtime dependency, so this module is imported
solely inside the isolated build environment ``uv build`` provisions.

Configuration lives under ``[tool.eawf.bundle]`` in ``pyproject.toml``:

* ``service_templates_dir`` — repo-relative source directory.
* ``service_templates`` — explicit filename allow-list to bundle.

The hook fails fast (``FileNotFoundError``) if a configured template is
missing so a packaging regression reds the build instead of shipping a
wheel with an empty ``_data/`` tree.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

#: Subdirectory under ``_data`` that holds the per-OS service templates.
_SERVICE_TEMPLATES_SUBDIR = "service_templates"

#: Name of the generated data package directory under ``src/eawf``.
_DATA_DIRNAME = "_data"


def _bundle_config(metadata_config: dict[str, Any]) -> dict[str, Any]:
    """Read the ``[tool.eawf.bundle]`` table from project metadata.

    Args:
        metadata_config: The ``tool`` table parsed from ``pyproject.toml``
            (Hatchling exposes it via ``self.metadata.config``).

    Returns:
        The ``[tool.eawf.bundle]`` mapping.

    Raises:
        KeyError: When the ``[tool.eawf.bundle]`` table is absent.
    """
    return metadata_config["tool"]["eawf"]["bundle"]


def populate_data_tree(root: Path, config: dict[str, Any]) -> list[Path]:
    """Copy the configured service templates into ``src/eawf/_data``.

    The destination tree is rebuilt from scratch on every call so a
    stale file from a prior build never lingers in the wheel.

    Args:
        root: Repository root (the build hook's ``self.root``).
        config: The ``[tool.eawf.bundle]`` mapping.

    Returns:
        The list of files written under ``src/eawf/_data`` (absolute
        paths), in bundling order.

    Raises:
        FileNotFoundError: When a configured template does not exist at
            ``service_templates_dir``.
    """
    src_dir = root / config["service_templates_dir"]
    dest_root = root / "src" / "eawf" / _DATA_DIRNAME
    dest_service_dir = dest_root / _SERVICE_TEMPLATES_SUBDIR

    if dest_root.exists():
        shutil.rmtree(dest_root)
    dest_service_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for name in config["service_templates"]:
        source = src_dir / name
        if not source.is_file():
            raise FileNotFoundError(f"bundle service template not found: {source!r}")
        target = dest_service_dir / name
        shutil.copyfile(source, target)
        written.append(target)
    return written


class BundleDataBuildHook(BuildHookInterface):  # type: ignore[type-arg]
    """Populate ``src/eawf/_data/`` before the wheel is built."""

    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        """Populate the data tree at the start of the build.

        Args:
            version: The build target version (unused; Hatchling
                resolves it from ``[tool.hatch.version]``).
            build_data: The mutable build-data mapping Hatchling passes
                through the build (unused here — ``force-include`` in
                ``pyproject.toml`` carries the generated tree).
        """
        config = _bundle_config(self.metadata.config)
        written = populate_data_tree(Path(self.root), config)
        self.app.display_info(
            f"bundle_data wrote files={len(written)} "
            f"dest={_DATA_DIRNAME}/{_SERVICE_TEMPLATES_SUBDIR}"
        )
