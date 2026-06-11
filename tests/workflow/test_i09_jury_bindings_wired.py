"""Tests: the four I09 jury-validation bindings stay wired in the idle gate.

P30-I09 wired four jury-validation bindings that this repo has a history of
shipping built-but-idle (the B091 idle-verifier regression). P30-I09-W08 registers
each in the idle-contract meta-gate (``tools/idle_contract_gate.py``) so a future
change that re-idles any of them fails the gate:

- **W05 / TRUST-5** -- :func:`check_spec_jury_ballot_fn_wired`: the daemon close
  path binds the LIVE per-item ballot fn (``_spec_jury_ballot_fn`` returns
  ``live_per_item_ballot_fn(...)``, not ``None``) and consults it.
- **W06 / TRUST-6** -- :func:`check_jury_reliability_map_wired`: the live convener
  threads the reputation reliability map into ``aggregate_jury(..., reliability=...)``.
- **W04 / TRUST-4** -- :func:`check_jury_block_authority_wired`: the ordered-oracle
  jury branch tests ``block_authority is BlockAuthority.BLOCKING`` before it raises
  ``LifecycleError``.
- **W07 / TRUST-7** -- :func:`check_validate_jury_cli_wired`: ``validate_jury`` has a
  live CLI caller (the ``eawf metrics jury-validation`` command).

Each binding asserts two contracts, mirroring ``test_resolve_routing_wired``:

- **pass on the real tree** -- the check passes against the live source.
- **red on a re-idled fixture** -- an injected ``module_text`` that drops the
  binding (the ballot fn reverts to ``return None``, the reliability map goes
  ``None``, the block-authority gate is bypassed, or the CLI caller is removed)
  fails the check with the matching :class:`GateFailure`.

The gate module is loaded by path because ``tools/`` is excluded from the package
and so is not importable by name (mirrors ``tests/unit/test_idle_contract_gate.py``).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

#: Repo root: this file is ``tests/workflow/...`` so the root is three parents up.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_gate_module() -> Any:
    """Import ``tools/idle_contract_gate.py`` by path for the binding probes.

    The module is registered in ``sys.modules`` before ``exec_module`` so the
    gate's ``@dataclass`` definitions (which resolve ``cls.__module__`` under
    ``from __future__ import annotations``) find their own namespace.
    """
    name = "idle_contract_gate_i09_under_test"
    path = _REPO_ROOT / "tools" / "idle_contract_gate.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


@pytest.fixture()
def gate() -> Any:
    """Return the freshly loaded gate module."""
    return _load_gate_module()


# --------------------------------------------------------------------------- #
# W05 / TRUST-5: the spec-jury close gate binds a live per-item ballot fn.
# --------------------------------------------------------------------------- #


def test_spec_jury_ballot_fn_passes_for_real_source(gate: Any) -> None:
    """check_spec_jury_ballot_fn_wired passes against the real state.py source."""
    result = gate.check_spec_jury_ballot_fn_wired()
    assert result.passed, result.message
    assert result.failure is None


def test_spec_jury_ballot_fn_reds_when_builder_returns_none(gate: Any) -> None:
    """Reverting the builder to a bare ``return None`` reds the gate.

    A re-idled builder no longer binds ``live_per_item_ballot_fn(...)``, so the
    producer is unreachable and the gate must fail SPEC_JURY_BALLOT_FN_IDLE.
    """
    idle_text = (
        "def _spec_jury_ballot_fn(state, wave, *, repo_root):\n"
        "    return None  # reverted: the producer is idle again\n"
        "\n"
        "async def _enforce_spec_jury_gate(state, wave, *, state_path, repo_root):\n"
        "    ballot_fn = _spec_jury_ballot_fn(state, wave, repo_root=repo_root)\n"
        "    return False\n"
    )
    result = gate.check_spec_jury_ballot_fn_wired(module_text=idle_text)
    assert not result.passed
    assert result.failure is gate.GateFailure.SPEC_JURY_BALLOT_FN_IDLE
    assert "spec-jury per-item ballot fn is idle" in result.message


def test_spec_jury_ballot_fn_reds_when_gate_stops_consulting(gate: Any) -> None:
    """Dropping the ``ballot_fn = _spec_jury_ballot_fn(`` consult reds the gate.

    Boundary: the builder still binds the live fn, but no caller consults it, so
    the producer is never reached -- the gate must still red.
    """
    idle_text = (
        "def _spec_jury_ballot_fn(state, wave, *, repo_root):\n"
        "    return live_per_item_ballot_fn(spawn_factory=sf, rubric=rb)\n"
        "\n"
        "async def _enforce_spec_jury_gate(state, wave, *, state_path, repo_root):\n"
        "    return False  # the builder is never consulted now\n"
    )
    result = gate.check_spec_jury_ballot_fn_wired(module_text=idle_text)
    assert not result.passed
    assert result.failure is gate.GateFailure.SPEC_JURY_BALLOT_FN_IDLE


def test_spec_jury_ballot_fn_passes_for_minimal_wired_text(gate: Any) -> None:
    """A boundary text carrying both the bind + the consult satisfies the gate."""
    wired_text = (
        "    return live_per_item_ballot_fn(spawn_factory=sf, rubric=rb)\n"
        "    ballot_fn = _spec_jury_ballot_fn(state, wave, repo_root=repo_root)\n"
    )
    result = gate.check_spec_jury_ballot_fn_wired(module_text=wired_text)
    assert result.passed
    assert result.failure is None


# --------------------------------------------------------------------------- #
# W06 / TRUST-6: the live convener threads the reliability map into the reducer.
# --------------------------------------------------------------------------- #


def test_jury_reliability_map_passes_for_real_source(gate: Any) -> None:
    """check_jury_reliability_map_wired passes against the real convener source."""
    result = gate.check_jury_reliability_map_wired()
    assert result.passed, result.message
    assert result.failure is None


def test_jury_reliability_map_reds_when_map_goes_none(gate: Any) -> None:
    """Dropping the ``reliability=`` thread reds the gate.

    A re-idled convener forwards a hardcoded ``None`` instead of the threaded map,
    so every juror weights neutrally regardless of the reputation map and the gate
    must fail JURY_RELIABILITY_MAP_IDLE.
    """
    idle_text = (
        "def _reduce_jury(*, wave_id, jurors, quorum, reliability=None):\n"
        "    aggregate = aggregate_jury(ballots, reliability=None)  # map dropped\n"
        "    return aggregate\n"
    )
    result = gate.check_jury_reliability_map_wired(module_text=idle_text)
    assert not result.passed
    assert result.failure is gate.GateFailure.JURY_RELIABILITY_MAP_IDLE
    assert "jury reliability map is idle" in result.message


def test_jury_reliability_map_reds_when_aggregate_unforwarded(gate: Any) -> None:
    """The map is threaded but never forwarded into aggregate_jury -- still idle.

    Boundary: ``reliability=reliability`` appears at the convener boundary, but
    the reducer calls ``aggregate_jury(ballots)`` with no reliability kwarg, so the
    seam is dead at the sink -- the gate must still red.
    """
    idle_text = (
        "    return _reduce_jury(wave_id=w, jurors=j, quorum=q, reliability=reliability)\n"
        "def _reduce_jury(*, wave_id, jurors, quorum, reliability=None):\n"
        "    aggregate = aggregate_jury(ballots)  # the map never reaches the sink\n"
    )
    result = gate.check_jury_reliability_map_wired(module_text=idle_text)
    assert not result.passed
    assert result.failure is gate.GateFailure.JURY_RELIABILITY_MAP_IDLE


def test_jury_reliability_map_passes_for_minimal_wired_text(gate: Any) -> None:
    """A boundary text carrying the thread + the aggregate forward satisfies it."""
    wired_text = (
        "    return _reduce_jury(wave_id=w, jurors=j, quorum=q, reliability=reliability)\n"
        "    aggregate = aggregate_jury(ballots, reliability=reliability)\n"
    )
    result = gate.check_jury_reliability_map_wired(module_text=wired_text)
    assert result.passed
    assert result.failure is None


# --------------------------------------------------------------------------- #
# W04 / TRUST-4: the oracle jury branch consults block authority before raising.
# --------------------------------------------------------------------------- #


def test_jury_block_authority_passes_for_real_source(gate: Any) -> None:
    """check_jury_block_authority_wired passes against the real oracle source."""
    result = gate.check_jury_block_authority_wired()
    assert result.passed, result.message
    assert result.failure is None


def test_jury_block_authority_reds_when_gate_bypassed(gate: Any) -> None:
    """Removing the authority gate reds the gate.

    A re-idled oracle raises the veto unconditionally (or never), so the earned
    block-authority test no longer guards the raise and the gate must fail
    JURY_BLOCK_AUTHORITY_IDLE.
    """
    idle_text = (
        "    if status != 'pass':\n"
        "        raise LifecycleError('jury vetoed')  # unconditional, authority bypassed\n"
    )
    result = gate.check_jury_block_authority_wired(module_text=idle_text)
    assert not result.passed
    assert result.failure is gate.GateFailure.JURY_BLOCK_AUTHORITY_IDLE
    assert "jury block authority is idle" in result.message


def test_jury_block_authority_reds_when_raise_dropped(gate: Any) -> None:
    """The authority gate is present but never raises -- still idle.

    Boundary: a calibrated jury that has earned blocking can no longer block
    because the raise was dropped, so the staged advisory-to-block gate is dead --
    the gate must still red.
    """
    idle_text = (
        "    if block_authority is BlockAuthority.BLOCKING:\n"
        "        logger.warning('would block but never raises')\n"
    )
    result = gate.check_jury_block_authority_wired(module_text=idle_text)
    assert not result.passed
    assert result.failure is gate.GateFailure.JURY_BLOCK_AUTHORITY_IDLE


def test_jury_block_authority_passes_for_minimal_wired_text(gate: Any) -> None:
    """A boundary text carrying the gate + the raise satisfies the check."""
    wired_text = (
        "    if block_authority is BlockAuthority.BLOCKING:\n"
        "        raise LifecycleError('cross-vendor jury vetoed close')\n"
    )
    result = gate.check_jury_block_authority_wired(module_text=wired_text)
    assert result.passed
    assert result.failure is None


# --------------------------------------------------------------------------- #
# W07 / TRUST-7: validate_jury has a live CLI caller.
# --------------------------------------------------------------------------- #


def test_validate_jury_cli_passes_for_real_source(gate: Any) -> None:
    """check_validate_jury_cli_wired passes against the real metrics CLI source."""
    result = gate.check_validate_jury_cli_wired()
    assert result.passed, result.message
    assert result.failure is None


def test_validate_jury_cli_reds_when_caller_removed(gate: Any) -> None:
    """Orphaning the reducer (no CLI caller) reds the gate.

    A re-idled CLI no longer binds ``validate_jury(...)``, so the reducer is
    unreachable from the operator surface and the gate must fail
    VALIDATE_JURY_CLI_IDLE.
    """
    idle_text = (
        "def jury_validation(flags):\n"
        "    cohort = build_jury_validation_cohort(state, state_path)\n"
        "    emit_json_or_text({}, 'no reducer call', flags=flags)\n"
    )
    result = gate.check_validate_jury_cli_wired(module_text=idle_text)
    assert not result.passed
    assert result.failure is gate.GateFailure.VALIDATE_JURY_CLI_IDLE
    assert "validate_jury is idle" in result.message


def test_validate_jury_cli_reds_on_docstring_only_reference(gate: Any) -> None:
    """A bare ``validate_jury`` reference (no call paren) does not satisfy the gate.

    Boundary: a docstring / cross-link mention of the symbol with no trailing
    ``(`` is not a live call, so the gate must still red on it.
    """
    prose_only = (
        "    # the report comes from validate_jury per the module docstring\n"
        "    return None\n"
    )
    result = gate.check_validate_jury_cli_wired(module_text=prose_only)
    assert not result.passed
    assert result.failure is gate.GateFailure.VALIDATE_JURY_CLI_IDLE


def test_validate_jury_cli_passes_for_minimal_wired_text(gate: Any) -> None:
    """A boundary text carrying just the validate_jury call satisfies the gate."""
    wired_text = "    report = validate_jury(cohort, ballots_by_wave={})\n"
    result = gate.check_validate_jury_cli_wired(module_text=wired_text)
    assert result.passed
    assert result.failure is None


# --------------------------------------------------------------------------- #
# Whole-gate: main() exits 0 over the real post-I09 tree (all rows pass).
# --------------------------------------------------------------------------- #


def test_main_exits_zero_over_real_tree(gate: Any) -> None:
    """The whole idle-contract gate exits 0 over the post-I09 tree.

    Runs ``main`` against a no-op diff range so the meta-gate sees no staged
    change; the four I09 binding rows (plus every prior row) pass against the
    real source, so the aggregate exit code is 0.
    """
    exit_code = gate.main(["idle_contract_gate.py", "HEAD..HEAD"])
    assert exit_code == 0
