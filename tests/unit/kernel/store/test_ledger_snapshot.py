"""A ledger sealed into a hashed snapshot reads exactly as it did before.

The seal moves a leading run of lines into ``ledger/snapshots/<stem>.<sha256>.jsonl``
and keeps the rest live. These tests pin what that must preserve -- every record,
in order, and the latest row of every key, compared by canonical-JSON digest on
fixture ledgers and on this repository's own Task ledger -- and what it must refuse:
a snapshot whose bytes no longer hash to its name, a second snapshot, a cut that
is not a line boundary.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.store.commit_policy import CommitPolicy, UndeclaredPathError, classify_path
from eawf.kernel.store.compaction import (
    LEGACY_ORIGIN_KIND,
    LegacySealPlan,
    plan_legacy_seal,
    recover_store_tree,
    seal_legacy_import,
)
from eawf.kernel.store.ledger import (
    SNAPSHOT_DIRNAME,
    LedgerRecord,
    LedgerSnapshotError,
    LedgerTornTailError,
    append_correction,
    append_ledger_record,
    compose_ledger_content,
    finish_interrupted_seal,
    latest_rows,
    ledger_snapshot,
    line_digest,
    read_ledger_content,
    read_ledger_records,
    render_ledger_line,
    seal_ledger_prefix,
    snapshot_digest_mismatch,
    snapshot_name,
    split_ledger_lines,
)
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection, StorageTier
from eawf.platform.lint.ledger_history import (
    sha_mentions,
    snapshot_findings,
    snapshot_owner,
)

AT: Final = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
REPO_ROOT: Final = Path(__file__).resolve().parents[4]
LEGACY_MARK: Final = b'"kind":"legacy"'


# --- helpers -----------------------------------------------------------------


def _record(key: str, *, origin: str = "native", keyed: bool = True, **extra: Any) -> LedgerRecord:
    payload: dict[str, Any] = {"origin": {"kind": origin}, **extra}
    if keyed:
        payload["key"] = key
    return LedgerRecord(
        collection=Epoch2Collection.TASK,
        record_key=key,
        status="COMPLETED",
        recorded_at=AT,
        payload=payload,
    )


def _write(path: Path, records: list[LedgerRecord]) -> bytes:
    content = "".join(f"{render_ledger_line(r)}\n" for r in records).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return content


def _canonical(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def _digests(path: Path) -> tuple[str, str]:
    """The canonical-JSON digests of the ledger's records and of its latest rows."""
    records = [r.model_dump(mode="json") for r in read_ledger_records(path)]
    return _canonical(records), _canonical(dict(latest_rows(path)))


def _mixed_ledger(path: Path) -> bytes:
    """Legacy rows, then native rows, then a legacy-payload correction, then a native row."""
    legacy = [_record(f"EAWF-{n:04d}", origin="legacy", keyed=False) for n in range(1, 4)]
    native = [_record("EAWF-0010"), _record("EAWF-0011")]
    _write(path, [*legacy, *native])
    fix = _record("EAWF-0001", origin="legacy", keyed=False, note="rebound")
    fix = fix.model_copy(update={"supersedes": line_digest(render_ledger_line(legacy[0]))})
    append_correction(path, fix)
    append_ledger_record(path, _record("EAWF-0012"))
    return path.read_bytes()


# --- naming and digest check ----------------------------------------------------


def test_snapshot_name_carries_the_sha256_of_its_bytes() -> None:
    content = b"line\n"
    assert snapshot_name("task", content) == f"task.{hashlib.sha256(content).hexdigest()}.jsonl"
    assert snapshot_digest_mismatch(snapshot_name("task", content), content) is None


@pytest.mark.parametrize("content", [b"", b"x\n", b"a\n" * 1000])
def test_snapshot_digest_holds_for_empty_single_and_large_content(content: bytes) -> None:
    assert snapshot_digest_mismatch(snapshot_name("task", content), content) is None


def test_snapshot_digest_mismatch_names_the_actual_digest() -> None:
    name = snapshot_name("task", b"a\n")
    reason = snapshot_digest_mismatch(name, b"b\n")
    assert reason is not None
    assert hashlib.sha256(b"b\n").hexdigest() in reason


@pytest.mark.parametrize(
    "name",
    ["task.jsonl", "task.abc.jsonl", f".{'0' * 64}.jsonl", f"task.{'0' * 63}.jsonl"],
)
def test_snapshot_digest_mismatch_refuses_a_malformed_name(name: str) -> None:
    reason = snapshot_digest_mismatch(name, b"")
    assert reason is not None
    assert "not named" in reason


def test_compose_reads_live_alone_without_a_snapshot() -> None:
    assert compose_ledger_content(b"", b"a\n") == b"a\n"


def test_compose_puts_the_snapshot_first() -> None:
    assert compose_ledger_content(b"a\n", b"b\n") == b"a\nb\n"


def test_compose_reads_an_interrupted_seal_as_the_unsealed_ledger() -> None:
    assert compose_ledger_content(b"a\n", b"a\nb\n") == b"a\nb\n"


# --- seal: equality, refusal, append ---------------------------------------------


def test_seal_preserves_records_and_latest_rows(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger" / "task.jsonl"
    content = _mixed_ledger(ledger)
    before = _digests(ledger)
    plan = plan_legacy_seal(ledger)
    assert plan is not None

    snapshot = seal_ledger_prefix(ledger, plan.cut)

    assert snapshot.parent.name == SNAPSHOT_DIRNAME
    assert snapshot.read_bytes() == content[: plan.cut]
    assert snapshot.name == snapshot_name("task", content[: plan.cut])
    assert read_ledger_content(ledger) == content
    assert _digests(ledger) == before
    assert LEGACY_MARK not in ledger.read_bytes()


def test_seal_of_the_whole_ledger_leaves_an_empty_live_file(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger" / "task.jsonl"
    content = _write(ledger, [_record("EAWF-0001", origin="legacy")])
    before = _digests(ledger)

    seal_ledger_prefix(ledger, len(content))

    assert ledger.read_bytes() == b""
    assert _digests(ledger) == before


def test_append_after_seal_leaves_the_snapshot_byte_identical(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger" / "task.jsonl"
    _mixed_ledger(ledger)
    plan = plan_legacy_seal(ledger)
    assert plan is not None
    snapshot = seal_ledger_prefix(ledger, plan.cut)
    sealed = snapshot.read_bytes()

    append_ledger_record(ledger, _record("EAWF-0099"))

    assert snapshot.read_bytes() == sealed
    assert latest_rows(ledger)["EAWF-0099"]["key"] == "EAWF-0099"
    assert read_ledger_records(ledger)[-1].record_key == "EAWF-0099"


def test_a_live_correction_supersedes_a_sealed_line(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger" / "task.jsonl"
    first = _record("EAWF-0001", status_note="old")
    content = _write(ledger, [first])
    seal_ledger_prefix(ledger, len(content))

    fix = _record("EAWF-0001", status_note="new").model_copy(
        update={"supersedes": line_digest(render_ledger_line(first))}
    )
    append_correction(ledger, fix)

    assert latest_rows(ledger)["EAWF-0001"]["status_note"] == "new"


@pytest.mark.parametrize("tamper", ["rewrite", "truncate", "rename"])
def test_reader_raises_on_a_snapshot_that_no_longer_matches_its_name(
    tmp_path: Path, tamper: str
) -> None:
    ledger = tmp_path / "ledger" / "task.jsonl"
    _mixed_ledger(ledger)
    plan = plan_legacy_seal(ledger)
    assert plan is not None
    snapshot = seal_ledger_prefix(ledger, plan.cut)
    latest_rows(ledger)
    if tamper == "rewrite":
        snapshot.write_bytes(snapshot.read_bytes().replace(b"EAWF-0002", b"EAWF-0003"))
    elif tamper == "truncate":
        snapshot.write_bytes(snapshot.read_bytes()[:-1])
    else:
        snapshot.rename(snapshot.with_name(f"task.{'0' * 64}.jsonl"))

    with pytest.raises(LedgerSnapshotError, match="refusing to read"):
        read_ledger_records(ledger)
    with pytest.raises(LedgerSnapshotError):
        latest_rows(ledger)
    with pytest.raises(LedgerSnapshotError):
        finish_interrupted_seal(ledger)


def test_reader_raises_on_two_snapshots(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger" / "task.jsonl"
    content = _write(ledger, [_record("EAWF-0001")])
    seal_ledger_prefix(ledger, len(content))
    extra = ledger.parent / SNAPSHOT_DIRNAME / snapshot_name("task", b"")
    extra.write_bytes(b"")

    with pytest.raises(LedgerSnapshotError, match="2 snapshots"):
        read_ledger_records(ledger)


def test_another_ledgers_snapshot_is_not_read(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger" / "task.jsonl"
    _write(ledger, [_record("EAWF-0001")])
    other = tmp_path / "ledger" / SNAPSHOT_DIRNAME / snapshot_name("batch", b"x\n")
    other.parent.mkdir(parents=True)
    other.write_bytes(b"x\n")

    assert ledger_snapshot(ledger) is None
    assert len(read_ledger_records(ledger)) == 1


def test_a_missing_ledger_reads_empty() -> None:
    missing = Path(tempfile.gettempdir()) / "eawf-no-such-ledger" / "task.jsonl"
    assert read_ledger_records(missing) == ()
    assert dict(latest_rows(missing)) == {}


@pytest.mark.parametrize("cut", [0, -1, 5, 10_000])
def test_seal_refuses_a_cut_off_a_line_boundary(tmp_path: Path, cut: int) -> None:
    ledger = tmp_path / "ledger" / "task.jsonl"
    content = _write(ledger, [_record("EAWF-0001"), _record("EAWF-0002")])

    with pytest.raises(LedgerSnapshotError, match="not a line boundary"):
        seal_ledger_prefix(ledger, cut)

    assert ledger.read_bytes() == content
    assert ledger_snapshot(ledger) is None


def test_seal_refuses_a_second_seal(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger" / "task.jsonl"
    content = _write(ledger, [_record("EAWF-0001"), _record("EAWF-0002")])
    first_cut = content.index(b"\n") + 1
    seal_ledger_prefix(ledger, first_cut)

    with pytest.raises(LedgerSnapshotError, match="already sealed"):
        seal_ledger_prefix(ledger, len(ledger.read_bytes()))


def test_seal_refuses_a_torn_ledger(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger" / "task.jsonl"
    content = _write(ledger, [_record("EAWF-0001")])
    ledger.write_bytes(content + b'{"torn')

    with pytest.raises(LedgerTornTailError):
        seal_ledger_prefix(ledger, len(content))


# --- interrupted seal and recovery -------------------------------------------------


def test_interrupted_seal_reads_unchanged_and_recovery_finishes_it(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger" / "task.jsonl"
    content = _mixed_ledger(ledger)
    before = _digests(ledger)
    plan = plan_legacy_seal(ledger)
    assert plan is not None
    # The crash point between the seal's two writes: snapshot on disk, live untouched.
    snapshot = ledger.parent / SNAPSHOT_DIRNAME / snapshot_name("task", content[: plan.cut])
    snapshot.parent.mkdir()
    snapshot.write_bytes(content[: plan.cut])

    assert _digests(ledger) == before
    assert finish_interrupted_seal(ledger) is True
    assert ledger.read_bytes() == content[plan.cut :]
    assert _digests(ledger) == before
    assert finish_interrupted_seal(ledger) is False


def test_finish_interrupted_seal_without_a_snapshot_is_a_no_op(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger" / "task.jsonl"
    content = _write(ledger, [_record("EAWF-0001")])
    assert finish_interrupted_seal(ledger) is False
    assert ledger.read_bytes() == content


# --- legacy plan ------------------------------------------------------------------


def test_plan_cuts_through_the_last_legacy_origin_line(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger" / "task.jsonl"
    content = _mixed_ledger(ledger)
    lines = content.split(b"\n")[:-1]

    plan = plan_legacy_seal(ledger)

    assert plan is not None
    assert plan.sealed_lines == len(lines) - 1
    assert plan.live_lines == 1
    assert plan.cut == len(content) - len(lines[-1]) - 1
    assert plan.sealed_digest == f"sha256:{hashlib.sha256(content[: plan.cut]).hexdigest()}"


def test_plan_is_none_without_a_legacy_line(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger" / "task.jsonl"
    _write(ledger, [_record("EAWF-0001")])
    assert plan_legacy_seal(ledger) is None


def test_plan_is_none_for_a_missing_or_empty_ledger(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger" / "task.jsonl"
    assert plan_legacy_seal(ledger) is None
    _write(ledger, [])
    assert plan_legacy_seal(ledger) is None


def test_plan_is_none_once_sealed(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger" / "task.jsonl"
    content = _write(ledger, [_record("EAWF-0001", origin="legacy"), _record("EAWF-0002")])
    seal_ledger_prefix(ledger, content.index(b"\n") + 1)
    assert plan_legacy_seal(ledger) is None


def test_plan_rejects_a_zero_cut() -> None:
    with pytest.raises(ValueError, match="greater than 0"):
        LegacySealPlan(sealed_digest=f"sha256:{'0' * 64}", cut=0, sealed_lines=1, live_lines=0)


def _generation(tmp_path: Path, records: list[LedgerRecord]) -> Path:
    state_path = tmp_path / "gen" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{}\n", encoding="utf-8")
    _write(ledger_path(state_path, Epoch2Collection.TASK), records)
    return state_path


def test_seal_legacy_import_refuses_a_stale_plan(tmp_path: Path) -> None:
    state_path = _generation(tmp_path, [_record("EAWF-0001", origin="legacy")])
    ledger = ledger_path(state_path, Epoch2Collection.TASK)
    plan = plan_legacy_seal(ledger)
    assert plan is not None
    append_ledger_record(ledger, _record("EAWF-0002", origin="legacy"))

    with pytest.raises(LedgerSnapshotError, match="changed since"):
        seal_legacy_import(state_path, collection=Epoch2Collection.TASK, plan=plan)
    assert ledger_snapshot(ledger) is None


def test_seal_legacy_import_seals_and_recovery_keeps_it(tmp_path: Path) -> None:
    state_path = _generation(
        tmp_path, [_record("EAWF-0001", origin="legacy"), _record("EAWF-0002")]
    )
    ledger = ledger_path(state_path, Epoch2Collection.TASK)
    before = _digests(ledger)
    plan = plan_legacy_seal(ledger)
    assert plan is not None

    snapshot = seal_legacy_import(state_path, collection=Epoch2Collection.TASK, plan=plan)
    recover_store_tree(state_path)

    assert snapshot.name.startswith("task.")
    assert _digests(ledger) == before
    assert LEGACY_MARK not in ledger.read_bytes()


# --- this repository's Task ledger -------------------------------------------------


def _origin_kind(payload: object) -> object:
    """Return ``origin.kind`` of a ledger payload, or ``None`` when it names none."""
    origin = payload.get("origin") if isinstance(payload, dict) else None
    return origin.get("kind") if isinstance(origin, dict) else None


def _repo_task_ledgers() -> list[Path]:
    return sorted((REPO_ROOT / ".ea" / "generations").glob("gen-*/ledger/task.jsonl"))


@pytest.mark.parametrize("source", _repo_task_ledgers(), ids=lambda p: p.parent.parent.name)
def test_this_repositorys_task_ledger_seals_without_changing_a_row(
    tmp_path: Path, source: Path
) -> None:
    ledger = tmp_path / "ledger" / "task.jsonl"
    shutil.copytree(source.parent, ledger.parent)
    before = _digests(ledger)
    plan = plan_legacy_seal(ledger)
    if plan is not None:
        seal_ledger_prefix(ledger, plan.cut)
        assert _digests(ledger) == before
    snapshot = ledger_snapshot(ledger)
    assert snapshot is not None
    assert snapshot_digest_mismatch(snapshot.name, snapshot.read_bytes()) is None
    lines = split_ledger_lines(ledger.read_bytes())
    live = [LedgerRecord.model_validate_json(line) for line in lines]
    assert not [r.record_key for r in live if _origin_kind(r.payload) == LEGACY_ORIGIN_KIND]
    # A retired import is filed after the seal as a DROPPED row wrapping the imported
    # record, so its legacy origin sits one level down and the row reads as plain data.
    retired = [r for r in live if _origin_kind(r.payload.get("payload")) == LEGACY_ORIGIN_KIND]
    assert [r.record_key for r in retired if r.status != "DROPPED"] == []
    sealed = snapshot.read_bytes()

    append_ledger_record(ledger, _record("EAWF-9999"))

    assert snapshot.read_bytes() == sealed
    assert read_ledger_records(ledger)[-1].record_key == "EAWF-9999"


# --- commit policy ------------------------------------------------------------------


def test_snapshot_path_is_a_committed_ledger() -> None:
    row = classify_path(
        f".ea/generations/gen-0123456789abcdef/ledger/snapshots/task.{'a' * 64}.jsonl"
    )
    assert row.policy is CommitPolicy.COMMITTED
    assert row.tier is StorageTier.LEDGER


def test_snapshot_star_does_not_cross_a_separator() -> None:
    with pytest.raises(UndeclaredPathError):
        classify_path(".ea/generations/gen-0123456789abcdef/ledger/snapshots/deep/task.jsonl")


# --- the append-only ledger gate's pure checks ----------------------------


def test_snapshot_findings_flag_two_snapshots_and_a_bad_digest() -> None:
    good = f"x/ledger/snapshots/{snapshot_name('task', b'a\n')}"
    bad = f"x/ledger/snapshots/task.{'0' * 64}.jsonl"
    findings = snapshot_findings("x/ledger/task.jsonl", {good: b"a\n", bad: b"b\n"})
    assert [f.path for f in findings] == ["x/ledger/task.jsonl", bad]
    assert snapshot_findings("x/ledger/task.jsonl", {}) == ()


@pytest.mark.parametrize(
    ("path", "owner"),
    [
        (
            ".ea/generations/gen-x/ledger/snapshots/task.abc.jsonl",
            ".ea/generations/gen-x/ledger/task.jsonl",
        ),
        (".ea/generations/gen-x/ledger/task.jsonl", None),
        (".ea/generations/gen-x/history/snapshots/task.abc.jsonl", None),
        ("", None),
    ],
)
def test_snapshot_owner(path: str, owner: str | None) -> None:
    assert snapshot_owner(path) == owner


def test_sha_mentions_skip_a_snapshot_row_the_live_file_supersedes() -> None:
    sha = "1" * 40
    row = render_ledger_line(_record("EAWF-0001", commit=sha))
    content = f"{row}\n".encode()

    standing, _ = sha_mentions("snap.jsonl", content)
    superseded, _ = sha_mentions(
        "snap.jsonl", content, superseded_later=frozenset({line_digest(row)})
    )

    assert [m.sha for m in standing] == [sha]
    assert superseded == ()
