"""A dispatched Run records, at its start, the runtime its launcher observed.

Each case drives ``runtime.run.dispatch`` over the registered verb through the real
native launcher of one provider, started against the fake binary that stands in for
it: the Claude Agent SDK talks to ``fake_claude_cli.py`` and the Codex SDK to
``fake_codex_server.py``. The fake is installed into the leased workspace when the
launch asks for it, because that is the directory the child runs in and reads its
script from. The worker's turn is parked behind an event, so the Run is read while it
is still ``RUNNING``: what it records there is what the start edge wrote, not something
the turn's end added later.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Final
from unittest import mock

import pytest

from eawf.kernel.projection.connection import read_method
from eawf.kernel.projection.row_facts import RUNTIME_FACTS
from eawf.kernel.state.epoch2.run import RunRuntimeTuple
from eawf.runtime.daemon import methods, native_dispatch
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.native_dispatch import RUN_DISPATCH_METHOD, compile_launchers
from eawf.runtime.daemon.run_supervisor import supervisor_of
from eawf.runtime.runtimes.adapter import (
    NativeLaunchOutcome,
    NativeLaunchRequest,
    NativeRunLauncher,
    TurnOutcome,
)
from eawf.runtime.runtimes.claude import adapter as claude_adapter
from eawf.runtime.runtimes.claude.adapter import ClaudeAdapter, ClaudeNativeLauncher
from eawf.runtime.runtimes.codex import adapter as codex_adapter
from eawf.runtime.runtimes.codex.adapter import CodexAdapter, CodexNativeLauncher
from tests import _provider_helpers as fx
from tests.fixtures.runtime.fake_claude_cli import VERSION as CLAUDE_VERSION
from tests.fixtures.runtime.fake_claude_cli import install_fake_claude
from tests.fixtures.runtime.fake_codex_server import VERSION as CODEX_VERSION
from tests.fixtures.runtime.fake_codex_server import install_fake_codex, turn_completed
from tests.integration.runtime.daemon.test_native_dispatch import (
    dispatch_params,
    make_canary,
    method_ctx,
    run_urn,
)
from tests.integration.runtime.daemon.test_run_supervisor import stored_run

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform == "win32", reason="the fakes are POSIX shebang scripts"),
]

#: The Run detail read the Activity view and ``eawf run`` reads open a Run on.
RUN_DETAIL_READ: Final = read_method("run.detail")

#: The model every fixture profile compiles to.
COMPILED_MODEL: Final = fx.CERTIFIED_MODEL

#: A finished turn, as the fake Claude CLI prints it.
CLAUDE_DONE: Final = {"type": "result", "result": "done"}

Install = Callable[[Path, dict[str, Any] | None], Path]


def _bypass_jail(
    argv: list[str],
    *,
    runtime: str,
    cwd: str | None,
    session: str = "",
    sink: object = None,
    private_home: object = None,
    storage_dirs: object = (),
) -> list[str]:
    """Leave the child unjailed so the fake runs on any host."""
    return argv


@pytest.fixture(autouse=True)
def _hermetic_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_host_homes: Path
) -> Iterator[None]:
    """Run each fake unjailed, with its home and every host home away from the operator's."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(claude_adapter, "_maybe_jail_argv", _bypass_jail)
    monkeypatch.setattr(codex_adapter, "_maybe_jail_argv", _bypass_jail)
    yield


# ---- the launcher under test ----------------------------------------------------


class FakeBinaryLauncher:
    """The real native launcher of one provider, started against that provider's fake.

    The fake is written into the leased workspace at launch, and the turn the real
    launcher hands back is parked until the test releases it.

    Attributes:
        provider_kind: The provider the wrapped launcher starts.
        release: Set by the test to let the parked turn resolve.
        outcome: What the real launcher answered, once it did.
    """

    def __init__(
        self,
        real: NativeRunLauncher,
        *,
        adapter: ClaudeAdapter | CodexAdapter,
        binary: Install,
        script: dict[str, Any] | None,
    ) -> None:
        """Wrap *real*, whose *adapter* runs the fake *binary* installs on *script*."""
        self.provider_kind = real.provider_kind
        self.release = asyncio.Event()
        self.outcome: NativeLaunchOutcome | None = None
        self._real = real
        self._adapter = adapter
        self._binary = binary
        self._script = script

    async def launch(self, request: NativeLaunchRequest) -> NativeLaunchOutcome:
        """Install the fake where the child runs, launch it, and park its turn."""
        self._adapter.cli_binary = str(self._binary(request.workspace, self._script))
        outcome = await self._real.launch(request)
        assert outcome.turn is not None, "a native launcher hands its turn back"
        self.outcome = outcome
        return outcome.model_copy(update={"turn": asyncio.create_task(self._parked(outcome))})

    async def _parked(self, outcome: NativeLaunchOutcome) -> TurnOutcome:
        """Resolve to the real turn's outcome only once the test lets it."""
        await self.release.wait()
        assert outcome.turn is not None
        return await outcome.turn


def claude_launcher(script: dict[str, Any]) -> FakeBinaryLauncher:
    """Return the Claude launcher started against the fake CLI playing *script*."""
    adapter = ClaudeAdapter()
    return FakeBinaryLauncher(
        ClaudeNativeLauncher(adapter=adapter),
        adapter=adapter,
        binary=install_fake_claude,
        script=script,
    )


def codex_launcher(script: dict[str, Any] | None = None) -> FakeBinaryLauncher:
    """Return the Codex launcher started against the fake app server playing *script*."""
    adapter = CodexAdapter()
    return FakeBinaryLauncher(
        CodexNativeLauncher(adapter=adapter),
        adapter=adapter,
        binary=install_fake_codex,
        script=script,
    )


def claude_params(canary: Any) -> dict[str, Any]:
    """Return a dispatch whose winning profile is the Claude lane."""
    profile = fx.profile_document(
        profile_id="fixture_claude",
        driver_ref=fx.OTHER_DRIVER_REF,
        auth_profile_ref=fx.OTHER_AUTH_REF,
        provider_options={"provider_kind": "claude"},
    )
    return dispatch_params(
        canary,
        provider_documents=fx.documents(
            profiles=[profile],
            routes=[fx.task_route_document(allowed_profiles=["fixture_claude"])],
            global_routes=[],
        ),
        bindings=[fx.binding(driver_ref=fx.OTHER_DRIVER_REF)],
    )


# ---- driving the daemon -------------------------------------------------------


def dispatch_parked(
    tmp_path: Path, launcher: FakeBinaryLauncher, *, claude: bool
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Dispatch through *launcher*, read the Run while its turn is parked, then end it.

    Returns:
        The stored Run read at ``RUNNING``, the Run detail row read then, and the
        stored Run once the supervisor ended it.
    """
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    ctx = method_ctx(runtime)
    params = claude_params(canary) if claude else dispatch_params(canary)
    seen: dict[str, Any] = {}

    async def scenario() -> None:
        answer = await methods.dispatch(RUN_DISPATCH_METHOD, ctx, params)
        assert answer["stage"] == "announced", answer
        context = ctx.native_root_context(canary.root / ".ea")
        task = supervisor_of(context).task_of(str(run_urn()))
        assert task is not None, "the dispatch handed its turn to no supervisor"
        seen["running"] = await asyncio.to_thread(stored_run, canary, runtime)
        seen["row"] = await detail_row(ctx, canary.root)
        launcher.release.set()
        await task

    with mock.patch.object(
        native_dispatch, "NATIVE_LAUNCHERS", dict(compile_launchers((launcher,)))
    ):
        asyncio.run(scenario())
    return seen["running"], seen["row"], stored_run(canary, runtime)


async def detail_row(ctx: MethodContext, root: Path) -> dict[str, Any]:
    """Return the Run as ``projection.run.detail.read`` answers for it."""
    answer = await methods.dispatch(
        RUN_DETAIL_READ, ctx, {"repo_root": str(root), "key": str(run_urn())}
    )
    (row,) = [row for row in answer["rows"] if row["urn"] == str(run_urn())]
    return dict(row)


def tuple_of(run: dict[str, Any]) -> RunRuntimeTuple:
    """Return the runtime tuple a stored Run records, which must be one."""
    stated = run["runtime_tuple"]
    assert stated is not None, "the dispatched Run recorded no runtime"
    return RunRuntimeTuple.model_validate(stated)


def runtime_facts(row: dict[str, Any]) -> dict[str, str]:
    """Return the runtime facts a projection row states, by fact name."""
    facts: dict[str, str] = row["facts"]
    return {name: facts[name] for name in RUNTIME_FACTS.values() if name in facts}


# ---- CR-1: the start records what the launcher observed, per provider ----------


def test_a_claude_dispatch_records_the_version_and_model_of_the_init_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Claude init message names the version and model; the child's environment the
    provider it is routed to."""
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    launcher = claude_launcher({"init_fields": {"model": COMPILED_MODEL}, "lines": [CLAUDE_DONE]})

    running, _row, ended = dispatch_parked(tmp_path, launcher, claude=True)

    expected = RunRuntimeTuple(
        harness="claude-code",
        harness_version=CLAUDE_VERSION.split()[0],
        provider="anthropic",
        model=COMPILED_MODEL,
    )
    assert running["status"] == "RUNNING"
    assert tuple_of(running) == expected
    assert launcher.outcome is not None
    assert launcher.outcome.runtime_tuple == expected
    assert tuple_of(ended) == expected


def test_a_codex_dispatch_records_the_initialize_version_and_the_thread_model(
    tmp_path: Path,
) -> None:
    """The Codex initialize answer names the version; ``thread/start`` the model and provider."""
    launcher = codex_launcher()

    running, _row, ended = dispatch_parked(tmp_path, launcher, claude=False)

    expected = RunRuntimeTuple(
        harness="codex",
        harness_version=CODEX_VERSION,
        provider="openai",
        model=COMPILED_MODEL,
    )
    assert running["status"] == "RUNNING"
    assert tuple_of(running) == expected
    assert tuple_of(ended) == expected


def test_a_claude_init_without_version_or_model_leaves_both_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A member the init message did not state stays absent, never the compiled one."""
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    launcher = claude_launcher(
        {"init_fields": {"claude_code_version": None, "model": None}, "lines": [CLAUDE_DONE]}
    )

    running, row, _ended = dispatch_parked(tmp_path, launcher, claude=True)

    assert tuple_of(running) == RunRuntimeTuple(harness="claude-code", provider="anthropic")
    assert runtime_facts(row) == {
        "runtime_harness": "claude-code",
        RUNTIME_FACTS["provider"]: "anthropic",
    }


def test_a_codex_user_agent_without_a_version_leaves_the_version_absent(
    tmp_path: Path,
) -> None:
    """An initialize answer naming no version records none; the thread's members stay."""
    launcher = codex_launcher({"user_agent": "codex_cli_rs (unknown)", "lines": [turn_completed()]})

    running, _row, _ended = dispatch_parked(tmp_path, launcher, claude=False)

    assert tuple_of(running) == RunRuntimeTuple(
        harness="codex", provider="openai", model=COMPILED_MODEL
    )


def test_a_misspelled_init_model_is_dropped_rather_than_recorded(tmp_path: Path) -> None:
    """A model outside the tuple's grammar is absent on the Run, not stored as said."""
    launcher = claude_launcher({"init_fields": {"model": "not a model"}, "lines": [CLAUDE_DONE]})

    running, _row, _ended = dispatch_parked(tmp_path, launcher, claude=True)

    assert tuple_of(running).model is None
    assert tuple_of(running).harness_version == CLAUDE_VERSION.split()[0]


# ---- CR-2: the projection row the Activity view reads states the tuple ---------


def test_the_run_detail_row_states_the_recorded_tuple_as_facts(tmp_path: Path) -> None:
    """Each recorded member is a fact of the Run's projection row, keyed as the view reads."""
    launcher = codex_launcher()

    running, row, _ended = dispatch_parked(tmp_path, launcher, claude=False)

    recorded = tuple_of(running)
    assert runtime_facts(row) == {
        RUNTIME_FACTS["harness"]: recorded.harness,
        RUNTIME_FACTS["harness_version"]: CODEX_VERSION,
        RUNTIME_FACTS["provider"]: "openai",
        RUNTIME_FACTS["model"]: COMPILED_MODEL,
    }
