"""Shared building blocks for PhaseSpec / IterSpec / WaveSpec models.

These types are imported by :mod:`eawf.kernel.spec.phase`, :mod:`eawf.kernel.spec.iter`,
:mod:`eawf.kernel.spec.wave` and (later) the research / hypothesis / decision /
audit spec modules. Defining them once here avoids drift between the
seven spec models and keeps the cross-spec citation contract (verdict
ids, brief paths, test refs, file scopes, evidence refs) in a single
authoritative place.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class _StrictModel(BaseModel):
    """Base model that forbids unknown keys.

    Per AGENTS rule 2 every YAML/JSON ingestion model uses
    ``ConfigDict(extra="forbid")`` so unrecognised keys fail at load
    time rather than silently drifting through the pipeline.
    """

    model_config = ConfigDict(extra="forbid")


# Verdict citation id pattern: V12, V12-RC3, D17, R5, H03-12.
#
#   V = verdict (cluster brief or earlier research)
#   D = decision (operator-ratified D# in a brief's §4 matrix)
#   R = recommendation (long-term-features long-term R# in §"Final picks")
#   H = hypothesis id (per-state H<NN>-<NN>)
VerdictIdStr = Annotated[
    str,
    Field(pattern=r"^[VDRH]\d+(-[A-Z0-9]+)?$"),
]


# Repo-relative path beneath .ea/local/research/ OR .ea/artifacts/research/.
# Local drafts and promoted artifacts are both addressable so cluster briefs
# can cite each other across the local-draft / promoted-artifact boundary.
BriefPathStr = Annotated[
    str,
    Field(
        min_length=1,
        pattern=r"^\.ea/(local|artifacts)/research/.+\.md$",
    ),
]


# Repo-relative path under tests/. Extension is intentionally loose so the
# same TestRef can point at .py, .svg snapshots, .json fixtures, .md golden
# files, .txt diff baselines, asciinema casts, or future test artefacts.
TestRef = Annotated[
    str,
    Field(
        min_length=1,
        pattern=r"^tests/.+$",
    ),
]


# Repo-relative path under src/, tools/, .ea/, docs/, build/, or tests/.
# The union covers every directory a spec's file_scope can legitimately
# touch; paths outside it indicate the spec is over-reaching its
# project-tree boundary.
FileScopeRef = Annotated[
    str,
    Field(
        min_length=1,
        pattern=r"^(src|tools|\.ea|docs|build|tests)/.+$",
    ),
]


class VerdictCitation(_StrictModel):
    """One verdict citation tying a spec back to a research brief.

    Used by PhaseSpec / IterSpec / WaveSpec / DecisionSpec to record
    which prior verdict, decision, recommendation, or hypothesis the
    spec implements. The pair ``(verdict_id, brief)`` is the addressable
    unit; ``line`` and ``note`` are optional annotations.
    """

    verdict_id: VerdictIdStr
    brief: BriefPathStr
    line: int | None = Field(default=None, ge=1)
    note: str | None = None


# Canonical evidence-reference vocabulary shared across the spec layer
# (HypothesisSpec.evidence_chain, DecisionSpec citations, EvidenceRecord
# refs) AND the agent-report layer (AgentReportEvidenceRef). Defining it
# once here means downstream models import the same Literal — the
# agent-report kind set is a strict subset (equal) by construction
# rather than by convention.
#
#   "audit"        -> audit URN
#   "artifact"     -> artifact id / URN
#   "decision"     -> decision URN (urn:eawf:v1:decision:OWNER/ID)
#   "store_record" -> store-record URN
#   "external_url" -> external http(s) URL
EvidenceKind = Literal[
    "audit",
    "artifact",
    "decision",
    "store_record",
    "external_url",
]


class EvidenceRef(_StrictModel):
    """One row of a HypothesisSpec.evidence_chain.

    Slim by design: the audit-DSL runner walks evidence chains looking
    for a typed reference (audit URN, artifact id, decision URN, store
    record URN, or external URL) plus a short summary. The full
    evidence document lives behind the ``ref``, not inlined here.
    """

    kind: EvidenceKind
    ref: str
    summary: str = Field(min_length=1, max_length=400)
