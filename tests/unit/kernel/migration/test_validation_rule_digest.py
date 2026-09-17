"""The DOM-004 totality digest covers the validation tables.

The validation tables decide whether a staged row is entailed by the
source: which fields are canonical references, which lifecycle target
becomes which entity kind, and which fabrication reasons exist. Editing
one of them changes what a total import means, so the published rule
digest has to move with it. These tests drive the registry payload
against a patched table and assert the digest actually follows -- a
payload that merely *mentions* the validation module would not.
"""

from __future__ import annotations

import pytest

from eawf.kernel.migration.epoch2 import validation
from eawf.kernel.migration.epoch2.registry import TOTALITY_RULE, totality_rule_payload
from eawf.kernel.migration.epoch2.rules import rule_digest
from eawf.kernel.migration.epoch2.validation import validation_rule_payload
from eawf.kernel.state.epoch2.values import EntityKind

pytestmark = pytest.mark.unit


def test_totality_payload_carries_the_validation_tables() -> None:
    """The manifest payload publishes the validation tables verbatim."""
    assert totality_rule_payload()["validation"] == validation_rule_payload()


def test_totality_rule_digest_is_taken_over_the_live_payload() -> None:
    """The published DOM-004 row digests exactly what the payload returns."""
    assert TOTALITY_RULE.rule_digest == rule_digest(totality_rule_payload())
    assert TOTALITY_RULE.rule_id == "DOM-004"


def test_editing_a_reference_table_moves_the_totality_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A canonical-reference edit reaches the digest through the registry."""
    before = rule_digest(totality_rule_payload())
    patched = {**validation.CANONICAL_REFERENCE_KINDS, "invented_ref": EntityKind.TASK}
    monkeypatch.setattr(validation, "CANONICAL_REFERENCE_KINDS", patched)

    assert rule_digest(totality_rule_payload()) != before


def test_editing_a_lifecycle_entity_table_moves_the_totality_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second validation table is wired through the same path."""
    before = rule_digest(totality_rule_payload())
    thinned = dict(validation.LIFECYCLE_ENTITY_KINDS)
    thinned.pop(next(iter(thinned)))
    monkeypatch.setattr(validation, "LIFECYCLE_ENTITY_KINDS", thinned)

    assert rule_digest(totality_rule_payload()) != before


def test_an_untouched_tree_digests_identically_across_calls() -> None:
    """The payload is deterministic, so the digest is reproducible."""
    assert rule_digest(totality_rule_payload()) == rule_digest(totality_rule_payload())


def test_validation_payload_names_every_table_the_digest_pins() -> None:
    """The keys are the contract: a dropped table would silently stop pinning."""
    assert set(validation_rule_payload()) == {
        "canonical_references",
        "measurement_reference",
        "legacy_reference_populations",
        "lifecycle_entity_kinds",
        "run_source_kinds",
        "fabrication_reasons",
    }


def test_empty_reference_table_still_digests(monkeypatch: pytest.MonkeyPatch) -> None:
    """The empty boundary is a table revision like any other, not an error."""
    monkeypatch.setattr(validation, "CANONICAL_REFERENCE_KINDS", {})

    payload = totality_rule_payload()

    assert payload["validation"]["canonical_references"] == {}
    assert len(rule_digest(payload)) == 64


def test_an_unserializable_table_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """A table the digest cannot encode raises rather than digesting a repr."""
    monkeypatch.setattr(validation, "LEGACY_REFERENCE_POPULATIONS", {"bad": object()})

    with pytest.raises(TypeError):
        rule_digest(totality_rule_payload())
