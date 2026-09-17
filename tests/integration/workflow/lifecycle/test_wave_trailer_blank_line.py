"""The wave-SHA index reads ``Eawf-Wave`` the way the commit lint does.

``tools/commit_prefix_lint.py`` accepts any line-anchored ``Eawf-Wave:`` line
anywhere in the message. Git's own trailer parser only reads the final
paragraph, so a commit that puts a blank line between ``Eawf-Wave:`` and
``Co-Authored-By:`` passed the lint yet resolved to no wave in
:mod:`eawf.workflow.lifecycle.wave_sha`, which left its pin unrepairable.

These cases build throwaway repositories under ``tmp_path`` and pin:

- the blank-line shape resolves in :func:`build_wave_sha_index`, the
  reachable-key map and the first-parent candidates;
- a mid-sentence mention resolves to nothing;
- the index and the lint agree on a table of message shapes, both on raw text
  and on the messages git actually stores.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from eawf.workflow.lifecycle import wave_sha
from eawf.workflow.lifecycle.wave_sha import (
    _body_wave_ids,
    _first_parent_wave_candidates,
    _parse_index,
    _reachable_wave_keys,
    build_wave_sha_index,
    commit_matches_wave,
    derive_wave_sha,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git required")

_LINT_PATH = Path(__file__).resolve().parents[4] / "tools" / "commit_prefix_lint.py"

_BLANK_LINE_MESSAGE = (
    "feat: land the blank-line trailer shape\n"
    "\n"
    "The trailer paragraph is followed by a blank line.\n"
    "\n"
    "Eawf-Wave: P32-I01-W02\n"
    "\n"
    "Co-Authored-By: Test Agent <agent@example.invalid>\n"
)
_MID_SENTENCE_MESSAGE = (
    "docs: mention a wave in prose\n\nThis follows Eawf-Wave: P32-I01-W03 from the prior phase.\n"
)
_SQUASH_MESSAGE = (
    "fix: squash\n"
    "\n"
    "body\n"
    "\n"
    "Eawf-Wave: P32-I01-W47\n"
    "Eawf-Wave: P32-I01-W50\n"
    "\n"
    "Eawf-Wave: P32-I01-W52\n"
)

# (case id, message, wave ids the index must read, in order)
_SHAPES: list[tuple[str, str, list[str]]] = [
    ("trailer-block", "feat: x\n\nEawf-Wave: P32-I01-W02\n", ["P32-I01-W02"]),
    ("blank-line-before-coauthor", _BLANK_LINE_MESSAGE, ["P32-I01-W02"]),
    (
        "coauthor-same-block",
        "feat: x\n\nEawf-Wave: P32-I01-W02\nCo-Authored-By: Test Agent <agent@example.invalid>\n",
        ["P32-I01-W02"],
    ),
    ("mid-sentence", _MID_SENTENCE_MESSAGE, []),
    ("trailing-prose", "feat: x\n\nEawf-Wave: P32-I01-W02 and more\n", []),
    ("indented", "feat: x\n\n  Eawf-Wave: P32-I01-W02\n", []),
    ("lowercase-key", "feat: x\n\neawf-wave: P32-I01-W02\n", []),
    ("no-space-after-colon", "feat: x\n\nEawf-Wave:P32-I01-W02\n", []),
    ("zero-wave", "feat: x\n\nEawf-Wave: P32-I01-W00\n", []),
    ("zero-iter", "feat: x\n\nEawf-Wave: P32-I00-W02\n", []),
    ("one-digit-wave", "feat: x\n\nEawf-Wave: P32-I01-W2\n", []),
    ("trailing-whitespace", "feat: x\n\nEawf-Wave: P32-I01-W02   \n", ["P32-I01-W02"]),
    ("short-form", "feat: x\n\nEawf-Wave: P32-W02\n", ["P32-I01-W02"]),
    ("wide-ids", "feat: x\n\nEawf-Wave: P100-I10-W100\n", ["P100-I10-W100"]),
    ("bracket-subject-only", "[P32-I01-W02] feat: x\n\nbody\n", []),
    ("empty", "", []),
    ("no-trailing-newline", "feat: x\n\nEawf-Wave: P32-I01-W02", ["P32-I01-W02"]),
    ("squash-several", _SQUASH_MESSAGE, ["P32-I01-W47", "P32-I01-W50", "P32-I01-W52"]),
    (
        "duplicate-line",
        "feat: x\n\nEawf-Wave: P32-I01-W02\nEawf-Wave: P32-I01-W02\n",
        ["P32-I01-W02"],
    ),
]


@pytest.fixture(scope="module")
def lint() -> ModuleType:
    """Load ``tools/commit_prefix_lint.py`` the way its own unit tests do."""
    tool_dir = str(_LINT_PATH.parent)
    if tool_dir not in sys.path:
        sys.path.insert(0, tool_dir)
    spec = importlib.util.spec_from_file_location("commit_prefix_lint", _LINT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["commit_prefix_lint"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return an empty repository that ignores the developer's git config."""
    global_config = tmp_path / "gitconfig"
    global_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "commit.gpgsign", "false")
    return root


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _commit(root: Path, *, name: str, message: str) -> str:
    """Commit one new file with *message* verbatim and return the SHA."""
    (root / name).write_text(f"{name}\n", encoding="utf-8")
    _git(root, "add", name)
    message_path = root.parent / f"{name}.msg"
    message_path.write_text(message, encoding="utf-8")
    _git(
        root, "commit", "-q", "--cleanup=verbatim", "--allow-empty-message", "-F", str(message_path)
    )
    return _git(root, "rev-parse", "HEAD").strip()


def _lint_wave_ids(lint: ModuleType, message: str) -> list[str]:
    """Return every wave the lint's trailer pattern names, normalised by the lint."""
    ids: list[str] = []
    for match in lint._WAVE_TRAILER_RE.finditer(message):
        ref = lint._trailer_scope_ref(match.group(0))
        assert ref is not None
        if ref.wave_id not in ids:
            ids.append(ref.wave_id)
    return ids


# ---- the blank-line shape resolves ------------------------------------------


def test_build_wave_sha_index_blank_line_trailer_resolves_wave(repo: Path) -> None:
    """The shape git's trailer parser misses is indexed under its wave id."""
    sha = _commit(repo, name="a.txt", message=_BLANK_LINE_MESSAGE)
    git_trailers = _git(repo, "log", "-1", "--format=%(trailers:key=Eawf-Wave,valueonly)", sha)
    assert git_trailers.strip() == ""

    index = build_wave_sha_index(repo)

    assert index["P32-I01-W02"] == sha
    assert derive_wave_sha("P32-I01-W02", index=index) == sha


def test_first_parent_wave_candidates_blank_line_trailer_resolves_wave(repo: Path) -> None:
    """The repair scan's two indexes both see the blank-line commit."""
    sha = _commit(repo, name="a.txt", message=_BLANK_LINE_MESSAGE)

    assert _first_parent_wave_candidates(repo) == {"P32-I01-W02": [sha]}
    assert _reachable_wave_keys(repo) == {sha: {"P32-I01-W02"}}


def test_commit_matches_wave_blank_line_trailer_matches(repo: Path) -> None:
    sha = _commit(repo, name="a.txt", message=_BLANK_LINE_MESSAGE)

    assert commit_matches_wave(sha, "P32-I01-W02", repo_root=repo) is True
    assert commit_matches_wave(sha, "P32-I01-W03", repo_root=repo) is False


def test_first_parent_wave_candidates_squash_indexes_every_wave(repo: Path) -> None:
    """A squash commit naming several waves is a candidate for each of them."""
    sha = _commit(repo, name="a.txt", message=_SQUASH_MESSAGE)

    candidates = _first_parent_wave_candidates(repo)
    index = build_wave_sha_index(repo)

    for wave_id in ("P32-I01-W47", "P32-I01-W50", "P32-I01-W52"):
        assert candidates[wave_id] == [sha]
        assert index[wave_id] == sha


def test_build_wave_sha_index_keeps_bracket_subject_and_trailer(repo: Path) -> None:
    """A bracket subject still indexes alongside a body trailer."""
    sha = _commit(
        repo,
        name="a.txt",
        message="[P32-W05] feat: legacy subject\n\nEawf-Wave: P32-I01-W05\n\nnote\n",
    )

    index = build_wave_sha_index(repo)

    assert index["[P32-W05]"] == sha
    assert index["P32-I01-W05"] == sha
    assert _reachable_wave_keys(repo) == {sha: {"[P32-W05]", "P32-I01-W05"}}


# ---- a mid-sentence mention resolves to nothing -----------------------------


def test_build_wave_sha_index_mid_sentence_mention_resolves_nothing(repo: Path) -> None:
    sha = _commit(repo, name="a.txt", message=_MID_SENTENCE_MESSAGE)

    assert build_wave_sha_index(repo) == {}
    assert derive_wave_sha("P32-I01-W03", index=build_wave_sha_index(repo)) is None
    assert _first_parent_wave_candidates(repo) == {}
    assert _reachable_wave_keys(repo) == {sha: set()}


def test_commit_matches_wave_mid_sentence_and_longer_id_do_not_match(repo: Path) -> None:
    """Neither prose nor a wave id that merely starts with the target counts."""
    prose = _commit(repo, name="a.txt", message=_MID_SENTENCE_MESSAGE)
    longer = _commit(repo, name="b.txt", message="feat: x\n\nEawf-Wave: P32-I01-W030\n")

    assert commit_matches_wave(prose, "P32-I01-W03", repo_root=repo) is False
    assert commit_matches_wave(longer, "P32-I01-W03", repo_root=repo) is False
    assert commit_matches_wave(longer, "P32-I01-W030", repo_root=repo) is True


# ---- parity with the commit lint --------------------------------------------


def test_body_wave_ids_pattern_matches_commit_lint(lint: ModuleType) -> None:
    """The mirrored pattern is byte-identical to the lint's, flags included."""
    assert wave_sha._WAVE_TRAILER_RE.pattern == lint._WAVE_TRAILER_RE.pattern
    assert wave_sha._WAVE_TRAILER_RE.flags == lint._WAVE_TRAILER_RE.flags


@pytest.mark.parametrize(
    ("message", "expected"),
    [pytest.param(message, expected, id=case) for case, message, expected in _SHAPES],
)
def test_body_wave_ids_agrees_with_commit_lint(
    lint: ModuleType, message: str, expected: list[str]
) -> None:
    """Both parsers read the same waves, and the lint's scope wave comes first."""
    ids = _body_wave_ids(message)
    lint_ref = lint._trailer_scope_ref(message)

    assert ids == expected
    assert ids == _lint_wave_ids(lint, message)
    assert bool(ids) is lint._has_wave_trailer(message)
    assert ids[:1] == ([] if lint_ref is None else [lint_ref.wave_id])


def test_build_wave_sha_index_agrees_with_commit_lint_on_stored_messages(
    lint: ModuleType, repo: Path
) -> None:
    """Every stored message indexes exactly the waves the lint reads from it."""
    shas: dict[str, str] = {}
    for position, (case, message, _expected) in enumerate(_SHAPES):
        shas[case] = _commit(repo, name=f"{position:02d}.txt", message=message)

    reachable = _reachable_wave_keys(repo)
    candidates = _first_parent_wave_candidates(repo)
    index = build_wave_sha_index(repo)

    for case, _message, expected in _SHAPES:
        sha = shas[case]
        stored = _git(repo, "show", "-s", "--format=%B", sha)
        lint_ids = _lint_wave_ids(lint, stored)
        assert lint_ids == expected, case
        trailer_keys = {key for key in reachable[sha] if not key.startswith("[")}
        assert trailer_keys == set(lint_ids), case
        for wave_id in lint_ids:
            assert sha in candidates[wave_id], case
            assert wave_id in index, case


# ---- boundaries --------------------------------------------------------------


def test_body_wave_ids_blank_message_is_empty() -> None:
    assert _body_wave_ids("") == []
    assert _body_wave_ids("\n\n  \n") == []


def test_parse_index_body_with_field_separator_keeps_trailer() -> None:
    """A stray unit separator inside a body cannot shift the trailer out of reach."""
    record = "\x00" + "\x1f".join(
        (
            "a" * 40,
            "refs/heads/main",
            "feat: x",
            "feat: x\n\nodd \x1f byte\n\nEawf-Wave: P32-I01-W02\n",
        )
    )

    assert _parse_index(record) == {"P32-I01-W02": "a" * 40}


def test_reachable_wave_keys_git_failure_is_empty(tmp_path: Path) -> None:
    """Outside a repository both history walks degrade to empty maps."""
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()

    assert _reachable_wave_keys(not_a_repo) == {}
    assert _first_parent_wave_candidates(not_a_repo) == {}
