"""The six lifecycle diagrams and the registry say exactly the same thing.

A diagram is what a reader reviews and the registry is what runs, so the
two drifting apart is the failure that matters: an edge added in code and
not in the picture ships unreviewed, and an edge drawn but never
registered promises a move the machine will refuse. Parity is therefore
checked in both directions, over states as well as edges, so a terminal
state and a state with no edges yet are both visible rather than implied
by their absence.

The comparison reads the checked-in diagram with an independent parser
rather than with the renderer that wrote it. Regenerate the fixtures with
``render_state_diagram`` when the registry legitimately changes; the diff
in the ``.mmd`` file is the review the gate exists to force.
"""

from __future__ import annotations

import pytest

from eawf.kernel.spec.release import ReleaseStatus
from eawf.kernel.state.epoch2.transitions import (
    DENIAL_REMEDIATION,
    ENTITY_STATUS_ENUM,
    GUARD_DENIALS,
    TERMINAL_STATUSES,
    TRANSITION_ROWS,
    DenialCode,
    LifecycleEntity,
    TransitionGuard,
    TransitionRow,
    TransitionVerb,
    index_rows,
    render_guards,
    render_state_diagram,
    rows_from,
)
from eawf.workflow.release.lifecycle import (
    RELEASE_DENIALS,
    RELEASE_TRANSITIONS,
    ReleaseGuardName,
)
from tests.unit.kernel.state._epoch2_transition_fixtures import (
    DiagramParseError,
    diagram_path,
    parse_state_diagram,
)

pytestmark = pytest.mark.unit

ENTITIES = list(LifecycleEntity)


def _registry_edges(entity: LifecycleEntity) -> set[tuple[str, str, str, tuple[str, ...]]]:
    """Return every registered edge of *entity* in the parser's shape."""
    return {
        (
            str(row.frm),
            str(row.to),
            row.verb.value,
            tuple(guard.value for guard in row.guards),
        )
        for row in TRANSITION_ROWS
        if row.entity is entity
    }


def _rendered_edges(entity: LifecycleEntity) -> set[tuple[str, str, str, tuple[str, ...]]]:
    """Return every edge the checked-in diagram of *entity* draws."""
    _states, edges = parse_state_diagram(diagram_path(entity).read_text(encoding="utf-8"))
    return set(edges)


# ---- edge parity, both directions -------------------------------------------


@pytest.mark.parametrize("entity", ENTITIES, ids=lambda item: item.value)
def test_every_rendered_edge_is_a_registry_edge(entity: LifecycleEntity) -> None:
    assert _rendered_edges(entity) - _registry_edges(entity) == set()


@pytest.mark.parametrize("entity", ENTITIES, ids=lambda item: item.value)
def test_every_registry_edge_is_rendered(entity: LifecycleEntity) -> None:
    assert _registry_edges(entity) - _rendered_edges(entity) == set()


@pytest.mark.parametrize("entity", ENTITIES, ids=lambda item: item.value)
def test_every_status_is_declared_including_terminal_and_empty_rows(
    entity: LifecycleEntity,
) -> None:
    states, _edges = parse_state_diagram(diagram_path(entity).read_text(encoding="utf-8"))
    assert list(states) == [str(status) for status in ENTITY_STATUS_ENUM[entity]]


@pytest.mark.parametrize("entity", ENTITIES, ids=lambda item: item.value)
def test_terminal_states_draw_no_outgoing_edge(entity: LifecycleEntity) -> None:
    drawn_sources = {frm for frm, _to, _verb, _guards in _rendered_edges(entity)}
    assert drawn_sources & TERMINAL_STATUSES[entity] == set()


@pytest.mark.parametrize("entity", ENTITIES, ids=lambda item: item.value)
def test_checked_in_diagram_matches_the_renderer(entity: LifecycleEntity) -> None:
    assert diagram_path(entity).read_text(encoding="utf-8") == render_state_diagram(entity)


def test_track_renders_one_edge_and_one_terminal_state() -> None:
    states, edges = parse_state_diagram(
        diagram_path(LifecycleEntity.TRACK).read_text(encoding="utf-8")
    )
    assert states == ("ACTIVE", "RETIRED")
    assert edges == (("ACTIVE", "RETIRED", "retired", ("no_open_milestones",)),)


# ---- the gate reds on a real defect -----------------------------------------


def test_parity_reds_when_a_registry_edge_is_dropped() -> None:
    rendered = _rendered_edges(LifecycleEntity.DELIVERY_BATCH)
    thinned = {
        edge for edge in _registry_edges(LifecycleEntity.DELIVERY_BATCH) if edge[1] != "MERGING"
    }
    assert rendered - thinned != set()


def test_parity_reds_when_the_diagram_gains_an_unregistered_edge() -> None:
    source = diagram_path(LifecycleEntity.DELIVERY_BATCH).read_text(encoding="utf-8")
    tampered = source + "    MERGING --> COMPLETED: completed [none]\n"
    _states, edges = parse_state_diagram(tampered)
    assert set(edges) - _registry_edges(LifecycleEntity.DELIVERY_BATCH) == {
        ("MERGING", "COMPLETED", "completed", ())
    }


def test_parity_reds_when_a_guard_is_dropped_from_a_drawn_edge() -> None:
    source = diagram_path(LifecycleEntity.TRACK).read_text(encoding="utf-8")
    tampered = source.replace("[no_open_milestones]", "[none]")
    _states, edges = parse_state_diagram(tampered)
    assert set(edges) - _registry_edges(LifecycleEntity.TRACK) == {
        ("ACTIVE", "RETIRED", "retired", ())
    }


# ---- the registry's own invariants ------------------------------------------


def test_every_guard_declares_a_denial_code() -> None:
    assert set(GUARD_DENIALS) == set(TransitionGuard)


def test_every_denial_code_declares_a_remediation() -> None:
    assert set(DENIAL_REMEDIATION) == set(DenialCode)
    assert all(text.endswith(".") for text in DENIAL_REMEDIATION.values())


def test_every_denial_code_is_reachable_or_structural() -> None:
    structural = {
        DenialCode.ILLEGAL_TRANSITION,
        DenialCode.TERMINAL_STATE,
        DenialCode.LEGACY_ORIGIN_IMMUTABLE,
        DenialCode.MISSING_TRANSITION_FIELDS,
    }
    assert set(GUARD_DENIALS.values()) | structural == set(DenialCode)


def test_every_verb_is_used_by_at_least_one_row() -> None:
    assert {row.verb for row in TRANSITION_ROWS} == set(TransitionVerb)


def test_every_status_is_a_source_or_terminal() -> None:
    for entity, status_enum in ENTITY_STATUS_ENUM.items():
        for status in status_enum:
            has_edges = bool(rows_from(entity, status))
            assert has_edges != (str(status) in TERMINAL_STATUSES[entity])


def test_index_rows_refuses_a_duplicate_edge() -> None:
    duplicate = tuple(row for row in TRANSITION_ROWS if row.entity is LifecycleEntity.TRACK) * 2
    with pytest.raises(ValueError, match="duplicate transition row"):
        index_rows(duplicate)


def test_index_rows_accepts_an_empty_registry() -> None:
    assert index_rows(()) == {}


def test_render_guards_names_an_unguarded_edge() -> None:
    assert render_guards(()) == "none"
    assert render_guards((TransitionGuard.LEASE_HELD,)) == "lease_held"


# ---- the Release rows restate the release machine ---------------------------


def test_release_rows_match_the_release_transition_table() -> None:
    registered = {
        (str(row.frm), str(row.to))
        for row in TRANSITION_ROWS
        if row.entity is LifecycleEntity.RELEASE
    }
    machine = {
        (str(frm), str(to)) for frm, edges in RELEASE_TRANSITIONS.items() for to, _guard in edges
    }
    assert registered == machine


def test_release_row_guards_match_the_release_machine_guards() -> None:
    for row in TRANSITION_ROWS:
        if row.entity is not LifecycleEntity.RELEASE:
            continue
        frm = ReleaseStatus(str(row.frm))
        machine_guards = {
            guard.value
            for target, guard in RELEASE_TRANSITIONS[frm]
            if str(target) == str(row.to) and guard is not ReleaseGuardName.NONE
        }
        assert {guard.value for guard in row.guards} == machine_guards


def test_release_row_denials_match_the_release_machine_denials() -> None:
    for row in TRANSITION_ROWS:
        if row.entity is not LifecycleEntity.RELEASE or not row.guards:
            continue
        edge = (ReleaseStatus(str(row.frm)), ReleaseStatus(str(row.to)))
        assert {GUARD_DENIALS[guard].value for guard in row.guards} == {RELEASE_DENIALS[edge].value}


# ---- the diagram parser refuses a malformed fixture -------------------------


def test_parser_refuses_content_before_the_header() -> None:
    with pytest.raises(DiagramParseError, match="content before the stateDiagram-v2 header"):
        parse_state_diagram("ACTIVE\nstateDiagram-v2\n")


def test_parser_refuses_a_diagram_with_no_header() -> None:
    with pytest.raises(DiagramParseError, match="declares no stateDiagram-v2 header"):
        parse_state_diagram("%% only a comment\n")


def test_parser_refuses_an_edge_without_a_verb_label() -> None:
    with pytest.raises(DiagramParseError, match="no verb label"):
        parse_state_diagram("stateDiagram-v2\n    A\n    B\n    A --> B\n")


def test_parser_refuses_an_edge_without_a_guard_bracket() -> None:
    with pytest.raises(DiagramParseError, match="no guard bracket"):
        parse_state_diagram("stateDiagram-v2\n    A\n    B\n    A --> B: retired\n")


def test_parser_refuses_an_edge_naming_an_undeclared_state() -> None:
    with pytest.raises(DiagramParseError, match="undeclared states"):
        parse_state_diagram("stateDiagram-v2\n    A\n    A --> B: retired [none]\n")


def test_parser_refuses_one_edge_drawn_twice() -> None:
    with pytest.raises(DiagramParseError, match="one edge twice"):
        parse_state_diagram(
            "stateDiagram-v2\n    A\n    B\n"
            "    A --> B: retired [none]\n    A --> B: retired [none]\n"
        )


def test_parser_accepts_a_diagram_with_no_edges() -> None:
    assert parse_state_diagram("stateDiagram-v2\n    A\n") == (("A",), ())


def test_registry_rows_are_frozen() -> None:
    row = TRANSITION_ROWS[0]
    assert isinstance(row, TransitionRow)
    with pytest.raises(AttributeError):
        row.verb = TransitionVerb.CANCELLED  # type: ignore[misc]
