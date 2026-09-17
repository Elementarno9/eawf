"""The correction lineage survives from ``create`` to the CANDIDATE.

``release.create`` has always known which terminal rung a new checkpoint
succeeds -- it refuses to open one until that rung is finished with --
and it named the predecessor in its reply. The stored DRAFT did not carry
it, so the lineage lived in one RPC response and nowhere a later reader
could find it: the record a burn or a reviewer reads superseded nothing.

These tests pin both halves of the repair. ``create`` writes
``supersedes_release_ref`` onto the row it records, and the candidate pin
carries it forward rather than dropping it, so the whole walk from the
open to the pinned candidate keeps one answer to "what did this version
replace".
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.release import Release, ReleaseStatus, release_key
from eawf.kernel.state.models import State
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.release import _superseding, create
from eawf.runtime.daemon.methods.release_candidate import candidate
from eawf.workflow.evidence._io import atomic_write_state, load_state
from eawf.workflow.evidence.measured_contract import (
    PREFLIGHT_CHECKPOINT_BANDS,
    PREFLIGHT_CONTRACTS,
    promote_measured_contract,
)
from eawf.workflow.release.admission import required_contract_ids
from eawf.workflow.release.adoption import adopt_publication
from eawf.workflow.release.publication import burn_release
from eawf.workflow.release.records import read_release_record, record_release
from tests._release_helpers import dev1_adoption, dev1_config, dev1_draft

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[5]
_EMPTY_STATE = _REPO_ROOT / "tests" / "fixtures" / "states" / "valid" / "01-empty-repo.json"

#: Project code of the empty-repo fixture.
SCOPE = "QR"

DEV1 = "0.7.0.dev1"
DEV2 = "0.7.0.dev2"
DEV1_KEY = release_key(DEV1)
DEV2_KEY = release_key(DEV2)

RECEIPTS: dict[str, dict[str, Any]] = {
    "pypi": {
        "target_id": "pypi",
        "version": DEV2,
        "job_conclusion": "success",
        "run_id": "1",
        "artifact_digests": {
            f"eawf-{DEV2}-py3-none-any.whl": f"sha256:{'1' * 64}",
            f"eawf-{DEV2}.tar.gz": f"sha256:{'3' * 64}",
        },
    },
    "npm": {
        "target_id": "npm",
        "version": "0.7.0-dev.2",
        "job_conclusion": "success",
        "run_id": "2",
        "artifact_digests": {f"elementarno-eawf-{DEV2}.tgz": f"sha256:{'5' * 64}"},
    },
    "github": {
        "target_id": "github",
        "version": DEV2,
        "job_conclusion": "success",
        "run_id": "2",
        "artifact_digests": {
            "RELEASE_NOTES.md": f"sha256:{'6' * 64}",
            "SHA256SUMS": f"sha256:{'7' * 64}",
            f"eawf-plugin-{DEV2}.tar.gz": f"sha256:{'8' * 64}",
        },
    },
}


def git(repo_root: Path, *args: str) -> str:
    """Run one git command in *repo_root* and return its stdout, stripped."""
    done = subprocess.run(
        ["git", "-C", str(repo_root), *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def state_with(versions: Sequence[str]) -> State:
    """Return the fixture state with every contract *versions* require promoted."""
    state = load_state(_EMPTY_STATE)
    for version in versions:
        for contract_id in required_contract_ids(version):
            promote_measured_contract(
                state,
                contract=PREFLIGHT_CONTRACTS[contract_id],
                scope_id=SCOPE,
                required_band=PREFLIGHT_CHECKPOINT_BANDS[contract_id],
            )
    return state


def burned_dev1() -> Release:
    """Return dev1 as its burn left it: partially released."""
    adopted = adopt_publication(dev1_draft(), dev1_config(), adoption=dev1_adoption())
    burned, _operation = burn_release(adopted, dev1_config(), None)
    return burned


def context(tmp_path: Path, *, state: State, records: Sequence[Release] = ()) -> MethodContext:
    """Return a context bound to a git checkout holding *state* and *records*."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    git(repo_root, "init", "--quiet")
    git(repo_root, "config", "user.email", "release@example.invalid")
    git(repo_root, "config", "user.name", "Release Test")
    (repo_root / "README.md").write_text("supersedes fixture\n", encoding="utf-8")
    git(repo_root, "add", "README.md")
    git(repo_root, "commit", "--quiet", "-m", "seed")
    state_path = repo_root / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_state(state_path, state)
    for record in records:
        record_release(state_path, record, recorded_at=datetime.now(UTC), summary="seed")
    return MethodContext(
        started_at="2026-09-17T00:00:00+00:00",
        pid=4321,
        protocol_version="1",
        version=DEV2,
        state_path=state_path,
    )


def receipts_dir(ctx: MethodContext) -> Path:
    """Write the dev2 receipts beside the checkout and return the directory."""
    directory = Path(str(ctx.state_path)).parent.parent / "receipts"
    directory.mkdir(parents=True, exist_ok=True)
    for target_id, body in RECEIPTS.items():
        (directory / f"publication-receipt-{target_id}.json").write_text(
            json.dumps(body), encoding="utf-8"
        )
    return directory


def stored(ctx: MethodContext, key: str) -> Release:
    """Return the record the collection currently holds for *key*."""
    record = read_release_record(Path(str(ctx.state_path)), key)
    assert record is not None
    return record


# --- create writes the lineage ------------------------------------------------


def test_create_persists_the_supersedes_ref_on_the_stored_draft(tmp_path: Path) -> None:
    """The row a later reader finds carries the rung it replaced."""
    ctx = context(tmp_path, state=state_with([DEV2]), records=[burned_dev1()])

    result = asyncio.run(create(ctx, {"version": DEV2}))

    assert result["supersedes_release_ref"] == DEV1_KEY
    draft = stored(ctx, DEV2_KEY)
    assert draft.status is ReleaseStatus.DRAFT
    assert draft.supersedes_release_ref == DEV1_KEY


def test_create_at_the_head_of_the_ladder_stores_no_lineage(tmp_path: Path) -> None:
    """The first rung succeeds nothing, so the field stays empty."""
    ctx = context(tmp_path, state=state_with([DEV1]))

    result = asyncio.run(create(ctx, {"version": DEV1}))

    assert result["supersedes_release_ref"] is None
    assert stored(ctx, DEV1_KEY).supersedes_release_ref is None


def test_the_reply_and_the_stored_row_name_the_same_predecessor(tmp_path: Path) -> None:
    """One answer, not two: the reply is the record, read back."""
    ctx = context(tmp_path, state=state_with([DEV2]), records=[burned_dev1()])

    result = asyncio.run(create(ctx, {"version": DEV2}))

    assert (
        result["release"]["supersedes_release_ref"] == stored(ctx, DEV2_KEY).supersedes_release_ref
    )


def test_superseding_refuses_a_record_that_supersedes_itself() -> None:
    """The record invariant is enforced on the way in, not assumed."""
    with pytest.raises(ValidationError, match="cannot supersede itself"):
        _superseding(dev1_draft(), DEV1_KEY)


# --- the pin keeps it ---------------------------------------------------------


def test_the_candidate_keeps_the_supersedes_ref_create_stored(tmp_path: Path) -> None:
    """The whole walk from open to pin keeps one lineage answer."""
    ctx = context(tmp_path, state=state_with([DEV2]), records=[burned_dev1()])
    asyncio.run(create(ctx, {"version": DEV2}))
    repo_root = Path(str(ctx.state_path)).parent.parent

    result = asyncio.run(
        candidate(
            ctx,
            {
                "version": DEV2,
                "receipts_dir": str(receipts_dir(ctx)),
                "source_sha": git(repo_root, "rev-parse", "HEAD"),
            },
        )
    )

    assert result["supersedes_release_ref"] == DEV1_KEY
    pinned = stored(ctx, DEV2_KEY)
    assert pinned.status is ReleaseStatus.CANDIDATE
    assert pinned.supersedes_release_ref == DEV1_KEY
    assert pinned.manifest_digest is not None
