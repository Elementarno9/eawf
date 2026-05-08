"""JSONL store compaction: deduplicates records by id, keeping the last occurrence.

Compaction is idempotent and preserves first-seen insertion order.
All reads and writes are performed under the portalocker advisory lock.
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from eawf.lock import portalock
from eawf.store.envelope import Envelope
from eawf.store.kinds import PAYLOAD_MODELS

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CompactReport:
    """Summary of a compaction run."""

    records_in: int
    records_out: int
    dedup_count: int


def compact_store(path: Path) -> CompactReport:
    """Deduplicate a JSONL store file in-place.

    Reads all records, keeps the *last* envelope for each ``id`` while
    preserving the *first-seen* order of ids, then atomically replaces
    the file via a tempfile + ``os.replace``.

    Args:
        path: Path to the ``.jsonl`` store file.

    Returns:
        A :class:`CompactReport` with ``records_in``, ``records_out``, and
        ``dedup_count``.
    """
    path = Path(path)
    if not path.exists():
        return CompactReport(0, 0, 0)

    with portalock.acquire(path, timeout=5.0):
        raw_lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        records_in = len(raw_lines)

        if records_in == 0:
            return CompactReport(0, 0, 0)

        latest: dict[str, Envelope] = {}
        order: list[str] = []
        for line in raw_lines:
            env = Envelope.model_validate_json(line)
            if env.kind not in PAYLOAD_MODELS:
                raise ValueError(f"unknown store kind {env.kind!r}")
            PAYLOAD_MODELS[env.kind].model_validate(env.payload)
            if env.id in latest and latest[env.id].kind != env.kind:
                raise ValueError(
                    f"kind drift on id {env.id!r}: {latest[env.id].kind.value} → {env.kind.value}"
                )
            if env.id not in latest:
                order.append(env.id)
            latest[env.id] = env

        records_out = len(latest)

        suffix = secrets.token_hex(4)
        tmp = path.with_name(f"{path.name}.tmp.{suffix}")
        try:
            with tmp.open("wb") as fh:
                for env_id in order:
                    fh.write(latest[env_id].model_dump_json().encode("utf-8"))
                    fh.write(b"\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

        report = CompactReport(
            records_in=records_in,
            records_out=records_out,
            dedup_count=records_in - records_out,
        )
        logger.info(f"compact_store {path} in={records_in} out={records_out}")
        return report
