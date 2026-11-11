"""Session start warns once about the hook failures recorded since the last warning.

A failed hook leaves a health row naming its host session. The next session
start reads the rows past the byte-offset cursor beside the record, reports
them in one line and moves the cursor, so the session after it says nothing.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner  # noqa: EAWF024 the hook's stdout is the contract

from eawf.runtime.hooks import health as health_module
from eawf.runtime.hooks import runner as runner_module
from eawf.runtime.hooks.event import HookEvent, HookEventType
from eawf.runtime.hooks.health import (
    HEALTH_CURSOR,
    HEALTH_RECORD,
    HEALTH_ROTATE_BYTES,
    HEALTH_ROTATED,
    HOOK_RAISED,
    HealthRow,
    HookFailure,
    failure_warning,
    read_cursor,
    read_health_rows,
    record_failures,
    take_failure_warning,
    write_cursor,
)
from eawf.runtime.hooks.runner import HookResult, HookRunner
from eawf.surfaces.cli.app import app

_AT = datetime(2026, 11, 10, tzinfo=UTC)

_WARNING = (
    "eawf: 1 hook failures since the last session (top: hook_raised x1); run eawf doctor hooks"
)


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a workspace with an ``.ea`` directory and no host plugin record."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / ".ea").mkdir()
    return tmp_path


def _refusing_daemon() -> Any:
    raise ConnectionRefusedError("no daemon")


def _hook_run(tree: Path, event: str, payload: dict[str, Any]) -> Any:
    return CliRunner().invoke(
        app,
        ["--workspace", str(tree), "hook", "run", event, "--runtime", "claude"],
        input=json.dumps(payload),
    )


def _session_start(tree: Path) -> Any:
    return _hook_run(tree, "session_start", {"session_id": "s2", "hook_event_name": "SessionStart"})


def _row(code: str = HOOK_RAISED, *, session_id: str | None = "s1") -> HealthRow:
    return HealthRow(
        at="2026-11-10T00:00:00+00:00",
        event="post_tool_use",
        hook="h",
        code=code,
        error="E",
        detail="",
        session_id=session_id,
    )


def _write_rows(tree: Path, *lines: bytes) -> Path:
    path = tree / ".ea" / HEALTH_RECORD
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(lines))
    return path


def _line(row: HealthRow) -> bytes:
    return row.model_dump_json().encode("utf-8") + b"\n"


def test_failure_warning_once(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _register(runner: HookRunner, **_kwargs: object) -> None:
        def _boom(_event: HookEvent) -> HookResult:
            raise RuntimeError("handler broke")

        runner.register(HookEventType.POST_TOOL_USE, _boom, name="test.boom")

    monkeypatch.setattr(runner_module, "register_runtime_capture_hooks", _register)

    failed = _hook_run(tree, "post_tool_use", {"session_id": "s1", "tool_name": "Bash"})
    first = _session_start(tree)
    second = _session_start(tree)

    assert failed.exit_code == 0, failed.output
    rows = read_health_rows(tree / ".ea").rows
    assert [(row.code, row.session_id) for row in rows] == [(HOOK_RAISED, "s1")]
    assert first.exit_code == 0, first.output
    document = json.loads(first.stdout)
    assert document["systemMessage"] == _WARNING
    assert _WARNING in document["hookSpecificOutput"]["additionalContext"]
    record_size = (tree / ".ea" / HEALTH_RECORD).stat().st_size
    assert read_cursor(tree / ".ea") == record_size
    assert (tree / ".ea" / HEALTH_CURSOR).parent == (tree / ".ea" / HEALTH_RECORD).parent
    assert second.exit_code == 0, second.output
    assert "hook failures since the last session" not in second.stdout


def test_codex_gets_the_warning_in_its_context_document_only(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_module, "_default_daemon_client_factory", _refusing_daemon)
    record_failures(
        tree / ".ea",
        [HookFailure("pre_tool_use", "h", HOOK_RAISED, "E", "", _AT)],
        session_id="s1",
    )

    result = CliRunner().invoke(
        app,
        ["--workspace", str(tree), "hook", "run", "session_start", "--runtime", "codex"],
        input=json.dumps({"session_id": "s2"}),
    )

    document = json.loads(result.stdout)
    assert "systemMessage" not in document
    assert _WARNING in document["hookSpecificOutput"]["additionalContext"]


def test_record_failures_writes_a_null_session_when_none_is_named(tree: Path) -> None:
    record_failures(tree / ".ea", [HookFailure("e", "h", HOOK_RAISED, "E", "d", _AT)])

    (row,) = read_health_rows(tree / ".ea").rows
    assert row.session_id is None


def test_read_health_rows_without_a_record_is_empty(tree: Path) -> None:
    read = read_health_rows(tree / ".ea")

    assert (read.rows, read.malformed, read.end) == ((), 0, 0)


def test_read_health_rows_of_an_empty_record_is_empty(tree: Path) -> None:
    _write_rows(tree, b"")

    assert read_health_rows(tree / ".ea").end == 0


def test_read_health_rows_reads_from_the_offset(tree: Path) -> None:
    first, second = _line(_row("a")), _line(_row("b"))
    _write_rows(tree, first, second)

    read = read_health_rows(tree / ".ea", offset=len(first))

    assert [row.code for row in read.rows] == ["b"]
    assert read.end == len(first) + len(second)


def test_read_health_rows_at_the_end_reads_nothing(tree: Path) -> None:
    line = _line(_row())
    _write_rows(tree, line)

    read = read_health_rows(tree / ".ea", offset=len(line))

    assert (read.rows, read.end) == ((), len(line))


def test_read_health_rows_past_the_end_reads_a_rewritten_record_whole(tree: Path) -> None:
    line = _line(_row())
    _write_rows(tree, line)

    read = read_health_rows(tree / ".ea", offset=len(line) + 1)

    assert len(read.rows) == 1
    assert read.end == len(line)


def test_read_health_rows_leaves_an_unfinished_line(tree: Path) -> None:
    line = _line(_row())
    _write_rows(tree, line, b'{"at":"2026')

    read = read_health_rows(tree / ".ea")

    assert (len(read.rows), read.end) == (1, len(line))


def test_read_health_rows_counts_a_row_that_does_not_parse(tree: Path) -> None:
    extra = json.dumps({**_row().model_dump(), "surprise": 1}).encode() + b"\n"
    _write_rows(tree, b"not json\n", extra, b"\n", _line(_row()))

    read = read_health_rows(tree / ".ea")

    assert (len(read.rows), read.malformed) == (1, 2)


def test_read_health_rows_reads_a_row_without_a_session(tree: Path) -> None:
    legacy = {k: v for k, v in _row().model_dump().items() if k != "session_id"}
    _write_rows(tree, json.dumps(legacy).encode() + b"\n")

    (row,) = read_health_rows(tree / ".ea").rows

    assert row.session_id is None


def test_read_health_rows_refuses_a_negative_offset(tree: Path) -> None:
    with pytest.raises(ValueError, match="negative"):
        read_health_rows(tree / ".ea", offset=-1)


def test_read_cursor_without_a_cursor_is_zero(tree: Path) -> None:
    assert read_cursor(tree / ".ea") == 0


@pytest.mark.parametrize("text", ["", "abc", "-3", "1.5"])
def test_read_cursor_of_a_garbled_cursor_is_zero(tree: Path, text: str) -> None:
    path = tree / ".ea" / HEALTH_CURSOR
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")

    assert read_cursor(tree / ".ea") == 0


def test_write_cursor_round_trips_and_leaves_no_staging_file(tree: Path) -> None:
    write_cursor(tree / ".ea", 0)
    write_cursor(tree / ".ea", 42)

    assert read_cursor(tree / ".ea") == 42
    assert os.listdir(tree / ".ea" / HEALTH_CURSOR.parent) == [HEALTH_CURSOR.name]


def test_write_cursor_refuses_a_negative_offset(tree: Path) -> None:
    with pytest.raises(ValueError, match="negative"):
        write_cursor(tree / ".ea", -1)


def test_failure_warning_of_no_rows_is_none() -> None:
    assert failure_warning(()) is None


def test_failure_warning_names_the_most_frequent_code() -> None:
    rows = (_row("a"), _row("b"), _row("b"))

    assert failure_warning(rows) == (
        "eawf: 3 hook failures since the last session (top: b x2); run eawf doctor hooks"
    )


def test_failure_warning_breaks_a_tie_by_the_earliest_code() -> None:
    assert "(top: a x1)" in str(failure_warning((_row("a"), _row("b"))))


def test_take_failure_warning_without_a_record_writes_no_cursor(tree: Path) -> None:
    assert take_failure_warning(tree / ".ea") is None
    assert not (tree / ".ea" / HEALTH_CURSOR).exists()


def test_take_failure_warning_reports_only_rows_after_the_cursor(tree: Path) -> None:
    first = _line(_row("a"))
    path = _write_rows(tree, first)
    assert take_failure_warning(tree / ".ea") is not None
    with path.open("ab") as handle:
        handle.write(_line(_row("b")))

    warning = take_failure_warning(tree / ".ea")

    assert warning == (
        "eawf: 1 hook failures since the last session (top: b x1); run eawf doctor hooks"
    )
    assert take_failure_warning(tree / ".ea") is None


def test_health_row_refuses_a_missing_field() -> None:
    with pytest.raises(ValueError, match="code"):
        HealthRow.model_validate({"at": "", "event": "", "hook": "", "error": "", "detail": ""})


# ---- the record's size bound -------------------------------------------------------------------


def _failure(code: str = HOOK_RAISED) -> HookFailure:
    return HookFailure(event="post_tool_use", hook="h", code=code, error="E", detail="", at=_AT)


def _coded(code: str) -> bytes:
    """Return a record line of a failure with *code*."""
    return _line(_row(code))


def _fill_past_limit(tree: Path, code: str) -> int:
    """Append rows of *code* until the record is past the rotation limit; return how many."""
    record = tree / ".ea" / HEALTH_RECORD
    record.parent.mkdir(parents=True, exist_ok=True)
    size = record.stat().st_size if record.is_file() else 0
    count = (HEALTH_ROTATE_BYTES - size) // len(_coded(code)) + 1
    with record.open("ab") as handle:
        handle.write(_coded(code) * count)
    return count


def test_record_failures_rotates_past_the_limit_and_keeps_the_newest(tree: Path) -> None:
    ea = tree / ".ea"
    first = _fill_past_limit(tree, "first")
    assert not (ea / HEALTH_ROTATED).exists()
    record_failures(ea, [_failure("second")])
    # The full record became the older generation; the new row starts a fresh one.
    assert (ea / HEALTH_ROTATED).stat().st_size > HEALTH_ROTATE_BYTES
    assert [row.code for row in read_health_rows(ea).rows] == ["first"] * first + ["second"]
    second = _fill_past_limit(tree, "second") + 1
    record_failures(ea, [_failure("third")])
    # A second rotation drops the oldest generation: the record stays bounded.
    rows = read_health_rows(ea).rows
    assert [row.code for row in rows] == ["second"] * second + ["third"]
    total = sum((ea / p).stat().st_size for p in (HEALTH_RECORD, HEALTH_ROTATED))
    assert total < 2 * HEALTH_ROTATE_BYTES + 1024


def test_the_cursor_stays_on_the_same_row_across_a_rotation(tree: Path) -> None:
    ea = tree / ".ea"
    _fill_past_limit(tree, "first")
    record_failures(ea, [_failure("second")])
    # Everything so far was reported.
    assert take_failure_warning(ea) is not None
    assert take_failure_warning(ea) is None
    added = _fill_past_limit(tree, "second")
    record_failures(ea, [_failure("third")])
    # The rotation dropped the reported first generation and moved the cursor back
    # by its bytes: the next warning names only rows written since the last one.
    warning = take_failure_warning(ea)
    assert warning is not None
    assert warning.startswith(f"eawf: {added + 1} hook failures since the last session")
    assert take_failure_warning(ea) is None


def test_a_rotation_past_an_unread_cursor_reads_the_newest_generation(tree: Path) -> None:
    ea = tree / ".ea"
    _fill_past_limit(tree, "first")
    record_failures(ea, [_failure("second")])
    _fill_past_limit(tree, "second")
    record_failures(ea, [_failure("third")])
    # The cursor never moved: rows of the dropped generation are gone, the rest read.
    assert read_cursor(ea) == 0
    assert take_failure_warning(ea) is not None
    assert read_health_rows(ea, offset=read_cursor(ea)).rows == ()


def test_a_record_of_exactly_the_limit_is_not_rotated(tree: Path) -> None:
    ea = tree / ".ea"
    record = ea / HEALTH_RECORD
    record.parent.mkdir(parents=True)
    record.write_bytes(b"x" * (HEALTH_ROTATE_BYTES - 1) + b"\n")
    write_cursor(ea, 3)
    record_failures(ea, [_failure()])
    # Past the limit only after this append; the next writer rotates.
    assert not (ea / HEALTH_ROTATED).exists()
    assert read_cursor(ea) == 3


# ---- one locked rotation rule ----------------------------------------------------------------


def test_rotation_repro_two_writers_rotating_at_once(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second writer rotating inside the first's rotation keeps the full generation."""
    ea = tree / ".ea"
    first = _fill_past_limit(tree, "first")
    real_replace = os.replace
    nested: list[str] = []

    def racing_replace(src: Any, dst: Any) -> None:
        # The other writer arrives just before this one moves the record.
        if Path(src).name == HEALTH_RECORD.name and not nested:
            nested.append("ran")
            record_failures(ea, [_failure("racer")])
        real_replace(src, dst)

    monkeypatch.setattr(health_module.os, "replace", racing_replace)
    record_failures(ea, [_failure("second")])

    # Exactly one rotation: the full generation survives and no row is lost.
    codes = [row.code for row in read_health_rows(ea).rows]
    assert codes == ["first"] * first + ["racer", "second"]


def test_rotation_repro_reader_keeps_its_layout(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rotation racing the session-start warning never repeats the warning."""
    ea = tree / ".ea"
    _fill_past_limit(tree, "first")
    record_failures(ea, [_failure("second")])
    assert take_failure_warning(ea) is not None
    _fill_past_limit(tree, "third")
    real_read = health_module.read_health_rows
    raced: list[str] = []

    def racing_read(ea_dir: Path, *, offset: int = 0) -> Any:
        read = real_read(ea_dir, offset=offset)
        if not raced:
            # Another hook rotates between the read and the cursor write.
            raced.append("ran")
            record_failures(ea, [_failure("racer")])
        return read

    monkeypatch.setattr(health_module, "read_health_rows", racing_read)
    assert take_failure_warning(ea) is not None
    monkeypatch.setattr(health_module, "read_health_rows", real_read)

    # The next session start reports only the racing row, never the whole record again.
    assert take_failure_warning(ea) == _WARNING.replace("hook_raised", "racer")
    assert take_failure_warning(ea) is None


def test_rotation_repro_reads_tolerate_a_rotation_between_reads(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ea = tree / ".ea"
    record = ea / HEALTH_RECORD
    record.parent.mkdir(parents=True)
    (ea / HEALTH_ROTATED).write_bytes(_coded("older"))
    record.write_bytes(_coded("current"))
    real_read_bytes = Path.read_bytes
    raced: list[str] = []

    def racing_read_bytes(self: Path) -> bytes:
        data = real_read_bytes(self)
        if self.name == HEALTH_ROTATED.name and not raced:
            raced.append("ran")
            real_replace = os.replace
            real_replace(record, ea / HEALTH_ROTATED)
            record.write_bytes(_coded("newest"))
        return data

    monkeypatch.setattr(Path, "read_bytes", racing_read_bytes)
    codes = [row.code for row in read_health_rows(ea).rows]
    # Read again after the rotation: no row twice, none of the kept rows missed.
    assert codes == ["current", "newest"]


def test_the_wrapper_and_the_writers_share_one_older_generation(tree: Path) -> None:
    ea = tree / ".ea"
    _fill_past_limit(tree, "first")
    record_failures(ea, [_failure("second")])
    assert (ea / HEALTH_ROTATED).name == "health.1.jsonl"
    assert not (ea / HEALTH_RECORD.with_name("health.jsonl.1")).exists()


@pytest.mark.parametrize("acked", [False, True], ids=["alone", "beside-an-acked-file"])
def test_a_leftover_legacy_generation_is_taken_over(tree: Path, acked: bool) -> None:
    ea = tree / ".ea"
    legacy = ea / HEALTH_RECORD.with_name("health.jsonl.1")
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(_coded("legacy") * 3)
    if acked:
        (ea / HEALTH_ROTATED).write_bytes(_coded("acked") * 5)
    (ea / HEALTH_RECORD).write_bytes(_coded("current"))
    # A cursor over files this release no longer pairs: its offsets mean nothing.
    write_cursor(ea, 2 * len(_coded("legacy")))

    warning = take_failure_warning(ea)

    assert not legacy.exists()
    assert (ea / HEALTH_ROTATED).read_bytes() == _coded("legacy") * 3
    # The cursor moved to the new end on adoption, so the adopted rows count as reported.
    assert warning is None
    record_failures(ea, [_failure("after")])
    assert take_failure_warning(ea) is not None


def test_a_writer_that_cannot_take_the_lock_still_appends(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eawf.runtime.lock import portalock

    ea = tree / ".ea"
    _fill_past_limit(tree, "first")
    monkeypatch.setattr(health_module, "_LOCK_TIMEOUT_S", 0.05)
    with portalock.acquire(ea / HEALTH_RECORD, timeout=1.0):
        assert record_failures(ea, [_failure("second")]) == 1
        # The session-start warning waits for the next session rather than racing.
        assert take_failure_warning(ea) is None
    assert not (ea / HEALTH_ROTATED).exists()
    assert read_health_rows(ea).rows[-1].code == "second"
