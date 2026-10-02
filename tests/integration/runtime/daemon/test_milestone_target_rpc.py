"""Setting a Milestone's target date commits once, or refuses and writes nothing.

The verb moves no status, so it is driven against every open status and
refused against both closed ones; the refusals are checked to leave the
document, the firehose and the WAL exactly as they were.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.projection.compute import patches_for_event
from eawf.kernel.store.compaction import read_document
from eawf.kernel.store.envelope import Envelope
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods.domain_envelope import DomainErrorCode
from eawf.runtime.daemon.methods.milestone_target import MILESTONE_SET_TARGET, TARGET_SET_EVENT
from eawf.runtime.daemon.wal import list_records
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    MILESTONE_URN,
    TASK_URN,
    document_path,
    firehose_path,
    method_context,
    provision,
    seed,
    seed_row,
    tree_root,
)

ACTOR = "OP-0001"
KEY = "req-target-0001"


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


def _canary(tmp_path: Path, status: str | None, **overrides: Any) -> CanaryProvision:
    """Provision a canary holding MLS-0030 at *status*, or no Milestone for ``None``."""
    canary = provision(tmp_path / "tree", code="TGT")
    if status is not None:
        row = seed_row("milestone", status)
        row.update(overrides)
        seed(canary, {"milestone": {"MLS-0030": row}})
    return canary


def _drive(
    canary: CanaryProvision,
    tmp_path: Path,
    *,
    bus: EventBus | None = None,
    **params: Any,
) -> dict[str, Any]:
    """Send the verb against *canary* and return its envelope."""
    ctx = method_context(tmp_path / "runtime")
    ctx.bus = bus
    request: dict[str, Any] = {
        "repo_root": str(canary.root),
        "urn": MILESTONE_URN,
        "expected_revision": 1,
        "idempotency_key": KEY,
        "actor": ACTOR,
        "target_date": "2026-10-09",
        **params,
    }
    return asyncio.run(methods.dispatch(MILESTONE_SET_TARGET, ctx, request))


def _stored(canary: CanaryProvision) -> dict[str, Any]:
    """Return the Milestone row the canary's document holds."""
    row = read_document(document_path(canary))["milestone"]["MLS-0030"]
    assert isinstance(row, dict)
    return row


def _firehose(canary: CanaryProvision) -> list[dict[str, Any]]:
    path = firehose_path(canary)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _wal(canary: CanaryProvision, tmp_path: Path) -> list[Path]:
    context = method_context(tmp_path / "runtime").native_root_context(tree_root(canary))
    return list(list_records(context.wal_dir))


@pytest.mark.parametrize("status", ["PLANNED", "ACTIVE", "ACCEPTANCE_REVIEW"])
def test_set_target_dates_an_open_milestone_and_leaves_its_status(
    status: str, tmp_path: Path
) -> None:
    canary = _canary(tmp_path, status)

    answer = _drive(canary, tmp_path)

    assert answer["status"] == "ok", answer["errors"]
    assert (answer["revision_before"], answer["revision_after"]) == (1, 2)
    assert answer["result"]["event_name"] == TARGET_SET_EVENT
    row = _stored(canary)
    assert (row["target_date"], row["status"], row["revision"]) == ("2026-10-09", status, 2)
    [event] = _firehose(canary)
    payload = event["payload"]
    assert payload["name"] == TARGET_SET_EVENT
    assert (payload["from_status"], payload["to_status"]) == (status, status)
    assert payload["target_date"] == "2026-10-09"
    assert len(_wal(canary, tmp_path)) == 1


def test_set_target_clears_a_stated_date(tmp_path: Path) -> None:
    canary = _canary(tmp_path, "ACTIVE", target_date="2026-10-05")

    answer = _drive(canary, tmp_path, target_date=None)

    assert answer["status"] == "ok", answer["errors"]
    assert _stored(canary)["target_date"] is None
    assert _firehose(canary)[0]["payload"]["target_date"] is None


def test_set_target_event_patches_the_milestone_routes(tmp_path: Path) -> None:
    canary = _canary(tmp_path, "PLANNED")
    bus = EventBus()
    published: list[Envelope] = []
    bus.publish = published.append  # type: ignore[method-assign]

    _drive(canary, tmp_path, bus=bus)

    [envelope] = published
    patches = patches_for_event(envelope)
    assert patches, "a console holding the roadmap hears the new revision"
    entry = patches[0].entries[0]
    assert (entry.key, entry.revision, entry.status) == ("MLS-0030", 2, "PLANNED")


@pytest.mark.parametrize("status", ["COMPLETED", "CANCELLED"])
def test_set_target_refuses_a_closed_milestone_and_writes_nothing(
    status: str, tmp_path: Path
) -> None:
    canary = _canary(tmp_path, status)
    before = document_path(canary).read_bytes()

    answer = _drive(canary, tmp_path)

    assert answer["status"] == "error"
    assert answer["errors"][0]["code"] == DomainErrorCode.ILLEGAL_TRANSITION.value
    assert answer["revision_before"] == answer["revision_after"] == 1
    assert document_path(canary).read_bytes() == before
    assert _firehose(canary) == []
    assert _wal(canary, tmp_path) == []


def test_set_target_refuses_a_stale_revision(tmp_path: Path) -> None:
    """Off by one against the stored revision is a conflict, never an overwrite."""
    canary = _canary(tmp_path, "PLANNED")
    before = document_path(canary).read_bytes()

    answer = _drive(canary, tmp_path, expected_revision=2)

    assert answer["errors"][0]["code"] == DomainErrorCode.REVISION_CONFLICT.value
    assert answer["revision_before"] == 1
    assert document_path(canary).read_bytes() == before


@pytest.mark.parametrize("value", ["2026-02-30", "soon", 20261009])
def test_set_target_refuses_a_value_that_names_no_day(value: Any, tmp_path: Path) -> None:
    canary = _canary(tmp_path, "PLANNED")
    before = document_path(canary).read_bytes()

    answer = _drive(canary, tmp_path, target_date=value)

    row = answer["errors"][0]
    assert row["code"] == DomainErrorCode.SCHEMA_VALIDATION_FAILED.value
    assert "target_date" in row["message"]
    assert document_path(canary).read_bytes() == before


def test_set_target_refuses_a_request_that_names_no_date(tmp_path: Path) -> None:
    """An absent date is refused, so a forgotten argument never clears one."""
    canary = _canary(tmp_path, "PLANNED", target_date="2026-10-05")
    ctx = method_context(tmp_path / "runtime")

    answer = asyncio.run(
        methods.dispatch(
            MILESTONE_SET_TARGET,
            ctx,
            {
                "repo_root": str(canary.root),
                "urn": MILESTONE_URN,
                "expected_revision": 1,
                "idempotency_key": KEY,
                "actor": ACTOR,
            },
        )
    )

    assert answer["errors"][0]["code"] == DomainErrorCode.SCHEMA_VALIDATION_FAILED.value
    assert _stored(canary)["target_date"] == "2026-10-05"


def test_set_target_retry_replays_and_other_parameters_conflict(tmp_path: Path) -> None:
    canary = _canary(tmp_path, "PLANNED")

    first = _drive(canary, tmp_path)
    again = _drive(canary, tmp_path)
    other = _drive(canary, tmp_path, target_date="2026-10-10")

    assert again["status"] == "ok" and again["result"] == first["result"]
    assert other["errors"][0]["code"] == DomainErrorCode.IDEMPOTENCY_CONFLICT.value
    assert len(_firehose(canary)) == 1
    assert _stored(canary)["target_date"] == "2026-10-09"


def test_set_target_refuses_a_urn_addressing_another_entity(tmp_path: Path) -> None:
    canary = _canary(tmp_path, "PLANNED")

    answer = _drive(canary, tmp_path, urn=TASK_URN)

    assert answer["errors"][0]["code"] == DomainErrorCode.IDENTITY_KIND_MISMATCH.value
    assert _firehose(canary) == []


def test_set_target_refuses_an_absent_milestone(tmp_path: Path) -> None:
    canary = _canary(tmp_path, None)

    answer = _drive(canary, tmp_path)

    assert answer["errors"][0]["code"] == DomainErrorCode.IDENTITY_NOT_FOUND.value
    assert answer["revision_before"] is None
