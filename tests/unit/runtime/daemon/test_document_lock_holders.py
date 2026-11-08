"""The document lock's holders are logged, and receipts are read per Run.

The live subagent check failed on lock waits that no lab test reproduced, so
each session logs which daemon method and code path held the document lock
and for how long. The largest holder was a tool result reading every Run's
host receipts, so a receipt read validates only the asked Run's lines.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.kernel.store.ledger import LedgerRecord
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.epoch2_root import DOCUMENT_LOCK_METHOD, Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import commit_ledger_append
from eawf.runtime.daemon.host_calls import HOST_RECEIPT_PAYLOAD_KIND, host_receipts
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    MILESTONE_URN,
    provision,
    root_context,
)

_WANTED = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000001"
_OTHER = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000002"


@pytest.fixture(autouse=True)
def _canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A freshly provisioned epoch-2 canary."""
    return provision(tmp_path / "repo")


@pytest.fixture
def context(canary: CanaryProvision, tmp_path: Path) -> Epoch2RootContext:
    """The canary's native context, with a WAL directory of its own."""
    return root_context(canary, tmp_path / "runtime")


def _hold_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("document_lock ")]


def test_a_session_logs_its_method_holder_and_times(
    context: Epoch2RootContext, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="eawf.runtime.daemon.epoch2_root")
    token = DOCUMENT_LOCK_METHOD.set("runtime.host.tool.observe")
    try:
        with context.session([MILESTONE_URN]):
            pass
    finally:
        DOCUMENT_LOCK_METHOD.reset(token)

    (line,) = _hold_lines(caplog)
    assert "method=runtime.host.tool.observe" in line
    assert "holder=tests.unit.runtime.daemon.test_document_lock_holders." in line
    assert "wait_ms=" in line
    assert "hold_ms=" in line


def test_a_session_outside_any_call_logs_no_method(
    context: Epoch2RootContext, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="eawf.runtime.daemon.epoch2_root")

    with context.session([MILESTONE_URN]):
        pass

    (line,) = _hold_lines(caplog)
    assert "method=- " in line


def test_a_session_whose_block_raises_still_logs_its_hold(
    context: Epoch2RootContext, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="eawf.runtime.daemon.epoch2_root")

    with pytest.raises(RuntimeError, match="boom"), context.session([MILESTONE_URN]):
        raise RuntimeError("boom")

    assert len(_hold_lines(caplog)) == 1


def _receipt_line(key: str, run_ref: object) -> LedgerRecord:
    return LedgerRecord(
        collection=Epoch2Collection.RECEIPT,
        record_key=key,
        status="succeeded",
        recorded_at=AT,
        payload={"payload_kind": HOST_RECEIPT_PAYLOAD_KIND, "run_ref": run_ref},
    )


def test_receipts_of_another_run_are_not_validated(context: Epoch2RootContext) -> None:
    with context.session([MILESTONE_URN]) as session:
        commit_ledger_append(session, _receipt_line("call-other", _OTHER))

        assert host_receipts(session, _WANTED) == ()


def test_a_corrupt_receipt_of_the_asked_run_is_refused(context: Epoch2RootContext) -> None:
    with context.session([MILESTONE_URN]) as session:
        commit_ledger_append(session, _receipt_line("call-wanted", _WANTED))

        with pytest.raises(ValidationError):
            host_receipts(session, _WANTED)


def test_a_receipt_without_a_run_string_is_refused(context: Epoch2RootContext) -> None:
    with context.session([MILESTONE_URN]) as session:
        commit_ledger_append(session, _receipt_line("call-none", None))

        with pytest.raises(ValidationError):
            host_receipts(session, _WANTED)


def test_an_empty_receipt_ledger_reads_as_no_receipts(context: Epoch2RootContext) -> None:
    with context.session([MILESTONE_URN]) as session:
        assert host_receipts(session, _WANTED) == ()
