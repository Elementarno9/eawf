"""Operator-only per-gate waiver semantics (P28-I01-W11).

A *waiver* lets an operator (NOT an agent) declare that a specific
failing :class:`~eawf.kernel.spec.common.GateSpec` is cleared via an
attestation. The waiver is recorded as a typed
:class:`~eawf.kernel.store.kinds.evidence.EvidenceRecord` whose
``produced_by="human"`` + ``status="waived"`` shape is the only
evidence the close-readiness compute (W06) consults when deciding
whether a gate counts as satisfied.

The :func:`apply_waiver` helper composes the record, enforces the
mode-gated linkage policy (see :data:`WaiverMode`), confirms the
authoring session is operator-owned (rejecting agent sessions), stamps
the wave's current SHA into ``metrics["wave_sha"]`` for SHA-bound
freshness, and persists the row via the daemon ``evidence.append``
RPC. A direct ``evidence.jsonl`` append behind the
``EAWF_EVIDENCE_DIRECT_WRITE=1`` env gate mirrors the W04 fallback so
CI / recovery shells without a daemon can still attest.

Mode gating (resolved from ``profile.verify.waiver_mode`` in the
merged config; default ``B`` when the leaf is absent):

* ``A`` (loose) — no reason, no decision/audit reference required.
  The summary falls back to ``"(no reason)"`` so the strict
  :class:`EvidenceRecord` validator still accepts the row. Operator
  identity is still recorded.
* ``B`` (default) — ``--reason`` required; decision / audit refs
  optional.
* ``C`` (collaborative) — ``--reason`` required AND at least one of
  ``--decision`` / ``--audit`` required. Mode C makes the operator's
  authorship explicit + traceable.

Bare ``--waive`` with no ``--reason`` is rejected upstream at the CLI
parse layer in modes B and C; :func:`apply_waiver` re-checks the
invariant defensively (see :class:`WaiverInput`).
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict
from pydantic import ValidationError as PydValidationError

from eawf.kernel.spec.common import _StrictModel
from eawf.kernel.state.enums import AgentSessionRole, StoreKind
from eawf.kernel.state.models import IdStr, State
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.evidence import EvidenceRecord, mint_evidence_id
from eawf.kernel.store.paths import store_path
from eawf.platform.profiles.models import VerifyBlock
from eawf.surfaces.cli import errors as cli_errors
from eawf.workflow.lifecycle.wave_sha import derive_wave_sha

logger = logging.getLogger(__name__)


#: Closed mode literal for waiver linkage policy. See module docstring.
WaiverMode = Literal["A", "B", "C", "disabled"]


#: Default waiver mode applied when ``profile.verify.waiver_mode`` is
#: absent from the merged config. Documented in the module docstring +
#: pinned by the test suite so a future config-default change is loud.
DEFAULT_WAIVER_MODE: WaiverMode = "B"


#: Fallback summary used when mode A allows a reason-less waiver. The
#: literal "(no reason)" satisfies the strict
#: :class:`EvidenceRecord.summary` min-length=1 validator without
#: forcing a reason on mode A operators.
WAIVER_NO_REASON_SUMMARY: str = "(no reason)"


#: Environment-variable gate for the direct-JSONL append fallback —
#: mirrors :data:`eawf.surfaces.cli.commands.evidence.EVIDENCE_DIRECT_WRITE_ENV`.
#: When set to ``"1"`` :func:`apply_waiver` writes the row to disk
#: directly; otherwise it returns the validated record without writing
#: and lets the CLI proxy through the daemon RPC.
EVIDENCE_DIRECT_WRITE_ENV: str = "EAWF_EVIDENCE_DIRECT_WRITE"


class WaiverInput(_StrictModel):
    """One operator-supplied waiver request for a single gate.

    Carries the gate id the operator wants cleared, the reason text,
    and at most one of a decision or audit reference. The model is the
    boundary the CLI handler passes to :func:`apply_waiver` so the
    library never sees raw ``argv`` strings.

    Attributes:
        gate_id: Stable id of the gate being waived (must match the
            gate's :attr:`~eawf.kernel.spec.common.GateSpec.id`).
        reason: Human-supplied justification. Must be non-empty in
            modes B and C; may be ``None`` in mode A (the persisted
            row falls back to :data:`WAIVER_NO_REASON_SUMMARY`).
        decision_ref: Optional decision URN this waiver leans on.
        audit_ref: Optional audit URN this waiver leans on.
    """

    model_config = ConfigDict(extra="forbid")

    gate_id: IdStr
    reason: str | None = None
    decision_ref: IdStr | None = None
    audit_ref: IdStr | None = None


def resolve_waiver_mode(
    source: dict[str, object] | VerifyBlock | None,
) -> WaiverMode:
    """Return the active :data:`WaiverMode` from a config dict or VerifyBlock.

    Two input shapes are accepted:

    * **Typed** — a :class:`~eawf.platform.profiles.models.VerifyBlock`
      (P28-I01-W10). The helper reads
      :attr:`~eawf.platform.profiles.models.VerifyBlock.waiver_mode`
      directly; the typed default (``"B"``) means the helper never
      surfaces ``DEFAULT_WAIVER_MODE`` for a typed input.
    * **Layered config** — the ``dict[str, object]`` returned by
      :func:`eawf.kernel.config.layered.merge_config` (the first
      element of its returned tuple). The helper looks under
      ``verify.waiver_mode``; the leaf is absent on every project
      until the layered-config writer wires it in, so this path
      returns :data:`DEFAULT_WAIVER_MODE` in v0.4.0.

    Args:
        source: Either a typed :class:`VerifyBlock`, a layered-config
            dict, or ``None`` (treated as "layer unavailable").

    Returns:
        The validated :data:`WaiverMode`. Unknown values on the
        dict-style path fall back to :data:`DEFAULT_WAIVER_MODE` so a
        typo cannot silently flip the policy.
    """
    if source is None:
        return DEFAULT_WAIVER_MODE
    if isinstance(source, VerifyBlock):
        return source.waiver_mode
    verify_cfg = source.get("verify")
    if not isinstance(verify_cfg, dict):
        return DEFAULT_WAIVER_MODE
    value = verify_cfg.get("waiver_mode")
    if value == "A":
        return "A"
    if value == "B":
        return "B"
    if value == "C":
        return "C"
    if value == "disabled":
        return "disabled"
    if value is not None:
        logger.warning(
            f"resolve_waiver_mode status='unknown-value' value={value!r} "
            f"defaulting_to={DEFAULT_WAIVER_MODE!r}"
        )
    return DEFAULT_WAIVER_MODE


def _validate_linkage(waiver: WaiverInput, mode: WaiverMode) -> None:
    """Raise :class:`ValidationError` when *waiver* fails the *mode* policy.

    Args:
        waiver: The operator-supplied waiver request.
        mode: Active :data:`WaiverMode`.

    Raises:
        cli_errors.ValidationError: When the linkage policy rejects
            *waiver* (missing reason in modes B/C, missing
            decision/audit in mode C).
    """
    reason_required = mode in ("B", "C")
    if reason_required and (waiver.reason is None or waiver.reason == ""):
        raise cli_errors.ValidationError(
            f"waiver for gate {waiver.gate_id!r} requires --reason in mode {mode}"
        )
    if mode == "C" and waiver.decision_ref is None and waiver.audit_ref is None:
        raise cli_errors.ValidationError(
            f"waiver for gate {waiver.gate_id!r} requires --decision or --audit in mode C"
        )


def _resolve_operator_session(state: State, *, operator_identity: str | None) -> None:
    """Confirm the active session subject is an operator.

    The v0.4.0 contract: a waiver carries the operator's session id in
    *operator_identity* — typically pulled from
    ``state.current.active_session_ids[0]``. The session MUST exist on
    :attr:`State.agent_sessions` and carry
    :attr:`AgentSessionRole.OPERATOR`. Agent sessions (executor,
    auditor, etc.) are rejected per the operator-only contract.

    Args:
        state: Validated state model.
        operator_identity: The session id claimed as the waiver author.
            ``None`` is treated as "no operator session attached" and
            rejected.

    Raises:
        cli_errors.ValidationError: When the session is missing,
            unknown, or attached to an agent role.
    """
    if operator_identity is None or operator_identity == "":
        raise cli_errors.ValidationError(
            "waiver requires operator session; no active session attached to state"
        )
    session = state.agent_sessions.get(operator_identity)
    if session is None:
        raise cli_errors.ValidationError(
            f"waiver requires operator session; unknown session {operator_identity!r}"
        )
    if session.role is not AgentSessionRole.OPERATOR:
        role_value = session.role.value
        raise cli_errors.ValidationError(
            f"waiver requires operator session; agent sessions cannot waive "
            f"(session={operator_identity!r} role={role_value!r})"
        )


def _build_waiver_record(
    *,
    wave_id: str,
    waiver: WaiverInput,
    operator_identity: str,
    wave_sha: str | None,
) -> EvidenceRecord:
    """Compose the typed :class:`EvidenceRecord` for *waiver*.

    The record carries:

    * ``produced_by="human"`` — the operator-only contract.
    * ``status="waived"`` — drives the W06 readiness ``waived_gate_ids``
      tally.
    * ``evidence_kind="attested"`` — the source-family for a human
      attestation per the spec.common literal.
    * ``refs=[gate_id, ...]`` — the waived gate id plus any
      ``decision`` / ``audit`` URN the operator linked.
    * ``summary`` — the operator's reason (or
      :data:`WAIVER_NO_REASON_SUMMARY` in mode A).
    * ``metrics={"wave_sha": <sha>, "operator_session": <id>}`` —
      ``wave_sha`` powers SHA-bound freshness (W11) so a stale waiver
      against an old SHA is filtered out of the readiness view;
      ``operator_session`` records the authoring session id so
      downstream review can re-trace who waived.

    Args:
        wave_id: Wave id the waiver is scoped to.
        waiver: The operator-supplied waiver request.
        operator_identity: Session id of the waiving operator (already
            validated by :func:`_resolve_operator_session`).
        wave_sha: Current wave SHA (may be ``None`` when the wave has
            not yet been committed — the record still persists and
            readiness treats a missing-sha row as fresh).

    Returns:
        Validated :class:`EvidenceRecord` ready for daemon append.

    Raises:
        cli_errors.ValidationError: When the composed record fails the
            :class:`EvidenceRecord` schema (e.g. invalid ref pattern).
    """
    refs: list[str] = [waiver.gate_id]
    if waiver.decision_ref is not None and waiver.decision_ref != "":
        refs.append(waiver.decision_ref)
    if waiver.audit_ref is not None and waiver.audit_ref != "":
        refs.append(waiver.audit_ref)
    summary = waiver.reason if waiver.reason else WAIVER_NO_REASON_SUMMARY
    metrics: dict[str, int | float | str] = {"operator_session": operator_identity}
    if wave_sha is not None:
        metrics["wave_sha"] = wave_sha
    try:
        return EvidenceRecord(
            id=mint_evidence_id(),
            scope_id=wave_id,
            produced_by="human",
            evidence_kind="attested",
            status="waived",
            summary=summary,
            refs=refs,
            metrics=metrics,
            created_at=datetime.now(UTC),
        )
    except PydValidationError as exc:
        raise cli_errors.ValidationError(f"waiver record failed validation: {exc}") from exc


def _append_direct(record: EvidenceRecord, *, state_path: Path) -> None:
    """Append *record* directly to ``<state_dir>/store/evidence.jsonl``.

    Mirrors :func:`eawf.surfaces.cli.commands.evidence._append_direct`
    so the on-disk shape is indistinguishable from a daemon-written row.
    Used only when :data:`EVIDENCE_DIRECT_WRITE_ENV` is set; the daemon
    path is the canonical writer per AGENTS rule 4.

    Args:
        record: Validated evidence record to append.
        state_path: Path to ``state.json`` (anchors the store dir).
    """
    evidence_path = store_path(state_path, StoreKind.EVIDENCE)
    envelope = Envelope(
        id=record.id,
        kind=StoreKind.EVIDENCE,
        scope_id=record.scope_id,
        created_at=record.created_at,
        summary=record.summary,
        payload=record.model_dump(mode="json"),
    )
    append_envelope(evidence_path, envelope)


def _direct_write_enabled() -> bool:
    """Return ``True`` when the direct-JSONL append fallback is opted in."""
    return os.environ.get(EVIDENCE_DIRECT_WRITE_ENV) == "1"


def apply_waiver(
    state: State,
    *,
    wave_id: str,
    waiver: WaiverInput,
    operator_identity: str | None,
    mode: WaiverMode,
    state_path: Path | None = None,
    repo_root: Path | None = None,
) -> EvidenceRecord:
    """Build, validate, and persist one operator waiver for a single gate.

    The function:

    1. Enforces the operator-only contract via
       :func:`_resolve_operator_session` (rejects agent sessions).
    2. Enforces the mode-gated linkage policy via
       :func:`_validate_linkage` (reason required in B/C; decision or
       audit required in C).
    3. Derives the current wave SHA via
       :func:`~eawf.workflow.lifecycle.wave_sha.derive_wave_sha` and
       stamps it into ``metrics["wave_sha"]``.
    4. Persists the row through the direct-JSONL fallback when
       :data:`EVIDENCE_DIRECT_WRITE_ENV` is set; otherwise returns the
       record without writing (the CLI handler proxies through the
       daemon ``evidence.append`` RPC in that case).

    Args:
        state: Validated state model. Read-only; the function does not
            mutate state.
        wave_id: Wave id the waiver is scoped to.
        waiver: Typed :class:`WaiverInput` (one gate per call).
        operator_identity: Session id of the waiving operator.
        mode: Resolved :data:`WaiverMode`.
        state_path: Path to ``state.json``. Required when the direct-
            write fallback is enabled; ignored when the CLI proxies
            through the daemon RPC.
        repo_root: Optional repository root forwarded to
            :func:`derive_wave_sha`. ``None`` defers to git's CWD.

    Returns:
        The validated (and, when the fallback is enabled, persisted)
        :class:`EvidenceRecord`.

    Raises:
        cli_errors.ValidationError: When the operator-only contract or
            the linkage policy reject the waiver, or when the composed
            record fails strict schema validation.
        RuntimeError: When the direct-write fallback is enabled but
            *state_path* is not supplied.
    """
    _resolve_operator_session(state, operator_identity=operator_identity)
    assert operator_identity is not None  # _resolve_operator_session rejected None
    _validate_linkage(waiver, mode)

    wave_sha = derive_wave_sha(wave_id, repo_root=repo_root)
    record = _build_waiver_record(
        wave_id=wave_id,
        waiver=waiver,
        operator_identity=operator_identity,
        wave_sha=wave_sha,
    )

    produced_by = record.produced_by
    logger.info(
        f"apply_waiver wave={wave_id!r} gate_id={waiver.gate_id!r} "
        f"produced_by={produced_by!r} mode={mode!r} status='built' "
        f"evidence_id={record.id!r}"
    )

    if _direct_write_enabled():
        if state_path is None:
            raise RuntimeError(f"{EVIDENCE_DIRECT_WRITE_ENV}=1 requires state_path on apply_waiver")
        _append_direct(record, state_path=state_path)
        logger.info(
            f"apply_waiver wave={wave_id!r} gate_id={waiver.gate_id!r} "
            f"status='persisted-direct' evidence_id={record.id!r}"
        )

    return record


__all__ = [
    "DEFAULT_WAIVER_MODE",
    "EVIDENCE_DIRECT_WRITE_ENV",
    "WAIVER_NO_REASON_SUMMARY",
    "WaiverInput",
    "WaiverMode",
    "apply_waiver",
    "resolve_waiver_mode",
]
