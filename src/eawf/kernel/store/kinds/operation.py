"""OperationRecord -- payload for :attr:`StoreKind.OPERATION` records.

A long-running verb is submitted rather than awaited: the daemon records the
operation, answers with its reference at once, and runs the work in the
background. Every state the operation passes through is one row, so a
follower reads the trail from any cursor and a crash leaves the last state it
reached on disk rather than nothing.

The rows live in the machine-local store tier (``<state_dir>/local/``): an
operation is the work one daemon on one machine was asked to do, not a fact
about the repository.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from eawf.kernel.state.types import UtcDatetime


class OperationState(StrEnum):
    """Where one submitted operation stands.

    Values:
        QUEUED: Accepted and recorded; the work has not started.
        RUNNING: The work is under way.
        SUCCEEDED: The work answered; the record carries the answer.
        FAILED: The work was refused or raised; the record carries the error.
        UNKNOWN: The daemon running the work stopped before recording an
            outcome, so whether the work finished cannot be told.
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


#: The states no later row can follow.
TERMINAL_STATES: Final[frozenset[OperationState]] = frozenset(
    {OperationState.SUCCEEDED, OperationState.FAILED, OperationState.UNKNOWN}
)

_Token = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=512)]


class OperationError(BaseModel):
    """The JSON-RPC error one failed operation answered with.

    Attributes:
        code: The JSON-RPC error code, the same one a direct call returns.
        message: The error message, carried unchanged.
        data: The structured error payload, when the error carried one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: int
    message: str
    data: dict[str, Any] | None = None


class OperationRecord(BaseModel):
    """One revision of one submitted operation.

    Attributes:
        schema_version: Record schema tag.
        operation_id: Stable identity across every revision of the operation.
        verb: The dotted JSON-RPC name of the work the operation runs.
        subject: The entity the work addresses.
        idempotency_key: The caller's key; a resubmission under the same key
            names the same operation.
        request_fingerprint: Digest of the request that opened the operation,
            so the same key carrying a different request is refused.
        state: Where the operation stands at this revision.
        revision: 0 at submission, one more per recorded state; a follower's
            cursor.
        submitted_at: When the operation was accepted.
        updated_at: When this revision was recorded.
        daemon_instance: The daemon process running the work, or ``None`` when
            no daemon runs it, so a non-terminal row whose daemon is gone reads
            as unknown rather than as running forever.
        result: The answer the work returned, present on success.
        error: The error the work answered with, present only on failure.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["operation/v1"] = "operation/v1"
    operation_id: UUID
    verb: _Token
    subject: _Token
    idempotency_key: _Token
    request_fingerprint: str | None = None
    state: OperationState
    revision: Annotated[int, Field(ge=0)]
    submitted_at: UtcDatetime
    updated_at: UtcDatetime
    daemon_instance: str | None = None
    result: dict[str, Any] | None = None
    error: OperationError | None = None

    @model_validator(mode="after")
    def _outcome_matches_state(self) -> OperationRecord:
        """Refuse an outcome the state does not carry.

        Raises:
            ValueError: When a success carries no answer, or an error sits
                on a state other than failed.
        """
        if self.state is OperationState.SUCCEEDED and self.result is None:
            raise ValueError("a succeeded operation carries its result")
        if self.error is not None and self.state is not OperationState.FAILED:
            raise ValueError(f"an error cannot sit on a {self.state.value} operation")
        return self


__all__ = ["TERMINAL_STATES", "OperationError", "OperationRecord", "OperationState"]
