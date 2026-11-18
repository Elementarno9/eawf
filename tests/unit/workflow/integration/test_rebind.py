"""Which Batch a ledger row is rebound through, what a document still names, and rewrite maps."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.runtime.content import StoredContent
from eawf.kernel.store.ledger import (
    LedgerRecord,
    append_ledger_record,
    line_digest,
    render_ledger_line,
)
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.workflow.integration.rebind import (
    REWRITE_MAP_INVALID,
    RebindDropError,
    RewriteMap,
    RewriteMapError,
    RewritePair,
    append_rewrite_pairs,
    document_mentions,
    keeps_rewritten_hash,
    load_rewrite_map,
    only_commits_rebound,
    pairs_to_record,
    plan_rebind,
    read_rewrite_pairs,
    recorded_hashes,
    rewrite_map_path,
    row_batch,
)
from eawf.workflow.integration.squash import SquashMap

TASKS = {"EAWF-0042": "BAT-0007"}
OLD, NEW = "a" * 40, "b" * 40


def _record(collection: Epoch2Collection, key: str, payload: dict[str, Any]) -> LedgerRecord:
    return LedgerRecord(
        collection=collection,
        record_key=key,
        status="x",
        recorded_at=datetime(2026, 10, 8, tzinfo=UTC),
        payload=payload,
    )


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        (_record(Epoch2Collection.BATCH, "BAT-0007", {"key": "BAT-0007"}), "BAT-0007"),
        (
            _record(
                Epoch2Collection.BATCH, "BMR-1-BAT-0008", {"batch_ref": "eawf://P/batch/BAT-0008"}
            ),
            "BAT-0008",
        ),
        (_record(Epoch2Collection.TASK, "EAWF-0042", {"key": "EAWF-0042"}), "BAT-0007"),
        (
            _record(Epoch2Collection.RECEIPT, "PRF-1", {"task_ref": "eawf://P/task/EAWF-0042"}),
            "BAT-0007",
        ),
        (_record(Epoch2Collection.RECEIPT, "PRF-2", {"task_ref": "eawf://P/task/EAWF-9999"}), None),
        (_record(Epoch2Collection.EVIDENCE, "EV-1", {}), None),
    ],
    ids=["batch-row", "batch-ref", "task-row", "receipt-task", "unplaced-task", "no-ref"],
)
def test_row_batch_reads_the_ref_the_row_carries(
    record: LedgerRecord, expected: str | None
) -> None:
    assert row_batch(record, TASKS) == expected


def test_document_mentions_only_bindings_to_source_commits() -> None:
    squash_map = SquashMap(
        branch="b", base="c" * 40, source_head=OLD, head=NEW, commits={OLD: NEW}, batches={}
    )

    assert document_mentions({}, squash_map) == ()
    assert document_mentions({"batch": {"X": {"head": NEW, "digest": "d" * 64}}}, squash_map) == ()
    assert document_mentions({"batch": {"X": {"binding": {"head_sha": OLD}}}}, squash_map) == (OLD,)
    assert document_mentions({"task": {"T": {"refs": [f"commit:{OLD}"]}}}, squash_map) == (OLD,)


def test_document_mentions_pass_over_a_sha_quoted_in_prose() -> None:
    squash_map = SquashMap(
        branch="b", base="c" * 40, source_head=OLD, head=NEW, commits={OLD: NEW}, batches={}
    )
    brief = {"campaign": {"CAM-1": {"brief": {"text": f"the population: {OLD}, and more"}}}}

    assert document_mentions(brief, squash_map) == ()


# ---- a map entry naming no commit drops the reference ---------------------------

ORPHAN = "c" * 40
REASON = "quotes a commit no published branch carries"


def _orphan_map(**overrides: Any) -> SquashMap:
    fields: dict[str, Any] = {
        "branch": "b",
        "base": "d" * 40,
        "source_head": NEW,
        "head": NEW,
        "commits": {ORPHAN: None},
        "dropped": {ORPHAN: REASON},
        "batches": {},
    }
    return SquashMap.model_validate({**fields, **overrides})


def _plan(tmp_path: Path, collection: Epoch2Collection, payload: dict[str, Any]) -> Any:
    state = tmp_path / "state.json"
    append_ledger_record(ledger_path(state, collection), _record(collection, "K-1", payload))
    return plan_rebind(
        state,
        _orphan_map(),
        {},
        trees={},
        parents={},
        recorded_at=datetime(2026, 10, 9, tzinfo=UTC),
    )


def test_a_dropped_commit_withholds_the_content_line_quoting_it(tmp_path: Path) -> None:
    content = {
        "payload_kind": "content_artifact",
        "artifact_ref": "artifact://content/run-00000001/" + "e" * 64,
        "run_ref": "eawf://EAWF/run/RUN-00000001",
        "lines": ["first", f'    ("P22-I01-W05", "{ORPHAN}"),', "last"],
        "total_lines": 3,
        "withheld_lines": 0,
        "recorded_at": "2026-10-06T23:14:44Z",
    }

    (row,) = _plan(tmp_path, Epoch2Collection.ARTIFACT, content)

    stored = StoredContent.model_validate(row.record.payload)
    assert stored.lines[0] == "first" and stored.lines[2] == "last"
    assert ORPHAN not in stored.lines[1] and REASON in stored.lines[1]
    assert stored.withheld_lines == 1
    assert row.rebinds == {ORPHAN: None}


def test_a_dropped_commit_withholds_receipt_text_and_match_lines(tmp_path: Path) -> None:
    payload = {
        "result": {
            "bounded_output": {
                "text": f"a\nsee {ORPHAN} here\nb",
                "matches": [{"path": "x.py", "line": 3, "text": f"x = '{ORPHAN}'"}],
            }
        }
    }

    (row,) = _plan(tmp_path, Epoch2Collection.RECEIPT, payload)

    output = row.record.payload["result"]["bounded_output"]
    assert output["text"].splitlines()[0::2] == ["a", "b"]
    assert REASON in output["text"].splitlines()[1]
    assert ORPHAN not in str(row.record.payload)
    assert REASON in output["matches"][0]["text"]


def test_a_dropped_commit_cannot_leave_a_binding_without_its_head(tmp_path: Path) -> None:
    with pytest.raises(RebindDropError, match="head_sha"):
        _plan(tmp_path, Epoch2Collection.RECEIPT, {"binding": {"head_sha": ORPHAN}})


def test_the_map_pairs_every_dropped_commit_with_a_reason() -> None:
    with pytest.raises(ValidationError, match="reason"):
        _orphan_map(dropped={})
    with pytest.raises(ValidationError, match="reason"):
        _orphan_map(commits={ORPHAN: NEW})
    assert _orphan_map().dropped == {ORPHAN: REASON}


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        ({}, {}, True),
        ({"head_sha": OLD}, {"head_sha": NEW}, True),
        (
            {"binding": {"head_sha": OLD, "tree_sha": OLD}},
            {"binding": {"head_sha": NEW, "tree_sha": NEW}},
            True,
        ),
        ({"reason": f"read at {OLD}"}, {"reason": f"read at {NEW}"}, True),
        ({"head_sha": OLD}, {"head_sha": NEW, "extra": 1}, False),
        ({"head_sha": OLD, "note": "x"}, {"head_sha": NEW, "note": "y"}, False),
        # one hex digit short of a commit SHA is content, not a commit
        ({"head_sha": OLD[:-1]}, {"head_sha": NEW[:-1]}, False),
        ({"digest": "sha256:" + "a" * 64}, {"digest": "sha256:" + "b" * 64}, False),
    ],
)
def test_only_commits_rebound_tells_a_rebind_from_any_other_change(
    before: dict[str, Any], after: dict[str, Any], expected: bool
) -> None:
    assert only_commits_rebound(before, after) is expected


# ---- an exact binding takes its new head's tree --------------------------------

NEW_TREE = "e" * 40
DIGESTS = {"contract_digest": "sha256:" + "c" * 64, "evidence_digest": "sha256:" + "d" * 64}


def _rebound_binding(tmp_path: Path, binding: dict[str, Any]) -> dict[str, Any]:
    """Rebind a Task row carrying *binding* from ``OLD`` to ``NEW`` and return the binding."""
    state = tmp_path / "state.json"
    record = _record(Epoch2Collection.TASK, "EAWF-0042", {"integrated_binding": binding})
    append_ledger_record(ledger_path(state, Epoch2Collection.TASK), record)
    squash_map = SquashMap(
        branch="b", base="c" * 40, source_head=OLD, head=NEW, commits={OLD: NEW}, batches={}
    )
    (row,) = plan_rebind(
        state,
        squash_map,
        TASKS,
        trees={NEW: NEW_TREE},
        parents={},
        recorded_at=datetime(2026, 10, 9, tzinfo=UTC),
    )
    rebound: dict[str, Any] = row.record.payload["integrated_binding"]
    return rebound


@pytest.mark.parametrize("digests", [{}, DIGESTS], ids=["new-row", "stored-row"])
def test_a_task_binding_takes_the_tree_of_its_new_head(
    tmp_path: Path, digests: dict[str, str]
) -> None:
    binding = {"head_sha": OLD, "tree_sha": "f" * 40, "policy_revision": 1, **digests}

    assert _rebound_binding(tmp_path, binding) == {**binding, "head_sha": NEW, "tree_sha": NEW_TREE}


def test_a_head_and_tree_pair_that_is_no_exact_binding_keeps_its_tree(tmp_path: Path) -> None:
    binding = {"head_sha": OLD, "tree_sha": "f" * 40}

    assert _rebound_binding(tmp_path, binding) == {"head_sha": NEW, "tree_sha": "f" * 40}


# ---- a history-rewrite map in git filter-repo format ----------------------------

TWIN, KEPT = "c" * 40, "d" * 40
HEADER = "old                                      new\n"


def _map_file(tmp_path: Path, body: str, header: str = HEADER) -> Path:
    path = tmp_path / "commit-map"
    path.write_text(header + body, encoding="utf-8")
    return path


def test_rewrite_map_loads_many_to_one_and_identity_pairs(tmp_path: Path) -> None:
    path = _map_file(tmp_path, f"{OLD} {NEW}\n{TWIN}\t{NEW}\n{KEPT} {KEPT}\n")

    assert load_rewrite_map(path) == RewriteMap(commits={OLD: NEW, TWIN: NEW, KEPT: KEPT})


@pytest.mark.parametrize(
    ("body", "expected"),
    [("", {}), (f"{OLD} {NEW}\n", {OLD: NEW}), (f"{OLD} {NEW}", {OLD: NEW})],
    ids=["header-only", "single", "no-trailing-newline"],
)
def test_rewrite_map_boundaries(tmp_path: Path, body: str, expected: dict[str, str]) -> None:
    assert load_rewrite_map(_map_file(tmp_path, body)).commits == expected


@pytest.mark.parametrize(
    ("header", "body", "line", "reason"),
    [
        ("", "", 1, "header"),
        ("new old\n", "", 1, "header"),
        (HEADER, f"{OLD}\n", 2, "two full"),
        (HEADER, f"{OLD} {NEW} {TWIN}\n", 2, "two full"),
        (HEADER, f"{OLD} {NEW[:-1]}\n", 2, "two full"),
        (HEADER, f"{OLD.upper()} {NEW}\n", 2, "two full"),
        (HEADER, f"{OLD} {NEW}\n\n{TWIN} {NEW}\n", 3, "two full"),
        (HEADER, f"{OLD} {NEW}\n{TWIN} {NEW}\n{OLD} {KEPT}\n", 4, "repeat"),
        (HEADER, f"{OLD} {'0' * 40}\n", 2, "zero"),
    ],
    ids=[
        "empty-file",
        "wrong-header",
        "one-hash",
        "three-hashes",
        "short-hash",
        "uppercase",
        "blank-line",
        "repeated-old",
        "zero-new",
    ],
)
def test_rewrite_map_refuses_naming_the_line(
    tmp_path: Path, header: str, body: str, line: int, reason: str
) -> None:
    with pytest.raises(RewriteMapError, match=f"{REWRITE_MAP_INVALID}: line {line}: .*{reason}"):
        load_rewrite_map(_map_file(tmp_path, body, header))


def test_rewrite_map_model_is_closed_and_refuses_a_zero_hash() -> None:
    with pytest.raises(ValidationError, match="extra"):
        RewriteMap.model_validate({"commits": {}, "head": NEW})
    with pytest.raises(ValidationError, match="zero"):
        RewriteMap(commits={OLD: "0" * 40})
    with pytest.raises(ValidationError):
        RewriteMap(commits={OLD: NEW[:-1]})
    with pytest.raises(ValidationError):
        RewriteMap.model_validate({"commits": "x"})


def test_rewrite_map_plans_commit_by_commit_ignoring_batches(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    batch = _record(Epoch2Collection.BATCH, "BAT-0007", {"key": "BAT-0007", "head_sha": OLD})
    append_ledger_record(ledger_path(state, Epoch2Collection.BATCH), batch)
    evidence = _record(Epoch2Collection.EVIDENCE, "EV-1", {"summary": f"saw {TWIN} and {KEPT}"})
    append_ledger_record(ledger_path(state, Epoch2Collection.EVIDENCE), evidence)

    rows = plan_rebind(
        state,
        RewriteMap(commits={OLD: NEW, TWIN: NEW, KEPT: KEPT}),
        TASKS,
        trees={NEW: NEW_TREE},
        parents={},
        recorded_at=datetime(2026, 11, 17, tzinfo=UTC),
    )

    assert [row.record.payload for row in rows] == [
        {"key": "BAT-0007", "head_sha": NEW},
        {"summary": f"saw {NEW} and {KEPT}"},
    ]
    assert [row.batch_key for row in rows] == [None, None]
    assert rows[1].rebinds == {TWIN: NEW}


def test_recorded_hashes_reads_effective_lines_of_every_ledger(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    assert recorded_hashes(state) == frozenset()
    first = _record(Epoch2Collection.RECEIPT, "PRF-1", {"head_sha": OLD, "digest": "e" * 64})
    path = ledger_path(state, Epoch2Collection.RECEIPT)
    append_ledger_record(path, first)
    assert recorded_hashes(state) == frozenset({OLD})
    append_ledger_record(
        path,
        first.model_copy(
            update={
                "payload": {"head_sha": NEW},
                "supersedes": line_digest(render_ledger_line(first)),
            }
        ),
    )

    assert recorded_hashes(state) == frozenset({NEW})


AT_PAIR = datetime(2026, 11, 17, 9, 30, tzinfo=UTC)


def test_rewrite_pairs_append_once_and_read_back(tmp_path: Path) -> None:
    path = rewrite_map_path(tmp_path / ".ea")
    rewrite_map = RewriteMap(commits={OLD: NEW, TWIN: NEW, KEPT: KEPT})
    assert read_rewrite_pairs(path) == ()

    first = pairs_to_record(rewrite_map, read_rewrite_pairs(path), at=AT_PAIR)
    append_rewrite_pairs(path, first)
    again = pairs_to_record(rewrite_map, read_rewrite_pairs(path), at=AT_PAIR)

    assert path == tmp_path / ".ea" / "store" / "rewrite_map.jsonl"
    assert [(pair.old, pair.new) for pair in first] == [(OLD, NEW), (TWIN, NEW)]
    assert again == ()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [sorted(json.loads(line)) for line in lines] == [["applied_at", "new", "old"]] * 2
    assert read_rewrite_pairs(path) == first


def test_a_rewrite_pair_line_is_closed(tmp_path: Path) -> None:
    path = rewrite_map_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"old": OLD, "new": NEW, "applied_at": "x", "extra": 1}) + "\n")

    with pytest.raises(RewriteMapError, match="rewrite_map_invalid: line 1") as caught:
        read_rewrite_pairs(path)
    assert isinstance(caught.value.__cause__, ValidationError)


@pytest.mark.parametrize(
    ("collection", "payload", "kept"),
    [
        (Epoch2Collection.ARTIFACT, {"payload_kind": "content_artifact"}, True),
        (Epoch2Collection.RECEIPT, {"payload_kind": "host_call_receipt"}, True),
        (Epoch2Collection.RECEIPT, {"payload_kind": "semantic_receipt"}, True),
        (Epoch2Collection.ARTIFACT, {"payload_kind": "host_call_receipt"}, False),
        (Epoch2Collection.ARTIFACT, {"head_sha": OLD}, False),
        (Epoch2Collection.BATCH, {}, False),
    ],
    ids=["capture", "host-call", "semantic", "kind-of-another-ledger", "no-kind", "empty"],
)
def test_keeps_rewritten_hash_only_for_rows_the_commit_policy_keeps_local(
    collection: Epoch2Collection, payload: dict[str, Any], kept: bool
) -> None:
    assert keeps_rewritten_hash(_record(collection, "K-1", payload)) is kept


def test_rewrite_map_leaves_a_capture_and_rebinds_the_row_beside_it(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    path = ledger_path(state, Epoch2Collection.ARTIFACT)
    capture = {"payload_kind": "content_artifact", "lines": [f"commit {OLD}"]}
    append_ledger_record(path, _record(Epoch2Collection.ARTIFACT, "ART-1", capture))
    append_ledger_record(path, _record(Epoch2Collection.ARTIFACT, "ART-2", {"head_sha": OLD}))

    rows = plan_rebind(
        state,
        RewriteMap(commits={OLD: NEW}),
        TASKS,
        trees={NEW: NEW_TREE},
        parents={},
        recorded_at=datetime(2026, 11, 17, tzinfo=UTC),
    )

    assert [(row.record.record_key, row.record.payload) for row in rows] == [
        ("ART-2", {"head_sha": NEW})
    ]


# ---- the record of applied pairs is replaced whole ------------------------------

MIDDLE, LAST = "e" * 40, "f" * 40


@pytest.mark.parametrize("failing", ["fsync", "replace"])
def test_rewrite_pairs_append_interrupted_keeps_the_earlier_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    path = rewrite_map_path(tmp_path / ".ea")
    earlier = (RewritePair(old=OLD, new=NEW, applied_at=AT_PAIR),)
    append_rewrite_pairs(path, earlier)
    before = path.read_bytes()

    def broken(*_args: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(f"os.{failing}", broken)
    with pytest.raises(OSError, match="No space"):
        append_rewrite_pairs(path, [RewritePair(old=TWIN, new=KEPT, applied_at=AT_PAIR)])
    monkeypatch.undo()

    assert path.read_bytes() == before
    assert read_rewrite_pairs(path) == earlier
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]


def test_rewrite_pairs_append_not_interrupted_keeps_earlier_pairs_first(tmp_path: Path) -> None:
    path = rewrite_map_path(tmp_path / ".ea")
    first = RewritePair(old=OLD, new=NEW, applied_at=AT_PAIR)
    second = RewritePair(old=TWIN, new=KEPT, applied_at=AT_PAIR)
    third = RewritePair(old=NEW, new=MIDDLE, applied_at=AT_PAIR)

    append_rewrite_pairs(path, [first])
    append_rewrite_pairs(path, [second, third])

    assert read_rewrite_pairs(path) == (first, second, third)
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]


def test_rewrite_pairs_append_interrupted_never_when_no_pair_touches_no_file(
    tmp_path: Path,
) -> None:
    path = rewrite_map_path(tmp_path / ".ea")
    append_rewrite_pairs(path, [])
    assert not (tmp_path / ".ea").exists()
    append_rewrite_pairs(path, [RewritePair(old=OLD, new=NEW, applied_at=AT_PAIR)])
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    append_rewrite_pairs(path, ())
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


# ---- a map joined from two rewrites is followed to its end ----------------------


def _plan_map(state: Path, commits: dict[str, str]) -> tuple[Any, ...]:
    return plan_rebind(
        state,
        RewriteMap(commits=commits),
        {},
        trees={NEW: NEW_TREE, MIDDLE: NEW_TREE, LAST: NEW_TREE},
        parents={},
        recorded_at=datetime(2026, 11, 18, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    "commits",
    [
        {OLD: MIDDLE, MIDDLE: LAST},
        {MIDDLE: LAST, OLD: MIDDLE},
        {OLD: MIDDLE, MIDDLE: LAST, LAST: LAST},
    ],
    ids=["in-order", "reversed", "end-names-itself"],
)
def test_rewrite_map_chain_lands_on_its_last_commit_in_one_run(
    tmp_path: Path, commits: dict[str, str]
) -> None:
    state = tmp_path / "state.json"
    path = ledger_path(state, Epoch2Collection.EVIDENCE)
    append_ledger_record(path, _record(Epoch2Collection.EVIDENCE, "EV-1", {"head_sha": OLD}))
    append_ledger_record(path, _record(Epoch2Collection.EVIDENCE, "EV-2", {"head_sha": MIDDLE}))

    rows = _plan_map(state, commits)

    assert [row.record.payload for row in rows] == [{"head_sha": LAST}, {"head_sha": LAST}]
    assert [row.rebinds for row in rows] == [{OLD: LAST}, {MIDDLE: LAST}]
    for row in rows:
        append_ledger_record(path, row.record)
    assert _plan_map(state, commits) == ()


def test_rewrite_map_without_a_chain_plans_one_step(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    path = ledger_path(state, Epoch2Collection.EVIDENCE)
    append_ledger_record(path, _record(Epoch2Collection.EVIDENCE, "EV-1", {"a": OLD, "b": TWIN}))
    append_ledger_record(path, _record(Epoch2Collection.EVIDENCE, "EV-2", {"head_sha": KEPT}))

    (row,) = _plan_map(state, {OLD: NEW, TWIN: NEW, KEPT: KEPT})

    assert row.record.payload == {"a": NEW, "b": NEW}
    assert row.rebinds == {OLD: NEW, TWIN: NEW}


@pytest.mark.parametrize(
    "commits",
    [{OLD: MIDDLE, MIDDLE: OLD}, {OLD: MIDDLE, MIDDLE: LAST, LAST: MIDDLE}],
    ids=["two-cycle", "cycle-after-a-step"],
)
def test_rewrite_map_chain_into_a_cycle_is_refused(tmp_path: Path, commits: dict[str, str]) -> None:
    state = tmp_path / "state.json"
    path = ledger_path(state, Epoch2Collection.EVIDENCE)
    append_ledger_record(path, _record(Epoch2Collection.EVIDENCE, "EV-1", {"head_sha": OLD}))

    with pytest.raises(RewriteMapError, match=f"{REWRITE_MAP_INVALID}: line \\d+: .*into itself"):
        _plan_map(state, commits)


# ---- a commit map that is not UTF-8 ---------------------------------------------


@pytest.mark.parametrize(
    ("raw", "line"),
    [
        (b"\xff\xfe", 1),
        (HEADER.encode() + b"\xc3\x28 " + NEW.encode() + b"\n", 2),
        (HEADER.encode() + f"{OLD} {NEW}\n".encode() + b"\x80\n", 3),
    ],
    ids=["header", "first-pair", "third-line"],
)
def test_rewrite_map_undecodable_is_refused_naming_the_line(
    tmp_path: Path, raw: bytes, line: int
) -> None:
    path = tmp_path / "commit-map"
    path.write_bytes(raw)

    with pytest.raises(
        RewriteMapError, match=f"{REWRITE_MAP_INVALID}: line {line}: .*UTF-8"
    ) as caught:
        load_rewrite_map(path)
    assert caught.value.line == line
    assert isinstance(caught.value.__cause__, UnicodeDecodeError)
