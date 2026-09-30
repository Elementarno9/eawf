"""PLAN-015 on the live path: a decision filed through ``domain.record.append``.

A cut-over tree files its decisions through the daemon's record append, so
that is where a decision must keep its alternatives, evidence, resolved
questions and supersession chain. Each test dispatches the real verb
against a real applied tree and reads the generation's decision ledger
back.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.store.ledger import effective_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.methods import MethodContext
from tests.integration.kernel.migration._legacy_continuation import (
    APPEND,
    call,
    ledger,
    method_context,
    seeded_applied_tree,
)

pytestmark = pytest.mark.integration

CONTAINER: Final = "eawf://EAWF/EAWF/EAWF"
EVIDENCE: Final = f"{CONTAINER}/evidence/EVD-0001"
QUESTION: Final = "eawf://EAWF/EAWF/_/question/QST-0001"
AT: Final = "2026-03-04T00:00:00Z"


def _decision(key: str, **overrides: Any) -> dict[str, Any]:
    """Return a proposed native decision, overridden field by field."""
    fields: dict[str, Any] = {
        "key": key,
        "scope_ref": f"{CONTAINER}/milestone/MLS-0004",
        "title": "Release every phase",
        "decision": "Each closed phase ships as at least a minor release.",
        "rationale": "A phase is the unit an operator can accept or roll back.",
        "alternatives": [
            {"key": "per_phase", "label": "Release every phase"},
            {"key": "per_train", "label": "Release every train"},
        ],
        "chosen_option_key": "per_phase",
        "consequences": ["Every phase close bumps the version"],
        "evidence_refs": [EVIDENCE],
        "resolved_question_refs": [QUESTION],
        "effective_policy_revision": 1,
        "created_at": AT,
    }
    fields.update(overrides)
    return fields


def _ratified(key: str, **overrides: Any) -> dict[str, Any]:
    return _decision(key, status="ACTIVE", ratified_by="OP-0001", ratified_at=AT, **overrides)


@pytest.fixture
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MethodContext:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(runtime))
    return method_context(runtime)


def _standing(tree: Any) -> dict[str, dict[str, Any]]:
    """Return the current payload of every native decision line, by key."""
    lines = effective_records(ledger(tree, Epoch2Collection.DECISION))
    return {
        line.record_key: line.payload["payload"]
        for line in lines
        if "key" in line.payload.get("payload", {})
    }


def test_plan_015_decision_keeps_alternatives_evidence_and_resolved_questions(
    tmp_path: Path, ctx: MethodContext
) -> None:
    tree = seeded_applied_tree(tmp_path / "repo")

    answer = call(tree, APPEND, ctx, kind="decision", record=_decision("D90"))

    assert answer["status"] == "ok", answer
    filed = _standing(tree)["D90"]
    assert [o["key"] for o in filed["alternatives"]] == ["per_phase", "per_train"]
    assert filed["evidence_refs"] == [EVIDENCE]
    assert filed["resolved_question_refs"] == [QUESTION]
    assert filed["status"] == "PROPOSED"


def test_plan_015_ratify_then_obsolete_file_successor_lines(
    tmp_path: Path, ctx: MethodContext
) -> None:
    tree = seeded_applied_tree(tmp_path / "repo")
    assert call(tree, APPEND, ctx, kind="decision", record=_decision("D90"))["status"] == "ok"

    ratified = call(tree, APPEND, ctx, kind="decision", record=_ratified("D90"))
    assert ratified["status"] == "ok", ratified
    assert _standing(tree)["D90"]["status"] == "ACTIVE"

    retired = call(
        tree,
        APPEND,
        ctx,
        kind="decision",
        record=_decision("D90", status="OBSOLETE", ratified_by="OP-0001", ratified_at=AT),
    )
    assert retired["status"] == "ok", retired
    assert _standing(tree)["D90"]["status"] == "OBSOLETE"
    assert (
        len(ledger(tree, Epoch2Collection.DECISION))
        - len(effective_records(ledger(tree, Epoch2Collection.DECISION)))
        == 2
    )


def test_plan_015_supersede_retires_the_old_decision_in_the_same_append(
    tmp_path: Path, ctx: MethodContext
) -> None:
    tree = seeded_applied_tree(tmp_path / "repo")
    assert call(tree, APPEND, ctx, kind="decision", record=_ratified("D90"))["status"] == "ok"

    answer = call(
        tree,
        APPEND,
        ctx,
        kind="decision",
        record=_ratified("D91", supersedes="D90", chosen_option_key="per_train"),
    )

    assert answer["status"] == "ok", answer
    standing = _standing(tree)
    assert standing["D90"]["status"] == "SUPERSEDED"
    assert standing["D90"]["superseded_by"] == "D91"
    assert standing["D91"]["supersedes"] == "D90"


@pytest.mark.parametrize(
    ("record", "code"),
    [
        (
            _decision("D90", alternatives=[{"key": "per_phase", "label": "Release every phase"}]),
            "schema_validation_failed",
        ),
        (_decision("D90", evidence_refs=[]), "schema_validation_failed"),
        (_ratified("D90", supersedes="D77"), "transition_guard_failed"),
        (_decision("D90", status="OBSOLETE"), "illegal_transition"),
    ],
    ids=["one-option", "no-evidence", "unknown-supersedes", "filed-retired"],
)
def test_plan_015_incomplete_or_dangling_decision_is_refused(
    tmp_path: Path, ctx: MethodContext, record: dict[str, Any], code: str
) -> None:
    tree = seeded_applied_tree(tmp_path / "repo")
    before = ledger(tree, Epoch2Collection.DECISION)

    answer = call(tree, APPEND, ctx, kind="decision", record=record)

    assert answer["status"] == "error", answer
    assert answer["errors"][0]["code"] == code
    assert ledger(tree, Epoch2Collection.DECISION) == before


def test_plan_015_standing_decision_is_not_edited_in_place(
    tmp_path: Path, ctx: MethodContext
) -> None:
    tree = seeded_applied_tree(tmp_path / "repo")
    assert call(tree, APPEND, ctx, kind="decision", record=_ratified("D90"))["status"] == "ok"
    before = ledger(tree, Epoch2Collection.DECISION)

    answer = call(
        tree, APPEND, ctx, kind="decision", record=_ratified("D90", chosen_option_key="per_train")
    )

    assert answer["status"] == "error", answer
    assert answer["errors"][0]["code"] == "illegal_transition"
    assert ledger(tree, Epoch2Collection.DECISION) == before


def test_plan_015_supersession_cycle_is_refused(tmp_path: Path, ctx: MethodContext) -> None:
    tree = seeded_applied_tree(tmp_path / "repo")
    assert call(tree, APPEND, ctx, kind="decision", record=_ratified("D90"))["status"] == "ok"
    assert (
        call(tree, APPEND, ctx, kind="decision", record=_ratified("D91", supersedes="D90"))[
            "status"
        ]
        == "ok"
    )
    before = ledger(tree, Epoch2Collection.DECISION)

    # D90 stands superseded by D91, so filing it again to supersede D91 would close a cycle.
    answer = call(tree, APPEND, ctx, kind="decision", record=_ratified("D90", supersedes="D91"))

    assert answer["status"] == "error", answer
    assert ledger(tree, Epoch2Collection.DECISION) == before


def test_plan_015_an_epoch1_shaped_decision_is_refused_naming_the_native_shape(
    tmp_path: Path, ctx: MethodContext
) -> None:
    tree = seeded_applied_tree(tmp_path / "repo")
    before = ledger(tree, Epoch2Collection.DECISION)
    legacy = {
        "id": "D90",
        "scope_id": "P04",
        "title": "Close the phase through the legacy continuation",
        "rationale": "The epoch-1 close verbs are fenced after the cutover.",
        "status": "active",
        "created_at": AT,
    }

    answer = call(tree, APPEND, ctx, kind="decision", record=legacy)

    assert answer["status"] == "error", answer
    error = answer["errors"][0]
    assert error["code"] == "schema_validation_failed"
    assert error["guard"] == "decision_epoch1_shape"
    assert "native Decision document" in error["message"]
    assert ledger(tree, Epoch2Collection.DECISION) == before
