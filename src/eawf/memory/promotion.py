"""Promote a session-scoped store record into a memory entry — and back.

Two directions are supported:

1. ``promote_record`` (existing) — store envelope → memory entry. The source
   may be any JSONL store envelope (``research``, ``audit``, ``decision``, …);
   the promotion copies its ``summary`` + payload-body into a new
   ``memory.jsonl`` envelope and mirrors a :class:`MemorySummary` into the
   state cache. The original envelope is preserved unmodified — promotion is
   a forward link, not a delete.

2. :func:`promote_to_artifact` (NEW in W03) — memory entry → durable artifact.
   A memory entry that has matured into a hard rule can be canonised as a
   :class:`~eawf.state.models.Decision` row; the promoter allocates a new
   ``DEC-<UTC-date>-<NN>`` id, writes a ``decision.jsonl`` envelope, mirrors
   a :class:`Decision` into ``state.decisions``, flips the source memory's
   :class:`~eawf.state.enums.MemoryStatus` to ``SUPERSEDED``, and links the
   pair via a fresh memory envelope carrying
   ``payload.promoted_to_artifact_id``.

   v0.1 ships memory→Decision only. Other targets (artifact, backlog,
   hypothesis) are deferred — each would need its own envelope schema.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from eawf.memory.store import MemoryRecord, add_memory, append_envelope, find_envelope
from eawf.state.enums import Confidence, DecisionStatus, MemoryStatus, StoreKind
from eawf.state.models import Decision, MemorySummary, State
from eawf.store.envelope import Envelope

logger = logging.getLogger(__name__)


class PromotionError(ValueError):
    """Raised when the source record is missing, of an unsupported kind, or stale."""


@dataclass(frozen=True)
class PromotionResult:
    """Composite return value for ``promote_record``."""

    record: MemoryRecord
    source_store_record_id: str


@dataclass(frozen=True)
class ArtifactPromotionResult:
    """Composite return value for ``promote_to_artifact``."""

    memory_id: str
    scope_id: str
    artifact_id: str
    artifact_kind: str
    decision: Decision
    decision_envelope: Envelope
    refreshed_memory_envelope: Envelope


def _load_source(store_path: Path, source_id: str) -> Envelope:
    """Return the latest envelope for *source_id* in *store_path*."""
    if not store_path.exists():
        raise PromotionError(f"store file does not exist: {store_path.name}")
    latest: Envelope | None = None
    for line in store_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        env = Envelope.model_validate_json(line)
        if env.id == source_id:
            latest = env
    if latest is None:
        raise PromotionError(f"source record {source_id!r} not found in {store_path.name}")
    return latest


def _extract_body(env: Envelope) -> str:
    """Choose the most informative payload field as the promoted body."""
    payload = env.payload
    for key in ("body", "text", "rationale", "findings", "summary"):
        if key in payload:
            value = payload[key]
            if isinstance(value, str):
                return value
            if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
                return "\n".join(value)
    return env.summary


def promote_record(
    *,
    state: State,
    source_store_path: Path,
    source_id: str,
    memory_path: Path,
    scope_id: str | None = None,
    confidence: Confidence = Confidence.MEDIUM,
    now: datetime | None = None,
) -> PromotionResult:
    """Promote a store record to a memory entry.

    Args:
        state: Loaded :class:`State`; ``state.memory_index`` is mutated in place.
        source_store_path: Path to the JSONL store containing the source record.
        source_id: ID of the source record.
        memory_path: Destination ``memory.jsonl`` path.
        scope_id: Optional override; defaults to the source record's
            ``scope_id`` (and falls back to ``"unscoped"`` when null).
        confidence: Confidence assigned to the new memory entry.
        now: Override for the current time (for tests).

    Returns:
        :class:`PromotionResult` carrying the new memory record and the source
        envelope ID.
    """
    src = _load_source(source_store_path, source_id)
    final_scope = scope_id or src.scope_id or "unscoped"
    body = _extract_body(src)
    title = src.summary if src.summary else source_id

    record = add_memory(
        state=state,
        memory_path=memory_path,
        scope_id=final_scope,
        title=title,
        body=body,
        confidence=confidence,
        now=now,
    )
    logger.info(f"promote_record source={source_id} memory={record.summary.id} scope={final_scope}")
    return PromotionResult(record=record, source_store_record_id=source_id)


def supersede(
    *,
    state: State,
    memory_path: Path,
    old_id: str,
    new_id: str,
) -> None:
    """Mark *old_id* superseded; *new_id* becomes the active replacement.

    Both entries must already exist in ``state.memory_index``. The old entry's
    ``status`` flips to :class:`MemoryStatus.SUPERSEDED`; ``memory.jsonl`` is
    not rewritten — supersession is captured by the cache flip plus compaction
    metadata when ``memory compact`` runs.
    """
    index = state.memory_index or {}
    if old_id not in index:
        raise PromotionError(f"memory entry {old_id!r} not in state.memory_index")
    if new_id not in index:
        raise PromotionError(f"memory entry {new_id!r} not in state.memory_index")
    old = index[old_id]
    index[old_id] = old.model_copy(update={"status": MemoryStatus.SUPERSEDED})
    state.memory_index = index
    logger.info(f"supersede old={old_id} new={new_id}")


_DECISION_KIND_DEFAULT: str = "decision"


def _next_decision_id(
    decisions_path: Path,
    existing: dict[str, Decision] | None,
    now: datetime,
) -> str:
    """Allocate a fresh ``DEC-<UTC-date>-<NN>`` ID avoiding collisions.

    Mirrors :func:`eawf.memory.store._next_memory_id`: scans the in-memory
    state plus the on-disk JSONL for IDs already taken on *now*'s UTC date,
    and returns the first unused two-digit suffix in ``[01, 99]``.
    """
    today = now.strftime("%Y%m%d")
    used: set[int] = set()
    if existing:
        for did in existing:
            if did.startswith(f"DEC-{today}-"):
                try:
                    used.add(int(did.rsplit("-", 1)[-1]))
                except ValueError:
                    continue
    if decisions_path.exists():
        for line in decisions_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                env = Envelope.model_validate_json(line)
            except Exception:
                continue
            if env.id.startswith(f"DEC-{today}-"):
                try:
                    used.add(int(env.id.rsplit("-", 1)[-1]))
                except ValueError:
                    continue
    for n in range(1, 100):
        if n not in used:
            return f"DEC-{today}-{n:02d}"
    raise PromotionError("decision id allocation saturated for today")


def _build_decision_envelope(
    *,
    summary: MemorySummary,
    body: str,
    artifact_id: str,
    now: datetime,
) -> tuple[Decision, Envelope]:
    """Compose the typed :class:`Decision` plus its JSONL envelope."""
    decision_summary = summary.summary
    rationale = body.strip() or summary.summary
    decision = Decision(
        id=artifact_id,
        scope_id=summary.scope_id,
        title=decision_summary,
        rationale=rationale,
        alternatives=[],
        status=DecisionStatus.ACTIVE,
        created_at=now,
        superseded_by=None,
    )
    env = Envelope(
        id=artifact_id,
        kind=StoreKind.DECISION,
        scope_id=summary.scope_id,
        created_at=now,
        updated_at=None,
        summary=f"decision {artifact_id}: {decision_summary[:100]}",
        payload={
            "summary": decision_summary,
            "rationale": rationale,
            "alternatives": [],
        },
        blob_refs=[],
        artifact_ids=[],
    )
    return decision, env


def promote_to_artifact(
    *,
    state: State,
    memory_path: Path,
    decisions_path: Path,
    source_id: str,
    artifact_kind: str = _DECISION_KIND_DEFAULT,
    artifact_id: str | None = None,
    now: datetime | None = None,
) -> ArtifactPromotionResult:
    """Promote a memory entry to a durable artifact (a Decision in v0.1).

    Procedure:

    1. Look up *source_id* in ``state.memory_index``. ``PromotionError`` is
       raised when the entry is missing or already :class:`MemoryStatus.PRUNED`.
    2. Allocate ``DEC-<UTC-date>-<NN>`` (or accept an explicit *artifact_id*).
       The id MUST NOT collide with an existing :class:`Decision` row.
    3. Append a ``decision.jsonl`` envelope summarising the memory.
    4. Append an updated ``memory.jsonl`` envelope so the JSONL replay
       reconstructs the supersession (the latest envelope's
       ``payload.promoted_to_artifact_id`` carries the link).
    5. Mirror a typed :class:`Decision` into ``state.decisions`` and flip the
       source memory's status to :class:`MemoryStatus.SUPERSEDED`, also
       setting ``MemorySummary.promoted_to_artifact_id``.

    The function MUST be called inside the surrounding state-transaction
    block — it mutates *state* in place; the caller persists.

    Args:
        state: Loaded :class:`State`. ``state.memory_index`` and
            ``state.decisions`` are mutated in place.
        memory_path: Path to ``memory.jsonl`` for the supersession envelope.
        decisions_path: Path to ``decision.jsonl`` for the new artifact row.
        source_id: ``MEM-…`` ID to canonise.
        artifact_kind: Target artifact kind. Only ``"decision"`` is
            supported in v0.1; other values raise :class:`PromotionError`.
        artifact_id: Pre-allocated artifact ID; auto-allocated when ``None``.
        now: Override for the current time (for tests).

    Returns:
        :class:`ArtifactPromotionResult` carrying every produced row so the
        caller can emit events / surface IDs.

    Raises:
        PromotionError: When *source_id* is missing, already pruned/promoted,
        the artifact_kind is unsupported, or the supplied *artifact_id*
        collides with an existing decision.
    """
    if artifact_kind != _DECISION_KIND_DEFAULT:
        raise PromotionError(
            f"artifact_kind {artifact_kind!r} is not supported in v0.1; only 'decision' is wired"
        )
    moment = now if now is not None else datetime.now(UTC)
    index = state.memory_index or {}
    if source_id not in index:
        raise PromotionError(f"memory entry {source_id!r} not in state.memory_index")
    summary = index[source_id]
    if summary.status == MemoryStatus.PRUNED:
        raise PromotionError(
            f"memory entry {source_id!r} is PRUNED — refusing to promote a tombstone"
        )

    decisions = dict(state.decisions or {})
    if artifact_id is None:
        artifact_id = _next_decision_id(decisions_path, decisions, moment)
    elif artifact_id in decisions:
        raise PromotionError(f"decision {artifact_id!r} already exists in state.decisions")

    src_env = find_envelope(memory_path, source_id)
    if src_env is None:
        raise PromotionError(f"memory envelope for {source_id!r} not found in {memory_path.name}")
    body_payload = src_env.payload.get("body")
    body_text = str(body_payload) if body_payload is not None else summary.summary

    decision, decision_env = _build_decision_envelope(
        summary=summary,
        body=body_text,
        artifact_id=artifact_id,
        now=moment,
    )

    # Appends happen after we've validated the inputs but before we mutate
    # state.* — the surrounding state_transaction holds portalock(state.json);
    # each JSONL append acquires its own per-file sibling lock so the write
    # ordering is: state-locked → append decision.jsonl → append memory.jsonl
    # → mutate state.{decisions,memory_index} → state_transaction commits.
    append_envelope(decisions_path, decision_env)

    refreshed_payload = dict(src_env.payload)
    refreshed_payload["promoted_to_artifact_id"] = artifact_id
    refreshed_env = src_env.model_copy(
        update={
            "updated_at": moment,
            "payload": refreshed_payload,
        }
    )
    append_envelope(memory_path, refreshed_env)

    decisions[artifact_id] = decision
    state.decisions = decisions
    index[source_id] = summary.model_copy(
        update={
            "status": MemoryStatus.SUPERSEDED,
            "promoted_to_artifact_id": artifact_id,
        }
    )
    state.memory_index = index

    logger.info(
        f"promote_to_artifact source={source_id} artifact={artifact_id} kind={artifact_kind}"
    )
    return ArtifactPromotionResult(
        memory_id=source_id,
        scope_id=summary.scope_id,
        artifact_id=artifact_id,
        artifact_kind=artifact_kind,
        decision=decision,
        decision_envelope=decision_env,
        refreshed_memory_envelope=refreshed_env,
    )
