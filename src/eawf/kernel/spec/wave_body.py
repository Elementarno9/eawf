"""Wave-spec body schema — the typed parse-target for criteria + gates.

A per-wave deliverable spec lives at ``.ea/specs/<wave-id>.md``. Its
markdown body carries a single fenced, structured block that encodes the
wave's success criteria and the gates that score them. This module owns
the typed parse-target that block deserialises into; it does NOT parse
the surrounding markdown (the extractor that pulls the fenced block out
of the document is wave W04) nor materialise the parsed rows into
``state.json`` (the ``eawf spec sync`` command is wave W05). The single
responsibility here is the strict Pydantic model the structured block
must validate against — so a malformed criteria/gate row fails at the
ingestion boundary rather than silently drifting into state.

Body encoding (the canonical format W04 extracts and W05 round-trips)
---------------------------------------------------------------------

The criteria block and the gate block are encoded as one fenced YAML
mapping inside the markdown body, fenced as ```` ```eawf-wave-body ````
so the extractor can locate it unambiguously. The mapping has exactly
two top-level keys — ``criteria`` (a list of :class:`CriterionSpec`
rows) and ``gates`` (a list of :class:`GateSpec` rows). YAML is chosen
over a bespoke table grammar because the criterion + gate models already
have nested fields (``response`` clauses, gate ``args`` maps) that a flat
table cannot express, and because a YAML mapping deserialises straight
into the existing Pydantic models with no intermediate hand-rolled
parser. Example:

.. code-block:: yaml

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

The model is strict (``ConfigDict(extra="forbid")``): an unknown
top-level key, or an unknown field on any criterion / gate row, raises
:class:`pydantic.ValidationError`. The per-row floors carry through —
e.g. a criterion whose ``measurable_signal`` is missing or under the
20-character floor fails because :class:`CriterionSpec` enforces it.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import Field, model_validator

from eawf.kernel.spec.common import CriterionSpec, GateSpec, _StrictModel

logger = logging.getLogger(__name__)

#: The fenced-code-block info string the markdown extractor keys
#: on to locate the structured wave-body block. Lives here so the schema
#: and the (later) extractor agree on one canonical fence label.
WAVE_BODY_FENCE = "eawf-wave-body"


class WaveSpecBody(_StrictModel):
    """Typed parse-target for a wave-spec markdown body's structured block.

    Wraps the wave's authored success criteria and the gates that score
    them into one strict document. The two lists reuse the existing
    :class:`~eawf.kernel.spec.common.CriterionSpec` and
    :class:`~eawf.kernel.spec.common.GateSpec` models, so every per-row
    invariant (the ``measurable_signal`` 20-300 char floor, the gate
    argv L0 policy, the strict ``extra="forbid"`` config) is enforced
    here for free at the ingestion boundary.

    Referential integrity between the two lists — every
    ``gate.criterion_id`` naming a present criterion and every
    ``criterion.gate_ids`` entry naming a present gate — is checked by
    the :meth:`_gate_criterion_refs_resolve` validator so an authoring
    typo (a gate pointing at a deleted criterion) fails the parse rather
    than the close gate.

    ``criteria`` may be empty: an advisory backend wave can carry no
    typed criteria, and an empty block is a valid (if uninteresting)
    document. ``gates`` may likewise be empty (an attested criterion
    needs no deterministic gate).
    """

    criteria: list[CriterionSpec] = Field(default_factory=list)
    gates: list[GateSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _gate_criterion_refs_resolve(self) -> WaveSpecBody:
        """Enforce cross-list referential integrity of criteria and gates.

        Every ``gate.criterion_id`` must name a criterion present in
        ``criteria``, and every id listed in a criterion's ``gate_ids``
        must name a gate present in ``gates``. Catching a dangling
        reference at parse time keeps a stale id from reaching the close
        gate, where it would silently degrade to the jury tier.

        Raises:
            ValueError: when a gate references an absent criterion id, or
                a criterion's ``gate_ids`` references an absent gate id.
        """
        criterion_ids = {c.id for c in self.criteria}
        gate_ids = {g.id for g in self.gates}

        for gate in self.gates:
            if gate.criterion_id not in criterion_ids:
                raise ValueError(
                    f"gate {gate.id!r} references unknown criterion: "
                    f"criterion_id={gate.criterion_id!r}"
                )
        for criterion in self.criteria:
            for ref in criterion.gate_ids:
                if ref not in gate_ids:
                    raise ValueError(
                        f"criterion {criterion.id!r} references unknown gate: gate_id={ref!r}"
                    )
        return self

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> WaveSpecBody:
        """Build a :class:`WaveSpecBody` from the structured-block mapping.

        The thin convenience loader the markdown extractor and the
        ``eawf spec sync`` command call once they have the parsed
        YAML mapping in hand. It is a direct ``model_validate`` pass — the
        method exists so callers depend on a named, documented entry
        point rather than reaching for ``model_validate`` directly, and so
        the strict-validation contract has one obvious home.

        Args:
            data: The deserialised structured-block mapping, with
                ``criteria`` and ``gates`` keys.

        Returns:
            The validated wave-spec body.

        Raises:
            pydantic.ValidationError: when *data* carries an unknown key,
                a malformed criterion / gate row, or a cross-reference to
                an absent criterion / gate id.
        """
        logger.debug(
            f"from_mapping criteria={len(data.get('criteria', []))} "
            f"gates={len(data.get('gates', []))}"
        )
        return cls.model_validate(data)
