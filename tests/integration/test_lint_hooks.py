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

from eawf.platform.lint import _conditional
from eawf.surfaces.cli.app import app

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


# The path-leak gate scans only the macOS / Windows / Linux home anchors
# (``_path_leak_patterns`` filters ``SensitiveScrubber.PATTERNS`` to those
# whose source mentions ``Users`` or ``home``); the bare ``~/`` tilde shape
# is intentionally outside the gate's scan set, so the CLI-level cases below
# exercise only the gate-covered anchors. The tilde placeholder/leak
# discrimination is asserted directly against the helper instead.


@pytest.mark.parametrize(
    "placeholder",
    [
        "/Users/<name>",
        "/Users/<name>/.config/eawf",
        "C:\\Users\\...",
        "/home/<user>",
    ],
)
def test_path_leak_lint_skips_doc_placeholder(tmp_path: Path, placeholder: str) -> None:
    # Pedagogical "do NOT commit these" placeholders carry an
    # angle-bracket or ellipsis and are documentation, not leaks —
    # symmetric with the email gate's reserved-domain skip.
    doc = tmp_path / "secrets-hygiene.md"
    doc.write_text(f"Never commit machine paths like {placeholder} to VCS.\n", encoding="utf-8")
    result = runner.invoke(app, ["hook", "path-leak-lint", str(doc)])
    assert result.exit_code == 0, result.stdout
    assert "clean" in result.stdout.lower()


@pytest.mark.parametrize(
    "real_path",
    [
        "/Users/realuser",  # pragma: allowlist secret
        "C:\\Users\\Bob",  # pragma: allowlist secret
        "/home/realuser",  # pragma: allowlist secret
    ],
)
def test_path_leak_lint_still_flags_real_path(tmp_path: Path, real_path: str) -> None:
    # A concrete home-dir path (no placeholder token) is still a leak.
    leak = tmp_path / "leak.py"
    leak.write_text(f'HOME = "{real_path}"\n', encoding="utf-8")  # pragma: allowlist secret
    result = runner.invoke(app, ["hook", "path-leak-lint", str(leak)])
    assert result.exit_code == 1, result.stdout
    assert "leak" in result.stdout.lower()


def test_is_placeholder_path_discriminates_placeholder_from_leak() -> None:
    from eawf.surfaces.cli.commands.hook import _is_placeholder_path

    assert _is_placeholder_path("/Users/<name>")
    assert _is_placeholder_path("/Users/<name>/...")
    assert _is_placeholder_path("C:\\Users\\...")
    assert _is_placeholder_path("~/Workspace/...")
    assert _is_placeholder_path("/home/<user>")
    assert not _is_placeholder_path("/Users/realuser")  # pragma: allowlist secret
    assert not _is_placeholder_path("C:\\Users\\Bob")  # pragma: allowlist secret
    assert not _is_placeholder_path("~/Workspace/myproject")  # pragma: allowlist secret


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


def test_is_state_bookkeeping_path_exempts_only_state_json() -> None:
    """Only state.json is exempt from the leak scans; the stores are NOT.

    The stores used to be exempt too, on the premise that a daemon-written file
    cannot carry user secrets. That held until one began carrying the raw stdout
    of spawned agents -- at which point the one file that could leak was the one
    file nobody scanned, and a home path reached a public repo. state.json keeps
    its exemption because its path-SHAPED hits are placeholders in rule prose
    that explains this very lint.
    """
    from eawf.surfaces.cli.commands.hook import _is_state_bookkeeping_path

    assert _is_state_bookkeeping_path(".ea/state.json")
    assert not _is_state_bookkeeping_path(".ea/store/event.jsonl")
    assert not _is_state_bookkeeping_path(".ea/store/audit.jsonl")
    assert not _is_state_bookkeeping_path("src/eawf/surfaces/cli/app.py")
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


# --- clarity lints --------------------------------------------------------


def test_eawf012_design_provenance_cli_blocks(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("def f():\n    # per Codex during implementation\n    return None\n")
    result = runner.invoke(app, ["hook", "eawf012-design-provenance", str(bad)])
    assert result.exit_code == 1, result.stdout
    assert "EAWF012" in result.stdout


def test_eawf012_design_provenance_cli_blocks_docstrings(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text('"""per Q12 during implementation."""\n')
    result = runner.invoke(app, ["hook", "eawf012-design-provenance", str(bad)])
    assert result.exit_code == 1, result.stdout
    assert "EAWF012" in result.stdout


def test_eawf013_bracket_position_cli_blocks(tmp_path: Path) -> None:
    bad = tmp_path / "bad.md"
    bad.write_text("A claim ends. [1]\n\n## References\n\n[1] `src/x.py`\n")
    result = runner.invoke(app, ["hook", "eawf013-bracket-position", str(bad)])
    assert result.exit_code == 1, result.stdout
    assert "EAWF013" in result.stdout


def test_eawf014_no_manual_wrap_cli_blocks(tmp_path: Path) -> None:
    bad = tmp_path / "bad.md"
    bad.write_text("A paragraph was manually wrapped\nacross two physical lines.\n")
    result = runner.invoke(app, ["hook", "eawf014-no-manual-wrap", str(bad)])
    assert result.exit_code == 1, result.stdout
    assert "EAWF014" in result.stdout


def test_eawf014_staged_scope_ignores_unchanged_debt(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "noreply@anthropic.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    path = repo / "docs" / "note.md"
    path.parent.mkdir(parents=True)
    path.write_text("Old wrapped\nparagraph continues.\n", encoding="utf-8")
    _git(["add", "docs/note.md"], repo)
    _git(["commit", "-qm", "base"], repo)

    path.write_text("Old wrapped\nparagraph continues.\n\nNew paragraph.\n", encoding="utf-8")
    _git(["add", "docs/note.md"], repo)

    result = runner.invoke(app, ["-w", str(repo), "hook", "eawf014-no-manual-wrap"])
    assert result.exit_code == 0, result.stdout


def test_eawf014_staged_scope_blocks_new_wrap(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "noreply@anthropic.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    path = repo / "docs" / "note.md"
    path.parent.mkdir(parents=True)
    path.write_text("Old wrapped\nparagraph continues.\n", encoding="utf-8")
    _git(["add", "docs/note.md"], repo)
    _git(["commit", "-qm", "base"], repo)

    path.write_text(
        "Old wrapped\nparagraph continues.\n\nNew wrapped\nparagraph continues.\n",
        encoding="utf-8",
    )
    _git(["add", "docs/note.md"], repo)

    result = runner.invoke(app, ["-w", str(repo), "hook", "eawf014-no-manual-wrap"])
    assert result.exit_code == 1, result.stdout
    assert "EAWF014" in result.stdout


def test_eawf014_staged_scope_skips_agents_md_goldens(tmp_path: Path) -> None:
    repo = _init_repo_with_staged(
        tmp_path,
        rel="tests/golden/agents_md/core_only.md",
        body="Generated wrapped\nfixture debt.\n",
    )

    result = runner.invoke(app, ["-w", str(repo), "hook", "eawf014-no-manual-wrap"])
    assert result.exit_code == 0, result.stdout


def _init_repo_with_committed_file(tmp_path: Path, *, rel: str, body: str) -> Path:
    """Init a repo and commit `rel` with `body` so it can later be relocated."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "noreply@anthropic.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    # Detach the operator's global pre-commit hooks (``core.hooksPath``) so a
    # seed body carrying a deliberate leak fixture commits without the global
    # path-leak gate blocking the test setup.
    _git(["config", "core.hooksPath", str(repo / ".disabled-hooks")], repo)
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    _git(["add", rel], repo)
    _git(["commit", "-qm", "seed"], repo)
    return repo


def test_path_leak_lint_skips_pure_relocation_of_committed_leak(tmp_path: Path) -> None:
    # A pure `git mv` carries an already-committed leak to a new path without
    # changing a byte; the gate must not re-flag pre-existing debt the move
    # neither introduced nor can fix (forward-fix-only hygiene).
    leak_body = 'p = "/Users/realuser/x"\n'  # pragma: allowlist secret
    repo = _init_repo_with_committed_file(tmp_path, rel="old/leak.py", body=leak_body)
    (repo / "new").mkdir()
    _git(["mv", "old/leak.py", "new/leak.py"], repo)
    result = runner.invoke(app, ["-w", str(repo), "hook", "path-leak-lint"])
    assert result.exit_code == 0, result.stdout
    assert "clean" in result.stdout.lower()


def test_eawf013_skips_pure_relocation_of_committed_debt(tmp_path: Path) -> None:
    # A relocated pre-chassis artifact must not re-trip the full-content
    # citation gate on its already-committed body.
    debt_body = "# Audit\n\n## References\n\n[1] `repo/path.py`\n[2] `repo/other.py`\n"
    repo = _init_repo_with_committed_file(tmp_path, rel="legacy-audit.md", body=debt_body)
    (repo / "audits").mkdir()
    _git(["mv", "legacy-audit.md", "audits/legacy-audit.md"], repo)
    result = runner.invoke(app, ["-w", str(repo), "hook", "eawf013-bracket-position"])
    assert result.exit_code == 0, result.stdout


def test_path_leak_lint_flags_relocation_with_added_leak(tmp_path: Path) -> None:
    # A move that ALSO edits the body (similarity below R100) stays in scope:
    # a leak introduced by the edit is still flagged, so the relocation skip
    # cannot smuggle a fresh leak past the gate.
    clean_body = "x = 1\n" * 20
    repo = _init_repo_with_committed_file(tmp_path, rel="old/mod.py", body=clean_body)
    (repo / "new").mkdir()
    _git(["mv", "old/mod.py", "new/mod.py"], repo)
    moved = repo / "new" / "mod.py"
    moved.write_text(
        clean_body + 'leak = "/Users/realuser/x"\n',  # pragma: allowlist secret
        encoding="utf-8",
    )
    _git(["add", "new/mod.py"], repo)
    result = runner.invoke(app, ["-w", str(repo), "hook", "path-leak-lint"])
    assert result.exit_code == 1, result.stdout
    assert "new/mod.py" in result.stdout


def test_eawf015_ears_advisory_cli_warns_without_blocking(tmp_path: Path) -> None:
    note = tmp_path / "note.md"
    note.write_text("The operator should review this before merge.\n")
    result = runner.invoke(app, ["hook", "eawf015-ears-advisory", str(note)])
    assert result.exit_code == 0, result.stdout
    assert "EAWF015" in result.stdout
    assert "warning" in result.stdout.lower()


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
    [
        "path-leak-lint",
        "email-leak-lint",
        "log-format-lint",
        "eawf002-log-key",
        "eawf003-logger-acquire",
        "eawf010-module-length",
        "eawf011-cognitive-complexity",
        "eawf012-design-provenance",
        "eawf013-bracket-position",
        "eawf014-no-manual-wrap",
        "eawf015-ears-advisory",
        "eawf019-math-facets",
        "eawf023-artifact-placement",
        "eawf024-test-tier-contract",
        "plugin-doctor-drift",
    ],
)
def test_hook_subcommands_registered(name: str) -> None:
    result = runner.invoke(app, ["hook", "--help"])
    assert result.exit_code == 0
    assert name in result.stdout


# --- eawf002-log-key ------------------------------------------------------


def test_eawf002_log_key_cli_blocks_on_id_suffix(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import logging\n"
        "logger = logging.getLogger(__name__)\n"
        'logger.info(f"close_wave wave_id={1} done")\n',
        encoding="utf-8",
    )
    result = runner.invoke(app, ["hook", "eawf002-log-key", str(bad)])
    assert result.exit_code == 1, result.stdout
    assert "EAWF002" in result.stdout


def test_eawf002_log_key_cli_clean_exits_zero(tmp_path: Path) -> None:
    good = tmp_path / "good.py"
    good.write_text(
        "import logging\n"
        "logger = logging.getLogger(__name__)\n"
        'logger.info(f"close_wave wave={1} done")\n',
        encoding="utf-8",
    )
    result = runner.invoke(app, ["hook", "eawf002-log-key", str(good)])
    assert result.exit_code == 0, result.stdout
    assert "clean" in result.stdout.lower()


def test_eawf002_log_key_skips_unparseable(tmp_path: Path) -> None:
    broken = tmp_path / "broken.py"
    broken.write_text("def (:\n", encoding="utf-8")
    result = runner.invoke(app, ["hook", "eawf002-log-key", str(broken)])
    assert result.exit_code == 0, result.stdout


# --- eawf003-logger-acquire -----------------------------------------------


def test_eawf003_logger_acquire_cli_blocks_on_hardcoded_name(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text(
        'import logging\nlogger = logging.getLogger("eawf")\n',
        encoding="utf-8",
    )
    result = runner.invoke(app, ["hook", "eawf003-logger-acquire", str(bad)])
    assert result.exit_code == 1, result.stdout
    assert "EAWF003" in result.stdout


def test_eawf003_logger_acquire_cli_clean_exits_zero(tmp_path: Path) -> None:
    good = tmp_path / "good.py"
    good.write_text(
        "import logging\nlogger = logging.getLogger(__name__)\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["hook", "eawf003-logger-acquire", str(good)])
    assert result.exit_code == 0, result.stdout
    assert "clean" in result.stdout.lower()


# --- eawf010-module-length ------------------------------------------------


def test_eawf010_module_length_cli_blocks_over_budget(tmp_path: Path) -> None:
    big = tmp_path / "big.py"
    big.write_text("x = 1\n" * 50, encoding="utf-8")
    result = runner.invoke(app, ["hook", "eawf010-module-length", str(big), "--max-loc", "10"])
    assert result.exit_code == 1, result.stdout
    assert "EAWF010" in result.stdout


def test_eawf010_module_length_cli_clean_under_budget(tmp_path: Path) -> None:
    small = tmp_path / "small.py"
    small.write_text("x = 1\n" * 5, encoding="utf-8")
    result = runner.invoke(app, ["hook", "eawf010-module-length", str(small), "--max-loc", "10"])
    assert result.exit_code == 0, result.stdout
    assert "clean" in result.stdout.lower()


def test_eawf010_module_length_cli_waiver_clears(tmp_path: Path) -> None:
    waived = tmp_path / "waived.py"
    body = "# noqa: EAWF010 generated table; split deferred\n" + ("x = 1\n" * 50)
    waived.write_text(body, encoding="utf-8")
    result = runner.invoke(app, ["hook", "eawf010-module-length", str(waived), "--max-loc", "10"])
    assert result.exit_code == 0, result.stdout
    assert "clean" in result.stdout.lower()


# --- eawf011-cognitive-complexity -----------------------------------------


def test_eawf011_cognitive_complexity_cli_blocks_over_budget(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    # A function with deep nested branches exceeds a low budget.
    bad.write_text(
        "def f(a, b, c):\n"
        "    if a:\n"
        "        if b:\n"
        "            if c:\n"
        "                return 1\n"
        "    return 0\n",
        encoding="utf-8",
    )
    result = runner.invoke(
        app, ["hook", "eawf011-cognitive-complexity", str(bad), "--max-complexity", "2"]
    )
    assert result.exit_code == 1, result.stdout
    assert "EAWF011" in result.stdout


def test_eawf011_cognitive_complexity_cli_clean_under_budget(tmp_path: Path) -> None:
    good = tmp_path / "good.py"
    good.write_text("def f(a):\n    return a + 1\n", encoding="utf-8")
    result = runner.invoke(
        app, ["hook", "eawf011-cognitive-complexity", str(good), "--max-complexity", "15"]
    )
    assert result.exit_code == 0, result.stdout
    assert "clean" in result.stdout.lower()


def test_eawf011_cognitive_complexity_skips_unparseable(tmp_path: Path) -> None:
    broken = tmp_path / "broken.py"
    broken.write_text("def (:\n", encoding="utf-8")
    result = runner.invoke(
        app, ["hook", "eawf011-cognitive-complexity", str(broken), "--max-complexity", "2"]
    )
    assert result.exit_code == 0, result.stdout


# --- eawf024-test-tier-contract -------------------------------------------


def test_eawf024_test_tier_cli_blocks_on_planted_unit_import(tmp_path: Path) -> None:
    # A planted tests/unit file importing subprocess+textual+CliRunner is a
    # mislabeled non-unit test; the gate must flag it. The path must be under
    # tests/unit/ for the dispatcher's tier predicate to include it.
    planted = tmp_path / "tests" / "unit" / "test_planted.py"
    planted.parent.mkdir(parents=True)
    planted.write_text(
        "import subprocess\nimport textual\nfrom typer.testing import CliRunner\n",
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        ["-w", str(tmp_path), "hook", "eawf024-test-tier-contract", "tests/unit/test_planted.py"],
    )
    assert result.exit_code == 1, result.stdout
    assert "EAWF024" in result.stdout


def test_eawf024_test_tier_cli_clean_unit_file_exits_zero(tmp_path: Path) -> None:
    ok = tmp_path / "tests" / "unit" / "test_ok.py"
    ok.parent.mkdir(parents=True)
    ok.write_text("import json\n\nx = json.dumps({})\n", encoding="utf-8")
    result = runner.invoke(
        app, ["-w", str(tmp_path), "hook", "eawf024-test-tier-contract", "tests/unit/test_ok.py"]
    )
    assert result.exit_code == 0, result.stdout
    assert "clean" in result.stdout.lower()


def test_eawf024_test_tier_cli_honours_noqa_waiver(tmp_path: Path) -> None:
    waived = tmp_path / "tests" / "unit" / "test_waived.py"
    waived.parent.mkdir(parents=True)
    waived.write_text("import subprocess  # noqa: EAWF024 fixture\n", encoding="utf-8")
    result = runner.invoke(
        app,
        ["-w", str(tmp_path), "hook", "eawf024-test-tier-contract", "tests/unit/test_waived.py"],
    )
    assert result.exit_code == 0, result.stdout
    assert "clean" in result.stdout.lower()


def test_eawf024_test_tier_cli_ignores_non_unit_path(tmp_path: Path) -> None:
    # An integration-tier file importing subprocess is legitimate; the
    # dispatcher's tier predicate excludes any path outside tests/unit/.
    integ = tmp_path / "tests" / "integration" / "test_it.py"
    integ.parent.mkdir(parents=True)
    integ.write_text("import subprocess\n", encoding="utf-8")
    result = runner.invoke(
        app,
        ["-w", str(tmp_path), "hook", "eawf024-test-tier-contract", "tests/integration/test_it.py"],
    )
    assert result.exit_code == 0, result.stdout


def test_plugin_doctor_drift_reports_marketplace_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absent local render under marketplace mode does not block the push.

    The hook reported missing=37 and told the operator to run `plugin sync`,
    which the conflict gate refuses because running it recreates the very
    duplicate the gate exists to prevent. That deadlock blocked every push.
    """
    from eawf.runtime.runtimes.claude.plugin_conflict import CCPluginConflict

    monkeypatch.setattr(
        "eawf.surfaces.cli.commands.plugin.detect_marketplace_install",
        lambda: CCPluginConflict(plugin_dir=tmp_path / "fake"),
    )
    monkeypatch.setattr(
        "eawf.platform.lint._conditional.relevant_for_hook",
        lambda *_args, **_kwargs: True,
    )
    result = runner.invoke(app, ["--json", "-w", str(tmp_path), "hook", "plugin-doctor-drift"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["mode"] == "marketplace"


def test_plugin_doctor_drift_still_blocks_without_marketplace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a marketplace install, a missing render is still real drift."""
    monkeypatch.setattr(
        "eawf.surfaces.cli.commands.plugin.detect_marketplace_install",
        lambda: None,
    )
    monkeypatch.setattr(
        "eawf.platform.lint._conditional.relevant_for_hook",
        lambda *_args, **_kwargs: True,
    )
    result = runner.invoke(app, ["-w", str(tmp_path), "hook", "plugin-doctor-drift"])
    assert result.exit_code != 0, result.output
    assert "drift detected" in result.output
