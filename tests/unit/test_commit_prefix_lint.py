"""Unit tests for ``tools/commit_prefix_lint.py``."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LINT_PATH = _REPO_ROOT / "tools" / "commit_prefix_lint.py"
_TOOL_DIR = _LINT_PATH.parent


def _load_module():
    if str(_TOOL_DIR) not in sys.path:
        sys.path.insert(0, str(_TOOL_DIR))
    spec = importlib.util.spec_from_file_location("commit_prefix_lint", _LINT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["commit_prefix_lint"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod():
    return _load_module()


_CLAUDE_TRAILER = "\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n"
_CODEX_TRAILER = "\n\nCo-Authored-By: Codex <noreply@openai.com>\n"


def _write_msg(tmp_path: Path, body: str, *, with_trailer: bool = True) -> Path:
    p = tmp_path / "COMMIT_EDITMSG"
    payload = body if not with_trailer else body.rstrip() + _CLAUDE_TRAILER
    p.write_text(payload, encoding="utf-8")
    return p


def _repo_without_vcs_override(tmp_path: Path) -> Path:
    """Return a repo root whose ``.ea/`` carries no ``vcs`` config block."""
    repo = tmp_path / "no-override-repo"
    (repo / ".ea").mkdir(parents=True)
    return repo


# --- subject grammar ---------------------------------------------------------


@pytest.mark.parametrize("commit_type", ["feat", "fix", "chore", "docs", "refactor", "test"])
def test_accepts_conventional_subject(tmp_path: Path, mod, commit_type: str) -> None:
    msg = _write_msg(tmp_path, f"{commit_type}: add the thing\n\nbody\n")
    code, diag = mod.lint(msg, ["src/eawf/x.py"])
    assert code == 0, diag


@pytest.mark.parametrize(
    "subject",
    [
        "[P14-W02] feat: a wave-form subject",
        "[P14-I02-W03] fix: a long wave-form subject",
        "[P14] state: a bare phase subject",
        "[P14-I01] docs: a bare iter subject",
        "[P14-CORE] state: the retired core suffix",
    ],
    ids=["wave", "long-wave", "bare-phase", "bare-iter", "core"],
)
def test_rejects_retired_lifecycle_subject(tmp_path: Path, mod, subject: str) -> None:
    msg = _write_msg(tmp_path, f"{subject}\n")
    code, diag = mod.lint(msg, ["src/eawf/x.py"])
    assert code == 1
    assert "commit subject rejected" in diag
    assert "'<type>: <summary>'" in diag


@pytest.mark.parametrize(
    "subject",
    ["gizmo: an unknown type", "feat:no space", "feat: ", "add the thing", "Feat: capital"],
    ids=["unknown-type", "no-space", "empty-summary", "no-type", "capital-type"],
)
def test_rejects_malformed_subject(tmp_path: Path, mod, subject: str) -> None:
    msg = _write_msg(tmp_path, f"{subject}\n")
    code, diag = mod.lint(msg, [])
    assert code == 1
    assert "commit subject rejected" in diag


def test_empty_subject_rejected(tmp_path: Path, mod) -> None:
    msg = _write_msg(tmp_path, "\n# just a comment\n", with_trailer=False)
    code, diag = mod.lint(msg, [])
    assert code == 1
    assert diag == "empty commit subject"


def test_skip_comment_lines_then_real_subject(tmp_path: Path, mod) -> None:
    msg = _write_msg(
        tmp_path,
        "# please enter the commit message\n# another comment\nfix: real subject\n",
    )
    code, diag = mod.lint(msg, [])
    assert code == 0, diag


def test_accepts_one_character_summary(tmp_path: Path, mod) -> None:
    msg = _write_msg(tmp_path, "fix: x\n")
    code, diag = mod.lint(msg, [])
    assert code == 0, diag


def test_rejects_task_trailer_on_an_unmarked_root(tmp_path: Path, mod) -> None:
    """A Task trailer names native work, which an unmarked root does not have."""
    msg = _write_msg(tmp_path, "feat: add the thing\n\nTask: EAWF-0001\n")
    code, diag = mod.lint(msg, ["src/eawf/x.py"], state_path=tmp_path / ".ea" / "state.json")
    assert code == 1
    assert "not marked epoch 2" in diag


def test_rejects_a_misplaced_task_trailer(tmp_path: Path, mod) -> None:
    msg = _write_msg(tmp_path, "feat: add the thing\n\nTask: EAWF-0001\nRefs: elsewhere\n")
    code, diag = mod.lint(msg, ["src/eawf/x.py"])
    assert code == 1
    assert "out of place" in diag


# --- task-prefix deprecation ---------------------------------------------------


def test_trailer_default_resolves_without_a_repo_override(tmp_path: Path, mod) -> None:
    assert mod._configured_subject_style(_repo_without_vcs_override(tmp_path)) == "trailer"


def test_trailer_default_yields_to_a_repo_bracket_override(tmp_path: Path, mod) -> None:
    repo = _repo_without_vcs_override(tmp_path)
    (repo / ".ea" / "config.yaml").write_text(
        "vcs:\n  conventions:\n    subject_style: bracket\n",
        encoding="utf-8",
    )
    assert mod._configured_subject_style(repo) == "bracket"


@pytest.mark.parametrize(
    ("subject", "subject_style", "expected"),
    [
        ("[EAWF-0001] feat: task-prefix form", "trailer", True),
        ("[EAWF-0001] feat: task-prefix form", "bracket", False),
        ("feat: trailer form", "trailer", False),
        ("", "trailer", False),
    ],
    ids=["prefix-trailer", "prefix-bracket", "conventional", "empty"],
)
def test_task_prefix_deprecation_boundaries(
    mod, subject: str, subject_style: str, expected: bool
) -> None:
    warning = mod._task_prefix_deprecation(subject, subject_style=subject_style)
    assert bool(warning) is expected


def test_task_prefix_warning_yields_to_a_missing_coauthor_trailer(
    tmp_path: Path, mod, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The co-author backstop outranks the advisory warning."""
    monkeypatch.setattr(mod, "_check_task_scope", lambda **_kwargs: None)
    msg = _write_msg(tmp_path, "[EAWF-0001] feat: no co-author\n", with_trailer=False)
    code, diag = mod.lint(msg, ["src/eawf/x.py"], subject_style="trailer")
    assert code == 1
    assert "missing recognized co-author trailer" in diag


def test_main_prints_the_task_prefix_warning_and_exits_zero(
    tmp_path: Path,
    mod,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _repo_without_vcs_override(tmp_path)
    msg = _write_msg(tmp_path, "[EAWF-0001] feat: prefix-form deliverable\n")
    monkeypatch.setattr(mod, "_find_repo_root", lambda *_args, **_kwargs: repo)
    monkeypatch.setattr(mod, "_staged_paths", lambda: ["src/eawf/x.py"])
    monkeypatch.setattr(mod, "_check_task_scope", lambda **_kwargs: None)

    assert mod.main(["commit_prefix_lint.py", str(msg)]) == 0
    assert "deprecated task-prefix subject" in capsys.readouterr().err


def test_main_rejects_a_missing_message_file(
    tmp_path: Path, mod, capsys: pytest.CaptureFixture[str]
) -> None:
    assert mod.main(["commit_prefix_lint.py", str(tmp_path / "absent")]) == 1
    assert "commit message file missing" in capsys.readouterr().err


def test_main_without_arguments_prints_usage(mod, capsys: pytest.CaptureFixture[str]) -> None:
    assert mod.main(["commit_prefix_lint.py"]) == 1
    assert "usage: commit_prefix_lint.py" in capsys.readouterr().err


@pytest.mark.parametrize("mode", ["--pre-push", "--check-rewrite"])
def test_main_has_no_rewrite_guard_mode(
    tmp_path: Path, mod, mode: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """The wave-trailer rewrite guard is retired; its flags read as a message path."""
    assert mod.main(["commit_prefix_lint.py", mode]) == 1
    assert "commit message file missing" in capsys.readouterr().err


# --- co-author trailer ----------------------------------------------------------


def test_accepts_codex_coauthor_trailer(tmp_path: Path, mod) -> None:
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("feat: add the thing\n\nbody" + _CODEX_TRAILER, encoding="utf-8")
    code, diag = mod.lint(msg, ["src/eawf/x.py"])
    assert code == 0, diag


def test_accepts_both_supported_coauthor_trailers(tmp_path: Path, mod) -> None:
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text(
        "feat: add the thing\n\nbody" + _CLAUDE_TRAILER + _CODEX_TRAILER, encoding="utf-8"
    )
    code, diag = mod.lint(msg, ["src/eawf/x.py"])
    assert code == 0, diag


@pytest.mark.parametrize(
    "body",
    [
        "feat: add thing\n\nbody only, no trailer\n",
        "feat: add thing\n\nbody\n# Co-Authored-By: Codex <noreply@openai.com>\n",
        "feat: add thing\n\nbody\n\nCo-Authored-By: Other <noreply@example.com>\n",
    ],
    ids=["missing", "comment-only", "unrecognized"],
)
def test_rejects_without_a_recognized_coauthor_trailer(tmp_path: Path, mod, body: str) -> None:
    msg = _write_msg(tmp_path, body, with_trailer=False)
    code, diag = mod.lint(msg, ["src/eawf/x.py"])
    assert code == 1
    assert "missing recognized co-author trailer" in diag


def test_disabled_coauthor_policy_rejects_any_trailer(tmp_path: Path, mod) -> None:
    msg = _write_msg(tmp_path, "feat: registry coauthor policy\n")
    code, diag = mod.lint(msg, ["src/eawf/x.py"], env={"EAWF_COAUTHOR_MODE": "disabled"})
    assert code == 1
    assert "disabled" in diag


def test_disabled_coauthor_policy_accepts_no_trailer(tmp_path: Path, mod) -> None:
    msg = _write_msg(tmp_path, "feat: registry coauthor policy\n\nbody\n", with_trailer=False)
    code, diag = mod.lint(msg, ["src/eawf/x.py"], env={"EAWF_COAUTHOR_MODE": "disabled"})
    assert code == 0, diag


# --- state path whitelist ---------------------------------------------------------


@pytest.mark.parametrize(
    "staged",
    [
        [],
        [".ea/specs/spec.md"],
        [".ea/state.json", ".ea/store/event.jsonl", ".ea/specs/spec.md"],
        [".ea/state.json", ".ea/store/audit.jsonl", ".ea/store/evidence.jsonl"],
        [
            ".ea/store/release_record.jsonl",
            ".ea/artifacts/evidence/2026-09-26-dev4-publication/dev4-baked.json",
        ],
    ],
    ids=["empty", "specs", "ea-only", "stores", "publication-evidence"],
)
def test_state_commit_accepts_bookkeeping_paths(tmp_path: Path, mod, staged: list[str]) -> None:
    msg = _write_msg(tmp_path, "state: land the bookkeeping\n")
    code, diag = mod.lint(msg, staged)
    assert code == 0, diag


@pytest.mark.parametrize(
    ("staged", "stray"),
    [
        ([".ea/state.json", "src/eawf/x.py"], "src/eawf/x.py"),
        ([".ea/state.json", "docs/x.md"], "docs/x.md"),
        ([".ea/store/release_record.jsonl", ".pre-commit-config.yaml"], ".pre-commit-config.yaml"),
        ([".ea/state.json", ".secrets.baseline"], ".secrets.baseline"),
        (
            [".ea/state.json", ".secrets.baseline", ".pre-commit-config.yaml"],
            ".pre-commit-config.yaml",
        ),
        ([".ea/state.json", ".ea/artifacts/audits/x.md"], ".ea/artifacts/audits/x.md"),
        (
            [".ea/state.json", ".ea/artifacts/evidence/2026-09-26-dev4-conformance/x.json"],
            ".ea/artifacts/evidence/2026-09-26-dev4-conformance/x.json",
        ),
        (
            [".ea/state.json", ".ea/artifacts/evidence/loose.json"],
            ".ea/artifacts/evidence/loose.json",
        ),
        ([".ea/generations/selected.json"], ".ea/generations/selected.json"),
    ],
    ids=[
        "source",
        "docs",
        "config-without-baseline",
        "secrets-baseline",
        "config-beside-baseline",
        "audit-artifact",
        "non-publication-evidence",
        "loose-evidence",
        "generation-on-unmarked-root",
    ],
)
def test_state_commit_rejects_non_state_paths(
    tmp_path: Path, mod, staged: list[str], stray: str
) -> None:
    msg = _write_msg(tmp_path, "state: land the bookkeeping\n")
    code, diag = mod.lint(msg, staged)
    assert code == 1
    assert "non-state paths" in diag
    assert stray in diag


def test_non_state_commit_is_not_path_gated(tmp_path: Path, mod) -> None:
    msg = _write_msg(tmp_path, "docs: refresh the reference\n")
    code, diag = mod.lint(msg, [".ea/artifacts/research/x.md", "src/eawf/x.py"])
    assert code == 0, diag


# --- probes -------------------------------------------------------------------------


def test_canonical_state_path_uses_supported_git_common_dir(
    tmp_path: Path, mod, monkeypatch: pytest.MonkeyPatch
) -> None:
    common_dir = tmp_path / "main" / ".git"
    seen: dict[str, object] = {}

    def _run(args: list[str], **kwargs: object) -> object:
        seen["args"] = args
        seen["cwd"] = kwargs.get("cwd")
        return SimpleNamespace(returncode=0, stdout=f"{common_dir}\n", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", _run)

    assert mod._canonical_state_path(tmp_path) == tmp_path / "main" / ".ea" / "state.json"
    assert seen["args"] == ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"]
    assert seen["cwd"] == tmp_path


def test_prior_commits_fails_open_when_git_is_missing(mod, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args, **_kwargs):
        raise OSError("binary not on PATH")

    monkeypatch.setattr(mod.subprocess, "run", _boom)
    assert mod._prior_commits(["Task: EAWF-0001"], repo_root=None) == []


def test_prior_commits_fails_open_on_timeout(mod, monkeypatch: pytest.MonkeyPatch) -> None:
    def _slow(*_args, **_kwargs):
        raise mod.subprocess.TimeoutExpired(cmd="log", timeout=1.0)

    monkeypatch.setattr(mod.subprocess, "run", _slow)
    assert mod._prior_commits(["Task: EAWF-0001"], repo_root=None) == []


def test_prior_commits_skips_a_non_zero_probe(mod, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *_a, **_kw: SimpleNamespace(returncode=128, stdout="", stderr="no HEAD"),
    )
    assert mod._prior_commits(["Task: EAWF-0001"], repo_root=None) == []


def test_prior_commits_returns_the_matched_sha(mod, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *_a, **_kw: SimpleNamespace(returncode=0, stdout="c" * 40 + "\n", stderr=""),
    )
    assert mod._prior_commits(["Task: EAWF-0001"], repo_root=None) == ["c" * 40]


def test_prior_commits_with_no_terms_runs_no_probe(mod, monkeypatch: pytest.MonkeyPatch) -> None:
    def _unexpected(*_args, **_kwargs):
        raise AssertionError("no term, no probe")

    monkeypatch.setattr(mod.subprocess, "run", _unexpected)
    assert mod._prior_commits([], repo_root=None) == []


# --- retired wave checks ----------------------------------------------------------------


def test_no_wave_check_remains(mod) -> None:
    records = mod.NativeRecords(
        batch_keys=frozenset({"BAT-0007"}),
        task_batches={"EAWF-0001": "BAT-0007", "EAWF-0002": "BAT-0007"},
    )
    message = (
        "refactor: retire the wave checks\n\nbody\n\n"
        "Batch: BAT-0007\nTask: EAWF-0001\nTask: EAWF-0002\n"
    )
    assert mod.pushed_commit_error(message, ["tools/commit_prefix_lint.py"], records) is None
    source = _LINT_PATH.read_text(encoding="utf-8")
    assert [token for token in ("Eawf-Wave", "P##", r"P\d", r"W\d") if token in source] == []


# --- seal regime: pushed ranges -------------------------------------------------------

_SEAL_BASE = "a" * 40
_SEAL_TIP = "f" * 40


def _code_commit(task: str) -> tuple[str, list[str]]:
    message = f"feat: land {task}\n\nWhy it lands.\n\nBatch: BAT-0007\nTask: {task}\n"
    return message, [f"src/eawf/{task.lower()}.py", f"tests/unit/test_{task.lower()}.py"]


def _check_range(
    mod, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, commits: list[tuple[str, list[str]]]
) -> tuple[int, str]:
    """Run ``check_pushed_commits`` over *commits*, oldest first, on a marked root."""
    by_sha = {f"{index:040x}": commit for index, commit in enumerate(commits, start=1)}

    def _fake_git(args: list[str], *, repo_root: Path | None) -> SimpleNamespace:
        if args[0] == "log":
            stdout = "".join(
                f"{sha}{mod._REC_SEP}{message}{mod._COMMIT_SEP}"
                for sha, (message, _paths) in reversed(by_sha.items())
            )
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        _message, paths = by_sha[args[-1]]
        return SimpleNamespace(returncode=0, stdout="\n".join(paths) + "\n", stderr="")

    records = mod.NativeRecords(
        batch_keys=frozenset({"BAT-0007"}),
        task_batches={"EAWF-0001": "BAT-0007", "EAWF-0002": "BAT-0007", "EAWF-0003": "BAT-0007"},
    )
    monkeypatch.setattr(mod, "_git", _fake_git)
    monkeypatch.setattr(mod, "_canonical_state_path", lambda _root: tmp_path / ".ea" / "state.json")
    monkeypatch.setattr(mod, "epoch2_generation", lambda _ea: tmp_path / "generation")
    monkeypatch.setattr(mod, "load_native_records", lambda _generation: records)
    env = {"PRE_COMMIT_FROM_REF": _SEAL_BASE, "PRE_COMMIT_TO_REF": _SEAL_TIP}
    return mod.check_pushed_commits(env, repo_root=tmp_path)


_SEAL_PATHS = [
    ".ea/generations/gen-0123456789abcdef/state.json",
    ".ea/generations/gen-0123456789abcdef/ledger/decision.jsonl",
]


def test_seal_regime_accepts_a_code_only_range(
    tmp_path: Path, mod, monkeypatch: pytest.MonkeyPatch
) -> None:
    commits = [_code_commit(task) for task in ("EAWF-0001", "EAWF-0002", "EAWF-0003")]

    assert _check_range(mod, monkeypatch, tmp_path, commits) == (0, "")


@pytest.mark.parametrize("code_commits", [0, 1, 3], ids=["seal-only", "one-code", "three-code"])
def test_seal_regime_accepts_a_range_ending_in_one_seal(
    tmp_path: Path, mod, monkeypatch: pytest.MonkeyPatch, code_commits: int
) -> None:
    tasks = ("EAWF-0001", "EAWF-0002", "EAWF-0003")[:code_commits]
    commits = [_code_commit(task) for task in tasks]
    commits.append(("state: seal the pull request state\n", _SEAL_PATHS))

    assert _check_range(mod, monkeypatch, tmp_path, commits) == (0, "")


def test_seal_regime_refuses_a_seal_carrying_source_without_asking_for_a_trailing_state_commit(
    tmp_path: Path, mod, monkeypatch: pytest.MonkeyPatch
) -> None:
    commits = [
        _code_commit("EAWF-0001"),
        ("state: seal the pull request state\n", [*_SEAL_PATHS, "src/eawf/x.py"]),
    ]

    code, diag = _check_range(mod, monkeypatch, tmp_path, commits)

    assert code == 1
    assert "state commit touches non-state paths: ['src/eawf/x.py']" in diag
    assert "ending with one 'state:" not in diag
    assert "trailing" not in diag
    assert "a 'state:' commit carries only committed state paths" in diag


def test_seal_regime_hint_covers_every_state_commit_carrying_source(
    tmp_path: Path, mod, monkeypatch: pytest.MonkeyPatch
) -> None:
    commits = [
        _code_commit("EAWF-0001"),
        ("state: seal the pull request state\n", _SEAL_PATHS),
        ("state: accept the Milestone\n", [*_SEAL_PATHS, "src/eawf/x.py"]),
    ]

    code, diag = _check_range(mod, monkeypatch, tmp_path, commits)

    assert code == 1
    assert "state commit touches non-state paths: ['src/eawf/x.py']" in diag
    assert "a 'state:' commit carries only committed state paths" in diag
    assert "'state: seal' commit carries" not in diag


def test_seal_regime_refuses_a_code_commit_without_a_task(
    tmp_path: Path, mod, monkeypatch: pytest.MonkeyPatch
) -> None:
    commits = [_code_commit("EAWF-0001"), ("feat: no trailers\n", ["src/eawf/y.py"])]

    code, diag = _check_range(mod, monkeypatch, tmp_path, commits)

    assert code == 1
    assert "carries no Task trailers" in diag
