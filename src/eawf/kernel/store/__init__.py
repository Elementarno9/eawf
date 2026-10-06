"""eawf JSONL store package.

Re-exports the canonical surface so callers can write::

    from eawf.kernel.store import Event, EventPayload, EventKind, append_envelope

instead of reaching into submodules. Per C07b (Q14 / D14) the
:class:`Event` model is the single source of truth for event envelopes:
C02 streaming, C06 reactivity, C09 telemetry, and C11 webhook ingress
all consume this shape rather than defining their own.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from eawf.kernel.state.enums import StoreKind
    from eawf.kernel.store.append import append_envelope, append_json_line
    from eawf.kernel.store.commit_census import GitUnavailableError, run_census
    from eawf.kernel.store.commit_policy import (
        EA_PATH_CLASSES,
        CensusFinding,
        CensusFindingKind,
        CommitPolicy,
        CommitPolicyError,
        PathClass,
        UndeclaredPathError,
        census_findings,
        classify_path,
        probe_paths,
    )
    from eawf.kernel.store.compact import CompactReport, compact_store
    from eawf.kernel.store.compaction import (
        CompactionCrashError,
        CompactionCrashPoint,
        CompactionResult,
        RecordLocation,
        RecoveryReport,
        compact_terminal_record,
        compact_terminal_task,
        locate_record,
        recover_store_tree,
    )
    from eawf.kernel.store.envelope import Envelope
    from eawf.kernel.store.generated import (
        GENERATED_ROOTS,
        HAND_AUTHORED_DIRS,
        GeneratedRetention,
        GeneratedRoot,
        GeneratedWriteError,
        generated_root_for,
        guard_generated_write,
    )
    from eawf.kernel.store.index import (
        LedgerIndex,
        LedgerIndexEntry,
        build_ledger_index,
        regenerate_indexes,
        write_ledger_index,
    )
    from eawf.kernel.store.kinds import PAYLOAD_MODELS
    from eawf.kernel.store.kinds.event import Event, EventKind, EventPayload
    from eawf.kernel.store.ledger import (
        LedgerAppendOnlyError,
        LedgerError,
        LedgerRecord,
        LedgerTornTailError,
        append_correction,
        append_ledger_record,
        effective_records,
        guarded_ledger_write,
        read_ledger_records,
        verify_append_only,
    )
    from eawf.kernel.store.paths import (
        index_path,
        ledger_path,
        store_dir,
        store_path,
        store_paths,
    )
    from eawf.kernel.store.tiers import (
        LEDGER_COLLECTIONS,
        TIER_TABLE,
        Epoch2Collection,
        StorageTier,
        TierAssignment,
        TierTableError,
        compile_tier_table,
        tier_for,
    )

# Re-exports resolve on first access: importing one store submodule (the
# statusline reads a ledger on every redraw) must not load every payload model.
_EXPORTS: dict[str, str] = {
    "StoreKind": "eawf.kernel.state.enums",
    "append_envelope": "eawf.kernel.store.append",
    "append_json_line": "eawf.kernel.store.append",
    "GitUnavailableError": "eawf.kernel.store.commit_census",
    "run_census": "eawf.kernel.store.commit_census",
    "EA_PATH_CLASSES": "eawf.kernel.store.commit_policy",
    "CensusFinding": "eawf.kernel.store.commit_policy",
    "CensusFindingKind": "eawf.kernel.store.commit_policy",
    "CommitPolicy": "eawf.kernel.store.commit_policy",
    "CommitPolicyError": "eawf.kernel.store.commit_policy",
    "PathClass": "eawf.kernel.store.commit_policy",
    "UndeclaredPathError": "eawf.kernel.store.commit_policy",
    "census_findings": "eawf.kernel.store.commit_policy",
    "classify_path": "eawf.kernel.store.commit_policy",
    "probe_paths": "eawf.kernel.store.commit_policy",
    "CompactReport": "eawf.kernel.store.compact",
    "compact_store": "eawf.kernel.store.compact",
    "CompactionCrashError": "eawf.kernel.store.compaction",
    "CompactionCrashPoint": "eawf.kernel.store.compaction",
    "CompactionResult": "eawf.kernel.store.compaction",
    "RecordLocation": "eawf.kernel.store.compaction",
    "RecoveryReport": "eawf.kernel.store.compaction",
    "compact_terminal_record": "eawf.kernel.store.compaction",
    "compact_terminal_task": "eawf.kernel.store.compaction",
    "locate_record": "eawf.kernel.store.compaction",
    "recover_store_tree": "eawf.kernel.store.compaction",
    "Envelope": "eawf.kernel.store.envelope",
    "GENERATED_ROOTS": "eawf.kernel.store.generated",
    "HAND_AUTHORED_DIRS": "eawf.kernel.store.generated",
    "GeneratedRetention": "eawf.kernel.store.generated",
    "GeneratedRoot": "eawf.kernel.store.generated",
    "GeneratedWriteError": "eawf.kernel.store.generated",
    "generated_root_for": "eawf.kernel.store.generated",
    "guard_generated_write": "eawf.kernel.store.generated",
    "LedgerIndex": "eawf.kernel.store.index",
    "LedgerIndexEntry": "eawf.kernel.store.index",
    "build_ledger_index": "eawf.kernel.store.index",
    "regenerate_indexes": "eawf.kernel.store.index",
    "write_ledger_index": "eawf.kernel.store.index",
    "PAYLOAD_MODELS": "eawf.kernel.store.kinds",
    "Event": "eawf.kernel.store.kinds.event",
    "EventKind": "eawf.kernel.store.kinds.event",
    "EventPayload": "eawf.kernel.store.kinds.event",
    "LedgerAppendOnlyError": "eawf.kernel.store.ledger",
    "LedgerError": "eawf.kernel.store.ledger",
    "LedgerRecord": "eawf.kernel.store.ledger",
    "LedgerTornTailError": "eawf.kernel.store.ledger",
    "append_correction": "eawf.kernel.store.ledger",
    "append_ledger_record": "eawf.kernel.store.ledger",
    "effective_records": "eawf.kernel.store.ledger",
    "guarded_ledger_write": "eawf.kernel.store.ledger",
    "read_ledger_records": "eawf.kernel.store.ledger",
    "verify_append_only": "eawf.kernel.store.ledger",
    "index_path": "eawf.kernel.store.paths",
    "ledger_path": "eawf.kernel.store.paths",
    "store_dir": "eawf.kernel.store.paths",
    "store_path": "eawf.kernel.store.paths",
    "store_paths": "eawf.kernel.store.paths",
    "LEDGER_COLLECTIONS": "eawf.kernel.store.tiers",
    "TIER_TABLE": "eawf.kernel.store.tiers",
    "Epoch2Collection": "eawf.kernel.store.tiers",
    "StorageTier": "eawf.kernel.store.tiers",
    "TierAssignment": "eawf.kernel.store.tiers",
    "TierTableError": "eawf.kernel.store.tiers",
    "compile_tier_table": "eawf.kernel.store.tiers",
    "tier_for": "eawf.kernel.store.tiers",
}


def __getattr__(name: str) -> Any:
    """Import the submodule that defines *name* and return the name.

    Raises:
        AttributeError: *name* is not a re-exported name.
    """
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module), name)
    globals()[name] = value
    return value


__all__ = [
    "EA_PATH_CLASSES",
    "GENERATED_ROOTS",
    "HAND_AUTHORED_DIRS",
    "LEDGER_COLLECTIONS",
    "PAYLOAD_MODELS",
    "TIER_TABLE",
    "CensusFinding",
    "CensusFindingKind",
    "CommitPolicy",
    "CommitPolicyError",
    "CompactReport",
    "CompactionCrashError",
    "CompactionCrashPoint",
    "CompactionResult",
    "Envelope",
    "Epoch2Collection",
    "Event",
    "EventKind",
    "EventPayload",
    "GeneratedRetention",
    "GeneratedRoot",
    "GeneratedWriteError",
    "GitUnavailableError",
    "LedgerAppendOnlyError",
    "LedgerError",
    "LedgerIndex",
    "LedgerIndexEntry",
    "LedgerRecord",
    "LedgerTornTailError",
    "PathClass",
    "RecordLocation",
    "RecoveryReport",
    "StorageTier",
    "StoreKind",
    "TierAssignment",
    "TierTableError",
    "UndeclaredPathError",
    "append_correction",
    "append_envelope",
    "append_json_line",
    "append_ledger_record",
    "build_ledger_index",
    "census_findings",
    "classify_path",
    "compact_store",
    "compact_terminal_record",
    "compact_terminal_task",
    "compile_tier_table",
    "effective_records",
    "generated_root_for",
    "guard_generated_write",
    "guarded_ledger_write",
    "index_path",
    "ledger_path",
    "locate_record",
    "probe_paths",
    "read_ledger_records",
    "recover_store_tree",
    "regenerate_indexes",
    "run_census",
    "store_dir",
    "store_path",
    "store_paths",
    "tier_for",
    "verify_append_only",
    "write_ledger_index",
]
