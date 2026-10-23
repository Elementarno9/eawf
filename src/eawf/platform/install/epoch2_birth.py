"""Bearing a freshly initialised tree at authority epoch 2.

After the flag day ``eawf init`` must not leave an epoch-1 tree behind: every
mutating verb refuses on one. A new tree is therefore born native: it carries
an opt-in declaration pinned to a backup of the skeleton the wizard just
wrote, a generation built from the project code, the selection pointer and
then the epoch marker, so the authority resolver grants it epoch 2 for the
same reason it grants a migrated tree. No v1 document is written or read on
the way; the skeleton (``config.yaml`` and any ``profile.yaml``) is what the
pinned backup could put back.

The generation and its activation are the canary's own writers; only the
declaration differs, because an initialised repository is not throwaway.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from eawf.kernel.migration.epoch2.canary import DisposableTarget, OptInDeclaration, opt_in_path
from eawf.kernel.migration.epoch2.generation import atomic_write_json, verify_selected_generation
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.platform.backup import BackupStore, snapshot_digest
from eawf.platform.install.canary import activate_born_generation, write_born_generation

logger = logging.getLogger(__name__)

#: Who a born tree's opt-in says declared it.
INIT_DECLARED_BY: Final = "eawf-init"

#: What a born tree's opt-in says it is for.
INIT_PURPOSE: Final = "born at authority epoch 2 by eawf init"

#: The note the pinned backup carries.
INIT_BACKUP_NOTE: Final = "eawf init: the skeleton an epoch-2 tree was born from"

#: The backup store's snapshot directory name: filesystem-safe ISO-8601 UTC.
_SNAPSHOT_TS_FORMAT: Final = "%Y-%m-%dT%H-%M-%SZ"


def bear_epoch2_tree(tree_root: Path, *, project_code: str, born_at: datetime) -> str:
    """Make ``tree_root`` an activated epoch-2 tree born for ``project_code``.

    The backup is looked up later under the tree's parent directory, the
    same per-repository directory the opt-in verifier reads, so it is
    written there too.

    Args:
        tree_root: The ``.ea`` directory the wizard wrote the skeleton into.
        project_code: The project the tree was initialised for; the
            generation is built from it.
        born_at: When the skeleton is backed up and the tree activated.

    Returns:
        The generation the tree reads from.

    Raises:
        MigrationReadSmokeFailedError: The born generation does not read
            back through the public readers.
        RuntimeError: The finished tree does not resolve to epoch 2.
        OSError: A file could not be written.
    """
    snapshot = BackupStore(tree_root.parent).write_snapshot(
        born_at.astimezone(UTC).strftime(_SNAPSHOT_TS_FORMAT), note=INIT_BACKUP_NOTE
    )
    atomic_write_json(
        opt_in_path(tree_root),
        OptInDeclaration(
            opt_in=True,
            declared_by=INIT_DECLARED_BY,
            purpose=INIT_PURPOSE,
            backup_ts=snapshot.ts,
            backup_digest=snapshot_digest(snapshot),
        ),
    )
    target = DisposableTarget.require(tree_root)
    generation_id, birth_digest = write_born_generation(
        target, birth={"init": {"project_code": project_code}}
    )
    activate_born_generation(
        target, generation_id=generation_id, birth_digest=birth_digest, activated_at=born_at
    )
    verify_selected_generation(target, generation_id=generation_id)
    if resolve_authority(tree_root).epoch != 2:
        raise RuntimeError(f"{tree_root.name} was born but does not resolve to epoch 2")
    logger.info(
        f"bear_epoch2_tree code={project_code} generation={generation_id} backup={snapshot.ts}"
    )
    return generation_id


__all__ = ["INIT_DECLARED_BY", "INIT_PURPOSE", "bear_epoch2_tree"]
