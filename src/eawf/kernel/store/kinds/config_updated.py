"""ConfigUpdatedPayload — typed body for ``StoreKind.CONFIG_UPDATED`` envelopes.

Emitted by the daemon's ``config.set_layer_value`` RPC after a
layered-config YAML write lands (P24-W10). Subscribers (TUI config
pane, watchers, hot-reload hooks) filter on
``Envelope.kind == config_updated`` and re-read the affected layer.

Per the C02 §5.13 envelope contract the payload is a typed Pydantic
model with ``extra="forbid"`` so a future field addition stays an
explicit schema bump, not silent drift.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ConfigUpdatedPayload(BaseModel):
    """Payload for :class:`StoreKind.CONFIG_UPDATED` envelopes.

    Attributes:
        layer: Canonical writable-layer label
            (``global`` | ``workspace`` | ``repo`` | ``local``).
        layer_path: Absolute on-disk path of the YAML layer that
            received the write — repo-relative-like; subscribers
            normalise as needed.
        key_path: Dotted key segments that were set (list form keeps
            the wire encoding unambiguous when a segment contains
            ``.``).
        value: Typed value that was written. ``Any`` because the
            registry-validated config schema covers scalar +
            list/dict types.
    """

    model_config = ConfigDict(extra="forbid")

    layer: str = Field(min_length=1)
    layer_path: str = Field(min_length=1)
    key_path: list[str] = Field(min_length=1)
    value: Any
