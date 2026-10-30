"""The release walk tags only after green push CI on the pinned commit and the record.

A tag push publishes, so the walk pins the source, waits for a successful
push-event CI run on exactly that commit, opens the release record and
sweeps preflight before it tags. Preflight itself refuses a commit CI has
not proven green on a push, and ``eawf release tag`` cannot overwrite a tag.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

import click
import pytest
import typer

from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import release_tag as release_tag_module
from eawf.workflow.release import pipeline as pipeline_module
from eawf.workflow.release.ci_runs import (
    CI_WORKFLOW,
    CiRun,
    failed_push_runs,
    green_push_run,
)
from eawf.workflow.release.pipeline import (
    PIPELINE_STEPS,
    PipelineHost,
    PipelineHostError,
    PipelineJournalRow,
    PipelineLayout,
    PipelineOptions,
    PipelineRefusal,
    PipelineRefusalCode,
    PipelineRpcError,
    PipelineStep,
    read_journal,
    run_post_merge_pipeline,
)
from eawf.workflow.release.pipeline_host import GitHubReleaseHost

VERSION: Final = "0.7.0.dev4"
MILESTONE: Final = "MLS-0103"
NOW: Final = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
#: Commit ids are derived rather than spelled, so no literal reads as a secret.
SOURCE: Final = hashlib.sha1(b"step-order-source").hexdigest()
OTHER: Final = hashlib.sha1(b"step-order-other").hexdigest()


def _run(
    *,
    event: str = "push",
    sha: str = SOURCE,
    status: str = "completed",
    conclusion: str = "success",
    run_id: int = 7,
) -> CiRun:
    return CiRun.model_validate(
        {
            "databaseId": run_id,
            "event": event,
            "headSha": sha,
            "status": status,
            "conclusion": conclusion,
        }
    )


GREEN: Final = _run()


# ---- step order --------------------------------------------------------------------


def test_pipeline_steps_tag_after_green_ci_the_record_and_preflight() -> None:
    named = ("pin_source", "ci_green", "create", "preflight", "tag", "publish_wait", "observe")
    assert [step.value for step in PIPELINE_STEPS if step.value in named] == list(named)
    assert PIPELINE_STEPS[0] is PipelineStep.PIN_SOURCE
    assert PIPELINE_STEPS.index(PipelineStep.CI_GREEN) == 1


def test_pipeline_steps_sweep_preflight_after_the_build_receipts_it_reads() -> None:
    order = PIPELINE_STEPS.index
    assert order(PipelineStep.BUILD_RECEIPTS) < order(PipelineStep.PREFLIGHT)
    assert order(PipelineStep.PUBLISH_WAIT) < order(PipelineStep.CANDIDATE)


# ---- the green push run lookup ---------------------------------------------------


def test_green_push_run_boundaries() -> None:
    assert green_push_run((), SOURCE) is None
    assert green_push_run((GREEN,), SOURCE) == GREEN
    later = _run(run_id=8)
    assert green_push_run((_run(conclusion="failure", run_id=6), later, GREEN), SOURCE) == later


@pytest.mark.parametrize(
    "run",
    [
        _run(conclusion="failure"),
        _run(conclusion="cancelled"),
        _run(status="in_progress", conclusion=""),
        _run(event="pull_request"),
        _run(event="schedule"),
        _run(sha=OTHER),
    ],
    ids=["failed", "cancelled", "running", "pull_request", "schedule", "other_sha"],
)
def test_green_push_run_refuses_a_run_that_is_not_a_green_push_on_the_commit(run: CiRun) -> None:
    assert green_push_run((run,), SOURCE) is None


def test_failed_push_runs_boundaries() -> None:
    failed = _run(conclusion="failure", run_id=9)
    assert failed_push_runs((), SOURCE) == ()
    assert failed_push_runs((failed,), SOURCE) == (failed,)
    assert failed_push_runs((failed, _run(conclusion="cancelled", run_id=10)), SOURCE) == (
        failed,
        _run(conclusion="cancelled", run_id=10),
    )


@pytest.mark.parametrize(
    "other",
    [_run(status="in_progress", conclusion="", run_id=10), _run(run_id=10)],
    ids=["rerun_running", "rerun_green"],
)
def test_failed_push_runs_holds_off_while_a_push_run_may_turn_green(other: CiRun) -> None:
    assert failed_push_runs((_run(conclusion="failure", run_id=9), other), SOURCE) == ()


@pytest.mark.parametrize(
    "run",
    [_run(event="pull_request", conclusion="failure"), _run(sha=OTHER, conclusion="failure")],
    ids=["pull_request", "other_sha"],
)
def test_failed_push_runs_ignores_a_run_that_is_no_push_on_the_commit(run: CiRun) -> None:
    assert failed_push_runs((run,), SOURCE) == ()


def test_ci_run_rejects_an_unknown_field() -> None:
    with pytest.raises(ValueError, match="extra"):
        CiRun.model_validate({**GREEN.model_dump(by_alias=True), "url": "x"})


def test_ci_run_rejects_a_missing_field() -> None:
    body = GREEN.model_dump(by_alias=True)
    del body["headSha"]
    with pytest.raises(ValueError, match="headSha"):
        CiRun.model_validate(body)


# ---- the steps against a fake host -------------------------------------------------


@dataclass
class _Host:
    """Git and the forge as the steps up to the tag push read them."""

    root: Path
    runs: list[tuple[CiRun, ...]] = field(default_factory=lambda: [(GREEN,)])
    blocker: str | None = None
    lookups: list[str] = field(default_factory=list)
    sleeps: list[float] = field(default_factory=list)
    pushes: list[str] = field(default_factory=list)

    @property
    def repo_root(self) -> Path:
        return self.root

    def dirty_paths(self) -> tuple[str, ...]:
        return ()

    def head(self) -> str:
        return SOURCE

    def ci_runs(self, sha: str) -> tuple[CiRun, ...]:
        self.lookups.append(sha)
        return self.runs.pop(0) if len(self.runs) > 1 else self.runs[0]

    def preflight(self, version: str, *, source: str) -> str | None:
        return self.blocker

    def remote_tag(self, tag: str) -> str | None:
        return None

    def target_ids(self, version: str) -> tuple[str, ...]:
        raise PipelineHostError("no checkpoint configuration here")

    def push_tag(self, tag: str, *, revision: str) -> None:
        self.pushes.append(tag)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def _no_rpc(method: str, params: Mapping[str, Any]) -> dict[str, Any]:
    """Refuse every daemon call, so a walk stops at the first step that needs one."""
    raise PipelineRpcError(f"no daemon here: {method}")


def _journal_through(root: Path, last: PipelineStep) -> None:
    """Journal every step up to and including *last*, as an earlier run would."""
    layout = PipelineLayout(root, VERSION)
    layout.journal.parent.mkdir(parents=True, exist_ok=True)
    steps = PIPELINE_STEPS[: PIPELINE_STEPS.index(last) + 1]
    rows = [
        PipelineJournalRow(
            version=VERSION,
            step=step,
            completed_at=NOW,
            facts=(
                {"source_sha": SOURCE, "milestone": MILESTONE, "evidence_dir": "evidence"}
                if step is PipelineStep.PIN_SOURCE
                else {}
            ),
        )
        for step in steps
    ]
    layout.journal.write_text("".join(row.model_dump_json() + "\n" for row in rows))


def _walk(host: _Host, *, publish: bool = False) -> PipelineRefusal:
    options = PipelineOptions(version=VERSION, milestone_key=MILESTONE, publish=publish)
    with pytest.raises(PipelineRefusal) as refused:
        run_post_merge_pipeline(
            options, host=cast(PipelineHost, host), rpc=_no_rpc, now=lambda: NOW
        )
    return refused.value


def _journaled(root: Path) -> list[PipelineStep]:
    return [row.step for row in read_journal(PipelineLayout(root, VERSION).journal)]


@pytest.mark.parametrize(
    "runs",
    [
        (),
        (_run(conclusion="failure"),),
        (_run(event="pull_request"),),
        (_run(sha=OTHER),),
    ],
    ids=["no_run", "failed_run", "pull_request_run", "other_sha_run"],
)
def test_preflight_refuses_a_commit_without_a_green_push_run(
    tmp_path: Path, runs: tuple[CiRun, ...]
) -> None:
    _journal_through(tmp_path, PipelineStep.BUILD_RECEIPTS)
    host = _Host(tmp_path, runs=[runs])
    refusal = _walk(host, publish=True)
    assert (refusal.step, refusal.code) == (
        PipelineStep.PREFLIGHT,
        PipelineRefusalCode.CI_NOT_GREEN,
    )
    assert SOURCE in refusal.detail
    assert host.lookups == [SOURCE]
    assert host.pushes == []
    assert PipelineStep.PREFLIGHT not in _journaled(tmp_path)


def test_preflight_admits_a_commit_with_a_green_push_run(tmp_path: Path) -> None:
    _journal_through(tmp_path, PipelineStep.BUILD_RECEIPTS)
    host = _Host(tmp_path, runs=[(_run(event="pull_request", run_id=3), GREEN)])
    refusal = _walk(host)
    # Preflight passed, so the walk reached the tag and stopped on the publish flag.
    assert (refusal.step, refusal.code) == (
        PipelineStep.TAG,
        PipelineRefusalCode.PUBLISH_FLAG_MISSING,
    )
    preflight = read_journal(PipelineLayout(tmp_path, VERSION).journal)[-1]
    assert preflight.step is PipelineStep.PREFLIGHT
    assert preflight.facts == {"ci_run": "7", "ready": "true"}
    assert host.pushes == []


def test_preflight_still_refuses_a_red_sweep_on_a_green_commit(tmp_path: Path) -> None:
    _journal_through(tmp_path, PipelineStep.BUILD_RECEIPTS)
    host = _Host(tmp_path, blocker="changelog (missing_section): add the section")
    refusal = _walk(host, publish=True)
    assert (refusal.step, refusal.code) == (
        PipelineStep.PREFLIGHT,
        PipelineRefusalCode.RELEASE_NOT_READY,
    )
    assert host.pushes == []


def test_tag_pushes_once_preflight_and_publish_admit_it(tmp_path: Path) -> None:
    _journal_through(tmp_path, PipelineStep.PREFLIGHT)
    host = _Host(tmp_path)
    _walk(host, publish=True)
    assert host.pushes == [f"v{VERSION}"]
    # The tag step trusts the journaled preflight and looks CI up no further.
    assert host.lookups == []


def test_ci_green_waits_for_the_push_run_to_succeed(tmp_path: Path) -> None:
    _journal_through(tmp_path, PipelineStep.PIN_SOURCE)
    running = _run(status="in_progress", conclusion="")
    host = _Host(tmp_path, runs=[(), (running,), (GREEN,)])
    _walk(host)
    assert host.lookups == [SOURCE, SOURCE, SOURCE]
    assert host.sleeps == [pipeline_module.CI_GREEN_BACKOFF_SECONDS] * 2
    ci = read_journal(PipelineLayout(tmp_path, VERSION).journal)[1]
    assert (ci.step, ci.facts) == (PipelineStep.CI_GREEN, {"ci_run": "7"})


def test_ci_green_refuses_a_failed_push_run_without_waiting(tmp_path: Path) -> None:
    _journal_through(tmp_path, PipelineStep.PIN_SOURCE)
    host = _Host(tmp_path, runs=[(_run(conclusion="failure", run_id=9),)])
    refusal = _walk(host)
    assert (refusal.step, refusal.code) == (
        PipelineStep.CI_GREEN,
        PipelineRefusalCode.CI_RUN_FAILED,
    )
    assert SOURCE in refusal.detail and "9" in refusal.detail
    assert host.sleeps == []
    assert _journaled(tmp_path) == [PipelineStep.PIN_SOURCE]


def test_ci_green_keeps_waiting_while_a_rerun_of_a_failed_run_is_in_progress(
    tmp_path: Path,
) -> None:
    _journal_through(tmp_path, PipelineStep.PIN_SOURCE)
    failed = _run(conclusion="failure", run_id=9)
    rerun = _run(status="in_progress", conclusion="", run_id=10)
    host = _Host(tmp_path, runs=[(failed, rerun), (failed, GREEN)])
    _walk(host)
    assert len(host.sleeps) == 1
    assert PipelineStep.CI_GREEN in _journaled(tmp_path)


def test_ci_green_refuses_once_the_wait_runs_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline_module, "CI_GREEN_ATTEMPTS", 2)
    _journal_through(tmp_path, PipelineStep.PIN_SOURCE)
    host = _Host(tmp_path, runs=[(_run(event="pull_request"),)])
    refusal = _walk(host)
    assert (refusal.step, refusal.code) == (PipelineStep.CI_GREEN, PipelineRefusalCode.CI_NOT_GREEN)
    assert SOURCE in refusal.detail
    assert host.lookups == [SOURCE, SOURCE]
    assert host.sleeps == [pipeline_module.CI_GREEN_BACKOFF_SECONDS]


def test_ci_green_refuses_a_forge_lookup_failure_by_name(tmp_path: Path) -> None:
    _journal_through(tmp_path, PipelineStep.PIN_SOURCE)

    class _Unreachable(_Host):
        def ci_runs(self, sha: str) -> tuple[CiRun, ...]:
            raise PipelineHostError("gh run list exited 4: auth required")

    refusal = _walk(_Unreachable(tmp_path))
    assert (refusal.step, refusal.code) == (PipelineStep.CI_GREEN, PipelineRefusalCode.CI_NOT_GREEN)
    assert "auth required" in refusal.detail


# ---- the GitHub host's lookup ------------------------------------------------------


def _github_host(tmp_path: Path, reply: str, argvs: list[Sequence[str]]) -> GitHubReleaseHost:
    host = GitHubReleaseHost(tmp_path, state_path=tmp_path / ".ea" / "state.json")

    def fake_run(argv: Sequence[str], *, timeout: float = 0.0) -> str:
        argvs.append(list(argv))
        return reply

    host._run = fake_run  # type: ignore[method-assign]
    return host


def test_github_host_lists_the_ci_runs_of_one_commit(tmp_path: Path) -> None:
    argvs: list[Sequence[str]] = []
    pull = _run(event="pull_request")
    reply = json.dumps([GREEN.model_dump(by_alias=True), pull.model_dump(by_alias=True)])
    runs = _github_host(tmp_path, reply, argvs).ci_runs(SOURCE)
    assert runs == (GREEN, pull)
    (argv,) = argvs
    assert argv[:3] == ["gh", "run", "list"]
    assert argv[argv.index("--workflow") + 1] == CI_WORKFLOW
    assert argv[argv.index("--commit") + 1] == SOURCE


def test_github_host_reads_no_runs_as_empty(tmp_path: Path) -> None:
    assert _github_host(tmp_path, "[]", []).ci_runs(SOURCE) == ()
    assert _github_host(tmp_path, "", []).ci_runs(SOURCE) == ()


def test_github_host_refuses_a_malformed_run(tmp_path: Path) -> None:
    host = _github_host(tmp_path, json.dumps([{"databaseId": 1}]), [])
    with pytest.raises(PipelineHostError, match=r"ci\.yaml"):
        host.ci_runs(SOURCE)


# ---- eawf release tag ---------------------------------------------------------------


def _tag_command() -> click.Command:
    root = typer.main.get_command(app)
    assert isinstance(root, click.Group)
    release = root.commands["release"]
    assert isinstance(release, click.Group)
    return release.commands["tag"]


def test_release_tag_offers_no_force_option() -> None:
    options = {opt for param in _tag_command().params for opt in param.opts}
    assert "--push" in options
    assert not options & {"--force", "-f"}
    assert "force" not in inspect.signature(release_tag_module._create_and_push_tag).parameters


def test_release_tag_help_names_the_v_star_trigger() -> None:
    help_text = _tag_command().help or ""
    assert "``v*``" in help_text
    assert "v0.*" not in help_text
