"""``release.advance_train`` walks the train from the stores, or changes nothing.

The verb used to take the record and its receipts from the caller and
return a DRAFT it never persisted, so the train could not move and
nothing the caller handed over was checked against what was recorded.
These tests pin the stored-record contract instead:

* the record and its newest receipts are read from the stores, and the
  advance is recorded with the validated receipt refs;
* a missing or expired stored receipt refuses ``prerequisite_receipt_stale``
  and a non-terminal record refuses ``checkpoint_not_terminal``, and
  neither refusal writes a row;
* no DRAFT is opened, so the next rung has no record until
  ``release create`` admits one;
* the train it judges against is the one the stores place: burned dev1
  plus baked dev2 has dev2 open, and dev2 cannot be advanced twice.

The CLI case drives ``eawf release advance`` through the real handler, so
the argv an operator types is proven to reach a stored record.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from eawf.kernel.spec.release import Release, ReleaseChannel, ReleaseStatus
from eawf.kernel.spec.release_config import ReleaseConfig, load_release_config
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.release import show
from eawf.runtime.daemon.methods.release_receipts import advance
from eawf.surfaces.cli.app import app
from eawf.workflow.release.adoption import adopt_publication
from eawf.workflow.release.advance import CheckpointGateReceipt
from eawf.workflow.release.publication import burn_release
from eawf.workflow.release.records import read_release_records, record_release
from eawf.workflow.release.train import DEV2_RELEASE_CONFIG_YAML, V07_TRAIN
from eawf.workflow.release.train_store import (
    read_train_advances,
    record_checkpoint_receipt,
    train_advances_path,
)
from tests._release_helpers import dev1_adoption, dev1_config, dev1_draft

pytestmark = pytest.mark.integration

DEV1_KEY = "REL-0.7.0.dev1"
DEV2_KEY = "REL-0.7.0.dev2"
DEV3_KEY = "REL-0.7.0.dev3"
SOURCE_SHA = "a" * 40
TREE_SHA = "b" * 40
MANIFEST_DIGEST = f"sha256:{'c' * 64}"

#: The revision the baked dev2 record is stored at.
DEV2_REVISION = 9

DEV2_ADVANCE = {"release_key": DEV2_KEY}


def dev2_config() -> ReleaseConfig:
    """Return the rendered dev2 checkpoint configuration."""
    return load_release_config(DEV2_RELEASE_CONFIG_YAML, train=V07_TRAIN)


#: The twelve gates dev2 requires, in configuration order.
DEV2_GATES = dev2_config().gates.required


def dev2_record(status: ReleaseStatus = ReleaseStatus.BAKED) -> Release:
    """Return the pinned dev2 record at *status*."""
    return Release(
        uid=UUID(int=52),
        key=DEV2_KEY,
        version="0.7.0.dev2",
        channel=ReleaseChannel.DEV,
        authority_epoch=1,
        status=status,
        approval_ref="receipt://approval/dev2",
        source_sha=SOURCE_SHA,
        source_tree_sha=TREE_SHA,
        manifest_ref="artifact://release/dev2-manifest",
        manifest_digest=MANIFEST_DIGEST,
        revision=DEV2_REVISION,
    )


def dev2_receipt(gate_index: int, **overrides: Any) -> CheckpointGateReceipt:
    """Return a receipt for one dev2 gate, fresh now unless overridden."""
    now = datetime.now(UTC)
    gate = DEV2_GATES[gate_index]
    payload: dict[str, Any] = {
        "gate": gate,
        "release_key": DEV2_KEY,
        "source_sha": SOURCE_SHA,
        "manifest_digest": MANIFEST_DIGEST,
        "issued_at": now - timedelta(hours=1),
        "expires_at": now + timedelta(hours=1),
        "receipt_ref": f"checkpoint-receipt://{DEV2_KEY}/{gate.value}/fresh",
    }
    payload.update(overrides)
    return CheckpointGateReceipt.model_validate(payload)


def stage(
    ctx: MethodContext,
    *,
    status: ReleaseStatus = ReleaseStatus.BAKED,
    receipts: list[CheckpointGateReceipt] | None = None,
) -> Path:
    """Store burned dev1, dev2 at *status* and dev2's receipts under *ctx*.

    Args:
        ctx: Context whose state root the stores live under.
        status: The status dev2 is stored at.
        receipts: The dev2 receipts to store; one fresh receipt per gate
            when omitted.

    Returns:
        The state path.
    """
    state_path = Path(str(ctx.state_path))
    adopted = adopt_publication(dev1_draft(), dev1_config(), adoption=dev1_adoption())
    burned, _operation = burn_release(adopted, dev1_config(), None)
    now = datetime.now(UTC)
    for record in (burned, dev2_record(status)):
        record_release(state_path, record, recorded_at=now, summary=f"seed {record.key}")
    rows = (
        [dev2_receipt(index) for index in range(len(DEV2_GATES))] if receipts is None else receipts
    )
    for receipt in rows:
        record_checkpoint_receipt(state_path, receipt, recorded_at=now, summary="seed")
    return state_path


def refused_advance(ctx: MethodContext) -> str:
    """Return the refusal message of a dev2 advance that must not succeed."""
    with pytest.raises(DaemonValidationError) as excinfo:
        asyncio.run(advance(ctx, DEV2_ADVANCE))
    return str(excinfo.value)


def open_index(ctx: MethodContext) -> int:
    """Return the open index ``release.show`` reports."""
    index: int = asyncio.run(show(ctx, {}))["current_checkpoint_index"]
    return index


# --- the stored-receipt success ----------------------------------------------


def test_advance_walks_past_a_baked_rung_on_its_stored_receipts(ctx: MethodContext) -> None:
    """dev2 baked with twelve fresh stored receipts moves the train onto dev3."""
    stage(ctx)
    assert open_index(ctx) == 1

    result = asyncio.run(advance(ctx, DEV2_ADVANCE))

    assert result["train"]["current_checkpoint_index"] == 2
    assert result["train"]["current_checkpoint"] == DEV3_KEY
    assert result["advance"]["closed_key"] == DEV2_KEY
    assert result["advance"]["opened_key"] == DEV3_KEY
    assert open_index(ctx) == 2


def test_advance_persists_the_validated_receipt_refs(ctx: MethodContext) -> None:
    """The recorded advance carries every stored ref, in configuration order."""
    state_path = stage(ctx)

    result = asyncio.run(advance(ctx, DEV2_ADVANCE))

    expected = [f"checkpoint-receipt://{DEV2_KEY}/{gate.value}/fresh" for gate in DEV2_GATES]
    (recorded,) = read_train_advances(state_path)
    assert result["receipt_refs"] == expected
    assert list(recorded.receipt_refs) == expected
    assert recorded.closed_revision == DEV2_REVISION
    assert result["advance_record_id"] == f"{V07_TRAIN.train_id}:{DEV2_KEY}"


def test_advance_echoes_the_stored_record_unchanged(ctx: MethodContext) -> None:
    """The closed record is the stored one, and the store still holds it as it was."""
    state_path = stage(ctx)

    result = asyncio.run(advance(ctx, DEV2_ADVANCE))

    assert result["closed"] == dev2_record().model_dump(mode="json")
    assert read_release_records(state_path)[DEV2_KEY] == dev2_record()


def test_advance_opens_no_draft_for_the_next_rung(ctx: MethodContext) -> None:
    """dev3 has no record after the advance; only release create opens one."""
    state_path = stage(ctx)

    result = asyncio.run(advance(ctx, DEV2_ADVANCE))

    assert "opened" not in result
    assert DEV3_KEY not in read_release_records(state_path)
    assert asyncio.run(show(ctx, {}))["record"] is None


def test_advance_reads_the_newest_receipt_of_each_gate(ctx: MethodContext) -> None:
    """An expired receipt stored before a fresh one does not block the move."""
    now = datetime.now(UTC)
    expired = dev2_receipt(
        0,
        issued_at=now - timedelta(days=3),
        expires_at=now - timedelta(days=2),
        receipt_ref="checkpoint-receipt://old",
    )
    fresh = [dev2_receipt(index) for index in range(len(DEV2_GATES))]
    stage(ctx, receipts=[expired, *fresh])

    result = asyncio.run(advance(ctx, DEV2_ADVANCE))

    assert "checkpoint-receipt://old" not in result["receipt_refs"]


# --- the refusals ------------------------------------------------------------


def test_advance_refuses_an_expired_stored_receipt_and_writes_nothing(ctx: MethodContext) -> None:
    """The newest receipt of one gate has expired: stale, and no advance row."""
    now = datetime.now(UTC)
    rows = [dev2_receipt(index) for index in range(len(DEV2_GATES))]
    rows[4] = dev2_receipt(4, issued_at=now - timedelta(days=2), expires_at=now - timedelta(days=1))
    state_path = stage(ctx, receipts=rows)

    message = refused_advance(ctx)

    assert "prerequisite_receipt_stale" in message
    assert DEV2_GATES[4].value in message
    assert "eawf release receipts 0.7.0.dev2" in message
    assert not train_advances_path(state_path).exists()
    assert open_index(ctx) == 1


def test_advance_refuses_a_missing_stored_receipt_as_stale(ctx: MethodContext) -> None:
    """A gate with no stored receipt at all answers the same wire code."""
    rows = [dev2_receipt(index) for index in range(len(DEV2_GATES) - 1)]
    state_path = stage(ctx, receipts=rows)

    message = refused_advance(ctx)

    assert message.startswith("validation_failed: prerequisite_receipt_stale")
    assert DEV2_GATES[-1].value in message
    assert not train_advances_path(state_path).exists()


def test_advance_refuses_an_empty_receipt_store(ctx: MethodContext) -> None:
    """A checkpoint nobody ran the producer for cannot advance."""
    stage(ctx, receipts=[])

    assert "prerequisite_receipt_stale" in refused_advance(ctx)


def test_advance_refuses_a_receipt_bound_to_other_source(ctx: MethodContext) -> None:
    """A stored receipt earned on another commit is stale for this record."""
    rows = [dev2_receipt(index) for index in range(len(DEV2_GATES))]
    rows[1] = dev2_receipt(1, source_sha="d" * 40)
    stage(ctx, receipts=rows)

    message = refused_advance(ctx)

    assert "prerequisite_receipt_stale" in message
    assert "source_sha" in message


@pytest.mark.parametrize(
    "status",
    [ReleaseStatus.APPROVED, ReleaseStatus.PUBLISHING, ReleaseStatus.VERIFYING],
)
def test_advance_refuses_a_non_terminal_record_and_writes_nothing(
    ctx: MethodContext, status: ReleaseStatus
) -> None:
    """A dev2 that has not finished refuses checkpoint_not_terminal."""
    state_path = stage(ctx, status=status)

    message = refused_advance(ctx)

    assert "checkpoint_not_terminal" in message
    assert status.value in message
    assert not train_advances_path(state_path).exists()
    assert open_index(ctx) == 1


def test_advance_refuses_a_rung_the_train_has_moved_past(ctx: MethodContext) -> None:
    """dev1 is burned and dev2 is open, so dev1 is not the rung to advance."""
    stage(ctx)

    with pytest.raises(DaemonValidationError, match="checkpoint_key_mismatch"):
        asyncio.run(advance(ctx, {"release_key": DEV1_KEY}))


def test_advance_refuses_a_second_advance_of_the_same_rung(ctx: MethodContext) -> None:
    """The first advance moved the train, so a repeat names the rung now open."""
    state_path = stage(ctx)
    asyncio.run(advance(ctx, DEV2_ADVANCE))

    message = refused_advance(ctx)

    assert "checkpoint_key_mismatch" in message
    assert DEV3_KEY in message
    assert len(read_train_advances(state_path)) == 1


def test_advance_refuses_a_key_with_no_stored_record(ctx: MethodContext) -> None:
    """Nothing stored for the key is a refusal, not a vacuous advance."""
    with pytest.raises(DaemonValidationError, match="no release record is stored"):
        asyncio.run(advance(ctx, DEV2_ADVANCE))


def test_advance_refuses_a_corrupt_receipt_store(ctx: MethodContext) -> None:
    """An unreadable receipt row refuses the advance instead of skipping a gate."""
    state_path = stage(ctx)
    path = state_path.parent / "store" / "release_checkpoint_receipt.jsonl"
    path.write_text("not json\n", encoding="utf-8")

    with pytest.raises(DaemonValidationError, match="is not an envelope"):
        asyncio.run(advance(ctx, DEV2_ADVANCE))


def test_advance_refuses_without_a_state_root() -> None:
    """A daemon with nowhere to read or record the move refuses it."""
    rootless = MethodContext(
        started_at="2026-09-17T00:00:00+00:00", pid=1, protocol_version="1", version="test"
    )

    with pytest.raises(DaemonValidationError, match="on-disk state root"):
        asyncio.run(advance(rootless, DEV2_ADVANCE))


def test_advance_rejects_a_malformed_release_key(ctx: MethodContext) -> None:
    """A key that is not ``REL-<version>`` fails the params model."""
    with pytest.raises(ValidationError):
        asyncio.run(advance(ctx, {"release_key": "dev2"}))


def test_advance_rejects_a_record_payload_param(ctx: MethodContext) -> None:
    """The caller can no longer hand over a record; the params forbid it."""
    with pytest.raises(ValidationError):
        asyncio.run(
            advance(ctx, {**DEV2_ADVANCE, "release": dev2_record().model_dump(mode="json")})
        )


# --- the CLI verb ------------------------------------------------------------


class _HandlerClient:
    """A ``DaemonClient`` stand-in that runs the real advance handler."""

    def __init__(self, ctx: MethodContext, calls: list[dict[str, Any]]) -> None:
        self._ctx = ctx
        self._calls = calls

    def __enter__(self) -> _HandlerClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Record the call and answer it from the handler."""
        self._calls.append({"method": method, "params": params})
        result: dict[str, Any] = asyncio.run(advance(self._ctx, params))
        return result


def test_release_advance_cli_sends_only_the_key_and_prints_the_next_step(
    ctx: MethodContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator names the checkpoint; the reply points at release create."""
    stage(ctx)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "eawf.surfaces.cli._daemon_client.DaemonClient",
        lambda *args, **kwargs: _HandlerClient(ctx, calls),
    )

    result = CliRunner().invoke(app, ["release", "advance", DEV2_KEY])

    assert result.exit_code == 0, result.output
    assert calls == [{"method": "release.advance_train", "params": {"release_key": DEV2_KEY}}]
    assert f"advanced past {DEV2_KEY} (baked) -> {DEV3_KEY} open" in result.output
    assert "next: eawf release create 0.7.0.dev3" in result.output


def test_release_advance_cli_emits_the_reply_as_json(
    ctx: MethodContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--json`` carries the whole reply, the recorded advance included."""
    stage(ctx)
    monkeypatch.setattr(
        "eawf.surfaces.cli._daemon_client.DaemonClient",
        lambda *args, **kwargs: _HandlerClient(ctx, []),
    )

    result = CliRunner().invoke(app, ["--json", "release", "advance", DEV2_KEY])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["advance"]["opened_key"] == DEV3_KEY


def test_release_advance_cli_takes_no_record_file() -> None:
    """The stored record is the only record; the old --release option is gone."""
    result = CliRunner().invoke(app, ["release", "advance", DEV2_KEY, "--release", "x.json"])

    assert result.exit_code != 0
    assert "No such option" in result.output
