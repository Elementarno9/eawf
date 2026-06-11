"""Tests for the ``journal_chain`` audit-DSL kind (P30-I16-W17).

The kind validates that a WAL / journal directory is an intact digest
chain: each record's stored digest recomputes, and each links back to its
predecessor. An intact chain passes; a tampered body or a broken link
fails. The kind reuses the daemon WAL primitive's
``compute_record_digest`` / ``verify_record_digest`` so the audit check and
the live replay refusal share one integrity definition.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import orjson
import pytest

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.runtime.daemon.wal import WAL_CHAIN_GENESIS, WalRecord, write_pending
from eawf.workflow.audit_dsl import CHECK_REGISTRY, CheckResult, CheckSpec
from eawf.workflow.audit_dsl.kinds.journal_chain import (
    JOURNAL_CHAIN_KIND,
    check_journal_chain,
)
from eawf.workflow.verify.readiness import wired_audit_dsl_kinds

pytestmark = pytest.mark.unit

_BASE_TIME = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)


def _envelope(env_id: str) -> Envelope:
    return Envelope(
        id=env_id,
        kind=StoreKind.EVENT,
        scope_id=None,
        created_at=_BASE_TIME,
        summary="test envelope",
        payload={"action": "noop"},
    )


def _record(record_id: str, *, prev_digest: str, offset_seconds: int) -> WalRecord:
    """Build a record linked to *prev_digest*, ordered by *offset_seconds*."""
    return WalRecord(
        record_id=record_id,
        envelope=_envelope(f"env-{record_id}"),
        idempotency_key=None,
        written_at=_BASE_TIME + timedelta(seconds=offset_seconds),
        before_state_version=f"sha:before-{record_id}",
        after_state_version=f"sha:after-{record_id}",
        prev_digest=prev_digest,
    )


def _seed_intact_chain(wal_dir: Path, length: int = 3) -> list[WalRecord]:
    """Write a *length*-record intact chain to *wal_dir*; return the records."""
    records: list[WalRecord] = []
    prev = WAL_CHAIN_GENESIS
    for index in range(length):
        record = _record(f"rec-{index:02d}", prev_digest=prev, offset_seconds=index)
        write_pending(wal_dir, record)
        records.append(record)
        assert record.digest is not None
        prev = record.digest
    return records


def _run(wal_dir: Path, cwd: Path) -> CheckResult:
    spec = CheckSpec(
        kind="journal_chain",
        name="journal_chain",
        args={"path": str(wal_dir.relative_to(cwd))},
    )
    return CHECK_REGISTRY["journal_chain"](spec, cwd)


def test_journal_chain_is_registered_to_its_callable() -> None:
    # The registry entry is the module's check callable -- proves the kind is
    # bound, not registered-but-idle.
    assert JOURNAL_CHAIN_KIND == "journal_chain"
    assert CHECK_REGISTRY[JOURNAL_CHAIN_KIND] is check_journal_chain


def test_journal_chain_is_wired_at_structural_tier() -> None:
    # The wired-on sweep (idle-contract gate) treats the kind as production
    # bound via the supplemental T2 checkout-gate tier.
    assert JOURNAL_CHAIN_KIND in wired_audit_dsl_kinds()


def test_journal_chain_callable_runs_directly(tmp_path: Path) -> None:
    # Call the symbol directly (not just via the registry dict) so the contract
    # has an asserting call-site.
    wal_dir = tmp_path / "wal"
    _seed_intact_chain(wal_dir, length=2)
    spec = CheckSpec(kind="journal_chain", name="journal_chain", args={"path": "wal"})
    result = check_journal_chain(spec, tmp_path)
    assert result.passed is True


def test_journal_chain_passes_for_intact_chain(tmp_path: Path) -> None:
    wal_dir = tmp_path / "wal"
    _seed_intact_chain(wal_dir, length=3)

    result = _run(wal_dir, tmp_path)

    assert result.passed is True
    assert result.status == "pass"
    assert "records=3" in (result.details or "")


def test_journal_chain_passes_for_empty_dir(tmp_path: Path) -> None:
    """A directory with no records is a vacuously-intact chain."""
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir()

    result = _run(wal_dir, tmp_path)

    assert result.passed is True
    assert result.status == "pass"


def test_journal_chain_fails_for_tampered_body(tmp_path: Path) -> None:
    """Editing a digested field on disk breaks that record's digest."""
    wal_dir = tmp_path / "wal"
    _seed_intact_chain(wal_dir, length=3)
    # Tamper the middle record's persisted body, leaving its stale digest.
    middle = wal_dir / "rec-01.pending.json"
    body = orjson.loads(middle.read_bytes())
    body["after_state_version"] = "sha:ATTACKER"
    middle.write_bytes(orjson.dumps(body))

    result = _run(wal_dir, tmp_path)

    assert result.passed is False
    assert result.status == "fail"
    assert "rec-01" in (result.details or "")
    assert "digest mismatch" in (result.details or "")


def test_journal_chain_fails_for_broken_link(tmp_path: Path) -> None:
    """A record whose prev_digest does not point at its predecessor fails.

    Re-pointing prev_digest is itself a digested field, so the record's own
    digest must be recomputed to keep it internally consistent -- this models
    an attacker who rewrote a record (and re-stamped its digest) but cannot
    forge the predecessor link the rest of the chain expects.
    """
    wal_dir = tmp_path / "wal"
    records = _seed_intact_chain(wal_dir, length=3)
    # Rebuild the middle record with a wrong prev_digest and re-stamp it
    # (digest=None forces the validator to recompute), then overwrite on disk.
    forged = records[1].model_copy(update={"prev_digest": "genesis", "digest": None})
    forged = WalRecord.model_validate(forged.model_dump(mode="json"))
    middle = wal_dir / "rec-01.pending.json"
    middle.write_bytes(orjson.dumps(forged.model_dump(mode="json")) + b"\n")

    result = _run(wal_dir, tmp_path)

    assert result.passed is False
    assert result.status == "fail"
    assert "rec-01" in (result.details or "")
    assert "does not link" in (result.details or "")


def test_journal_chain_fails_for_missing_path(tmp_path: Path) -> None:
    spec = CheckSpec(kind="journal_chain", name="journal_chain", args={"path": "no-such-dir"})
    result = CHECK_REGISTRY["journal_chain"](spec, tmp_path)
    assert result.passed is False
    assert result.status == "fail"
    assert "not a directory" in (result.details or "")


def test_journal_chain_fails_for_missing_arg(tmp_path: Path) -> None:
    spec = CheckSpec(kind="journal_chain", name="journal_chain", args={})
    result = CHECK_REGISTRY["journal_chain"](spec, tmp_path)
    assert result.passed is False
    assert result.status == "fail"
    assert "path" in (result.details or "")


def test_journal_chain_fails_for_corrupt_record(tmp_path: Path) -> None:
    """A record that cannot parse is itself a chain integrity failure."""
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir()
    (wal_dir / "rec-bad.pending.json").write_bytes(b"{not-json")

    result = _run(wal_dir, tmp_path)

    assert result.passed is False
    assert result.status == "fail"
    assert "unreadable" in (result.details or "")
