"""LINT-008, LINT-031: a commit names a Task that exists and is live, in either grammar.

The generation here is the real cutover's output with native Tasks
planted beside the imported rows (the fixtures of
``tests/unit/test_commit_prefix_lint_epoch2.py``), so existence is proved
against the importer's own shape rather than a hand-written imitation.
Both grammars are read: the trailer form, which is the default and
passes silently, and the task-prefix form, which passes with a warning
while the trailer form is configured and silently once the prefix form is.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.unit.test_commit_prefix_lint_epoch2 import cut, marked, mod, native

__all__ = ["cut", "marked", "mod", "native"]

_COAUTHOR = "Co-Authored-By: Claude <noreply@anthropic.com>"


def _lint(
    mod: Any,
    tmp_path: Path,
    ea_dir: Path,
    text: str,
    *,
    subject_style: str | None = "trailer",
) -> tuple[int, str]:
    message = tmp_path / "COMMIT_EDITMSG"
    message.write_text(text, encoding="utf-8")
    state = ea_dir / "state.json"
    return mod.lint(
        message,
        ["src/eawf/x.py"],
        env={},
        state_path=state,
        repo_root=ea_dir.parent,
        subject_style=subject_style,
        canonical_state_path=state,
    )


# --- LINT-008: a Task that does not exist in state is rejected ------------------


@pytest.mark.parametrize(
    "text",
    [
        f"fix: land it\n\nTask: EAWF-0999\n{_COAUTHOR}\n",
        f"[EAWF-0999] fix: land it\n\n{_COAUTHOR}\n",
    ],
)
def test_lint_008_a_commit_naming_a_nonexistent_task_is_rejected(
    mod: Any, tmp_path: Path, native: Path, text: str
) -> None:
    """Gate fire in both grammars: the key is well formed and names nobody's work."""
    code, diag = _lint(mod, tmp_path, native, text)

    assert code == 1
    assert "Task trailer rejected: 'EAWF-0999'" in diag
    assert "no such Task" in diag


def test_lint_008_a_task_that_exists_but_is_not_live_is_rejected(
    mod: Any, tmp_path: Path, native: Path
) -> None:
    code, diag = _lint(mod, tmp_path, native, f"[EAWF-0139] fix: land it\n\n{_COAUTHOR}\n")

    assert code == 1
    assert "status 'PLANNED'" in diag


# --- LINT-031: both grammars, a configurable default, the position, existence ----


def test_lint_031_the_trailer_form_validates_silently(
    mod: Any, tmp_path: Path, native: Path
) -> None:
    assert _lint(mod, tmp_path, native, f"feat: land it\n\nTask: EAWF-0137\n{_COAUTHOR}\n") == (
        0,
        "",
    )


def test_lint_031_the_prefix_form_validates_with_a_warning_under_the_trailer_default(
    mod: Any, tmp_path: Path, native: Path
) -> None:
    code, diag = _lint(mod, tmp_path, native, f"[EAWF-0138] feat: land it\n\n{_COAUTHOR}\n")

    assert code == 0
    assert diag.startswith("deprecated task-prefix subject: '[EAWF-0138] feat: land it'")
    assert "'Task: <KEY>' trailer" in diag


def test_lint_031_the_default_grammar_is_read_from_configuration(
    mod: Any, tmp_path: Path, native: Path
) -> None:
    """With the prefix form configured it passes silently; unset, the trailer form rules."""
    text = f"[EAWF-0137] feat: land it\n\n{_COAUTHOR}\n"
    unset = _lint(mod, tmp_path, native, text, subject_style=None)
    (native / "config.yaml").write_text(
        "vcs:\n  conventions:\n    subject_style: bracket\n", encoding="utf-8"
    )
    configured = _lint(mod, tmp_path, native, text, subject_style=None)

    assert unset[0] == 0
    assert unset[1].startswith("deprecated task-prefix subject")
    assert configured == (0, "")


def test_lint_031_the_trailer_position_is_enforced_on_a_live_task(
    mod: Any, tmp_path: Path, native: Path
) -> None:
    code, diag = _lint(
        mod, tmp_path, native, f"feat: land it\n\nTask: EAWF-0137\nSee-Also: x\n{_COAUTHOR}\n"
    )

    assert code == 1
    assert "Task trailer out of place" in diag


def test_lint_031_a_prefix_and_trailer_naming_different_tasks_are_rejected(
    mod: Any, tmp_path: Path, native: Path
) -> None:
    code, diag = _lint(
        mod, tmp_path, native, f"[EAWF-0137] feat: land it\n\nTask: EAWF-0138\n{_COAUTHOR}\n"
    )

    assert code == 1
    assert "subject/trailer task mismatch" in diag


def test_lint_031_a_prefix_and_trailer_naming_one_task_agree(
    mod: Any, tmp_path: Path, native: Path
) -> None:
    code, _ = _lint(
        mod, tmp_path, native, f"[EAWF-0137] feat: land it\n\nTask: EAWF-0137\n{_COAUTHOR}\n"
    )

    assert code == 0


def test_lint_031_a_prior_prefix_form_commit_caps_the_task(
    mod: Any, tmp_path: Path, native: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One commit per Task counts a commit in either grammar."""
    prior = "e" * 40
    monkeypatch.setattr(
        mod, "_prior_wave_commits", lambda terms, **_kw: [prior] if "[EAWF-0137]" in terms else []
    )

    code, diag = _lint(mod, tmp_path, native, f"feat: more\n\nTask: EAWF-0137\n{_COAUTHOR}\n")

    assert code == 1
    assert f"second commit for task EAWF-0137: {prior[:12]}" in diag


def test_lint_031_a_prefix_on_an_unmarked_root_is_rejected(mod: Any, tmp_path: Path) -> None:
    ea_dir = tmp_path / "plain" / ".ea"
    ea_dir.mkdir(parents=True)

    code, diag = _lint(mod, tmp_path, ea_dir, f"[EAWF-0137] feat: land it\n\n{_COAUTHOR}\n")

    assert code == 1
    assert "this root is not marked epoch 2" in diag
