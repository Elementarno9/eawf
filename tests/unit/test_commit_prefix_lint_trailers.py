"""Wave trailers in ``tools/commit_prefix_lint.py``: several per commit, none dropped.

A batch commit carries one ``Eawf-Wave`` line per wave it lands, and the
commit-msg check accepts it. A rewrite of pushed history is checked by
``--check-rewrite OLD NEW`` (and the ``--pre-push`` hook built on it): every
wave the old side names by trailer must still be named on the new side, or
the post-merge repin has nothing to find that wave by.

Git is modelled here; ``tests/integration/test_wave_trailer_guard_e2e.py``
runs the same checks against a scratch repository.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TOOL_DIR = _REPO_ROOT / "tools"
_CLAUDE_TRAILER = "\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n"
_OLD = "a" * 40
_NEW = "b" * 40
_BASE = "c" * 40
_OLD_SIDE = [
    "feat: a\n\nEawf-Wave: P34-I01-W01\n",
    "feat: b\n\nEawf-Wave: P34-I01-W02\nEawf-Wave: P34-I01-W03\n",
]


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


@pytest.fixture()
def guard(lint: Any) -> Any:
    return _load("wave_trailer_guard")


def _model_git(
    guard: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    is_ancestor: int,
    base_rc: int = 0,
    new_side: list[str] | None = None,
    old_side: list[str] = _OLD_SIDE,
) -> None:
    """Answer the guard's merge-base probes and range reads without git."""

    def fake_git(args: list[str], *, repo_root: Path | None) -> SimpleNamespace:
        if args[:2] == ["merge-base", "--is-ancestor"]:
            return SimpleNamespace(returncode=is_ancestor, stdout="", stderr="")
        return SimpleNamespace(returncode=base_rc, stdout=f"{_BASE}\n", stderr="")

    def fake_range(rev_range: str, *, repo_root: Path | None) -> list[str] | None:
        return old_side if rev_range.endswith(_OLD) else new_side

    monkeypatch.setattr(guard, "_git", fake_git)
    monkeypatch.setattr(guard, "_range_messages", fake_range)


# ---------------------------------------------------------------------------
# commit-msg: several trailers on one commit
# ---------------------------------------------------------------------------


def test_lint_accepts_a_commit_carrying_several_wave_trailers(
    tmp_path: Path, lint: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lint, "_prior_wave_commits", lambda *_a, **_kw: [])
    iter_id = "P34-I01"
    waves = [f"{iter_id}-W02", f"{iter_id}-W03"]
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {
                "current": {"phase_id": "P34", "iter_id": iter_id},
                "phases": {"P34": {"id": "P34", "status": "active", "iter_ids": [iter_id]}},
                "iters": {
                    iter_id: {
                        "id": iter_id,
                        "phase_id": "P34",
                        "status": "active",
                        "wave_ids": waves,
                    }
                },
                "waves": {w: {"id": w, "iter_id": iter_id, "status": "claimed"} for w in waves},
            }
        ),
        encoding="utf-8",
    )
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text(
        "feat: land a batch\n\nBody.\n\nEawf-Wave: P34-I01-W02\nEawf-Wave: P34-I01-W03"
        + _CLAUDE_TRAILER,
        encoding="utf-8",
    )

    code, diag = lint.lint(msg, ["src/eawf/x.py"], state_path=state, canonical_state_path=state)

    assert code == 0, diag


# ---------------------------------------------------------------------------
# The pure comparison
# ---------------------------------------------------------------------------


def test_dropped_wave_ids_accepts_a_batch_keeping_every_trailer(guard: Any) -> None:
    new = ["feat: ab\n\nEawf-Wave: P34-I01-W01\nEawf-Wave: P34-W02\nEawf-Wave: P34-I01-W03\n"]

    assert guard.dropped_wave_ids(_OLD_SIDE, new) == []


def test_dropped_wave_ids_names_the_missing_wave(guard: Any) -> None:
    new = ["feat: ab\n\nEawf-Wave: P34-I01-W01\nEawf-Wave: P34-I01-W02\n"]

    assert guard.dropped_wave_ids(_OLD_SIDE, new) == ["P34-I01-W03"]


def test_dropped_wave_ids_boundaries(guard: Any) -> None:
    assert guard.dropped_wave_ids([], []) == []
    assert guard.dropped_wave_ids(["chore: no trailer\n"], []) == []
    assert guard.dropped_wave_ids(["x\n\nEawf-Wave: P34-I01-W01\n"], []) == ["P34-I01-W01"]
    # A trailer quoted inside prose is not a trailer line.
    assert guard.trailer_wave_ids(["see Eawf-Wave: P34-I01-W01 above\n"]) == set()


# ---------------------------------------------------------------------------
# check_rewrite over modelled history
# ---------------------------------------------------------------------------


def test_check_rewrite_fast_forward_reads_no_messages(
    guard: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_git(guard, monkeypatch, is_ancestor=0, new_side=None)

    assert guard.check_rewrite(_OLD, _NEW) == (0, "")


def test_check_rewrite_accepts_a_squash_keeping_every_trailer(
    guard: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    squash = "feat: s\n\nEawf-Wave: P34-I01-W01\nEawf-Wave: P34-I01-W02\nEawf-Wave: P34-I01-W03\n"
    _model_git(guard, monkeypatch, is_ancestor=1, new_side=[squash])

    assert guard.check_rewrite(_OLD, _NEW) == (0, "")


def test_check_rewrite_refuses_a_squash_dropping_a_trailer(
    guard: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_git(guard, monkeypatch, is_ancestor=1, new_side=["feat: s\n\nEawf-Wave: P34-I01-W01\n"])

    code, diag = guard.check_rewrite(_OLD, _NEW)

    assert code == 1
    assert "drops the Eawf-Wave trailer of P34-I01-W02, P34-I01-W03" in diag


def test_check_rewrite_refuses_an_unknown_tip(guard: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _model_git(guard, monkeypatch, is_ancestor=128, base_rc=128)

    code, diag = guard.check_rewrite(_OLD, _NEW)

    assert code == 1
    assert "fetch the remote tip first" in diag


def test_check_rewrite_refuses_unreadable_ranges(
    guard: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_git(guard, monkeypatch, is_ancestor=1, new_side=None)

    code, diag = guard.check_rewrite(_OLD, _NEW)

    assert code == 1
    assert "cannot read the commits" in diag


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"PRE_COMMIT_FROM_REF": "0" * 40, "PRE_COMMIT_TO_REF": _NEW},
        {"PRE_COMMIT_FROM_REF": _OLD, "PRE_COMMIT_TO_REF": "0" * 40},
    ],
    ids=["no-variables", "new-ref", "deleted-ref"],
)
def test_check_pre_push_passes_without_history_to_replace(
    guard: Any, monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    _model_git(guard, monkeypatch, is_ancestor=1, new_side=[])

    assert guard.check_pre_push(env) == (0, "")


def test_check_pre_push_refuses_a_dropping_rewrite(
    guard: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_git(guard, monkeypatch, is_ancestor=1, new_side=[])
    env = {"PRE_COMMIT_FROM_REF": _OLD, "PRE_COMMIT_TO_REF": _NEW}

    code, diag = guard.check_pre_push(env)

    assert code == 1
    assert "P34-I01-W01, P34-I01-W02, P34-I01-W03" in diag


def test_trailer_task_keys_boundaries(guard: Any) -> None:
    assert guard.trailer_task_keys([]) == set()
    assert guard.trailer_task_keys(["fix: x\n\nsee Task: EAWF-0137 above\n"]) == set()
    assert guard.trailer_task_keys(
        ["fix: x\n\nTask: EAWF-0137\n", "fix: y\n\nTask: EAWF-0138\n"]
    ) == {"EAWF-0137", "EAWF-0138"}


_TASK_SIDE = [*_OLD_SIDE, "fix: c\n\nTask: EAWF-0137\n"]


def test_check_pre_push_accepts_a_rewrite_keeping_the_task_trailer(
    guard: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    squash = "".join(_TASK_SIDE)
    _model_git(guard, monkeypatch, is_ancestor=1, new_side=[squash], old_side=_TASK_SIDE)
    env = {"PRE_COMMIT_FROM_REF": _OLD, "PRE_COMMIT_TO_REF": _NEW}

    assert guard.check_pre_push(env) == (0, "")


def test_check_pre_push_refuses_a_rewrite_dropping_the_task_trailer(
    guard: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_git(guard, monkeypatch, is_ancestor=1, new_side=_OLD_SIDE, old_side=_TASK_SIDE)
    env = {"PRE_COMMIT_FROM_REF": _OLD, "PRE_COMMIT_TO_REF": _NEW}

    code, diag = guard.check_pre_push(env)

    assert code == 1
    assert "drops the Task trailer of EAWF-0137" in diag
    assert "Eawf-Wave" not in diag.split(".", 1)[0]


def test_main_check_rewrite_rejects_wrong_arity(
    lint: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    assert lint.main(["lint", "--check-rewrite", _OLD]) == 1
    assert "usage: commit_prefix_lint.py --check-rewrite" in capsys.readouterr().err


def test_main_pre_push_lint_passes_without_variables(
    lint: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PRE_COMMIT_FROM_REF", raising=False)
    monkeypatch.delenv("PRE_COMMIT_TO_REF", raising=False)

    assert lint.main(["lint", "--pre-push-lint"]) == 0


# ---------------------------------------------------------------------------
# The Batch regime a pushed commit is held to
# ---------------------------------------------------------------------------

# The real message of 137db4bc8: a Task-trailered scratch-era commit that names
# no Batch, which the pushed-commit lint must refuse.
_NO_BATCH_MESSAGE = (
    "fix: check a plugin bundle named by a relative root\n\n"
    "publication_findings joined the root onto paths it had already\n"
    "joined, so the tag-only plugin release validate gate, which names\n"
    "the bundle relative to the checkout, read build/eawf-plugin twice.\n\n"
    "Task: EAWF-0298\n"
)
_GENERATION_ID = "gen-0123456789abcdef"


def _batch_message(*trailers: str) -> str:
    return "feat: land the batch\n\nWhy it lands.\n\n" + "\n".join(trailers) + "\n"


@pytest.fixture()
def records(lint: Any) -> Any:
    return lint.NativeRecords(
        batch_keys=frozenset({"BAT-0305", "BAT-0306"}),
        task_batches={
            "EAWF-0331": "BAT-0306",
            "EAWF-0332": "BAT-0306",
            "EAWF-0321": "BAT-0305",
            "EAWF-0399": None,
        },
    )


def test_batch_commit_accepts_one_batch_and_two_of_its_tasks(lint: Any, records: Any) -> None:
    message = _batch_message("Batch: BAT-0306", "Task: EAWF-0331", "Task: EAWF-0332")

    assert lint.batch_commit_error(message, ["src/eawf/x.py"], records) is None


def test_batch_commit_accepts_a_batch_without_task_trailers(lint: Any, records: Any) -> None:
    assert lint.batch_commit_error(_batch_message("Batch: BAT-0305"), ["a.py"], records) is None


def test_batch_commit_refuses_the_real_message_without_a_batch(lint: Any, records: Any) -> None:
    error = lint.batch_commit_error(_NO_BATCH_MESSAGE, ["src/eawf/x.py"], records)

    assert error is not None
    assert "carries no Batch trailers" in error


def test_batch_commit_refuses_two_batch_trailers(lint: Any, records: Any) -> None:
    message = _batch_message("Batch: BAT-0305", "Batch: BAT-0306")

    error = lint.batch_commit_error(message, ["a.py"], records)

    assert error is not None
    assert "carries 2 (BAT-0305, BAT-0306) Batch trailers" in error


def test_batch_commit_refuses_a_task_outside_the_named_batch(lint: Any, records: Any) -> None:
    message = _batch_message("Batch: BAT-0306", "Task: EAWF-0331", "Task: EAWF-0321")

    error = lint.batch_commit_error(message, ["a.py"], records)

    assert error == "Task trailer 'EAWF-0321' belongs to Batch 'BAT-0305', not 'BAT-0306'"


@pytest.mark.parametrize(
    ("trailers", "expected"),
    [
        (("Batch: BAT-0306", "Task: EAWF-0399"), "belongs to Batch None"),
        (("Batch: BAT-0306", "Task: EAWF-9999"), "names no Task of the canonical generation"),
        (("Batch: BAT-9999",), "names no Batch of the canonical generation"),
        (("Batch: BAT-306",), "is not a Batch key (BAT-<NNNN>)"),
        (("Batch: bat-0306",), "is not a Batch key (BAT-<NNNN>)"),
        (("Batch: BAT-03060",), "is not a Batch key (BAT-<NNNN>)"),
    ],
    ids=["task-without-batch", "unknown-task", "unknown-batch", "short-key", "lowercase", "long"],
)
def test_batch_commit_refuses_unresolved_records(
    lint: Any, records: Any, trailers: tuple[str, ...], expected: str
) -> None:
    error = lint.batch_commit_error(_batch_message(*trailers), ["a.py"], records)

    assert error is not None
    assert expected in error


def test_batch_commit_ignores_a_trailer_quoted_in_prose(lint: Any, records: Any) -> None:
    error = lint.batch_commit_error("feat: x\n\nsee Batch: BAT-0306 above\n", ["a.py"], records)

    assert error is not None
    assert "carries no Batch trailers" in error


@pytest.mark.parametrize("subject", ["wip", "", "[P34-W01] feat: x", "feature: x"])
def test_batch_commit_refuses_a_free_form_subject(lint: Any, records: Any, subject: str) -> None:
    error = lint.batch_commit_error(f"{subject}\n\nBatch: BAT-0306\n", ["a.py"], records)

    assert error is not None
    assert "subject is not '<type>: <summary>'" in error


def test_batch_commit_refuses_an_unmarked_canonical_root(lint: Any) -> None:
    error = lint.batch_commit_error(_batch_message("Batch: BAT-0306"), ["a.py"], None)

    assert error == "Batch trailer 'BAT-0306': the canonical root is not marked epoch 2"


def test_state_commit_needs_no_batch_and_stages_only_state_paths(lint: Any, records: Any) -> None:
    state_paths = [f".ea/generations/{_GENERATION_ID}/state.json", ".ea/epoch2-opt-in.json"]

    assert lint.batch_commit_error("state: record the push\n", state_paths, records) is None
    assert lint.batch_commit_error("state: record the push\n", [], records) is None
    error = lint.batch_commit_error("state: x\n", [*state_paths, "src/eawf/x.py"], records)
    assert error == "state commit touches non-state paths: ['src/eawf/x.py']"
    # Off an epoch-2 root the generations tree is not a committed state path.
    assert lint.batch_commit_error("state: x\n", state_paths, None) is not None


def test_batch_commit_skips_the_commit_cap(
    lint: Any, records: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(**_kw: Any) -> tuple[int, str]:
        raise AssertionError("the cap must not run for a Batch commit")

    monkeypatch.setattr(lint, "_check_commit_cap", refuse)

    assert lint.batch_commit_error(_batch_message("Batch: BAT-0306"), ["a.py"], records) is None


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
    message = _batch_message("Batch: BAT-0306", "Task: EAWF-0331", "Task: EAWF-0332")
    assert lint.batch_commit_error(message, ["a.py"], loaded) is None


def test_load_native_records_boundaries_and_errors(tmp_path: Path, lint: Any) -> None:
    empty = lint.load_native_records(_write_generation(tmp_path / "empty", {"batch": {}}))
    assert empty.batch_keys == frozenset()
    assert empty.task_batches == {}
    with pytest.raises(ValueError, match="'batch' must be an object"):
        lint.load_native_records(_write_generation(tmp_path / "bad", {"batch": []}))
    with pytest.raises(OSError):
        lint.load_native_records(tmp_path / "missing")
