"""The committed receipt backfill for the P32 and P33 waves that closed unproven.

Thirty-four P32 waves closed with required command gates and no receipt,
because the band-scoped close gate skipped them, and P32-I01-W44 closed
with attested criteria and no gate at all. Nine P33 waves closed
with nothing a gate could prove: W94 to W101 were authored at phase close
with attested criteria and no gate, and I02-W01's criteria were converted
into single-token greps that pass on any file naming the token.

These assertions read the repository's own state and stores, not a
fixture. For each of the 44 waves the record must show one of two things.
Either every required criterion carries a named waiver, or the wave holds
real command gates, no grep gate survives, and a ``gate_rereceipt`` row
re-ran exactly those gates at the wave's on-main commit. A red re-run is
allowed only as a finding: an open backlog row must name the wave and the
binding, so a failure can never read as a quiet pass.

Two record corrections ride the same backfill: the measured contract row
MCT-26091101 carries the corpus magnitude under its own name, and P32-I01-W09
CR-01 names the shorthand that is actually ambiguous in the pinned corpus.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf.kernel.state.enums import GateReceiptResult, WaveStatus
from eawf.kernel.state.models import State, Wave
from eawf.kernel.store.kinds.gate_rereceipt import GateRereceiptBinding
from eawf.workflow.lifecycle.gate_kind_rewrite import COMMAND_GATE_KIND, GREP_GATE_KINDS

pytestmark = pytest.mark.integration

_EA_ROOT = Path(__file__).resolve().parents[3] / ".ea"
_STATE_PATH = _EA_ROOT / "state.json"
_REREC_STORE_PATH = _EA_ROOT / "store" / "gate_rereceipt.jsonl"

_P32_WAVES: tuple[str, ...] = tuple(
    f"P32-I01-W{index:02d}"
    for index in (
        2, 3, 4, 5, 8, 9, 10, 11, 13, 19, 20, 22, 24, 25, 26, 27, 28,
        29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45,
        46,
    )
)  # fmt: skip
_P33_WAVES: tuple[str, ...] = (
    *(f"P33-I01-W{index}" for index in range(94, 102)),
    "P33-I02-W01",
)
_BACKFILL_WAVES: tuple[str, ...] = _P32_WAVES + _P33_WAVES


@pytest.fixture(scope="module")
def state() -> State:
    """The committed state document."""
    return State.model_validate_json(_STATE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def bindings() -> dict[str, list[GateRereceiptBinding]]:
    """Every committed re-receipt binding, grouped by wave in append order."""
    grouped: dict[str, list[GateRereceiptBinding]] = {}
    if not _REREC_STORE_PATH.exists():
        return grouped
    for line in _REREC_STORE_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        envelope: dict[str, Any] = json.loads(line)
        binding = GateRereceiptBinding.model_validate(envelope["payload"])
        grouped.setdefault(binding.wave_id, []).append(binding)
    return grouped


def _gate_manifest_digest(wave: Wave) -> str:
    """The digest a re-receipt binds its gate list to, computed the same way."""
    manifest = [gate.model_dump(mode="json") for gate in wave.gates]
    return hashlib.sha256(orjson.dumps(manifest, option=orjson.OPT_SORT_KEYS)).hexdigest()


def _is_waived(wave: Wave) -> bool:
    """Whether every required criterion carries a named waiver."""
    required = [criterion for criterion in wave.success_criteria if criterion.required]
    return bool(required) and all(
        criterion.waiver_reason and criterion.waiver_reason.strip() for criterion in required
    )


def test_backfill_set_is_the_thirty_five_p32_and_nine_p33_waves() -> None:
    """The scope is fixed, so a wave silently leaving it reds here."""
    assert len(_P32_WAVES) == 35
    assert len(_P33_WAVES) == 9
    assert len(set(_BACKFILL_WAVES)) == 44


def test_no_closed_p32_or_p33_wave_keeps_an_ungated_legacy_criterion(state: State) -> None:
    """The frozen list is cross-checked against the record it was drawn from.

    A required criterion that is not deliberately ``attested`` must be proved
    by a non-grep gate or carry a named waiver, so a closed wave with the
    same unproven profile as the backfill set cannot sit outside it.
    """
    unproven: list[str] = []
    for wave in state.waves.values():
        if wave.status is not WaveStatus.CLOSED or not wave.id.startswith(("P32-", "P33-")):
            continue
        gates = {gate.id: gate for gate in wave.gates}
        for criterion in wave.success_criteria:
            if not criterion.required or criterion.kind == "attested":
                continue
            if criterion.waiver_reason and criterion.waiver_reason.strip():
                continue
            kinds = {gates[gate_id].kind for gate_id in criterion.gate_ids if gate_id in gates}
            if not kinds or kinds <= set(GREP_GATE_KINDS):
                unproven.append(f"{wave.id} {criterion.id}")
    assert not unproven, f"closed waves with an unproven required criterion: {unproven}"


@pytest.mark.parametrize("wave_id", _BACKFILL_WAVES)
def test_wave_has_a_command_gate_rereceipt_or_a_named_waiver(
    wave_id: str,
    state: State,
    bindings: dict[str, list[GateRereceiptBinding]],
) -> None:
    """Each wave is either waived by name or re-run on real command gates."""
    wave = state.waves[wave_id]
    assert wave.status == WaveStatus.CLOSED
    if _is_waived(wave):
        return

    kinds = {gate.kind for gate in wave.gates}
    assert COMMAND_GATE_KIND in kinds, f"{wave_id} binds no command gate"
    assert not kinds & GREP_GATE_KINDS, f"{wave_id} still carries grep gates {kinds}"
    gated = {gate.criterion_id for gate in wave.gates}
    for criterion in wave.success_criteria:
        if criterion.required and not criterion.waiver_reason:
            assert criterion.id in gated, f"{wave_id} {criterion.id} has no gate"

    rows = bindings.get(wave_id, [])
    assert rows, f"{wave_id} has no gate_rereceipt row"
    binding = rows[-1]
    assert binding.landed_sha == wave.commit, f"{wave_id} re-ran off its on-main commit"
    assert binding.gate_manifest_digest == _gate_manifest_digest(wave), (
        f"{wave_id} was re-run against a gate list it no longer records"
    )
    assert {outcome.gate_id for outcome in binding.gates} == {gate.id for gate in wave.gates}
    blocked = [o.gate_id for o in binding.gates if o.result is GateReceiptResult.BLOCKED]
    assert not blocked, f"{wave_id} gates {blocked} never ran"


@pytest.mark.parametrize("wave_id", _BACKFILL_WAVES)
def test_a_red_rereceipt_is_named_as_an_open_finding(
    wave_id: str,
    state: State,
    bindings: dict[str, list[GateRereceiptBinding]],
) -> None:
    """A failing re-run must be filed, never absorbed as a pass."""
    rows = bindings.get(wave_id, [])
    if not rows:
        pytest.skip(f"{wave_id} has no binding; the coverage test reports it")
    binding = rows[-1]
    red = [o.gate_id for o in binding.gates if o.result is not GateReceiptResult.PASS]
    if not red:
        return
    findings = [
        row
        for row in state.backlog.values()
        if row.status.value == "open"
        and wave_id in (row.description or "")
        and binding.id in (row.description or "")
    ]
    assert findings, f"{wave_id} binding {binding.id} is red on {red} with no open backlog row"


def test_measured_contract_row_names_the_corpus_magnitude(state: State) -> None:
    """MCT-26091101 no longer spells the corpus magnitude as a scale band."""
    artifact = state.artifacts["MCT-26091101"]
    observed = artifact.metadata["observed"]
    assert "scale_band" not in observed
    assert observed["corpus_magnitude"] == "thousands"


def test_w09_criterion_names_the_ambiguous_corpus_shorthand(state: State) -> None:
    """The pinned corpus's ambiguous shorthand is P14-W01, and the text says so."""
    criterion = next(
        row for row in state.waves["P32-I01-W09"].success_criteria if row.id == "CR-01"
    )
    assert "P14-W01" in criterion.text
