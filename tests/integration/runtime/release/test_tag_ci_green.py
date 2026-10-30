"""``eawf release tag --push`` and ``eawf release preflight`` read the commit's CI runs.

A hand-run tag meets the proof the release pipeline's CI wait asks for: a
successful push-event run of the CI workflow on exactly the commit. The
sweep is the one the cut commit earns, handed to the verb in place of the
one it would compute, because the verb sweeps under the running package's
version and the cut checkpoint has been superseded; the forge is a fake
host, so no test reaches ``gh``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import release_tag
from eawf.workflow.release.ci_runs import CiRun
from eawf.workflow.release.pipeline import PipelineHostError
from tests.integration.runtime.release.test_tag_chokepoint import (
    CUT_TAG,
    CUT_VERSION,
    _git,
    chokepoint_sweep,
    cut_commit,
)

pytestmark = pytest.mark.integration

__all__ = ["cut_commit"]


class FakeHost:
    """A forge that holds *runs* for every commit and records what it was asked."""

    def __init__(self, *runs: dict[str, object], error: str | None = None) -> None:
        self._runs = runs
        self._error = error
        self.asked: list[str] = []

    def ci_runs(self, sha: str) -> tuple[CiRun, ...]:
        self.asked.append(sha)
        if self._error is not None:
            raise PipelineHostError(self._error)
        return tuple(CiRun.model_validate({**run, "headSha": sha}) for run in self._runs)


GREEN = {"databaseId": 41, "event": "push", "status": "completed", "conclusion": "success"}
RED = {"databaseId": 42, "event": "push", "status": "completed", "conclusion": "failure"}


@pytest.fixture()
def ready(cut_commit: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The cut commit, with the verb handed the ready sweep the commit earns."""
    sweep = chokepoint_sweep(cut_commit, monkeypatch)
    assert sweep.ready is True
    monkeypatch.setattr(release_tag, "_sweep_release", lambda **_: sweep)
    return cut_commit


def _host(monkeypatch: pytest.MonkeyPatch, host: FakeHost) -> FakeHost:
    monkeypatch.setattr(release_tag, "_release_host", lambda *_, **__: host)
    return host


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _tag(*args: str) -> object:
    return CliRunner().invoke(app, ["--json", "release", "tag", CUT_VERSION, "--push", *args])


def test_release_tag_push_refuses_a_commit_with_no_green_push_run(
    ready: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _host(monkeypatch, FakeHost())

    result = _tag()

    assert result.exit_code != 0
    assert f"release tag refuses {CUT_TAG}" in result.output
    assert f"commit {_head(ready)} has no successful ci.yaml push run" in result.output
    assert host.asked == [_head(ready)]
    assert _git(ready, "tag", "--list", CUT_TAG).stdout == ""


def test_release_tag_push_refuses_failed_runs_and_unreadable_runs(
    ready: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _host(monkeypatch, FakeHost(RED))
    assert "failed (42 failure)" in _tag("--dry-run").output

    _host(monkeypatch, FakeHost(error="gh exited 4"))
    result = _tag("--dry-run")
    assert result.exit_code != 0
    assert "could not be read: gh exited 4" in result.output


def test_release_tag_push_dry_run_plans_the_tag_over_a_green_push_run(
    ready: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _host(monkeypatch, FakeHost(RED, GREEN))

    result = _tag("--dry-run")

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["result"]["tag"] == CUT_TAG
    assert host.asked == [_head(ready)]


def test_release_tag_without_push_reads_no_ci_runs(
    ready: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _host(monkeypatch, FakeHost())

    result = CliRunner().invoke(app, ["release", "tag", CUT_VERSION, "--dry-run"])

    assert result.exit_code == 0, result.output
    assert host.asked == []


def _preflight(repo: Path, *args: str) -> object:
    return CliRunner().invoke(
        app, ["release", "preflight", CUT_VERSION, "--source", _head(repo), *args]
    )


def test_release_preflight_reports_a_failing_ci_check(
    ready: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _host(monkeypatch, FakeHost())

    result = _preflight(ready)

    assert result.exit_code != 0
    assert "ready=True" in result.output
    assert " REQ ci_green             fail" in result.output
    assert f"ci_green: commit {_head(ready)} has no successful ci.yaml push run" in result.output
    assert host.asked == [_head(ready)]


def test_release_preflight_passes_over_a_green_push_run(
    ready: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _host(monkeypatch, FakeHost(GREEN))

    result = _preflight(ready)

    assert result.exit_code == 0, result.output
    assert " REQ ci_green             pass" in result.output


def test_release_preflight_json_carries_the_ci_check(
    ready: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _host(monkeypatch, FakeHost(RED))

    result = CliRunner().invoke(
        app, ["--json", "release", "preflight", CUT_VERSION, "--source", _head(ready)]
    )

    payload = json.loads(result.output)
    body = payload["result"] if "operation" in payload else payload
    assert body["ci_green"] == {
        "status": "fail",
        "detail": f"every ci.yaml push run of {_head(ready)} failed (42 failure)",
    }


def test_release_preflight_skips_the_ci_check_the_tag_workflow_owns(
    ready: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _host(monkeypatch, FakeHost())

    result = _preflight(ready, "--skip-ci-runs")

    assert result.exit_code == 0, result.output
    assert " REQ ci_green             skipped" in result.output
    assert host.asked == []


def test_release_tag_workflow_skips_the_ci_lookup_its_ci_job_owns() -> None:
    """The tag workflow's preflight has no forge token; require-green-ci proves CI there."""
    workflow = Path(__file__).resolve().parents[4] / ".github" / "workflows" / "release.yaml"
    text = workflow.read_text(encoding="utf-8")

    assert 'uv run eawf release preflight "${TAG#v}" --source "${SHA}" --skip-ci-runs' in text
    assert "require-green-ci:" in text
