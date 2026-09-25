"""A ledger killed mid-append reads again after the next boot, WAL or not.

Each test kills the in-flight cap verb inside a ledger append, after part
of the line reached the disk and before its newline did, then runs the
daemon's boot passes in the order ``run`` does: the torn-tail repair, the
native replay, the compaction recovery. The WAL record that would name the
tree is swept in the cases that matter, because that is the gap the repair
closes: recovery that finds trees only through the WAL never sees them.

The kill is a ``BaseException``, so no handler on the way out tidies up
after it; what the disk holds when it propagates is what a killed process
leaves.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Any, Final

import orjson
import pytest

from eawf.kernel.store import ledger as ledger_module
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.ledger import (
    LedgerRecord,
    LedgerTornTailError,
    read_ledger_records,
    render_ledger_line,
    split_torn_tail,
)
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.platform.registry.models import Registry, RegistryRepoEntry
from eawf.runtime.daemon import epoch2_transaction, methods
from eawf.runtime.daemon.epoch2_recovery import (
    TAIL_REPAIR_EVENT_SUFFIX,
    LedgerTailRepairReport,
    recover_native_store_trees,
    repair_native_ledger_tails,
    replay_native_wal,
)
from eawf.runtime.daemon.main import _native_tree_roots
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.run_budget import RUN_BUDGET_METER_METHOD
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    document_path,
    firehose_path,
    method_context,
    provision,
    seed,
    seed_row,
    tree_root,
)

pytestmark = pytest.mark.integration

RUN_KEY: Final = "RUN-00000010"
RUN_URN: Final = f"eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/{RUN_KEY}"

#: A reading well over the cap, with no process group to signal, so the
#: verb appends three run-ledger lines: the notice, the request and the ack.
CAP: Final = 1_000
OVER_CAP_OUTPUT: Final = 5_000

REPAIR_NAME: Final = f"ledger.run.{TAIL_REPAIR_EVENT_SUFFIX}"


class _Kill(BaseException):
    """The process dying at the seam a test chose."""


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A provisioned canary holding one running Run."""
    provisioned = provision(tmp_path / "repo")
    seed(provisioned, {"run": {RUN_KEY: seed_row("run", "RUNNING")}})
    return provisioned


@pytest.fixture
def ctx(tmp_path: Path) -> MethodContext:
    """A daemon context with a WAL directory of its own."""
    return method_context(tmp_path / "runtime")


def _wal_dir(ctx: MethodContext) -> Path:
    assert isinstance(ctx.wal_dir, Path)
    return ctx.wal_dir


def _meter(ctx: MethodContext, canary: CanaryProvision) -> dict[str, Any]:
    """Drive the in-flight cap verb over one reading past the cap."""
    params: dict[str, Any] = {
        "repo_root": str(canary.root),
        "urn": RUN_URN,
        "control_request_ref": "CTL-0000000a",
        "actor": "OP-0001",
        "samples": [{"input_tokens": 100, "output_tokens": OVER_CAP_OUTPUT}],
        "base_budget": CAP,
        "enforce": "hard",
        "multiplier": 1.0,
        "idempotency_key": "idem-budget",
    }
    return asyncio.run(methods.dispatch(RUN_BUDGET_METER_METHOD, ctx, params))


def _tear_append(monkeypatch: pytest.MonkeyPatch, *, on_call: int) -> None:
    """Kill the *on_call*-th ledger append after half its line is on disk."""
    real = epoch2_transaction.append_ledger_record
    calls = {"n": 0}

    def torn(path: Path, record: LedgerRecord, **kwargs: Any) -> int:
        calls["n"] += 1
        if calls["n"] < on_call:
            return real(path, record, **kwargs)
        line = render_ledger_line(record).encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as handle:
            handle.write(line[: len(line) // 2])
        raise _Kill

    monkeypatch.setattr(epoch2_transaction, "append_ledger_record", torn)


def _ledger(canary: CanaryProvision) -> Path:
    return ledger_path(document_path(canary), Epoch2Collection.RUN)


def _rows(canary: CanaryProvision) -> list[dict[str, Any]]:
    path = firehose_path(canary)
    if not path.exists():
        return []
    return [orjson.loads(line) for line in path.read_bytes().splitlines() if line.strip()]


def _repair_rows(canary: CanaryProvision) -> list[dict[str, Any]]:
    return [row for row in _rows(canary) if row["payload"]["name"] == REPAIR_NAME]


def _sweep_wal(ctx: MethodContext) -> None:
    """Remove every native WAL record, as retention GC eventually does."""
    shutil.rmtree(_wal_dir(ctx) / "native", ignore_errors=True)


def _boot(
    ctx: MethodContext, canary: CanaryProvision, *, scan: bool = True
) -> LedgerTailRepairReport | None:
    """Run the native boot passes in ``run``'s order."""
    report = None
    if scan:
        report = repair_native_ledger_tails(_wal_dir(ctx), tree_roots=(tree_root(canary),))
    replay_native_wal(_wal_dir(ctx))
    recover_native_store_trees(_wal_dir(ctx))
    return report


def _killed(ctx: MethodContext, canary: CanaryProvision, mp: pytest.MonkeyPatch, n: int) -> None:
    _tear_append(mp, on_call=n)
    with pytest.raises(_Kill):
        _meter(ctx, canary)
    mp.undo()


def test_repair_native_ledger_tails_cuts_a_tail_no_wal_record_names(
    ctx: MethodContext, canary: CanaryProvision, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate: swept WAL, torn tail, and the next boot reads the ledger."""
    _killed(ctx, canary, monkeypatch, 3)
    _sweep_wal(ctx)
    before = _ledger(canary).read_bytes()
    kept, torn = split_torn_tail(before)
    assert torn

    report = _boot(ctx, canary)

    records = read_ledger_records(_ledger(canary))
    assert [item.payload.get("payload_kind") for item in records] == ["budget_notice", "control"]
    assert _ledger(canary).read_bytes() == kept
    assert report == LedgerTailRepairReport(
        tree_count=1, truncated_ledgers=1, journaled_rows=1, dropped_bytes=len(torn)
    )
    rows = _repair_rows(canary)
    assert len(rows) == 1
    assert rows[0]["payload"]["kept_bytes"] == len(kept)
    assert rows[0]["payload"]["dropped_bytes"] == len(torn)
    assert "canonical_sequence" not in rows[0]["payload"]
    Envelope.model_validate(rows[0])


def test_boot_without_the_scan_leaves_the_swept_torn_ledger_unreadable(
    ctx: MethodContext, canary: CanaryProvision, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect the scan closes: WAL-driven recovery never finds the tree."""
    _killed(ctx, canary, monkeypatch, 3)
    _sweep_wal(ctx)

    _boot(ctx, canary, scan=False)

    with pytest.raises(LedgerTornTailError):
        read_ledger_records(_ledger(canary))


def test_repair_native_ledger_tails_is_idempotent_on_a_second_boot(
    ctx: MethodContext, canary: CanaryProvision, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second boot finds nothing to cut and journals nothing."""
    _killed(ctx, canary, monkeypatch, 2)
    _sweep_wal(ctx)
    _boot(ctx, canary)
    ledger_bytes = _ledger(canary).read_bytes()
    firehose_bytes = firehose_path(canary).read_bytes()

    report = _boot(ctx, canary)

    assert report is not None
    assert report.truncated_ledgers == 0
    assert report.journaled_rows == 0
    assert _ledger(canary).read_bytes() == ledger_bytes
    assert firehose_path(canary).read_bytes() == firehose_bytes


def test_repair_runs_before_the_replay_so_a_pending_line_lands_clean(
    ctx: MethodContext, canary: CanaryProvision, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Torn tail plus a WAL record whose line and firehose row are pending."""
    _killed(ctx, canary, monkeypatch, 1)
    assert _rows(canary) == []

    _boot(ctx, canary)

    records = read_ledger_records(_ledger(canary))
    assert [item.payload.get("payload_kind") for item in records] == ["budget_notice"]
    assert _ledger(canary).read_bytes().count(b"\n") == 1
    names = [row["payload"]["name"] for row in _rows(canary)]
    assert names == [REPAIR_NAME, "ledger.run.appended"]


def test_replay_alone_cannot_finish_a_pending_line_behind_a_torn_tail(
    ctx: MethodContext, canary: CanaryProvision, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Why the order matters: the replay's own read refuses a torn ledger."""
    _killed(ctx, canary, monkeypatch, 1)

    with pytest.raises(LedgerTornTailError):
        replay_native_wal(_wal_dir(ctx))


def test_repair_journals_once_when_a_crash_split_the_row_from_the_cut(
    ctx: MethodContext, canary: CanaryProvision, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A boot killed after journalling and before cutting re-cuts silently."""
    _killed(ctx, canary, monkeypatch, 2)
    _sweep_wal(ctx)

    def dead(_path: Path) -> int:
        raise _Kill

    monkeypatch.setattr("eawf.runtime.daemon.epoch2_recovery.truncate_torn_tail", dead)
    with pytest.raises(_Kill):
        _boot(ctx, canary)
    monkeypatch.undo()
    assert len(_repair_rows(canary)) == 1

    report = _boot(ctx, canary)

    assert report is not None
    assert report.truncated_ledgers == 1
    assert report.journaled_rows == 0
    assert len(_repair_rows(canary)) == 1
    assert len(read_ledger_records(_ledger(canary))) == 1


def test_repair_native_ledger_tails_empties_a_ledger_holding_only_a_fragment(
    ctx: MethodContext, canary: CanaryProvision, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary: no complete line at all, so nothing is kept."""
    _killed(ctx, canary, monkeypatch, 1)
    _sweep_wal(ctx)

    _boot(ctx, canary)

    assert _ledger(canary).read_bytes() == b""
    assert read_ledger_records(_ledger(canary)) == ()
    assert len(_repair_rows(canary)) == 1


def test_repair_native_ledger_tails_leaves_a_clean_ledger_byte_identical(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    """Boundary: complete records only, so no byte moves and no row lands."""
    _meter(ctx, canary)
    before = _ledger(canary).read_bytes()
    rows_before = _rows(canary)

    report = repair_native_ledger_tails(_wal_dir(ctx), tree_roots=(tree_root(canary),))

    assert report.tree_count == 1
    assert report.truncated_ledgers == 0
    assert _ledger(canary).read_bytes() == before
    assert _rows(canary) == rows_before


def test_repair_native_ledger_tails_skips_roots_that_are_not_epoch2(
    tmp_path: Path, ctx: MethodContext
) -> None:
    """Error path: an epoch-1 or missing root is never scanned or written."""
    epoch1 = tmp_path / "plain" / ".ea"
    epoch1.mkdir(parents=True)

    report = repair_native_ledger_tails(
        _wal_dir(ctx), tree_roots=(epoch1, tmp_path / "missing" / ".ea")
    )

    assert report == LedgerTailRepairReport()
    assert list(epoch1.iterdir()) == []


def test_repair_native_ledger_tails_counts_an_unreadable_tree_as_skipped(
    ctx: MethodContext, canary: CanaryProvision, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error path: one tree failing does not stop the boot."""
    _killed(ctx, canary, monkeypatch, 2)

    def broken(_content: bytes) -> tuple[bytes, bytes]:
        raise OSError("disk went away")

    monkeypatch.setattr("eawf.runtime.daemon.epoch2_recovery.split_torn_tail", broken)
    report = repair_native_ledger_tails(_wal_dir(ctx), tree_roots=(tree_root(canary),))

    assert report.skipped_trees == 1
    assert report.truncated_ledgers == 0


def test_split_torn_tail_boundaries(tmp_path: Path) -> None:
    """Empty, clean, fragment-only and mixed contents split as documented."""
    assert split_torn_tail(b"") == (b"", b"")
    assert split_torn_tail(b"a\n") == (b"a\n", b"")
    assert split_torn_tail(b"frag") == (b"", b"frag")
    assert split_torn_tail(b"a\nb\nfr") == (b"a\nb\n", b"fr")
    assert ledger_module.truncate_torn_tail(tmp_path / "absent.jsonl") == 0


def test_native_tree_roots_reads_the_registry_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound tree first, then every registered repository's tree."""
    registry_path = tmp_path / "registry.json"
    registry = Registry(repos={"ABC": RegistryRepoEntry(code="ABC", path=str(tmp_path / "abc"))})
    registry_path.write_text(registry.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("EAWF_REGISTRY_PATH", str(registry_path))
    state = tmp_path / "bound" / ".ea" / "state.json"

    assert _native_tree_roots(state) == (state.parent, tmp_path / "abc" / ".ea")


def test_native_tree_roots_survives_an_unreadable_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error path: a missing registry still yields the bound tree."""
    monkeypatch.setenv("EAWF_REGISTRY_PATH", str(tmp_path / "absent.json"))
    state = tmp_path / "bound" / ".ea" / "state.json"

    assert _native_tree_roots(state) == (state.parent,)
