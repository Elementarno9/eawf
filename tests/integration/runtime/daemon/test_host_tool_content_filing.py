"""A host tool result is filed beside superseding content lines of another Run.

The ledger-history repair re-files content lines with an ``unreachable_shas``
declaration beside their fields, and those lines stay committed. Filing a tool result
reads the artifact ledger first, so neither that declaration nor any other Run's line
may refuse the filing; and the declared line itself still reads back as content.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record, read_collection_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.platform.lint.ledger_history import UNREACHABLE_KEY
from eawf.runtime.daemon.content_store import CONTENT_REF_PREFIX
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.host_tool import HOST_TOOL_OBSERVE_METHOD
from eawf.runtime.daemon.methods.run_content import RUN_CONTENT_READ_METHOD
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import document_path
from tests.integration.runtime.daemon.test_host_context_boundary import call, canary, ctx
from tests.integration.runtime.daemon.test_provider_permission_producer import RUN_URN, SESSION

pytestmark = pytest.mark.integration

__all__ = ["canary", "ctx"]

_OTHER_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000020"
_SHA: Final = "ab" * 20
_DECLARED_REF: Final = (
    "artifact://content/run-00000020/"
    "00000000000000000000000000000000000000000000000000000000000000aa"
)


def _declared_payload(**extra: Any) -> dict[str, Any]:
    """Return a superseding content payload of the other Run, shaped as the repair writes it."""
    return {
        "payload_kind": "content_artifact",
        "artifact_ref": _DECLARED_REF,
        "run_ref": "eawf://EAWF/run/RUN-00000020",
        "lines": ["== ./report.md", f"candidate `{_SHA}`; verdict pass."],
        "total_lines": 2,
        "withheld_lines": 0,
        "recorded_at": "2026-10-30T03:44:34.212803Z",
        UNREACHABLE_KEY: [
            {
                "sha": _SHA,
                "archive_ref": f"refs/eawf/archive/{_SHA}",
                "reason": "no commit on origin/main shares its tree or patch id",
            }
        ],
        **extra,
    }


def _file_beside(canary: CanaryProvision, payload: dict[str, Any]) -> None:
    append_ledger_record(
        ledger_path(document_path(canary), Epoch2Collection.ARTIFACT),
        LedgerRecord(
            collection=Epoch2Collection.ARTIFACT,
            record_key=payload["artifact_ref"],
            status="content_artifact",
            recorded_at=datetime(2026, 10, 31, 4, 11, 56, tzinfo=UTC),
            supersedes="sha256:" + "1" * 64,
            payload=payload,
        ),
    )


def _observe_result(ctx: MethodContext, canary: CanaryProvision) -> Any:
    return call(
        HOST_TOOL_OBSERVE_METHOD,
        ctx,
        canary,
        harness="claude-code",
        host_session_id=SESSION,
        tool_name="Bash",
        host_call_key="toolu_01bash",
        phase="result",
        output="built 3 targets\nall green",
    )


def _filed_refs(canary: CanaryProvision) -> list[str]:
    records = read_collection_records(document_path(canary), Epoch2Collection.ARTIFACT)
    return [record.record_key for record in records]


def test_a_declared_row_no_longer_refuses_tool_results(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    _file_beside(canary, _declared_payload())

    answer = _observe_result(ctx, canary)

    assert answer["status"] == "succeeded"
    assert answer["receipt_id"]
    assert answer["run_ref"] == RUN_URN
    filed = [ref for ref in _filed_refs(canary) if ref != _DECLARED_REF]
    assert len(filed) == 1
    assert filed[0].startswith(f"{CONTENT_REF_PREFIX}run-00000010/")
    own = call(
        RUN_CONTENT_READ_METHOD,
        ctx,
        canary,
        urn=RUN_URN,
        refs=[filed[0], answer["receipt_id"]],
    )
    assert own["missing"] == []
    by_ref, by_receipt = own["contents"]
    assert by_ref["lines"] == by_receipt["lines"] == ["built 3 targets", "all green"]
    declared = call(RUN_CONTENT_READ_METHOD, ctx, canary, urn=_OTHER_URN, refs=[_DECLARED_REF])
    assert declared["missing"] == []
    assert declared["contents"][0]["lines"] == _declared_payload()["lines"]


def test_another_runs_malformed_row_never_refuses_a_filing(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    _file_beside(canary, _declared_payload(raw="unfamiliar"))

    answer = _observe_result(ctx, canary)

    assert answer["status"] == "succeeded"
    assert len(_filed_refs(canary)) == 2


def test_an_own_runs_malformed_row_still_refuses_the_read(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    ref = f"{CONTENT_REF_PREFIX}run-00000010/" + "b" * 64
    _file_beside(canary, {**_declared_payload(raw="unfamiliar"), "artifact_ref": ref})

    with pytest.raises(ValidationError) as refused:
        call(RUN_CONTENT_READ_METHOD, ctx, canary, urn=RUN_URN, refs=[ref])
    assert {item["type"] for item in refused.value.errors()} == {"extra_forbidden"}
