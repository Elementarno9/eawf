"""Tests: ``spec.sync`` resolves every named eawf verb against the CLI tree.

A gate argv or a ``measurable_signal`` may name a verb path the CLI does
not have (``eawf telemetry sync``). The measurability lint reads criterion
vocabulary and the L0 argv policy reads only the argv head, so such a spec
used to sync clean and strand the wave at close, when the gate finally ran
and exited non-zero.

Coverage:

* the walk itself -- an unknown verb at the root and at depth, a real leaf
  command with its own positional arguments, global options and their
  values, a non-eawf head, a signal carrying zero / one / two commands;
* the error path -- ``require_resolvable_eawf_verbs`` raises
  ``DaemonValidationError`` naming the unresolved verb, and a tree that is
  not a Click group raises ``TypeError``;
* the wiring -- ``spec.sync`` refuses the unknown-verb body before any
  write and still accepts a body whose gate argv names a real leaf.
"""

from __future__ import annotations

import asyncio
import os
import textwrap
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
import orjson
import pytest

from eawf import __version__
from eawf.kernel.spec.common import CriterionSpec, GateSpec
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.models import State
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.spec import sync
from eawf.runtime.daemon.methods.spec_sync_lints import (
    _eawf_command_tree,
    find_unknown_eawf_verbs,
    require_resolvable_eawf_verbs,
)

_T0 = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)
_WAVE_ID = "P33-I01-W17"
_SIGNAL = "the verb walk refuses an unresolved eawf verb path at spec sync"


def _criterion(*, signal: str = _SIGNAL, gate_ids: list[str] | None = None) -> CriterionSpec:
    """Build a typed criterion carrying *signal* and optional gate bindings."""
    return CriterionSpec(
        id="CR-01",
        text="spec sync refuses an unresolved eawf verb path before any write",
        kind="behavioral",
        acceptance_style="binary",
        evidence_kind="deterministic",
        quality_dimension="functional_suitability",
        measurable_signal=signal,
        gate_ids=gate_ids or [],
    )


def _gate(argv: list[str], *, kind: str = "command_exit_zero") -> GateSpec:
    """Build a gate row whose ``args['argv']`` is *argv*."""
    return GateSpec(
        id="G-01",
        criterion_id="CR-01",
        kind=kind,
        args={"argv": argv},
        policy="block",
        cadence="every-wave",
    )


# ---- the walk --------------------------------------------------------------


def test_unknown_verb_under_a_real_group_is_flagged() -> None:
    findings = find_unknown_eawf_verbs([], [_gate(["eawf", "telemetry", "sync"])])
    assert [(f.resolved, f.verb) for f in findings] == [("eawf telemetry", "sync")]
    assert findings[0].path == "eawf telemetry sync"
    assert "gate 'G-01' argv" in findings[0].render()


def test_unknown_verb_at_the_root_is_flagged() -> None:
    findings = find_unknown_eawf_verbs([], [_gate(["uv", "run", "eawf", "telemetri", "sync"])])
    assert [(f.resolved, f.verb) for f in findings] == [("eawf", "telemetri")]


def test_real_leaf_verb_with_arguments_resolves() -> None:
    """A real leaf plus its positionals resolves; the args are not verbs."""
    assert (
        find_unknown_eawf_verbs([], [_gate(["eawf", "release", "show", "REL-1", "--json"])]) == []
    )
    assert find_unknown_eawf_verbs([], [_gate(["uv", "run", "eawf", "spec", "sync"])]) == []
    assert find_unknown_eawf_verbs([], [_gate(["eawf", "status"])]) == []


def test_non_eawf_argv_is_skipped_by_the_walk() -> None:
    """Argv under another head is never walked, even when it names a verb."""
    assert find_unknown_eawf_verbs([], [_gate(["pytest", "tests/unit", "-q"])]) == []
    assert find_unknown_eawf_verbs([], [_gate(["just", "test"])]) == []
    assert find_unknown_eawf_verbs([], [_gate(["uv", "run", "pytest", "telemetry", "sync"])]) == []


def test_bare_eawf_argv_names_no_verb() -> None:
    """Boundary: a single-token argv leaves the walk with nothing to resolve."""
    assert find_unknown_eawf_verbs([], [_gate(["eawf"])]) == []


def test_global_option_value_is_not_walked_as_a_verb() -> None:
    """A value-taking global option consumes the token after it."""
    argv = ["eawf", "--workspace", "telemetry", "--json", "spec", "sync"]
    assert find_unknown_eawf_verbs([], [_gate(argv)]) == []


def test_non_command_gate_argv_is_not_walked() -> None:
    """Only command gates carry an argv the gate runner will execute."""
    gate = GateSpec(
        id="G-01",
        criterion_id="CR-01",
        kind="schema_validate",
        args={"argv": ["eawf", "telemetry", "sync"]},
        policy="block",
        cadence="every-wave",
    )
    assert find_unknown_eawf_verbs([], [gate]) == []


def test_mis_shaped_argv_arg_is_not_walked() -> None:
    """Error path: a non-list argv on a non-command kind is left alone."""
    gate = GateSpec(
        id="G-01",
        criterion_id="CR-01",
        kind="schema_validate",
        args={"argv": "eawf telemetry sync"},
        policy="block",
        cadence="every-wave",
    )
    assert find_unknown_eawf_verbs([], [gate]) == []


# ---- measurable_signal sequences -------------------------------------------


def test_unknown_verb_in_a_measurable_signal_is_flagged() -> None:
    criterion = _criterion(signal="uv run eawf telemetry sync exits 0 on the synced wave")
    findings = find_unknown_eawf_verbs([criterion], [])
    assert [(f.resolved, f.verb) for f in findings] == [("eawf telemetry", "sync")]
    assert "criterion 'CR-01' measurable_signal" in findings[0].render()


def test_backticked_signal_command_resolves() -> None:
    criterion = _criterion(signal="`uv run eawf spec sync` exits 0 and the typed rows land.")
    assert find_unknown_eawf_verbs([criterion], []) == []


def test_signal_without_a_command_yields_no_finding() -> None:
    """Boundary: prose naming no ``uv run eawf`` sequence is never walked."""
    assert find_unknown_eawf_verbs([_criterion()], []) == []
    other = _criterion(signal="uv run pytest -q over the daemon methods suite")
    assert find_unknown_eawf_verbs([other], []) == []


def test_two_signal_commands_are_walked_independently() -> None:
    criterion = _criterion(
        signal="uv run eawf spec sync passes, then uv run eawf telemetry sync exits 0"
    )
    findings = find_unknown_eawf_verbs([criterion], [])
    assert [(f.resolved, f.verb) for f in findings] == [("eawf telemetry", "sync")]


def test_gate_findings_precede_criterion_findings() -> None:
    criterion = _criterion(signal="uv run eawf telemetri status reports the row")
    findings = find_unknown_eawf_verbs([criterion], [_gate(["eawf", "telemetry", "sync"])])
    assert [f.verb for f in findings] == ["sync", "telemetri"]


# ---- the raiser ------------------------------------------------------------


def test_require_resolvable_eawf_verbs_passes_a_resolvable_body() -> None:
    require_resolvable_eawf_verbs(
        wave_id=_WAVE_ID,
        criteria=[_criterion()],
        gates=[_gate(["uv", "run", "eawf", "spec", "sync"])],
    )


def test_require_resolvable_eawf_verbs_names_the_unresolved_verb() -> None:
    with pytest.raises(DaemonValidationError) as excinfo:
        require_resolvable_eawf_verbs(
            wave_id=_WAVE_ID,
            criteria=[],
            gates=[_gate(["eawf", "telemetry", "sync"])],
        )
    message = str(excinfo.value)
    assert "validation_failed" in message
    assert "eawf telemetry sync" in message
    assert _WAVE_ID in message


def test_command_tree_rejects_a_non_group_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """Error path: a CLI that builds no group leaves nothing to walk."""
    import typer.main

    monkeypatch.setattr(typer.main, "get_command", lambda app: click.Command("eawf"))
    _eawf_command_tree.cache_clear()
    try:
        with pytest.raises(TypeError, match="Click group"):
            _eawf_command_tree()
    finally:
        _eawf_command_tree.cache_clear()


# ---- the spec.sync wiring --------------------------------------------------

_BODY_TEMPLATE = textwrap.dedent(
    """\
    criteria:
      - id: CR-01
        text: returns the synced gate rows; pytest
          tests/unit/runtime/daemon/methods/test_spec_sync_verb_resolution.py
        kind: behavioral
        acceptance_style: binary
        evidence_kind: deterministic
        quality_dimension: functional_suitability
        measurable_signal: the synced command gate argv names a real eawf leaf verb
        gate_ids: [G-01]
    gates:
      - id: G-01
        criterion_id: CR-01
        kind: command_exit_zero
        args: {{argv: [{argv}]}}
        policy: block
        cadence: every-wave
    """
)

_UNKNOWN_VERB_YAML = _BODY_TEMPLATE.format(argv="eawf, telemetry, sync")
_REAL_VERB_YAML = _BODY_TEMPLATE.format(argv="uv, run, eawf, release, show, REL-1")


def _wrap_body(yaml_block: str) -> str:
    return (
        "# Wave deliverable\n\nAuthored prose.\n\n"
        f"```eawf-wave-body\n{yaml_block}```\n\nTrailing prose.\n"
    )


def _state_payload() -> dict[str, Any]:
    """A minimal valid State with one PENDING wave under P33-I01."""
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:EAWF",
        "updated_at": _T0.isoformat(),
        "project": {
            "code": "EAWF",
            "slug": "eawf",
            "title": "EAWF",
            "description": None,
            "domains": ["x"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:EAWF",
        },
        "current": {"project_code": "EAWF"},
        "workspace": None,
        "phases": {
            "P33": {
                "id": "P33",
                "scope_id": "EAWF",
                "track_id": None,
                "title": "P33",
                "status": "active",
                "iter_ids": ["P33-I01"],
                "outcome_ids": [],
                "opened_at": _T0.isoformat(),
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P33-I01": {
                "id": "P33-I01",
                "phase_id": "P33",
                "title": "I01",
                "status": "active",
                "wave_ids": [_WAVE_ID],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": _T0.isoformat(),
                "closed_at": None,
            }
        },
        "waves": {
            _WAVE_ID: {
                "id": _WAVE_ID,
                "iter_id": "P33-I01",
                "title": "resolve eawf verbs named in criteria",
                "status": "pending",
                "file_scopes": [],
                "success_criteria": [],
                "gates": [],
                "effort_bucket": "S",
                "agent_role": "executor",
                "opened_at": _T0.isoformat(),
                "closed_at": None,
                "outcome": None,
                "sessions": {},
                "intent": {
                    "problem": "a gate argv may name an eawf verb the CLI does not have",
                    "desired_outcome": "spec sync refuses an unresolved eawf verb path",
                    "planned_steps": [],
                    "source_brief_ids": [],
                },
            }
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _prepare(tmp_path: Path, yaml_block: str) -> tuple[Path, Path, MethodContext]:
    repo_root = tmp_path / f"repo-{uuid.uuid4().hex[:8]}"
    repo_root.mkdir()
    state_path = repo_root / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        State.model_validate(_state_payload()).model_dump_json(), encoding="utf-8"
    )
    spec_path = repo_root / ".ea" / "specs" / "P33" / "P33-I01" / f"{_WAVE_ID}.md"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(_wrap_body(yaml_block), encoding="utf-8")
    wal_dir = tmp_path / f"wal-{uuid.uuid4().hex[:8]}"
    wal_dir.mkdir(parents=True, exist_ok=True)
    ctx = MethodContext(
        started_at=_T0.isoformat(),
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
        bus=EventBus(),
        event_path=store_path(state_path, StoreKind.EVENT),
        state_path=state_path,
        wal_dir=wal_dir,
        idempotency_cache={},
    )
    return repo_root, state_path, ctx


def _run(body: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(body())


def test_sync_refuses_an_unknown_verb_before_any_write(tmp_path: Path) -> None:
    """The reject names the verb path and the wave row stays untouched."""
    repo_root, state_path, ctx = _prepare(tmp_path, _UNKNOWN_VERB_YAML)
    before = state_path.read_bytes()
    assert ctx.wal_dir is not None

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="eawf telemetry sync"):
            await sync(ctx, {"wave_id": _WAVE_ID, "repo_root": str(repo_root)})

    _run(body)

    assert state_path.read_bytes() == before
    assert not list(Path(ctx.wal_dir).glob("*.json"))
    wave = State.model_validate(orjson.loads(state_path.read_bytes())).waves[_WAVE_ID]
    assert wave.success_criteria == []
    assert wave.gates == []


def test_sync_accepts_a_gate_argv_naming_a_real_leaf_verb(tmp_path: Path) -> None:
    """The same body with a resolvable verb path syncs its rows through."""
    repo_root, state_path, ctx = _prepare(tmp_path, _REAL_VERB_YAML)

    async def body() -> None:
        result = await sync(ctx, {"wave_id": _WAVE_ID, "repo_root": str(repo_root)})
        assert result["criteria_count"] == 1
        assert result["gates_count"] == 1

    _run(body)

    wave = State.model_validate(orjson.loads(state_path.read_bytes())).waves[_WAVE_ID]
    assert [gate.args["argv"] for gate in wave.gates] == [
        ["uv", "run", "eawf", "release", "show", "REL-1"]
    ]
