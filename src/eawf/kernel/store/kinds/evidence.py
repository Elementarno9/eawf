"""EvidenceRecord — payload model for StoreKind.EVIDENCE records.

EvidenceRecord is the v0.4 verify-spine artefact: every deterministic
gate, jury-graded check, or operator attestation appends one row that
later flows (close-readiness, compile-gate, waivers) read. The store is
a non-state JSONL append alongside ``event.jsonl`` and ``audit.jsonl`` —
no ``MutationKind`` is allocated for evidence, so state.json stays
derivable from the typed Mutation log alone.

The :func:`mint_evidence_id` helper produces ids of the form
``EV-<12 hex>`` so the id collision space tracks ``EV-`` namespace
length, mirroring the existing 4-byte suffix conventions in
:mod:`eawf.kernel.state.io` and :mod:`eawf.kernel.state.writer` doubled to
12 hex chars per the W04 contract.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import ConfigDict, Field

from eawf.kernel.spec.common import _StrictModel
from eawf.kernel.state.models import IdStr
from eawf.kernel.state.types import UtcDatetime

#: Closed literal for who produced the evidence row.
ProducedBy = Literal["human", "agent", "tool", "canary"]

#: Closed literal for the evidence-source family — ``deterministic`` for
#: a code-gated check (pytest, mypy, ruff), ``jury`` for an LLM-graded
#: attestation, ``attested`` for an operator sign-off. W03 may later
#: alias these via a typed enum; until then the bare Literal is the
#: source of truth.
EvidenceSourceKind = Literal["deterministic", "jury", "attested"]

#: Closed literal for the binary evidence outcome.
EvidenceStatus = Literal["pass", "fail", "blocked", "waived"]


def mint_evidence_id() -> str:
    """Return a fresh evidence-record id of the form ``EV-<12 hex>``.

    Uses :func:`secrets.token_hex` for cryptographic-quality randomness so
    parallel attesters can mint without coordination. The 6-byte body
    (12 hex chars) inherits the suffix-width contract from the rest of
    the state subsystem doubled to give a wider collision-free space for
    high-frequency verify-spine appends.

    Returns:
        Twelve-character hex id prefixed with ``EV-`` (15 chars total).
    """
    return f"EV-{secrets.token_hex(6)}"


class EvidenceRecord(_StrictModel):
    """Typed payload for a ``StoreKind.EVIDENCE`` envelope.

    Every v0.4 verify-spine append — deterministic gate result, jury
    grading, operator attestation — lands as one of these. Downstream
    consumers (close-readiness, compile-gate, waivers) re-validate the
    payload via ``EvidenceRecord.model_validate`` after reading the
    envelope back from ``evidence.jsonl``.

    Attributes:
        id: Stable record id minted by :func:`mint_evidence_id`. Format
            ``EV-<12 hex>``; lives alongside the envelope ``id`` so a
            consumer can route on either without a second JSONL scan.
        scope_id: URN of the phase / iter / wave / decision the evidence
            backs. Matches the envelope ``scope_id`` by convention but
            duplicated on the payload so consumers can re-validate
            without rebuilding the envelope.
        produced_by: Who minted the row — ``human`` for an operator,
            ``agent`` for a subagent, ``tool`` for a deterministic
            checker, ``canary`` for a synthetic seed.
        evidence_kind: Source-family discriminator
            (``deterministic`` / ``jury`` / ``attested``).
        status: Binary outcome (``pass`` / ``fail`` / ``blocked`` /
            ``waived``).
        summary: One-line human description (1-500 chars).
        refs: List of typed references (decision / audit / artifact ids)
            this evidence row substantiates.
        metrics: Optional scalar metrics map (e.g. coverage percent,
            duration seconds, jury votes). Values are JSON-scalar only
            so the row round-trips through the JSONL store losslessly.
        created_at: Timezone-aware UTC timestamp of evidence creation.
    """

    model_config = ConfigDict(extra="forbid")

    id: IdStr
    scope_id: IdStr
    produced_by: ProducedBy
    evidence_kind: EvidenceSourceKind
    status: EvidenceStatus
    summary: Annotated[str, Field(min_length=1, max_length=500)]
    refs: list[IdStr] = Field(default_factory=list)
    metrics: dict[str, int | float | str] | None = None
    created_at: UtcDatetime


def deterministic_pass_record(
    *,
    scope_id: str,
    criterion_id: str,
    gate_id: str,
    tier: int,
    detail: str = "",
) -> EvidenceRecord:
    """Mint a ``deterministic`` / ``pass`` :class:`EvidenceRecord` for a gate.

    The verify-spine close gate calls this once per criterion whose
    cheapest deterministic falsifier passed, so the deterministic-evidence
    pipeline is no longer write-idle: the row it returns is the single
    signal the trust scorecard reads to label a wave ``verified`` (a row
    with ``evidence_kind == "deterministic"`` and ``status == "pass"``).

    The record carries ``produced_by="tool"`` — a deterministic checker,
    not a human or agent — and stamps the originating tier + gate id onto
    ``metrics`` / ``refs`` so a later audit can re-trace which gate at
    which oracle tier minted the row. The id is freshly minted via
    :func:`mint_evidence_id` and ``created_at`` is the current UTC instant.

    Args:
        scope_id: Wave URN the evidence backs (also the envelope scope).
        criterion_id: Id of the criterion the gate scored. Recorded on
            ``refs`` and ``metrics["criterion_id"]`` for traceability.
        gate_id: Id of the deterministic gate that passed. Recorded on
            ``refs`` (first entry) and ``metrics["gate_id"]``.
        tier: Oracle tier (1-7) the producing gate sat at. Stamped onto
            ``metrics["oracle_tier"]`` so an audit can confirm the
            cheapest falsifier minted the row.
        detail: Optional one-line gate-runner detail folded into the
            summary; truncated to keep the ``summary`` under its 500-char
            cap.

    Returns:
        A validated :class:`EvidenceRecord` ready for daemon append.
    """
    summary = f"deterministic gate {gate_id} passed criterion {criterion_id} at tier T{tier}"
    if detail:
        summary = f"{summary}: {detail}"
    summary = summary[:500]
    return EvidenceRecord(
        id=mint_evidence_id(),
        scope_id=scope_id,
        produced_by="tool",
        evidence_kind="deterministic",
        status="pass",
        summary=summary,
        refs=[gate_id, criterion_id],
        metrics={"criterion_id": criterion_id, "gate_id": gate_id, "oracle_tier": tier},
        created_at=datetime.now(UTC),
    )


__all__ = [
    "EvidenceRecord",
    "EvidenceSourceKind",
    "EvidenceStatus",
    "ProducedBy",
    "deterministic_pass_record",
    "mint_evidence_id",
]
