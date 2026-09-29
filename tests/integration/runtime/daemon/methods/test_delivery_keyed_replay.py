"""The delivery verbs honour the idempotency key their help promises.

``--idempotency-key`` is documented as "a retry replays its receipt".
These cases dispatch the registered verbs over a disposable epoch-2
canary and check the promise: a retry under one key returns the first
answer without acting again, the same key on a different request is
refused, and a refusal files nothing, so its retry runs again.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.store.ledger import read_ledger_records
from eawf.platform.install.canary import canary_ref, provision_canary
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.delivery_acceptance import (
    DELIVERY_RECONCILE_MERGE_METHOD,
    DELIVERY_RECORD_EVIDENCE_METHOD,
)

pytestmark = pytest.mark.integration

CONTAINER: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
AT: Final = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    provision_canary(repo_root=tmp_path / "repo", ref=canary_ref("KEY"), provisioned_at=AT)
    return tmp_path / "repo"


def _dispatch(repo: Path, method: str, **params: Any) -> dict[str, Any]:
    ctx = MethodContext(
        started_at=AT.isoformat(),
        pid=1,
        protocol_version="1",
        version="test",
        wal_dir=repo.parent / "runtime" / "wal",
    )
    result: dict[str, Any] = asyncio.run(
        methods.dispatch(method, ctx, {"repo_root": str(repo), **params})
    )
    return result


def _refusal(repo: Path, method: str, **params: Any) -> str:
    with pytest.raises(DaemonValidationError) as caught:
        _dispatch(repo, method, **params)
    return str(caught.value)


def _evidence(repo: Path, key: str, summary: str = "the install smoke passed") -> dict[str, Any]:
    return _dispatch(
        repo,
        DELIVERY_RECORD_EVIDENCE_METHOD,
        urn=f"{CONTAINER}/milestone/MLS-0030",
        actor="OPERATOR",
        idempotency_key=key,
        kind="artifact",
        summary=summary,
    )


def _evidence_rows(repo: Path) -> int:
    paths = list((repo / ".ea").rglob("ledger/evidence.jsonl"))
    return sum(len(read_ledger_records(path)) for path in paths)


def test_idempotency_key_retry_replays_the_first_answer(repo: Path) -> None:
    first = _evidence(repo, "evidence-1")
    again = _evidence(repo, "evidence-1")
    assert first["created"] is True
    assert again == first
    assert _evidence_rows(repo) == 1


def test_idempotency_key_a_new_key_is_a_new_request(repo: Path) -> None:
    _evidence(repo, "evidence-1")
    other = _evidence(repo, "evidence-2")
    assert other["created"] is False
    assert _evidence_rows(repo) == 1


def test_idempotency_key_reused_on_a_different_request_is_refused(repo: Path) -> None:
    _evidence(repo, "evidence-1")
    with pytest.raises(DaemonValidationError, match="evidence-1"):
        _evidence(repo, "evidence-1", summary="a different observation")
    assert _evidence_rows(repo) == 1


def test_idempotency_key_a_refusal_files_nothing_so_its_retry_runs_again(repo: Path) -> None:
    params: dict[str, Any] = {
        "urn": f"{CONTAINER}/batch/BAT-0404",
        "actor": "OPERATOR",
        "idempotency_key": "reconcile-1",
        "observation": {
            "batch_ref": f"{CONTAINER}/batch/BAT-0404",
            "target_branch": "main",
            "observed_at": AT.isoformat(),
            "target_head_sha": "1a" * 20,
            "contained_shas": ["1a" * 20],
        },
    }
    first = _refusal(repo, DELIVERY_RECONCILE_MERGE_METHOD, **params)
    again = _refusal(repo, DELIVERY_RECONCILE_MERGE_METHOD, **params)
    assert "BAT-0404" in first
    assert again == first
