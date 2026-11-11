"""Unit tests for ``tools/ci_changes.py``, the CI heavy-job classifier.

The classifier decides whether a run may skip the test matrix and the
twice-green job: only when every change since the last green ``ci.yaml``
tree is a path under ``.ea/``.
Git and the GitHub API are both injected, so no test spawns git or
touches the network; the two default adapters are exercised through
patched ``subprocess.run`` / ``urlopen``.
``tools/`` is not a package, so the module is loaded by path.
"""

from __future__ import annotations

import http.client
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


class RoutedFetch:
    """An API fetcher answering the runs list and each run's jobs list."""

    def __init__(
        self, mod: ModuleType, runs: object, jobs: dict[int, object] | None = None
    ) -> None:
        self._mod = mod
        self._runs = runs
        self._jobs = jobs or {}
        self.urls: list[str] = []

    def __call__(self, url: str) -> object:
        self.urls.append(url)
        if "/actions/workflows/" in url:
            return self._runs
        for run_id, body in self._jobs.items():
            if url == _jobs_url(run_id):
                return body
        raise self._mod.BaselineError(f"HTTP Error 404 for {url}")


def _run(
    sha: str,
    *,
    repo: str = _REPO,
    conclusion: str = "success",
    event: str = "push",
    branch: str = "main",
    run_id: int = 1,
) -> dict[str, Any]:
    return {
        "id": run_id,
        "head_sha": sha,
        "conclusion": conclusion,
        "event": event,
        "head_branch": branch,
        "head_repository": {"full_name": repo},
    }


def _jobs_url(run_id: int) -> str:
    return f"{_API}/repos/{_REPO}/actions/runs/{run_id}/jobs?per_page=100"


_COVERAGE_JOB = "Coverage gate over the combined shards"


def _jobs(coverage: str | None = "success") -> dict[str, Any]:
    """Render a jobs response; ``None`` leaves the coverage job out."""
    jobs: list[dict[str, Any]] = [{"name": "ci-ok", "conclusion": "success"}]
    if coverage is not None:
        jobs.append({"name": _COVERAGE_JOB, "conclusion": coverage})
    return {"total_count": len(jobs), "jobs": jobs}


def _runs(*runs: dict[str, Any]) -> dict[str, Any]:
    return {"total_count": len(runs), "workflow_runs": list(runs)}


def _diff_key(old: str, new: str) -> tuple[str, ...]:
    return ("diff", "--name-status", "--no-renames", "-z", old, new)


def _log_key(green: str, base: str) -> tuple[str, ...]:
    return ("log", "--format=", "--name-status", "--no-renames", "-m", "-z", f"{green}..{base}")


def _ancestor_key(green: str) -> tuple[str, ...]:
    return ("merge-base", "--is-ancestor", green, "HEAD")


def _rev_key(revision: str) -> tuple[str, ...]:
    return ("rev-parse", "--verify", f"{revision}^{{commit}}")


def _z(*paths: str, status: str = "M") -> str:
    """Render ``--name-status -z`` output listing every path under one status."""
    return "".join(f"{status}\0{path}\0" for path in paths)


def _push_git(mod: ModuleType, diff: str, *, green: str = _GREEN) -> FakeGit:
    """Prime a push checkout that contains *green* and differs from it by *diff*."""
    return FakeGit(mod, {_ancestor_key(green): "", _diff_key(green, "HEAD"): diff})


def _push_env(tmp_path: Path) -> dict[str, str]:
    return {
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REPOSITORY": _REPO,
        "GITHUB_REF_NAME": "main",
        "GITHUB_API_URL": _API,
        "GITHUB_OUTPUT": str(tmp_path / "github_output"),
    }


def _pr_env(tmp_path: Path, *, head_repo: str = _REPO, draft: object = True) -> dict[str, str]:
    """Return a pull request environment; the payload is a draft by default."""
    event_path = tmp_path / "event.json"
    pull_request: dict[str, Any] = {"head": {"repo": {"full_name": head_repo}}}
    if draft is not None:
        pull_request["draft"] = draft
    payload = {"action": "synchronize", "pull_request": pull_request}
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
        ".ea/a",
        ".ea/state.json",
        ".ea/store/evidence.jsonl",
        ".ea/generations/gen-ca819f3245bf71a8/history/change.jsonl",
        ".ea/config.yaml",
        ".ea/profile.yaml",
        ".ea/state.json.bak",
        ".ea/" + "x/" * 200 + "row.jsonl",
    ],
)
def test_is_bookkeeping_accepts_every_ea_path(mod: ModuleType, path: str) -> None:
    assert mod.is_bookkeeping(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "",
        ".ea",
        ".ea.yaml",
        ".eap/row.jsonl",
        "ea/state.json",
        "docs/.ea/state.json",
        "./.ea/state.json",
        ".secrets.baseline",
        ".secrets.baseline.bak",
        "src/eawf/kernel/state/models.py",
        ".github/workflows/ci.yaml",
    ],
)
def test_is_bookkeeping_rejects_paths_outside_ea(mod: ModuleType, path: str) -> None:
    assert mod.is_bookkeeping(path) is False


# --- push runs ---------------------------------------------------------------


def test_classify_push_state_only_diff_reports_no_code(mod: ModuleType, tmp_path: Path) -> None:
    git = _push_git(mod, _z(".ea/state.json"))
    verdict = mod.classify(_push_env(tmp_path), git=git, fetch=FakeFetch(_runs(_run(_GREEN))))
    assert verdict.code is False
    assert "only state bookkeeping" in verdict.reason
    assert _GREEN[:12] in verdict.reason
    assert verdict.green == _GREEN


def test_classify_push_secrets_baseline_diff_reports_code(mod: ModuleType, tmp_path: Path) -> None:
    git = _push_git(mod, _z(".secrets.baseline"))
    verdict = mod.classify(_push_env(tmp_path), git=git, fetch=FakeFetch(_runs(_run(_GREEN))))
    assert verdict.code is True
    assert ".secrets.baseline" in verdict.reason


@pytest.mark.parametrize("status", ["A", "M", "D", "T"])
def test_classify_push_ea_paths_report_no_code_under_every_status(
    mod: ModuleType, tmp_path: Path, status: str
) -> None:
    diff = _z(".ea/state.json", ".ea/generations/g/state.json", status=status)
    git = _push_git(mod, diff)
    verdict = mod.classify(_push_env(tmp_path), git=git, fetch=FakeFetch(_runs(_run(_GREEN))))
    assert verdict.code is False
    assert "only state bookkeeping" in verdict.reason


def test_classify_push_single_added_ea_path_reports_no_code(
    mod: ModuleType, tmp_path: Path
) -> None:
    git = _push_git(mod, _z(".ea/store/new.jsonl", status="A"))
    verdict = mod.classify(_push_env(tmp_path), git=git, fetch=FakeFetch(_runs(_run(_GREEN))))
    assert verdict.code is False


def test_classify_push_code_file_moved_into_ea_reports_code(
    mod: ModuleType, tmp_path: Path
) -> None:
    """``--no-renames`` keeps the source-side deletion of the moved file."""
    diff = _z("src/eawf/app.py", status="D") + _z(".ea/app.py", status="A")
    git = _push_git(mod, diff)
    verdict = mod.classify(_push_env(tmp_path), git=git, fetch=FakeFetch(_runs(_run(_GREEN))))
    assert verdict.code is True
    assert "src/eawf/app.py" in verdict.reason


def test_classify_push_malformed_name_status_reports_code(mod: ModuleType, tmp_path: Path) -> None:
    """An odd field count cannot be paired, so the classifier fails open."""
    git = _push_git(mod, "M\0.ea/state.json\0A\0")
    verdict = mod.classify(_push_env(tmp_path), git=git, fetch=FakeFetch(_runs(_run(_GREEN))))
    assert verdict.code is True
    assert "not pairs" in verdict.reason


def test_classify_push_identical_tree_reports_no_code(mod: ModuleType, tmp_path: Path) -> None:
    git = _push_git(mod, "")
    verdict = mod.classify(_push_env(tmp_path), git=git, fetch=FakeFetch(_runs(_run(_GREEN))))
    assert verdict.code is False


def test_classify_push_code_diff_reports_code(mod: ModuleType, tmp_path: Path) -> None:
    git = _push_git(mod, _z(".ea/state.json", "src/eawf/cli.py"))
    verdict = mod.classify(_push_env(tmp_path), git=git, fetch=FakeFetch(_runs(_run(_GREEN))))
    assert verdict.code is True
    assert "src/eawf/cli.py" in verdict.reason
    assert ".ea/state.json" not in verdict.reason


def test_classify_push_many_code_paths_are_summarised(mod: ModuleType, tmp_path: Path) -> None:
    paths = [f"src/m{index}.py" for index in range(7)]
    git = _push_git(mod, _z(*paths))
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
    assert verdict.green == ""
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
    git = _push_git(mod, _z(".ea/state.json"))
    verdict = mod.classify(_push_env(tmp_path), git=git, fetch=FakeFetch(runs))
    assert verdict.code is False
    assert git.calls == [_ancestor_key(_GREEN), _diff_key(_GREEN, "HEAD")]


def test_classify_push_queries_the_green_runs_of_every_ref(mod: ModuleType, tmp_path: Path) -> None:
    fetch = FakeFetch(_runs(_run(_GREEN)))
    git = _push_git(mod, "")
    mod.classify(_push_env(tmp_path), git=git, fetch=fetch)
    assert fetch.urls == [
        f"{_API}/repos/{_REPO}/actions/workflows/ci.yaml/runs?status=success&per_page=100"
    ]


def test_classify_push_no_green_head_in_history_reports_code(
    mod: ModuleType, tmp_path: Path
) -> None:
    """Green runs on unrelated branches never anchor the push."""
    runs = _runs(_run(_HEAD, branch="feature/x"), _run(_BASE, event="pull_request"))
    git = FakeGit(mod, {_diff_key(_HEAD, "HEAD"): "", _diff_key(_BASE, "HEAD"): ""})
    verdict = mod.classify(_push_env(tmp_path), git=git, fetch=FakeFetch(runs))
    assert verdict.code is True
    assert "with a head the push contains" in verdict.reason
    assert git.calls == [_ancestor_key(_HEAD), _ancestor_key(_BASE)]


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
    git = _push_git(mod, "", green=sha)
    verdict = mod.classify(_push_env(tmp_path), git=git, fetch=FakeFetch(_runs(_run(sha))))
    assert verdict.code is False


def test_classify_unreachable_green_commit_reports_code(mod: ModuleType, tmp_path: Path) -> None:
    verdict = mod.classify(
        _push_env(tmp_path), git=FakeGit(mod, {}), fetch=FakeFetch(_runs(_run(_GREEN)))
    )
    assert verdict.code is True
    assert "with a head the push contains" in verdict.reason


@pytest.mark.parametrize("missing", ["GITHUB_REPOSITORY", "GITHUB_REF_NAME"])
def test_classify_push_missing_env_reports_code(
    mod: ModuleType, tmp_path: Path, missing: str
) -> None:
    env = _push_env(tmp_path)
    del env[missing]
    fetch = FakeFetch(_runs(_run(_GREEN)))
    verdict = mod.classify(env, git=_push_git(mod, ""), fetch=fetch)
    assert verdict.code is True
    assert f"{missing} is unset" in verdict.reason


@pytest.mark.parametrize("event", ["schedule", "workflow_dispatch"])
def test_classify_scheduled_and_dispatched_runs_report_code(mod: ModuleType, event: str) -> None:
    fetch = FakeFetch(_runs(_run(_GREEN)))
    git = _push_git(mod, "")
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
    verdict = mod.classify(env, git=_push_git(mod, ""), fetch=fetch)
    assert verdict.code is True
    assert "no green baseline" in verdict.reason
    assert fetch.urls == []


def test_classify_default_api_url_when_unset(mod: ModuleType, tmp_path: Path) -> None:
    env = _push_env(tmp_path)
    del env["GITHUB_API_URL"]
    fetch = FakeFetch(_runs(_run(_GREEN)))
    mod.classify(env, git=_push_git(mod, ""), fetch=fetch)
    assert fetch.urls[0].startswith("https://api.github.com/repos/")


def test_runs_url_encodes_a_slashed_branch(mod: ModuleType) -> None:
    url = mod.runs_url(
        api_url=f"{_API}/", repository=_REPO, branch="feature/abc-v0.7", event="pull_request"
    )
    assert url == (
        f"{_API}/repos/{_REPO}/actions/workflows/ci.yaml/runs"
        "?branch=feature%2Fabc-v0.7&event=pull_request&status=success&per_page=100"
    )


def test_runs_url_without_branch_or_event_lists_every_ref(mod: ModuleType) -> None:
    url = mod.runs_url(api_url=_API, repository=_REPO, branch="", event=None)
    assert url == f"{_API}/repos/{_REPO}/actions/workflows/ci.yaml/runs?status=success&per_page=100"


def test_green_runs_keeps_every_qualifying_run_newest_first(mod: ModuleType) -> None:
    runs = _runs(
        _run(_GREEN, run_id=3),
        _run(_HEAD, conclusion="failure"),
        _run(_BASE, event="pull_request", run_id=2),
    )
    assert mod.green_runs(runs, repository=_REPO) == [
        mod.GreenRun(run_id=3, head_sha=_GREEN, event="push"),
        mod.GreenRun(run_id=2, head_sha=_BASE, event="pull_request"),
    ]
    assert mod.green_runs(_runs(), repository=_REPO) == []
    with pytest.raises(mod.BaselineError, match="no 'workflow_runs' list"):
        mod.green_runs({"workflow_runs": {}}, repository=_REPO)


@pytest.mark.parametrize(("raw", "expected"), [(None, 0), (True, 0), ("5", 0), (-4, 0), (9, 9)])
def test_green_runs_keeps_only_a_positive_integer_run_id(
    mod: ModuleType, raw: object, expected: int
) -> None:
    run = _run(_GREEN)
    run["id"] = raw
    run["event"] = 7
    assert mod.green_runs(_runs(run), repository=_REPO) == [
        mod.GreenRun(run_id=expected, head_sha=_GREEN, event="")
    ]


def test_ran_full_configuration_reads_the_coverage_job(mod: ModuleType) -> None:
    assert mod.ran_full_configuration(_jobs("success")) is True
    assert mod.ran_full_configuration(_jobs("skipped")) is False
    assert mod.ran_full_configuration({"jobs": []}) is False
    assert mod.ran_full_configuration({"jobs": ["x", {"name": _COVERAGE_JOB}]}) is False
    with pytest.raises(mod.BaselineError, match="no 'jobs' list"):
        mod.ran_full_configuration({"jobs": {}})


def test_jobs_url_names_the_run(mod: ModuleType) -> None:
    assert mod.jobs_url(api_url=f"{_API}/", repository=_REPO, run_id=42) == _jobs_url(42)


def test_full_configuration_job_names_the_ci_coverage_job(mod: ModuleType) -> None:
    """The proof reads the job by its display name, so it must match ci.yaml."""
    workflow = (_REPO_ROOT / ".github" / "workflows" / "ci.yaml").read_text(encoding="utf-8")
    assert f"    name: {mod.FULL_CONFIGURATION_JOB}\n" in workflow.split("\n  coverage:\n")[1]


# --- pull request runs -------------------------------------------------------


def test_classify_pull_request_state_only_head_and_base_reports_no_code(
    mod: ModuleType, tmp_path: Path
) -> None:
    git = _pr_git(mod, head_diff=_z(".ea/state.json"), base_log=_z(".ea/store/x.jsonl"))
    verdict = mod.classify(_pr_env(tmp_path), git=git, fetch=FakeFetch(_runs(_run(_GREEN))))
    assert verdict.code is False
    assert "head and base branch" in verdict.reason
    assert verdict.green == _GREEN


def test_classify_pull_request_ea_only_head_and_base_reports_no_code(
    mod: ModuleType, tmp_path: Path
) -> None:
    base_log = _z(".ea/store/new.jsonl", status="A") + _z(".ea/generations/g/state.json")
    git = _pr_git(mod, head_diff=_z(".ea/state.json", status="D"), base_log=base_log)
    verdict = mod.classify(_pr_env(tmp_path), git=git, fetch=FakeFetch(_runs(_run(_GREEN))))
    assert verdict.code is False


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
    assert verdict.green == _GREEN


def test_classify_pull_request_base_code_change_reports_code(
    mod: ModuleType, tmp_path: Path
) -> None:
    git = _pr_git(
        mod,
        head_diff=_z(".ea/state.json"),
        base_log=_z(".ea/state.json") + _z("src/eawf/app.py", status="A"),
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
        "?branch=feature%2Fabc&event=pull_request&status=success&per_page=100"
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


# --- full-configuration baselines ---------------------------------------------


def test_classify_ready_pull_request_repro_draft_baseline(mod: ModuleType, tmp_path: Path) -> None:
    """A draft goes green on the head, then is marked ready with no new
    commit: the draft's fast-lane run never ran coverage, e2e or the macOS
    shards, so the ready run must not inherit it.
    """
    env = _pr_env(tmp_path, draft=False)
    fetch = RoutedFetch(mod, _runs(_run(_GREEN, event="pull_request")), {1: _jobs(None)})
    git = _pr_git(mod, head_diff="", base_log="")
    verdict = mod.classify(env, git=git, fetch=fetch)
    assert verdict.code is True
    assert "full configuration" in verdict.reason
    assert verdict.green == ""


@pytest.mark.parametrize("coverage", [None, "skipped", "failure", "cancelled", "Success"])
def test_classify_ready_pull_request_refuses_runs_without_coverage_success(
    mod: ModuleType, tmp_path: Path, coverage: str | None
) -> None:
    fetch = RoutedFetch(mod, _runs(_run(_GREEN, event="pull_request")), {1: _jobs(coverage)})
    git = _pr_git(mod, head_diff="", base_log="")
    verdict = mod.classify(_pr_env(tmp_path, draft=False), git=git, fetch=fetch)
    assert verdict.code is True


def test_classify_ready_pull_request_skips_draft_runs_for_an_older_full_run(
    mod: ModuleType, tmp_path: Path
) -> None:
    newer = "e" * 40
    runs = _runs(
        _run(newer, event="pull_request", run_id=2), _run(_GREEN, event="pull_request", run_id=1)
    )
    fetch = RoutedFetch(mod, runs, {2: _jobs("skipped"), 1: _jobs("success")})
    git = _pr_git(mod, head_diff=_z(".ea/state.json"), base_log="")
    verdict = mod.classify(_pr_env(tmp_path, draft=False), git=git, fetch=fetch)
    assert verdict.code is False
    assert verdict.green == _GREEN
    assert fetch.urls[1:] == [_jobs_url(2), _jobs_url(1)]


@pytest.mark.parametrize("draft", [None, "true", 1])
def test_classify_pull_request_without_a_boolean_draft_flag_needs_a_full_run(
    mod: ModuleType, tmp_path: Path, draft: object
) -> None:
    """A payload that does not say draft is read as ready: the stricter rule."""
    fetch = RoutedFetch(mod, _runs(_run(_GREEN, event="pull_request")), {1: _jobs(None)})
    git = _pr_git(mod, head_diff="", base_log="")
    verdict = mod.classify(_pr_env(tmp_path, draft=draft), git=git, fetch=fetch)
    assert verdict.code is True


def test_classify_draft_pull_request_keeps_draft_baselines(mod: ModuleType, tmp_path: Path) -> None:
    fetch = RoutedFetch(mod, _runs(_run(_GREEN, event="pull_request")))
    git = _pr_git(mod, head_diff="", base_log="")
    verdict = mod.classify(_pr_env(tmp_path, draft=True), git=git, fetch=fetch)
    assert verdict.code is False
    assert len(fetch.urls) == 1


@pytest.mark.parametrize("body", [{}, [], {"jobs": None}, {"jobs": "x"}])
def test_classify_ready_pull_request_malformed_jobs_reports_code(
    mod: ModuleType, tmp_path: Path, body: object
) -> None:
    fetch = RoutedFetch(mod, _runs(_run(_GREEN, event="pull_request")), {1: body})
    git = _pr_git(mod, head_diff="", base_log="")
    verdict = mod.classify(_pr_env(tmp_path, draft=False), git=git, fetch=fetch)
    assert verdict.code is True


def test_classify_ready_pull_request_jobs_api_failure_reports_code(
    mod: ModuleType, tmp_path: Path
) -> None:
    fetch = RoutedFetch(mod, _runs(_run(_GREEN, event="pull_request")))
    git = _pr_git(mod, head_diff="", base_log="")
    verdict = mod.classify(_pr_env(tmp_path, draft=False), git=git, fetch=fetch)
    assert verdict.code is True
    assert "HTTP Error 404" in verdict.reason


@pytest.mark.parametrize("run_id", [None, "7", True])
def test_classify_ready_pull_request_run_without_an_id_is_no_baseline(
    mod: ModuleType, tmp_path: Path, run_id: object
) -> None:
    run = _run(_GREEN, event="pull_request")
    run["id"] = run_id
    fetch = RoutedFetch(mod, _runs(run), {1: _jobs()})
    git = _pr_git(mod, head_diff="", base_log="")
    verdict = mod.classify(_pr_env(tmp_path, draft=False), git=git, fetch=fetch)
    assert verdict.code is True
    assert len(fetch.urls) == 1


def test_classify_ready_pull_request_bounds_the_jobs_lookups(
    mod: ModuleType, tmp_path: Path
) -> None:
    limit = mod.PROOF_LOOKUPS
    ids = range(1, limit + 2)
    runs = _runs(*(_run(f"{i:040x}", event="pull_request", run_id=i) for i in ids))
    jobs = {i: _jobs(None) for i in ids[:-1]} | {ids[-1]: _jobs()}
    fetch = RoutedFetch(mod, runs, jobs)
    git = _pr_git(mod, head_diff="", base_log="")
    verdict = mod.classify(_pr_env(tmp_path, draft=False), git=git, fetch=fetch)
    assert verdict.code is True
    assert len(fetch.urls) == 1 + limit


def test_classify_dev_push_repro_draft_baseline(mod: ModuleType, tmp_path: Path) -> None:
    """dev fast-forwarded to a head whose only green run is a draft's fast
    lane (its ready run went red) must run the full configuration.
    """
    env = _push_env(tmp_path)
    env["GITHUB_REF_NAME"] = "dev"
    fetch = RoutedFetch(mod, _runs(_run(_HEAD, event="pull_request")), {1: _jobs(None)})
    git = FakeGit(mod, {_ancestor_key(_HEAD): "", _diff_key(_HEAD, "HEAD"): ""})
    verdict = mod.classify(env, git=git, fetch=fetch)
    assert verdict.code is True
    assert _diff_key(_HEAD, "HEAD") not in git.calls


@pytest.mark.parametrize("event", ["schedule", "workflow_dispatch", "merge_group", None])
def test_classify_dev_push_ignores_runs_of_other_events(
    mod: ModuleType, tmp_path: Path, event: str | None
) -> None:
    run = _run(_HEAD)
    run["event"] = event
    git = FakeGit(mod, {_ancestor_key(_HEAD): "", _diff_key(_HEAD, "HEAD"): ""})
    verdict = mod.classify(_push_env(tmp_path), git=git, fetch=RoutedFetch(mod, _runs(run)))
    assert verdict.code is True
    assert git.calls == []


def test_classify_dev_push_takes_a_push_run_without_a_jobs_lookup(
    mod: ModuleType, tmp_path: Path
) -> None:
    fetch = RoutedFetch(mod, _runs(_run(_GREEN, branch="dev")))
    verdict = mod.classify(_push_env(tmp_path), git=_push_git(mod, ""), fetch=fetch)
    assert verdict.code is False
    assert len(fetch.urls) == 1


@pytest.mark.parametrize("ref", ["release", "release/0.7", "release/0.7/rc"])
def test_classify_release_push_repro_other_ref_baseline(
    mod: ModuleType, tmp_path: Path, ref: str
) -> None:
    """A release push compares with the newest green push of its own ref,
    never with a dev run on the same commit, so twice-green still runs.
    """
    env = _push_env(tmp_path)
    env["GITHUB_REF_NAME"] = ref
    fetch = RoutedFetch(mod, _runs())
    git = FakeGit(mod, {_ancestor_key(_HEAD): "", _diff_key(_HEAD, "HEAD"): ""})
    verdict = mod.classify(env, git=git, fetch=fetch)
    assert verdict.code is True
    assert fetch.urls == [mod.runs_url(api_url=_API, repository=_REPO, branch=ref, event="push")]


def test_classify_release_push_takes_its_own_ref_baseline(mod: ModuleType, tmp_path: Path) -> None:
    env = _push_env(tmp_path)
    env["GITHUB_REF_NAME"] = "release/0.7"
    fetch = RoutedFetch(mod, _runs(_run(_GREEN, branch="release/0.7")))
    git = FakeGit(mod, {_diff_key(_GREEN, "HEAD"): _z(".ea/state.json")})
    verdict = mod.classify(env, git=git, fetch=fetch)
    assert verdict.code is False
    assert verdict.green == _GREEN
    assert git.calls == [_diff_key(_GREEN, "HEAD")]


@pytest.mark.parametrize("ref", ["releases", "release-0.7", "dev/release", "main"])
def test_classify_push_off_the_release_train_lists_every_ref(
    mod: ModuleType, tmp_path: Path, ref: str
) -> None:
    env = _push_env(tmp_path)
    env["GITHUB_REF_NAME"] = ref
    fetch = RoutedFetch(mod, _runs(_run(_GREEN)))
    mod.classify(env, git=_push_git(mod, ""), fetch=fetch)
    assert fetch.urls == [mod.runs_url(api_url=_API, repository=_REPO)]


# --- criteria gates ----------------------------------------------------------


def test_ea_only_diff_is_bookkeeping(mod: ModuleType, tmp_path: Path) -> None:
    """Every added, edited, deleted or renamed path under ``.ea/`` is
    bookkeeping; one path outside it turns the verdict back to code.
    """
    ea_only = (
        _z(".ea/generations/gen-1/state.json", ".ea/state.json")
        + _z(".ea/generations/gen-1/history/change.jsonl", status="A")
        + _z(".ea/store/old.jsonl", status="D")
        # --no-renames lists a rename as a deletion plus an addition.
        + _z(".ea/generations/gen-1/tasks/a.yaml", status="D")
        + _z(".ea/generations/gen-2/tasks/a.yaml", status="A")
        + _z(".ea/profile.yaml", status="T")
    )
    env = _push_env(tmp_path)
    git = FakeGit(mod, {_ancestor_key(_GREEN): "", _diff_key(_GREEN, "HEAD"): ea_only})
    assert mod.main(env, git=git, fetch=FakeFetch(_runs(_run(_GREEN)))) == 0
    assert Path(env["GITHUB_OUTPUT"]).read_text(encoding="utf-8") == (
        f"code=false\ngreen={_GREEN}\n"
    )

    mixed = ea_only + _z("src/eawf/cli.py")
    git = FakeGit(mod, {_ancestor_key(_GREEN): "", _diff_key(_GREEN, "HEAD"): mixed})
    verdict = mod.classify(env, git=git, fetch=FakeFetch(_runs(_run(_GREEN))))
    assert verdict.code_line == "code=true"
    assert "src/eawf/cli.py" in verdict.reason
    assert ".ea/" not in verdict.reason


def test_green_tree_from_any_ref(
    mod: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ``dev`` push fast-forwarded to a pull request head whose tree went
    green in a full-configuration pull request run inherits that run and
    skips the matrix.
    """
    other = "e" * 40
    runs = _runs(
        _run(other, event="pull_request", branch="feature/other", run_id=2),
        _run(_HEAD, event="pull_request", branch="feature/abc", run_id=1),
    )
    env = _push_env(tmp_path)
    env["GITHUB_REF_NAME"] = "dev"
    fetch = RoutedFetch(mod, runs, {1: _jobs("success")})
    # The unrelated branch's head is not in the pushed history, so its git
    # probe fails and the classifier moves on to the next green run.
    git = FakeGit(mod, {_ancestor_key(_HEAD): "", _diff_key(_HEAD, "HEAD"): ""})
    assert mod.main(env, git=git, fetch=fetch) == 0
    assert Path(env["GITHUB_OUTPUT"]).read_text(encoding="utf-8") == f"code=false\ngreen={_HEAD}\n"
    assert capsys.readouterr().out.startswith("code=false: ")
    assert fetch.urls == [
        f"{_API}/repos/{_REPO}/actions/workflows/ci.yaml/runs?status=success&per_page=100",
        _jobs_url(1),
    ]
    assert git.calls == [_ancestor_key(other), _ancestor_key(_HEAD), _diff_key(_HEAD, "HEAD")]

    scheduled = {"GITHUB_EVENT_NAME": "schedule", "GITHUB_OUTPUT": env["GITHUB_OUTPUT"]}
    assert mod.main(scheduled, git=git, fetch=fetch) == 0
    assert Path(env["GITHUB_OUTPUT"]).read_text(encoding="utf-8").endswith("code=true\ngreen=\n")

    del env["GITHUB_OUTPUT"]
    assert mod.main(env, git=git, fetch=fetch) == 2


# --- main --------------------------------------------------------------------


def test_main_appends_code_false_to_github_output(
    mod: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _push_env(tmp_path)
    output = Path(env["GITHUB_OUTPUT"])
    output.write_text("earlier=1\n", encoding="utf-8")
    git = _push_git(mod, _z(".ea/state.json"))
    assert mod.main(env, git=git, fetch=FakeFetch(_runs(_run(_GREEN)))) == 0
    assert output.read_text(encoding="utf-8") == f"earlier=1\ncode=false\ngreen={_GREEN}\n"
    assert capsys.readouterr().out.startswith("code=false: ")


def test_main_writes_code_true_when_the_baseline_is_missing(
    mod: ModuleType, tmp_path: Path
) -> None:
    env = _push_env(tmp_path)
    assert mod.main(env, git=FakeGit(mod, {}), fetch=FakeFetch(_runs())) == 0
    assert Path(env["GITHUB_OUTPUT"]).read_text(encoding="utf-8") == "code=true\ngreen=\n"


def test_main_writes_code_true_for_a_scheduled_run(
    mod: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "github_output"
    env = {"GITHUB_EVENT_NAME": "schedule", "GITHUB_OUTPUT": str(output)}
    assert mod.main(env, git=FakeGit(mod, {}), fetch=FakeFetch(_runs())) == 0
    assert output.read_text(encoding="utf-8") == "code=true\ngreen=\n"
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


class _TruncatedBody(io.BytesIO):
    def read(self, size: int | None = -1) -> bytes:
        raise http.client.IncompleteRead(b'{"workflow_runs": [', 512)


def test_github_fetcher_truncated_body_raises_baseline_error(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A body cut short mid-read is an HTTPException, not an OSError."""

    def truncated(request: Any, timeout: float) -> io.BytesIO:
        return _TruncatedBody()

    monkeypatch.setattr(mod.urllib.request, "urlopen", truncated)
    with pytest.raises(mod.BaselineError, match="IncompleteRead"):
        mod.github_fetcher("token-value")(f"{_API}/runs")
    env = _push_env(tmp_path)
    assert mod.main(env, git=FakeGit(mod, {}), fetch=mod.github_fetcher("")) == 0
    assert Path(env["GITHUB_OUTPUT"]).read_text(encoding="utf-8") == "code=true\ngreen=\n"


def test_github_fetcher_transport_error_raises_baseline_error(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def offline(request: Any, timeout: float) -> io.BytesIO:
        raise urllib.error.URLError("network is unreachable")

    monkeypatch.setattr(mod.urllib.request, "urlopen", offline)
    with pytest.raises(mod.BaselineError, match="network is unreachable"):
        mod.github_fetcher("token-value")(f"{_API}/runs")
