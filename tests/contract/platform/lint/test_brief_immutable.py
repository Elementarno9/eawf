"""EAWF028, the brief-immutability guard, over fixtures, a staged index and the real history.

Requirement row proved here, by id:

- ``AUTH-003``: a committed research brief is immutable evidence. A staged edit that
  only repairs its format passes; one that changes its wording is refused with
  ``brief_immutable`` and the superseding-brief remediation.

The fire-proof cases replay this repository's own history: the brief edits that changed
wording are refused, and the files the markdown and unwrap repairs touched without
changing a word pass.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from eawf.platform.lint import eawf028_brief_immutable as eawf028
from eawf.platform.lint.eawf028_brief_immutable import (
    BRIEF_ROOT,
    REFUSAL_CODE,
    RULE_CODE,
    check_brief,
    staged_edits,
    staged_texts,
    wording_hash,
    words,
)
from eawf.surfaces.cli.app import app

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[4]

BRIEF = (
    "# Daemon topology\n"
    "\n"
    "The daemon owns every write to the state file, and a client reaches it through one\n"
    "socket per tree.\n"
    "\n"
    "```\n"
    "eawf daemon start\n"
    "```\n"
    "\n"
    "| Verb | Effect |\n"
    "|---|---|\n"
    "| `a|b` | pipes |\n"
)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout


# --- the word sequence ------------------------------------------------------


@pytest.mark.parametrize(
    "repaired",
    [
        pytest.param(BRIEF.replace("one\nsocket", "one socket"), id="rewrap"),
        pytest.param(BRIEF.replace("\n", "  \n"), id="trailing-spaces"),
        pytest.param(BRIEF.replace("\n", "\r\n"), id="crlf"),
        pytest.param(BRIEF.replace("\n\n", "\n\n\n\n"), id="blank-line-run"),
        pytest.param(BRIEF.replace("```\neawf", "```bash\neawf"), id="fence-language"),
        pytest.param(BRIEF.replace("`a|b`", "`a\\|b`"), id="escaped-pipe"),
        pytest.param(BRIEF.replace("|---|---|", "| :--- | ---: |"), id="delimiter-row"),
    ],
)
def test_a_format_repair_keeps_the_wording_hash(repaired: str) -> None:
    assert repaired != BRIEF
    assert wording_hash(repaired) == wording_hash(BRIEF)
    assert check_brief("brief.md", BRIEF, repaired) is None


@pytest.mark.parametrize(
    "edited",
    [
        pytest.param(BRIEF.replace("every write", "most writes"), id="reworded"),
        pytest.param(BRIEF + "\nA later finding.\n", id="appended"),
        pytest.param(BRIEF.replace("# Daemon topology\n", ""), id="removed"),
        pytest.param(BRIEF.replace("one\nsocket", "onesocket"), id="joined-words"),
        pytest.param(BRIEF.replace("`a|b`", "`ab`"), id="pipe-dropped-from-a-word"),
    ],
)
def test_a_wording_edit_is_refused_as_brief_immutable(edited: str) -> None:
    finding = check_brief("brief.md", BRIEF, edited)
    assert finding is not None
    rendered = finding.render()
    assert rendered.startswith(f"brief.md: {RULE_CODE} {REFUSAL_CODE}: wording changed")
    assert "file a superseding brief" in rendered
    assert BRIEF_ROOT in rendered


def test_the_finding_quotes_the_first_changed_words() -> None:
    finding = check_brief("brief.md", BRIEF, BRIEF.replace("every write", "most writes"))
    assert finding is not None
    assert (finding.before, finding.after) == ("every write", "most writes")


def test_the_quoted_span_is_capped_at_eight_words() -> None:
    added = " ".join(f"w{n}" for n in range(20))
    finding = check_brief("brief.md", "", added)
    assert finding is not None
    assert finding.before == ""
    assert finding.after.split() == [f"w{n}" for n in range(8)]


def test_empty_and_whitespace_only_texts_share_one_hash() -> None:
    assert words("") == []
    assert wording_hash("") == wording_hash(" \n\r\n\t\n")
    assert check_brief("brief.md", "", "\n\n") is None


def test_a_single_word_brief_is_compared_word_for_word() -> None:
    assert check_brief("brief.md", "evidence", " evidence\n") is None
    assert check_brief("brief.md", "evidence", "evidence.") is not None


def test_a_fence_close_and_a_code_line_keep_their_text() -> None:
    assert words("```python\nx = 1\n```\n") == ["```", "x", "=", "1", "```"]
    assert words("~~~ text\n~~~\n") == ["~~~", "~~~"]


# --- the staged index -------------------------------------------------------


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    brief = root / BRIEF_ROOT / "2026-05-16-topology.md"
    brief.parent.mkdir(parents=True)
    brief.write_text(BRIEF, encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "guide.md").write_text("guide\n", encoding="utf-8")
    _git(root.parent, "init", "-q", str(root))
    _git(root, "config", "user.email", "ci@example.com")
    _git(root, "config", "user.name", "test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _stage(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _git(root, "add", "-A")


_TOPOLOGY = f"{BRIEF_ROOT}2026-05-16-topology.md"


def _hook(root: Path) -> Result:
    return runner.invoke(app, ["-w", str(root), "hook", "eawf028-brief-immutable"])


def test_a_staged_format_repair_is_clean(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _stage(root, _TOPOLOGY, BRIEF.replace("```\neawf", "```bash\neawf").replace("\n", "\r\n"))
    assert staged_edits(root) == [(_TOPOLOGY, _TOPOLOGY)]
    result = _hook(root)
    assert result.exit_code == 0, result.output
    assert result.stdout == "eawf028-brief-immutable: clean (1 file(s) scanned)\n"


def test_a_staged_wording_edit_is_refused(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _stage(root, _TOPOLOGY, BRIEF.replace("every write", "most writes"))
    result = _hook(root)
    assert result.exit_code == 1
    assert (
        f"{_TOPOLOGY}: {RULE_CODE} {REFUSAL_CODE}: wording changed ('every write' -> 'most writes')"
    ) in result.output


def test_a_renamed_brief_is_compared_to_its_committed_path(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    moved = f"{BRIEF_ROOT}long-term/2026-05-16-topology.md"
    (root / moved).parent.mkdir()
    _git(root, "mv", _TOPOLOGY, moved)
    _stage(root, moved, BRIEF.replace("every write", "most writes"))
    assert staged_edits(root) == [(_TOPOLOGY, moved)]
    assert staged_texts(root, _TOPOLOGY, moved)[0] == BRIEF
    result = _hook(root)
    assert result.exit_code == 1
    assert f"{moved}: {RULE_CODE} {REFUSAL_CODE}" in result.output


def test_new_briefs_other_files_and_removals_are_out_of_scope(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _stage(root, f"{BRIEF_ROOT}2026-09-30-superseding.md", "A new brief.\n")
    _stage(root, f"{BRIEF_ROOT}notes.txt", "not markdown\n")
    _stage(root, "docs/guide.md", "a reworded guide\n")
    assert staged_edits(root) == []
    assert _hook(root).exit_code == 0
    _git(root, "commit", "-q", "-m", "more")
    _git(root, "rm", "-q", _TOPOLOGY)
    _stage(root, f"{BRIEF_ROOT}notes.txt", "still not markdown\n")
    assert staged_edits(root) == []


def test_outside_a_git_tree_the_guard_raises(tmp_path: Path) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        staged_edits(tmp_path)


# --- the real history -------------------------------------------------------

_LONG_TERM = f"{BRIEF_ROOT}long-term/"

#: Brief edits in this repository's history that changed wording: nuggets folded into a
#: committed backlog brief, and a name replaced across the record.
_WORDING_EDITS = [
    ("4c064f43a", "2026-05-18-future-ideas.md"),
    ("ade580161", "2026-05-16-c00-spec-index.md"),
    ("ade580161", "2026-05-17-c10-operations.md"),
]

#: The files the unwrap and the markdown repair touched without changing a word.
_FORMAT_REPAIRS = [
    *(
        ("ba2447e40", name)
        for name in (
            "2026-05-16-c00-spec-index.md",
            "2026-05-16-c01-foundations.md",
            "2026-05-16-c05-cli-surface.md",
            "2026-05-16-c08-configurability-profiles.md",
            "2026-05-17-c06-operator-surface.md",
            "2026-05-17-c11-external-integrations.md",
            "2026-05-17-spec-series-combined-audit.md",
            "2026-05-18-c12-implementation-rollup.md",
            "2026-05-18-future-ideas.md",
        )
    ),
    *(
        ("f3576855b", name)
        for name in (
            "2026-05-16-c01-foundations.md",
            "2026-05-16-c02-daemon-topology.md",
            "2026-05-16-c08-configurability-profiles.md",
            "2026-05-17-c06-operator-surface.md",
            "2026-05-17-c09-quality-observability.md",
            "2026-05-17-c10-operations.md",
            "2026-05-17-spec-series-combined-audit.md",
            "2026-05-18-c12-implementation-rollup.md",
            "2026-05-18-future-ideas.md",
        )
    ),
]


def _history(commit: str, name: str) -> tuple[str, str]:
    path = f"{_LONG_TERM}{name}"
    return _git(REPO_ROOT, "show", f"{commit}^:{path}"), _git(REPO_ROOT, "show", f"{commit}:{path}")


@pytest.mark.parametrize(("commit", "name"), _WORDING_EDITS)
def test_a_past_wording_edit_is_refused(commit: str, name: str) -> None:
    before, after = _history(commit, name)
    assert eawf028.check_brief(name, before, after) is not None


@pytest.mark.parametrize(("commit", "name"), _FORMAT_REPAIRS)
def test_a_past_format_repair_passes(commit: str, name: str) -> None:
    before, after = _history(commit, name)
    assert before != after
    assert eawf028.check_brief(name, before, after) is None
