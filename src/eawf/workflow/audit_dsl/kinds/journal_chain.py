"""``journal_chain`` audit-DSL kind (P30-I16, T2 structural).

Validates that a digest-chained WAL / journal directory is intact: every
record's stored :attr:`~eawf.runtime.daemon.wal.WalRecord.digest` matches a
fresh recompute over its content, and each record links back to its
predecessor (``prev_digest`` of record N equals ``digest`` of record N-1,
in ``written_at`` order). A tampered record body (any digested field
edited on disk) recomputes to a different digest and breaks the chain at
its link, so the kind is the structural falsifier behind the WAL
integrity invariant the daemon's startup replay enforces -- the same
``verify_record_digest`` gate, surfaced as a standalone audit check.

This is the deterministic counterpart to the live replay refusal: replay
poisons a tampered record at boot, while ``journal_chain`` lets a close
gate or audit assert "this journal is an intact chain" without booting the
daemon.

Target resolution
-----------------

The journal target is read from ``spec.args``:

* ``path`` -- a repo-relative path under ``cwd`` (or an absolute path)
  pointing at a WAL directory holding ``<id>.<status>.json`` records. The
  kind loads every live-status record (``pending`` / ``applied`` /
  ``fsynced``; ``poisoned/`` is excluded) and verifies the chain.

Failure handling
----------------

* A malformed ``args`` (missing / non-str ``path``) yields
  ``status="fail"`` -- never a raised exception.
* A missing path or a path that is not a directory yields
  ``status="fail"``.
* A record that fails schema parsing, a record whose digest does not
  recompute, or a broken ``prev_digest`` link all yield ``status="fail"``
  naming the first offending record id.
* An empty (or absent-record) directory is a vacuously-intact chain and
  passes.
"""

from __future__ import annotations

import logging
from pathlib import Path

from eawf.runtime.daemon import wal
from eawf.runtime.daemon.wal import WalRecord, verify_record_digest
from eawf.workflow.audit_dsl.models import CheckResult, CheckSpec

logger = logging.getLogger(__name__)

#: The audit-DSL kind string this module registers.
JOURNAL_CHAIN_KIND: str = "journal_chain"


def _load_chain(wal_dir: Path) -> tuple[list[WalRecord], str | None]:
    """Load every live-status WAL record, sorted by ``written_at``.

    Returns a ``(records, error)`` pair: exactly one is meaningful.
    ``records`` is the ordered record list on success; ``error`` is a
    one-line note when a record cannot be parsed (which is itself a chain
    integrity failure -- a corrupt record breaks the journal).
    """
    records: list[WalRecord] = []
    for path in wal.list_records(wal_dir):
        try:
            records.append(wal.read_record(path))
        except (ValueError, OSError) as exc:
            return [], f"record {path.name!r} is unreadable: {exc}"
    records.sort(key=lambda r: (r.written_at, r.record_id))
    return records, None


def _first_chain_break(records: list[WalRecord]) -> str | None:
    """Return a one-line note for the first broken link, or ``None`` if intact.

    Two integrity properties are checked per record, in order: the record's
    own digest must recompute (catches a tampered body), and its
    ``prev_digest`` must equal the predecessor's ``digest`` (catches a
    deleted / reordered / re-pointed record). The first record links to the
    genesis sentinel.
    """
    expected_prev = wal.WAL_CHAIN_GENESIS
    for record in records:
        if not verify_record_digest(record):
            return f"record {record.record_id!r} digest mismatch (tampered body)"
        if record.prev_digest != expected_prev:
            return (
                f"record {record.record_id!r} prev_digest {record.prev_digest!r} "
                f"does not link to predecessor {expected_prev!r}"
            )
        # ``digest`` is non-None here: verify_record_digest returned True,
        # which requires a stored digest.
        assert record.digest is not None
        expected_prev = record.digest
    return None


def check_journal_chain(spec: CheckSpec, cwd: Path) -> CheckResult:
    """Verify a WAL / journal directory is an intact digest chain.

    Args (read from ``spec.args``):
        path: Repo-relative (or absolute) path to a WAL directory holding
            ``<id>.<status>.json`` records.

    Returns:
        :class:`CheckResult` with ``status="pass"`` when every record's
        digest recomputes and each links to its predecessor;
        ``status="fail"`` (naming the first offending record) when the
        args are malformed, the path is missing / not a directory, a
        record fails to parse, a digest is tampered, or a chain link is
        broken. Never raises -- a bad criterion degrades to a failed
        check, not an aborted run.
    """
    path_arg = spec.args.get("path")
    if not isinstance(path_arg, str) or not path_arg:
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            status="fail",
            details="missing or non-str arg 'path'",
        )
    target = (cwd / path_arg).resolve() if not Path(path_arg).is_absolute() else Path(path_arg)
    if not target.is_dir():
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            status="fail",
            details=f"path={path_arg} is not a directory",
        )

    records, load_error = _load_chain(target)
    if load_error is not None:
        logger.debug(f"check_journal_chain load-fail name={spec.name!r} reason={load_error}")
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            status="fail",
            details=load_error,
        )

    break_note = _first_chain_break(records)
    if break_note is not None:
        logger.debug(f"check_journal_chain broken name={spec.name!r} reason={break_note}")
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            status="fail",
            details=break_note,
        )

    logger.debug(f"check_journal_chain ok name={spec.name!r} records={len(records)}")
    return CheckResult(
        name=spec.name,
        kind=spec.kind,
        passed=True,
        status="pass",
        details=f"intact chain path={path_arg} records={len(records)}",
    )


__all__ = ["JOURNAL_CHAIN_KIND", "check_journal_chain"]
