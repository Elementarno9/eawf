"""A filed proof read back from the ledger is checked against the key it stores.

A line whose stored freshness key its own inputs no longer produce is refused on read,
whether the key or the inputs were edited; the doctor re-key repair supersedes such a
line rather than the reader forgiving it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.delivery.receipts import (
    ProofFreshnessKey,
    ProofReceipt,
    RevisionBinding,
    RevisionRefKind,
    canonical_digest,
)
from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.spec.common import GateSpec
from eawf.kernel.state.enums import GateReceiptResult
from eawf.runtime.daemon.methods.delivery_completion import FiledProof

AT = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
TASK = parse_qualified_urn("eawf://EAWF/task/EAWF-0013")
STALE_KEY = "0" * 64


def _key() -> ProofFreshnessKey:
    binding = RevisionBinding(
        repository_ref=parse_qualified_urn("eawf://EAWF/repository/EAWF"),
        ref_kind=RevisionRefKind.INTEGRATION,
        head_sha="1" * 40,
        tree_sha="2" * 40,
        parent_sha="4" * 40,
        batch_ref=parse_qualified_urn("eawf://EAWF/batch/BAT-0100"),
        integration_generation=2,
        manifest_digest=canonical_digest("manifest"),
        criteria_digest=canonical_digest("criteria"),
        policy_digest=canonical_digest("binding-policy"),
        environment_digest=None,
        bound_at=AT,
    )
    return ProofFreshnessKey(
        revision_binding=binding,
        criterion_digest=canonical_digest("criterion"),
        gate_digest=canonical_digest("gate"),
        selector_digest=canonical_digest("selector"),
        policy_digest=canonical_digest("policy"),
        runner_digest=canonical_digest("runner"),
        environment_digest=canonical_digest("environment"),
    )


def _gate() -> GateSpec:
    return GateSpec(
        id="G-01",
        criterion_id="CR-001",
        kind="command_exit_zero",
        args={"argv": ["uv", "run", "pytest", "-q"]},
        policy="block",
        cadence="every-wave",
    )


def _receipt(key: ProofFreshnessKey) -> ProofReceipt:
    return ProofReceipt(
        id="RCP-0001",
        scope_id="EAWF-0013",
        gate_id="G-01",
        criterion_ids=("CR-001",),
        evidence_kind="deterministic",
        freshness=key,
        freshness_key=key.digest(),
        result=GateReceiptResult.PASS,
        exit_status=0,
        started_at=AT,
        ended_at=AT + timedelta(seconds=5),
    )


def _line(**receipt_overrides: Any) -> dict[str, Any]:
    payload = FiledProof(task_ref=TASK, gate=_gate(), receipt=_receipt(_key())).model_dump(
        mode="json"
    )
    payload["receipt"].update(receipt_overrides)
    return payload


def test_a_line_with_its_current_key_reads_back_unchanged() -> None:
    line = _line()
    assert FiledProof.model_validate(line).model_dump(mode="json") == line


def test_a_line_whose_stored_key_its_inputs_no_longer_produce_is_refused() -> None:
    with pytest.raises(ValidationError, match="freshness_key does not match its inputs"):
        FiledProof.model_validate(_line(freshness_key=STALE_KEY))


def test_a_line_whose_inputs_were_edited_is_refused() -> None:
    line = _line()
    line["receipt"]["freshness"]["revision_binding"]["batch_ref"] = "eawf://EAWF/batch/BAT-0101"
    with pytest.raises(ValidationError, match="freshness_key does not match its inputs"):
        FiledProof.model_validate(line)


def test_a_line_whose_key_is_off_by_one_digit_is_refused() -> None:
    key = _key().digest()
    flipped = key[:-1] + ("0" if key[-1] != "0" else "1")
    with pytest.raises(ValidationError, match="freshness_key does not match its inputs"):
        FiledProof.model_validate(_line(freshness_key=flipped))


@pytest.mark.parametrize("key", ["", "0" * 63, "0" * 65, "G" * 64])
def test_a_line_whose_key_is_malformed_is_refused(key: str) -> None:
    with pytest.raises(ValidationError, match="freshness_key"):
        FiledProof.model_validate(_line(freshness_key=key))


def test_a_built_receipt_keeps_its_strict_key_check() -> None:
    key = _key()
    built = _receipt(key)
    assert FiledProof(task_ref=TASK, gate=_gate(), receipt=built).receipt is built
    with pytest.raises(ValidationError, match="freshness_key does not match its inputs"):
        ProofReceipt.model_validate({**built.model_dump(), "freshness_key": STALE_KEY})


def test_a_line_with_corrupt_freshness_inputs_is_refused() -> None:
    line = _line()
    del line["receipt"]["freshness"]["gate_digest"]
    with pytest.raises(ValidationError, match="gate_digest"):
        FiledProof.model_validate(line)


def test_a_line_whose_receipt_states_no_freshness_is_refused() -> None:
    line = _line()
    del line["receipt"]["freshness"]
    with pytest.raises(ValidationError, match="freshness"):
        FiledProof.model_validate(line)


def test_a_line_with_no_receipt_is_refused() -> None:
    line = _line()
    del line["receipt"]
    with pytest.raises(ValidationError, match="receipt"):
        FiledProof.model_validate(line)


@pytest.mark.parametrize("payload", [None, "proof", 7, []])
def test_a_payload_that_is_not_a_mapping_is_refused(payload: Any) -> None:
    with pytest.raises(ValidationError):
        FiledProof.model_validate(payload)
