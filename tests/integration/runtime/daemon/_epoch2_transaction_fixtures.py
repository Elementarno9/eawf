"""Scaffolding the epoch-2 transaction integration tests share.

Three suites drive the same transaction against the same kind of tree: a
freshly provisioned canary whose generation document has been seeded with
one or more records. Building that by hand in each file would let the
three drift, so the canary, the seed loader and the daemon context live
here once.

Records are taken from the transition registry's own seed fixture rather
than written again, so a record the reducer accepts is the same record the
transaction is handed.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import yaml

from eawf.kernel.delivery.acceptance import MilestoneAcceptanceBundle
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.epoch2.authority import require_native_authority
from eawf.kernel.store.compaction import read_document, write_document
from eawf.kernel.store.paths import store_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision, canary_ref, provision_canary
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.methods import MethodContext

#: The transition registry's seed records, four levels up lands on ``tests/``.
SEED_RECORDS: Final = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "epoch2"
    / "transitions"
    / "seed_records.yaml"
)

#: When every transaction under test is stamped. The transaction takes the
#: time from its caller, so a fixed stamp keeps a run reproducible.
AT: Final = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)

#: The workspace, project and repository slots the seed records are spelled
#: under. The canary's own keys differ, which is exactly the point: the
#: transaction locks and orders by the URN it is handed.
MILESTONE_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/milestone/MLS-0030"
BATCH_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/batch/BAT-0007"
TASK_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0042"

#: The container slot every seeded URN is spelled under.
_CONTAINER: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"

#: The sealed approval an acceptance is taken on.
APPROVAL_URN: Final = f"{_CONTAINER}/pending-action/ACT-0001"


def seed_row(entity: str, status: str) -> dict[str, Any]:
    """Return one seeded record payload, ready to place in a document.

    Args:
        entity: The entity token, such as ``milestone``.
        status: The status the record sits in.

    Returns:
        A deep copy of the seed payload, so a caller may edit it freely.

    Raises:
        KeyError: No seed is declared for that entity or status.
    """
    document = yaml.safe_load(SEED_RECORDS.read_text(encoding="utf-8"))
    return copy.deepcopy(document["records"][entity][status])


def legacy_task_row() -> dict[str, Any]:
    """Return the seeded Task projected from epoch 1."""
    document = yaml.safe_load(SEED_RECORDS.read_text(encoding="utf-8"))
    return copy.deepcopy(document["legacy_origin"]["task"])


def rekeyed(row: dict[str, Any], *, key: str) -> dict[str, Any]:
    """Return *row* re-spelled under *key*, URN included.

    Args:
        row: A seed payload.
        key: The public key the copy should carry.

    Returns:
        The copy. Its URN keeps every slot but the entity key, which the
        record model requires to agree with the key field.
    """
    copied = copy.deepcopy(row)
    copied["key"] = key
    copied["urn"] = f"{str(row['urn']).rsplit('/', 1)[0]}/{key}"
    return copied


def provision(repo_root: Path, *, code: str = "TXN") -> CanaryProvision:
    """Provision a disposable epoch-2 canary at *repo_root*."""
    return provision_canary(repo_root=repo_root, ref=canary_ref(code), provisioned_at=AT)


def tree_root(provisioned: CanaryProvision) -> Path:
    """Return the fenced ``.ea`` tree of a provisioned canary."""
    return provisioned.root / ".ea"


def document_path(provisioned: CanaryProvision) -> Path:
    """Return the selected generation's document of a provisioned canary."""
    authority = require_native_authority(tree_root(provisioned))
    assert authority.target is not None
    assert authority.generation_id is not None
    return authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT


def firehose_path(provisioned: CanaryProvision) -> Path:
    """Return the canary's firehose, which keeps its epoch-1 location."""
    return store_path(tree_root(provisioned) / "state.json", StoreKind.EVENT)


def seed(provisioned: CanaryProvision, rows: dict[str, dict[str, Any]]) -> None:
    """Place *rows* into the canary's document, keyed by collection.

    Args:
        provisioned: The canary to seed.
        rows: ``{collection: {record_key: payload}}``.
    """
    path = document_path(provisioned)
    document = read_document(path)
    for collection, members in rows.items():
        document.setdefault(collection, {}).update(members)
    write_document(path, document)


def method_context(runtime_root: Path) -> MethodContext:
    """Return a daemon context with a WAL directory of its own."""
    return MethodContext(
        started_at=AT.isoformat(),
        pid=1,
        protocol_version="1",
        version="test",
        wal_dir=runtime_root / "wal",
    )


def root_context(provisioned: CanaryProvision, runtime_root: Path) -> Epoch2RootContext:
    """Return the native context of a provisioned canary."""
    return method_context(runtime_root).native_root_context(tree_root(provisioned))


def acceptance_bundle_row() -> dict[str, Any]:
    """Return revision one of the seeded Milestone's acceptance bundle.

    The journey mirrors the seed record's single step, and the binding is
    the one a completed Milestone carries, so the bundle describes the
    same acceptance the transaction would commit.
    """
    return MilestoneAcceptanceBundle.model_validate(
        {
            "milestone_ref": MILESTONE_URN,
            "revision": 1,
            "accepted_binding": seed_row("milestone", "COMPLETED")["accepted_binding"],
            "steps": [
                {
                    "step_id": "AS-01",
                    "passed": True,
                    "observation": "the install completed and reported the version",
                    "evidence_kinds": ["artifact"],
                    "evidence_refs": [f"{_CONTAINER}/evidence/EVD-0002"],
                }
            ],
            "sealed_at": AT.isoformat(),
        }
    ).model_dump(mode="json")


def sealed_approval_row() -> dict[str, Any]:
    """Return the sealed protected approval an acceptance is taken on.

    The resolver is a person because the record admits nothing else, and
    the recorded digest is the bundle's own, so a request presenting any
    other bundle is refused rather than accepted.
    """
    return {
        "id": "ACT-0001",
        "kind": "protected_approval",
        "subject_ref": MILESTONE_URN,
        "question": "Accept MLS-0030 on the bundle you just read?",
        "bundle_digest": MilestoneAcceptanceBundle.model_validate(acceptance_bundle_row()).digest(),
        "options": [
            {"option_id": "approve", "label": "Accept the Milestone", "effect": "approve"},
            {"option_id": "decline", "label": "Do not accept it", "effect": "decline"},
        ],
        "idempotency_key": "req-accept-0001",
        "status": "SEALED",
        "requested_by": {"principal_kind": "human", "principal_id": "OP-0001"},
        "resolution_actor": {"principal_kind": "human", "principal_id": "OP-0001"},
        "selected_option_id": "approve",
        "receipt_ref": f"{_CONTAINER}/evidence/EVD-0001",
        "created_at": AT.isoformat(),
        "updated_at": AT.isoformat(),
    }


def approval_rows() -> dict[str, dict[str, Any]]:
    """Return the document rows an acceptance needs beside its Milestone."""
    return {Epoch2Collection.PENDING_ACTION.value: {"ACT-0001": sealed_approval_row()}}


__all__ = [
    "APPROVAL_URN",
    "AT",
    "BATCH_URN",
    "MILESTONE_URN",
    "SEED_RECORDS",
    "TASK_URN",
    "acceptance_bundle_row",
    "approval_rows",
    "document_path",
    "firehose_path",
    "legacy_task_row",
    "method_context",
    "provision",
    "rekeyed",
    "root_context",
    "sealed_approval_row",
    "seed",
    "seed_row",
    "tree_root",
]
