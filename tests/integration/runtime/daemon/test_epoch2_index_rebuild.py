"""After a run of terminal transitions, what is left and what regenerates.

Two promises are checked here and they pull in opposite directions. The
document must shrink to the work still in flight, which means bytes leave
it; and nothing the tree still needs may be lost with them, which means
the ledger and the index over it have to account for every record that
left.

The index is the part that is allowed to be thrown away. It is a pure
function of the ledger's bytes -- no clock, no filesystem, no process
identity -- so the test for it is not that it exists but that deleting it
and rebuilding it at daemon start reproduces the previous file byte for
byte. Every offset it publishes is then read back out of the ledger and
digested, so an index that agreed with itself while addressing the wrong
bytes would still fail.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf.kernel.store.compaction import read_document
from eawf.kernel.store.index import build_ledger_index, render_index
from eawf.kernel.store.ledger import line_digest, read_ledger_records, render_ledger_line
from eawf.kernel.store.paths import index_dir, index_path, ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import main as daemon_main
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import (
    CANONICAL_SEQUENCE_KEY,
    TransitionRequest,
    run_transaction,
)
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    MILESTONE_URN,
    document_path,
    provision,
    rekeyed,
    root_context,
    seed,
    seed_row,
)

pytestmark = pytest.mark.integration

ACTOR = "OP-0001"
REASON = "operator-cancelled"

#: The Milestones this suite drives. Three are cancelled and leave the
#: document; two are activated and stay in it, which is what makes "only
#: work in flight" a claim with a counter-example rather than a tautology.
CANCELLED_KEYS = ("MLS-0040", "MLS-0041", "MLS-0042")
ACTIVE_KEYS = ("MLS-0043", "MLS-0044")

_URN_BASE = MILESTONE_URN.rsplit("/", 1)[0]
_BOOT_SKIP_REASON = "daemon boot path is POSIX-only in this suite"


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A canary holding five planned Milestones, keyed apart."""
    provisioned = provision(tmp_path / "repo")
    planned = seed_row("milestone", "PLANNED")
    seed(
        provisioned,
        {"milestone": {key: rekeyed(planned, key=key) for key in (*CANCELLED_KEYS, *ACTIVE_KEYS)}},
    )
    return provisioned


@pytest.fixture
def runtime_root(tmp_path: Path) -> Path:
    """The daemon runtime directory the native WAL is namespaced under."""
    return tmp_path / "runtime"


@pytest.fixture
def context(canary: CanaryProvision, runtime_root: Path) -> Epoch2RootContext:
    """The native context of the canary, with a WAL directory of its own."""
    return root_context(canary, runtime_root)


def _move(context: Epoch2RootContext, key: str, *, to_status: str) -> None:
    run_transaction(
        context=context,
        request=TransitionRequest.model_validate(
            {
                "urn": f"{_URN_BASE}/{key}",
                "to_status": to_status,
                "expected_revision": 1,
                "idempotency_key": f"req-{key}",
                "actor": ACTOR,
                "reason_code": REASON,
            }
        ),
        now=AT,
    )


def _run_the_batch(context: Epoch2RootContext) -> None:
    """Cancel three Milestones and activate two, in one pass."""
    for key in CANCELLED_KEYS:
        _move(context, key, to_status="CANCELLED")
    for key in ACTIVE_KEYS:
        _move(context, key, to_status="ACTIVE")


def _milestone_ledger(canary: CanaryProvision) -> Path:
    return ledger_path(document_path(canary), Epoch2Collection.MILESTONE)


def _milestone_index(canary: CanaryProvision) -> Path:
    return index_path(document_path(canary), Epoch2Collection.MILESTONE)


def _document_keys(canary: CanaryProvision) -> list[str]:
    rows = read_document(document_path(canary)).get("milestone", {})
    assert isinstance(rows, dict)
    return sorted(rows)


def _index_bytes(canary: CanaryProvision) -> dict[str, bytes]:
    """Return every derived index file of the tree, keyed by file name."""
    directory = index_dir(document_path(canary))
    if not directory.exists():
        return {}
    return {path.name: path.read_bytes() for path in sorted(directory.iterdir())}


def _boot(runtime_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    """Run the daemon's boot prologue with its listener stubbed out."""
    project = tmp_path / "project" / ".ea"
    project.mkdir(parents=True, exist_ok=True)
    (project / "state.json").write_bytes(orjson.dumps({"placeholder": True}))
    monkeypatch.setenv("EA_STATE", str(project / "state.json"))
    monkeypatch.setattr(daemon_main, "ensure_runtime_dir", lambda: runtime_root)
    monkeypatch.setattr(daemon_main, "pid_path", lambda: runtime_root / "eawfd.pid")
    monkeypatch.setattr(daemon_main, "log_path", lambda: runtime_root / "eawfd.log")
    monkeypatch.setattr(daemon_main, "socket_path", lambda: runtime_root / "eawfd.sock")
    monkeypatch.setattr(
        daemon_main.asyncio, "run", lambda coro: coro.close() if hasattr(coro, "close") else None
    )
    return daemon_main.run(foreground=True)


# ---------------------------------------------------------------------------
# What the document is left holding
# ---------------------------------------------------------------------------


def test_a_run_of_terminal_transitions_leaves_only_work_in_flight(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    _run_the_batch(context)

    assert _document_keys(canary) == sorted(ACTIVE_KEYS)
    assert sorted(
        line.record_key for line in read_ledger_records(_milestone_ledger(canary))
    ) == sorted(CANCELLED_KEYS)


def test_the_run_leaves_the_high_water_mark_at_its_last_ordinal(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    """Compaction rewrites the document, so it must carry the counter through."""
    _run_the_batch(context)

    document = read_document(document_path(canary))
    assert document[CANONICAL_SEQUENCE_KEY] == len(CANCELLED_KEYS) + len(ACTIVE_KEYS)


def test_one_terminal_transition_is_one_committed_line(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    """The single boundary: the first move creates the ledger and its index."""
    _move(context, CANCELLED_KEYS[0], to_status="CANCELLED")

    index = orjson.loads(_milestone_index(canary).read_bytes())
    assert index["line_count"] == 1
    assert [entry["record_key"] for entry in index["entries"]] == [CANCELLED_KEYS[0]]


def test_a_tree_with_no_terminal_transition_grows_no_index(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    """The empty boundary: nothing finished, so nothing was indexed."""
    for key in ACTIVE_KEYS:
        _move(context, key, to_status="ACTIVE")

    assert not _milestone_ledger(canary).exists()
    assert _index_bytes(canary) == {}


# ---------------------------------------------------------------------------
# What the index says, and that it says it about the right bytes
# ---------------------------------------------------------------------------


def test_every_index_entry_addresses_the_line_it_names(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    """Read each published offset back out of the ledger and digest it."""
    _run_the_batch(context)
    content = _milestone_ledger(canary).read_bytes()

    index = orjson.loads(_milestone_index(canary).read_bytes())

    assert index["line_count"] == len(CANCELLED_KEYS)
    for entry in index["entries"]:
        line = content[entry["offset"] : entry["offset"] + entry["length"]].decode("utf-8")
        assert line_digest(line) == entry["digest"]
        assert entry["superseded"] is False


def test_the_index_agrees_with_a_rebuild_from_the_ledger_alone(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    _run_the_batch(context)

    rebuilt = build_ledger_index(Epoch2Collection.MILESTONE, _milestone_ledger(canary).read_bytes())

    assert render_index(rebuilt) == _milestone_index(canary).read_bytes()


def test_the_line_digests_are_the_ones_the_records_render_to(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    """The index digests the bytes, and the bytes are the records."""
    _run_the_batch(context)

    index = orjson.loads(_milestone_index(canary).read_bytes())
    records = read_ledger_records(_milestone_ledger(canary))

    assert [entry["digest"] for entry in index["entries"]] == [
        line_digest(render_ledger_line(record)) for record in records
    ]


# ---------------------------------------------------------------------------
# The rebuild at daemon start
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason=_BOOT_SKIP_REASON)
def test_the_index_rebuilt_at_daemon_start_is_byte_identical(
    context: Epoch2RootContext,
    canary: CanaryProvision,
    runtime_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_the_batch(context)
    before = _index_bytes(canary)
    assert before

    assert _boot(runtime_root, tmp_path, monkeypatch) == 0

    assert _index_bytes(canary) == before


@pytest.mark.skipif(sys.platform == "win32", reason=_BOOT_SKIP_REASON)
def test_a_deleted_index_is_reproduced_by_the_boot(
    context: Epoch2RootContext,
    canary: CanaryProvision,
    runtime_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleting the derived tier is safe: the boot puts the same bytes back."""
    _run_the_batch(context)
    before = _index_bytes(canary)
    for path in index_dir(document_path(canary)).iterdir():
        path.unlink()

    assert _boot(runtime_root, tmp_path, monkeypatch) == 0

    assert _index_bytes(canary) == before


@pytest.mark.skipif(sys.platform == "win32", reason=_BOOT_SKIP_REASON)
def test_the_boot_leaves_the_document_and_the_ledger_untouched(
    context: Epoch2RootContext,
    canary: CanaryProvision,
    runtime_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the derived tier is rewritten when nothing was half-moved."""
    _run_the_batch(context)
    document = document_path(canary).read_bytes()
    ledger = _milestone_ledger(canary).read_bytes()

    _boot(runtime_root, tmp_path, monkeypatch)

    assert document_path(canary).read_bytes() == document
    assert _milestone_ledger(canary).read_bytes() == ledger
    assert _document_keys(canary) == sorted(ACTIVE_KEYS)


@pytest.mark.skipif(sys.platform == "win32", reason=_BOOT_SKIP_REASON)
def test_the_rebuild_is_stable_across_repeated_boots(
    context: Epoch2RootContext,
    canary: CanaryProvision,
    runtime_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_the_batch(context)
    _boot(runtime_root, tmp_path, monkeypatch)
    after_first: dict[str, Any] = dict(_index_bytes(canary))

    _boot(runtime_root, tmp_path, monkeypatch)

    assert _index_bytes(canary) == after_first
