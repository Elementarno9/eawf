"""A locked Run read costs the run ledger lines appended since the last read, not the ledger.

Every Run verb reads the run ledger under the document lock. These tests fill
a tree's run ledger with 30,000 lines, mostly other Runs', and check that a
warm read validates only what was appended since, that the per-Run scans
validate only the asked Run's lines, and that every answer is the one a full
read of the same bytes gives.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.runtime.control import ControlDisposition, ControlFact, ControlPhase
from eawf.kernel.runtime.events import RunEventRecord
from eawf.kernel.runtime.provider import ControlKind
from eawf.kernel.store.compaction import (
    RecordInTwoPlacesError,
    RecordLocation,
    document_rows,
    locate_record,
    read_document,
)
from eawf.kernel.store.ledger import (
    LedgerRecord,
    LedgerTornTailError,
    effective_records,
    line_digest,
    read_ledger_records,
    render_ledger_line,
    truncate_torn_tail,
)
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.methods import host_subagent
from eawf.runtime.daemon.methods import run as run_methods
from eawf.runtime.daemon.methods.run import RunEventAnswer, append_run_event
from eawf.runtime.daemon.native_dispatch import stored_run
from eawf.runtime.daemon.run_events import RunEventAppend, names_another_run
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    document_path,
    method_context,
    provision,
    rekeyed,
    seed,
    seed_row,
    tree_root,
)

pytestmark = pytest.mark.integration

#: The Run every read asks for, RUNNING in the document.
RUN_KEY: Final = "RUN-00000010"
RUN_URN: Final = f"eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/{RUN_KEY}"

#: How many lines the synthetic run ledger holds, about the live one's 29,687.
LEDGER_LINES: Final = 30_000

#: How many other Runs the synthetic lines belong to.
OTHER_RUNS: Final = 1_000

#: The daemon's recording clock for every append.
NOW: Final = datetime(2026, 11, 8, 1, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Tree:
    """One provisioned tree whose run ledger holds the synthetic lines."""

    canary: CanaryProvision
    context: Epoch2RootContext

    @property
    def ledger(self) -> Path:
        """The tree's run ledger."""
        return ledger_path(document_path(self.canary), Epoch2Collection.RUN)


def other_key(number: int) -> str:
    """Return the key of the *number*-th other Run, all above :data:`RUN_KEY`."""
    return f"RUN-{100_000 + number:08d}"


def event_args(event: int, sequence: int) -> RunEventAppend:
    """Return one reasoning event of the asked Run."""
    return RunEventAppend.model_validate(
        {
            "urn": RUN_URN,
            "event_ref": f"EVT-{event:08x}",
            "run_sequence": sequence,
            "event_kind": "reasoning_started",
            "payload": {"payload_kind": "reasoning_summary", "phase": "started"},
            "actor": "OP-0001",
        }
    )


def ledger_line(key: str, payload: dict[str, Any], *, status: str) -> str:
    """Return the text of one run ledger line keyed *key*."""
    record = LedgerRecord(
        collection=Epoch2Collection.RUN,
        record_key=key,
        status=status,
        recorded_at=AT,
        payload=payload,
    )
    return render_ledger_line(record)


def control_line(run_urn: str) -> str:
    """Return a requested cancel of the Run at *run_urn*."""
    fact = ControlFact(
        control_request_ref="CTL-0000000a",
        run_ref=parse_qualified_urn(run_urn),
        control=ControlKind.CANCEL,
        phase=ControlPhase.REQUESTED,
        disposition=ControlDisposition.REQUESTING,
        actor="OP-0001",
        recorded_at=AT,
        sequence=1,
    )
    return ledger_line(fact.control_request_ref, fact.model_dump(mode="json"), status="requested")


def run_record_line(key: str) -> str:
    """Return the compacted record of a COMPLETED Run keyed *key*."""
    return ledger_line(key, rekeyed(seed_row("run", "COMPLETED"), key=key), status="COMPLETED")


def append_lines(path: Path, lines: list[str]) -> None:
    """Append *lines* to the ledger at *path* as a writer would."""
    with path.open("ab") as handle:
        handle.write("".join(f"{line}\n" for line in lines).encode("utf-8"))


def build_tree(root: Path) -> Tree:
    """Provision a tree with the asked Run RUNNING and 30,000 run ledger lines.

    The asked Run holds one event and one requested cancel; every other line
    is another Run's event, control fact or compacted record.
    """
    canary = provision(root / "repo")
    seed(canary, {"run": {RUN_KEY: seed_row("run", "RUNNING")}})
    tree = Tree(canary, method_context(root / "runtime").native_root_context(tree_root(canary)))
    append_run_event(tree.context, event_args(1, 1), now=NOW)
    held = tree.ledger.read_text(encoding="utf-8").splitlines()
    (event,) = [line for line in held if '"payload_kind":"run_event"' in line]
    control, record = control_line(RUN_URN), run_record_line(RUN_KEY)
    lines = [control]
    for number in range(OTHER_RUNS):
        lines.append(control.replace(RUN_KEY, other_key(number)))
        lines.append(record.replace(RUN_KEY, other_key(number)))
    remaining = LEDGER_LINES - len(held) - len(lines)
    lines.extend(event.replace(RUN_KEY, other_key(n % OTHER_RUNS)) for n in range(remaining))
    append_lines(tree.ledger, lines)
    return tree


@pytest.fixture
def tree(tmp_path: Path) -> Tree:
    """A tree whose run ledger holds the synthetic lines."""
    return build_tree(tmp_path / "a")


@pytest.fixture
def ledger_validations(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Collect every ledger line validated as a LedgerRecord while the test runs."""
    seen: list[str] = []
    original: Callable[..., LedgerRecord] = LedgerRecord.model_validate_json

    def counting(data: str | bytes, *args: object, **kwargs: object) -> LedgerRecord:
        seen.append(data if isinstance(data, str) else data.decode("utf-8"))
        return original(data, *args, **kwargs)

    monkeypatch.setattr(LedgerRecord, "model_validate_json", counting)
    yield seen


@pytest.fixture
def run_refs_validated(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Collect the run_ref of every payload validated as a run event or a control fact."""
    seen: list[str] = []
    for model in (RunEventRecord, ControlFact):
        original = model.model_validate

        def counting(data: Any, *args: Any, _original: Any = original, **kwargs: Any) -> Any:
            if isinstance(data, dict):
                seen.append(str(data.get("run_ref")))
            return _original(data, *args, **kwargs)

        monkeypatch.setattr(model, "model_validate", counting)
    yield seen


@contextmanager
def full_reads(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Answer every Run verb by a full read validating every Run's lines, as before the index."""
    with monkeypatch.context() as patch:
        patch.setattr(run_methods, "indexed_ledger_records", read_ledger_records)
        patch.setattr(run_methods, "names_another_run", lambda payload, run_keys: False)
        patch.setattr(host_subagent, "indexed_ledger_records", read_ledger_records)
        yield


def full_read_run(tree: Tree) -> Any:
    """Return the asked Run as a full read of the run ledger finds it."""
    with tree.context.session([parse_qualified_urn(RUN_URN)]) as session:
        return stored_run(session, read_ledger_records(tree.ledger), parse_qualified_urn(RUN_URN))


def answer_of(answer: RunEventAnswer) -> tuple[Any, ...]:
    """Return the parts of an append's answer a caller acts on."""
    return (
        answer.disposition,
        answer.run_status,
        answer.next_sequence,
        answer.last_contiguous_sequence,
        answer.event["run_sequence"],
    )


def test_a_warm_locked_run_read_parses_only_appended_lines(
    tree: Tree, ledger_validations: list[str]
) -> None:
    urn = parse_qualified_urn(RUN_URN)
    assert len(read_ledger_records(tree.ledger)) == LEDGER_LINES
    host_subagent._read_run(tree.context, urn)
    appended = [run_record_line(other_key(OTHER_RUNS + n)) for n in range(5)]
    append_lines(tree.ledger, appended)
    ledger_validations.clear()

    started = time.perf_counter()
    run = host_subagent._read_run(tree.context, urn)
    warm_s = time.perf_counter() - started

    assert ledger_validations == appended

    # Both reads run on the same host in the same moment, so load moves them together.
    started = time.perf_counter()
    full = full_read_run(tree)
    full_s = time.perf_counter() - started

    assert 3 * warm_s < full_s
    assert run == full


def test_an_event_append_validates_only_its_runs_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_refs_validated: list[str]
) -> None:
    indexed, full = build_tree(tmp_path / "a"), build_tree(tmp_path / "b")
    append_run_event(indexed.context, event_args(2, 2), now=NOW)
    with full_reads(monkeypatch):
        expected = answer_of(append_run_event(full.context, event_args(2, 2), now=NOW))
    run_refs_validated.clear()

    answer = answer_of(append_run_event(indexed.context, event_args(3, 3), now=NOW))

    assert run_refs_validated
    assert set(run_refs_validated) == {RUN_URN}
    with full_reads(monkeypatch):
        assert answer == answer_of(append_run_event(full.context, event_args(3, 3), now=NOW))
    assert expected[2] == 3

    # Dropping the asked Run's events through a rename sends the next one back to the start.
    for side in (indexed, full):
        kept = [
            line
            for line in side.ledger.read_text(encoding="utf-8").splitlines()
            if not ('"payload_kind":"run_event"' in line and RUN_KEY in line)
        ]
        replacement = side.ledger.with_name("run.jsonl.replacement")
        replacement.write_text("".join(f"{line}\n" for line in kept), encoding="utf-8")
        os.replace(replacement, side.ledger)
    replaced = answer_of(append_run_event(indexed.context, event_args(4, 1), now=NOW))
    with full_reads(monkeypatch):
        assert replaced == answer_of(append_run_event(full.context, event_args(4, 1), now=NOW))
    assert replaced[2] == 2


def test_run_key_and_record_location_see_appended_lines(
    tree: Tree, ledger_validations: list[str]
) -> None:
    state = document_path(tree.canary)
    document = read_document(state)
    assert host_subagent._next_run_key(document, tree.ledger) == other_key(OTHER_RUNS)
    assert locate_record(state, collection=Epoch2Collection.RUN, record_key=RUN_KEY) is (
        RecordLocation.DOCUMENT
    )

    newest = run_record_line(other_key(OTHER_RUNS + 7))
    # A later line of another key supersedes the record of the first other Run.
    first = run_record_line(other_key(0))
    retraction = json.loads(run_record_line(other_key(OTHER_RUNS + 8)))
    retraction["supersedes"] = line_digest(first)
    # A correction of the same key supersedes the second other Run's record and stands.
    correction = json.loads(run_record_line(other_key(1)))
    correction["supersedes"] = line_digest(run_record_line(other_key(1)))
    correction["status"] = "FAILED"
    appended = [
        newest,
        *(json.dumps(line, separators=(",", ":")) for line in (retraction, correction)),
    ]
    append_lines(tree.ledger, appended)
    ledger_validations.clear()

    assert host_subagent._next_run_key(document, tree.ledger) == other_key(OTHER_RUNS + 9)
    assert locate_record(state, collection=Epoch2Collection.RUN, record_key=other_key(0)) is (
        RecordLocation.ABSENT
    )
    assert ledger_validations == appended
    standing = {item.record_key for item in effective_records(read_ledger_records(tree.ledger))}
    doc_keys = document_rows(document, Epoch2Collection.RUN).keys()
    for key in (
        other_key(OTHER_RUNS + 7),
        other_key(0),
        other_key(1),
        other_key(2),
        "RUN-09999999",
    ):
        expected = (
            RecordLocation.LEDGER
            if key in standing
            else RecordLocation.DOCUMENT
            if key in doc_keys
            else RecordLocation.ABSENT
        )
        assert locate_record(state, collection=Epoch2Collection.RUN, record_key=key) is expected

    append_lines(tree.ledger, [run_record_line(RUN_KEY)])
    with pytest.raises(RecordInTwoPlacesError):
        locate_record(state, collection=Epoch2Collection.RUN, record_key=RUN_KEY)


def test_a_half_written_last_line_refuses_the_locked_read_until_repaired(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    urn = parse_qualified_urn(RUN_URN)
    host_subagent._read_run(tree.context, urn)
    with tree.ledger.open("ab") as handle:
        handle.write(run_record_line(other_key(OTHER_RUNS))[:40].encode("utf-8"))

    with pytest.raises(LedgerTornTailError):
        host_subagent._read_run(tree.context, urn)
    with full_reads(monkeypatch), pytest.raises(LedgerTornTailError):
        host_subagent._read_run(tree.context, urn)
    truncate_torn_tail(tree.ledger)
    assert host_subagent._read_run(tree.context, urn) == full_read_run(tree)


@pytest.mark.parametrize(
    ("payload", "run_keys", "expected"),
    [
        ({"run_ref": RUN_URN}, {RUN_KEY}, False),
        ({"run_ref": RUN_URN}, {other_key(0)}, True),
        ({"run_ref": RUN_URN}, set(), True),
        ({"run_ref": RUN_URN}, {other_key(0), RUN_KEY}, False),
        ({"run_ref": RUN_KEY}, {RUN_KEY}, False),
        ({"run_ref": ""}, {RUN_KEY}, True),
        ({"run_ref": f"{RUN_URN}0"}, {RUN_KEY}, True),
        ({}, {RUN_KEY}, False),
        ({"run_ref": None}, {RUN_KEY}, False),
        ({"run_ref": 10}, {RUN_KEY}, False),
        ({"run_ref": ["RUN-00000010"]}, set(), False),
    ],
    ids=[
        "own-key",
        "other-key",
        "no-keys",
        "one-of-several",
        "bare-key",
        "empty-ref",
        "off-by-one-digit",
        "missing-ref",
        "null-ref",
        "int-ref",
        "list-ref",
    ],
)
def test_names_another_run(payload: dict[str, Any], run_keys: set[str], expected: bool) -> None:
    assert names_another_run(payload, run_keys) is expected


def corrupt_event_line(key: str) -> str:
    """Return an event line of the Run keyed *key* that carries nothing but its reference."""
    line = json.loads(run_record_line(key))
    line["payload"] = {"payload_kind": "run_event", "run_ref": f"{RUN_URN.rsplit('/', 1)[0]}/{key}"}
    return json.dumps(line, separators=(",", ":"))


def test_a_corrupt_event_line_refuses_only_its_own_runs_append(tree: Tree) -> None:
    append_lines(tree.ledger, [corrupt_event_line(other_key(0))])
    assert append_run_event(tree.context, event_args(5, 2), now=NOW).next_sequence == 3

    append_lines(tree.ledger, [corrupt_event_line(RUN_KEY)])
    with pytest.raises(ValidationError):
        append_run_event(tree.context, event_args(6, 3), now=NOW)
