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
    "LEDGER_COLLECTIONS",
    "PAYLOAD_MODELS",
    "TIER_TABLE",
    "CompactReport",
    "CompactionCrashError",
    "CompactionCrashPoint",
    "CompactionResult",
    "Envelope",
    "Epoch2Collection",
    "Event",
    "EventKind",
    "EventPayload",
    "LedgerAppendOnlyError",
    "LedgerError",
    "LedgerIndex",
    "LedgerIndexEntry",
    "LedgerRecord",
    "LedgerTornTailError",
    "RecordLocation",
    "RecoveryReport",
    "StorageTier",
    "StoreKind",
    "TierAssignment",
    "TierTableError",
    "append_correction",
    "append_envelope",
    "append_json_line",
    "append_ledger_record",
    "build_ledger_index",
    "compact_store",
    "compact_terminal_record",
    "compact_terminal_task",
    "compile_tier_table",
    "effective_records",
    "guarded_ledger_write",
    "index_path",
    "ledger_path",
    "locate_record",
    "read_ledger_records",
    "recover_store_tree",
    "regenerate_indexes",
    "store_dir",
    "store_path",
    "store_paths",
    "tier_for",
    "verify_append_only",
    "write_ledger_index",
]
