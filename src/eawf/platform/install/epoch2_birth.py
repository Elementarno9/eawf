"""Bearing a freshly initialised tree at authority epoch 2.

After the flag day ``eawf init`` must not leave an epoch-1 tree behind: every
mutating verb refuses on one. A new tree is therefore born at epoch 2 the way
a live repository reaches it by migration: it carries an opt-in declaration
pinned to a verified backup, a generation, the selection pointer and then the
epoch marker, so the authority resolver grants it epoch 2 for exactly the
reason it grants a migrated tree. The skeleton the wizard just wrote is the
corpus, so the backup the declaration pins is a real restore point for it.

The generation and its activation are the canary's own writers; only the
declaration differs, because an initialised repository is not throwaway.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Final

from eawf.kernel.migration.epoch2.canary import DisposableTarget
from eawf.kernel.migration.epoch2.generation import verify_selected_generation
from eawf.kernel.migration.epoch2.opt_in import declare_opt_in
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.platform.install.canary import activate_born_generation, write_born_generation

logger = logging.getLogger(__name__)

#: Who a born tree's opt-in says declared it.
INIT_DECLARED_BY: Final = "eawf-init"

#: What a born tree's opt-in says it is for.
INIT_PURPOSE: Final = "born at authority epoch 2 by eawf init"

#: The note the pinned backup carries.
INIT_BACKUP_NOTE: Final = "eawf init: the skeleton an epoch-2 tree was born from"


def bear_epoch2_tree(state_path: Path, *, project_code: str, born_at: datetime) -> str:
    """Make the tree holding ``state_path`` an activated epoch-2 tree.

    Args:
        state_path: The ``state.json`` the wizard wrote; its directory is
            the tree root.
        project_code: The project the tree was initialised for; it names
            the generation.
        born_at: When the tree is activated.

    Returns:
        The generation the tree reads from.

    Raises:
        BackupError: The skeleton could not be backed up.
        MigrationReadSmokeFailedError: The born generation does not read
            back through the public readers.
        RuntimeError: The finished tree does not resolve to epoch 2.
        OSError: A file could not be written.
    """
    tree_root = state_path.parent
    declare_opt_in(
        state_path,
        declared_by=INIT_DECLARED_BY,
        purpose=INIT_PURPOSE,
        note=INIT_BACKUP_NOTE,
        when=born_at,
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
    logger.info(f"bear_epoch2_tree code={project_code} generation={generation_id}")
    return generation_id


__all__ = ["INIT_DECLARED_BY", "INIT_PURPOSE", "bear_epoch2_tree"]
