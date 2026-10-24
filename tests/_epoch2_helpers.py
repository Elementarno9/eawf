"""Test helpers that lay down a tree the way ``eawf init`` leaves it after the flag day.

A plain epoch-1 tree refuses every mutating verb, so a test that drives a
mutating verb needs a tree born at epoch 2. These helpers bear one through
the same library writer ``eawf init`` uses, rather than a second writer.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.state.models import Artifact
from eawf.kernel.store.compaction import read_document, write_document
from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.epoch2_birth import bear_epoch2_tree
from eawf.workflow.evidence._io import MEASURED_CONTRACT_STATUS


def lay_epoch2_tree(
    repo_root: Path, *, state: dict[str, Any] | None = None, project_code: str = "QR"
) -> Path:
    """Write ``state`` as the tree's document, bear the tree at epoch 2, return its path.

    Args:
        repo_root: The repository root; the tree is ``repo_root/.ea``.
        state: The epoch-1 document to write first; empty when omitted.
        project_code: The code the generation is named for.

    Returns:
        The tree's ``state.json``.
    """
    state_path = repo_root / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(orjson.dumps(state if state is not None else {}))
    bear_epoch2_tree(
        state_path.parent, project_code=project_code, project_title="", born_at=datetime.now(UTC)
    )
    return state_path


def lay_contract_tree(repo_root: Path, contracts: Iterable[Artifact], *, project_code: str) -> Path:
    """Bear *repo_root*'s tree at epoch 2 holding *contracts* as promoted.

    ``release.create`` reads the measured contracts of the tree's selected
    generation, so the project row is written as a migrated tree carries
    it and the contracts are filed as one promotion line of the
    generation's evidence ledger.

    Args:
        repo_root: The repository root; the tree is ``repo_root/.ea``.
        contracts: The promoted measured contracts.
        project_code: The code the contracts were measured in.

    Returns:
        The tree's ``state.json`` slot.
    """
    state_path = lay_epoch2_tree(repo_root, project_code=project_code)
    authority = resolve_authority(state_path.parent)
    assert authority.target is not None and authority.generation_id is not None
    document = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    body = read_document(document)
    body["project"] = {
        project_code: {"payload": {"payload": {"code": project_code}}, "status": "imported"}
    }
    write_document(document, body)
    rows = [contract.model_dump(mode="json") for contract in contracts]
    if rows:
        append_ledger_record(
            ledger_path(document, Epoch2Collection.EVIDENCE),
            LedgerRecord(
                collection=Epoch2Collection.EVIDENCE,
                record_key="RPT-seed",
                status=MEASURED_CONTRACT_STATUS,
                recorded_at=datetime.now(UTC),
                payload={"spike_report_ref": "artifact://spike/seeded", "contracts": rows},
            ),
        )
    return state_path
