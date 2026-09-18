"""The control-outcome vocabulary is nine values and its phase map is total.

Two rules are pinned here, and they are the two a renderer would
otherwise invent for itself.

The first is that every persisted disposition is reachable from exactly
one phase. The compiled map is asserted verbatim rather than derived from
the module under test, because a table that checks itself proves only
that it is self-consistent; the pairings below are the contract, and a
pairing outside them has to fail before the line is appended.

The second is that ``idle`` is a projection and never a fact. It is the
answer for a control nobody issued, so it appears on no phase, no
:class:`ControlFact` can carry it, and the only place it is produced is
the projection over an empty fact sequence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.runtime.control import (
    PHASE_DISPOSITIONS,
    PROJECTED_DISPOSITIONS,
    TERMINAL_EFFECT_STATUS,
    ControlDisposition,
    ControlFact,
    ControlPhase,
    compile_phase_dispositions,
    compile_terminal_effects,
)
from eawf.kernel.runtime.provider import ControlKind
from eawf.kernel.state.epoch2.run import RunStatus
from eawf.runtime.control.reducer import project_disposition

pytestmark = pytest.mark.unit

RUN_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000010"
REQUEST = "CTL-0000000a"
EFFECT = "EFF-0000000b"
RECEIPT = "REC-0000000c"
AT = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)

#: The nine ratified outcome values, spelled as the wire carries them.
NINE_VALUES = (
    "idle",
    "requesting",
    "accepted",
    "confirmed",
    "rejected",
    "invalidated",
    "unknown",
    "recovery",
    "superseded",
)

#: The phase compatibility contract, written out rather than derived.
EXPECTED_MAP = {
    ControlPhase.REQUESTED: {ControlDisposition.REQUESTING},
    ControlPhase.ACKNOWLEDGED: {
        ControlDisposition.ACCEPTED,
        ControlDisposition.REJECTED,
        ControlDisposition.INVALIDATED,
        ControlDisposition.SUPERSEDED,
    },
    ControlPhase.EFFECTED: {
        ControlDisposition.CONFIRMED,
        ControlDisposition.UNKNOWN,
        ControlDisposition.RECOVERY,
    },
}


def fact(**overrides: Any) -> ControlFact:
    """Build a legal requested fact, with *overrides* applied."""
    payload: dict[str, Any] = {
        "control_request_ref": REQUEST,
        "run_ref": RUN_URN,
        "control": ControlKind.CANCEL,
        "phase": ControlPhase.REQUESTED,
        "disposition": ControlDisposition.REQUESTING,
        "actor": "OP-0001",
        "recorded_at": AT,
        "sequence": 1,
    }
    payload.update(overrides)
    return ControlFact.model_validate(payload)


# ---------------------------------------------------------------------------
# RUN-048: the vocabulary and the total phase map
# ---------------------------------------------------------------------------


def test_control_disposition_is_exactly_the_nine_ratified_values() -> None:
    assert tuple(value.value for value in ControlDisposition) == NINE_VALUES


def test_phase_dispositions_is_total_over_the_three_phases() -> None:
    assert set(PHASE_DISPOSITIONS) == set(ControlPhase)


def test_phase_dispositions_matches_the_written_contract() -> None:
    assert {phase: set(values) for phase, values in PHASE_DISPOSITIONS.items()} == EXPECTED_MAP


def test_every_persisted_disposition_is_reachable_from_exactly_one_phase() -> None:
    reached = [value for values in PHASE_DISPOSITIONS.values() for value in values]
    persisted = set(ControlDisposition) - PROJECTED_DISPOSITIONS

    assert sorted(reached, key=str) == sorted(persisted, key=str)


@pytest.mark.parametrize("phase", list(ControlPhase))
@pytest.mark.parametrize("disposition", list(ControlDisposition))
def test_control_fact_admits_a_pairing_only_when_the_map_does(
    phase: ControlPhase, disposition: ControlDisposition
) -> None:
    """Every one of the 27 pairings is admitted or refused by the map alone."""
    proof: dict[str, Any] = {}
    if disposition is ControlDisposition.CONFIRMED:
        proof["effect_ref"] = EFFECT
    if disposition in {ControlDisposition.UNKNOWN, ControlDisposition.RECOVERY}:
        proof["receipt_ref"] = RECEIPT

    if disposition in PHASE_DISPOSITIONS[phase]:
        built = fact(phase=phase, disposition=disposition, **proof)
        assert built.disposition is disposition
        return
    with pytest.raises(ValidationError, match="does not admit disposition"):
        fact(phase=phase, disposition=disposition, **proof)


# ---------------------------------------------------------------------------
# RUN-049: idle is projected, superseded writes no receipt
# ---------------------------------------------------------------------------


def test_idle_is_admitted_by_no_phase() -> None:
    assert not any(ControlDisposition.IDLE in values for values in PHASE_DISPOSITIONS.values())


def test_idle_is_the_projection_over_an_absent_request() -> None:
    assert project_disposition(()) is ControlDisposition.IDLE


def test_a_single_fact_projects_to_its_own_disposition() -> None:
    assert project_disposition((fact(),)) is ControlDisposition.REQUESTING


def test_a_superseded_row_carrying_a_receipt_fails_validation() -> None:
    with pytest.raises(ValidationError, match="neither applied nor refused"):
        fact(
            phase=ControlPhase.ACKNOWLEDGED,
            disposition=ControlDisposition.SUPERSEDED,
            receipt_ref=RECEIPT,
        )


def test_a_confirmed_effect_without_an_effect_reference_fails_validation() -> None:
    with pytest.raises(ValidationError, match="names the effect that was observed"):
        fact(phase=ControlPhase.EFFECTED, disposition=ControlDisposition.CONFIRMED)


def test_an_unknown_effect_without_a_receipt_fails_validation() -> None:
    with pytest.raises(ValidationError, match="reconciliation receipt"):
        fact(phase=ControlPhase.EFFECTED, disposition=ControlDisposition.UNKNOWN)


def test_an_unknown_effect_naming_an_effect_fails_validation() -> None:
    with pytest.raises(ValidationError, match="no effect was observed"):
        fact(
            phase=ControlPhase.EFFECTED,
            disposition=ControlDisposition.UNKNOWN,
            receipt_ref=RECEIPT,
            effect_ref=EFFECT,
        )


def test_a_requested_fact_naming_an_effect_fails_validation() -> None:
    with pytest.raises(ValidationError, match="records no effect reference"):
        fact(effect_ref=EFFECT)


def test_a_fact_with_an_unknown_key_is_refused() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        fact(disposition_note="looks harmless")


def test_a_sequence_below_one_is_refused() -> None:
    with pytest.raises(ValidationError, match="greater than 0"):
        fact(sequence=0)


def test_a_malformed_request_reference_is_refused() -> None:
    with pytest.raises(ValidationError, match="should match pattern"):
        fact(control_request_ref="CTL-XYZ")


# ---------------------------------------------------------------------------
# The compilers refuse a table that is not total and single-valued
# ---------------------------------------------------------------------------


def test_compiling_an_empty_table_names_every_missing_phase() -> None:
    with pytest.raises(ValueError, match="no dispositions declared for phase"):
        compile_phase_dispositions({})


def test_compiling_a_table_missing_one_phase_is_refused() -> None:
    declared = dict(EXPECTED_MAP)
    declared.pop(ControlPhase.EFFECTED)

    with pytest.raises(ValueError, match="effected"):
        compile_phase_dispositions(declared)


def test_a_phase_admitting_idle_is_refused() -> None:
    declared = {phase: set(values) for phase, values in EXPECTED_MAP.items()}
    declared[ControlPhase.REQUESTED].add(ControlDisposition.IDLE)

    with pytest.raises(ValueError, match="admits projected disposition idle"):
        compile_phase_dispositions(declared)


def test_a_disposition_reachable_from_two_phases_is_refused() -> None:
    declared = {phase: set(values) for phase, values in EXPECTED_MAP.items()}
    declared[ControlPhase.EFFECTED].add(ControlDisposition.ACCEPTED)

    with pytest.raises(ValueError, match="reachable from both"):
        compile_phase_dispositions(declared)


def test_a_disposition_reachable_from_no_phase_is_refused() -> None:
    declared = {phase: set(values) for phase, values in EXPECTED_MAP.items()}
    declared[ControlPhase.EFFECTED].remove(ControlDisposition.RECOVERY)

    with pytest.raises(ValueError, match="no phase reaches disposition recovery"):
        compile_phase_dispositions(declared)


def test_terminal_effects_is_total_over_every_control_kind() -> None:
    assert set(TERMINAL_EFFECT_STATUS) == set(ControlKind)


def test_cancel_and_interrupt_are_the_controls_that_end_a_run() -> None:
    ending = {kind for kind, status in TERMINAL_EFFECT_STATUS.items() if status is not None}

    assert ending == {ControlKind.CANCEL, ControlKind.INTERRUPT}
    assert TERMINAL_EFFECT_STATUS[ControlKind.CANCEL] is RunStatus.CANCELLED


def test_compiling_an_empty_terminal_effect_table_is_refused() -> None:
    with pytest.raises(ValueError, match="no terminal effect declared for control"):
        compile_terminal_effects({})


def test_compiling_terminal_effects_without_every_control_is_refused() -> None:
    with pytest.raises(ValueError, match="no terminal effect declared for control"):
        compile_terminal_effects({ControlKind.CANCEL: RunStatus.CANCELLED})


def test_compiling_terminal_effects_onto_a_running_status_is_refused() -> None:
    declared: dict[ControlKind, RunStatus | None] = dict.fromkeys(ControlKind)
    declared[ControlKind.CANCEL] = RunStatus.RUNNING

    with pytest.raises(ValueError, match="which is not terminal"):
        compile_terminal_effects(declared)
