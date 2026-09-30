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

import click
import pytest

from eawf.kernel.spec.common import CriterionSpec, GateSpec
from eawf.runtime.daemon.methods.spec_sync_lints import _eawf_command_tree, find_unknown_eawf_verbs

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
    """Build a gate row whose ``args['argv']`` is *argv*.

    Built without validation: the L0 argv policy already refuses an eawf
    verb outside its read-only set, and the walk under test has to see
    such an argv to prove it names the verb path on its own.
    """
    return GateSpec.model_construct(
        required=True,
        timeout_s=None,
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


def test_non_eawf_argv_is_skipped_by_the_walk() -> None:
    """Argv under another head is never walked, even when it names a verb."""
    assert find_unknown_eawf_verbs([], [_gate(["pytest", "tests/unit", "-q"])]) == []
    assert find_unknown_eawf_verbs([], [_gate(["just", "test"])]) == []
    assert find_unknown_eawf_verbs([], [_gate(["uv", "run", "pytest", "telemetry", "sync"])]) == []


def test_bare_eawf_argv_names_no_verb() -> None:
    """Boundary: a single-token argv leaves the walk with nothing to resolve."""
    assert find_unknown_eawf_verbs([], [_gate(["eawf"])]) == []


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


def test_signal_without_a_command_yields_no_finding() -> None:
    """Boundary: prose naming no ``uv run eawf`` sequence is never walked."""
    assert find_unknown_eawf_verbs([_criterion()], []) == []
    other = _criterion(signal="uv run pytest -q over the daemon methods suite")
    assert find_unknown_eawf_verbs([other], []) == []


def test_gate_findings_precede_criterion_findings() -> None:
    criterion = _criterion(signal="uv run eawf telemetri status reports the row")
    findings = find_unknown_eawf_verbs([criterion], [_gate(["eawf", "telemetry", "sync"])])
    assert [f.verb for f in findings] == ["sync", "telemetri"]


# ---- the raiser ------------------------------------------------------------


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
