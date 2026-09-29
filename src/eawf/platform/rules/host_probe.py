"""Probe which files each supported host loads from a repository at session start.

A render transaction removes the outputs its manifest says are obsolete, but
the manifest is only the render's own record: a generated file it no longer
names -- because the manifest was lost, or an earlier generation wrote it --
stays on disk and stays in the host's loaded chain. Removal is therefore
confirmed against what the host would load, read from disk the way the host
discovers it, not against the manifest.

The loading profile follows the certified host facts
(:mod:`eawf.platform.rules.host_facts`): a runtime with an import shim loads
the shim and every file its ``@path`` lines import, transitively; any other
runtime loads the projections it reads. Claude additionally discovers every
skill under the carrier directory, which is how a role carrier reaches it.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Final

from eawf.kernel.runtime.host_facts import HostFactRegistry, ProjectionKind
from eawf.observability.telemetry.models import RuntimeName
from eawf.platform.rules.carriers import CARRIER_DIRECTORY

#: Runtimes that discover skills in a directory, and the file each skill is.
_SKILL_DISCOVERY: Final[Mapping[RuntimeName, str]] = {"claude": f"{CARRIER_DIRECTORY}/*/SKILL.md"}


def host_loaded_files(
    repo_root: Path,
    registry: HostFactRegistry,
    *,
    projections: Mapping[ProjectionKind, str],
) -> tuple[str, ...]:
    """Return every existing file some supported host loads from *repo_root*.

    Only paths inside *repo_root* are followed; an import that climbs out of
    it is not part of the repository's chain.

    Args:
        repo_root: The repository root.
        registry: The certified host facts naming each runtime's discovery.
        projections: The repository-relative target of each projection kind.

    Returns:
        Sorted repository-relative POSIX paths, each loaded by at least one
        runtime.
    """
    root = repo_root.resolve()
    loaded: set[str] = set()
    for record in registry.records:
        if record.import_shim is not None:
            loaded.update(_import_chain(root, record.import_shim))
        else:
            loaded.update(
                target for kind in record.reads if (root / (target := projections[kind])).is_file()
            )
        pattern = _SKILL_DISCOVERY.get(record.runtime)
        if pattern is not None:
            loaded.update(path.relative_to(root).as_posix() for path in root.glob(pattern))
    return tuple(sorted(loaded))


def _import_chain(root: Path, entry: str) -> set[str]:
    """Return *entry* and every in-repository file its imports reach.

    Args:
        root: The resolved repository root.
        entry: The repository-relative file the host discovers first.

    Returns:
        The existing files of the chain; a cycle is visited once.
    """
    chain: set[str] = set()
    pending = [entry]
    while pending:
        path = (root / pending.pop()).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in chain:
            continue
        chain.add(relative)
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("@") and len(line) > 1:
                pending.append(PurePosixPath(relative).parent.joinpath(line[1:]).as_posix())
    return chain


__all__ = ["host_loaded_files"]
