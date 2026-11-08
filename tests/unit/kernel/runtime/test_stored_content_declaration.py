"""A superseding content line's ``unreachable_shas`` declaration never refuses the read.

The ledger-history repair re-files a content line whose text cites a commit no longer
on the integration branch, and declares beside the line's fields the archive ref that
keeps the commit alive. Such lines stay committed, so the reader drops the declaration
and still refuses every other unknown key.
"""

from __future__ import annotations

import json
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.runtime.content import StoredContent
from eawf.platform.lint.ledger_history import UNREACHABLE_KEY

_SHA: Final = "ab" * 20
_REF: Final = (
    "artifact://content/run-00000010/"
    "00000000000000000000000000000000000000000000000000000000000000aa"
)

#: A superseding artifact ledger line as the repair writes it, scrubbed to synthetic
#: values: the run, digests, commit and text are invented, the structure is kept.
_SUPERSEDING_LINE: Final = (
    '{"schema_version":"2.0","collection":"artifact",'
    f'"record_key":"{_REF}","status":"content_artifact",'
    '"recorded_at":"2026-10-31T04:11:56.319099Z",'
    '"supersedes":"sha256:1111111111111111111111111111111111111111111111111111111111111111",'
    '"payload":{"payload_kind":"content_artifact",'
    f'"artifact_ref":"{_REF}",'
    '"run_ref":"eawf://EAWF/run/RUN-00000010",'
    '"lines":["== ./report.md","Task EXMP-0001 candidate `' + _SHA + '`; verdict pass.",""],'
    '"total_lines":3,"withheld_lines":0,'
    '"recorded_at":"2026-10-30T03:44:34.212803Z",'
    '"unreachable_shas":[{"sha":"' + _SHA + '",'
    '"archive_ref":"refs/eawf/archive/' + _SHA + '",'
    '"reason":"no commit on origin/main shares its tree or patch id"}]}}'
)


def _payload() -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(_SUPERSEDING_LINE)["payload"]
    return payload


def _codes(error: ValidationError) -> set[str]:
    return {item["type"] for item in error.errors()}


def test_a_superseding_declaration_is_dropped() -> None:
    payload = _payload()
    assert payload[UNREACHABLE_KEY][0]["archive_ref"] == f"refs/eawf/archive/{_SHA}"

    content = StoredContent.model_validate(payload)

    assert content.artifact_ref == _REF
    assert content.total_lines == 3
    assert UNREACHABLE_KEY not in content.model_dump()
    assert not hasattr(content, UNREACHABLE_KEY)
    with pytest.raises(ValidationError) as refused:
        StoredContent.model_validate({**payload, "raw": "leaked"})
    assert _codes(refused.value) == {"extra_forbidden"}


def test_the_read_leaves_the_ledger_payload_whole() -> None:
    payload = _payload()

    StoredContent.model_validate(payload)

    assert payload == _payload()


def test_an_empty_declaration_is_dropped_too() -> None:
    content = StoredContent.model_validate({**_payload(), UNREACHABLE_KEY: []})

    assert UNREACHABLE_KEY not in content.model_dump()


def test_a_payload_without_the_declaration_validates_unchanged() -> None:
    payload = {key: value for key, value in _payload().items() if key != UNREACHABLE_KEY}

    assert StoredContent.model_validate(payload) == StoredContent.model_validate(_payload())


def test_only_the_exact_declaration_key_is_dropped() -> None:
    with pytest.raises(ValidationError) as refused:
        StoredContent.model_validate({**_payload(), "unreachable_sha": []})
    assert _codes(refused.value) == {"extra_forbidden"}


def test_a_declared_payload_missing_a_field_is_still_refused() -> None:
    payload = _payload()
    del payload["run_ref"]

    with pytest.raises(ValidationError) as refused:
        StoredContent.model_validate(payload)
    assert _codes(refused.value) == {"missing"}


def test_a_payload_that_is_not_a_mapping_is_refused() -> None:
    with pytest.raises(ValidationError):
        StoredContent.model_validate([UNREACHABLE_KEY])
