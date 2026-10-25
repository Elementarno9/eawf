"""Withholding leaked lines from the content rows no commit holds yet.

Every leak fixture is assembled from split pieces, so this file carries no
literal the leak lints or detect-secrets would flag.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eawf.kernel.runtime.content import CONTENT_PAYLOAD_KIND, WITHHELD_LINE, StoredContent
from eawf.kernel.runtime.content_redaction import (
    REDACTION_LEAK_OUTSIDE_CONTENT,
    REDACTION_NONCANONICAL_LINE,
    REDACTION_PUBLISHED_CHANGED,
    REDACTION_ROW_CITED,
    ContentRedaction,
    ContentRedactionError,
    plan_content_redaction,
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


def test_a_leak_outside_content_lines_is_refused() -> None:
    live = _file(_other("EVD-0001", f"wrote {HOME_PATH}"))

    with pytest.raises(ContentRedactionError) as caught:
        _plan(b"", live)
    assert caught.value.code == REDACTION_LEAK_OUTSIDE_CONTENT


def test_a_leak_behind_a_json_escape_outside_content_is_refused() -> None:
    live = _file(_other("EVD-0003", f"first line\n{SERVER_PATH}"))

    with pytest.raises(ContentRedactionError) as caught:
        _plan(b"", live)
    assert caught.value.code == REDACTION_LEAK_OUTSIDE_CONTENT


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
