"""Audits, decisions and artifacts filed through ``domain.record.append``.

A repository cut over mid-phase still records the audit, the decisions and
the artifacts its remaining work cites. The epoch-1 verbs that used to
write them are fenced, so each case dispatches the daemon's record append
on the ``applied_tree`` cutover seeded with work in flight, and checks the
two things the cut promises: every write lands in the generation's ledger,
and the fenced epoch-1 surfaces keep their bytes.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.methods import MethodContext
from tests.integration.kernel.migration._cutover_harness import SEEDED_SURFACES
from tests.integration.kernel.migration._legacy_continuation import (
    APPEND,
    ITER,
    call,
    ledger,
    method_context,
    seeded_applied_tree,
)

AUDIT = {
    "id": "A900",
    "scope_id": ITER,
    "kind": "evaluation",
    "status": "complete",
    "created_at": "2026-03-04T00:00:00Z",
    "verdict": "pass",
}
DECISION = {
    "key": "D90",
    "scope_ref": "eawf://EAWF/EAWF/EAWF/milestone/MLS-0004",
    "title": "Close the phase against its imported audit",
    "decision": "The imported phase closes against its imported audit.",
    "rationale": "The epoch-1 close verbs are fenced after the cutover.",
    "alternatives": [
        {"key": "continue", "label": "Continue the imported phase"},
        {"key": "reopen", "label": "Reopen the phase natively"},
    ],
    "chosen_option_key": "continue",
    "consequences": ["The phase closes against its imported audit"],
    "evidence_refs": ["eawf://EAWF/EAWF/EAWF/evidence/EVD-0090"],
    "effective_policy_revision": 1,
    "status": "ACTIVE",
    "ratified_by": "OP-0001",
    "ratified_at": "2026-03-04T00:00:00Z",
    "created_at": "2026-03-04T00:00:00Z",
}
ARTIFACT = {
    "id": "ART-90",
    "kind": "research",
    "uri": ".ea/artifacts/research/ART-90.md",
    "urn": "urn:eawf:v1:artifact:P04/ART-90",
    "created_at": "2026-03-04T00:00:00Z",
}


@pytest.fixture
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MethodContext:
    """Return a daemon context whose runtime directory is this test's own."""
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(runtime))
    return method_context(runtime)


def _ok(answer: dict[str, Any]) -> dict[str, Any]:
    assert answer["status"] == "ok", answer
    result: dict[str, Any] = answer["result"]
    return result


def _digests(root: Path) -> dict[str, str]:
    return {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in SEEDED_SURFACES
    }


def test_record_append_files_each_kind_in_the_generation_alone(
    tmp_path: Path, ctx: MethodContext
) -> None:
    tree = seeded_applied_tree(tmp_path / "repo")
    fenced = _digests(tree.target_root)

    for kind, record in (("audit", AUDIT), ("decision", DECISION), ("artifact", ARTIFACT)):
        key = record.get("id", record.get("key"))
        filed = _ok(call(tree, APPEND, ctx, kind=kind, record=record))
        assert filed["record_key"] == key
        assert filed["collection"] == kind
        (line,) = [item for item in ledger(tree, Epoch2Collection(kind)) if item.record_key == key]
        assert line.payload["payload"].get("id", line.payload["payload"].get("key")) == key

    assert _digests(tree.target_root) == fenced


def test_record_append_of_an_unknown_kind_is_a_schema_refusal(
    tmp_path: Path, ctx: MethodContext
) -> None:
    """Error path: the kind is a closed set, checked before any lock is taken."""
    tree = seeded_applied_tree(tmp_path / "repo")

    answer = call(tree, APPEND, ctx, kind="memory", record=AUDIT)

    assert answer["status"] == "error", answer
    assert answer["errors"][0]["code"] == "schema_validation_failed"


@pytest.mark.parametrize(
    ("kind", "record"),
    [
        ("audit", {**AUDIT, "unexpected": "field"}),
        ("decision", {key: value for key, value in DECISION.items() if key != "rationale"}),
        ("artifact", {**ARTIFACT, "created_at": "not a date"}),
        ("audit", {**AUDIT, "id": "A001"}),
    ],
)
def test_record_append_refuses_a_malformed_or_duplicate_record(
    tmp_path: Path, ctx: MethodContext, kind: str, record: dict[str, Any]
) -> None:
    tree = seeded_applied_tree(tmp_path / "repo")
    before = ledger(tree, Epoch2Collection(kind))

    answer = call(tree, APPEND, ctx, kind=kind, record=record)

    assert answer["status"] == "error", answer
    assert answer["errors"][0]["code"] == "schema_validation_failed"
    assert ledger(tree, Epoch2Collection(kind)) == before
