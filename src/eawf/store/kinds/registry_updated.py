"""RegistryUpdatedPayload — typed body for ``StoreKind.REGISTRY_UPDATED`` envelopes.

Emitted by the daemon's ``registry.update`` RPC after a registry
mutation lands (P24-W10). Subscribers (TUI workspace dashboard,
registry-status watchers) filter on
``Envelope.kind == registry_updated`` and re-read the file.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RegistryUpdatedPayload(BaseModel):
    """Payload for :class:`StoreKind.REGISTRY_UPDATED` envelopes.

    Attributes:
        operation: One of ``add`` / ``remove`` / ``rename``. Mirrors
            the JSON-RPC ``operation`` argument so subscribers can
            branch without re-reading the file.
        repo_id: Project-code-shape identifier the operation targets.
        registry_path: Absolute on-disk path of
            ``~/.eawf/registry.json`` (or a test override).
        fields: Operation-specific extras the caller passed (e.g.
            ``{"path": "...", "title": "..."}`` for ``add``;
            ``{"new_code": "..."}`` for ``rename``). Empty for
            ``remove``.
    """

    model_config = ConfigDict(extra="forbid")

    operation: str = Field(min_length=1)
    repo_id: str = Field(min_length=1)
    registry_path: str = Field(min_length=1)
    fields: dict[str, Any] = Field(default_factory=dict)
