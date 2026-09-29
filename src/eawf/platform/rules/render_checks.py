"""Check the render transaction against the ignore file and against each host.

Two checks bracket the writes of :func:`eawf.platform.rules.render.write_rule_projections`:

- before any write, the managed ``.gitignore`` block must ignore every
  generated path, because the block is the one committed declaration of
  what is generated and an output missing from it would be committed by
  every clone;
- around the removal of obsolete outputs, the files each host loads are
  probed (:mod:`eawf.platform.rules.host_probe`), because the manifest is
  only the render's own record and a generated file it lost track of stays
  in the host's chain.
"""

from __future__ import annotations

from pathlib import Path

from eawf.platform.install.gitignore_writer import (
    GitignoreBlockPlan,
    plan_gitignore_block,
    unenumerated_paths,
)
from eawf.platform.rules.host_facts import load_host_facts
from eawf.platform.rules.host_probe import host_loaded_files
from eawf.platform.rules.render import (
    CARD_TARGET,
    POLICY_TARGET,
    PROJECTION_MANIFEST_PATH,
    ProjectionManifest,
    RuleProjectionUnenumeratedError,
    classify_projection,
)


def checked_ignore_block(repo_root: Path, manifest: ProjectionManifest) -> GitignoreBlockPlan:
    """Plan the managed ``.gitignore`` block and refuse a generated path it misses.

    Every output but the committed card is generated, the manifest sidecar
    included.

    Args:
        repo_root: The repository root.
        manifest: The manifest of the render in progress.

    Returns:
        The planned ``.gitignore``.

    Raises:
        RuleProjectionUnenumeratedError: When the block does not ignore a
            generated path.
        ManagedBlockError: When the ``.gitignore`` markers are not exactly
            one ordered pair.
    """
    ignore = plan_gitignore_block(repo_root)
    generated = (
        *(record.target for record in manifest.projections if record.kind != "card"),
        *manifest.generated,
        PROJECTION_MANIFEST_PATH,
    )
    unenumerated = unenumerated_paths(ignore.patterns, generated)
    if unenumerated:
        raise RuleProjectionUnenumeratedError(
            f"the managed .gitignore block does not ignore generated {list(unenumerated)}; "
            f"add a pattern for each to the shipped block before rendering"
        )
    return ignore


def stale_host_loaded(repo_root: Path, manifest: ProjectionManifest) -> tuple[Path, ...]:
    """Name the eawf-stamped files a host loads that *manifest* does not write.

    Args:
        repo_root: The repository root.
        manifest: The manifest of the render in progress.

    Returns:
        Loaded files carrying an eawf stamp, hand-edited or not, outside the
        render's targets. A file made only of imports is never named: its
        shape alone does not prove eawf wrote it.
    """
    loaded = host_loaded_files(
        repo_root,
        load_host_facts(),
        projections={"card": CARD_TARGET, "policy": POLICY_TARGET},
    )
    targets = frozenset(manifest.targets)
    return tuple(
        repo_root / relative
        for relative in loaded
        if relative not in targets and _stamped(repo_root / relative)
    )


def _stamped(path: Path) -> bool:
    """Return whether *path* carries an eawf stamp, matching its body or not.

    Args:
        path: An existing file.

    Returns:
        ``True`` for a stamped projection, view or carrier.
    """
    state = classify_projection(path)
    if state == "hand_edited":
        return True
    lines = path.read_text(encoding="utf-8").splitlines()
    return state == "generated" and not all(line.startswith("@") for line in lines)


__all__ = ["checked_ignore_block", "stale_host_loaded"]
