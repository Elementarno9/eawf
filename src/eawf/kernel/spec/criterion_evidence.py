"""Criterion verification flavor -- how a CriterionSpec is checked.

Distinct from :data:`~eawf.kernel.spec.common.EvidenceKind` (which
classifies *what* a reference points at), this vocabulary classifies
*how* a criterion's evidence is gathered when the readiness compute, the
compile-gate and the durable review gate score it. It is re-exported from
:mod:`eawf.kernel.spec.common`, the home of the models that carry it.
"""

from __future__ import annotations

from typing import Final, Literal

#   "deterministic" -> an automated check (test exit code, regex match,
#                      schema validation) that produces a bit answer.
#   "jury"          -> a vote of multiple agent reviewers; the
#                      minority-veto policy lives in the gate machinery.
#   "attested"      -> a human operator signs off; the attestation is
#                      stored as a typed Decision row.
#   "rendered_run"  -> a claim about what a reader of a rendered artifact
#                      sees; its gates run like deterministic ones, but a
#                      static source scan cannot back it, and a reviewer
#                      must cite the rendered run's receipt rather than a
#                      source reading.
CriterionEvidenceKind = Literal[
    "deterministic",
    "jury",
    "attested",
    "rendered_run",
]

#: The evidence kinds whose criterion is discharged by running its gates.
GATE_RUN_EVIDENCE_KINDS: Final[frozenset[str]] = frozenset({"deterministic", "rendered_run"})
