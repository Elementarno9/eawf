"""The 0.7.1 patch checkpoint resolves through preflight and the daemon.

Preflight and ``release.create`` both resolve a checkpoint's
configuration against the train that declares it. Before the train was
looked up by version, every verb read the 0.7.0 ladder and refused 0.7.1
as unauthored, so the patch could be neither swept nor opened.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.spec.release import ReleaseStatus
from eawf.kernel.spec.release_config import ReleaseConfigError
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.release import create, show
from eawf.workflow.evidence import provider_certification
from eawf.workflow.evidence._io import ContractView
from eawf.workflow.evidence.measured_contract import (
    PREFLIGHT_CHECKPOINT_BANDS,
    PREFLIGHT_CONTRACTS,
    promote_measured_contract,
)
from eawf.workflow.evidence.provider_certification import canary_evidence_path
from eawf.workflow.release.admission import checkpoint_release_config, required_contract_ids
from eawf.workflow.release.records import read_release_records
from tests._contract_view import contract_view
from tests._epoch2_helpers import lay_contract_tree
from tests._release_helpers import accepted_canary_export

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[5]
_EMPTY_STATE = _REPO_ROOT / "tests" / "fixtures" / "states" / "valid" / "01-empty-repo.json"
SCOPE = "QR"
PATCH = "0.7.1"
PATCH_KEY = "REL-0.7.1"
PATCH_REF = "milestone://eawf/patch-0.7.1"
PATCH_EVIDENCE_DIR = (".ea", "artifacts", "evidence", "patch-0.7.1-stable")


@pytest.fixture(autouse=True)
def mapped_patch_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Map the patch rung's evidence directory, as its evidence commit does."""
    monkeypatch.setitem(
        provider_certification.CANARY_EVIDENCE_DIRS,
        PATCH_KEY,
        PATCH_EVIDENCE_DIR,
    )


def stage_patch_export(repo_root: Path) -> None:
    """Commit a canary export for the patch rung that accepts its one bundle."""
    document: dict[str, Any] = accepted_canary_export({"reference": PATCH_REF})
    document["release_key"] = PATCH_KEY
    path = canary_evidence_path(repo_root, PATCH_KEY)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def patch_state() -> ContractView:
    """Return the fixture state with every contract the patch rung requires promoted."""
    state = contract_view(_EMPTY_STATE)
    for contract_id in required_contract_ids(PATCH):
        promote_measured_contract(
            state,
            contract=PREFLIGHT_CONTRACTS[contract_id],
            scope_id=SCOPE,
            required_band=PREFLIGHT_CHECKPOINT_BANDS[contract_id],
        )
    return state


def context(tmp_path: Path) -> MethodContext:
    """Bind a test daemon context to a scratch state holding no release record."""
    state_path = lay_contract_tree(tmp_path, patch_state().artifacts.values(), project_code=SCOPE)
    stage_patch_export(tmp_path)
    return MethodContext(
        started_at=datetime.now(UTC).isoformat(),
        pid=4321,
        protocol_version="1",
        version=PATCH,
        state_path=state_path,
    )


def test_preflight_resolves_the_patch_config_against_its_own_train(tmp_path: Path) -> None:
    stage_patch_export(tmp_path)

    config = checkpoint_release_config(PATCH, repo_root=tmp_path)

    assert config.release_key == PATCH_KEY
    assert config.membership_refs == (PATCH_REF,)


def test_preflight_without_a_patch_export_refuses_on_membership_not_on_the_train(
    tmp_path: Path,
) -> None:
    with pytest.raises(ReleaseConfigError, match=r"'REL-0\.7\.1' requires non-empty"):
        checkpoint_release_config(PATCH, repo_root=tmp_path)


def test_create_opens_the_patch_rung_with_no_predecessor(tmp_path: Path) -> None:
    ctx = context(tmp_path)

    result = asyncio.run(create(ctx, {"version": PATCH, "membership_refs": [PATCH_REF]}))

    assert result["release"]["key"] == PATCH_KEY
    assert result["release"]["status"] == ReleaseStatus.DRAFT.value
    assert result["supersedes_release_ref"] is None
    assert PATCH_KEY in read_release_records(Path(str(ctx.state_path)))


def test_show_names_the_train_of_the_version_asked_for(tmp_path: Path) -> None:
    ctx = context(tmp_path)

    patch = asyncio.run(show(ctx, {"version": PATCH}))
    stable = asyncio.run(show(ctx, {"version": "0.7.0"}))

    assert patch["train_id"] == "TRAIN-0.7.1"
    assert stable["train_id"] == "TRAIN-0.7.0"


def test_show_without_a_version_moves_to_the_patch_train_once_it_has_a_record(
    tmp_path: Path,
) -> None:
    ctx = context(tmp_path)
    assert asyncio.run(show(ctx, {}))["train_id"] == "TRAIN-0.7.0"

    asyncio.run(create(ctx, {"version": PATCH, "membership_refs": [PATCH_REF]}))
    shown = asyncio.run(show(ctx, {}))

    assert shown["train_id"] == "TRAIN-0.7.1"
    assert shown["record"]["key"] == PATCH_KEY


def test_create_refuses_a_version_no_train_declares(tmp_path: Path) -> None:
    ctx = context(tmp_path)

    with pytest.raises(DaemonValidationError, match="no release train declares a checkpoint"):
        asyncio.run(create(ctx, {"version": "9.9.9"}))


SECOND_PATCH = "0.7.2"
SECOND_KEY = "REL-0.7.2"
SECOND_REF = "milestone://eawf/MLS-0108"


def test_v072_admission(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert required_contract_ids(SECOND_PATCH) == required_contract_ids(PATCH)
    monkeypatch.setitem(
        provider_certification.CANARY_EVIDENCE_DIRS,
        SECOND_KEY,
        (".ea", "artifacts", "evidence", "patch-0.7.2-stable"),
    )
    ctx = context(tmp_path)
    state_path = Path(str(ctx.state_path))
    asyncio.run(create(ctx, {"version": PATCH, "membership_refs": [PATCH_REF]}))
    before = read_release_records(state_path)[PATCH_KEY]

    with pytest.raises(ReleaseConfigError, match=r"'REL-0\.7\.2' requires non-empty"):
        checkpoint_release_config(SECOND_PATCH, repo_root=tmp_path)
    document: dict[str, Any] = accepted_canary_export({"reference": SECOND_REF})
    document["release_key"] = SECOND_KEY
    path = canary_evidence_path(tmp_path, SECOND_KEY)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    config = checkpoint_release_config(SECOND_PATCH, repo_root=tmp_path)
    assert config.release_key == SECOND_KEY
    assert config.membership_refs == (SECOND_REF,)

    result = asyncio.run(create(ctx, {"version": SECOND_PATCH, "membership_refs": [SECOND_REF]}))

    assert result["release"]["key"] == SECOND_KEY
    assert result["release"]["status"] == ReleaseStatus.DRAFT.value
    assert result["supersedes_release_ref"] is None
    records = read_release_records(state_path)
    assert records[PATCH_KEY] == before
    assert asyncio.run(show(ctx, {"version": PATCH}))["train_id"] == "TRAIN-0.7.1"
    shown = asyncio.run(show(ctx, {}))
    assert shown["train_id"] == "TRAIN-0.7.2"
    assert shown["record"]["key"] == SECOND_KEY
