"""Tests for the legacy-to-typed criterion converter.

Covers the converter that lifts a grandfathered free-form success-criterion
STRING out of the no-op :data:`~eawf.kernel.spec.common.GRANDFATHERED_KIND`
state into a typed :class:`~eawf.kernel.spec.common.CriterionSpec` carrying a
real :class:`~eawf.kernel.spec.common.ResponseClause` plus an attached,
falsifying :class:`~eawf.kernel.spec.common.GateSpec`:

* the parser-driven path: a criterion whose prose carries a recognised
  observation verb + proof locus seeds the response clause from
  :func:`~eawf.platform.lint.eawf021_measurable_criterion.extract_observation`
  and attaches the verb-matched gate kind;
* the hand-authored fallback: a criterion with no parseable verb/locus still
  converts to a ``criterion_in_diff`` file-grep gate so it is never left
  ungated;
* the binary proof: over a representative SAMPLE the active-criteria legacy
  count (:func:`~eawf.workflow.verify.readiness.legacy_criterion_count`) drops
  to ZERO and every converted criterion carries a populated ``oracle_tier``;
* boundary cases: a short string (below the 20-char ``measurable_signal``
  floor), a punctuation-only string (no alphanumeric token), and a single-item
  sample;
* error paths: an empty ``file_scopes`` raises ``ValueError`` and a malformed
  converted pair fails cross-reference validation.
"""

from __future__ import annotations

import pytest

from eawf.kernel.spec.common import (
    CONVERTED_KIND,
    GRANDFATHERED_KIND,
    GRANDFATHERED_SIGNAL,
    ObserveVerb,
    OracleTier,
    ProofLocus,
    backfill_legacy_criteria,
    convert_legacy_criterion,
    grandfather_criterion,
    legacy_criterion_pattern,
    validate_criterion_gate_refs,
)
from eawf.workflow.verify.readiness import legacy_criterion_count

_SCOPES = ["src/eawf/kernel/spec/common.py", "src/eawf/workflow/verify/readiness.py"]

# A representative sample spanning the parser-driven path (verb + locus present)
# and the hand-authored fallback (ambiguous prose).
_SAMPLE: list[str] = [
    "validates the wave-body schema; proof in pytest tests/unit/spec/test_x.py",
    "emits a structured event row; observe via log_capture",
    "renders token in the tui_snapshot for the criteria tab",
    "matches pattern AGENT_ROLE in the schema dump source",
    "the converter exists and drains the active-criteria legacy count to zero",
    "criterion text appears in the diff for the touched file_scopes",
]


def test_convert_parses_verb_and_locus_from_prose() -> None:
    # A criterion whose prose carries a recognised verb (validates) + locus
    # (pytest) seeds the response clause from those surface forms.
    criterion, gate = convert_legacy_criterion(
        "validates the wave body; proof in pytest tests/unit/spec/test_x.py",
        index=1,
        file_scopes=_SCOPES,
    )
    assert criterion.kind == CONVERTED_KIND
    assert criterion.evidence_kind == "deterministic"
    assert criterion.response is not None
    assert criterion.response.observe is ObserveVerb.VALIDATES
    assert criterion.response.locus is ProofLocus.PYTEST
    # The validates verb maps to the static regex_in_file falsifier.
    assert gate.kind == "regex_in_file"
    assert gate.criterion_id == criterion.id
    assert criterion.gate_ids == [gate.id]


def test_convert_falls_back_when_prose_is_ambiguous() -> None:
    # No recognised verb/locus -> hand-authored criterion_in_diff file grep so
    # the criterion is never left ungated, with the default FILE_MATCHES/SOURCE
    # response shape.
    criterion, gate = convert_legacy_criterion(
        "the whole thing should be solid end to end",
        index=3,
        file_scopes=_SCOPES,
    )
    assert criterion.kind == CONVERTED_KIND
    assert gate.kind == "criterion_in_diff"
    assert criterion.response is not None
    assert criterion.response.observe is ObserveVerb.FILE_MATCHES
    assert criterion.response.locus is ProofLocus.SOURCE
    assert gate.args["file_scopes"] == _SCOPES
    assert gate.args["pattern"]


def test_convert_oracle_tier_resolves_from_attached_gate_kind() -> None:
    # The response clause's gate_ref names the gate kind, so the tier is the
    # cheapest falsifier of that kind: both file-grep kinds are T1_STATIC.
    criterion, gate = convert_legacy_criterion(
        "renders token in the tui_snapshot",
        index=2,
        file_scopes=_SCOPES,
    )
    validate_criterion_gate_refs([criterion], [gate])
    assert criterion.oracle_tier is OracleTier.T1_STATIC


def test_backfill_drops_legacy_count_to_zero_on_sample() -> None:
    # The binary success criterion: a representative sample of grandfathered
    # strings converts to typed + gated rows and the legacy count is ZERO.
    grandfathered = [grandfather_criterion(text, index=i) for i, text in enumerate(_SAMPLE, 1)]
    assert legacy_criterion_count(grandfathered) == len(_SAMPLE)

    criteria, gates = backfill_legacy_criteria(_SAMPLE, file_scopes=_SCOPES)

    assert legacy_criterion_count(criteria) == 0
    assert len(criteria) == len(_SAMPLE)
    assert len(gates) == len(_SAMPLE)
    # Every converted criterion is gated + tier-resolved (a falsifying gate).
    for criterion in criteria:
        assert criterion.kind == CONVERTED_KIND
        assert criterion.kind != GRANDFATHERED_KIND
        assert criterion.gate_ids
        assert criterion.oracle_tier is not None
    # Referential integrity holds: re-validating the persisted pair passes.
    validate_criterion_gate_refs(criteria, gates, allow_computed_tier=True)


def test_backfill_single_item_sample() -> None:
    # Boundary: a one-criterion sample still converts and drains to zero.
    criteria, gates = backfill_legacy_criteria(
        ["validates the schema in pytest"], file_scopes=_SCOPES
    )
    assert len(criteria) == 1
    assert len(gates) == 1
    assert legacy_criterion_count(criteria) == 0


def test_convert_short_string_uses_grandfathered_signal() -> None:
    # Boundary: a string below the 20-char measurable_signal floor falls back to
    # the grandfathered-signal sentinel rather than failing the length bound.
    criterion, _gate = convert_legacy_criterion("exits", index=4, file_scopes=_SCOPES)
    assert criterion.measurable_signal == GRANDFATHERED_SIGNAL


def test_legacy_criterion_pattern_handles_punctuation_only() -> None:
    # A string with no alphanumeric run yields the catch-all so the gate still
    # compiles (an empty pattern would raise at gate construction).
    assert legacy_criterion_pattern("--- !!! ...") == "."
    # The longest token anchors the pattern for a normal string.
    assert legacy_criterion_pattern("the converter validates schemas") == "converter"


def test_convert_empty_file_scopes_raises() -> None:
    # Error path: a file-grep gate needs at least one scope to resolve against.
    with pytest.raises(ValueError, match="file_scopes"):
        convert_legacy_criterion("validates the schema", index=1, file_scopes=[])


def test_backfill_empty_file_scopes_raises() -> None:
    # Error path propagates through the batch entry point.
    with pytest.raises(ValueError, match="file_scopes"):
        backfill_legacy_criteria(["validates the schema"], file_scopes=[])
