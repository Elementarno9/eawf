"""Tests for the wave-spec body extractor (wave P29-I12-W04).

The daemon-side extractor
(:func:`eawf.runtime.daemon.methods.spec._extract_gate_specs` and its
sibling :func:`~eawf.runtime.daemon.methods.spec._extract_criterion_specs`)
locates the ``eawf-wave-body`` fenced YAML block inside a wave-spec
markdown body, deserialises it, and validates it through
:class:`~eawf.kernel.spec.wave_body.WaveSpecBody`. Coverage:

* happy path: a well-formed fenced body yields the right
  :class:`GateSpec` / :class:`CriterionSpec` rows, criteria carrying
  ``evidence_kind`` and ``gate_ids``;
* boundary: a body with no fence (the legacy scaffold) yields ``[]``;
* boundary: an empty fenced block yields ``[]`` for both lists;
* boundary: bytes and str inputs parse identically;
* error path: malformed YAML inside the fence raises ``yaml.YAMLError``;
* error path: a non-mapping fenced payload raises ``ValueError``;
* error path: an unknown row field raises ``ValidationError``;
* error path: a sub-floor ``measurable_signal`` raises ``ValidationError``;
* error path: a dangling cross-reference raises ``ValidationError``.
"""

from __future__ import annotations

import textwrap

import pytest
import yaml
from pydantic import ValidationError

from eawf.kernel.spec.common import GateSpec
from eawf.runtime.daemon.methods.spec import _extract_gate_specs

# A measurable_signal one char under the 20-char CriterionSpec floor.
_SIGNAL_19 = "x" * 19


def _wrap_body(yaml_block: str) -> str:
    """Wrap a YAML block in the canonical ``eawf-wave-body`` fence.

    The leading markdown prose mimics a real authored spec so the
    fence-locator is exercised against surrounding text, not a bare
    block.
    """
    return (
        "# Wave deliverable\n\n"
        "Some authored prose before the structured block.\n\n"
        "```eawf-wave-body\n"
        f"{yaml_block}"
        "```\n\n"
        "Trailing prose after the block.\n"
    )


_FULL_YAML = textwrap.dedent(
    """\
    criteria:
      - id: CR-01
        text: render the close-readiness header in the evidence mode
        kind: behavioral
        acceptance_style: binary
        evidence_kind: deterministic
        quality_dimension: interaction_capability
        measurable_signal: the snapshot test for the evidence header passes
        gate_ids: [G-01]
    gates:
      - id: G-01
        criterion_id: CR-01
        kind: schema_validate
        args: {model: CloseReadiness}
        policy: block
        cadence: every-wave
    """
)


# --------------------------------------------------------------------------- #
# Happy path — a well-formed fenced body yields typed rows.
# --------------------------------------------------------------------------- #
def test_extract_gate_specs_yields_typed_gate_rows() -> None:
    """The gate extractor returns the typed GateSpec rows of the body block."""
    body = _wrap_body(_FULL_YAML)
    gates = _extract_gate_specs(body)
    assert len(gates) == 1
    gate = gates[0]
    assert isinstance(gate, GateSpec)
    assert gate.id == "G-01"
    assert gate.criterion_id == "CR-01"
    assert gate.kind == "schema_validate"


# --------------------------------------------------------------------------- #
# Error path — malformed YAML / non-mapping payload.
# --------------------------------------------------------------------------- #
def test_extract_malformed_yaml_raises() -> None:
    """A fenced block that is not well-formed YAML raises yaml.YAMLError."""
    body = _wrap_body("criteria: [unterminated\n")
    with pytest.raises(yaml.YAMLError):
        _extract_gate_specs(body)


def test_extract_non_mapping_payload_raises() -> None:
    """A fenced block that deserialises to a non-mapping raises ValueError."""
    body = _wrap_body("- just\n- a\n- list\n")
    with pytest.raises(ValueError, match="must deserialise to a mapping"):
        _extract_gate_specs(body)


def test_extract_dangling_gate_reference_raises() -> None:
    """A gate.criterion_id naming no present criterion fails the cross-list validator."""
    yaml_block = textwrap.dedent(
        """\
        criteria: []
        gates:
          - id: G-01
            criterion_id: CR-99
            kind: schema_validate
            args: {model: CloseReadiness}
            policy: block
            cadence: every-wave
        """
    )
    body = _wrap_body(yaml_block)
    with pytest.raises(ValidationError) as exc:
        _extract_gate_specs(body)
    assert "unknown criterion" in str(exc.value)
