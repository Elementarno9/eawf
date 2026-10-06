"""Typed params / result models for the ``state.*`` JSON-RPC surface.

Every handler in the state-method family validates its wire params
through one of these strict (``extra="forbid"``) models and returns the
matching result model dumped in JSON mode, so the RPC contract lives in
one place rather than in each handler body.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ---- Params + Result models ------------------------------------------------


class ReadParams(BaseModel):
    """Params for :func:`read`.

    Attributes:
        scope_id: Optional scope filter (not yet enforced; returns
            the full state — projection lands in a later wave).
        fields: Optional projection list (not yet enforced — see above).
        repo_root: Optional absolute path of the repo whose ``state.json``
            the daemon should read. The CLI proxy forwards ``flags.workspace``
            (or ``Path.cwd()``) here so the daemon — which is one per user,
            not one per repo — resolves the right anchor regardless of the
            boot-time cwd. Omitting falls back to ``ctx.state_path`` with a
            one-shot ``daemon_anchor_fallback`` warning.
    """

    model_config = ConfigDict(extra="forbid")
    scope_id: str | None = None
    fields: list[str] | None = None
    repo_root: str | None = None


class ReadResult(BaseModel):
    """Result of :func:`read`.

    The ``state`` field carries the full validated state payload as a
    JSON-mode dict; callers re-validate against
    :class:`eawf.kernel.state.models.State` if they need a typed object.
    """

    model_config = ConfigDict(extra="forbid")
    state: dict[str, Any]
    version: str


class WaveLandParams(BaseModel):
    """Params for :func:`wave_land_rpc`."""

    model_config = ConfigDict(extra="forbid")
    repo_root: str
    wave_id: str
    outcome: str | None = None
    keep_worktree: bool = False


class WaveLandRpcResult(BaseModel):
    """Result of :func:`wave_land_rpc`."""

    model_config = ConfigDict(extra="forbid")
    wave: str
    commits: list[str]
    outcome: str
    closed: bool
    worktree_cleaned: bool
    merged_commit: str
    integration_id: str | None = None
    close_attempt: dict[str, Any] | None = None
    close_backgrounded: bool = False


class WaveLandBatchParams(BaseModel):
    """Params for :func:`wave_land_batch_rpc`."""

    model_config = ConfigDict(extra="forbid")
    repo_root: str
    iter_id: str | None = None
    ready_only: bool = False
    keep_worktree: bool = False


class WaveLandBatchRpcResult(BaseModel):
    """Result of :func:`wave_land_batch_rpc`."""

    model_config = ConfigDict(extra="forbid")
    landed: list[WaveLandRpcResult]
    failed_wave: str | None
    error: str | None
    skipped: list[str]
    barrier_requirements: dict[str, list[str]]
    close_mode: Literal["durable_async", "daemonless_synchronous"]


class WaveAutolandParams(BaseModel):
    """Params for :func:`wave_autoland_rpc`."""

    model_config = ConfigDict(extra="forbid")
    repo_root: str
    iter_id: str | None = None
    keep_worktree: bool = False
    dry_run: bool = False


class WaveAutolandRpcResult(BaseModel):
    """Result of :func:`wave_autoland_rpc`."""

    model_config = ConfigDict(extra="forbid")
    order: list[str]
    landed: list[dict[str, Any]]
    failed_wave: str | None
    error: str | None
    remaining: list[str]
    dry_run: bool


# ---- track.* params + result -------------------------------------------------


class TrackSyncParams(BaseModel):
    """Params for :func:`track_sync_rpc`.

    ``track_id`` names an existing Track whose measured outcome statuses are
    recomputed from their samples (the same reducer the wave-close hook fires).
    An unknown id is a no-op (the reducer returns no changes). When omitted the
    daemon syncs the Track under :attr:`CurrentPointers.track_id`.
    """

    model_config = ConfigDict(extra="forbid")
    repo_root: str | None = None
    track_id: str | None = None


class TrackSyncRpcResult(BaseModel):
    """Result of :func:`track_sync_rpc`."""

    model_config = ConfigDict(extra="forbid")
    track_id: str | None
    changed_outcome_ids: list[str]
    changed: int


class WorktreeReconcileParams(BaseModel):
    """Params for :func:`worktree_reconcile_rpc`."""

    model_config = ConfigDict(extra="forbid")
    repo_root: str = Field(min_length=1)
    dry_run: bool = False


class WorktreeReconcileRow(BaseModel):
    """One row the reconcile retired, or would retire, and why."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["worktree", "session"]
    row_id: str
    reason: Literal["no_git_worktree", "bound_worktrees_gone", "predates_daemon_boot"]


class WorktreeReconcileRpcResult(BaseModel):
    """Result of :func:`worktree_reconcile_rpc`."""

    model_config = ConfigDict(extra="forbid")
    retired: list[WorktreeReconcileRow]
    dry_run: bool
    written: bool


# ---- Idempotency cache ------------------------------------------------------


class CachedMutation(BaseModel):
    """One row in the daemon's in-memory idempotency cache.

    Stored verbatim under :class:`MethodContext.idempotency_cache` (a
    plain dict keyed by ``idempotency_key``). Entries older than
    :data:`IDEMPOTENCY_TTL_SECONDS` are pruned on every lookup; the
    durable replay guarantee lives in the WAL, not here.

    Attributes:
        result: The :class:`MutateResult` dict returned to the original
            caller. On replay this is returned verbatim with
            ``idempotent_replay=True`` flipped on.
        cached_at: ``time.monotonic()`` value when the entry was
            written; used for TTL eviction.
    """

    model_config = ConfigDict(extra="forbid")
    result: dict[str, Any]
    cached_at: float = Field(ge=0.0)
