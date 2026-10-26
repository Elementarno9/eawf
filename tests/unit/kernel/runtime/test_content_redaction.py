"""Withholding leaked lines from the content rows no commit holds yet.

Every leak fixture is assembled from split pieces, so this file carries no
literal the leak lints or detect-secrets would flag.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eawf.kernel.runtime.content import CONTENT_PAYLOAD_KIND, WITHHELD_LINE, StoredContent
from eawf.kernel.runtime.content_redaction import (
    REDACTION_NONCANONICAL_LINE,
    REDACTION_PUBLISHED_CHANGED,
    REDACTION_ROW_AMBIGUOUS,
    REDACTION_ROW_CITED,
    REDACTION_ROW_COMPACTED,
    REDACTION_ROW_CONTENT,
    REDACTION_ROW_NOT_FOUND,
    REDACTION_ROW_PUBLISHED,
    REDACTION_ROW_REVISION,
    REDACTION_ROW_SUPERSEDES,
    ContentRedaction,
    ContentRedactionError,
    NonContentLeak,
    plan_content_redaction,
    plan_row_drop,
)
from eawf.kernel.store.ledger import (
    LedgerRecord,
    LedgerTornTailError,
    line_digest,
    render_ledger_line,
    split_ledger_lines,
)
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.observability.logging.state_leak import default_allowed_emails

pytestmark = pytest.mark.unit

RUN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000010"
AT = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
SERVER_PATH = "/" + "srv" + "/" + "checkout" + "/eawf/.ea/locks/epoch2/document.lock"
HOME_PATH = "/" + "home" + "/" + "alice" + "/notes"
ALLOWED = default_allowed_emails()


def _content(ref: str, *lines: str, withheld: int = 0) -> str:
    stored = StoredContent(
        artifact_ref=f"artifact://content/run-00000010/{ref}",
        run_ref=RUN,
        lines=lines,
        total_lines=len(lines),
        withheld_lines=withheld,
        recorded_at=AT,
    )
    record = LedgerRecord(
        collection=Epoch2Collection.ARTIFACT,
        record_key=stored.artifact_ref,
        status=CONTENT_PAYLOAD_KIND,
        recorded_at=AT,
        payload=stored.model_dump(mode="json"),
    )
    return render_ledger_line(record)


def _other(key: str, text: str, *, supersedes: str | None = None) -> str:
    record = LedgerRecord(
        collection=Epoch2Collection.ARTIFACT,
        record_key=key,
        status="recorded",
        recorded_at=AT,
        supersedes=supersedes,
        payload={"summary": text},
    )
    return render_ledger_line(record)


def _file(*lines: str) -> bytes:
    return "".join(f"{line}\n" for line in lines).encode("utf-8")


def _plan(published: bytes, live: bytes) -> ContentRedaction:
    return plan_content_redaction(published, live, allowed_emails=ALLOWED)


def _keys(plan: ContentRedaction) -> tuple[str, ...]:
    return tuple(leak.record_key for leak in plan.non_content_leaks)


def _row(key: str, payload: dict[str, object]) -> str:
    record = LedgerRecord(
        collection=Epoch2Collection.ARTIFACT,
        record_key=key,
        status="recorded",
        recorded_at=AT,
        payload=payload,
    )
    return render_ledger_line(record)


def _drop_code(published: bytes, live: bytes, key: str, elsewhere: list[object]) -> str:
    with pytest.raises(ContentRedactionError) as caught:
        plan_row_drop(published, live, record_key=key, elsewhere=elsewhere)
    return caught.value.code


def test_an_empty_ledger_plans_nothing() -> None:
    plan = _plan(b"", b"")

    assert (plan.content, plan.record_keys, plan.withheld_lines, plan.unpublished_rows) == (
        b"",
        (),
        0,
        0,
    )


def test_a_clean_unpublished_row_is_left_byte_for_byte() -> None:
    live = _file(_content("aa", "built 3 targets"))

    plan = _plan(b"", live)

    assert plan.content == live
    assert plan.record_keys == ()
    assert plan.unpublished_rows == 1


def test_a_server_path_line_is_withheld_and_the_row_keeps_its_ref() -> None:
    published = _file(_content("aa", "ok"))
    live = published + _file(_content("bb", "start", f"locked {SERVER_PATH}", "done"))

    plan = _plan(published, live)

    assert plan.content.startswith(published)
    redacted = LedgerRecord.model_validate_json(split_ledger_lines(plan.content)[-1])
    stored = StoredContent.model_validate(redacted.payload)
    assert stored.lines == ("start", WITHHELD_LINE, "done")
    assert stored.withheld_lines == 1
    assert stored.artifact_ref == "artifact://content/run-00000010/bb"
    assert plan.record_keys == ("artifact://content/run-00000010/bb",)
    assert plan.withheld_lines == 1
    assert "checkout" not in plan.content.decode("utf-8")


def test_lines_withheld_at_capture_are_counted_once() -> None:
    live = _file(_content("cc", WITHHELD_LINE, f"see {SERVER_PATH}", withheld=1))

    plan = _plan(b"", live)

    stored = StoredContent.model_validate(
        LedgerRecord.model_validate_json(split_ledger_lines(plan.content)[0]).payload
    )
    assert stored.lines == (WITHHELD_LINE, WITHHELD_LINE)
    assert stored.withheld_lines == 2
    assert plan.withheld_lines == 1


def test_a_committed_row_is_never_touched_even_when_it_leaks() -> None:
    published = _file(_content("dd", f"old {SERVER_PATH}"))
    live = published + _file(_content("ee", "fine"))

    plan = _plan(published, live)

    assert plan.content == live
    assert plan.record_keys == ()


def test_a_ledger_that_moved_off_its_commit_is_refused() -> None:
    published = _file(_content("aa", "ok"))
    live = _file(_content("aa", "changed"))

    with pytest.raises(ContentRedactionError) as caught:
        _plan(published, live)
    assert caught.value.code == REDACTION_PUBLISHED_CHANGED


def test_a_torn_tail_is_refused() -> None:
    live = _file(_content("aa", "ok"))[:-1]

    with pytest.raises(LedgerTornTailError):
        _plan(b"", live)


def test_a_line_that_does_not_render_back_is_refused() -> None:
    live = _file(_content("aa", "ok").replace('","', '", "', 1))

    with pytest.raises(ContentRedactionError) as caught:
        _plan(b"", live)
    assert caught.value.code == REDACTION_NONCANONICAL_LINE


def test_content_rows_are_redacted_when_a_non_content_line_leaks() -> None:
    other = _other("EVD-0001", f"wrote {HOME_PATH}")
    live = _file(_content("aa", "start", f"locked {SERVER_PATH}"), other)

    plan = _plan(b"", live)

    lines = split_ledger_lines(plan.content)
    stored = StoredContent.model_validate(LedgerRecord.model_validate_json(lines[0]).payload)
    assert stored.lines == ("start", WITHHELD_LINE)
    assert lines[1] == other
    assert plan.record_keys == ("artifact://content/run-00000010/aa",)
    assert _keys(plan) == ("EVD-0001",)
    assert "alice" not in "".join(_keys(plan))


def test_a_leaking_non_content_line_alone_writes_nothing_and_is_listed() -> None:
    live = _file(_other("EVD-0001", f"wrote {HOME_PATH}"), _other("EVD-0002", "clean"))

    plan = _plan(b"", live)

    assert plan.content == live
    assert plan.record_keys == ()
    assert _keys(plan) == ("EVD-0001",)


def test_a_leak_behind_a_json_escape_outside_content_is_listed() -> None:
    live = _file(_other("EVD-0003", f"first line\n{SERVER_PATH}"))

    assert _keys(_plan(b"", live)) == ("EVD-0003",)


def test_a_committed_leaking_non_content_line_is_not_listed() -> None:
    published = _file(_other("EVD-0004", f"wrote {HOME_PATH}"))

    assert _plan(published, published).non_content_leaks == ()


def test_a_second_plan_over_the_redacted_ledger_changes_nothing() -> None:
    live = _file(_content("aa", f"at {SERVER_PATH}"), _other("EVD-0001", f"wrote {HOME_PATH}"))
    first = _plan(b"", live)

    again = _plan(b"", first.content)

    assert again.content == first.content
    assert again.record_keys == ()
    assert _keys(again) == ("EVD-0001",)


def test_a_grep_context_line_is_withheld() -> None:
    live = _file(_content("hh", f"513-{SERVER_PATH}", "514-ok"))

    plan = _plan(b"", live)

    stored = StoredContent.model_validate(
        LedgerRecord.model_validate_json(split_ledger_lines(plan.content)[0]).payload
    )
    assert stored.lines == (WITHHELD_LINE, "514-ok")


def test_a_row_another_line_supersedes_is_refused() -> None:
    leaking = _content("ff", f"at {SERVER_PATH}")
    live = _file(leaking, _other("EVD-0002", "corrected", supersedes=line_digest(leaking)))

    with pytest.raises(ContentRedactionError) as caught:
        _plan(b"", live)
    assert caught.value.code == REDACTION_ROW_CITED


def test_an_allowlisted_address_is_kept() -> None:
    live = _file(_content("gg", "Co-Authored-By: Claude <noreply@anthropic.com>"))

    assert _plan(b"", live).content == live


def test_a_leaking_non_content_line_names_its_payload_kind() -> None:
    live = _file(_row("SPK-0001", {"payload_kind": "spike_report", "summary": SERVER_PATH}))

    assert _plan(b"", live).non_content_leaks == (
        NonContentLeak(record_key="SPK-0001", payload_kind="spike_report"),
    )


def test_a_dropped_line_leaves_every_other_line_as_it_was() -> None:
    published = _file(_other("EVD-0000", "committed"))
    before, after = _other("EVD-0002", "before"), _other("EVD-0003", "after")
    live = published + _file(before, _row("EVD-0001", {"payload_kind": "x", "s": HOME_PATH}), after)

    drop = plan_row_drop(published, live, record_key="EVD-0001", elsewhere=[])

    assert drop.content == published + _file(before, after)
    assert drop.record_key == "EVD-0001"
    assert drop.payload_kind == "x"
    assert drop.unpublished_rows == 2


def test_dropping_the_only_unpublished_line_leaves_the_published_bytes() -> None:
    published = _file(_other("EVD-0000", "committed"))
    live = published + _file(_other("EVD-0001", f"wrote {HOME_PATH}"))

    drop = plan_row_drop(published, live, record_key="EVD-0001", elsewhere=[])

    assert drop.content == published
    assert drop.payload_kind is None
    assert drop.unpublished_rows == 0


def test_dropping_from_an_empty_ledger_finds_nothing() -> None:
    assert _drop_code(b"", b"", "EVD-0001", []) == REDACTION_ROW_NOT_FOUND


def test_dropping_a_published_row_is_refused() -> None:
    published = _file(_other("EVD-0001", f"wrote {HOME_PATH}"))
    live = published + _file(_other("EVD-0001", "corrected"))

    assert _drop_code(published, live, "EVD-0001", []) == REDACTION_ROW_PUBLISHED


def test_dropping_a_key_on_two_unpublished_lines_is_refused() -> None:
    live = _file(_other("EVD-0001", "one"), _other("EVD-0001", "two"))

    assert _drop_code(b"", live, "EVD-0001", []) == REDACTION_ROW_AMBIGUOUS


def test_dropping_a_content_row_is_refused() -> None:
    live = _file(_content("aa", f"at {SERVER_PATH}"))

    code = _drop_code(b"", live, "artifact://content/run-00000010/aa", [])

    assert code == REDACTION_ROW_CONTENT


def test_dropping_a_compacted_record_is_refused() -> None:
    live = _file(_row("ART-0001", {"key": "ART-0001", "summary": HOME_PATH}))

    assert _drop_code(b"", live, "ART-0001", []) == REDACTION_ROW_COMPACTED


def test_dropping_a_superseded_row_is_refused() -> None:
    leaking = _other("EVD-0001", f"wrote {HOME_PATH}")
    live = _file(leaking, _other("EVD-0002", "fixed", supersedes=line_digest(leaking)))

    assert _drop_code(b"", live, "EVD-0001", []) == REDACTION_ROW_CITED


def test_dropping_a_row_the_document_names_is_refused() -> None:
    live = _file(_other("EVD-0001", f"wrote {HOME_PATH}"))
    document = {"plan_revision": {"PRV-0001": {"proposal_ref": "EVD-0001"}}}

    assert _drop_code(b"", live, "EVD-0001", [document]) == REDACTION_ROW_CITED


def test_a_key_that_only_prefixes_a_citation_does_not_refuse() -> None:
    live = _file(_other("EVD-0001", f"wrote {HOME_PATH}"))

    drop = plan_row_drop(b"", live, record_key="EVD-0001", elsewhere=[["EVD-00010"], 1, None])

    assert drop.content == b""


_URN = "urn:eawf:v1:artifact:PRJ-EAWF/"


@pytest.mark.parametrize(
    ("target", "line", "elsewhere", "code"),
    [
        pytest.param(
            "ART-0001#r1",
            _row("ART-0001#r1", {"summary": HOME_PATH}),
            [{"plan_steps": [{"produced": [f"{_URN}ART-0001#r1"]}]}],
            REDACTION_ROW_CITED,
            id="urn-cites-revision-key",
        ),
        pytest.param(
            "ART-0002#r1",
            _row("ART-0002#r1", {"payload_kind": "artifact_revision", "text": HOME_PATH}),
            [],
            REDACTION_ROW_REVISION,
            id="artifact-revision-row",
        ),
        pytest.param(
            "EVD-0002",
            _other("EVD-0002", "fixed", supersedes="sha256:" + "a" * 64),
            [],
            REDACTION_ROW_SUPERSEDES,
            id="row-supersedes-another",
        ),
        pytest.param(
            "EVD-0001",
            _other("EVD-0001", f"wrote {HOME_PATH}"),
            [{"refs": ["see EVD-00010", f"{_URN}EVD-00010#r1"], "EVD-00010": 1}],
            None,
            id="longer-key-does-not-cite",
        ),
        pytest.param(
            "EVD-0001",
            _other("EVD-0001", f"wrote {HOME_PATH}"),
            [],
            None,
            id="uncited-row",
        ),
    ],
)
def test_a_drop_refuses_a_row_a_urn_cites_or_that_supersedes_another(
    target: str, line: str, elsewhere: list[object], code: str | None
) -> None:
    published = _file(_other("EVD-0000", "committed"))
    live = published + _file(line)

    if code is None:
        assert plan_row_drop(published, live, record_key=target, elsewhere=elsewhere).content == (
            published
        )
    else:
        assert _drop_code(published, live, target, elsewhere) == code


def test_dropping_from_a_ledger_that_moved_off_its_commit_is_refused() -> None:
    published = _file(_other("EVD-0000", "committed"))

    assert _drop_code(published, _file(_other("EVD-0001", "x")), "EVD-0001", []) == (
        REDACTION_PUBLISHED_CHANGED
    )


def test_dropping_beside_a_noncanonical_line_is_refused() -> None:
    live = _file(_other("EVD-0001", "x"), _other("EVD-0002", "y").replace('","', '", "', 1))

    assert _drop_code(b"", live, "EVD-0001", []) == REDACTION_NONCANONICAL_LINE


def test_dropping_from_a_torn_ledger_is_refused() -> None:
    with pytest.raises(LedgerTornTailError):
        plan_row_drop(b"", _other("EVD-0001", "x").encode(), record_key="EVD-0001", elsewhere=[])
