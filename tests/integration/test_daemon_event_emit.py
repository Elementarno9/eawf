"""Integration: the C09 event union rejects a body that contradicts its tag.

Every typed event the daemon persists is validated through
:data:`eawf.kernel.store.kinds.events.C09EventPayloadUnion` before it is
folded into an envelope, so a payload whose body does not match its
``event_type`` discriminator fails at validation rather than at projection.
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from eawf.kernel.store.kinds.events import C09EventPayloadUnion

_UNION_ADAPTER: TypeAdapter[object] = TypeAdapter(C09EventPayloadUnion)


@pytest.mark.integration
def test_discriminated_union_rejects_tag_body_mismatch() -> None:
    """A body that contradicts its ``event_type`` tag fails fast at append."""
    # A dispatch_cost-shaped body wearing the runtime_switched tag must be
    # rejected by the discriminator dispatch before it could ever persist.
    mismatched = {
        "event_type": "runtime_switched",
        "timestamp": "2026-05-22T00:00:00+00:00",
        "wave_id": "W10",
        "attempt_id": "a-1",
        "runtime": "claude",
        "model": "claude-opus-4-7",
        "input_tokens": 1,
        "output_tokens": 1,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cost_usd": "0.1",
        "pricing_version": "2026.05.17",
    }
    with pytest.raises(ValidationError):
        _UNION_ADAPTER.validate_python(mismatched)
