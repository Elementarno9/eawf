"""In-loop cross-vendor deny parity proof (P30-I12-W09).

The adapter-level parity proof (``test_cross_vendor_parity.py``, I10-W05)
spawns each vendor adapter DIRECTLY and asserts each lane honours a denied
tool by its own real mechanism. This module is the complementary *in-LOOP*
proof: it drives the daemon-owned fleet auto-drain loop
(:func:`eawf.runtime.daemon.methods.fleet.arm_drive`) with the REAL
:func:`~eawf.runtime.daemon.methods.fleet._default_spawner`, so the loop
spawns each runtime through the runtime-agnostic
:func:`eawf.runtime.daemon.methods.agent.dispatch` (``spawn=True``) path -- the
same per-lane dispatch the live drive uses -- and proves the deny survives
that whole path end-to-end, not just a direct adapter call.

Two success criteria are pinned:

* C1: a drive seeded with a claude, a codex, and an opencode wave spawns each
  through ``agent.dispatch(spawn=True)`` (the real spawner, NOT an injected
  fake) and records exactly one lane per run.
* C2: each lane's deny is enforced by the mechanism that runtime actually
  offers -- claude/codex assert the denied tool is absent from the
  inverted-allowlist argv (claude's ``--disallowedTools`` token / codex's
  ``-c tools.<name>=<bool>`` grant map), and opencode honours the deny via its
  FS-jail floor (the denied tool never reaches any positive argv grant, and the
  deny is recorded as an ``argv-deny`` enforcement event).

Only ``asyncio.create_subprocess_exec`` is faked (per adapter module) so no
real CLI / network / auth / cost runs; the jail seam is neutralised to a
passthrough so the inner argv assertions stay deterministic across hosts, and
``build_child_env`` is pinned to a fixed scrubbed env. Every other layer --
the claim, the runtime-agnostic dispatch, the real adapter argv build, the
metering, the report binding -- runs for real.

The claude + codex adapters are READ-ONLY here: this wave imports them to
assert the inverted-allowlist argv but mutates only the opencode lane + the
fleet loop's per-lane dispatch path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from eawf import __version__
from eawf.kernel.state.models import State
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.fleet import FleetRunState, FleetTerminalReason, arm_drive
from eawf.runtime.runtimes.claude import adapter as claude_adapter
from eawf.runtime.runtimes.codex import adapter as codex_adapter
from eawf.runtime.runtimes.opencode import adapter as opencode_adapter
from eawf.workflow.evidence._io import load_state

pytestmark = pytest.mark.integration

#: The three frontier waves, one per runtime lane. Each wave pins its
#: ``runtime_preference`` to a single vendor so the real spawner routes the
#: lane to that vendor's adapter.
_CLAUDE_WAVE = "P30-I12-W07"
_CODEX_WAVE = "P30-I12-W08"
_OPENCODE_WAVE = "P30-I12-W09"
_WAVE_IDS = [_CLAUDE_WAVE, _CODEX_WAVE, _OPENCODE_WAVE]

#: The runtime each wave pins, keyed by wave id. ``claude-code`` is the
#: plugin-manifest spelling the routing + selector key on.
_WAVE_RUNTIME = {
    _CLAUDE_WAVE: "claude-code",
    _CODEX_WAVE: "codex",
    _OPENCODE_WAVE: "opencode",
}

#: The denied tools the global sandbox policy withholds from every lane. Both
#: are members of :data:`eawf.runtime.sandbox.policy.TOOL_UNIVERSE` so the
#: inverted-allowlist assertions have a non-trivial complement to check.
_DENIED = ["Edit", "Bash"]

_FAKE_PID = 4321


# --------------------------------------------------------------------------- #
# Per-vendor canned answer streams. Each carries a schema-valid executor body
# whose ``wave_id`` matches the lane it serves (the verify gate equality
# check), so the real report-binding + verify path passes and the lane closes.
# --------------------------------------------------------------------------- #


def _executor_answer(wave_id: str) -> str:
    """Return a schema-valid ``ExecutorReportBody`` JSON string for *wave_id*."""
    return json.dumps(
        {
            "role": "executor",
            "verdict": "pass",
            "confidence": "high",
            "summary": "executor implemented the wave in-loop",
            "wave_id": wave_id,
            "files_changed": ["src/eawf/runtime/daemon/methods/fleet.py"],
            "tests_run": ["uv run pytest tests/integration -q"],
            "outcome": "spawned through the in-loop cross-vendor dispatch path",
        }
    )


def _claude_stdout(wave_id: str) -> bytes:
    envelope: dict[str, object] = {
        "type": "result",
        "is_error": False,
        "session_id": "claude-sess",
        "result": _executor_answer(wave_id),
        "total_cost_usd": 0.0123,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 42,
            "cache_creation_input_tokens": 80,
            "cache_read_input_tokens": 200,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 50,
                "ephemeral_1h_input_tokens": 30,
            },
        },
    }
    return json.dumps(envelope).encode("utf-8")


def _codex_stdout(wave_id: str) -> bytes:
    events: list[dict[str, object]] = [
        {"type": "thread.started", "thread_id": "thr-codex-1"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": _executor_answer(wave_id)},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 1000, "cached_input_tokens": 0, "output_tokens": 17},
        },
    ]
    return ("\n".join(json.dumps(row) for row in events) + "\n").encode("utf-8")


def _opencode_stdout(wave_id: str) -> bytes:
    events: list[dict[str, object]] = [
        {"type": "step_start", "sessionID": "opencode-sess", "part": {"type": "step-start"}},
        {
            "type": "text",
            "sessionID": "opencode-sess",
            "part": {"type": "text", "text": _executor_answer(wave_id)},
        },
        {
            "type": "step_finish",
            "sessionID": "opencode-sess",
            "part": {
                "type": "step-finish",
                "tokens": {"input": 100, "output": 42, "cache": {"write": 80, "read": 200}},
                "cost": 0.0021,
            },
        },
    ]
    return ("\n".join(json.dumps(row) for row in events) + "\n").encode("utf-8")


_VENDOR_STDOUT = {
    "claude-code": (claude_adapter, _claude_stdout),
    "codex": (codex_adapter, _codex_stdout),
    "opencode": (opencode_adapter, _opencode_stdout),
}


class _FakeProcess:
    """Minimal stand-in for :class:`asyncio.subprocess.Process`."""

    def __init__(self, *, stdout: bytes, pid: int = _FAKE_PID) -> None:
        self._stdout = stdout
        self.returncode: int | None = 0
        self.pid = pid

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, b""

    def kill(self) -> None:  # pragma: no cover - the happy path never times out
        pass

    async def wait(self) -> int:  # pragma: no cover - the happy path never kills
        return 0


#: The CLI binary each vendor's argv leads with, mapped to the runtime key. The
#: shared subprocess fake routes the canned answer by the binary it is invoked
#: with, since all three adapters share one ``asyncio`` module object (a
#: per-module patch would let the last vendor's stdout win for every lane).
_BINARY_RUNTIME = {"claude": "claude-code", "codex": "codex", "opencode": "opencode"}


def _patch_subprocess_fleet(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[list[str]]]:
    """Fake the shared subprocess factory + neutralise each adapter's jail.

    The three adapters share ONE ``asyncio`` module (and one ``build_child_env``
    symbol), so a per-module subprocess patch would let the last vendor's stdout
    win for every lane. Instead ONE fake routes the canned answer by the CLI
    binary in argv[0] (``claude`` / ``codex`` / ``opencode``) and records each
    lane's argv under its runtime key. The per-module ``_maybe_jail_argv``
    (distinct per adapter) is neutralised on each adapter so the inner argv
    assertions stay deterministic across hosts. Returns the per-runtime captured
    argv map.

    The canned answer carries an executor body whose ``wave_id`` is recovered
    from the dispatched wave (the only wave pinned to that runtime in this
    fixture), so the report binding + verify gate pass and the lane closes.
    """
    argv_by_runtime: dict[str, list[list[str]]] = {rt: [] for rt in _WAVE_RUNTIME.values()}
    runtime_to_wave = {runtime: wid for wid, runtime in _WAVE_RUNTIME.items()}

    async def _fake_exec(*argv: str, **_kwargs: object) -> _FakeProcess:
        binary = next((token for token in argv if token in _BINARY_RUNTIME), None)
        if binary is None:
            raise AssertionError(f"spawn argv carried no known vendor binary: {argv!r}")
        runtime = _BINARY_RUNTIME[binary]
        argv_by_runtime[runtime].append(list(argv))
        _module, stdout_for = _VENDOR_STDOUT[runtime]
        return _FakeProcess(stdout=stdout_for(runtime_to_wave[runtime]))

    # One shared subprocess factory (claude.asyncio is codex.asyncio is
    # opencode.asyncio) + one shared env-scrub stub.
    monkeypatch.setattr(claude_adapter.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(claude_adapter, "build_child_env", lambda *_a, **_k: {"PATH": "/usr/bin"})
    # The jail wrapper is defined per-adapter, so neutralise each one.
    for module in (claude_adapter, codex_adapter, opencode_adapter):
        monkeypatch.setattr(
            module, "_maybe_jail_argv", lambda argv, *, runtime, cwd, session="", sink=None: argv
        )
    return argv_by_runtime


# --------------------------------------------------------------------------- #
# On-disk state: three PENDING waves (one per vendor) + a global deny policy.
# --------------------------------------------------------------------------- #


def _wave_payload(wave_id: str) -> dict[str, Any]:
    return {
        "id": wave_id,
        "iter_id": "P30-I12",
        "title": f"Lane {wave_id[-3:]}",
        "status": "pending",
        "deps": [],
        "blocks": [],
        "file_scopes": ["src/eawf/runtime/daemon/methods/fleet.py"],
        "success_criteria": [
            {
                "id": "CR-01",
                "text": "spawn in-loop through the real dispatch path",
                "kind": "legacy",
                "acceptance_style": "binary",
                "evidence_kind": "attested",
                "quality_dimension": "functional_suitability",
                "measurable_signal": "spawn in-loop through the real dispatch path",
            }
        ],
        "agent_role": "executor",
        "effort_bucket": "M",
        "claim_session_id": None,
        "worktree_id": None,
        "token_budget": None,
        "tokens_consumed": 0,
        "outcome": None,
        "opened_at": "2026-06-11T00:00:00Z",
        "closed_at": None,
        "runtime_preference": [_WAVE_RUNTIME[wave_id]],
    }


def _state_payload() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:EAWF",
        "updated_at": "2026-06-11T00:00:00Z",
        "dispatch_paused": False,
        "project": {
            "code": "EAWF",
            "slug": "eawf",
            "title": "Eawf",
            "description": "",
            "domains": ["workflow"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:EAWF",
        },
        "current": {
            "project_code": "EAWF",
            "track_id": None,
            "phase_id": "P30",
            "iter_id": "P30-I12",
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {
            "P30": {
                "id": "P30",
                "scope_id": "EAWF",
                "title": "Binding pass",
                "status": "active",
                "iter_ids": ["P30-I12"],
                "outcome_ids": [],
                "opened_at": "2026-06-11T00:00:00Z",
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P30-I12": {
                "id": "P30-I12",
                "phase_id": "P30",
                "title": "Fleet drive parity",
                "status": "active",
                "wave_ids": list(_WAVE_IDS),
                "estimate_id": None,
                "audit_id": None,
                "opened_at": "2026-06-11T00:00:00Z",
                "closed_at": None,
            }
        },
        "waves": {wid: _wave_payload(wid) for wid in _WAVE_IDS},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
        "sandbox_policies": {
            "POL-1": {
                "id": "POL-1",
                "scope_kind": "global",
                "scope_id": "EAWF",
                "allowed_tools": [],
                "denied_tools": list(_DENIED),
                "granted_at": "2026-06-11T00:00:00Z",
            }
        },
    }


def _write_state(tmp_path: Path) -> Path:
    state = State.model_validate(_state_payload())
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    path = state_dir / "state.json"
    path.write_text(state.model_dump_json(), encoding="utf-8")
    return path


def _ctx(state_path: Path) -> MethodContext:
    event_path = state_path.parent / "store" / "event.jsonl"
    return MethodContext(
        started_at="2026-06-11T00:00:00+00:00",
        pid=4321,
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        bus=EventBus(),
        event_path=event_path,
        state_path=state_path,
    )


def _drive_inloop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, dict[str, list[list[str]]]]:
    """Drive the loop over the 3-vendor frontier via the REAL spawner.

    Patches each adapter module's subprocess factory (one canned answer per
    vendor) + neutralises the jail, then arms the drive with the live
    ``_default_spawner`` (no spawn override -- the loop genuinely spawns each
    lane through ``agent.dispatch(spawn=True)``). Concurrency 1 + a ``closed``
    watcher keep the round structure deterministic. Returns the terminal run +
    the per-vendor captured argv map.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    argv_by_runtime = _patch_subprocess_fleet(monkeypatch)

    run = arm_drive(
        ctx,
        frontier=list(_WAVE_IDS),
        concurrency=1,
        # NO spawn override: the loop drives the live spawner, which claims the
        # wave then dispatches it spawn=True through the runtime-agnostic path.
        watch=lambda c, lane: "closed",
    )
    return run, argv_by_runtime


# --------------------------------------------------------------------------- #
# C1: the loop spawns all three runtimes via agent.dispatch(spawn=True).
# --------------------------------------------------------------------------- #


def test_loop_spawns_all_three_runtimes_one_lane_per_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1: each of the three lanes is spawned through the real dispatch path.

    The drive is armed with the LIVE spawner, so each lane is claimed then
    dispatched ``spawn=True`` through ``agent.dispatch`` onto its pinned
    vendor's adapter. Every vendor's faked subprocess factory received exactly
    one spawn, proving the loop drove all three runtimes through the one
    runtime-agnostic dispatch seam.
    """
    _run, argv_by_runtime = _drive_inloop(tmp_path, monkeypatch)

    # Exactly one spawn landed on each vendor's adapter (one lane per run).
    for runtime in ("claude-code", "codex", "opencode"):
        assert len(argv_by_runtime[runtime]) == 1, f"{runtime} did not spawn exactly once"

    # The argv each vendor's real adapter built carries that vendor's CLI verb,
    # confirming the lane routed to the right adapter (not a single shared one).
    assert "claude" in argv_by_runtime["claude-code"][0]
    assert "codex" in argv_by_runtime["codex"][0]
    assert "exec" in argv_by_runtime["codex"][0]
    assert "opencode" in argv_by_runtime["opencode"][0]
    assert "run" in argv_by_runtime["opencode"][0]


def test_loop_drains_all_three_lanes_to_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1: the three lanes drain to DONE/drained with one closed lane each.

    The loop dispatched + closed all three lanes (concurrency 1 = one lane in
    flight per round), so the run reaches DONE/drained with three claimed +
    dispatched + closed counters and an empty terminal lane registry.
    """
    run, _argv = _drive_inloop(tmp_path, monkeypatch)

    assert run.run_state is FleetRunState.DONE
    assert run.terminal_reason is FleetTerminalReason.DRAINED
    assert run.counters.claimed == 3
    assert run.counters.dispatched == 3
    assert run.counters.closed == 3
    assert run.counters.forked == 0
    assert run.lanes == {}
    assert run.frontier == []


def test_loop_registers_an_executor_session_per_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1: each spawned lane registered exactly one executor session row.

    The live spawner registers an executor ``AgentSession`` per lane through
    the canonical writer; after the drive the persisted state carries one
    executor session per wave, each pinned to that lane's runtime -- proving the
    spawn=True path ran end-to-end for all three vendors.
    """
    run, _argv = _drive_inloop(tmp_path, monkeypatch)
    assert run.run_state is FleetRunState.DONE

    state = load_state(tmp_path / ".ea" / "state.json")
    by_scope = {
        sess.scope_id: sess.runtime
        for sess in state.agent_sessions.values()
        if sess.scope_id in _WAVE_IDS
    }
    assert set(by_scope) == set(_WAVE_IDS)
    for wid in _WAVE_IDS:
        assert by_scope[wid] == _WAVE_RUNTIME[wid]


# --------------------------------------------------------------------------- #
# C2: deny is honoured by each runtime's REAL mechanism, in-loop.
# --------------------------------------------------------------------------- #


def _claude_disallowed_token(argv: list[str]) -> str | None:
    """Return the value of claude's ``--disallowedTools`` flag, or ``None``."""
    for index, token in enumerate(argv):
        if token == "--disallowedTools" and index + 1 < len(argv):
            return argv[index + 1]
    return None


def _codex_tool_grants(argv: list[str]) -> dict[str, bool]:
    """Parse the ``-c tools.<name>=<bool>`` grant map from a codex argv."""
    grants: dict[str, bool] = {}
    for index, token in enumerate(argv):
        if token != "-c" or index + 1 >= len(argv):
            continue
        key_value = argv[index + 1]
        if not key_value.startswith("tools."):
            continue
        key, _, value = key_value.partition("=")
        grants[key.removeprefix("tools.")] = value.strip().lower() == "true"
    return grants


def test_claude_lane_denies_via_inverted_allowlist_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C2: the in-loop claude lane carries the denied tools on --disallowedTools.

    Claude grants an inverted allowlist by carrying the deny-list on the
    ``--disallowedTools`` argv flag; the denied tools land on that flag (the
    deny mechanism) and never appear as a bare positive grant token -- proving
    the deny reached the real claude argv through the whole in-loop path.
    """
    _run, argv_by_runtime = _drive_inloop(tmp_path, monkeypatch)
    argv = argv_by_runtime["claude-code"][0]

    disallowed = _claude_disallowed_token(argv)
    assert disallowed is not None, "claude argv carried no --disallowedTools flag"
    # The deny flag carries the sorted, space-joined denied set.
    assert disallowed == "Bash Edit"
    # The denied tools never appear as a standalone positive grant token.
    assert "Edit" not in argv
    assert "Bash" not in argv


def test_codex_lane_denies_via_inverted_allowlist_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C2: the in-loop codex lane pins each denied tool false + omits it from true.

    Codex grants by inverted allowlist via ``-c tools.<name>=<bool>``: the
    denied tools are absent from the ``true`` grant set AND pinned ``false``, so
    a denied tool can never reach the child's effective grant through the
    in-loop dispatch path.
    """
    _run, argv_by_runtime = _drive_inloop(tmp_path, monkeypatch)
    argv = argv_by_runtime["codex"][0]

    grants = _codex_tool_grants(argv)
    granted_true = {tool for tool, granted in grants.items() if granted}
    for denied in _DENIED:
        assert denied.lower() not in granted_true, f"{denied} leaked into the codex true grant"
        assert grants.get(denied.lower()) is False, f"{denied} was not pinned false"


def test_opencode_lane_denies_via_fs_jail_floor_not_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C2: the in-loop opencode lane honours the deny by its FS-jail floor.

    opencode offers no per-call deny flag, so its deny is honoured by the
    FS-jail floor (the mechanism it actually offers): the denied tool names
    never reach any positive argv grant, and no claude/codex-style grant flag
    appears on the opencode argv -- so the deny cannot silently pass through.
    """
    _run, argv_by_runtime = _drive_inloop(tmp_path, monkeypatch)
    argv = argv_by_runtime["opencode"][0]

    # No fictional allow/deny flag: the denied tool names never appear on argv,
    # and neither the claude nor the codex grant surfaces are present.
    assert not any(tool in argv for tool in _DENIED)
    assert "--disallowedTools" not in argv
    assert not any(token.startswith("tools.") for token in argv)


def test_opencode_deny_is_jail_backed_warn_distinct_from_argv_hard_deny(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C2: opencode's deny mechanism is the jail floor (warn), not an argv hard deny.

    The mechanism opencode actually offers is the FS-jail floor, NOT a per-call
    argv deny flag. This pins the distinction the in-loop opencode lane relies
    on: handed a deny + an enforcement sink, the opencode adapter records the
    deny at ``warn`` severity (jail-backed) -- distinct from the claude / codex
    ``block`` severity (the argv hard deny) -- and the denied tool names never
    reach a positive argv grant. This is the per-runtime mechanism the loop's
    opencode lane consumes; tests 1-3 prove the lane routes there and the argv
    stays grant-free in-loop, and this proves the mechanism is the jail floor.
    """
    import asyncio

    from eawf.runtime.runtimes.opencode.adapter import OpenCodeAdapter
    from eawf.runtime.sandbox.egress_proxy import SandboxEnforcementEvent

    calls: list[list[str]] = []

    async def _fake_exec(*argv: str, **_kwargs: object) -> _FakeProcess:
        calls.append(list(argv))
        return _FakeProcess(stdout=_opencode_stdout(_OPENCODE_WAVE))

    monkeypatch.setattr(opencode_adapter.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(
        opencode_adapter,
        "_maybe_jail_argv",
        lambda argv, *, runtime, cwd, session="", sink=None: argv,
    )
    monkeypatch.setattr(opencode_adapter, "build_child_env", lambda *_a, **_k: {"PATH": "/usr/bin"})

    recorded: list[SandboxEnforcementEvent] = []
    asyncio.run(
        OpenCodeAdapter().spawn_session(
            "x",
            model="m",
            denied_tools=list(_DENIED),
            session="SES-x",
            enforcement_sink=recorded.append,
        )
    )

    deny_events = [event for event in recorded if event.kind == "argv-deny"]
    assert len(deny_events) == 1
    # Jail-backed: warn severity, NOT the claude / codex block hard-deny.
    assert deny_events[0].severity == "warn"
    assert deny_events[0].target == "Bash Edit"
    # The denied tool names never reached a positive argv grant.
    argv = calls[0]
    assert not any(tool in argv for tool in _DENIED)
    assert "--disallowedTools" not in argv
