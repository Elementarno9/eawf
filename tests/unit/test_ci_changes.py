"""Unit tests for ``tools/ci_changes.py``, the CI heavy-job classifier.

The classifier decides whether a run may skip the test matrix and the
twice-green job: only when every path changed since the last green
``ci.yaml`` tree is state bookkeeping. Git and the GitHub API are both
injected, so no test spawns git or touches the network; the two default
adapters are exercised through patched ``subprocess.run`` / ``urlopen``.
``tools/`` is not a package, so the module is loaded by path.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import urllib.error
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TOOL_PATH = _REPO_ROOT / "tools" / "ci_changes.py"

_REPO = "owner/repo"
_GREEN = "a" * 40
_HEAD = "b" * 40
_BASE = "c" * 40
_API = "https://api.example.test"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ci_changes", _TOOL_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ci_changes"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod() -> ModuleType:
    return _load_module()


class FakeGit:
    """A git runner answering only the argv it was primed with."""

    def __init__(self, mod: ModuleType, answers: dict[tuple[str, ...], str]) -> None:
        self._mod = mod
        self._answers = answers
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args: Sequence[str]) -> str:
        key = tuple(args)
        self.calls.append(key)
        if key not in self._answers:
            raise self._mod.BaselineError(f"fatal: bad object in {key!r}")
        return self._answers[key]


class FakeFetch:
    """An API fetcher returning one canned body and recording the URLs."""

    def __init__(self, body: object) -> None:
        self._body = body
        self.urls: list[str] = []

    def __call__(self, url: str) -> object:
        self.urls.append(url)
        return self._body


def _run(sha: str, *, repo: str = _REPO, conclusion: str = "success") -> dict[str, Any]:
    return {"head_sha": sha, "conclusion": conclusion, "head_repository": {"full_name": repo}}


def _runs(*runs: dict[str, Any]) -> dict[str, Any]:
    return {"total_count": len(runs), "workflow_runs": list(runs)}


def _diff_key(old: str, new: str) -> tuple[str, ...]:
    return ("diff", "--name-only", "--no-renames", "-z", old, new)


def _log_key(green: str, base: str) -> tuple[str, ...]:
    return ("log", "--format=", "--name-only", "--no-renames", "-m", "-z", f"{green}..{base}")


def _rev_key(revision: str) -> tuple[str, ...]:
    return ("rev-parse", "--verify", f"{revision}^{{commit}}")


def _z(*paths: str) -> str:
    return "".join(f"{path}\0" for path in paths)


def _push_env(tmp_path: Path) -> dict[str, str]:
    return {
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REPOSITORY": _REPO,
        "GITHUB_REF_NAME": "main",
        "GITHUB_API_URL": _API,
        "GITHUB_OUTPUT": str(tmp_path / "github_output"),
    }


def _pr_env(tmp_path: Path, *, head_repo: str = _REPO) -> dict[str, str]:
    event_path = tmp_path / "event.json"
    payload = {"pull_request": {"head": {"repo": {"full_name": head_repo}}}}
    event_path.write_text(json.dumps(payload), encoding="utf-8")
    return {
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_REPOSITORY": _REPO,
        "GITHUB_REF_NAME": "7/merge",
        "GITHUB_HEAD_REF": "feature/abc",
        "GITHUB_EVENT_PATH": str(event_path),
        "GITHUB_API_URL": _API,
        "GITHUB_OUTPUT": str(tmp_path / "github_output"),
    }


def _pr_git(mod: ModuleType, *, head_diff: str, base_log: str) -> FakeGit:
    return FakeGit(
        mod,
        {
            _rev_key("HEAD^1"): f"{_BASE}\n",
            _rev_key("HEAD^2"): f"{_HEAD}\n",
            _diff_key(_GREEN, _HEAD): head_diff,
            _log_key(_GREEN, _BASE): base_log,
        },
    )


# --- bookkeeping paths -------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        ".ea/state.json",
        ".ea/store/evidence.jsonl",
        ".ea/store/nested/row.jsonl",
        ".secrets.baseline",
    ],
)
def test_is_bookkeeping_accepts_state_ledgers_and_secrets_baseline(
    mod: ModuleType, path: str
) -> None:
    assert mod.is_bookkeeping(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "",
        ".ea/config.yaml",
        ".ea/profile.yaml",
        ".ea/artifacts/audits/report.md",
        ".ea/state.json.bak",
        ".ea/store",
        ".ea/storefront/row.jsonl",
        "docs/.ea/state.json",
        "src/eawf/kernel/state/models.py",
        ".github/workflows/ci.yaml",
    ],
)
def test_is_bookkeeping_rejects_code_and_other_ea_paths(mod: ModuleType, path: str) -> None:
    assert mod.is_bookkeeping(path) is False


# --- push runs ---------------------------------------------------------------


def test_classify_push_state_only_diff_reports_no_code(mod: ModuleType, tmp_path: Path) -> None:
    git = FakeGit(
        mod,
        {
            _diff_key(_GREEN, "HEAD"): _z(
                ".ea/state.json", ".ea/store/gate.jsonl", ".secrets.baseline"
            )
        },
    )
    verdict = mod.classify(_push_env(tmp_path), git=git, fetch=FakeFetch(_runs(_run(_GREEN))))
    assert verdict.code is False
    assert "only state bookkeeping" in verdict.reason
    assert _GREEN[:12] in verdict.reason


def test_classify_push_identical_tree_reports_no_code(mod: ModuleType, tmp_path: Path) -> None:
    git = FakeGit(mod, {_diff_key(_GREEN, "HEAD"): ""})
    verdict = mod.classify(_push_env(tmp_path), git=git, fetch=FakeFetch(_runs(_run(_GREEN))))
    assert verdict.code is False


def test_classify_push_code_diff_reports_code(mod: ModuleType, tmp_path: Path) -> None:
    git = FakeGit(mod, {_diff_key(_GREEN, "HEAD"): _z(".ea/state.json", "src/eawf/cli.py")})
    verdict = mod.classify(_push_env(tmp_path), git=git, fetch=FakeFetch(_runs(_run(_GREEN))))
    assert verdict.code is True
    assert "src/eawf/cli.py" in verdict.reason
    assert ".ea/state.json" not in verdict.reason


def test_classify_push_non_state_ea_path_reports_code(mod: ModuleType, tmp_path: Path) -> None:
    git = FakeGit(mod, {_diff_key(_GREEN, "HEAD"): _z(".ea/config.yaml")})
    verdict = mod.classify(_push_env(tmp_path), git=git, fetch=FakeFetch(_runs(_run(_GREEN))))
    assert verdict.code is True


def test_classify_push_many_code_paths_are_summarised(mod: ModuleType, tmp_path: Path) -> None:
    paths = [f"src/m{index}.py" for index in range(7)]
    git = FakeGit(mod, {_diff_key(_GREEN, "HEAD"): _z(*paths)})
    verdict = mod.classify(_push_env(tmp_path), git=git, fetch=FakeFetch(_runs(_run(_GREEN))))
    assert verdict.code is True
    assert "src/m4.py" in verdict.reason
    assert "src/m5.py" not in verdict.reason
    assert verdict.reason.endswith("and 2 more")


def test_classify_push_missing_baseline_reports_code(mod: ModuleType, tmp_path: Path) -> None:
    git = FakeGit(mod, {})
    verdict = mod.classify(_push_env(tmp_path), git=git, fetch=FakeFetch(_runs()))
    assert verdict.code is True
    assert "no green baseline" in verdict.reason
    assert git.calls == []


def test_classify_push_red_and_foreign_runs_are_no_baseline(
    mod: ModuleType, tmp_path: Path
) -> None:
    runs = _runs(_run(_HEAD, conclusion="failure"), _run(_BASE, repo="someone/fork"))
    git = FakeGit(mod, {_diff_key(_HEAD, "HEAD"): "", _diff_key(_BASE, "HEAD"): ""})
    verdict = mod.classify(_push_env(tmp_path), git=git, fetch=FakeFetch(runs))
    assert verdict.code is True
    assert git.calls == []


def test_classify_push_uses_the_newest_green_run(mod: ModuleType, tmp_path: Path) -> None:
    runs = _runs(_run(_GREEN), _run(_BASE))
    git = FakeGit(mod, {_diff_key(_GREEN, "HEAD"): _z(".ea/state.json")})
    verdict = mod.classify(_push_env(tmp_path), git=git, fetch=FakeFetch(runs))
    assert verdict.code is False
    assert git.calls == [_diff_key(_GREEN, "HEAD")]


def test_classify_push_queries_the_green_push_runs_of_the_branch(
    mod: ModuleType, tmp_path: Path
) -> None:
    fetch = FakeFetch(_runs(_run(_GREEN)))
    git = FakeGit(mod, {_diff_key(_GREEN, "HEAD"): ""})
    mod.classify(_push_env(tmp_path), git=git, fetch=fetch)
    assert fetch.urls == [
        f"{_API}/repos/{_REPO}/actions/workflows/ci.yaml/runs"
        "?branch=main&event=push&status=success&per_page=30"
    ]


def test_classify_api_failure_reports_code(mod: ModuleType, tmp_path: Path) -> None:
    def failing_fetch(url: str) -> object:
        raise mod.BaselineError("HTTP Error 403: rate limited")

    verdict = mod.classify(_push_env(tmp_path), git=FakeGit(mod, {}), fetch=failing_fetch)
    assert verdict.code is True
    assert "HTTP Error 403" in verdict.reason


@pytest.mark.parametrize(
    "body",
    [[], {}, {"workflow_runs": None}, {"workflow_runs": ["not-a-run"]}],
)
def test_classify_malformed_runs_payload_reports_code(
    mod: ModuleType, tmp_path: Path, body: object
) -> None:
    verdict = mod.classify(_push_env(tmp_path), git=FakeGit(mod, {}), fetch=FakeFetch(body))
    assert verdict.code is True


@pytest.mark.parametrize("sha", ["--output=/dev/null", "HEAD", _GREEN[:39], _GREEN.upper(), None])
def test_classify_malformed_green_sha_never_reaches_git(
    mod: ModuleType, tmp_path: Path, sha: object
) -> None:
    run = _run(_GREEN)
    run["head_sha"] = sha
    git = FakeGit(mod, {})
    verdict = mod.classify(_push_env(tmp_path), git=git, fetch=FakeFetch(_runs(run)))
    assert verdict.code is True
    assert git.calls == []


def test_classify_accepts_a_sha256_green_sha(mod: ModuleType, tmp_path: Path) -> None:
    sha = "d" * 64
    git = FakeGit(mod, {_diff_key(sha, "HEAD"): ""})
    verdict = mod.classify(_push_env(tmp_path), git=git, fetch=FakeFetch(_runs(_run(sha))))
    assert verdict.code is False


def test_classify_unreachable_green_commit_reports_code(mod: ModuleType, tmp_path: Path) -> None:
    verdict = mod.classify(
        _push_env(tmp_path), git=FakeGit(mod, {}), fetch=FakeFetch(_runs(_run(_GREEN)))
    )
    assert verdict.code is True
    assert "bad object" in verdict.reason


@pytest.mark.parametrize("missing", ["GITHUB_REPOSITORY", "GITHUB_REF_NAME"])
def test_classify_push_missing_env_reports_code(
    mod: ModuleType, tmp_path: Path, missing: str
) -> None:
    env = _push_env(tmp_path)
    del env[missing]
    fetch = FakeFetch(_runs(_run(_GREEN)))
    verdict = mod.classify(env, git=FakeGit(mod, {_diff_key(_GREEN, "HEAD"): ""}), fetch=fetch)
    assert verdict.code is True
    assert f"{missing} is unset" in verdict.reason


@pytest.mark.parametrize("event", ["schedule", "workflow_dispatch"])
def test_classify_scheduled_and_dispatched_runs_report_code(mod: ModuleType, event: str) -> None:
    fetch = FakeFetch(_runs(_run(_GREEN)))
    git = FakeGit(mod, {_diff_key(_GREEN, "HEAD"): ""})
    verdict = mod.classify({"GITHUB_EVENT_NAME": event}, git=git, fetch=fetch)
    assert verdict.code is True
    assert verdict.reason == f"a {event} run re-proves the whole tree"
    assert fetch.urls == []
    assert git.calls == []


@pytest.mark.parametrize("event", ["", "merge_group", "Schedule"])
def test_classify_other_event_reports_code(mod: ModuleType, tmp_path: Path, event: str) -> None:
    env = _push_env(tmp_path)
    env["GITHUB_EVENT_NAME"] = event
    fetch = FakeFetch(_runs(_run(_GREEN)))
    verdict = mod.classify(env, git=FakeGit(mod, {_diff_key(_GREEN, "HEAD"): ""}), fetch=fetch)
    assert verdict.code is True
    assert "no green baseline" in verdict.reason
    assert fetch.urls == []


def test_classify_default_api_url_when_unset(mod: ModuleType, tmp_path: Path) -> None:
    env = _push_env(tmp_path)
    del env["GITHUB_API_URL"]
    fetch = FakeFetch(_runs(_run(_GREEN)))
    mod.classify(env, git=FakeGit(mod, {_diff_key(_GREEN, "HEAD"): ""}), fetch=fetch)
    assert fetch.urls[0].startswith("https://api.github.com/repos/")


def test_runs_url_encodes_a_slashed_branch(mod: ModuleType) -> None:
    url = mod.runs_url(
        api_url=f"{_API}/", repository=_REPO, branch="feature/abc-v0.7", event="pull_request"
    )
    assert url == (
        f"{_API}/repos/{_REPO}/actions/workflows/ci.yaml/runs"
        "?branch=feature%2Fabc-v0.7&event=pull_request&status=success&per_page=30"
    )


# --- pull request runs -------------------------------------------------------


def test_classify_pull_request_state_only_head_and_base_reports_no_code(
    mod: ModuleType, tmp_path: Path
) -> None:
    git = _pr_git(mod, head_diff=_z(".ea/state.json"), base_log=_z(".ea/store/release.jsonl"))
    verdict = mod.classify(_pr_env(tmp_path), git=git, fetch=FakeFetch(_runs(_run(_GREEN))))
    assert verdict.code is False
    assert "head and base branch" in verdict.reason


def test_classify_pull_request_unchanged_head_and_base_reports_no_code(
    mod: ModuleType, tmp_path: Path
) -> None:
    git = _pr_git(mod, head_diff="", base_log="")
    verdict = mod.classify(_pr_env(tmp_path), git=git, fetch=FakeFetch(_runs(_run(_GREEN))))
    assert verdict.code is False


def test_classify_pull_request_head_code_change_reports_code(
    mod: ModuleType, tmp_path: Path
) -> None:
    git = _pr_git(mod, head_diff=_z(".ea/state.json", "tools/ci_changes.py"), base_log="")
    verdict = mod.classify(_pr_env(tmp_path), git=git, fetch=FakeFetch(_runs(_run(_GREEN))))
    assert verdict.code is True
    assert "the head since green" in verdict.reason
    assert "tools/ci_changes.py" in verdict.reason


def test_classify_pull_request_base_code_change_reports_code(
    mod: ModuleType, tmp_path: Path
) -> None:
    git = _pr_git(
        mod, head_diff=_z(".ea/state.json"), base_log=_z(".ea/state.json", "src/eawf/app.py")
    )
    verdict = mod.classify(_pr_env(tmp_path), git=git, fetch=FakeFetch(_runs(_run(_GREEN))))
    assert verdict.code is True
    assert "base branch" in verdict.reason
    assert "src/eawf/app.py" in verdict.reason


def test_classify_pull_request_queries_the_green_runs_of_the_head_branch(
    mod: ModuleType, tmp_path: Path
) -> None:
    fetch = FakeFetch(_runs(_run(_GREEN)))
    mod.classify(_pr_env(tmp_path), git=_pr_git(mod, head_diff="", base_log=""), fetch=fetch)
    assert fetch.urls == [
        f"{_API}/repos/{_REPO}/actions/workflows/ci.yaml/runs"
        "?branch=feature%2Fabc&event=pull_request&status=success&per_page=30"
    ]


def test_classify_pull_request_from_a_fork_reports_code(mod: ModuleType, tmp_path: Path) -> None:
    fetch = FakeFetch(_runs(_run(_GREEN)))
    git = _pr_git(mod, head_diff="", base_log="")
    verdict = mod.classify(_pr_env(tmp_path, head_repo="someone/fork"), git=git, fetch=fetch)
    assert verdict.code is True
    assert "someone/fork" in verdict.reason
    assert fetch.urls == []


@pytest.mark.parametrize(
    "content", ["{not json", "[]", '{"pull_request": {"head": {"repo": null}}}']
)
def test_classify_pull_request_unusable_event_payload_reports_code(
    mod: ModuleType, tmp_path: Path, content: str
) -> None:
    env = _pr_env(tmp_path)
    Path(env["GITHUB_EVENT_PATH"]).write_text(content, encoding="utf-8")
    git = _pr_git(mod, head_diff="", base_log="")
    verdict = mod.classify(env, git=git, fetch=FakeFetch(_runs(_run(_GREEN))))
    assert verdict.code is True


def test_classify_pull_request_missing_event_file_reports_code(
    mod: ModuleType, tmp_path: Path
) -> None:
    env = _pr_env(tmp_path)
    env["GITHUB_EVENT_PATH"] = str(tmp_path / "absent.json")
    git = _pr_git(mod, head_diff="", base_log="")
    verdict = mod.classify(env, git=git, fetch=FakeFetch(_runs(_run(_GREEN))))
    assert verdict.code is True
    assert "event payload is unreadable" in verdict.reason


def test_classify_pull_request_non_merge_checkout_reports_code(
    mod: ModuleType, tmp_path: Path
) -> None:
    git = FakeGit(mod, {_rev_key("HEAD^1"): f"{_BASE}\n", _diff_key(_GREEN, _HEAD): ""})
    verdict = mod.classify(_pr_env(tmp_path), git=git, fetch=FakeFetch(_runs(_run(_GREEN))))
    assert verdict.code is True


def test_classify_pull_request_unresolvable_parent_reports_code(
    mod: ModuleType, tmp_path: Path
) -> None:
    git = FakeGit(mod, {_rev_key("HEAD^1"): "--not-a-sha\n", _rev_key("HEAD^2"): f"{_HEAD}\n"})
    verdict = mod.classify(_pr_env(tmp_path), git=git, fetch=FakeFetch(_runs(_run(_GREEN))))
    assert verdict.code is True
    assert "not a commit" in verdict.reason


def test_classify_pull_request_without_head_ref_reports_code(
    mod: ModuleType, tmp_path: Path
) -> None:
    env = _pr_env(tmp_path)
    env["GITHUB_HEAD_REF"] = ""
    git = _pr_git(mod, head_diff="", base_log="")
    verdict = mod.classify(env, git=git, fetch=FakeFetch(_runs(_run(_GREEN))))
    assert verdict.code is True
    assert "GITHUB_HEAD_REF is unset" in verdict.reason


# --- main --------------------------------------------------------------------


def test_main_appends_code_false_to_github_output(
    mod: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _push_env(tmp_path)
    output = Path(env["GITHUB_OUTPUT"])
    output.write_text("earlier=1\n", encoding="utf-8")
    git = FakeGit(mod, {_diff_key(_GREEN, "HEAD"): _z(".ea/state.json")})
    assert mod.main(env, git=git, fetch=FakeFetch(_runs(_run(_GREEN)))) == 0
    assert output.read_text(encoding="utf-8") == "earlier=1\ncode=false\n"
    assert capsys.readouterr().out.startswith("code=false: ")


def test_main_writes_code_true_when_the_baseline_is_missing(
    mod: ModuleType, tmp_path: Path
) -> None:
    env = _push_env(tmp_path)
    assert mod.main(env, git=FakeGit(mod, {}), fetch=FakeFetch(_runs())) == 0
    assert Path(env["GITHUB_OUTPUT"]).read_text(encoding="utf-8") == "code=true\n"


def test_main_writes_code_true_for_a_scheduled_run(
    mod: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "github_output"
    env = {"GITHUB_EVENT_NAME": "schedule", "GITHUB_OUTPUT": str(output)}
    assert mod.main(env, git=FakeGit(mod, {}), fetch=FakeFetch(_runs())) == 0
    assert output.read_text(encoding="utf-8") == "code=true\n"
    assert capsys.readouterr().out == "code=true: a schedule run re-proves the whole tree\n"


def test_main_without_github_output_exits_two(
    mod: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _push_env(tmp_path)
    del env["GITHUB_OUTPUT"]
    assert mod.main(env, git=FakeGit(mod, {}), fetch=FakeFetch(_runs())) == 2
    assert "GITHUB_OUTPUT is unset" in capsys.readouterr().err


# --- default git runner ------------------------------------------------------


class _Completed:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout


def test_run_git_returns_stdout(mod: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> _Completed:
        seen.append(argv)
        assert kwargs["check"] is True
        return _Completed(".ea/state.json\0")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    assert mod.run_git(["diff", "--name-only"]) == ".ea/state.json\0"
    assert seen == [["git", "diff", "--name-only"]]


def test_run_git_nonzero_exit_raises_baseline_error(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(argv: list[str], **kwargs: Any) -> _Completed:
        raise mod.subprocess.CalledProcessError(128, argv, stderr="fatal: bad object\n")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    with pytest.raises(mod.BaselineError, match="git diff failed: fatal: bad object"):
        mod.run_git(["diff", _GREEN, "HEAD"])


def test_run_git_missing_binary_raises_baseline_error(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(argv: list[str], **kwargs: Any) -> _Completed:
        raise FileNotFoundError(2, "No such file or directory", "git")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    with pytest.raises(mod.BaselineError, match="git could not start"):
        mod.run_git(["rev-parse", "HEAD"])


# --- default API fetcher -----------------------------------------------------


def _capture_urlopen(mod: ModuleType, monkeypatch: pytest.MonkeyPatch, body: bytes) -> list[Any]:
    requests: list[Any] = []

    def fake_urlopen(request: Any, timeout: float) -> io.BytesIO:
        requests.append(request)
        return io.BytesIO(body)

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    return requests


def test_github_fetcher_decodes_json_with_a_bearer_token(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests = _capture_urlopen(mod, monkeypatch, b'{"workflow_runs": []}')
    body = mod.github_fetcher("token-value")(f"{_API}/runs")
    assert body == {"workflow_runs": []}
    assert requests[0].full_url == f"{_API}/runs"
    assert requests[0].get_header("Authorization") == "Bearer token-value"


def test_github_fetcher_without_token_sends_no_authorization(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests = _capture_urlopen(mod, monkeypatch, b"{}")
    mod.github_fetcher("")(f"{_API}/runs")
    assert requests[0].get_header("Authorization") is None


def test_github_fetcher_bad_json_raises_baseline_error(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    _capture_urlopen(mod, monkeypatch, b"<html>")
    with pytest.raises(mod.BaselineError, match="runs lookup failed"):
        mod.github_fetcher("token-value")(f"{_API}/runs")


def test_github_fetcher_transport_error_raises_baseline_error(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def offline(request: Any, timeout: float) -> io.BytesIO:
        raise urllib.error.URLError("network is unreachable")

    monkeypatch.setattr(mod.urllib.request, "urlopen", offline)
    with pytest.raises(mod.BaselineError, match="network is unreachable"):
        mod.github_fetcher("token-value")(f"{_API}/runs")
