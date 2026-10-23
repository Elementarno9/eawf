"""Native trailers in ``tools/commit_prefix_lint.py``: the per-Batch push regime.

A pushed commit names each Task it delivers by ``Task`` trailer and, when it
names several, their one Batch by ``Batch`` trailer; ``--pre-push-lint``
holds every pushed commit to that. Git is modelled here;
``tests/integration/test_commit_prefix_lint_pre_push_e2e.py`` runs the same
check against a scratch repository.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TOOL_DIR = _REPO_ROOT / "tools"


def _load(name: str) -> Any:
    if str(_TOOL_DIR) not in sys.path:
        sys.path.insert(0, str(_TOOL_DIR))
    spec = importlib.util.spec_from_file_location(name, _TOOL_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def lint() -> Any:
    return _load("commit_prefix_lint")


def test_main_pre_push_lint_passes_without_variables(
    lint: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PRE_COMMIT_FROM_REF", raising=False)
    monkeypatch.delenv("PRE_COMMIT_TO_REF", raising=False)

    assert lint.main(["lint", "--pre-push-lint"]) == 0


# ---------------------------------------------------------------------------
# The per-Batch regime a pushed commit is held to
# ---------------------------------------------------------------------------

# The real message of 137db4bc8: a Task-trailered commit that names no Batch,
# the shape every per-Task commit takes.
_TASK_ONLY_MESSAGE = (
    "fix: check a plugin bundle named by a relative root\n\n"
    "publication_findings joined the root onto paths it had already\n"
    "joined, so the tag-only plugin release validate gate, which names\n"
    "the bundle relative to the checkout, read build/eawf-plugin twice.\n\n"
    "Task: EAWF-0298\n"
)
# The real message of a46f645f5: pushed with neither a Task nor a Batch
# trailer, so nothing ties it to the work it delivers.
_NO_TRAILER_MESSAGE = (
    "feat: re-plan a PLANNED Milestone through a child plan revision\n\n"
    "- A child of an APPLIED revision on the same Milestone rewrites it in\n"
    "  place: carried records keep their identity at the next revision, and\n"
    "  dropped PLANNED Batches and Tasks are cancelled in the same write\n"
    "- Move the successor-document build into planning/materialise.py\n"
)
_GENERATION_ID = "gen-0123456789abcdef"


def _batch_message(*trailers: str) -> str:
    return "feat: land the task\n\nWhy it lands.\n\n" + "\n".join(trailers) + "\n"


@pytest.fixture()
def records(lint: Any) -> Any:
    return lint.NativeRecords(
        batch_keys=frozenset({"BAT-0305", "BAT-0306"}),
        task_batches={
            "EAWF-0298": "BAT-0305",
            "EAWF-0331": "BAT-0306",
            "EAWF-0332": "BAT-0306",
            "EAWF-0321": "BAT-0305",
            "EAWF-0399": None,
        },
    )


def test_pushed_commit_accepts_the_real_task_only_message(lint: Any, records: Any) -> None:
    assert lint.pushed_commit_error(_TASK_ONLY_MESSAGE, ["src/eawf/x.py"], records) is None


def test_pushed_commit_accepts_a_task_without_a_batch(lint: Any, records: Any) -> None:
    message = _batch_message("Task: EAWF-0399")

    assert lint.pushed_commit_error(message, ["a.py"], records) is None


def test_pushed_commit_accepts_a_task_beside_its_batch(lint: Any, records: Any) -> None:
    message = _batch_message("Batch: BAT-0306", "Task: EAWF-0331")

    assert lint.pushed_commit_error(message, ["src/eawf/x.py"], records) is None


def test_pushed_commit_refuses_the_real_message_without_any_trailer(
    lint: Any, records: Any
) -> None:
    error = lint.pushed_commit_error(_NO_TRAILER_MESSAGE, ["src/eawf/x.py"], records)

    assert error == "carries no Task trailers; a commit names at least one Task"


# A squashed Batch commit: one Batch trailer and one Task trailer per Task it
# delivers, the shape every commit takes once a branch is squashed per Batch.
_BATCH_SQUASH_MESSAGE = (
    "feat: close a Batch and its Tasks once they land\n\n"
    "- Pin the branch head and walk the Batch to COMPLETED\n"
    "- Complete each ready Task the landing carries\n\n"
    "Batch: BAT-0306\n"
    "Task: EAWF-0331\n"
    "Task: EAWF-0332\n"
)


def test_pushed_commit_accepts_a_batch_commit_naming_several_of_its_tasks(
    lint: Any, records: Any
) -> None:
    assert lint.pushed_commit_error(_BATCH_SQUASH_MESSAGE, ["src/eawf/x.py"], records) is None


@pytest.mark.parametrize(
    ("trailers", "expected"),
    [
        (("Batch: BAT-0305",), "carries no Task trailers"),
        (
            ("Task: EAWF-0331", "Task: EAWF-0332"),
            "carries 2 (EAWF-0331, EAWF-0332) Task trailers without a Batch trailer",
        ),
        (
            ("Batch: BAT-0306", "Task: EAWF-0331", "Task: EAWF-0331"),
            "names Task 'EAWF-0331' twice",
        ),
        (
            ("Batch: BAT-0306", "Task: EAWF-0331", "Task: EAWF-0321"),
            "Task trailer 'EAWF-0321' belongs to Batch 'BAT-0305', not 'BAT-0306'",
        ),
        (
            ("Batch: BAT-0306", "Task: EAWF-0331", "Task: EAWF-9999"),
            "Task trailer 'EAWF-9999' names no Task of the canonical generation",
        ),
    ],
    ids=["batch-only", "two-tasks", "repeated-task", "foreign-second-task", "unknown-second"],
)
def test_pushed_commit_refuses_other_than_one_task(
    lint: Any, records: Any, trailers: tuple[str, ...], expected: str
) -> None:
    error = lint.pushed_commit_error(_batch_message(*trailers), ["a.py"], records)

    assert error is not None
    assert expected in error


def test_pushed_commit_refuses_two_batch_trailers(lint: Any, records: Any) -> None:
    message = _batch_message("Batch: BAT-0305", "Batch: BAT-0306", "Task: EAWF-0321")

    error = lint.pushed_commit_error(message, ["a.py"], records)

    assert error == (
        "carries 2 (BAT-0305, BAT-0306) Batch trailers; a commit names at most one Batch"
    )


def test_pushed_commit_refuses_a_task_outside_the_named_batch(lint: Any, records: Any) -> None:
    message = _batch_message("Batch: BAT-0306", "Task: EAWF-0321")

    error = lint.pushed_commit_error(message, ["a.py"], records)

    assert error == "Task trailer 'EAWF-0321' belongs to Batch 'BAT-0305', not 'BAT-0306'"


@pytest.mark.parametrize(
    ("trailers", "expected"),
    [
        (("Batch: BAT-0306", "Task: EAWF-0399"), "belongs to Batch None"),
        (("Task: EAWF-9999",), "names no Task of the canonical generation"),
        (("Batch: BAT-9999", "Task: EAWF-0331"), "names no Batch of the canonical generation"),
        (("Batch: BAT-306", "Task: EAWF-0331"), "is not a Batch key (BAT-<NNNN>)"),
        (("Batch: bat-0306", "Task: EAWF-0331"), "is not a Batch key (BAT-<NNNN>)"),
        (("Batch: BAT-03060", "Task: EAWF-0331"), "is not a Batch key (BAT-<NNNN>)"),
    ],
    ids=["task-without-batch", "unknown-task", "unknown-batch", "short-key", "lowercase", "long"],
)
def test_pushed_commit_refuses_unresolved_records(
    lint: Any, records: Any, trailers: tuple[str, ...], expected: str
) -> None:
    error = lint.pushed_commit_error(_batch_message(*trailers), ["a.py"], records)

    assert error is not None
    assert expected in error


def test_pushed_commit_ignores_a_trailer_quoted_in_prose(lint: Any, records: Any) -> None:
    error = lint.pushed_commit_error("feat: x\n\nsee Task: EAWF-0331 above\n", ["a.py"], records)

    assert error is not None
    assert "carries no Task trailers" in error


@pytest.mark.parametrize("subject", ["wip", "", "[P34-W01] feat: x", "feature: x"])
def test_pushed_commit_refuses_a_free_form_subject(lint: Any, records: Any, subject: str) -> None:
    error = lint.pushed_commit_error(f"{subject}\n\nTask: EAWF-0331\n", ["a.py"], records)

    assert error is not None
    assert "subject is not '<type>: <summary>'" in error


def test_pushed_commit_refuses_an_unmarked_canonical_root(lint: Any) -> None:
    error = lint.pushed_commit_error(_batch_message("Task: EAWF-0331"), ["a.py"], None)

    assert error == "Task trailer 'EAWF-0331': the canonical root is not marked epoch 2"


def test_state_commit_needs_no_task_and_stages_only_state_paths(lint: Any, records: Any) -> None:
    state_paths = [f".ea/generations/{_GENERATION_ID}/state.json", ".ea/epoch2-opt-in.json"]

    assert lint.pushed_commit_error("state: record the push\n", state_paths, records) is None
    assert lint.pushed_commit_error("state: record the push\n", [], records) is None
    error = lint.pushed_commit_error("state: x\n", [*state_paths, "src/eawf/x.py"], records)
    assert error == "state commit touches non-state paths: ['src/eawf/x.py']"
    # Off an epoch-2 root the generations tree is not a committed state path.
    assert lint.pushed_commit_error("state: x\n", state_paths, None) is not None


def test_pushed_commit_skips_the_commit_cap(
    lint: Any, records: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(**_kw: Any) -> tuple[int, str]:
        raise AssertionError("the cap must not run for a pushed commit")

    monkeypatch.setattr(lint, "_check_commit_cap", refuse)

    assert lint.pushed_commit_error(_batch_message("Task: EAWF-0331"), ["a.py"], records) is None


def test_task_trailer_position_admits_several_tasks_beside_a_batch(lint: Any) -> None:
    batch = _batch_message("Batch: BAT-0306", "Task: EAWF-0331", "Task: EAWF-0332")
    no_batch = _batch_message("Task: EAWF-0331", "Task: EAWF-0332")

    assert lint.task_trailer_position_error(batch) is None
    assert "repeated" in lint.task_trailer_position_error(no_batch)


def test_history_verdict_accepts_a_published_batch_commit(lint: Any) -> None:
    history = _load("commit_grammar_history")
    message = _batch_message("Batch: BAT-0306", "Task: EAWF-0331", "Task: EAWF-0332")

    assert history.history_verdict(message, predates_switch=False) == (0, "")


def _write_generation(root: Path, document: object) -> Path:
    generation = root / _GENERATION_ID
    generation.mkdir(parents=True)
    (generation / "state.json").write_text(json.dumps(document), encoding="utf-8")
    return generation


def test_load_native_records_reads_any_batch_status_and_task_refs(
    tmp_path: Path, lint: Any
) -> None:
    generation = _write_generation(
        tmp_path,
        {
            "batch": {
                "BAT-0305": {"key": "BAT-0305", "status": "ACTIVE"},
                "BAT-0306": {"key": "BAT-0306", "status": "PLANNED"},
                "P34-I01": {"status": "ACTIVE"},
            },
            "task": {
                "EAWF-0331": {"batch_ref": "eawf://EAWF/EAWF/EAWF/batch/BAT-0306"},
                "EAWF-0332": {"batch_ref": "eawf://EAWF/batch/BAT-0306"},
                "EAWF-0399": {"batch_ref": None},
            },
        },
    )

    loaded = lint.load_native_records(generation)

    assert loaded.batch_keys == {"BAT-0305", "BAT-0306"}
    assert loaded.task_batches == {
        "EAWF-0331": "BAT-0306",
        "EAWF-0332": "BAT-0306",
        "EAWF-0399": None,
    }
    message = _batch_message("Batch: BAT-0306", "Task: EAWF-0331")
    assert lint.pushed_commit_error(message, ["a.py"], loaded) is None


def test_load_native_records_reads_the_batch_of_a_compacted_task(tmp_path: Path, lint: Any) -> None:
    generation = _write_generation(tmp_path, {"batch": {}, "task": {}})
    ledger = generation / "ledger"
    ledger.mkdir()
    envelope = {
        "collection": "task",
        "record_key": "EAWF-0323",
        "status": "COMPLETED",
        "payload": {"key": "EAWF-0323", "batch_ref": "eawf://EAWF/batch/BAT-0305"},
    }
    (ledger / "task.jsonl").write_text(json.dumps(envelope) + "\n", encoding="utf-8")
    batch = {"collection": "batch", "record_key": "BAT-0305", "status": "COMPLETED", "payload": {}}
    (ledger / "batch.jsonl").write_text(json.dumps(batch) + "\n", encoding="utf-8")

    loaded = lint.load_native_records(generation)

    assert loaded.task_batches == {"EAWF-0323": "BAT-0305"}
    message = _batch_message("Batch: BAT-0305", "Task: EAWF-0323")
    assert lint.pushed_commit_error(message, ["a.py"], loaded) is None


def test_load_native_records_boundaries_and_errors(tmp_path: Path, lint: Any) -> None:
    empty = lint.load_native_records(_write_generation(tmp_path / "empty", {"batch": {}}))
    assert empty.batch_keys == frozenset()
    assert empty.task_batches == {}
    with pytest.raises(ValueError, match="'batch' must be an object"):
        lint.load_native_records(_write_generation(tmp_path / "bad", {"batch": []}))
    with pytest.raises(OSError):
        lint.load_native_records(tmp_path / "missing")
