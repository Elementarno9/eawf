"""Tests: the jury-validation CLI binding stays wired in the idle gate.

This repo has a history of shipping jury-validation bindings built-but-idle
(the B091 idle-verifier regression). The idle-contract meta-gate
(``tools/idle_contract_gate.py``) registers :func:`check_validate_jury_cli_wired`
so a change that re-idles it fails the gate: ``validate_jury`` must keep a live
CLI caller (the ``eawf metrics jury-validation`` command).

The binding asserts two contracts:

- **pass on the real tree** -- the check passes against the live source.
- **red on a re-idled fixture** -- an injected ``module_text`` that removes the
  CLI caller fails the check with the matching :class:`GateFailure`.

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
_REPO_ROOT = Path(__file__).resolve().parents[3]


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
        "    # the report comes from validate_jury per the module docstring\n    return None\n"
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


def test_main_exits_zero_over_real_tree(gate: Any) -> None:
    """The whole idle-contract gate exits 0 over the real tree.

    Runs ``main`` against a no-op diff range so the meta-gate sees no staged
    change; every binding row passes against the real source, so the
    aggregate exit code is 0.
    """
    exit_code = gate.main(["idle_contract_gate.py", "HEAD..HEAD"])
    assert exit_code == 0
