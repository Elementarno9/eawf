"""The ladder guard reached through the two surfaces operators use.

The unit tier pins the advance guard itself; this module pins that the
guard is actually wired -- that ``eawf release train show --json`` emits
the ladder an operator reads before advancing, and that
``release.advance_train`` refuses on the wire with the same named denial
the library raises rather than silently succeeding at the RPC boundary.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from typer.testing import CliRunner

from eawf.kernel.spec.release import Release, ReleaseChannel, ReleaseStatus
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.release import advance
from eawf.surfaces.cli.app import app
from eawf.workflow.release.train import DEV1_RELEASE_CONFIG_YAML

pytestmark = pytest.mark.integration

runner = CliRunner()

#: A pinned dev1 build: its commit, tree and approved manifest digest.
SOURCE_SHA = "a" * 40
TREE_SHA = "b" * 40
MANIFEST_DIGEST = f"sha256:{'c' * 64}"

#: The eight gate names the authored dev1 configuration requires, read
#: off the shipped YAML so a change to the checkpoint reds this module
#: instead of leaving it agreeing with a copy.
DEV1_GATE_NAMES = tuple(
    line.strip().removeprefix("- ")
    for line in DEV1_RELEASE_CONFIG_YAML.splitlines()
    if line.startswith("      - ") and "target_id" not in line
)

#: Context the advance RPC runs under; the verb is a pure projection, so
#: it needs no state root.
CTX = MethodContext(
    started_at="2026-09-04T00:00:00+00:00",
    pid=4321,
    protocol_version="1",
    version="0.7.0.dev1",
)

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def dev1_record(status: ReleaseStatus = ReleaseStatus.BAKED) -> dict[str, Any]:
    """Return the serialized dev1 record at *status*."""
    return Release(
        uid=UUID(int=31),
        key="REL-0.7.0.dev1",
        version="0.7.0.dev1",
        channel=ReleaseChannel.DEV,
        authority_epoch=1,
        status=status,
        source_sha=SOURCE_SHA,
        source_tree_sha=TREE_SHA,
        manifest_ref="artifact://release/manifest",
        manifest_digest=MANIFEST_DIGEST,
        approval_ref="receipt://approval/dev1",
    ).model_dump(mode="json")


def receipts(**overrides: str) -> list[dict[str, Any]]:
    """Return one fresh receipt per required dev1 gate."""
    return [
        {
            "gate": gate,
            "release_key": "REL-0.7.0.dev1",
            "source_sha": SOURCE_SHA,
            "manifest_digest": MANIFEST_DIGEST,
            "issued_at": (NOW - timedelta(days=1)).isoformat(),
            "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            "receipt_ref": f"receipt://gate/{gate}",
            **overrides,
        }
        for gate in DEV1_GATE_NAMES
    ]


# --- eawf release train show -------------------------------------------------


def test_the_authored_gate_names_are_the_eight_dev1_gates() -> None:
    """The receipt builder reads the shipped configuration, not a copy."""
    assert len(DEV1_GATE_NAMES) == 8
    assert DEV1_GATE_NAMES[0] == "version_consistency"


def test_train_show_json_emits_the_ladder_index_and_statuses() -> None:
    """The command-local --json flag renders the machine-readable ladder."""
    result = runner.invoke(app, ["release", "train", "show", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["train_id"] == "TRAIN-0.7.0"
    assert payload["current_checkpoint_index"] == 0
    assert [row["status"] for row in payload["checkpoints"]] == ["open", *["pending"] * 6]


def test_train_show_honours_the_global_json_flag() -> None:
    """``eawf --json release train show`` renders the same body."""
    local = runner.invoke(app, ["release", "train", "show", "--json"])
    globally = runner.invoke(app, ["--json", "release", "train", "show"])

    assert globally.exit_code == 0, globally.output
    assert json.loads(globally.stdout) == json.loads(local.stdout)


def test_train_show_text_marks_the_open_rung() -> None:
    """The default rendering points at the checkpoint currently open."""
    result = runner.invoke(app, ["release", "train", "show"])

    assert result.exit_code == 0, result.output
    assert "* REL-0.7.0.dev1  status=open" in result.stdout
    assert "REL-0.7.0.dev2  status=pending" in result.stdout


def test_train_without_a_subcommand_lists_show() -> None:
    """The group refuses to guess and names its verb."""
    result = runner.invoke(app, ["release", "train"])

    assert "show" in result.output


# --- release.advance_train ---------------------------------------------------


def test_advance_train_rpc_opens_the_next_rung() -> None:
    """A baked dev1 with fresh receipts moves the ladder onto dev2."""
    result = asyncio.run(advance(CTX, {"release": dev1_record(), "receipts": receipts()}))

    assert result["train"]["current_checkpoint_index"] == 1
    assert result["opened"]["key"] == "REL-0.7.0.dev2"
    assert result["opened"]["status"] == ReleaseStatus.DRAFT.value
    assert len(result["receipt_refs"]) == len(DEV1_GATE_NAMES)


def test_advance_train_rpc_returns_the_closing_record_unchanged() -> None:
    """The record that closed is echoed back exactly as it was offered."""
    offered = dev1_record()

    result = asyncio.run(advance(CTX, {"release": offered, "receipts": receipts()}))

    assert result["closed"] == offered


def test_advance_train_rpc_refuses_a_checkpoint_still_verifying() -> None:
    """The named denial survives the RPC boundary."""
    with pytest.raises(DaemonValidationError, match="checkpoint_not_terminal"):
        asyncio.run(
            advance(
                CTX,
                {"release": dev1_record(ReleaseStatus.VERIFYING), "receipts": receipts()},
            )
        )


def test_advance_train_rpc_refuses_a_receipt_bound_to_other_source() -> None:
    """A receipt earned on a different build refuses, naming its gate."""
    stale = receipts()
    stale[2]["source_sha"] = "d" * 40

    with pytest.raises(DaemonValidationError) as excinfo:
        asyncio.run(advance(CTX, {"release": dev1_record(), "receipts": stale}))

    assert "prerequisite_receipt_stale" in str(excinfo.value)
    assert DEV1_GATE_NAMES[2] in str(excinfo.value)


def test_advance_train_rpc_refuses_when_a_gate_carries_no_receipt() -> None:
    """The empty offering fails closed rather than advancing vacuously."""
    with pytest.raises(DaemonValidationError, match="prerequisite_receipt_missing"):
        asyncio.run(advance(CTX, {"release": dev1_record(), "receipts": []}))
