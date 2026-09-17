"""Native mutations pass the same leak scrub every state writer already runs.

The epoch-2 document is committed, and a transition may carry free text an
agent or an operator wrote: a criterion, a reason, a title. The scrub the
epoch-1 daemon write path uses is the one check between that text and a
public commit, so the native transaction reuses it rather than growing a
second pattern set that would drift from the first.

Where the check sits is the whole point. It runs against the proposed
document before the WAL intent is written, so a refused mutation leaves no
journalled intent for recovery to replay, no rewritten document and no
firehose row -- the text never reaches a file at all.
"""

from __future__ import annotations

import copy
import tempfile
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.store.compaction import read_document, write_document
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import (
    TransactionRefusalCode,
    TransactionRefusedError,
    TransitionRequest,
    run_transaction,
)
from eawf.runtime.daemon.wal import list_records
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    TASK_URN,
    document_path,
    firehose_path,
    provision,
    root_context,
    seed,
    seed_row,
)

ACTOR = "OP-0001"

#: A credential-shaped token, a concrete home path and a real-looking
#: address: one shape from each family the scrub declares. Each sample is
#: allowlisted from the commit-time leak gates by design -- they are the
#: fixtures that prove the write-time gate sees the same shapes.
LEAKS = [
    pytest.param(
        "/Users/mallory/work/notes.md",  # pragma: allowlist secret
        "home_path",
        id="home-path",
    ),
    pytest.param("ghp_" + "a" * 36, "token", id="github-token"),
    pytest.param(
        "mallory@leaky-corp.com",  # pragma: allowlist secret
        "email",
        id="email",
    ),
]

#: Text the scrub deliberately lets through: a documented placeholder, a
#: reserved example domain, and a tilde path that carries no username.
CLEAN = [
    pytest.param("/Users/<name>/work", id="placeholder-path"),
    pytest.param("test@example.com", id="reserved-domain"),
    pytest.param("~/.eawf/registry.json", id="tilde-path"),
]


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A canary holding one drafted Task, ready to be promoted."""
    provisioned = provision(tmp_path / "repo", code="SCRUB")
    seed(provisioned, {"task": {"EAWF-0042": seed_row("task", "DRAFT")}})
    return provisioned


@pytest.fixture
def context(canary: CanaryProvision, tmp_path: Path) -> Epoch2RootContext:
    """The native context of the canary, with a WAL directory of its own."""
    return root_context(canary, tmp_path / "runtime")


def _promotion(text: str) -> TransitionRequest:
    """Return a Task promotion whose one criterion carries *text*."""
    planned = seed_row("task", "PLANNED")
    criteria: list[dict[str, Any]] = copy.deepcopy(planned["criteria"])
    criteria[0]["text"] = text
    return TransitionRequest.model_validate(
        {
            "urn": TASK_URN,
            "to_status": "PLANNED",
            "expected_revision": 1,
            "idempotency_key": "req-promote",
            "actor": ACTOR,
            "updates": {
                "batch_ref": planned["batch_ref"],
                "due_scope": planned["due_scope"],
                "criteria": criteria,
            },
        }
    )


@pytest.mark.parametrize(("text", "kind"), LEAKS)
def test_leaking_free_text_is_refused_before_the_wal_intent(
    context: Epoch2RootContext, canary: CanaryProvision, text: str, kind: str
) -> None:
    before = document_path(canary).read_bytes()

    with pytest.raises(TransactionRefusedError) as caught:
        run_transaction(context=context, request=_promotion(text), now=AT)

    assert caught.value.code is TransactionRefusalCode.SCHEMA_VALIDATION_FAILED
    assert caught.value.detail.startswith("state_leak_refused: ")
    assert kind in caught.value.detail
    assert list_records(context.wal_dir) == []
    assert document_path(canary).read_bytes() == before
    assert not firehose_path(canary).exists()


def test_the_refusal_names_the_field_and_not_the_text(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    """The refusal travels to terminals and logs, so it never repeats the leak."""
    token = "ghp_" + "b" * 36

    with pytest.raises(TransactionRefusedError) as caught:
        run_transaction(context=context, request=_promotion(token), now=AT)

    assert "task.EAWF-0042.criteria[0].text" in caught.value.detail
    assert token not in str(caught.value)


@pytest.mark.parametrize("text", CLEAN)
def test_clean_free_text_commits(
    context: Epoch2RootContext, canary: CanaryProvision, text: str
) -> None:
    committed = run_transaction(context=context, request=_promotion(text), now=AT)

    assert committed.receipt.event_name == "domain.task.promoted"
    row = read_document(document_path(canary))["task"]["EAWF-0042"]
    assert (row["status"], row["revision"]) == ("PLANNED", 2)
    assert row["criteria"][0]["text"] == text
    assert len(list_records(context.wal_dir)) == 1


def test_text_already_in_the_document_is_not_re_flagged(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    """The scrub diffs the write; a leak that predates it is somebody else's."""
    path = document_path(canary)
    document = read_document(path)
    older = "/Users/mallory/older/leak.md"  # pragma: allowlist secret
    document["legacy"] = {"LEG-0001": {"note": older}}
    write_document(path, document)

    committed = run_transaction(context=context, request=_promotion("a clean criterion"), now=AT)

    assert committed.receipt.revision_after == 2
    assert len(list_records(context.wal_dir)) == 1
