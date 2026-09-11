"""eawf JSONL store package.

Re-exports the canonical surface so callers can write::

    from eawf.kernel.store import Event, EventPayload, EventKind, append_envelope

instead of reaching into submodules. Per C07b (Q14 / D14) the
:class:`Event` model is the single source of truth for event envelopes:
C02 streaming, C06 reactivity, C09 telemetry, and C11 webhook ingress
all consume this shape rather than defining their own.
"""

from __future__ import annotations

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
