"""Tests for the EAWF024 test-tier contract lint.

Covers the banned-import rule (a ``tests/unit/`` file must not import
``subprocess`` / ``textual`` / ``CliRunner``) across boundary and error
paths, the ``# noqa: EAWF024`` waiver, and the ``is_unit_tier_path``
dispatcher predicate. The check is content-only, so these tests feed it
source snippets as strings -- the module itself imports nothing banned
and is clean under its own rule.

The second half covers the sibling tier contract that is enforced by
``tests/conftest.py`` rather than by a lint rule: no test may resolve the
repository's own ``.ea`` tree by fall-through, and every test runs under
its own ``EAWF_RUNTIME_DIR``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.platform.lint.eawf024_test_tier_contract import (
    RULE_CODE,
    TierViolation,
    check_source,
    is_unit_tier_path,
)
from tests.conftest import REPO_EA_DIR, REPO_ROOT, RepoStateAccessError, guard_repo_ea_path


def test_check_source_flags_plain_subprocess_import() -> None:
    findings = check_source("import subprocess\n")
    assert [f.imported for f in findings] == ["subprocess"]
    assert findings[0].lineno == 1
    assert findings[0].code == RULE_CODE


def test_check_source_flags_from_subprocess_import() -> None:
    findings = check_source("from subprocess import run\n")
    assert [f.imported for f in findings] == ["subprocess"]


def test_check_source_flags_plain_textual_import() -> None:
    findings = check_source("import textual\n")
    assert [f.imported for f in findings] == ["textual"]


def test_check_source_flags_dotted_textual_import() -> None:
    findings = check_source("from textual.widgets import Button\n")
    assert [f.imported for f in findings] == ["textual"]


def test_check_source_flags_clirunner_name_import() -> None:
    findings = check_source("from typer.testing import CliRunner\n")
    assert [f.imported for f in findings] == ["CliRunner"]


def test_check_source_flags_multiple_sorted_by_position() -> None:
    source = "import subprocess\nimport textual\nfrom typer.testing import CliRunner\n"
    findings = check_source(source)
    assert [(f.lineno, f.imported) for f in findings] == [
        (1, "subprocess"),
        (2, "textual"),
        (3, "CliRunner"),
    ]


def test_check_source_clean_import_yields_no_finding() -> None:
    # A single allowed import (boundary: exactly one non-banned import).
    assert check_source("import json\n") == []


def test_check_source_empty_source_yields_no_finding() -> None:
    # Boundary: empty file.
    assert check_source("") == []


def test_check_source_string_literal_mentioning_subprocess_not_flagged() -> None:
    # The AST scan inspects import statements only; a string that merely
    # names subprocess must never false-fire.
    assert check_source('X = "we call subprocess here"\n') == []


def test_check_source_noqa_waiver_clears_violation() -> None:
    findings = check_source("import subprocess  # noqa: EAWF024 deliberate fixture\n")
    assert findings == []


def test_check_source_noqa_waiver_only_clears_marked_line() -> None:
    source = "import subprocess  # noqa: EAWF024\nimport textual\n"
    findings = check_source(source)
    assert [f.imported for f in findings] == ["textual"]


def test_check_source_raises_on_unparseable() -> None:
    with pytest.raises(SyntaxError):
        check_source("def (:\n")


def test_render_contains_code_and_token() -> None:
    violation = TierViolation(lineno=3, col_offset=0, imported="textual")
    rendered = violation.render()
    assert RULE_CODE in rendered
    assert "textual" in rendered
    assert rendered.startswith("3:0:")


def test_is_unit_tier_path_accepts_unit_python_file() -> None:
    assert is_unit_tier_path("tests/unit/test_x.py")
    assert is_unit_tier_path("tests/unit/sub/test_y.py")


def test_is_unit_tier_path_rejects_other_tiers_and_non_python() -> None:
    assert not is_unit_tier_path("tests/integration/test_x.py")
    assert not is_unit_tier_path("tests/tui/test_x.py")
    assert not is_unit_tier_path("tests/unit/data.json")
    assert not is_unit_tier_path("src/eawf/foo.py")


def test_is_unit_tier_path_folds_backslashes() -> None:
    assert is_unit_tier_path("tests\\unit\\test_x.py")


def test_is_unit_tier_path_accepts_absolute_path() -> None:
    assert is_unit_tier_path("/tmp/repo/tests/unit/test_x.py")
    assert not is_unit_tier_path("/tmp/repo/tests/integration/test_x.py")


# --- repository .ea isolation contract --------------------------------------


def test_guard_repo_ea_path_raises_for_the_repo_state_file() -> None:
    with pytest.raises(RepoStateAccessError, match="repository's own state tree"):
        guard_repo_ea_path(REPO_EA_DIR / "state.json", origin="probe")


def test_guard_repo_ea_path_raises_for_the_ea_directory_itself() -> None:
    # Boundary: the guarded root, not a child of it.
    with pytest.raises(RepoStateAccessError):
        guard_repo_ea_path(REPO_EA_DIR, origin="probe")


def test_guard_repo_ea_path_allows_the_repo_root() -> None:
    # Off-by-one the other way: one level above the guarded root is fine.
    assert guard_repo_ea_path(REPO_ROOT, origin="probe") == REPO_ROOT


def test_guard_repo_ea_path_allows_a_name_prefixed_sibling() -> None:
    # ``.eawf`` starts with ``.ea``; a prefix match would false-fire here.
    sibling = REPO_ROOT / ".eawf" / "registry.json"
    assert guard_repo_ea_path(sibling, origin="probe") == sibling


def test_guard_repo_ea_path_allows_an_ea_dir_outside_the_repo(tmp_path: Path) -> None:
    candidate = tmp_path / ".ea" / "state.json"
    assert guard_repo_ea_path(candidate, origin="probe") == candidate


def test_guard_repo_ea_path_returns_the_path_unresolved(tmp_path: Path) -> None:
    # The guard is a pass-through: it must not normalise its caller's path.
    candidate = tmp_path / "sub" / ".." / ".ea"
    assert guard_repo_ea_path(candidate, origin="probe") == candidate


def test_guard_repo_ea_path_rejects_a_non_path_argument() -> None:
    with pytest.raises(TypeError):
        guard_repo_ea_path(3, origin="probe")  # type: ignore[arg-type]


def test_guard_repo_ea_path_names_the_origin_in_the_message() -> None:
    with pytest.raises(RepoStateAccessError, match="resolve_state_path"):
        guard_repo_ea_path(REPO_EA_DIR / "state.json", origin="resolve_state_path")


def test_repo_ea_guard_is_wired_into_a_remote_resolver_alias() -> None:
    """A module that did ``from ... import resolve_state_path`` sees the guard.

    ``functools.wraps`` leaves ``__wrapped__`` on the guarded callable and
    the bare resolver has none, so this distinguishes the rebound alias
    from the original without calling either.
    """
    from eawf.surfaces.cli.commands import roadmap

    assert getattr(roadmap.resolve_state_path, "__wrapped__", None) is not None


def test_repo_ea_guard_reds_on_a_resolver_falling_through_to_the_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard fires on the real defect: a resolve with nothing chosen.

    With no ``EA_STATE`` and no workspace, the pwd-upward walk finds the
    repository's own state on the first hop -- the accident that let tests
    read and write live project state.
    """
    from eawf.surfaces.cli.commands import roadmap

    monkeypatch.delenv("EA_STATE", raising=False)
    monkeypatch.chdir(REPO_ROOT)
    with pytest.raises(RepoStateAccessError):
        roadmap.resolve_state_path(None)


def test_repo_ea_guard_allows_a_deliberately_targeted_repo_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit ``EA_STATE`` is a choice, so the repo-census family still reads."""
    from eawf.surfaces.cli.commands import roadmap

    target = REPO_EA_DIR / "state.json"
    monkeypatch.setenv("EA_STATE", str(target))
    assert roadmap.resolve_state_path(None) == target


def test_repo_ea_guard_allows_an_explicit_workspace(tmp_path: Path) -> None:
    from eawf.surfaces.cli.commands import roadmap

    assert roadmap.resolve_state_path(tmp_path) == tmp_path / ".ea" / "state.json"


def test_every_test_runs_under_its_own_runtime_dir() -> None:
    """The autouse ``own_runtime_dir`` guard has an isolated dir to assert on."""
    import os

    configured = os.environ.get("EAWF_RUNTIME_DIR")
    assert configured, "EAWF_RUNTIME_DIR must be set for every test"
    resolved = Path(configured).resolve()
    assert REPO_EA_DIR not in resolved.parents
    assert resolved != REPO_EA_DIR
