"""The URN re-key plan: one appended superseding row per old-URN line."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.identity.errors import IdentityError, IdentityRejection
from eawf.kernel.migration.epoch2.urn_rekey import (
    DEFAULT_PROJECT_KEY,
    UrnRekeyPlan,
    plan_urn_rekey,
    rekey_urn,
)
from eawf.kernel.store.ledger import (
    LedgerRecord,
    LedgerTornTailError,
    effective_records,
    line_digest,
    read_ledger_records,
    render_ledger_line,
    verify_append_only,
)
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection

OLD = "eawf://EAWF/EAWF/EAWF"
OLD_PROJECT_LEVEL = "eawf://EAWF/EAWF/_"
WRITTEN_AT = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
REKEYED_AT = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
PROSE = f"every URN reads `{OLD}/...` since the cutover"


def _record(
    collection: Epoch2Collection,
    key: str,
    payload: dict[str, Any],
    *,
    supersedes: str | None = None,
) -> LedgerRecord:
    return LedgerRecord(
        collection=collection,
        record_key=key,
        status="open",
        recorded_at=WRITTEN_AT,
        supersedes=supersedes,
        payload=payload,
    )


def _write_ledger(path: Path, records: list[LedgerRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes("".join(f"{render_ledger_line(r)}\n" for r in records).encode())


def _has_old_urn(value: Any) -> bool:
    if isinstance(value, str):
        return rekey_urn(value, DEFAULT_PROJECT_KEY) is not None
    if isinstance(value, dict):
        return any(_has_old_urn(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_old_urn(item) for item in value)
    return False


@pytest.fixture
def generation(tmp_path: Path) -> Path:
    """A generation whose ledgers mix old-URN, superseded and URN-free lines."""
    state_path = tmp_path / "gen-x" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "batch": {
                    "BAT-0001": {
                        "key": "BAT-0001",
                        "milestone_ref": f"{OLD}/milestone/MLS-0001",
                        "repository_ref": f"{OLD}/repository/EAWF",
                    }
                },
                "notes": [PROSE, 3, None],
            },
            indent=2,
            sort_keys=True,
        )
    )
    task = Epoch2Collection.TASK
    stale = _record(task, "EAWF-0001", {"batch_ref": f"{OLD}/batch/BAT-0001"})
    corrected = _record(
        task,
        "EAWF-0001",
        {"batch_ref": f"{OLD}/batch/BAT-0002", "title": "Fix the thing"},
        supersedes=line_digest(render_ledger_line(stale)),
    )
    _write_ledger(
        ledger_path(state_path, task),
        [
            stale,
            corrected,
            _record(task, "EAWF-0002", {"title": PROSE, "count": 7}),
            _record(
                task,
                "EAWF-0003",
                {
                    "links": [f"{OLD_PROJECT_LEVEL}/claim/CLM-0001#rung-2", "plain"],
                    "nested": {"ref": f"{OLD_PROJECT_LEVEL}/project/EAWF"},
                },
            ),
        ],
    )
    _write_ledger(
        ledger_path(state_path, Epoch2Collection.RUN),
        [_record(Epoch2Collection.RUN, "RUN-0001", {"task_ref": f"{OLD}/task/EAWF-0003"})],
    )
    return state_path


def _apply(plan: UrnRekeyPlan) -> None:
    for path, appended in plan.ledger_appends.items():
        before = path.read_bytes()
        after = before + appended
        verify_append_only(before, after)
        path.write_bytes(after)


def test_rekey_plans_one_row_per_effective_old_urn_line(generation: Path) -> None:
    plan = plan_urn_rekey(generation, recorded_at=REKEYED_AT)
    task_path = ledger_path(generation, Epoch2Collection.TASK)
    task_lines = task_path.read_text().splitlines()
    run_lines = ledger_path(generation, Epoch2Collection.RUN).read_text().splitlines()

    assert [(row.ledger.name, row.record.record_key) for row in plan.rows] == [
        ("task.jsonl", "EAWF-0001"),
        ("task.jsonl", "EAWF-0003"),
        ("run.jsonl", "RUN-0001"),
    ]
    assert [row.record.supersedes for row in plan.rows] == [
        line_digest(task_lines[1]),
        line_digest(task_lines[3]),
        line_digest(run_lines[0]),
    ]
    assert plan.rows[0].record.payload == {
        "batch_ref": "eawf://EAWF/batch/BAT-0002",
        "title": "Fix the thing",
    }
    assert plan.rows[1].record.payload == {
        "links": ["eawf://EAWF/claim/CLM-0001#rung-2", "plain"],
        "nested": {"ref": "eawf://EAWF/project/EAWF"},
    }
    for row in plan.rows:
        assert row.record.recorded_at == REKEYED_AT
        assert row.record.status == "open"


def test_rekey_keeps_non_urn_payload_bytes(generation: Path) -> None:
    plan = plan_urn_rekey(generation, recorded_at=REKEYED_AT)
    old_line = ledger_path(generation, Epoch2Collection.TASK).read_text().splitlines()[1]
    new_line = render_ledger_line(plan.rows[0].record)
    old_payload = json.dumps(json.loads(old_line)["payload"]).replace(OLD, "eawf://EAWF")
    assert json.dumps(json.loads(new_line)["payload"]) == old_payload


def test_rekey_is_append_only(generation: Path) -> None:
    plan = plan_urn_rekey(generation, recorded_at=REKEYED_AT)
    assert set(plan.ledger_appends) == {
        ledger_path(generation, Epoch2Collection.TASK),
        ledger_path(generation, Epoch2Collection.RUN),
    }
    _apply(plan)
    for collection in (Epoch2Collection.TASK, Epoch2Collection.RUN):
        effective = effective_records(read_ledger_records(ledger_path(generation, collection)))
        assert effective
        assert not any(_has_old_urn(record.payload) for record in effective)
    task_effective = effective_records(
        read_ledger_records(ledger_path(generation, Epoch2Collection.TASK))
    )
    assert [record.record_key for record in task_effective] == [
        "EAWF-0002",
        "EAWF-0001",
        "EAWF-0003",
    ]


def test_rekey_rewrites_the_generation_document(generation: Path) -> None:
    plan = plan_urn_rekey(generation, recorded_at=REKEYED_AT)
    assert plan.document is not None
    assert plan.document["batch"]["BAT-0001"] == {
        "key": "BAT-0001",
        "milestone_ref": "eawf://EAWF/milestone/MLS-0001",
        "repository_ref": "eawf://EAWF/repository/EAWF",
    }
    assert plan.document["notes"] == [PROSE, 3, None]


def test_rekey_is_idempotent(generation: Path) -> None:
    first = plan_urn_rekey(generation, recorded_at=REKEYED_AT)
    _apply(first)
    assert first.document is not None
    generation.write_text(json.dumps(first.document, indent=2, sort_keys=True))

    again = plan_urn_rekey(generation, recorded_at=REKEYED_AT)
    assert again.rows == ()
    assert dict(again.ledger_appends) == {}
    assert again.document is None


@pytest.mark.parametrize("target", ["", "E", "eawf", "A/B", "EAWF-PROJECT-KEY1", "1ABC"])
def test_rekey_rejects_invalid_target(generation: Path, target: str) -> None:
    with pytest.raises(IdentityError) as caught:
        plan_urn_rekey(generation, recorded_at=REKEYED_AT, project_key=target)
    assert caught.value.code is IdentityRejection.SYMBOL_KEY_INVALID


@pytest.mark.parametrize("target", ["AB", "ABCDEFGHIJKLMNOP"])
def test_rekey_accepts_boundary_length_targets(generation: Path, target: str) -> None:
    plan = plan_urn_rekey(generation, recorded_at=REKEYED_AT, project_key=target)
    assert plan.project_key == target
    assert plan.rows[0].record.payload["batch_ref"] == f"eawf://{target}/batch/BAT-0002"
    assert plan.rows[1].record.payload["nested"] == {"ref": f"eawf://{target}/project/{target}"}


def test_rekey_of_an_empty_generation_plans_nothing(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text("{}")
    plan = plan_urn_rekey(state_path, recorded_at=REKEYED_AT)
    assert plan.rows == ()
    assert plan.document is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (f"{OLD}/task/EAWF-0001", "eawf://EAWF/task/EAWF-0001"),
        (f"{OLD_PROJECT_LEVEL}/release/REL-0.7.0", "eawf://EAWF/release/REL-0.7.0"),
        (f"{OLD_PROJECT_LEVEL}/claim/CLM-0001#rung-4", "eawf://EAWF/claim/CLM-0001#rung-4"),
        (f"{OLD_PROJECT_LEVEL}/workspace/EAWF", "eawf://EAWF/workspace/EAWF"),
        ("eawf://EAWF/task/EAWF-0001", None),
        (PROSE, None),
        ("", None),
        (f"see {OLD}/task/EAWF-0001", None),
    ],
)
def test_rekey_urn_rewrites_only_whole_three_slot_urns(value: str, expected: str | None) -> None:
    assert rekey_urn(value, DEFAULT_PROJECT_KEY) == expected


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (f"{OLD}/gadget/G-1", IdentityRejection.URN_UNKNOWN_KIND),
        (f"{OLD_PROJECT_LEVEL}/claim/CLM-0001#rung-5", IdentityRejection.IDENTITY_KIND_MISMATCH),
        (f"{OLD_PROJECT_LEVEL}/task/EAWF-0001", IdentityRejection.REPOSITORY_SLOT_INVALID),
    ],
)
def test_rekey_urn_refuses_a_broken_three_slot_urn(value: str, code: IdentityRejection) -> None:
    with pytest.raises(IdentityError) as caught:
        rekey_urn(value, DEFAULT_PROJECT_KEY)
    assert caught.value.code is code


def test_rekey_urn_refuses_a_non_string() -> None:
    with pytest.raises(TypeError):
        rekey_urn(42, DEFAULT_PROJECT_KEY)  # type: ignore[arg-type]


def test_rekey_refuses_a_missing_document(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        plan_urn_rekey(tmp_path / "state.json", recorded_at=REKEYED_AT)


def test_rekey_refuses_a_torn_ledger(generation: Path) -> None:
    path = ledger_path(generation, Epoch2Collection.TASK)
    path.write_bytes(path.read_bytes() + b'{"schema_version":')
    with pytest.raises(LedgerTornTailError):
        plan_urn_rekey(generation, recorded_at=REKEYED_AT)


def test_rekey_refuses_a_line_that_is_not_a_record(generation: Path) -> None:
    path = ledger_path(generation, Epoch2Collection.TASK)
    path.write_bytes(path.read_bytes() + b'{"unexpected": true}\n')
    with pytest.raises(ValidationError):
        plan_urn_rekey(generation, recorded_at=REKEYED_AT)


def test_rekey_refuses_a_naive_timestamp(generation: Path) -> None:
    with pytest.raises(ValidationError):
        plan_urn_rekey(generation, recorded_at=datetime(2026, 10, 6, 12, 0))
