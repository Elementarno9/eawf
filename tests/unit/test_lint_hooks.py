"""Unit tests for the diff-scoped ``eawf hook`` lint gates (P27 W04).

Pins:

- ``path-leak-lint`` / ``email-leak-lint`` exit 1 on a seeded leak and
  0 on a clean diff (explicit file args bypass the git diff).
- The email allowlist (no-reply + canonical author rows) is preserved
  so a co-author trailer address does not trip the gate.
- ``log-format-lint`` exits 1 on a non-conforming ``logger.<level>``
  call site and 0 on a conforming one.
- The ``_conditional`` helper early-exits (empty list) when the diff
  name-only shows no relevant change, and narrows by the per-hook
  filter when it does.
- ``plugin-doctor-drift`` skips fast (exit 0) when no plugin-surface
  path changed.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.cli.app import app
from eawf.lint import _conditional

runner = CliRunner()


# --- _conditional helper --------------------------------------------------


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _init_repo_with_branch_change(tmp_path: Path, *, rel: str, body: str) -> Path:
    """Init a repo with a `base` commit, then a branch commit adding `rel`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "noreply@anthropic.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "seed.txt"], repo)
    _git(["commit", "-qm", "base"], repo)
    _git(["branch", "base-ref"], repo)
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    _git(["add", rel], repo)
    _git(["commit", "-qm", "branch change"], repo)
    return repo


def test_changed_files_lists_branch_diff(tmp_path: Path) -> None:
    repo = _init_repo_with_branch_change(tmp_path, rel="src/eawf/x.py", body="x = 1\n")
    files = _conditional.changed_files("base-ref", cwd=repo)
    assert files == ["src/eawf/x.py"]


def test_changed_files_empty_on_bad_base(tmp_path: Path) -> None:
    repo = _init_repo_with_branch_change(tmp_path, rel="src/eawf/x.py", body="x = 1\n")
    assert _conditional.changed_files("does-not-exist", cwd=repo) == []


def test_changed_files_empty_outside_repo(tmp_path: Path) -> None:
    # tmp_path itself is not a git tree -> empty (fail-open skip).
    assert _conditional.changed_files("origin/main", cwd=tmp_path) == []


def test_select_relevant_filters_by_pattern() -> None:
    files = ["src/eawf/a.py", "docs/readme.md", "src/eawf/b.py"]
    pattern = re.compile(r"^src/eawf/.*\.py$")
    assert _conditional.select_relevant(files, pattern) == ["src/eawf/a.py", "src/eawf/b.py"]


def test_relevant_for_hook_narrows_log_format(tmp_path: Path) -> None:
    repo = _init_repo_with_branch_change(tmp_path, rel="docs/readme.md", body="hi\n")
    # A docs-only change is not relevant to the log-format gate.
    assert _conditional.relevant_for_hook("log-format-lint", "base-ref", cwd=repo) == []


def test_relevant_for_hook_unknown_returns_all(tmp_path: Path) -> None:
    repo = _init_repo_with_branch_change(tmp_path, rel="docs/readme.md", body="hi\n")
    assert _conditional.relevant_for_hook("not-a-hook", "base-ref", cwd=repo) == ["docs/readme.md"]


def _init_repo_with_staged(tmp_path: Path, *, rel: str, body: str) -> Path:
    """Init a repo with one committed file, then stage a new file `rel`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "noreply@anthropic.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "seed.txt"], repo)
    _git(["commit", "-qm", "base"], repo)
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    _git(["add", rel], repo)
    return repo


def test_staged_files_lists_index(tmp_path: Path) -> None:
    repo = _init_repo_with_staged(tmp_path, rel="src/eawf/y.py", body="y = 1\n")
    assert _conditional.staged_files(cwd=repo) == ["src/eawf/y.py"]


def test_staged_files_empty_when_nothing_staged(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "noreply@anthropic.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "seed.txt"], repo)
    _git(["commit", "-qm", "base"], repo)
    assert _conditional.staged_files(cwd=repo) == []


def test_relevant_for_hook_staged_narrows(tmp_path: Path) -> None:
    repo = _init_repo_with_staged(tmp_path, rel="docs/readme.md", body="hi\n")
    # docs change staged -> not relevant to log-format gate.
    assert _conditional.relevant_for_hook("log-format-lint", cwd=repo, staged=True) == []


# --- path-leak-lint -------------------------------------------------------


def test_path_leak_lint_exits_one_on_seeded_leak(tmp_path: Path) -> None:
    leak = tmp_path / "leak.py"
    body = 'HOME = "/Users/somebody/secret/config"\n'  # pragma: allowlist secret
    leak.write_text(body, encoding="utf-8")
    result = runner.invoke(app, ["hook", "path-leak-lint", str(leak)])
    assert result.exit_code == 1, result.stdout
    assert "leak" in result.stdout.lower()


def test_path_leak_lint_clean_exits_zero(tmp_path: Path) -> None:
    ok = tmp_path / "ok.py"
    ok.write_text('VALUE = "/usr/local/etc/eawf"\nX = 1\n', encoding="utf-8")
    result = runner.invoke(app, ["hook", "path-leak-lint", str(ok)])
    assert result.exit_code == 0, result.stdout
    assert "clean" in result.stdout.lower()


def test_path_leak_lint_detects_windows_and_linux_home(tmp_path: Path) -> None:
    win = tmp_path / "win.txt"
    win_body = "path = C:\\Users\\someone\\file\n"  # pragma: allowlist secret
    win.write_text(win_body, encoding="utf-8")
    linux = tmp_path / "linux.txt"
    linux_body = "path = /home/someone/file\n"  # pragma: allowlist secret
    linux.write_text(linux_body, encoding="utf-8")
    assert runner.invoke(app, ["hook", "path-leak-lint", str(win)]).exit_code == 1
    assert runner.invoke(app, ["hook", "path-leak-lint", str(linux)]).exit_code == 1


def test_path_leak_lint_json_output(tmp_path: Path) -> None:
    leak = tmp_path / "leak.py"
    body = 'p = "/Users/x/y"\n'  # pragma: allowlist secret
    leak.write_text(body, encoding="utf-8")
    result = runner.invoke(app, ["--json", "hook", "path-leak-lint", str(leak)])
    assert result.exit_code == 1, result.stdout
    payload = json.loads(result.stdout)
    assert payload["clean"] is False
    assert payload["findings"][0]["lineno"] == 1
    assert payload["findings"][0]["path"] == str(leak)


def test_path_leak_lint_staged_fallback_clean_noop(tmp_path: Path) -> None:
    # No explicit files + nothing staged -> staged scan is empty -> no-op
    # exit 0. This is the `pre-commit run --all-files` scenario; the gate
    # must not scan / flag the working tree.
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "noreply@anthropic.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    # An unstaged leak file in the tree must NOT be flagged.
    untracked_body = 'p = "/Users/x/y"\n'  # pragma: allowlist secret
    (repo / "untracked_leak.py").write_text(untracked_body, encoding="utf-8")
    result = runner.invoke(app, ["-w", str(repo), "hook", "path-leak-lint"])
    assert result.exit_code == 0, result.stdout
    assert "clean" in result.stdout.lower()


def test_path_leak_lint_staged_fallback_flags_staged_leak(tmp_path: Path) -> None:
    staged_body = 'p = "/Users/x/y"\n'  # pragma: allowlist secret
    repo = _init_repo_with_staged(tmp_path, rel="staged_leak.py", body=staged_body)
    result = runner.invoke(app, ["-w", str(repo), "hook", "path-leak-lint"])
    assert result.exit_code == 1, result.stdout
    assert "staged_leak.py" in result.stdout


# --- email-leak-lint ------------------------------------------------------


def test_email_leak_lint_exits_one_on_seeded_leak(tmp_path: Path) -> None:
    leak = tmp_path / "leak.md"
    body = "contact person@acmecorp.io for details\n"  # pragma: allowlist secret
    leak.write_text(body, encoding="utf-8")
    result = runner.invoke(app, ["hook", "email-leak-lint", str(leak)])
    assert result.exit_code == 1, result.stdout


def test_email_leak_lint_skips_reserved_example_domain(tmp_path: Path) -> None:
    # RFC 2606 reserved domains are standard placeholders, never real PII.
    ok = tmp_path / "fixture.py"
    ok.write_text('addr = "test@example.com"\n', encoding="utf-8")
    result = runner.invoke(app, ["hook", "email-leak-lint", str(ok)])
    assert result.exit_code == 0, result.stdout


def test_email_leak_lint_skips_action_version_ref(tmp_path: Path) -> None:
    # A version / action pin like ``setup-uv@v8.1.0`` is not an email
    # (its top-level label is not an alphabetic TLD).
    ok = tmp_path / "ci.yaml"
    ok.write_text("      - uses: astral-sh/setup-uv@v8.1.0\n", encoding="utf-8")
    result = runner.invoke(app, ["hook", "email-leak-lint", str(ok)])
    assert result.exit_code == 0, result.stdout


def test_is_state_bookkeeping_path_excludes_daemon_files() -> None:
    from eawf.cli.commands.hook import _is_state_bookkeeping_path

    assert _is_state_bookkeeping_path(".ea/state.json")
    assert _is_state_bookkeeping_path(".ea/store/event.jsonl")
    assert _is_state_bookkeeping_path(".ea/store/audit.jsonl")
    assert not _is_state_bookkeeping_path("src/eawf/cli/app.py")
    assert not _is_state_bookkeeping_path(".ea/profile.yaml")


def test_email_leak_lint_allowlists_noreply(tmp_path: Path) -> None:
    ok = tmp_path / "trailer.txt"
    ok.write_text("Co-Authored-By: Claude <noreply@anthropic.com>\n", encoding="utf-8")
    result = runner.invoke(app, ["hook", "email-leak-lint", str(ok)])
    assert result.exit_code == 0, result.stdout


def test_email_leak_lint_clean_exits_zero(tmp_path: Path) -> None:
    ok = tmp_path / "plain.txt"
    ok.write_text("no addresses here\n", encoding="utf-8")
    result = runner.invoke(app, ["hook", "email-leak-lint", str(ok)])
    assert result.exit_code == 0, result.stdout


def test_leak_gates_honour_allowlist_marker(tmp_path: Path) -> None:
    # A line carrying the inline allowlist marker is exempt from both
    # the path and the email gate (by-design fixture / pattern source).
    marked = tmp_path / "fixture.py"
    marked.write_text(
        'P = "/Users/x/y"  # pragma: allowlist secret\n'  # pragma: allowlist secret
        'E = "person@example.com"  # pragma: allowlist secret\n',  # pragma: allowlist secret
        encoding="utf-8",
    )
    assert runner.invoke(app, ["hook", "path-leak-lint", str(marked)]).exit_code == 0
    assert runner.invoke(app, ["hook", "email-leak-lint", str(marked)]).exit_code == 0


# --- log-format-lint ------------------------------------------------------


def test_log_format_lint_exits_one_on_bad_call(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import logging\n"
        "logger = logging.getLogger(__name__)\n"
        'logger.info("oops: this is freeform prose")\n',
        encoding="utf-8",
    )
    result = runner.invoke(app, ["hook", "log-format-lint", str(bad)])
    assert result.exit_code == 1, result.stdout
    assert "violation" in result.stdout.lower()


def test_log_format_lint_clean_exits_zero(tmp_path: Path) -> None:
    good = tmp_path / "good.py"
    good.write_text(
        "import logging\n"
        "logger = logging.getLogger(__name__)\n"
        'logger.info(f"create_worktree wave={1} branch={2!r}")\n',
        encoding="utf-8",
    )
    result = runner.invoke(app, ["hook", "log-format-lint", str(good)])
    assert result.exit_code == 0, result.stdout
    assert "clean" in result.stdout.lower()


def test_log_format_lint_skips_unparseable(tmp_path: Path) -> None:
    broken = tmp_path / "broken.py"
    broken.write_text("def (:\n", encoding="utf-8")
    result = runner.invoke(app, ["hook", "log-format-lint", str(broken)])
    # SyntaxError files are skipped, not failed by this gate.
    assert result.exit_code == 0, result.stdout


def test_log_format_lint_ignores_non_python_arg(tmp_path: Path) -> None:
    txt = tmp_path / "note.txt"
    txt.write_text('logger.info("oops: prose")\n', encoding="utf-8")
    result = runner.invoke(app, ["hook", "log-format-lint", str(txt)])
    assert result.exit_code == 0, result.stdout


# --- plugin-doctor-drift conditional skip ---------------------------------


def test_plugin_doctor_drift_skips_on_no_relevant_change(tmp_path: Path) -> None:
    repo = _init_repo_with_branch_change(tmp_path, rel="docs/readme.md", body="hi\n")
    result = runner.invoke(
        app, ["-w", str(repo), "hook", "plugin-doctor-drift", "--base", "base-ref"]
    )
    assert result.exit_code == 0, result.stdout
    assert "skip" in result.stdout.lower()


def test_plugin_doctor_drift_skip_json(tmp_path: Path) -> None:
    repo = _init_repo_with_branch_change(tmp_path, rel="docs/readme.md", body="hi\n")
    result = runner.invoke(
        app, ["--json", "-w", str(repo), "hook", "plugin-doctor-drift", "--base", "base-ref"]
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["skipped"] is True
    assert payload["clean"] is True


# --- help smoke -----------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["path-leak-lint", "email-leak-lint", "log-format-lint", "plugin-doctor-drift"],
)
def test_hook_subcommands_registered(name: str) -> None:
    result = runner.invoke(app, ["hook", "--help"])
    assert result.exit_code == 0
    assert name in result.stdout
