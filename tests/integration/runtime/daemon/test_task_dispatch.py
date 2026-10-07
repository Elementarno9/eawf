"""``runtime.task.dispatch`` turns one Task into one announced Run; ``eawf run dispatch`` sends it.

The suite drives the registered verb against a canary with a real git repository
under it. The Task is a seeded row, the provider facts are the installed launchers
read under a fixture machine certification appended to the canary, and the worker
is a fake launcher whose turn parks on an event the test releases, so no agent
binary runs and the operator's home is never read. Each refusal is asserted to
leave the run collection empty, which is the "before any Run is created" claim
read off the tree rather than inferred from the answer.

The CLI half replaces the daemon transport with a recorder: the command is a
dispatch layer, so what it sends and what it prints are its whole behaviour.
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final
from unittest import mock

import pytest
import yaml
from eawf.runtime.daemon.run_supervisor import supervisor_of
from typer.testing import CliRunner

import eawf.runtime.daemon.methods.domain
import eawf.runtime.daemon.methods.domain_create
import eawf.runtime.daemon.methods.task_dispatch  # noqa: F401  (registers the verbs the suite reaches)
from eawf.kernel.config import layered
from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.runtime.certification import CapabilityCertification, ConformanceStageRecord
from eawf.kernel.state.epoch2.task import Task
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.kinds.runtime_certification import MachineCertification
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods, native_dispatch
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.delivery import stored_tasks
from eawf.runtime.daemon.methods.task_dispatch import TASK_DISPATCH_METHOD, TaskDispatchRefusal
from eawf.runtime.daemon.native_dispatch import compile_launchers, run_ledger
from eawf.runtime.runtimes.adapter import (
    NativeLaunchOutcome,
    NativeLaunchRequest,
    TurnOutcome,
    compose_worker_hello,
)
from eawf.runtime.runtimes.claude.plugin_install import install_plugin
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import domain as domain_commands
from eawf.surfaces.cli.commands import run_dispatch
from eawf.workflow.evidence.machine_certification import append_machine_certification
from eawf.workflow.runtime.compile import CompileRejection
from tests import _provider_helpers as fx
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import seed, seed_row
from tests.integration.runtime.daemon.test_native_dispatch import ACTOR, make_canary, method_ctx

pytestmark = pytest.mark.integration

TASK_KEY: Final = "EAWF-0042"
TASK_URN: Final = parse_qualified_urn(fx.TASK_URN)
FIRST_RUN: Final = "RUN-00000001"
NOW: Final = datetime.now(UTC)
CLAUDE_VERSION: Final = "2.1.288"

#: The token ceiling every dispatch names unless a case says otherwise; the
#: shipped governor bounds in-flight tokens, so an uncapped Run is never admitted.
BUDGET: Final = 100_000
CODEX_VERSION: Final = "0.159.2"
GOLDEN_SKILL: Final = (
    Path(__file__).resolve().parents[3]
    / "golden"
    / "plugin_install"
    / "claude"
    / "skills"
    / "dispatch"
    / "SKILL.md"
)

runner = CliRunner()


@pytest.fixture(autouse=True)
def _private_homes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_host_homes: Path
) -> Iterator[None]:
    """Read the global config layer and every host home from empty directories."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    yield


# ---- the tree -----------------------------------------------------------------


def certification(runtime_id: str, version: str) -> MachineCertification:
    """Return a current machine certification of *runtime_id*, as the certifier writes it."""
    verified_at = NOW - timedelta(days=1)
    expires_at = verified_at + timedelta(days=90)
    evidence = f"artifact://runtime-certification/{runtime_id}/{version}"
    capabilities = tuple(
        CapabilityCertification(
            capability_id=capability_id,
            level=True,
            status="verified",
            basis="native",
            evidence_ref=evidence,
            verified_at=verified_at,
            expires_at=expires_at,
        )
        for capability_id in ("tool_use", "streaming")
    )
    return MachineCertification.model_validate(
        {
            "certification_urn": f"certification://{runtime_id}/{version}/{verified_at:%Y-%m-%d}",
            "runtime_id": runtime_id,
            "harness_version": version,
            "tuple_digest": fx.digest("d"),
            "outcome": "certified",
            "probe": ConformanceStageRecord(
                stage="probe",
                outcome="passed",
                evidence_ref=evidence,
                started_at=verified_at,
                completed_at=verified_at,
            ),
            "verified_at": verified_at,
            "expires_at": expires_at,
            "capabilities": capabilities,
            "reason": f"{runtime_id} {version} probed",
        }
    )


def task_row(status: str = "PLANNED", **overrides: Any) -> dict[str, Any]:
    """Return the seeded Task in *status*, claiming the canary's ``src`` tree."""
    row = seed_row("task", status)
    row["write_claims"] = ["src"]
    row.update(overrides)
    return row


def make_tree(
    tmp_path: Path,
    *,
    status: str = "PLANNED",
    certified: tuple[str, ...] = ("claude-code",),
    **task: Any,
) -> CanaryProvision:
    """Provision a canary holding one Task, certified for each runtime in *certified*."""
    canary = make_canary(tmp_path / "repo", rows={})
    seed(canary, {"task": {TASK_KEY: task_row(status, **task)}})
    versions = {"claude-code": CLAUDE_VERSION, "codex": CODEX_VERSION}
    for runtime_id in certified:
        append_machine_certification(
            canary.root / ".ea" / "state.json", certification(runtime_id, versions[runtime_id])
        )
    return canary


def write_preference(canary: CanaryProvision, preference: list[str]) -> None:
    """Set the repository layer's ``runtime.preference`` ladder."""
    path = canary.root / ".ea" / "config.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    document = document or {}
    document.setdefault("runtime", {})["preference"] = preference
    path.write_text(yaml.safe_dump(document), encoding="utf-8")


def runs(ctx: MethodContext, canary: CanaryProvision) -> list[str]:
    """Return the key of every Run the tree holds, in either tier."""
    context = ctx.native_root_context(canary.root / ".ea")
    with context.session([TASK_URN]) as session:
        keys = set(document_rows(session.read_document(), Epoch2Collection.RUN))
        keys |= {
            line.record_key
            for line in read_ledger_records(run_ledger(session))
            if "payload_kind" not in line.payload
        }
    return sorted(keys)


def task_now(ctx: MethodContext, canary: CanaryProvision) -> Task:
    """Return the seeded Task as the tree holds it now."""
    context = ctx.native_root_context(canary.root / ".ea")
    with context.session([TASK_URN]) as session:
        return next(task for task in stored_tasks(session) if task.urn == TASK_URN)


# ---- the fake worker ----------------------------------------------------------


class ParkedLauncher:
    """A launcher whose worker announces itself, then parks its turn until released.

    Attributes:
        provider_kind: The provider the fake stands in for.
        release: Set by the test to let the turn end.
        capsule: The capsule the last launch carried.
        launches: How many workers were started.
    """

    def __init__(self, provider_kind: str) -> None:
        """Stand in for *provider_kind*'s launcher."""
        self.provider_kind = provider_kind
        self.release = asyncio.Event()
        self.capsule: AuthorityCapsule | None = None
        self.launches = 0

    async def launch(self, request: NativeLaunchRequest) -> NativeLaunchOutcome:
        """Announce the worker and hand the parked turn back."""
        self.launches += 1
        self.capsule = request.capsule
        session_ref = f"{self.provider_kind}-session-{self.launches}"
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
            turn=asyncio.get_running_loop().create_task(self._turn()),
        )

    async def _turn(self) -> TurnOutcome:
        await self.release.wait()
        return TurnOutcome(exit_kind="completed")


def dispatch(
    ctx: MethodContext,
    canary: CanaryProvision,
    launcher: ParkedLauncher,
    *calls: dict[str, Any],
) -> list[dict[str, Any] | DaemonValidationError]:
    """Send each of *calls* in turn, then release the parked turns and await their end.

    Returns:
        Each call's answer, or the refusal it raised, in call order.
    """

    async def scenario() -> list[dict[str, Any] | DaemonValidationError]:
        answers: list[dict[str, Any] | DaemonValidationError] = []
        for call in calls:
            params = {"repo_root": str(canary.root), "actor": ACTOR, "budget": BUDGET, **call}
            try:
                answers.append(await methods.dispatch(TASK_DISPATCH_METHOD, ctx, params))
            except DaemonValidationError as refusal:
                answers.append(refusal)
        launcher.release.set()
        supervisor = supervisor_of(ctx.native_root_context(canary.root / ".ea"))
        for answer in answers:
            if isinstance(answer, dict):
                turn = supervisor.task_of(answer["run_ref"])
                if turn is not None:
                    await turn
        return answers

    with mock.patch.object(
        native_dispatch, "NATIVE_LAUNCHERS", dict(compile_launchers((launcher,)))
    ):
        return asyncio.run(scenario())


def refusal_of(answer: dict[str, Any] | DaemonValidationError) -> str:
    """Return the refusal message of *answer*, failing when it was accepted."""
    assert isinstance(answer, DaemonValidationError), answer
    return str(answer)


# ---- CR-1 and CR-2: claim, create, start and announce -----------------------------


def test_dispatch_claims_starts_and_announces_one_run(tmp_path: Path) -> None:
    canary = make_tree(tmp_path)
    ctx = method_ctx(tmp_path / "runtime")
    launcher = ParkedLauncher("claude")

    (answer,) = dispatch(ctx, canary, launcher, {"task": TASK_KEY, "provider": "claude-code"})

    assert isinstance(answer, dict), answer
    assert answer["task_key"] == TASK_KEY
    assert answer["run_key"] == FIRST_RUN
    assert answer["provider"] == "claude-code"
    assert answer["stage"] == "announced"
    assert answer["attempt_ref"] == answer["dispatch"]["attempt"]["attempt_ref"]
    assert runs(ctx, canary) == [FIRST_RUN]
    task = task_now(ctx, canary)
    assert task.status.value == "RUNNING"
    assert str(task.active_run_ref) == answer["run_ref"]
    assert task.claimed_by == ACTOR
    assert launcher.launches == 1


def test_dispatch_seals_the_named_budget_and_the_task_criteria(tmp_path: Path) -> None:
    canary = make_tree(tmp_path)
    ctx = method_ctx(tmp_path / "runtime")
    launcher = ParkedLauncher("claude")

    dispatch(ctx, canary, launcher, {"task": str(TASK_URN), "budget": 1})

    assert launcher.capsule is not None
    assert launcher.capsule.budget.tokens == 1
    assert launcher.capsule.report_schema_ref == "schema://executor-report/v1"
    assert launcher.capsule.scope_ref == TASK_URN


def test_dispatch_defaults_to_the_first_preferred_runtime(tmp_path: Path) -> None:
    canary = make_tree(tmp_path, certified=("claude-code", "codex"))
    write_preference(canary, ["codex", "claude-code"])
    ctx = method_ctx(tmp_path / "runtime")

    (answer,) = dispatch(ctx, canary, ParkedLauncher("codex"), {"task": TASK_KEY})

    assert isinstance(answer, dict), answer
    assert answer["provider"] == "codex"
    assert answer["dispatch"]["attempt"]["provider_kind"] == "codex"


def test_dispatch_starts_a_claimed_task_without_claiming_it_again(tmp_path: Path) -> None:
    canary = make_tree(tmp_path, status="CLAIMED", claimed_by="OP-0002")
    ctx = method_ctx(tmp_path / "runtime")

    (answer,) = dispatch(ctx, canary, ParkedLauncher("claude"), {"task": TASK_KEY})

    assert isinstance(answer, dict), answer
    task = task_now(ctx, canary)
    assert task.status.value == "RUNNING"
    assert task.claimed_by == "OP-0002"
    assert runs(ctx, canary) == [FIRST_RUN]


def test_dispatch_refuses_a_second_run_while_the_first_is_open(tmp_path: Path) -> None:
    canary = make_tree(tmp_path)
    ctx = method_ctx(tmp_path / "runtime")
    launcher = ParkedLauncher("claude")

    first, second = dispatch(ctx, canary, launcher, {"task": TASK_KEY}, {"task": TASK_KEY})

    assert isinstance(first, dict), first
    assert TaskDispatchRefusal.TASK_RUN_OPEN.value in refusal_of(second)
    assert runs(ctx, canary) == [FIRST_RUN]
    assert launcher.launches == 1


# ---- CR-2: refusals leave no Run ----------------------------------------------------


@pytest.mark.parametrize("status", ["COMPLETED", "CANCELLED", "FAILED", "DROPPED"])
def test_dispatch_refuses_a_settled_task_before_any_run(tmp_path: Path, status: str) -> None:
    canary = make_tree(tmp_path, status=status)
    ctx = method_ctx(tmp_path / "runtime")
    launcher = ParkedLauncher("claude")

    (answer,) = dispatch(ctx, canary, launcher, {"task": TASK_KEY})

    assert TaskDispatchRefusal.TASK_SETTLED.value in refusal_of(answer)
    assert runs(ctx, canary) == []
    assert launcher.launches == 0


@pytest.mark.parametrize("status", ["DRAFT", "DEFERRED", "READY_TO_INTEGRATE"])
def test_dispatch_refuses_a_task_not_yet_dispatchable(tmp_path: Path, status: str) -> None:
    canary = make_tree(tmp_path, status=status)
    ctx = method_ctx(tmp_path / "runtime")

    (answer,) = dispatch(ctx, canary, ParkedLauncher("claude"), {"task": TASK_KEY})

    assert TaskDispatchRefusal.TASK_NOT_READY.value in refusal_of(answer)
    assert runs(ctx, canary) == []


def test_dispatch_refuses_an_uncertified_runtime_before_claiming(tmp_path: Path) -> None:
    canary = make_tree(tmp_path, certified=())
    ctx = method_ctx(tmp_path / "runtime")

    (answer,) = dispatch(ctx, canary, ParkedLauncher("claude"), {"task": TASK_KEY})

    assert CompileRejection.NO_ELIGIBLE_PROFILE.value in refusal_of(answer)
    assert runs(ctx, canary) == []
    assert task_now(ctx, canary).status.value == "PLANNED"


def test_dispatch_refuses_an_uncapped_run_the_governor_never_admits(tmp_path: Path) -> None:
    canary = make_tree(tmp_path)
    ctx = method_ctx(tmp_path / "runtime")

    (answer,) = dispatch(ctx, canary, ParkedLauncher("claude"), {"task": TASK_KEY, "budget": None})

    assert TaskDispatchRefusal.BUDGET_REQUIRED.value in refusal_of(answer)
    assert runs(ctx, canary) == []
    assert task_now(ctx, canary).status.value == "PLANNED"


def test_dispatch_refuses_a_preference_no_launcher_drives(tmp_path: Path) -> None:
    canary = make_tree(tmp_path)
    write_preference(canary, ["opencode"])
    ctx = method_ctx(tmp_path / "runtime")

    (answer,) = dispatch(ctx, canary, ParkedLauncher("claude"), {"task": TASK_KEY})

    assert TaskDispatchRefusal.PROVIDER_UNSUPPORTED.value in refusal_of(answer)
    assert runs(ctx, canary) == []


def test_dispatch_refuses_a_task_with_no_write_claims(tmp_path: Path) -> None:
    canary = make_tree(tmp_path, write_claims=[])
    ctx = method_ctx(tmp_path / "runtime")

    (answer,) = dispatch(ctx, canary, ParkedLauncher("claude"), {"task": TASK_KEY})

    refusal = refusal_of(answer)
    assert "dispatch_write_claims_required" in refusal
    assert "declares no write_claims" in refusal
    assert runs(ctx, canary) == []


def test_dispatch_refuses_an_unknown_task(tmp_path: Path) -> None:
    canary = make_tree(tmp_path)
    ctx = method_ctx(tmp_path / "runtime")

    (answer,) = dispatch(ctx, canary, ParkedLauncher("claude"), {"task": "EAWF-9999"})

    assert "identity_not_found" in refusal_of(answer)
    assert runs(ctx, canary) == []


@pytest.mark.parametrize(
    "call",
    [
        pytest.param({"task": TASK_KEY, "provider": "opencode"}, id="unknown-provider"),
        pytest.param({"task": TASK_KEY, "budget": 0}, id="zero-budget"),
        pytest.param({"task": ""}, id="empty-task"),
        pytest.param({"task": TASK_KEY, "base": "main"}, id="undeclared-field"),
    ],
)
def test_dispatch_refuses_a_malformed_request(tmp_path: Path, call: dict[str, Any]) -> None:
    canary = make_tree(tmp_path)
    ctx = method_ctx(tmp_path / "runtime")

    (answer,) = dispatch(ctx, canary, ParkedLauncher("claude"), call)

    assert "schema_validation_failed" in refusal_of(answer)
    assert runs(ctx, canary) == []


# ---- CR-1: the CLI is a dispatch over the verb --------------------------------------


class RecordingTransport:
    """Stands in for the daemon transport and answers as a dispatch would."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, method: str, params: dict[str, Any], **_: Any) -> dict[str, Any]:
        self.calls.append((method, params))
        return {
            "task_key": params["task"],
            "run_key": FIRST_RUN,
            "attempt_ref": "ATT-0001",
            "stage": "announced",
        }


@pytest.mark.parametrize(
    ("extra", "sent"),
    [
        pytest.param([], {}, id="defaults"),
        pytest.param(
            ["--provider", "codex", "--budget", "5"], {"provider": "codex", "budget": 5}, id="named"
        ),
    ],
)
def test_cli_sends_only_the_task_dispatch_verb(
    monkeypatch: pytest.MonkeyPatch, extra: list[str], sent: dict[str, Any]
) -> None:
    transport = RecordingTransport()
    monkeypatch.setattr(domain_commands, "_native_answer", transport)

    result = runner.invoke(app, ["run", "dispatch", TASK_KEY, "--actor", ACTOR, *extra])

    assert result.exit_code == exit_codes.OK, result.output
    assert transport.calls == [(TASK_DISPATCH_METHOD, {"task": TASK_KEY, "actor": ACTOR, **sent})]
    assert f"dispatched {TASK_KEY} as {FIRST_RUN} (attempt ATT-0001, stage announced)" in (
        result.output
    )


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["--actor", ACTOR, "--provider", "opencode"], id="unknown-provider"),
        pytest.param(["--actor", ACTOR, "--budget", "0"], id="zero-budget"),
        pytest.param([], id="no-actor"),
    ],
)
def test_cli_refuses_a_malformed_invocation_without_sending(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    transport = RecordingTransport()
    monkeypatch.setattr(domain_commands, "_native_answer", transport)

    result = runner.invoke(app, ["run", "dispatch", TASK_KEY, *argv])

    assert result.exit_code != exit_codes.OK
    assert transport.calls == []


def test_cli_handler_imports_nothing_from_the_task_compiler() -> None:
    tree = ast.parse(Path(run_dispatch.__file__).read_text(encoding="utf-8"))
    imported = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not {name for name in imported if name.startswith("eawf.workflow.runtime")}
    assert not {name for name in imported if name.startswith("eawf.kernel.config")}


# ---- CR-4: the rendered skill grants the verb ---------------------------------------


def test_plugin_install_dispatch_skill_allows_the_task_dispatch_verb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(layered, "global_config_path", lambda: tmp_path / "absent-global.yaml")
    (tmp_path / "render").mkdir()
    install_plugin(tmp_path / "render", persist_manifest=False)
    rendered = (tmp_path / "render" / ".claude" / "skills" / "dispatch" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    (allowed,) = [line for line in rendered.splitlines() if line.startswith("- Allowed RPCs:")]

    assert "`runtime.task.dispatch`" in allowed
    assert rendered == GOLDEN_SKILL.read_text(encoding="utf-8")
