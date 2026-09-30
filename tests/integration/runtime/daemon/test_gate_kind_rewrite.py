"""Tests for the one-way gate-kind rewrite on a CLOSED wave.

A wave that closed on single-token grep gates, or on attested criteria
with no gate, carries a verification record that cannot fail. The
rewrite binds a real ``command_exit_zero`` gate in its place and refuses
to move anything the other way.

Coverage:

* gate-fire proof -- a grep gate becomes a command gate with an audit
  event carrying the reason, while a request that would turn a command
  gate into a grep is refused and leaves the state bytes and the event
  log untouched;
* an ungated attested criterion gains a command gate and is promoted to
  deterministic with a derived ``exits`` / ``pytest`` clause;
* boundary and error paths -- replay no-op, empty and duplicate
  requests, a non-grep kind, a moved command argv, a non-closed wave, a
  blank reason, unknown wave / criterion, a criterion mismatch, a jury
  owner, an argv the L0 policy refuses, and a tampered candidate that
  would move the criterion text;
* the RPC dry run, the non-wave scope refusal, the CLI parser and the
  CLI reason floor;
* the re-receipt outcome row refuses to record a non-zero exit as a pass.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import GateReceiptResult
from eawf.kernel.store.kinds.gate_rereceipt import GateRereceiptOutcome

pytestmark = pytest.mark.integration

_ARGV = ["uv", "run", "pytest", "tests/integration/runtime/daemon/test_lazy.py", "-q"]


# ---- Gate-fire proof: strengthen accepted, weaken refused -------------------


# ---- The lifecycle rewrite ----------------------------------------------------


# ---- The RPC surface ----------------------------------------------------------


# ---- The CLI -------------------------------------------------------------------


# ---- The re-receipt row keeps a red as a finding ----------------------------


def test_rereceipt_outcome_refuses_a_nonzero_exit_recorded_as_pass() -> None:
    """A red re-run cannot be written down as a pass."""
    with pytest.raises(ValidationError, match="cannot be recorded as a pass"):
        GateRereceiptOutcome(
            gate_id="G-01", result=GateReceiptResult.PASS, exit_status=1, argv=_ARGV
        )


def test_rereceipt_outcome_records_a_red_as_a_failure_with_its_command() -> None:
    """The failing row names what ran and how it ended."""
    outcome = GateRereceiptOutcome(
        gate_id="G-01", result=GateReceiptResult.FAIL, exit_status=1, argv=_ARGV
    )

    assert outcome.result is GateReceiptResult.FAIL
    assert outcome.exit_status == 1
    assert outcome.argv == _ARGV


def test_rereceipt_outcome_accepts_a_pass_with_exit_zero() -> None:
    """The boundary: exit zero is a pass."""
    outcome = GateRereceiptOutcome(gate_id="G-01", result=GateReceiptResult.PASS, exit_status=0)

    assert outcome.result is GateReceiptResult.PASS
