"""State-schema migration package — ``eawf migrate`` chain runner + steps.

The package owns the version-chain machinery for ``state.json``:

* :mod:`eawf.migrations._base` — the :class:`Migration` protocol, the
  ordered-chain builder, the per-step pre/post invariant runner, the
  backup/restore discipline, and the **canonical-writer route** every
  migration write goes through (``portalock`` + the daemon's
  ``atomic_write_json_locked`` primitive — never the lock-acquiring
  ``atomic_write_json`` bypass).
* ``v1_0_to_v1_1`` (and future ``vX_Y_to_vX_Z`` modules) — one concrete
  :class:`Migration` step apiece.

CLI dispatch lives in :mod:`eawf.cli.commands.migrate`; the migration
logic itself lives here per AGENTS rule 1 (CLI is dispatch; library
implements).
"""

from __future__ import annotations

# Import the concrete step modules for their import-time registration into
# DEFAULT_REGISTRY. Each ``vX_Y_to_vX_Z`` module calls ``_register`` at
# import time, so importing the package wires the full chain.
from eawf.migrations import v1_0_to_v1_1 as _v1_0_to_v1_1  # noqa: F401
from eawf.migrations._base import (
    DEFAULT_REGISTRY,
    Migration,
    MigrationError,
    MigrationStepError,
    build_migration_chain,
    current_target_version,
    run_chain,
    write_canonical,
)

__all__ = [
    "DEFAULT_REGISTRY",
    "Migration",
    "MigrationError",
    "MigrationStepError",
    "build_migration_chain",
    "current_target_version",
    "run_chain",
    "write_canonical",
]
