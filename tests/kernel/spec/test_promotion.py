"""Tests for :mod:`eawf.kernel.spec.promotion`.

Pins the persistence-layer argv-policy check the daemon ``spec.promote``
handler runs before flipping a spec to READY:

1. Argv-bearing gates with a clean argv pass.
2. Argv-bearing gates with a shell-deny / metachar / non-allowlisted
   head raise :class:`SpecPromoteValidationError` naming the gate id
   and the underlying L0 reject reason.
3. A missing ``args['argv']`` on an argv-bearing kind raises the same
   typed error.
4. Non-argv kinds (``regex_match``, ``schema_validate``) are skipped
   so a mixed spec with one ``regex_match`` + one good
   ``command_exit_zero`` passes.
5. The function is a no-op on an empty iterable (so the daemon can
   call it pre-write even when the body parser has not yet landed —
   v0.4.0 seam ahead of W08).
6. Custom ``allowlist`` argument is honoured (overrides
   :data:`DEFAULT_GATE_ARGV_ALLOWLIST`).
"""

from __future__ import annotations

import pytest

from eawf.kernel.spec.common import GateSpec
from eawf.kernel.spec.promotion import (
    ARGV_BEARING_GATE_KINDS,
    DEFAULT_GATE_ARGV_ALLOWLIST,
    SpecPromoteValidationError,
    validate_argv_gates,
)


def _build_gate(
    *,
    id: str = "G1",
    criterion_id: str = "C1",
    kind: str = "command_exit_zero",
    args: dict[str, object] | None = None,
    policy: str = "block",
    cadence: str = "every-wave",
) -> GateSpec:
    """Return a GateSpec with the supplied overrides on minimal valid defaults."""
    return GateSpec.model_validate(
        {
            "id": id,
            "criterion_id": criterion_id,
            "kind": kind,
            "args": args if args is not None else {"argv": ["pytest", "-q"]},
            "policy": policy,
            "cadence": cadence,
        }
    )


def _construct_gate(
    *,
    id: str = "G1",
    criterion_id: str = "C1",
    kind: str = "command_exit_zero",
    args: dict[str, object] | None = None,
    policy: str = "block",
    cadence: str = "every-wave",
) -> GateSpec:
    """Return a GateSpec bypassing model_validate (skips W09 construction check).

    The persistence-layer :func:`validate_argv_gates` check is the
    backstop AFTER the model-level check. Tests that exercise the
    persistence path in isolation build their rejecting rows via
    :meth:`pydantic.BaseModel.model_construct` so the construction
    validator does not intercept the input first.
    """
    return GateSpec.model_construct(
        id=id,
        criterion_id=criterion_id,
        kind=kind,
        args=args if args is not None else {"argv": ["pytest", "-q"]},
        policy=policy,
        cadence=cadence,
        required=True,
        timeout_s=None,
    )


# Happy paths -----------------------------------------------------------------


def test_validate_argv_gates_empty_iterable_passes() -> None:
    """An empty iterable is a no-op pass (v0.4.0 daemon-seam contract)."""
    assert validate_argv_gates([]) is None


def test_validate_argv_gates_clean_command_exit_zero_passes() -> None:
    """A single clean ``command_exit_zero`` row validates without raising."""
    gate = _build_gate(args={"argv": ["uv", "run", "pytest", "-q"]})
    assert validate_argv_gates([gate]) is None


def test_validate_argv_gates_skips_non_argv_kinds() -> None:
    """Kinds outside :data:`ARGV_BEARING_GATE_KINDS` are skipped."""
    gate = _build_gate(kind="regex_match", args={"pattern": r"^OK$", "input": "OK"})
    # No argv key — would explode if the validator did not skip the row.
    assert validate_argv_gates([gate]) is None


def test_validate_argv_gates_mixed_kinds_skips_non_argv() -> None:
    """A spec with one regex_match + one good command_exit_zero passes."""
    regex_gate = _build_gate(
        id="G1",
        kind="regex_match",
        args={"pattern": "x", "input": "x"},
    )
    cmd_gate = _build_gate(
        id="G2",
        kind="command_exit_zero",
        args={"argv": ["ruff", "check", "."]},
    )
    assert validate_argv_gates([regex_gate, cmd_gate]) is None


def test_default_allowlist_used_when_caller_omits_allowlist() -> None:
    """``allowlist=None`` falls back to :data:`DEFAULT_GATE_ARGV_ALLOWLIST`."""
    gate = _build_gate(args={"argv": ["pre-commit", "run", "--all-files"]})
    # ``pre-commit`` is in the default; this passes only because the
    # default is wired through correctly.
    assert "pre-commit" in DEFAULT_GATE_ARGV_ALLOWLIST
    assert validate_argv_gates([gate]) is None


def test_custom_allowlist_overrides_default() -> None:
    """An explicit ``allowlist`` argument overrides the module-level default.

    Uses :meth:`pydantic.BaseModel.model_construct` to build the
    GateSpec with a head (``customtool``) that the construction-time
    validator would reject against the default allowlist; the
    persistence-layer call then passes when the explicit allowlist
    names it.
    """
    gate = _construct_gate(args={"argv": ["customtool", "subcmd"]})
    assert validate_argv_gates([gate], allowlist=["customtool"]) is None


# Reject paths ----------------------------------------------------------------


def test_validate_argv_gates_rejects_shell_deny_head() -> None:
    """``argv=["sh", "-c", "rm -rf /"]`` raises :class:`SpecPromoteValidationError`.

    The reject names the offending gate id + the underlying L0 reason.
    Built via :meth:`pydantic.BaseModel.model_construct` so the
    persistence-layer check is exercised in isolation.
    """
    gate = _construct_gate(id="G42", args={"argv": ["sh", "-c", "rm -rf /"]})
    with pytest.raises(SpecPromoteValidationError) as exc_info:
        validate_argv_gates([gate])
    message = str(exc_info.value)
    assert "G42" in message
    assert "rejected by L0 policy" in message


def test_validate_argv_gates_rejects_non_allowlisted_head() -> None:
    """An out-of-allowlist head raises naming the gate id."""
    gate = _construct_gate(id="G7", args={"argv": ["rogue", "--flag"]})
    with pytest.raises(SpecPromoteValidationError) as exc_info:
        validate_argv_gates([gate])
    message = str(exc_info.value)
    assert "G7" in message
    assert "not in the caller-supplied allowlist" in message


def test_validate_argv_gates_rejects_shell_metachars() -> None:
    """A shell-metacharacter inside any argv element raises."""
    gate = _construct_gate(id="G5", args={"argv": ["pytest", "tests/*"]})
    with pytest.raises(SpecPromoteValidationError) as exc_info:
        validate_argv_gates([gate])
    message = str(exc_info.value)
    assert "G5" in message
    assert "shell metacharacter" in message


def test_validate_argv_gates_rejects_missing_argv() -> None:
    """An argv-bearing kind with no ``args['argv']`` raises naming the gate id.

    The model_validator would normally catch this at construction
    time — bypass via :meth:`pydantic.BaseModel.model_construct`
    (through :func:`_construct_gate`) so the persistence-layer check
    is exercised in isolation. Pins the contract that
    :func:`validate_argv_gates` is independently defensive (defense in
    depth).
    """
    gate = _construct_gate(id="G9", args={})
    with pytest.raises(SpecPromoteValidationError) as exc_info:
        validate_argv_gates([gate])
    message = str(exc_info.value)
    assert "G9" in message
    assert "missing required args['argv']" in message


def test_validate_argv_gates_subclass_of_value_error() -> None:
    """:class:`SpecPromoteValidationError` is a :class:`ValueError`.

    The daemon RPC handler's existing ``except ValueError`` branch
    maps it onto ``-32602 validation_failed``; this pin keeps the
    JSON-RPC error mapping intact.
    """
    assert issubclass(SpecPromoteValidationError, ValueError)


def test_argv_bearing_gate_kinds_includes_command_exit_zero() -> None:
    """The argv-bearing kind table includes ``command_exit_zero``."""
    assert "command_exit_zero" in ARGV_BEARING_GATE_KINDS
