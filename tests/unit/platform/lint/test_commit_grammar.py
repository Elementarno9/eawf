"""LINT-006, LINT-009, LINT-010, LINT-031: the commit grammar as written after the switch.

``tools/commit_prefix_lint.py`` reads two grammars during the transition:
the trailer form (``<type>: <summary>`` with a ``Task: <KEY>`` trailer)
and the task-prefix form (``[<KEY>] <type>: <summary>``). These cases
need no lifecycle state: the trailer's position, the bookkeeping path set
keyed by conventional type, and the history mode that reads what the
grammar used to be. Proving a named Task against state lives in
``tests/contract/platform/lint/test_commit_task_existence.py``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO = Path(__file__).resolve().parents[4]
_TOOL_DIR = _REPO / "tools"
_COAUTHOR = "Co-Authored-By: Claude <noreply@anthropic.com>"


def _load(name: str) -> Any:
    if str(_TOOL_DIR) not in sys.path:
        sys.path.insert(0, str(_TOOL_DIR))
    spec = importlib.util.spec_from_file_location(name, _TOOL_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def lint() -> Any:
    return _load("commit_prefix_lint")


@pytest.fixture()
def history(lint: Any) -> Any:
    return _load("commit_grammar_history")


def _run(lint: Any, tmp_path: Path, text: str, staged: list[str]) -> tuple[int, str]:
    message = tmp_path / "COMMIT_EDITMSG"
    message.write_text(text, encoding="utf-8")
    return lint.lint(
        message,
        staged,
        env={"EAWF_COAUTHOR_MODE": "disabled"},
        # An absent state file reads as nothing in flight, so no lifecycle
        # the checkout happens to carry decides these cases.
        state_path=tmp_path / "missing-state.json",
        subject_style="trailer",
    )


# --- LINT-006: the Task trailer is the last trailer before the co-author ones ---


@pytest.mark.parametrize(
    "text",
    [
        "feat: a\n\nbody\n\nTask: EAWF-0001\n",
        f"feat: a\n\nTask: EAWF-0001\n{_COAUTHOR}\n",
        f"feat: a\n\nTask: EAWF-0001\n\n{_COAUTHOR}\n",
        f"feat: a\n\nEawf-Wave: P02-I01-W01\nTask: EAWF-0001\n{_COAUTHOR}\n{_COAUTHOR}\n",
        "feat: a\n\nTask: EAWF-0001\n# Please enter the commit message\n",
        "feat: a\n\nTask: EAWF-0001\n# ------------------------ >8 ------------------------\n+x\n",
        "feat: a\n\nno task named here\n",
    ],
)
def test_lint_006_a_task_trailer_in_place_has_no_position_error(lint: Any, text: str) -> None:
    assert lint.task_trailer_position_error(text) is None


@pytest.mark.parametrize(
    ("text", "said"),
    [
        ("feat: a\n\nTask: EAWF-0001\nEawf-Wave: P02-I01-W01\n", "is followed by"),
        ("feat: a\n\nTask: EAWF-0001\n\nmore body after it\n", "is followed by"),
        (f"feat: a\n\n{_COAUTHOR}\nTask: EAWF-0001\n", "follows a co-author trailer"),
        ("feat: a\n\nTask: EAWF-0001\nTask: EAWF-0002\n", "repeated"),
        ("feat: a\n\nTask: EAWF-0001\n\nTask: EAWF-0001\n", "repeated"),
    ],
)
def test_lint_006_a_misplaced_or_repeated_task_trailer_is_named(
    lint: Any, text: str, said: str
) -> None:
    error = lint.task_trailer_position_error(text)

    assert error is not None
    assert said in error


def test_lint_006_the_commit_msg_hook_rejects_a_misplaced_task_trailer(
    lint: Any, tmp_path: Path
) -> None:
    """Gate fire: position is checked before the Task is looked up at all."""
    code, diag = _run(
        lint, tmp_path, "fix: land it\n\nTask: EAWF-0001\nReviewed-By: someone\n", ["src/x.py"]
    )

    assert code == 1
    assert "Task trailer out of place" in diag


# --- LINT-009: bookkeeping is recognised by its type, its path set is data ------


def test_lint_009_a_bare_state_commit_meets_the_bookkeeping_path_set(
    lint: Any, tmp_path: Path
) -> None:
    """Gate fire: a plain ``state:`` subject used to skip the whitelist entirely."""
    code, diag = _run(lint, tmp_path, "state: start the run\n", [".ea/state.json", "src/x.py"])

    assert code == 1
    assert "state-type commit touches non-state paths: ['src/x.py']" in diag


def test_lint_009_a_bare_state_commit_admits_bookkeeping_paths(lint: Any, tmp_path: Path) -> None:
    """The same set the bracketed ``[P##] state:`` form has always met."""
    staged = [".ea/state.json", ".ea/store/audit.jsonl", ".secrets.baseline"]

    code, diag = _run(lint, tmp_path, "state: record the move\n", staged)

    assert code == 0, diag


def test_lint_009_a_deliverable_type_carries_any_path(lint: Any, tmp_path: Path) -> None:
    code, diag = _run(lint, tmp_path, "feat: add it\n", ["src/x.py", ".ea/state.json"])

    assert code == 0, diag


def test_lint_009_the_bookkeeping_path_set_is_declared_as_data(lint: Any) -> None:
    assert ".ea/state.json" in lint._STATE_ONLY_ALLOWED
    assert ".ea/store/" in lint._STATE_ONLY_PREFIXES
    assert ".ea/generations/" in lint._EPOCH2_STATE_PREFIXES


# --- LINT-010: history written under the epoch-1 grammar stays lintable -------


@pytest.mark.parametrize(
    "subject",
    [
        "[P12-CORE] polish: tidy the header",
        "[P08-I02-CORE] state: close the iter",
        "[CORE] chore: bump",
        "[P03] fix: an old bare-phase fix",
        "[P05-W02] style: format",
        "[P01] Phase one kickoff",
        "[P30-I04-W03] feat: a wave commit",
    ],
)
def test_lint_010_an_epoch1_subject_predating_the_switch_passes_silently(
    history: Any, subject: str
) -> None:
    assert history.history_verdict(f"{subject}\n", predates_switch=True) == (0, "")


def test_lint_010_the_retired_suffix_after_the_switch_is_rejected(history: Any) -> None:
    code, diag = history.history_verdict("[P12-CORE] polish: tidy\n", predates_switch=False)

    assert code == 1
    assert "matches no commit grammar" in diag


def test_lint_010_a_subject_no_era_accepts_is_rejected_before_the_switch(history: Any) -> None:
    code, diag = history.history_verdict("tidy things up\n", predates_switch=True)

    assert code == 1
    assert "no epoch-1 commit grammar" in diag


@pytest.mark.parametrize(
    ("message", "warning"),
    [
        ("feat: x\n\nTask: EAWF-0001\n", ""),
        ("[P36] state: bake the release\n", ""),
        ("[P36-I01-W02] feat: x\n", "deprecated bracket-prefix wave subject"),
        ("[EAWF-0001] feat: x\n", "deprecated task-prefix subject"),
    ],
)
def test_lint_010_a_commit_after_the_switch_is_held_to_the_current_grammar(
    history: Any, message: str, warning: str
) -> None:
    code, diag = history.history_verdict(message, predates_switch=False)

    assert code == 0
    assert diag.startswith(warning) if warning else diag == ""


def test_lint_010_a_misplaced_task_trailer_after_the_switch_is_rejected(history: Any) -> None:
    code, diag = history.history_verdict(
        "feat: x\n\nTask: EAWF-0001\nFoo: bar\n", predates_switch=False
    )

    assert code == 1
    assert "out of place" in diag


def test_lint_010_check_history_is_reachable_from_the_hook_entry_point(lint: Any) -> None:
    assert lint.main(["commit_prefix_lint.py", "--check-history"]) == 1
