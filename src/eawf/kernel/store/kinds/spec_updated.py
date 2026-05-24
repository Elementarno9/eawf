"""SpecUpdatedPayload — typed body for ``StoreKind.SPEC_UPDATED`` envelopes.

Emitted by the daemon's ``spec.{init,validate,promote,archive}`` RPCs
(P25-W03 / C03). Subscribers (TUI spec panels, audit DSL ``verify-
implements``) filter on ``Envelope.kind == spec_updated`` and re-read
the cache or the on-disk file as appropriate.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SpecOperationStr = Literal["init", "validate", "promote", "archive"]
SpecStatusStr = Literal["DRAFT", "READY", "IMPLEMENTED", "ARCHIVED"]


class SpecUpdatedPayload(BaseModel):
    """Payload for :class:`StoreKind.SPEC_UPDATED` envelopes.

    Attributes:
        operation: Which spec.* RPC produced the envelope. Mirrors
            the JSON-RPC method tail so subscribers can branch
            without re-reading the cache.
        scope_id: ``P##`` / ``P##-I##`` / ``P##-I##-W##`` —
            the scope the operation targets.
        spec_urn: Full ``urn:eawf:v1:spec:<repo>/<phase>[/...]``
            string. Stable across all four operations for the same
            scope so subscribers can deduplicate by URN.
        status: Lifecycle state AFTER the operation completes. For
            ``validate`` calls this mirrors the unchanged on-disk
            status; for ``init`` / ``promote`` / ``archive`` it
            reflects the transition target.
        file_path: Repo-relative spec file path on disk. Empty
            string after ARCHIVED (the file has been ``git rm``'d).
        file_sha: Git blob SHA of the spec body at this transition;
            empty after ARCHIVED.
    """

    model_config = ConfigDict(extra="forbid")

    operation: SpecOperationStr
    scope_id: str = Field(min_length=1)
    spec_urn: str = Field(min_length=1)
    status: SpecStatusStr
    file_path: str = ""
    file_sha: str = ""
