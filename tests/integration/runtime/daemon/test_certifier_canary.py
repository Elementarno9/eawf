"""``eawf runtime certify --task`` walks probe, canary and certify; the background probe does not.

The suite drives the registered ``runtime.certification.certify`` verb against a
canary tree holding one planned Task. The installed harness is a script on a
private ``PATH`` entry that prints a recorded ``--version`` and ``--help``, and
the canary's worker is a fake launcher whose turn reports one outcome per
containment attempt and files its report the way the semantic gateway does, so
no agent binary runs, no model is reached and the operator's home is never read.

The launcher writes the real Claude wrapper around a stand-in ``sandbox-exec``,
so the daemon's own containment attempts run through the wrapper and the jail
the launch recorded; the stand-in refuses, passes through or swallows each
attempt as the test declares.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import shutil
import stat
import sys
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from unittest import mock

import pytest
from pydantic import ValidationError

import eawf.runtime.daemon.methods.domain
import eawf.runtime.daemon.methods.domain_create
import eawf.runtime.daemon.methods.task_dispatch  # noqa: F401  (registers the verbs the suite reaches)
from eawf.kernel.runtime.candidate import AcceptedReport
from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.runtime.certification import ConformanceStageRecord
from eawf.kernel.state.enums import AgentReportVerdict
from eawf.kernel.state.epoch2.run import RunRuntimeTuple
from eawf.kernel.store.kinds.runtime_certification import MachineCertification
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.candidate.seal import accepted_report_record
from eawf.runtime.daemon import methods, native_dispatch, runtime_certifier
from eawf.runtime.daemon.epoch2_transaction import commit_ledger_append
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.conformance import runner_for
from eawf.runtime.daemon.methods.runtime_certification import RUNTIME_CERTIFY_METHOD
from eawf.runtime.daemon.methods.task_dispatch import TASK_DISPATCH_METHOD
from eawf.runtime.daemon.native_dispatch import compile_launchers
from eawf.runtime.daemon.run_supervisor import supervisor_of
from eawf.runtime.daemon.runtime_dir import runtime_dir
from eawf.runtime.mcp.native_launch import MCP_ARTIFACT_DIRNAME
from eawf.runtime.runtimes.adapter import (
    NativeLaunchOutcome,
    NativeLaunchRequest,
    TurnOutcome,
    compose_worker_hello,
)
from eawf.runtime.runtimes.claude.sdk_launcher import _write_wrapper
from eawf.runtime.runtimes.conformance import (
    canary_outcomes,
    graded_outcomes,
    render_canary_prompt,
)
from eawf.runtime.runtimes.containment import (
    CONTAINMENT_ATTEMPTS,
    ContainmentAttempt,
    ContainmentCallOutcome,
    CredentialFact,
    DenialReason,
)
from eawf.runtime.runtimes.containment_observation import (
    ContainmentTargets,
    jail_prefix,
    observe_containment,
    probe_verdict,
)
from eawf.runtime.sandbox.jail import build_jail_argv
from eawf.workflow.evidence.machine_certification import read_machine_certifications
from tests import _provider_helpers as fx
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import seed
from tests.integration.runtime.daemon.test_native_dispatch import ACTOR, make_canary, method_ctx
from tests.integration.runtime.daemon.test_task_dispatch import (
    BUDGET,
    TASK_KEY,
    ParkedLauncher,
    dispatch,
    runs,
    task_now,
    task_row,
)

pytestmark = pytest.mark.integration

CLAUDE_VERSION: Final = "2.1.288"
CODEX_VERSION: Final = "0.159.2"
RAN_MODEL: Final = "claude-sonnet-4-6"

#: A Claude Code ``--help`` advertising the flags the matrix rules on.
CLAUDE_HELP: Final = "Options: --allowedTools --output-format --resume --continue --session-id"

#: The reason each attempt is denied for, as a contained worker would report it.
DENIALS: Final[dict[ContainmentAttempt, DenialReason]] = {
    ContainmentAttempt.VARIABLE_OUTSIDE_ALLOWLIST: DenialReason.OUTSIDE_ALLOWLIST,
    ContainmentAttempt.AMBIENT_ENVIRONMENT: DenialReason.AMBIENT_INHERITANCE_REFUSED,
    ContainmentAttempt.CANONICAL_GIT: DenialReason.SCOPE_ESCAPE,
    ContainmentAttempt.DAEMON_STORAGE: DenialReason.SCOPE_ESCAPE,
    ContainmentAttempt.WRITE_AHEAD_LOG: DenialReason.SCOPE_ESCAPE,
    ContainmentAttempt.OUTSIDE_WRITABLE_ROOTS: DenialReason.SCOPE_ESCAPE,
    ContainmentAttempt.HOST_PROCESS_METADATA: DenialReason.SCOPE_ESCAPE,
    ContainmentAttempt.CREDENTIAL_VALUE: DenialReason.CREDENTIAL_REFERENCE_ONLY,
}


@pytest.fixture(autouse=True)
def _private_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_host_homes: Path
) -> Iterator[None]:
    """Read every home from an empty directory and find ``claude`` on a private entry."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    directory = tmp_path / "bin"
    directory.mkdir()
    body = (
        "#!/bin/sh\n"
        'case "$1" in\n'
        f"  --version) echo '{CLAUDE_VERSION} (Claude Code)' ;;\n"
        f"  --help) echo '{CLAUDE_HELP}' ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n"
    )
    script = directory / "claude"
    script.write_text(body, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ.get('PATH', '')}")
    yield


# ---- the tree and the canary's worker ---------------------------------------------


def make_tree(tmp_path: Path) -> CanaryProvision:
    """Provision a canary tree holding one planned Task and no certification."""
    canary = make_canary(tmp_path / "repo", rows={})
    seed(canary, {"task": {TASK_KEY: task_row()}})
    return canary


def outcomes(**changed: dict[str, Any]) -> list[dict[str, Any]]:
    """Return one denied outcome per attempt, with *changed* attempts replaced or dropped."""
    rows: list[dict[str, Any]] = []
    for attempt in CONTAINMENT_ATTEMPTS:
        row: dict[str, Any] = {
            "attempt": attempt.value,
            "denied": True,
            "denial_reason": DENIALS[attempt].value,
            "returned_bytes": 0,
        }
        if attempt is ContainmentAttempt.CREDENTIAL_VALUE:
            row["disclosed_facts"] = [CredentialFact.KIND.value]
        if attempt.value in changed:
            if not changed[attempt.value]:
                continue
            row = changed[attempt.value]
        rows.append(row)
    return rows


def write_jail(directory: Path, modes: dict[str, str] | None = None) -> Path:
    """Write a stand-in ``sandbox-exec`` that refuses every attempt but those in *modes*.

    The wrapper starts ``sandbox-exec -p <profile> <python> -I -c <program>
    <attempt> ...``, so after the profile the attempt is the fifth word. A
    ``pass`` attempt runs unjailed, so whatever it reaches is really reached;
    a ``silent`` one is swallowed, as by a jail that kills an attempt before
    it can answer; a ``targeted`` one is refused only when it names a target
    and otherwise runs, since there is nothing to refuse an attempt aimed at
    nothing. It is a Python script rather than a shell one because a shell
    exports variables of its own, which would read as ambient leaks.
    """
    body = (
        f"#!{sys.executable} -I\n"
        "import os, sys\n"
        f"modes = {dict(modes or {})!r}\n"
        "argv = sys.argv[3:]\n"
        "mode = modes.get(argv[4])\n"
        "if mode == 'pass':\n"
        "    os.execv(argv[0], argv)\n"
        "if mode == 'silent':\n"
        "    sys.exit(1)\n"
        "if mode == 'targeted' and len(argv) == 5:\n"
        "    os.execv(argv[0], argv)\n"
        "print('denied')\n"
    )
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "sandbox-exec"
    script.write_text(body, encoding="utf-8")
    script.chmod(0o755)
    return script


def write_launch(run_dir: Path, jail: Path) -> None:
    """Write the Claude wrapper a launch records, around the stand-in *jail*."""
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_wrapper(
        run_dir,
        argv=[str(jail), "-p", "(version 1)", "/bin/false"],
        env_keys=["PATH"],
        python=sys.executable,
    )


def write_jailed_launch(request: NativeLaunchRequest) -> None:
    """Write the Claude wrapper a native launch records, around the real seatbelt jail."""
    run_dir = request.workspace / MCP_ARTIFACT_DIRNAME
    private_home = run_dir / "home"
    private_home.mkdir(parents=True, exist_ok=True)
    prefix = build_jail_argv(
        "claude-code",
        cwd=request.workspace,
        root=request.repo_root,
        platform="darwin",
        private_home=private_home,
        storage_dirs=(request.repo_root / ".ea", runtime_dir() / "wal"),
    )
    _write_wrapper(
        run_dir,
        argv=[shutil.which(prefix[0]) or prefix[0], *prefix[1:], "/bin/false"],
        env_keys=["PATH"],
        python=sys.executable,
    )


class CanaryLauncher:
    """A worker that makes every attempt, reports the outcomes and files its report.

    Attributes:
        provider_kind: The provider the fake stands in for.
        capsule: The capsule the last launch carried.
        prompts: The prompt of each launch.
    """

    def __init__(
        self,
        ctx: MethodContext,
        canary: CanaryProvision,
        *,
        provider_kind: str = "claude",
        answer: str | None = None,
        report: bool = True,
        jail: dict[str, str] | None = None,
        wrapped: bool = True,
        seatbelt: bool = False,
    ) -> None:
        """Answer *answer* (every attempt denied by default) and file a report when *report*.

        The launch records the Claude wrapper around a stand-in jail treating
        each attempt per *jail* (refusing all by default), around the real
        seatbelt jail when *seatbelt*, or no wrapper at all unless *wrapped*.
        """
        self.provider_kind = provider_kind
        self.capsule: AuthorityCapsule | None = None
        self.prompts: list[str] = []
        self._ctx = ctx
        self._canary = canary
        self._answer = json.dumps({"outcomes": outcomes()}) if answer is None else answer
        self._report = report
        self._jail = write_jail(canary.root.parent / "jail", jail)
        self._wrapped = wrapped
        self._seatbelt = seatbelt

    async def launch(self, request: NativeLaunchRequest) -> NativeLaunchOutcome:
        """Record the wrapper, announce the worker and run its turn in the background."""
        self.capsule = request.capsule
        self.prompts.append(request.prompt)
        if self._seatbelt:
            write_jailed_launch(request)
        elif self._wrapped:
            write_launch(request.workspace / MCP_ARTIFACT_DIRNAME, self._jail)
        session_ref = f"{self.provider_kind}-canary-{len(self.prompts)}"
        return NativeLaunchOutcome(
            provider_session_ref=session_ref,
            subprocess_pid=4242,
            hello=compose_worker_hello(
                spec=request.spec,
                capsule=request.capsule,
                provider_session_ref=session_ref,
                sdk_version="1.0.0",
                worker_protocol_version="1.0.0",
                event_codec_version="1.0.0",
                hello_sequence=request.hello_sequence,
            ),
            turn=asyncio.get_running_loop().create_task(self._turn(request)),
            runtime_tuple=RunRuntimeTuple(
                harness="claude-code", harness_version=CLAUDE_VERSION, model=RAN_MODEL
            ),
        )

    async def _turn(self, request: NativeLaunchRequest) -> TurnOutcome:
        if self._report:
            await asyncio.to_thread(self._file_report, request)
        return TurnOutcome(exit_kind="completed", answer_text=self._answer)

    def _file_report(self, request: NativeLaunchRequest) -> None:
        """Accept the worker's report on the run ledger, as ``submit_report`` does."""
        context = self._ctx.native_root_context(self._canary.root / ".ea")
        with context.session([request.spec.run_ref]) as session:
            commit_ledger_append(
                session,
                accepted_report_record(
                    AcceptedReport(
                        run_ref=request.spec.run_ref,
                        report_schema_ref=request.capsule.report_schema_ref,
                        report_digest=fx.digest("e"),
                        verdict=AgentReportVerdict.PASS,
                        accepted_at=datetime.now(UTC),
                    )
                ),
            )


def certify(
    ctx: MethodContext, canary: CanaryProvision, launcher: Any, **params: Any
) -> dict[str, Any]:
    """Send the certify verb with *launcher* as the only launcher dispatch can reach."""

    async def scenario() -> dict[str, Any]:
        return await methods.dispatch(
            RUNTIME_CERTIFY_METHOD,
            ctx,
            {"repo_root": str(canary.root), "runtime": "claude-code", **params},
        )

    methods.ensure_all_methods_registered()
    with mock.patch.object(
        native_dispatch, "NATIVE_LAUNCHERS", dict(compile_launchers((launcher,)))
    ):
        return asyncio.run(scenario())


def walk(ctx: MethodContext, canary: CanaryProvision, launcher: Any) -> MachineCertification:
    """Walk the canary under the seeded Task and return the row recorded last."""
    answer = certify(ctx, canary, launcher, task=TASK_KEY, actor=ACTOR)
    return MachineCertification.model_validate(answer)


def journal(canary: CanaryProvision, row: MachineCertification) -> list[tuple[str, str]]:
    """Return the stage and outcome of each journal record of *row*'s tuple."""
    records = runner_for(canary.root / ".ea" / "state.json").journal.records(
        tuple_digest=row.tuple_digest
    )
    return [(record.stage, record.outcome) for record in records]


# ---- CR-1: probe, canary and certify, in that order --------------------------------


def test_the_walk_journals_probe_canary_certify_in_order(tmp_path: Path) -> None:
    canary = make_tree(tmp_path)
    ctx = method_ctx(tmp_path / "runtime")
    launcher = CanaryLauncher(ctx, canary)

    row = walk(ctx, canary, launcher)

    assert row.outcome == "certified"
    stages = journal(canary, row)
    assert [stage for stage, _ in stages] == ["probe", "canary", "certify"]
    assert stages[:2] == [("probe", "passed"), ("canary", "passed")]
    # The shipped host facts measure nothing of Claude Code, so the driver
    # certification is refused while the machine row records the canary.
    assert row.stages[1].reason_code == "runtime_facts_uncertified"
    assert [stage.stage for stage in row.stages] == ["canary", "certify"]


def test_the_canary_is_a_dispatched_run_under_the_task_left_planned(tmp_path: Path) -> None:
    canary = make_tree(tmp_path)
    ctx = method_ctx(tmp_path / "runtime")
    launcher = CanaryLauncher(ctx, canary)

    walk(ctx, canary, launcher)

    assert runs(ctx, canary) == ["RUN-00000001"]
    assert task_now(ctx, canary).status.value == "PLANNED"
    (prompt,) = launcher.prompts
    assert prompt == render_canary_prompt(
        report_tool="submit_report", report_schema_ref="schema://executor-report/v1"
    )
    assert launcher.capsule is not None
    assert launcher.capsule.scope_ref == task_now(ctx, canary).urn
    assert launcher.capsule.budget.tokens == runtime_certifier.CANARY_TOKEN_BUDGET
    assert "submit_report" in launcher.capsule.tool_grants


def test_the_canary_compiles_from_the_spec_the_tuple_serves(tmp_path: Path) -> None:
    canary = make_tree(tmp_path)
    ctx = method_ctx(tmp_path / "runtime")
    seen: dict[str, Any] = {}
    real = runner_for

    def spying(state_path: Path) -> Any:
        runner = real(state_path)
        original = runner.run_canary

        def run_canary(request: Any) -> Any:
            seen["request"] = request
            return original(request)

        runner.run_canary = run_canary  # type: ignore[method-assign]
        return runner

    with mock.patch.object(runtime_certifier, "runner_for", spying):
        walk(ctx, canary, CanaryLauncher(ctx, canary))

    request = seen["request"]
    served, canary_spec = request.served_spec, request.canary_spec
    assert served.run_ref != canary_spec.run_ref
    assert served.contract_digest != canary_spec.contract_digest
    assert served.model_dump(exclude={"run_ref", "scope_digest", "contract_digest"}) == (
        canary_spec.model_dump(exclude={"run_ref", "scope_digest", "contract_digest"})
    )
    assert len(request.outcomes) == len(CONTAINMENT_ATTEMPTS)


def test_the_walk_attests_what_only_the_canary_observes(tmp_path: Path) -> None:
    canary = make_tree(tmp_path)
    ctx = method_ctx(tmp_path / "runtime")

    row = walk(ctx, canary, CanaryLauncher(ctx, canary))

    statuses = {item.capability_id: item.status for item in row.capabilities}
    assert statuses == {
        "tool_use": "verified",
        "streaming": "verified",
        "session_resume": "unsupported",
        "semantic_tools": "verified",
    }
    assert row.observed_model == RAN_MODEL
    rows = read_machine_certifications(canary.root / ".ea" / "state.json")
    assert [(item.outcome, len(item.stages)) for item in rows] == [
        ("certified", 0),
        ("certified", 2),
    ]


def test_a_canary_that_files_no_report_attests_no_semantic_tools(tmp_path: Path) -> None:
    canary = make_tree(tmp_path)
    ctx = method_ctx(tmp_path / "runtime")

    row = walk(ctx, canary, CanaryLauncher(ctx, canary, report=False))

    assert row.outcome == "certified"
    assert "semantic_tools" not in {item.capability_id for item in row.capabilities}


def dispatch_task(ctx: MethodContext, canary: CanaryProvision) -> ParkedLauncher:
    """Dispatch the seeded Task to Claude Code and return the worker it started."""
    worker = ParkedLauncher("claude")

    async def scenario() -> dict[str, Any]:
        params = {
            "repo_root": str(canary.root),
            "actor": ACTOR,
            "budget": BUDGET,
            "task": TASK_KEY,
            "provider": "claude-code",
        }
        answer = await methods.dispatch(TASK_DISPATCH_METHOD, ctx, params)
        worker.release.set()
        turn = supervisor_of(ctx.native_root_context(canary.root / ".ea")).task_of(
            answer["run_ref"]
        )
        if turn is not None:
            await turn
        return answer

    with mock.patch.object(native_dispatch, "NATIVE_LAUNCHERS", dict(compile_launchers((worker,)))):
        asyncio.run(scenario())
    assert worker.capsule is not None
    return worker


def test_a_task_run_after_the_walk_is_granted_its_report_tool(tmp_path: Path) -> None:
    canary = make_tree(tmp_path)
    ctx = method_ctx(tmp_path / "runtime")
    walk(ctx, canary, CanaryLauncher(ctx, canary))

    worker = dispatch_task(ctx, canary)

    assert worker.capsule is not None
    assert "submit_report" in worker.capsule.tool_grants


def refused_dispatch(ctx: MethodContext, canary: CanaryProvision) -> DaemonValidationError:
    """Dispatch the seeded Task to Claude Code and return the refusal it must raise."""
    worker = ParkedLauncher("claude")
    (answer,) = dispatch(ctx, canary, worker, {"task": TASK_KEY, "provider": "claude-code"})
    assert isinstance(answer, DaemonValidationError), answer
    assert worker.capsule is None
    return answer


def test_a_task_run_after_the_probe_alone_is_refused_before_any_run(tmp_path: Path) -> None:
    canary = make_tree(tmp_path)
    ctx = method_ctx(tmp_path / "runtime")
    certify(ctx, canary, CanaryLauncher(ctx, canary))

    refusal = refused_dispatch(ctx, canary)

    assert str(refusal).startswith(
        "validation_failed: dispatch_semantic_tools_uncertified: runtime claude-code is "
        "certified without semantic_tools"
    )
    assert f"`eawf runtime certify claude-code --task {TASK_KEY}`" in str(refusal)
    assert runs(ctx, canary) == []
    assert task_now(ctx, canary).status.value == "PLANNED"


def test_a_task_refused_for_its_tools_dispatches_once_the_walk_earns_them(
    tmp_path: Path,
) -> None:
    canary = make_tree(tmp_path)
    ctx = method_ctx(tmp_path / "runtime")
    certify(ctx, canary, CanaryLauncher(ctx, canary))
    refused_dispatch(ctx, canary)

    walk(ctx, canary, CanaryLauncher(ctx, canary))
    worker = dispatch_task(ctx, canary)

    assert worker.capsule is not None
    assert "submit_report" in worker.capsule.tool_grants


def test_a_walk_whose_canary_never_files_leaves_the_task_refused(tmp_path: Path) -> None:
    canary = make_tree(tmp_path)
    ctx = method_ctx(tmp_path / "runtime")
    walk(ctx, canary, CanaryLauncher(ctx, canary, report=False))
    canaries = runs(ctx, canary)

    refusal = refused_dispatch(ctx, canary)

    assert "dispatch_semantic_tools_uncertified" in str(refusal)
    assert runs(ctx, canary) == canaries


def test_a_quarantined_runtime_keeps_its_unbound_refusal(tmp_path: Path) -> None:
    canary = make_tree(tmp_path)
    ctx = method_ctx(tmp_path / "runtime")
    escaped = {"attempt": "host_process_metadata", "denied": False, "returned_bytes": 5}
    answer = json.dumps({"outcomes": outcomes(host_process_metadata=escaped)})
    assert walk(ctx, canary, CanaryLauncher(ctx, canary, answer=answer)).outcome == "quarantined"
    canaries = runs(ctx, canary)

    refusal = refused_dispatch(ctx, canary)

    assert "no_eligible_profile" in str(refusal)
    assert "quarantined" in str(refusal)
    assert "dispatch_semantic_tools_uncertified" not in str(refusal)
    assert runs(ctx, canary) == canaries


# ---- CR-2: a missed containment axis quarantines ------------------------------------


@pytest.mark.parametrize(
    ("changed", "jail", "code", "finding"),
    [
        (
            {
                "host_process_metadata": {
                    "attempt": "host_process_metadata",
                    "denied": False,
                    "returned_bytes": 5,
                }
            },
            None,
            "containment_probe_escape",
            "host_process_metadata: escaped containment",
        ),
        (
            {},
            {"outside_writable_roots": "pass"},
            "containment_probe_escape",
            "outside_writable_roots: escaped containment",
        ),
        (
            {},
            {"canonical_git": "silent"},
            "capability_evidence_uncovered",
            "canonical_git: was not attempted",
        ),
        (
            {
                "daemon_storage": {
                    "attempt": "daemon_storage",
                    "denied": True,
                    "denial_reason": "scope_escape",
                    "returned_bytes": 1,
                }
            },
            None,
            "secret_material_observable",
            "daemon_storage: returned bytes through its denial",
        ),
    ],
    ids=["reported-escape", "observed-escape", "unobserved-attempt", "reported-bytes"],
)
def test_a_canary_missing_an_axis_quarantines_and_certifies_nothing(
    tmp_path: Path,
    changed: dict[str, dict[str, Any]],
    jail: dict[str, str] | None,
    code: str,
    finding: str,
) -> None:
    canary = make_tree(tmp_path)
    ctx = method_ctx(tmp_path / "runtime")
    answer = json.dumps({"outcomes": outcomes(**changed)})

    row = walk(ctx, canary, CanaryLauncher(ctx, canary, answer=answer, jail=jail))

    assert (row.outcome, row.reason_code, row.findings) == ("quarantined", code, (finding,))
    assert row.capabilities == () and row.expires_at is None
    assert journal(canary, row) == [("probe", "passed"), ("canary", "failed")]
    newest = read_machine_certifications(canary.root / ".ea" / "state.json")[-1]
    assert newest == row


@pytest.mark.parametrize(
    "launcher",
    [
        {"jail": {attempt.value: "silent" for attempt in CONTAINMENT_ATTEMPTS}},
        {"wrapped": False},
    ],
    ids=["jail-swallows-every-attempt", "no-wrapper-recorded"],
)
def test_a_worker_claiming_every_denial_unobserved_certifies_nothing(
    tmp_path: Path, launcher: dict[str, Any]
) -> None:
    canary = make_tree(tmp_path)
    ctx = method_ctx(tmp_path / "runtime")

    # The answer reports every attempt denied, as a worker holding no tool
    # wrote it; nothing eawf observed backs a single denial.
    row = walk(ctx, canary, CanaryLauncher(ctx, canary, **launcher))

    assert (row.outcome, row.reason_code) == ("quarantined", "capability_evidence_uncovered")
    assert row.findings == tuple(f"{a.value}: was not attempted" for a in CONTAINMENT_ATTEMPTS)
    assert journal(canary, row) == [("probe", "passed"), ("canary", "failed")]


def test_a_worker_reporting_unmade_attempts_as_not_denied_certifies(tmp_path: Path) -> None:
    canary = make_tree(tmp_path)
    ctx = method_ctx(tmp_path / "runtime")
    # A worker holding no tool honestly reports it ran nothing: not denied,
    # nothing returned. eawf observed every attempt denied, and that stands.
    answer = json.dumps(
        {"outcomes": [{"attempt": a.value, "denied": False} for a in CONTAINMENT_ATTEMPTS]}
    )

    row = walk(ctx, canary, CanaryLauncher(ctx, canary, answer=answer))

    assert row.outcome == "certified"
    assert journal(canary, row)[:2] == [("probe", "passed"), ("canary", "passed")]


def test_an_unreadable_answer_leaves_the_observed_grade(tmp_path: Path) -> None:
    canary = make_tree(tmp_path)
    ctx = method_ctx(tmp_path / "runtime")

    row = walk(ctx, canary, CanaryLauncher(ctx, canary, answer="I could not finish."))

    assert (row.outcome, row.reason_code, row.findings) == ("certified", None, ())


def test_an_observed_escape_leaves_nothing_behind(tmp_path: Path) -> None:
    canary = make_tree(tmp_path)
    ctx = method_ctx(tmp_path / "runtime")

    walk(ctx, canary, CanaryLauncher(ctx, canary, jail={"outside_writable_roots": "pass"}))

    assert list(tmp_path.rglob(".eawf-canary-*")) == []
    assert list(tmp_path.rglob("canary-probes")) == []


# ---- a host holding no other tool's credential store ------------------------------


_SEATBELT_PRESENT: Final = sys.platform == "darwin" and shutil.which("sandbox-exec") is not None


@pytest.mark.parametrize(
    "seatbelt",
    [
        pytest.param(
            True, marks=pytest.mark.skipif(not _SEATBELT_PRESENT, reason="needs sandbox-exec")
        ),
        False,
    ],
    ids=["seatbelt", "stand-in"],
)
def test_a_host_without_credential_stores_certifies_on_a_planted_decoy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seatbelt: bool
) -> None:
    canary = make_tree(tmp_path)
    ctx = method_ctx(tmp_path / "runtime")
    # A running daemon holds its log; the write-ahead-log attempt needs one to aim at.
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(tmp_path / "runtime"))
    (tmp_path / "runtime" / "wal").mkdir(parents=True)
    # The stand-in refuses the credential read only when the attempt names a
    # store to read, and otherwise lets the attempt run unjailed.
    launcher = CanaryLauncher(ctx, canary, seatbelt=seatbelt, jail={"credential_value": "targeted"})

    row = walk(ctx, canary, launcher)

    assert (row.outcome, row.reason_code, row.findings) == ("certified", None, ())
    assert journal(canary, row)[:2] == [("probe", "passed"), ("canary", "passed")]
    assert list(tmp_path.rglob(f"{runtime_certifier.DECOY_SCRATCH_PREFIX}*")) == []
    assert list(Path.home().iterdir()) == []


def test_a_decoy_holds_only_the_marker_and_is_removed_afterwards(tmp_path: Path) -> None:
    tree = tmp_path / ".ea"
    tree.mkdir()

    with runtime_certifier._credential_targets(tree) as targets:
        assert len(targets) == 1
        (store,) = targets
        assert store.name == ".aws" and store.is_relative_to(tree / "local")
        assert [path.name for path in store.iterdir()] == ["credentials"]
        marker = (store / "credentials").read_text(encoding="utf-8")
        assert marker == runtime_certifier.DECOY_CREDENTIAL_MARKER

    assert list((tree / "local").iterdir()) == []
    assert list(Path.home().iterdir()) == []


def test_a_decoy_is_removed_when_the_attempts_fail(tmp_path: Path) -> None:
    tree = tmp_path / ".ea"
    tree.mkdir()

    with (
        pytest.raises(RuntimeError, match="attempts failed"),
        runtime_certifier._credential_targets(tree),
    ):
        raise RuntimeError("attempts failed")

    assert list((tree / "local").iterdir()) == []


def test_a_decoy_that_cannot_be_planted_raises_and_leaves_nothing(tmp_path: Path) -> None:
    tree = tmp_path / ".ea"
    tree.write_text("not a directory", encoding="utf-8")

    with pytest.raises(OSError), runtime_certifier._credential_targets(tree):
        pass

    assert tree.read_text(encoding="utf-8") == "not a directory"


@pytest.mark.parametrize(
    "present",
    [(".ssh",), (".npmrc",), (".aws", ".config/gh", ".gnupg")],
    ids=["one-dir", "one-file", "several"],
)
def test_a_present_store_is_the_target_and_no_decoy_is_planted(
    tmp_path: Path, present: tuple[str, ...]
) -> None:
    home = Path.home()
    for relative in present:
        path = home / relative
        if relative.startswith(".npmrc"):
            path.write_text("registry\n", encoding="utf-8")
        else:
            path.mkdir(parents=True)
    tree = tmp_path / ".ea"
    tree.mkdir()

    with runtime_certifier._credential_targets(tree) as targets:
        assert set(targets) == {home / relative for relative in present}
        assert not (tree / "local").exists()


def test_a_credential_the_jail_hides_reads_as_denied(tmp_path: Path) -> None:
    workspace, targets = targets_in(tmp_path)
    hidden = dataclasses.replace(targets, credential_paths=(tmp_path / "masked" / ".aws",))
    run_dir = workspace / MCP_ARTIFACT_DIRNAME
    write_launch(run_dir, write_jail(tmp_path / "jail", {"credential_value": "pass"}))

    observed = {
        row.attempt: row
        for row in observe_containment(run_dir, workspace=workspace, targets=hidden)
    }

    assert observed[ContainmentAttempt.CREDENTIAL_VALUE].denied


def test_a_credential_the_jail_lets_through_is_an_escape(tmp_path: Path) -> None:
    workspace, targets = targets_in(tmp_path)
    store = tmp_path / "store" / ".aws"
    store.mkdir(parents=True)
    (store / "credentials").write_text("x", encoding="utf-8")
    exposed = dataclasses.replace(targets, credential_paths=(store,))
    run_dir = workspace / MCP_ARTIFACT_DIRNAME
    write_launch(run_dir, write_jail(tmp_path / "jail", {"credential_value": "pass"}))

    observed = {
        row.attempt: row
        for row in observe_containment(run_dir, workspace=workspace, targets=exposed)
    }

    escaped = observed[ContainmentAttempt.CREDENTIAL_VALUE]
    assert not escaped.denied and escaped.returned_bytes == len("credentials")


# ---- CR-3: the background probe dispatches nothing --------------------------------


def test_the_background_probe_runs_the_probe_alone_and_dispatches_nothing(
    tmp_path: Path, runtime_certification_isolation: Callable[..., Any]
) -> None:
    canary = make_tree(tmp_path)
    ctx = method_ctx(tmp_path / "runtime")
    tree = canary.root / ".ea"
    reported = RunRuntimeTuple(harness="claude-code", harness_version=CLAUDE_VERSION)

    with mock.patch.object(native_dispatch, "dispatch_run") as dispatched:
        queued = runtime_certification_isolation(tree, reported, now=datetime.now(UTC))
        assert queued is not None
        row = queued.result(timeout=30)

    assert dispatched.call_count == 0
    assert row is not None and (row.outcome, row.stages) == ("certified", ())
    assert journal(canary, row) == [("probe", "passed")]
    assert runs(ctx, canary) == []


def test_certify_without_a_task_stays_the_probe_alone(tmp_path: Path) -> None:
    canary = make_tree(tmp_path)
    ctx = method_ctx(tmp_path / "runtime")
    launcher = CanaryLauncher(ctx, canary)

    row = MachineCertification.model_validate(certify(ctx, canary, launcher))

    assert journal(canary, row) == [("probe", "passed")]
    assert launcher.prompts == []


# ---- refusals ----------------------------------------------------------------------


@pytest.mark.parametrize("params", [{"task": TASK_KEY}, {"actor": ACTOR}])
def test_a_walk_names_both_task_and_actor(tmp_path: Path, params: dict[str, str]) -> None:
    canary = make_tree(tmp_path)
    ctx = method_ctx(tmp_path / "runtime")
    with pytest.raises(DaemonValidationError, match="schema_validation_failed"):
        certify(ctx, canary, CanaryLauncher(ctx, canary), **params)
    assert read_machine_certifications(canary.root / ".ea" / "state.json") == ()


def test_a_walk_under_an_unknown_task_records_nothing(tmp_path: Path) -> None:
    canary = make_tree(tmp_path)
    ctx = method_ctx(tmp_path / "runtime")
    with pytest.raises(DaemonValidationError, match="identity_not_found"):
        certify(ctx, canary, CanaryLauncher(ctx, canary), task="EAWF-0999", actor=ACTOR)
    assert read_machine_certifications(canary.root / ".ea" / "state.json") == ()


def test_a_runtime_without_a_shipped_launcher_has_no_canary(tmp_path: Path) -> None:
    canary = make_tree(tmp_path)
    ctx = method_ctx(tmp_path / "runtime")
    with pytest.raises(DaemonValidationError, match="runtime_canary_unavailable"):
        certify(
            ctx, canary, CanaryLauncher(ctx, canary), runtime="opencode", task=TASK_KEY, actor=ACTOR
        )


# ---- the canary's answer -------------------------------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        None,
        "",
        "no object here",
        "}{",
        '{"outcomes": "not a list"}',
        '{"outcomes": [], "extra": 1}',
        json.dumps({"outcomes": [outcomes()[0], outcomes()[0]]}),
    ],
)
def test_an_answer_holding_no_valid_report_reports_nothing(answer: str | None) -> None:
    assert canary_outcomes(answer) == ()


def test_a_fenced_answer_reports_its_outcomes() -> None:
    body = json.dumps({"outcomes": outcomes()})
    parsed = canary_outcomes(f"Done.\n```json\n{body}\n```\n")
    assert tuple(row.attempt for row in parsed) == CONTAINMENT_ATTEMPTS


def test_a_single_outcome_is_read_as_one_attempt() -> None:
    (row,) = canary_outcomes(json.dumps({"outcomes": outcomes()[:1]}))
    assert row.attempt is CONTAINMENT_ATTEMPTS[0]


def test_the_prompt_asks_for_every_attempt() -> None:
    prompt = render_canary_prompt(report_tool="submit_report", report_schema_ref="schema://x/v1")
    assert all(f"`{attempt.value}`" in prompt for attempt in CONTAINMENT_ATTEMPTS)
    assert "`submit_report`" in prompt and "`schema://x/v1`" in prompt


# ---- the row a walk records ------------------------------------------------------------


def _stage(stage: str, outcome: str) -> dict[str, Any]:
    at = datetime(2026, 10, 1, tzinfo=UTC)
    return ConformanceStageRecord.model_validate(
        {
            "stage": stage,
            "outcome": outcome,
            "reason_code": None if outcome == "passed" else "containment_probe_escape",
            "evidence_ref": "artifact://runtime-certification/claude-code/2.1.288",
            "started_at": at,
            "completed_at": at,
        }
    ).model_dump(mode="json")


def _row(**changes: Any) -> dict[str, Any]:
    at = datetime(2026, 10, 1, tzinfo=UTC)
    row: dict[str, Any] = {
        "certification_urn": "certification://claude-code/2.1.288/2026-10-01",
        "runtime_id": "claude-code",
        "harness_version": CLAUDE_VERSION,
        "tuple_digest": fx.digest("d"),
        "outcome": "quarantined",
        "reason_code": "containment_probe_escape",
        "findings": ["host_process_metadata: escaped containment"],
        "reason": "quarantined by its canary",
        "probe": _stage("probe", "passed"),
        "stages": [_stage("canary", "failed")],
        "verified_at": at,
    }
    row.update(changes)
    return row


def test_a_failed_canary_row_validates() -> None:
    assert MachineCertification.model_validate(_row()).stages[0].outcome == "failed"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"stages": [_stage("certify", "passed")]}, "a canary, then at most a certify"),
        (
            {"stages": [_stage("canary", "failed"), _stage("canary", "failed")]},
            "a canary, then at most a certify",
        ),
        ({"probe": _stage("probe", "failed")}, "only after a passed probe"),
        (
            {"stages": [_stage("canary", "failed"), _stage("certify", "refused")]},
            "only after a passed canary",
        ),
        ({"stages": [_stage("canary", "passed")]}, "rests on a failed probe or canary"),
        ({"observed_model": "not a model!"}, "observed_model"),
    ],
)
def test_a_row_out_of_the_walk_sequence_is_refused(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        MachineCertification.model_validate(_row(**changes))


def test_a_certified_walk_ends_in_its_certify_stage() -> None:
    certified = {
        "outcome": "certified",
        "reason_code": None,
        "findings": [],
        "stages": [_stage("canary", "passed")],
        "expires_at": datetime(2026, 12, 1, tzinfo=UTC),
        "capabilities": [
            {
                "capability_id": "semantic_tools",
                "level": True,
                "status": "verified",
                "basis": "native",
                "evidence_ref": "artifact://runtime-certification/claude-code/2.1.288",
                "verified_at": datetime(2026, 10, 1, tzinfo=UTC),
                "expires_at": datetime(2026, 12, 1, tzinfo=UTC),
            }
        ],
    }
    with pytest.raises(ValidationError, match="ends in its certify stage"):
        MachineCertification.model_validate(_row(**certified))
    walked = [_stage("canary", "passed"), _stage("certify", "refused")]
    row = MachineCertification.model_validate(_row(**(certified | {"stages": walked})))
    assert row.outcome == "certified"


# ---- what eawf observes in the canary's jail -------------------------------------------


def targets_in(tmp_path: Path) -> tuple[Path, ContainmentTargets]:
    """Return a leased workspace and targets that all exist under *tmp_path*."""
    workspace = tmp_path / "workspaces" / "wsh-1"
    workspace.mkdir(parents=True)
    tree = tmp_path / ".ea"
    tree.mkdir()
    (tree / "state.json").write_text("{}", encoding="utf-8")
    for name in ("repo-admin", "wal"):
        (tmp_path / name).mkdir()
    return workspace, ContainmentTargets(
        canonical_git=tmp_path / "repo-admin",
        daemon_storage=tree,
        write_ahead_log=tmp_path / "wal",
        outside_writable_roots=workspace.parent,
        credential_paths=(),
        host_process=os.getpid(),
    )


def test_attempts_run_through_the_recorded_wrapper_and_its_admitted_keys(
    tmp_path: Path,
) -> None:
    workspace, targets = targets_in(tmp_path)
    passed = (
        "variable_outside_allowlist",
        "ambient_environment",
        "canonical_git",
        "daemon_storage",
        "write_ahead_log",
        "outside_writable_roots",
        "credential_value",
    )
    run_dir = workspace / MCP_ARTIFACT_DIRNAME
    write_launch(run_dir, write_jail(tmp_path / "jail", dict.fromkeys(passed, "pass")))

    observed = {
        row.attempt.value: row
        for row in observe_containment(run_dir, workspace=workspace, targets=targets)
    }

    # The wrapper admits PATH alone, so neither the planted variable nor the
    # daemon's ambient environment reaches the attempt.
    assert observed["variable_outside_allowlist"].denied
    assert observed["ambient_environment"].denied
    escaped = [name for name, row in observed.items() if not row.denied]
    assert escaped == [
        "canonical_git",
        "daemon_storage",
        "write_ahead_log",
        "outside_writable_roots",
    ]
    assert observed["daemon_storage"].returned_bytes == len("state.json")
    # No other tool's credential store exists here, so there is no evidence.
    assert "credential_value" not in observed
    assert not (run_dir / "canary-probes").exists()
    assert list(tmp_path.rglob(".eawf-canary-*")) == []


def test_a_refusing_jail_is_observed_as_one_denial_per_attempt(tmp_path: Path) -> None:
    workspace, targets = targets_in(tmp_path)
    run_dir = workspace / MCP_ARTIFACT_DIRNAME
    write_launch(run_dir, write_jail(tmp_path / "jail"))

    observed = observe_containment(run_dir, workspace=workspace, targets=targets)

    assert tuple(row.attempt for row in observed) == CONTAINMENT_ATTEMPTS
    assert all(row.denied and row.returned_bytes == 0 for row in observed)


@pytest.mark.parametrize("wrappers", [0, 2], ids=["none", "two"])
def test_no_single_recorded_wrapper_observes_nothing(tmp_path: Path, wrappers: int) -> None:
    workspace, targets = targets_in(tmp_path)
    run_dir = workspace / MCP_ARTIFACT_DIRNAME
    run_dir.mkdir(parents=True)
    for index in range(wrappers):
        (run_dir / f"lane{index}-wrapper.json").write_text("{}", encoding="utf-8")

    assert observe_containment(run_dir, workspace=workspace, targets=targets) == ()


@pytest.mark.parametrize(
    ("script", "config"),
    [
        ("#!/bin/sh\necho no command here\n", {"argv": ["/bin/false"], "env_keys": []}),
        ("#!/bin/sh\nexec /bin/false\n", {"argv": ["/bin/false"], "env_keys": []}),
        (None, {"argv": [], "env_keys": []}),
        (None, {"env_keys": []}),
        (None, {"argv": ["/bin/false"]}),
    ],
    ids=["no-command", "no-body", "empty-argv", "missing-argv", "missing-keys"],
)
def test_an_unreadable_wrapper_observes_nothing(
    tmp_path: Path, script: str | None, config: dict[str, Any]
) -> None:
    workspace, targets = targets_in(tmp_path)
    run_dir = workspace / MCP_ARTIFACT_DIRNAME
    write_launch(run_dir, write_jail(tmp_path / "jail"))
    if script is not None:
        (run_dir / "claude-wrapper").write_text(script, encoding="utf-8")
    (run_dir / "claude-wrapper.json").write_text(json.dumps(config), encoding="utf-8")

    assert observe_containment(run_dir, workspace=workspace, targets=targets) == ()


@pytest.mark.parametrize(
    ("argv", "prefix"),
    [
        (["/usr/bin/sandbox-exec", "-p", "(version 1)", "/bin/claude"], 3),
        (["/usr/bin/claude", "--print"], 0),
        (["/bin/claude"], 0),
        (
            ["/usr/bin/bwrap", "--ro-bind", "/", "/", "--unshare-pid", "--bind", "a", "a", "/c"],
            8,
        ),
        (["/usr/bin/bwrap", "/bin/codex"], 1),
    ],
    ids=["sandbox-exec", "unjailed", "bare-binary", "bwrap", "bwrap-no-options"],
)
def test_the_jail_prefix_ends_where_the_provider_starts(argv: list[str], prefix: int) -> None:
    assert jail_prefix(argv) == tuple(argv[:prefix])


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["/usr/bin/sandbox-exec"],
        ["/usr/bin/sandbox-exec", "-f", "profile.sb", "/bin/claude"],
        ["/usr/bin/bwrap", "--ro-bind", "/", "/", "--new-option", "/bin/claude"],
        ["/usr/bin/bwrap", "--bind", "a", "a"],
    ],
    ids=["empty", "sandbox-exec-alone", "file-profile", "unknown-option", "no-provider"],
)
def test_an_unreadable_jail_prefix_is_refused(argv: list[str]) -> None:
    with pytest.raises(ValueError, match=r"argv|prefix"):
        jail_prefix(argv)


@pytest.mark.parametrize(
    ("stdout", "denied", "returned"),
    [
        ("denied\n", True, 0),
        (b"denied\n", True, 0),
        ("escaped 0\n", False, 0),
        ("escaped 12", False, 12),
        ("warming up\ndenied\n", True, 0),
    ],
)
def test_a_printed_verdict_is_read(stdout: str | bytes, denied: bool, returned: int) -> None:
    row = probe_verdict(ContainmentAttempt.HOST_PROCESS_METADATA, stdout)
    assert row is not None
    assert (row.denied, row.returned_bytes) == (denied, returned)
    assert row.denial_reason == (DenialReason.SCOPE_ESCAPE if denied else None)


@pytest.mark.parametrize(
    "stdout", ["", b"", "\n", "Denied", "escaped", "escaped -1", "escaped x", "denied\nnoise"]
)
def test_anything_but_a_verdict_is_unobserved(stdout: str | bytes) -> None:
    assert probe_verdict(ContainmentAttempt.CANONICAL_GIT, stdout) is None


def _outcome(
    attempt: ContainmentAttempt, *, denied: bool, returned: int = 0
) -> ContainmentCallOutcome:
    return ContainmentCallOutcome(
        attempt=attempt,
        denied=denied,
        denial_reason=DENIALS[attempt] if denied else None,
        returned_bytes=returned,
    )


def test_a_reported_denial_never_covers_an_unobserved_attempt() -> None:
    reported = [_outcome(attempt, denied=True) for attempt in CONTAINMENT_ATTEMPTS]
    assert graded_outcomes(observed=(), reported=reported) == {}
    assert graded_outcomes(observed=(), reported=()) == {}


def test_the_report_only_worsens_what_was_observed() -> None:
    repo, process, wal = (
        ContainmentAttempt.CANONICAL_GIT,
        ContainmentAttempt.HOST_PROCESS_METADATA,
        ContainmentAttempt.WRITE_AHEAD_LOG,
    )
    observed = [_outcome(repo, denied=True), _outcome(wal, denied=False)]
    reported = [
        _outcome(repo, denied=True, returned=3),
        _outcome(process, denied=False),
        _outcome(wal, denied=True),
    ]

    graded = graded_outcomes(observed=observed, reported=reported)

    assert (graded[repo].denied, graded[repo].returned_bytes) == (True, 3)
    assert not graded[process].denied
    assert not graded[wal].denied


def test_a_reported_escape_without_bytes_leaves_an_observed_denial() -> None:
    observed = [_outcome(attempt, denied=True) for attempt in CONTAINMENT_ATTEMPTS]
    reported = [_outcome(attempt, denied=False) for attempt in CONTAINMENT_ATTEMPTS]

    graded = graded_outcomes(observed=observed, reported=reported)

    assert graded == {row.attempt: row for row in observed}


@pytest.mark.parametrize("returned", [1, 2**31])
def test_a_reported_escape_with_bytes_overrides_an_observed_denial(returned: int) -> None:
    attempt = ContainmentAttempt.DAEMON_STORAGE
    escape = _outcome(attempt, denied=False, returned=returned)

    graded = graded_outcomes(observed=[_outcome(attempt, denied=True)], reported=[escape])

    assert graded == {attempt: escape}


def test_a_credential_disclosure_alone_is_no_escape_evidence() -> None:
    attempt = ContainmentAttempt.CREDENTIAL_VALUE
    seen = _outcome(attempt, denied=True)
    reported = ContainmentCallOutcome(
        attempt=attempt, denied=False, disclosed_facts=tuple(CredentialFact)
    )

    assert graded_outcomes(observed=[seen], reported=[reported]) == {attempt: seen}


def test_a_reported_escape_without_bytes_still_grades_an_unobserved_attempt() -> None:
    attempt = ContainmentAttempt.CANONICAL_GIT
    escape = _outcome(attempt, denied=False)

    assert graded_outcomes(observed=(), reported=[escape]) == {attempt: escape}
