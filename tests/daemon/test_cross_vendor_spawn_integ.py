"""Daemon-level cross-vendor spawn integration: the REAL adapter path (B088).

The existing ``test_live_spawn_dispatch.py`` cases drive
:func:`eawf.runtime.daemon.methods.agent.dispatch` (``spawn=True``) with a
``_StubAdapter`` that REPLACES ``spawn_session`` wholesale -- so the real
:meth:`CodexAdapter.spawn_session` / :meth:`OpenCodeAdapter.spawn_session`
(argv build -> ``_maybe_jail_argv`` -> ``build_child_env`` -> the vendor
event-stream parse) never executes on the daemon path. This module closes
that gap: it drives a codex AND an opencode spawn through the REAL adapter,
faking ONLY :func:`asyncio.create_subprocess_exec` (no real CLI, network,
auth, or cost) -- the jail seam, the env scrub, and the parse all run for
real.

Two integration concerns:

1. **The real parse + metering path.** A canned vendor event stream is
   replayed by a fake subprocess; the real adapter parses it into a
   :class:`~eawf.runtime.runtimes.adapter.SpawnResult`, the metering writer
   prices that result via the per-vendor pricing, and a ``dispatch_cost``
   row carrying the right runtime + a real (non-zero) cost lands on the
   event log.
2. **The opencode FS-jail engagement.** Post-5bbdbffa the opencode auth
   lane is registered with the shared jail, so the REAL ``_maybe_jail_argv``
   (with the wrapper mocked present + the cwd inside a repo root) WRAPS the
   inner argv rather than degrading to unjailed -- distinct from the
   wrapper-absent path that runs the argv unchanged.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from eawf import __version__
from eawf.kernel.store.envelope import Envelope
from eawf.observability.telemetry.pricing import lookup_pricing
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.agent import dispatch
from eawf.runtime.runtimes.codex import adapter as codex_adapter
from eawf.runtime.runtimes.opencode import adapter as opencode_adapter

pytestmark = pytest.mark.integration

_WAVE_ID = "P29-I04-W01"
_FAKE_PID = 4321


#: The spawned agent's modeled answer: a schema-valid ``ExecutorReportBody``
#: JSON string. The live-spawn report path binds the agent's OWN answer text to
#: a validated executor body via the schema-assist re-ask loop, so the canned
#: vendor answer must be valid executor JSON (not free prose). ``wave_id``
#: matches the dispatched wave (the verify gate equality check).
_EXECUTOR_ANSWER = json.dumps(
    {
        "role": "executor",
        "verdict": "pass",
        "confidence": "high",
        "summary": "executor implemented the wave",
        "wave_id": _WAVE_ID,
        "files_changed": ["src/eawf/runtime/daemon/methods/agent.py"],
        "tests_run": ["uv run pytest tests/daemon -q"],
        "outcome": "spawned through the real adapter and bound the report",
    }
)


# --------------------------------------------------------------------------- #
# Canned vendor event streams (captured shapes; NEVER a real subprocess).
# --------------------------------------------------------------------------- #

#: A well-formed ``codex exec --json`` stream: thread.started -> item.completed
#: (agent_message) -> turn.completed (usage). The gross input total (27243)
#: includes the 4480 cached tokens, which the parse splits so they never
#: double-count.
_CODEX_EVENTS: list[dict[str, object]] = [
    {"type": "thread.started", "thread_id": "thr-codex-1"},
    {"type": "turn.started"},
    {
        "type": "item.completed",
        "item": {"id": "item_0", "type": "agent_message", "text": _EXECUTOR_ANSWER},
    },
    {
        "type": "turn.completed",
        "usage": {
            "input_tokens": 27243,
            "cached_input_tokens": 4480,
            "output_tokens": 17,
        },
    },
]

#: A well-formed ``opencode run --format json`` stream: step_start -> text ->
#: step_finish carrying the token map + a self-reported cost.
_OPENCODE_EVENTS: list[dict[str, object]] = [
    {"type": "step_start", "sessionID": "ses-opencode-1", "part": {"type": "step-start"}},
    {
        "type": "text",
        "sessionID": "ses-opencode-1",
        "part": {"type": "text", "text": _EXECUTOR_ANSWER},
    },
    {
        "type": "step_finish",
        "sessionID": "ses-opencode-1",
        "part": {
            "type": "step-finish",
            "reason": "stop",
            "tokens": {
                "total": 30580,
                "input": 30489,
                "output": 7,
                "cache": {"write": 12, "read": 34},
            },
            "cost": 0.0021,
        },
    },
]


def _ndjson(events: list[dict[str, object]]) -> bytes:
    """Serialise an event list to a newline-delimited JSON byte stream."""
    return ("\n".join(json.dumps(row) for row in events) + "\n").encode("utf-8")


class _FakeProcess:
    """Minimal stand-in for :class:`asyncio.subprocess.Process`.

    Replays a fixed ``(stdout, stderr)`` from :meth:`communicate`; the
    patched ``create_subprocess_exec`` factory records the argv separately.
    """

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


def _patch_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    adapter_module: Any,
    stdout: bytes,
) -> list[list[str]]:
    """Fake ONLY ``create_subprocess_exec`` on *adapter_module*.

    The jail seam, the env scrub, and the result parse are left REAL -- the
    point of this integration is that the daemon drives the genuine adapter
    machinery. Returns a sink capturing each spawned argv so a test can see
    the command line the real adapter built.
    """
    calls: list[list[str]] = []

    async def _fake_exec(*argv: str, **_kwargs: object) -> _FakeProcess:
        calls.append(list(argv))
        return _FakeProcess(stdout=stdout)

    monkeypatch.setattr(adapter_module.asyncio, "create_subprocess_exec", _fake_exec)
    return calls


# --------------------------------------------------------------------------- #
# On-disk state with a wave pinned to a foreign runtime.
# --------------------------------------------------------------------------- #


def _state_payload(*, runtime: str, effort_bucket: str) -> dict[str, Any]:
    """A minimal valid State whose only wave pins ``runtime_preference=[runtime]``."""
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:EAWF",
        "updated_at": "2026-06-01T00:00:00Z",
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
            "phase_id": "P29",
            "iter_id": "P29-I04",
            "active_wave_ids": [_WAVE_ID],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {
            "P29": {
                "id": "P29",
                "scope_id": "EAWF",
                "title": "v0.5",
                "status": "active",
                "iter_ids": ["P29-I04"],
                "outcome_ids": [],
                "opened_at": "2026-06-01T00:00:00Z",
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P29-I04": {
                "id": "P29-I04",
                "phase_id": "P29",
                "title": "Spawn spine",
                "status": "active",
                "wave_ids": [_WAVE_ID],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": "2026-06-01T00:00:00Z",
                "closed_at": None,
            }
        },
        "waves": {
            _WAVE_ID: {
                "id": _WAVE_ID,
                "iter_id": "P29-I04",
                "title": "Cross-vendor spawn",
                "status": "claimed",
                "deps": [],
                "blocks": [],
                "file_scopes": ["src/eawf/runtime/daemon/methods/agent.py"],
                "success_criteria": [
                    {
                        "id": "CR-01",
                        "text": "spawn through the real adapter",
                        "kind": "legacy",
                        "acceptance_style": "binary",
                        "evidence_kind": "attested",
                        "quality_dimension": "functional_suitability",
                        "measurable_signal": "spawn through the real adapter",
                    }
                ],
                "agent_role": "executor",
                "effort_bucket": effort_bucket,
                "claim_session_id": None,
                "worktree_id": None,
                "token_budget": None,
                "tokens_consumed": 0,
                "outcome": None,
                "opened_at": "2026-06-01T00:00:00Z",
                "claimed_at": "2026-06-01T00:00:00Z",
                "closed_at": None,
                "runtime_preference": [runtime],
            }
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _write_state(tmp_path: Path, *, runtime: str, effort_bucket: str) -> Path:
    """Serialise a valid :class:`State` to ``<tmp>/.ea/state.json``."""
    from eawf.kernel.state.models import State

    state = State.model_validate(_state_payload(runtime=runtime, effort_bucket=effort_bucket))
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    path = state_dir / "state.json"
    path.write_text(state.model_dump_json(), encoding="utf-8")
    return path


def _ctx(state_path: Path, *, event_path: Path) -> MethodContext:
    return MethodContext(
        started_at="2026-06-01T00:00:00+00:00",
        pid=4321,
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        bus=EventBus(),
        event_path=event_path,
        state_path=state_path,
    )


def _read_envelopes(path: Path) -> list[Envelope]:
    rows: list[Envelope] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(Envelope.model_validate_json(line))
    return rows


def _dispatch_cost_payloads(event_path: Path) -> list[dict[str, Any]]:
    return [
        env.payload
        for env in _read_envelopes(event_path)
        if env.payload.get("event_type") == "dispatch_cost"
    ]


def _run(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Criterion 1: real codex/opencode SpawnResult -> metering -> dispatch_cost.
# --------------------------------------------------------------------------- #


def test_codex_spawn_flows_through_real_parse_and_metering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A codex spawn drives the REAL adapter parse + metering to a priced cost.

    Only ``create_subprocess_exec`` is faked; the real ``CodexAdapter``
    parses the canned ``codex exec --json`` stream into a ``SpawnResult``,
    the metering writer prices it against ``gpt-5-codex``, and the emitted
    ``dispatch_cost`` carries the codex runtime + the real token-derived
    cost (the gross input split so cached tokens never double-count).
    """
    state_path = _write_state(tmp_path, runtime="codex", effort_bucket="L")
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    argv_calls = _patch_subprocess(monkeypatch, codex_adapter, _ndjson(_CODEX_EVENTS))
    ctx = _ctx(state_path, event_path=event_path)

    _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    # The real adapter built (and the fake exec received) a codex argv.
    assert len(argv_calls) == 1
    assert "codex" in argv_calls[0]
    assert "exec" in argv_calls[0]

    pricing = lookup_pricing("gpt-5-codex")
    assert pricing is not None
    # The real parse split 27243 gross into 27243-4480 non-cached + 4480 read.
    expected = (
        (27243 - 4480) * pricing.input_per_token
        + 17 * pricing.output_per_token
        + 4480 * pricing.cache_read_per_token
    )
    costs = _dispatch_cost_payloads(event_path)
    assert len(costs) == 1
    assert costs[0]["runtime"] == "codex"
    assert costs[0]["model"] == "gpt-5-codex"
    assert Decimal(costs[0]["cost_usd"]) == expected
    assert Decimal(costs[0]["cost_usd"]) > Decimal("0")


def test_opencode_spawn_flows_through_real_parse_and_metering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An opencode spawn drives the REAL adapter parse + metering to a priced cost.

    Only ``create_subprocess_exec`` is faked; the real ``OpenCodeAdapter``
    parses the canned ``opencode run --format json`` stream, the metering
    writer prices it against the resolved ``anthropic/claude-opus-4-8``
    model, and the emitted ``dispatch_cost`` carries the opencode runtime +
    a real non-zero cost.
    """
    state_path = _write_state(tmp_path, runtime="opencode", effort_bucket="L")
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    argv_calls = _patch_subprocess(monkeypatch, opencode_adapter, _ndjson(_OPENCODE_EVENTS))
    ctx = _ctx(state_path, event_path=event_path)

    _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    assert len(argv_calls) == 1
    assert "opencode" in argv_calls[0]
    assert "run" in argv_calls[0]

    costs = _dispatch_cost_payloads(event_path)
    assert len(costs) == 1
    assert costs[0]["runtime"] == "opencode"
    assert costs[0]["model"] == "anthropic/claude-opus-4-8"
    assert Decimal(costs[0]["cost_usd"]) > Decimal("0")


def test_codex_spawn_result_carries_real_runtime_and_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The metered cost reflects the REAL parsed token tally, not a stub spread.

    A zero-output variant proves the cost tracks the parsed stream: dropping
    the output tokens lowers the priced cost below the full-stream cost, so
    the figure is derived from the real parse rather than a canned constant.
    """
    no_output = [
        {"type": "thread.started", "thread_id": "thr-codex-2"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": _EXECUTOR_ANSWER},
        },
        {"type": "turn.completed", "usage": {"input_tokens": 1000, "output_tokens": 0}},
    ]
    state_path = _write_state(tmp_path, runtime="codex", effort_bucket="L")
    event_path = tmp_path / ".ea" / "store" / "event.jsonl"
    _patch_subprocess(monkeypatch, codex_adapter, _ndjson(no_output))
    ctx = _ctx(state_path, event_path=event_path)

    _run(dispatch(ctx, {"wave_id": _WAVE_ID, "spawn": True}))

    pricing = lookup_pricing("gpt-5-codex")
    assert pricing is not None
    # No cached tokens here, so the whole 1000 is non-cached input; no output.
    expected = 1000 * pricing.input_per_token
    costs = _dispatch_cost_payloads(event_path)
    assert len(costs) == 1
    assert Decimal(costs[0]["cost_usd"]) == expected


# --------------------------------------------------------------------------- #
# Criterion 2: opencode FS-jail engages (lane registered post-5bbdbffa).
# --------------------------------------------------------------------------- #


def test_opencode_maybe_jail_argv_wraps_when_wrapper_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the wrapper present + cwd in a repo root, the REAL seam JAILS the argv.

    Drives the genuine ``_maybe_jail_argv`` (NOT a passthrough stub) with the
    jail primitives mocked so the host platform does not matter: the wrapper
    resolves, the cwd is inside a repo root, and ``jail_command`` prefixes a
    marker. The opencode lane is registered with the shared jail (5bbdbffa),
    so the inner ``opencode run ...`` argv is WRAPPED rather than degrading.
    """
    inner = ["opencode", "run", "--format", "json", "-m", "anthropic/claude-x", "prompt"]
    seen: list[list[str]] = []

    def _fake_jail_command(argv: list[str], *, runtime: str, cwd: Path, root: Path) -> list[str]:
        seen.append(list(argv))
        return ["JAIL", runtime, *argv]

    monkeypatch.setattr(opencode_adapter, "jail_supported", lambda: True)
    monkeypatch.setattr(opencode_adapter, "_jail_wrapper_binary", lambda _platform: "sandbox-exec")
    monkeypatch.setattr(opencode_adapter.shutil, "which", lambda _binary: "/usr/bin/sandbox-exec")
    monkeypatch.setattr(opencode_adapter, "_repo_root_for", lambda _path: tmp_path)
    monkeypatch.setattr(opencode_adapter, "is_path_inside", lambda _path, *, root: True)
    monkeypatch.setattr(opencode_adapter, "jail_command", _fake_jail_command)

    jailed = opencode_adapter._maybe_jail_argv(list(inner), runtime="opencode", cwd=str(tmp_path))

    # The lane is registered, so the real seam wrapped the full inner argv.
    assert seen == [inner]
    assert jailed[0] == "JAIL"
    assert jailed[1] == "opencode"
    assert jailed[2:] == inner


def test_opencode_maybe_jail_argv_unjailed_when_wrapper_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With NO wrapper on PATH, the REAL seam runs the argv unchanged (the contrast).

    The distinct degrade path: the platform supports a jail but the wrapper
    binary is absent, so ``_maybe_jail_argv`` returns the inner argv
    untouched -- the wrapper-present jail above is the meaningful contrast.
    """
    inner = ["opencode", "run", "--format", "json", "-m", "anthropic/claude-x", "prompt"]

    monkeypatch.setattr(opencode_adapter, "jail_supported", lambda: True)
    monkeypatch.setattr(opencode_adapter, "_jail_wrapper_binary", lambda _platform: "sandbox-exec")
    # Wrapper binary does not resolve on PATH -> unjailed degrade.
    monkeypatch.setattr(opencode_adapter.shutil, "which", lambda _binary: None)

    result = opencode_adapter._maybe_jail_argv(list(inner), runtime="opencode", cwd=None)

    assert result == inner
