"""Spec promote→READY validators (W09 — argv-policy attach point).

The daemon ``spec.promote`` handler (``runtime/daemon/methods/spec.py``)
flips a spec from DRAFT to READY via the
:func:`eawf.workflow.lifecycle.spec.validate_spec_transition` DAG. Before
the READY flip lands on disk this module runs the
*persistence-layer* argv-policy check: every :class:`GateSpec` whose
``kind`` is in :data:`ARGV_BEARING_GATE_KINDS` has its ``args["argv"]``
routed through :func:`eawf.runtime.sandbox.argv_policy.validate_gate_argv`
with the resolved allowlist. A reject raises
:class:`SpecPromoteValidationError` and the promote handler must NOT
flip the cache entry — atomicity is "validate first, then write" so a
bad spec stays in its prior status.

The same check fires at GateSpec **construction time** via the
``@model_validator`` on :class:`eawf.kernel.spec.common.GateSpec` (W09
adds it in the same commit) so a parser that builds GateSpec rows from
a markdown spec body cannot smuggle bad argv past
``model_validate``. The two checks are defense-in-depth: the
construction check stops bad-shaped rows at parse time; the persistence
check stops a bad row that was constructed in code (bypassing the
parser) from reaching READY.

Allowlist resolution is deliberately conservative for v0.4.0 — the
caller may pass an explicit ``allowlist`` to override the
module-level default :data:`DEFAULT_GATE_ARGV_ALLOWLIST`. A later wave
(P28-I01-W10) lands the profile-fed allowlist on
``ProfileBody.verify.argv_allowlist``; until then the default tuple is
the source of truth, intentionally narrow (only the dev-loop wrappers
+ tools the gauntlet already runs).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Final

from eawf.kernel.spec.common import GateSpec
from eawf.runtime.sandbox.argv_policy import (
    ArgvPolicyError,
    validate_gate_argv,
)

logger = logging.getLogger(__name__)


#: Gate kinds whose ``args["argv"]`` MUST pass the L0 argv-policy.
#:
#: Today only ``command_exit_zero`` carries an argv vector. Future
#: argv-bearing kinds (``script_run``, ``benchmark_exec``) extend this
#: set; non-argv kinds (``regex_match``, ``schema_validate``) stay
#: outside.
ARGV_BEARING_GATE_KINDS: Final[frozenset[str]] = frozenset({"command_exit_zero"})


#: Default argv-policy allowlist used when the caller does not supply one.
#:
#: TODO(P28-I01-W10): once the profile schema lands the
#: ``ProfileBody.verify.argv_allowlist`` field, the promote handler reads
#: the allowlist from the resolved profile and passes it through to
#: :func:`validate_argv_gates`. Until then this tuple is the conservative
#: floor — only the dev-loop wrappers + gate tools the local gauntlet
#: already invokes.
DEFAULT_GATE_ARGV_ALLOWLIST: Final[tuple[str, ...]] = (
    "uv",
    "uvx",
    "run",
    "pytest",
    "ruff",
    "mypy",
    "pre-commit",
    "git",
)


class SpecPromoteValidationError(ValueError):
    """Raised when an embedded gate's argv fails policy at promote time.

    Subclasses :class:`ValueError` so the daemon RPC handler's existing
    ``except ValueError`` branch maps the reject onto the same
    ``-32602 validation_failed`` JSON-RPC error class used for the rest
    of the promote-time invariants. The message names the offending
    gate id + the underlying argv-policy reason so triage works from the
    typed error alone.
    """


def validate_argv_gates(
    gates: Iterable[GateSpec],
    *,
    allowlist: Iterable[str] | None = None,
) -> None:
    """Validate every argv-bearing gate's argv vector through L0 policy.

    Walks *gates* and for each entry whose ``kind`` is in
    :data:`ARGV_BEARING_GATE_KINDS` extracts ``args["argv"]`` and routes
    it through :func:`eawf.runtime.sandbox.argv_policy.validate_gate_argv`
    with the resolved allowlist. A missing or non-list ``argv`` value on
    a kind that requires one raises
    :class:`SpecPromoteValidationError` naming the gate id. A
    :class:`eawf.runtime.sandbox.argv_policy.ArgvPolicyError` from the
    underlying validator is wrapped + re-raised as
    :class:`SpecPromoteValidationError` carrying the gate id + the
    original policy-reject message.

    The function returns ``None`` on pass (an empty *gates* iterable
    passes trivially) so the promote handler can call it in a
    pre-write position: ``validate_argv_gates(gates); write_cache(...)``.

    Args:
        gates: Iterable of :class:`GateSpec` rows to inspect. Non-argv
            kinds are skipped.
        allowlist: Optional iterable of permitted argv heads. Defaults
            to :data:`DEFAULT_GATE_ARGV_ALLOWLIST` when ``None``.

    Raises:
        SpecPromoteValidationError: When any argv-bearing gate's
            ``args["argv"]`` is missing, mis-shaped, or rejected by the
            L0 argv-policy. The message names the offending gate id
            and the underlying reason so callers can re-emit it
            verbatim.
    """
    resolved_allowlist = (
        list(allowlist) if allowlist is not None else list(DEFAULT_GATE_ARGV_ALLOWLIST)
    )
    for gate in gates:
        if gate.kind not in ARGV_BEARING_GATE_KINDS:
            continue
        argv = gate.args.get("argv")
        if argv is None:
            logger.warning(
                f"validate_argv_gates reject gate={gate.id!r} "
                f"reason=missing-argv kind={gate.kind!r}"
            )
            raise SpecPromoteValidationError(
                f"gate {gate.id!r} kind={gate.kind!r} missing required args['argv']"
            )
        try:
            validate_gate_argv(argv, allowlist=resolved_allowlist)
        except ArgvPolicyError as exc:
            reason = str(exc)
            logger.warning(
                f"validate_argv_gates reject gate={gate.id!r} reason=argv-policy detail={reason!r}"
            )
            raise SpecPromoteValidationError(
                f"gate {gate.id!r} argv rejected by L0 policy: {reason}"
            ) from exc


__all__ = [
    "ARGV_BEARING_GATE_KINDS",
    "DEFAULT_GATE_ARGV_ALLOWLIST",
    "SpecPromoteValidationError",
    "validate_argv_gates",
]
